"""Always-on, redacted file logging for the package.

All modules log to children of the ``security_notebook_tools`` logger (``.auth``, ``.config``,
``.sentinel``, ``.adx``, ``.timerange``, ``.tablepolicy``, ``.query``, ``.batch``, ``.format``,
``.pivot`` and ``.magic``). Every record at INFO and above is appended to ``notebooks.log`` in the
project root and nothing is printed to the notebook cell, unless a call is made with
``debug=True`` (see below). The project root is the first
directory, walking up from the current working directory, that contains ``.git`` or
``pyproject.toml``; the walk never goes above the user's home directory, and if there is no
marker the working directory itself is used. The path is resolved on every record (the root is
cached per working directory), so ``os.chdir`` in a notebook is respected, and the file is only
created when the first record is written, with mode 0600. It is opened without following
symbolic links and records are dropped unless it is a regular file owned by the current user
with a single link. Add ``notebooks.log`` to ``.gitignore``.

The same handler is attached to the ``azure`` logger namespace (``azure.core``,
``azure.identity``, ``azure.monitor`` ...) but only passes WARNING and above from there, so the
SDKs' warnings and errors reach the file and their request/response chatter does not. Their
logger levels are not changed. The Kusto SDK (``azure.kusto``) does not log.

Each line is ``<UTC RFC 3339 timestamp> <LEVEL> <logger> [<notebook>]: <message>``, for
example::

    2026-10-23T14:03:12.345Z INFO security_notebook_tools.sentinel [hunt.ipynb]: Running ...

The notebook is the file name (no directory) of the notebook the kernel runs, so lines from
several notebooks sharing one ``notebooks.log`` can be told apart (see :func:`notebook_name`).
When it is unknown (a plain script or console, or a front end that does not say) the
`` [<notebook>]`` part is left out.

Levels, the same in every module:

* DEBUG: internal detail, such as reusing a cached client, credential or table policy and the
  start of each dependency call. Never written to the file.
* INFO: an operation starting or completing: a public function's entry and result summary (rows
  and columns, duration), a result cache hit, miss or bypass, a client or credential created,
  each dependency call completing with its duration, the cost guard passing, the table policy
  fetched, ``clear_cache``, a ``%%kql`` run, the extension loading.
* WARNING: recoverable or refused: a dependency call that failed (exception type and redacted
  message, no traceback), each batch retry, partial results, and a call rejected by argument
  validation or the cost guard (``ConfigurationError``, ``%%kql`` ``UsageError``).
* ERROR: the operation failed and an exception reaches the user. Logged once per failed public
  call, at the public boundary (:func:`security_notebook_tools._errors.public_call`). An error
  nobody anticipated is logged once, with its traceback, by ``unexpected``; the boundary then
  stays silent.

Nothing sensitive is written. Call sites log fingerprints instead of KQL text (a prefix of a
keyed HMAC-SHA256 with a per-process random key, and the length, :func:`fingerprint`), mask
names that come from the environment (:func:`mask_name`, :func:`mask_id`), never log result
values, environment variable values, HTTP headers, bodies or query strings, credentials,
tokens, request properties or the notebook's namespace (only the notebook's file name is
read from it, see :func:`notebook_name`), describe a ``timespan`` by its
duration or dates only (:func:`describe_timespan`), pass fields through :func:`format_fields`
(which writes only plain values and ``<TypeName>`` for anything else), and log SDK exception
messages only through :func:`redact_exception_text` (best effort: quoted text, known secret
shapes and identifiers are removed). Line breaks in a message are escaped. As a
backstop the file formatter runs :func:`redact` over every line, traceback included, and renders
tracebacks itself: each frame is a file, line and function name without source text, and each
exception message is shortened and stripped of quoted strings.

Failures to open or write the file are ignored so logging never breaks a query.

Debug output (``debug=True``): ``exec_query``, ``exec_batch_query``, ``format_dataframe``,
``pivot_dataframe`` and ``%%kql --debug`` open a debug scope for the duration of the call (see
:func:`debug_scope`). While it is active, every package record at DEBUG and above is also
printed to that notebook cell's output, unredacted (same line format as the file), whichever
function or thread logs it: nested public calls inherit the scope and the batch's worker
threads run in a copy of the caller's context. The scope lives in a ``contextvars.ContextVar``,
so a concurrent call (another thread, another task) that did not ask for it prints nothing.
The package logger itself is at DEBUG so the records exist; the file handler stays at INFO, so
debug records never reach ``notebooks.log``, and the cell handler only passes records while a
scope is active. It writes to the ``sys.stdout`` the scope captured on entry (Jupyter points it
at the running cell) and only ever receives the package's own records, never the ``azure``
loggers'. Nothing is printed outside such a scope.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import stat
import sys
import threading
import time
import traceback
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Any

PACKAGE_LOGGER = "security_notebook_tools"
THIRD_PARTY_LOGGERS = ("azure",)
LOG_FILE_NAME = "notebooks.log"
LOG_FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s%(snt_notebook)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
_ROOT_MARKERS = (".git", "pyproject.toml")
_EXCEPTION_TEXT_LIMIT = 200
_TRACEBACK_TEXT_LIMIT = 300
_MAX_CHAIN = 20
_LOGGED_ATTR = "_snt_failure_logged"
_NOTEBOOK_ATTR = "snt_notebook"

# --- Redaction ---------------------------------------------------------------------------------

_MAX_INPUT = 4096  # every redaction pass works on at most this many characters per text
_REDACTED = "<redacted>"
_SCHEME = r"[A-Za-z][A-Za-z0-9+.-]{0,30}://"
_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_HEADERS = (
    "authorization|proxy-authorization|cookie|set-cookie|ocp-apim-subscription-key|"
    "x-functions-key|x-api-key|x-ms-authorization-auxiliary"
)
_HEADER = re.compile(
    rf"(?<![A-Za-z0-9-])(?P<key>{_HEADERS})(?P<q>[\"']?)[ \t]*[:=][ \t]*[^\n]*",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_SAS = re.compile(r"\bSharedAccessSignature\s+\S+", re.IGNORECASE)
_URL_USERINFO = re.compile(rf"(?P<scheme>\b{_SCHEME})[^/\s@'\"<>]+@")
_URL_QUERY = re.compile(rf"(?P<base>\b{_SCHEME}[^\s?#'\"<>]*)[?#](?!<redacted>)[^\s'\"]*")
_BARE_QUERY = re.compile(r"(?<=[\w/.\]-])\?(?=[\w.%\[\]-]+=)(?!<redacted>)[^\s'\"]*")
# A key is a word (any prefix, any case, camelCase too) ending in one of these, or exactly "code".
_KEY_SUFFIXES = (
    "token|secret|password|passwd|pwd|api[_-]?key|credential|sig|signature|"
    "accountkey|sharedaccesskey|sharedaccesssignature|key"
)
_STRONG_SUFFIXES = (
    "token", "secret", "password", "passwd", "pwd", "apikey", "api_key", "api-key",
    "credential", "sig", "signature", "accountkey", "sharedaccesskey", "sharedaccesssignature",
    "secretkey", "privatekey", "accesskey",
)  # fmt: skip
_KEY_VALUE = re.compile(
    rf"(?<![A-Za-z0-9])(?P<key>[A-Za-z0-9_.-]{{0,30}}?(?:{_KEY_SUFFIXES})|code)"
    r"""(?P<q1>["']?)\s*(?P<sep>[:=]|%3[Dd])\s*"""
    r"""(?:(?P<q2>["'])(?P<quoted>[^"'\n]*)(?P=q2)|(?P<bare>[^\s&;,'"}\]]+))""",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"(?<![\w.%+-])[\w.%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_ARM_SEGMENT = re.compile(r"(/(?:subscriptions|resourceGroups|workspaces)/)[^/\s'\"?]+", re.I)
_GUID = re.compile(
    r"(?<![0-9A-Za-z])([0-9A-Fa-f]{4})[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Za-z])"
)
_GUID_FULL = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
_IPV6 = re.compile(
    r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f]{1,4})?(?![\w:])"
)
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")
_HOME = re.compile(
    r"(?P<pre>/home/|/Users/|\b[A-Za-z]:[\\/]+Users[\\/]+)(?P<user>[^/\\\s'\":]+)", re.IGNORECASE
)
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")
_HEX = frozenset("0123456789abcdefABCDEF")
_PREFIX_LETTERS = frozenset("bBrRuUfFhH@")


class SafeText(str):
    """Text a helper has already made safe to log (a fingerprint, a masked name ...).

    :func:`format_fields` writes only simple values and these; everything else becomes
    ``<TypeName>``.
    """

    __slots__ = ()


def _mask_key_value(match: re.Match[str]) -> str:
    key, sep = match["key"].lower(), match["sep"]
    if key == "code":
        before = match.string[match.start() - 1 : match.start()]
        if before in ("_", "-", ".") or (sep != "=" and not match["q1"]):
            return match[0]  # status_code=403 / "error code: 400" are ordinary text
    elif not key.endswith(_STRONG_SUFFIXES) and sep != "=" and not match["q1"]:
        return match[0]  # "...key: x" in prose; only key=value or {"key": ...} is masked
    return f"{match['key']}{match['q1']}{sep}{' ' if sep == ':' else ''}{_REDACTED}"


def _mask_ip(match: re.Match[str]) -> str:
    text = match[0]
    if not any(c in _HEX for c in text):
        return text
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return text
    return "<ip>"


def _mask_long_token(match: re.Match[str]) -> str:
    text = match[0]
    if all(c in _HEX for c in text) or (
        any(c.isdigit() for c in text)
        and any(c.isupper() for c in text)
        and any(c.islower() for c in text)
    ):
        return "<secret>"
    return text


def redact(text: str) -> str:
    r"""Mask secrets and personal data in ``text`` (safe to call more than once).

    Masked: JWTs and other bearer tokens, the values of ``Authorization``, ``Cookie``,
    ``Set-Cookie`` and API key headers (to the end of the line), ``SharedAccessSignature``
    values, the query string and user info of URLs, key-like parameters (a word ending in
    ``token``, ``secret``, ``password``, ``key``, ``sig``, ``credential`` ... in any case,
    followed by ``=``, ``:`` or ``%3D``; ``key`` and ``code`` only as ``key=value`` or in
    quoted JSON form), email addresses, IPv4 and IPv6 addresses (candidates are checked with
    :mod:`ipaddress`, so ``14:02:11`` or ``1.2.3`` stay), the subscription, resource group and
    workspace segments of Azure resource paths, GUIDs (only the first four hex digits are
    kept, ``1a2b…``), the user name in home directories (``/home/<user>/``,
    ``C:\Users\<user>\``) and long random-looking strings. Input beyond 4096 characters is cut
    off first, which bounds the running time.

    Args:
        text: The text to redact.

    Returns:
        The text with the matches replaced.
    """
    if len(text) > _MAX_INPUT:
        text = text[:_MAX_INPUT] + "…"
    text = _JWT.sub("<token>", text)
    text = _HEADER.sub(lambda m: f"{m['key']}{m['q']}: {_REDACTED}", text)
    text = _BEARER.sub(f"Bearer {_REDACTED}", text)
    text = _SAS.sub(f"SharedAccessSignature {_REDACTED}", text)
    text = _URL_USERINFO.sub(rf"\g<scheme>{_REDACTED}@", text)
    text = _URL_QUERY.sub(r"\g<base>?<redacted>", text)
    text = _BARE_QUERY.sub("?<redacted>", text)
    text = _KEY_VALUE.sub(_mask_key_value, text)
    text = _EMAIL.sub("<email>", text)
    text = _ARM_SEGMENT.sub(r"\1<name>", text)
    text = _GUID.sub("\\1…", text)
    text = _IPV6.sub(_mask_ip, text)
    text = _IPV4.sub(_mask_ip, text)
    text = _HOME.sub(r"\g<pre><user>", text)
    return _LONG_TOKEN.sub(_mask_long_token, text)


def _is_word(char: str) -> bool:
    return char.isalnum() or char == "_"


def _opening(text: str, i: int) -> int | None:
    """If the quote at ``i`` opens a literal, the number of prefix letters before it, else None."""
    k = 0
    while k < 2 and i - 1 - k >= 0 and text[i - 1 - k] in _PREFIX_LETTERS:
        k += 1
    before = i - 1 - k
    if before >= 0 and _is_word(text[before]):
        return None  # an apostrophe inside a word: doesn't, user's
    return k


def _strip_quoted(text: str) -> str:
    """Replace every quoted literal with ``'…'``; text after an unclosed quote is cut off.

    Quotes are ``'``, ``"`` and a backtick. A quote opens a literal when the character before
    it (or before its prefix letters: ``b'..'``, ``r'..'``, ``h'..'``, ``@'..'`` ...) is not a
    word character, and closes at the next same quote that no word character follows (so
    ``'user's name'`` is one literal). Linear time.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        prefix = _opening(text, i) if char in "'\"`" else None
        if prefix is None:
            out.append(char)
            i += 1
            continue
        del out[len(out) - prefix :]
        j = i + 1
        while j < n:
            if text[j] == "\\":
                j += 2
            elif text[j] == char and not (j + 1 < n and _is_word(text[j + 1])):
                break
            else:
                j += 1
        if j >= n:
            out.append(char + "…")
            break
        out.append(f"{char}…{char}")
        i = j + 1
    return "".join(out)


def redact_exception_text(exc: BaseException | str, limit: int = _EXCEPTION_TEXT_LIMIT) -> str:
    """Make an exception's message safe to log: no quoted text, redacted, one short line.

    SDK messages often echo fragments of the KQL, usually in quotes, so every quoted literal
    (``'...'``, ``"..."`` or a backtick pair, with an optional ``b``/``r``/``u``/``f``/``h``/``@``
    prefix) becomes ``'…'`` and text after an unbalanced quote is cut off, before :func:`redact`
    runs. The result is put on one line and truncated to ``limit`` characters. The exception
    raised to the user is not affected, only what is logged. This is best effort: unquoted
    fragments of a message are kept.

    Args:
        exc: The exception, or its message.
        limit: Maximum length of the result.

    Returns:
        The sanitised text, possibly empty.
    """
    text = str(exc)
    if len(text) > _MAX_INPUT:
        text = text[:_MAX_INPUT] + "…"
    flat = " ".join(redact(_strip_quoted(text)).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def describe_exception(exc: BaseException, limit: int = _EXCEPTION_TEXT_LIMIT) -> str:
    """Return ``"TypeName: sanitised message"`` for ``exc`` (see :func:`redact_exception_text`)."""
    name = type(exc).__name__
    text = redact_exception_text(exc, limit)
    return f"{name}: {text}" if text else name


_FINGERPRINT_KEY = secrets.token_bytes(32)  # per process: a short query cannot be brute-forced


def fingerprint(text: object) -> SafeText:
    """Identify KQL (or other text) in a log line without revealing it.

    The digest is an HMAC-SHA256 under a random key that lives only in this process, so the
    same text has the same fingerprint during one session but cannot be guessed from it.

    Returns:
        ``hmac:<first 12 hex digits> len=<characters>``; for a non-string, ``<TypeName>``.
    """
    if not isinstance(text, str):
        return SafeText(f"<{type(text).__name__}>")
    data = text.encode("utf-8", "replace")
    digest = hmac.new(_FINGERPRINT_KEY, data, hashlib.sha256).hexdigest()[:12]
    return SafeText(f"hmac:{digest} len={len(text)}")


def mask_id(value: str | None) -> SafeText:
    """Mask an identifier for a log line: a GUID keeps its first four digits (``1a2b…``).

    Anything that is not a GUID (a tenant domain, say) is replaced by ``<id>``; ``None`` is
    ``none``.
    """
    if not value:
        return SafeText("none")
    value = value.strip()
    return SafeText(f"{value[:4]}…" if _GUID_FULL.fullmatch(value) else "<id>")


def mask_name(value: str | None) -> SafeText:
    """Mask a name that comes from the environment (workspace, host, database).

    Returns:
        The first two characters and the length, e.g. ``"contoso-prod"`` becomes
        ``co…(len 12)``; ``None`` is ``none``.
    """
    if not value:
        return SafeText("none")
    head = re.sub(r"[^A-Za-z0-9]", "?", value[:2])
    return SafeText(f"{head}…(len {len(value)})")


def describe_timespan(timespan: object) -> SafeText:
    """Describe a ``timespan`` argument for a log line, whatever it is.

    Returns:
        A ``timedelta`` as its duration, a tuple of datetimes/timedeltas as the ISO form of
        each item, ``none`` for ``None`` and ``<TypeName>`` for anything else (a notebook
        variable may hold any object, so its value is never logged).
    """
    if timespan is None:
        return SafeText("none")
    if isinstance(timespan, timedelta):
        return SafeText(str(timespan))
    if isinstance(timespan, tuple) and len(timespan) <= 2:
        parts = [_simple(item) for item in timespan]
        return SafeText("(" + ", ".join(parts) + ")")
    return SafeText(f"<{type(timespan).__name__}>")


_SAFE_STR = re.compile(r"[\w.-]{1,64}", re.ASCII)
_MAX_LIST = 20


def _simple(value: object, *, quote: bool = False) -> str:
    """One value for a log line, or ``<TypeName>`` if it is not a plain, short one."""
    try:
        if value is None or isinstance(value, bool | int | float):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, timedelta):
            return str(value)
        if isinstance(value, SafeText):
            return repr(str(value)) if quote else str(value)
        if isinstance(value, str) and _SAFE_STR.fullmatch(value):
            return repr(value) if quote else value
        return f"<{type(value).__name__}>"
    except Exception:
        return "<unprintable>"


def format_fields(fields: Mapping[str, Any]) -> str:
    """Render ``fields`` as `` key=value`` pairs; this never raises.

    Only plain values are written: ints, floats, bools, timedeltas, datetimes, short
    identifier-like strings, text from the masking helpers (:class:`SafeText`), and lists or
    tuples of at most 20 of those. Anything else is written as ``<TypeName>``; ``None`` is
    left out.
    """
    parts: list[str] = []
    try:
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, list | tuple) and not isinstance(value, SafeText):
                shown = [_simple(item, quote=True) for item in value[:_MAX_LIST]]
                if len(value) > _MAX_LIST:
                    shown.append("…")
                parts.append(f" {key}=[{', '.join(shown)}]")
            else:
                parts.append(f" {key}={_simple(value)}")
    except Exception:
        parts.append(" <fields unavailable>")
    return "".join(parts)


# --- Marks: which exceptions were already logged ------------------------------------------------


def mark_failure_logged(exc: BaseException) -> None:
    """Record that ``exc`` was already logged at ERROR, so a public boundary stays silent."""
    with contextlib.suppress(Exception):
        setattr(exc, _LOGGED_ATTR, True)


def clear_failure_mark(exc: BaseException) -> None:
    """Remove the mark again once ``exc`` leaves the package (an instance may be raised twice)."""
    with contextlib.suppress(Exception):
        delattr(exc, _LOGGED_ATTR)


def failure_logged(exc: BaseException) -> bool:
    """Whether ``exc`` or one of its causes was marked by :func:`mark_failure_logged`."""
    current: BaseException | None = exc
    for _ in range(_MAX_CHAIN):
        if current is None:
            return False
        if getattr(current, _LOGGED_ATTR, False):
            return True
        current = current.__cause__
    return False


# --- Dependency calls --------------------------------------------------------------------------


_calls = threading.local()  # per thread: how deep dependency calls are nested, what was reported


class _LazyFields:
    """:func:`format_fields` of a mapping, computed only if a handler formats the record.

    DEBUG records are created on every call but almost never formatted, so the DEBUG start line
    of a dependency call does not render its fields up front.
    """

    __slots__ = ("fields",)

    def __init__(self, fields: Mapping[str, Any]) -> None:
        self.fields = fields

    def __str__(self) -> str:
        return format_fields(self.fields)


@contextlib.contextmanager
def dependency_call(
    logger: logging.Logger, name: str, *, level: int = logging.INFO, **fields: Any
) -> Iterator[dict[str, Any]]:
    """Log a call into a dependency (an SDK, pandas, IPython): start, outcome and duration.

    DEBUG on entry; ``level`` (INFO by default) on success with the duration and the result
    fields the block adds to the yielded dict; WARNING on failure with the duration, the
    exception type and its redacted message, and then the exception propagates unchanged. A
    failure already reported by a nested ``dependency_call`` is only logged at DEBUG by the
    outer one.

    Args:
        logger: The calling module's logger.
        name: What is being called, e.g. ``"Log Analytics query"``.
        level: Level of the success line.
        **fields: Facts about the call to log (identifiers masked, never KQL or values).

    Yields:
        A dict to put result facts in (``call["rows"] = 10``).
    """
    result: dict[str, Any] = {}
    depth = getattr(_calls, "depth", 0)
    start = time.perf_counter()
    try:
        _calls.depth = depth + 1
        logger.debug("%s: calling%s", name, _LazyFields(fields))
        yield result
    except BaseException as exc:
        elapsed = time.perf_counter() - start
        if getattr(_calls, "reported", None) is exc:
            logger.debug("%s: failed after %.2fs (reported above)", name, elapsed)
        else:
            _calls.reported = exc
            logger.warning(
                "%s: failed after %.2fs%s: %s",
                name,
                elapsed,
                format_fields(fields),
                describe_exception(exc),
            )
        raise
    else:
        logger.log(
            level,
            "%s: done in %.2fs%s",
            name,
            time.perf_counter() - start,
            format_fields({**fields, **result}),
        )
    finally:
        _calls.depth = depth
        if depth == 0:
            _calls.reported = None


# --- Debug scope: per-call output in the notebook cell --------------------------------------------


class _DebugScope:
    """A debug scope: the stream of the cell that asked for it (None: use stdout).

    Context copies share the object, so ending the scope (``active = False``) also silences a
    worker thread that outlives the call, such as one left running after a ``KeyboardInterrupt``.
    """

    __slots__ = ("active", "stream")

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.active = True


_debug: contextvars.ContextVar[_DebugScope | None] = contextvars.ContextVar(
    "security_notebook_tools_debug", default=None
)


def debug_active() -> bool:
    """Whether the calling context is inside a :func:`debug_scope`."""
    scope = _debug.get()
    return scope is not None and scope.active


@contextlib.contextmanager
def debug_scope(enabled: bool) -> Iterator[None]:
    """Print the package's DEBUG-and-above records to the notebook cell while the block runs.

    Does nothing when ``enabled`` is false or a scope is already active (an inner call inherits
    it, and an inner ``debug=False`` never turns it off). Otherwise it captures the current
    ``sys.stdout`` (the running cell's stream in Jupyter) and ends the scope on exit, however
    the block ends. The scope belongs to the current context: other threads and tasks are not
    affected, and a worker thread sees it only if it runs in a copy of this context (see
    :func:`in_current_context`).

    Args:
        enabled: The call's ``debug`` argument.

    Yields:
        None.
    """
    if not enabled or debug_active():
        yield
        return
    scope = _DebugScope(sys.stdout)
    token = _debug.set(scope)
    try:
        yield
    finally:
        scope.active = False
        try:
            _debug.reset(token)
        except ValueError:  # reset from another context: end the scope the plain way
            _debug.set(None)


def in_current_context(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Bind ``func(*args, **kwargs)`` to a copy of the current context, to run it elsewhere.

    ``ThreadPoolExecutor`` threads do not inherit context variables, so a job that must log
    into the caller's debug scope is submitted through this. Each call takes its own copy, so
    jobs can run at the same time (a ``Context`` can only be entered once at a time).

    Args:
        func: The callable to run.
        *args: Its positional arguments.
        **kwargs: Its keyword arguments.

    Returns:
        A callable with no arguments that runs ``func`` inside the captured context.
    """
    context = contextvars.copy_context()
    return lambda: context.run(func, *args, **kwargs)


# --- The log file ------------------------------------------------------------------------------


def _home() -> Path | None:
    try:
        return Path.home().resolve()
    except Exception:
        return None


_root_cache: dict[str, Path] = {}


def project_root(start: Path | None = None) -> Path:
    """Find the project root for ``start`` (default: the current working directory).

    The search never goes above the user's home directory: if ``start`` is inside it, the walk
    stops at home (which is checked too), and if no marker is found by then ``start`` itself
    is used. Without ``start`` the answer is cached per working directory string, so a record
    does not touch the file system again.

    Args:
        start: Directory to start from.

    Returns:
        The nearest ancestor (or ``start`` itself) containing ``.git`` or ``pyproject.toml``,
        or ``start`` if there is none.
    """
    if start is None:
        key = os.getcwd()
        cached = _root_cache.get(key)
        if cached is not None:
            return cached
        _root_cache[key] = root = project_root(Path(key))
        return root
    start = start.resolve()
    home = _home()
    for directory in (start, *start.parents):
        if any((directory / marker).exists() for marker in _ROOT_MARKERS):
            return directory
        if directory == home:
            break
    return start


def log_file_path() -> Path:
    """Return the path of ``notebooks.log`` in the current project root."""
    return project_root() / LOG_FILE_NAME


def _open_log(path: Path) -> IO[str]:
    """Open ``path`` for appending, refusing anything but our own regular, unshared file.

    The file is created with mode 0600, symbolic links are not followed, and the opened file
    must be a regular file owned by the current user with a single link (where the platform
    has those notions).
    """
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        getuid = getattr(os, "getuid", None)
        if getuid is not None and (info.st_uid != getuid() or info.st_nlink != 1):
            raise OSError("not owned by the current user, or hard-linked")
        return os.fdopen(fd, "a", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def _exception_line(exc: BaseException) -> str:
    """``module.Type: message`` with the message made safe for the log file."""
    cls = type(exc)
    name = (
        cls.__qualname__
        if cls.__module__ in ("builtins", "__main__")
        else (f"{cls.__module__}.{cls.__qualname__}")
    )
    text = redact_exception_text(exc, _TRACEBACK_TEXT_LIMIT)
    return f"{name}: {text}" if text else name


def _render_exception(exc: BaseException, seen: set[int]) -> list[str]:
    """Like :func:`traceback.format_exception`, but safe: no source lines, redacted messages."""
    if id(exc) in seen or len(seen) >= _MAX_CHAIN:
        return []
    seen.add(id(exc))
    out: list[str] = []
    if exc.__cause__ is not None:
        earlier = _render_exception(exc.__cause__, seen)
        if earlier:
            out += [*earlier, "\nThe above exception was the direct cause of the following "]
            out[-1] += "exception:\n\n"
    elif exc.__context__ is not None and not exc.__suppress_context__:
        earlier = _render_exception(exc.__context__, seen)
        if earlier:
            out += [*earlier, "\nDuring handling of the above exception, another exception "]
            out[-1] += "occurred:\n\n"
    if exc.__traceback__ is not None:
        out.append("Traceback (most recent call last):\n")
        # Frames only name the file, line and function: source lines can hold literals.
        out.extend(
            f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
            for frame in traceback.extract_tb(exc.__traceback__)
        )
    out.append(_exception_line(exc) + "\n")
    return out


# Where front ends record the notebook a kernel runs: VS Code puts its path in the user
# namespace, Jupyter Server (2.0+) sets JPY_SESSION_NAME and ipykernel copies it to __session__.
_NOTEBOOK_NAMESPACE_KEYS = ("__vsc_ipynb_file__", "__session__")
_NOTEBOOK_ENV = "JPY_SESSION_NAME"
_NOTEBOOK_NAME_LIMIT = 64
_NOTEBOOK_UNSAFE = re.compile(r"[^\w .+()-]")


def notebook_name() -> str | None:
    """Return the file name of the notebook this kernel runs, or None if it is unknown.

    Looked up on every call, never cached, from the running IPython shell's
    ``__vsc_ipynb_file__`` (VS Code) or ``__session__`` (ipykernel) variable, then the
    ``JPY_SESSION_NAME`` environment variable (Jupyter Server). Only those two names are read
    from the namespace, and IPython is not imported if it is not loaded already. Only the last
    path component is kept; characters other than letters, digits, space and ``_.+()-`` become
    ``?`` and the result is cut to 64 characters. This never raises.
    """
    try:
        candidates: list[object] = []
        ipython = sys.modules.get("IPython")
        get_ipython = getattr(ipython, "get_ipython", None)
        shell = get_ipython() if get_ipython is not None else None
        namespace = getattr(shell, "user_ns", None)
        if isinstance(namespace, dict):
            candidates += [namespace.get(key) for key in _NOTEBOOK_NAMESPACE_KEYS]
        candidates.append(os.environ.get(_NOTEBOOK_ENV))
        for value in candidates:
            if isinstance(value, str):
                name = re.split(r"[\\/]", value.strip())[-1].strip()
                if name:
                    name = _NOTEBOOK_UNSAFE.sub("?", name)
                    if len(name) > _NOTEBOOK_NAME_LIMIT:
                        name = name[: _NOTEBOOK_NAME_LIMIT - 1] + "…"
                    return name
    except Exception:
        return None
    return None


class _UtcFormatter(logging.Formatter):
    """Formatter that renders timestamps in UTC and adds the notebook name to the line."""

    converter = time.gmtime

    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802 - logging hook
        """Format the line, filling in `` [<notebook>]`` (empty when it is unknown)."""
        name = notebook_name()
        setattr(record, _NOTEBOOK_ATTR, f" [{name}]" if name else "")
        return super().formatMessage(record)


class _RedactingFormatter(_UtcFormatter):
    r"""UTC formatter: one record has one message line, and every line is redacted.

    Line breaks in the message are escaped (``\n``) so the message cannot forge further
    records; the traceback is rendered by :func:`_render_exception` without source text, and
    each line of the result goes through :func:`redact` on its own.
    """

    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802 - logging hook
        """Format the line and escape carriage returns and line feeds in it."""
        return super().formatMessage(record).replace("\r", "\\r").replace("\n", "\\n")

    def formatException(self, ei: Any) -> str:  # noqa: N802 - the logging.Formatter hook
        """Render the traceback with each exception message sanitised and shortened."""
        try:
            return "".join(_render_exception(ei[1], set())).rstrip("\n")
        except Exception:
            return "<traceback unavailable>"

    def format(self, record: logging.LogRecord) -> str:
        """Format ``record`` and redact the result (message, traceback and stack alike)."""
        cached = record.exc_text
        if record.exc_info:
            record.exc_text = None  # never reuse text another handler rendered unredacted
        try:
            text = super().format(record)
        finally:
            record.exc_text = cached
        return "\n".join(redact(line) for line in text.split("\n"))


class _ProjectFileHandler(logging.Handler):
    """Append records to ``notebooks.log``, opening it lazily and following path changes.

    Records from outside the package (the ``azure`` loggers) are only written from WARNING up.
    """

    snt_file_handler = True

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level)
        self.setFormatter(_RedactingFormatter(LOG_FORMAT, DATE_FORMAT))
        self._stream: IO[str] | None = None
        self._path: Path | None = None

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        """Pass package records at the handler level, third-party ones from WARNING up."""
        if not super().filter(record):
            return False
        own = record.name == PACKAGE_LOGGER or record.name.startswith(PACKAGE_LOGGER + ".")
        return own or record.levelno >= logging.WARNING

    def emit(self, record: logging.LogRecord) -> None:
        """Append one formatted record, silently dropping it if the file is unusable."""
        try:
            path = log_file_path()
            if self._stream is None or path != self._path:
                self._close_stream()
                self._stream = _open_log(path)
                self._path = path
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self._close_stream()

    def _close_stream(self) -> None:
        stream, self._stream, self._path = self._stream, None, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()

    def close(self) -> None:
        """Close the open file, if any."""
        self._close_stream()
        super().close()


class _CellHandler(logging.Handler):
    """Print records to the notebook cell that opened the active :func:`debug_scope`.

    The filter passes a record only while the emitting context is in a scope, so concurrent
    calls that did not ask for debug output print nothing. It writes to the stream the scope
    captured (the current ``sys.stdout`` if that is gone). Unlike the file handler it does not
    redact: debug output is for the person running the call, and it is never written to the file
    (be careful when sharing a notebook saved with debug output). Errors are swallowed so
    output can never break a call.
    """

    snt_cell_handler = True

    def __init__(self, level: int = logging.DEBUG) -> None:
        super().__init__(level)
        self.setFormatter(_UtcFormatter(LOG_FORMAT, DATE_FORMAT))

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        """Pass records at the handler level while a debug scope is active."""
        return debug_active() and super().filter(record)

    def emit(self, record: logging.LogRecord) -> None:
        """Write one formatted line to the cell, silently dropping it on any error."""
        try:
            scope = _debug.get()
            stream = scope.stream if scope is not None else None
            if stream is None:
                stream = sys.stdout
            stream.write(self.format(record) + "\n")
            flush = getattr(stream, "flush", None)
            if flush is not None:
                flush()
        except Exception:
            return


def _install(*handlers: logging.Handler) -> None:
    """Attach the handlers to their loggers, replacing any older copy of the same kind.

    The file handler goes on the package and ``azure`` loggers, the cell handler on the package
    logger only. Run again (a module reload) it leaves exactly one handler of each kind.
    """
    for handler in handlers:
        if isinstance(handler, _CellHandler):
            kind, names = "snt_cell_handler", (PACKAGE_LOGGER,)
        else:
            kind, names = "snt_file_handler", (PACKAGE_LOGGER, *THIRD_PARTY_LOGGERS)
        for name in names:
            target = logging.getLogger(name)
            for old in [h for h in target.handlers if getattr(h, kind, False)]:
                if old is not handler:
                    target.removeHandler(old)
                    old.close()
            if handler not in target.handlers:
                target.addHandler(handler)


logger = logging.getLogger(PACKAGE_LOGGER)
logger.setLevel(logging.DEBUG)  # records exist; the handlers decide who sees which level
file_handler = _ProjectFileHandler()
cell_handler = _CellHandler()
_install(file_handler, cell_handler)


def close_log_file() -> None:
    """Close the log file's stream; it is reopened on the next record."""
    file_handler._close_stream()
