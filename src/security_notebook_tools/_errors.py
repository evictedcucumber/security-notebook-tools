"""Plain-language explanations of network, HTTP and service failures, and the catch-all.

Known failures are explained (:func:`describe_network_error`, :func:`describe_status`,
:func:`describe_codes`). Anything else that escapes an SDK is turned into a ``QueryError`` by
:func:`unexpected`, which also logs it at ERROR with its traceback so it can be reported.

:func:`public_call` is the one place where a failed public call is logged (WARNING when the
call was rejected, ERROR when it failed); it stays silent for an error :func:`unexpected` already
logged or a nested public call already reported.

The Azure and Kusto SDKs wrap low-level errors (``ConnectionResetError`` inside urllib3 inside
requests inside the SDK's own exception), so the cause is only visible by walking the exception
chain. These helpers do that and turn the result into a sentence with a practical hint.
"""

from __future__ import annotations

import errno
import logging
import socket
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from ._exceptions import ConfigurationError, QueryError, SecurityNotebookToolsError
from ._log import (
    clear_failure_mark,
    describe_exception,
    failure_logged,
    format_fields,
    mark_failure_logged,
)

_MAX_DEPTH = 20
_public = threading.local()  # per thread: how deep public calls are nested

# HTTP statuses worth retrying. 504 and 408 are deliberately absent: a query that timed out
# would only time out again.
TRANSIENT_STATUS = frozenset({429, 502, 503})

_RESET = (
    "connection reset by peer: the server or something between you and it (proxy, firewall, "
    "VPN, NAT, e.g. WSL2's) closed the connection. Usually transient: a pooled keep-alive "
    "connection that went stale, or an idle timeout on the network path during a long-running "
    "query. Retry; for a batch, a smaller `interval` keeps each query short"
)
_TIMED_OUT = (
    "the connection timed out: no answer arrived in time. Check the VPN/proxy and retry; for a "
    "batch, a smaller `interval` keeps each query short"
)
_BY_ERRNO = {
    errno.ECONNRESET: _RESET,
    errno.ECONNREFUSED: (
        "connection refused: nothing is listening at that address or a firewall rejected it. "
        "Check the endpoint URI and the VPN/proxy"
    ),
    errno.ETIMEDOUT: _TIMED_OUT,
    errno.EPIPE: (
        "broken pipe: the connection was closed while sending the request. Usually transient; retry"
    ),
    errno.ECONNABORTED: (
        "connection aborted: the connection was dropped locally or by the network path. "
        "Usually transient; retry"
    ),
    errno.ENETUNREACH: "network unreachable: there is no route to the service. Check the VPN/proxy",
    errno.EHOSTUNREACH: "host unreachable: there is no route to the service. Check the VPN/proxy",
}

_STATUS_HINTS = {
    400: "bad request: check the KQL syntax and the arguments",
    401: "not authenticated: run `az login` (the token may have expired)",
    403: "forbidden: your account has no read access to the workspace/database",
    404: "not found: check the workspace, cluster URI and database names",
    408: "the request timed out: narrow the time range or raise `timeout`",
    413: "the request or response is too large: narrow the time range or return fewer rows",
    429: "too many requests: the service is throttling; wait and retry",
    500: "internal server error on the service: retry, and report it if it persists",
    502: "bad gateway: the service is temporarily unavailable; retry",
    503: "service temporarily unavailable; retry",
    504: "the gateway timed out: narrow the time range or raise `timeout`",
}


def _chain(exc: BaseException) -> list[BaseException]:
    """The exception and its causes/contexts, outermost first, without cycles, capped."""
    seen: set[int] = set()
    out: list[BaseException] = []
    todo: list[BaseException | None] = [exc]
    while todo and len(out) < _MAX_DEPTH:
        current = todo.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        out.append(current)
        todo.extend((current.__cause__, current.__context__))
    return out


def find_os_error(exc: BaseException) -> OSError | None:
    """Return the innermost ``OSError`` in ``exc``'s chain, or None."""
    found = [e for e in _chain(exc) if isinstance(e, OSError)]
    return found[-1] if found else None


def is_network_error(exc: BaseException) -> bool:
    """Whether ``exc``'s chain holds a socket-level failure (reset, refused, timeout, DNS).

    Other OS errors, such as ``FileNotFoundError`` for a missing ``az`` CLI, do not count.
    """
    return any(
        isinstance(e, ConnectionError | TimeoutError | socket.gaierror)
        or (isinstance(e, OSError) and e.errno in _BY_ERRNO)
        for e in _chain(exc)
    )


def describe_network_error(exc: BaseException) -> str:
    """Explain a network failure from the innermost ``OSError`` in the exception chain.

    Args:
        exc: The exception raised by an SDK; its ``__cause__``/``__context__`` chain is walked.

    Returns:
        A sentence with the cause and, where it helps, a hint. Without an ``OSError`` in the
        chain it is the innermost exception's type and message.
    """
    os_error = find_os_error(exc)
    if os_error is None:
        chain = _chain(exc)
        inner = chain[-1]
        text = f"{type(inner).__name__}: {inner}"
        if any(type(e).__name__.endswith(("TimeoutError", "Timeout")) for e in chain):
            return f"{_TIMED_OUT} ({text})"
        return text
    code = os_error.errno
    tag = f"[Errno {code}] " if code is not None else ""
    if isinstance(os_error, socket.gaierror):
        return (
            f"{tag}DNS lookup failed ({os_error.strerror or os_error}): the host name could not "
            "be resolved. Check the endpoint URI, the network and the VPN/proxy"
        )
    if code in _BY_ERRNO:
        return f"{tag}{_BY_ERRNO[code]}"
    if isinstance(os_error, TimeoutError):
        return f"{tag}{_TIMED_OUT}"
    return f"{tag}{type(os_error).__name__}: {os_error.strerror or os_error}"


def describe_status(status: int | None) -> str | None:
    """Return a short hint for an HTTP status, or None when there is nothing useful to say."""
    if not isinstance(status, int) or isinstance(status, bool):
        return None
    if status in _STATUS_HINTS:
        return _STATUS_HINTS[status]
    if 500 <= status < 600:
        return f"server error (HTTP {status}) on the service: retry, and report it if it persists"
    return None


# Service error codes (Log Analytics `error.code`, Kusto `OneApiError.code`), matched as
# lower-case substrings, first match wins. The more specific needles come first.
_CODE_HINTS = (
    ("syntaxerror", "KQL syntax error: check the query near the position the message gives"),
    (
        "semanticerror",
        "KQL semantic error: a table, column or function name is wrong or not available here",
    ),
    (
        "entitynotfound",
        "a table, column, function or database was not found: check its name and your access",
    ),
    (
        "pathnotfound",
        "a table or function was not found in the workspace: check its name and your access",
    ),
    ("workspacenotfound", "workspace not found: check SENTINEL_WORKSPACE_ID and your access"),
    (
        "insufficientaccess",
        "no permission to read this data: ask for a reader role on the workspace "
        "(Log Analytics Reader or Microsoft Sentinel Reader)",
    ),
    ("forbidden", "forbidden: your account has no read access to this data"),
    ("unauthorized", "not authorised: run `az login`, then check your access"),
    (
        "payloadtoolarge",
        "the result is too large: narrow the time range, filter, or summarize/limit the rows",
    ),
    (
        "limitsexceeded",
        "a service limit was exceeded (result size or memory): narrow the time range, filter, "
        "or summarize/limit the rows",
    ),
    ("toolarge", "the result is too large: narrow the time range or return fewer rows"),
    ("timeout", "the query timed out: narrow the time range or raise `timeout`"),
    ("timedout", "the query timed out: narrow the time range or raise `timeout`"),
    ("badargument", "an argument or the query is invalid: check the KQL and the time range"),
    ("badrequest", "the request was rejected as invalid: check the KQL"),
)


def _odata_codes(error: Any, depth: int = 0) -> Iterator[str]:
    """Yield the codes in an ``ODataV4Format``-like object, outermost first (defensive)."""
    if error is None or depth > 5:
        return
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        yield code
    for detail in getattr(error, "details", None) or ():
        yield from _odata_codes(detail, depth + 1)
    inner = getattr(error, "innererror", None)
    while isinstance(inner, dict) and depth < 5:
        depth += 1
        code = inner.get("code")
        if isinstance(code, str) and code:
            yield code
        inner = inner.get("innererror")


def azure_error_codes(exc: BaseException) -> list[str]:
    """The service error codes of an Azure ``HttpResponseError`` (``exc.error`` and nested)."""
    try:
        return list(dict.fromkeys(_odata_codes(getattr(exc, "error", None))))
    except Exception:
        return []


def kusto_error_codes(exc: BaseException) -> list[str]:
    """The ``OneApiError`` codes of a ``KustoApiError`` or ``KustoMultiApiError``."""
    try:
        if hasattr(exc, "get_api_errors"):
            errors = list(exc.get_api_errors())
        elif hasattr(exc, "get_api_error"):
            errors = [exc.get_api_error()]
        else:
            return []
        codes = [getattr(e, "code", None) for e in errors]
        return list(dict.fromkeys(c for c in codes if isinstance(c, str) and c))
    except Exception:
        return []


def describe_codes(codes: Iterable[str]) -> str | None:
    """Return the hints for service error codes (``"BadRequest_SyntaxError"`` ...), or None."""
    hints: list[str] = []
    for code in codes:
        low = code.lower()
        hint = next((text for needle, text in _CODE_HINTS if needle in low), None)
        if hint and hint not in hints:
            hints.append(hint)
    return "; ".join(hints) or None


def hints_for(status: int | None, codes: Iterable[str]) -> str:
    """Join the status and code hints, as `` (hint; hint)``, or an empty string."""
    codes = list(codes)
    parts = [h for h in (describe_codes(codes), describe_status(status)) if h]
    text = "; ".join(parts)
    if codes:
        text = f"{text}; " if text else ""
        text += "service error code: " + ", ".join(codes)
    return f" ({text})" if text else ""


def log_unexpected(logger: logging.Logger, operation: str, exc: BaseException) -> None:
    """Log an error nobody anticipated at ERROR, with its traceback.

    The traceback is rendered with redaction (see :mod:`._log`). The record is the only ERROR
    for the failure: the public boundary stays silent for an exception raised after this, once
    the error that is raised has been passed to :func:`mark_failure_logged` (:func:`unexpected`
    does that).
    """
    logger.error(
        "Unexpected %s during %s; this case is not handled yet, please report it with the "
        "traceback",
        type(exc).__name__,
        operation,
        exc_info=exc,
    )


def unexpected(logger: logging.Logger, operation: str, exc: BaseException) -> QueryError:
    """Log an error nobody anticipated with its traceback and return a ``QueryError`` for it.

    Call it from an ``except Exception`` block and ``raise unexpected(...) from exc``. The log
    record carries ``exc_info`` and the message tells the reader to report it; the returned
    error names the operation and the original type and message, and the original is chained,
    so the traceback is also in the cell and in ``notebooks.log``. The returned error is marked
    as logged, so the public boundary does not log it again.

    Args:
        logger: The module's logger.
        operation: What was being done, e.g. ``"Sentinel query"``.
        exc: The unexpected exception.

    Returns:
        A ``QueryError`` to raise from ``exc``.
    """
    log_unexpected(logger, operation, exc)
    name = type(exc).__name__
    error = QueryError(
        f"Unexpected {name} during {operation}: {exc}. This case is not handled yet; the "
        "original exception and its full traceback are chained below. Please report it."
    )
    mark_failure_logged(error)
    return error


@contextmanager
def unexpected_errors(logger: logging.Logger, operation: str) -> Iterator[None]:
    """Turn any ``Exception`` raised inside the block into :func:`unexpected`'s ``QueryError``.

    Package errors (``SecurityNotebookToolsError``, including ``ConfigurationError``) pass
    through unchanged, and so do ``KeyboardInterrupt``, ``SystemExit`` and other
    ``BaseException`` that are not ``Exception``. Wrap only SDK calls, never the cost guard.
    """
    try:
        yield
    except SecurityNotebookToolsError:
        raise
    except Exception as exc:
        raise unexpected(logger, operation, exc) from exc


@contextmanager
def public_call(
    logger: logging.Logger,
    name: str,
    *,
    rejected: tuple[type[BaseException], ...] = (ConfigurationError,),
    **fields: Any,
) -> Iterator[dict[str, Any]]:
    """Log a public function: entry, result summary, and its failure exactly once.

    INFO on entry (with ``fields``) and on success (with the duration and the result fields the
    block adds to the yielded dict, such as ``rows`` and ``columns``). An exception is logged
    once and re-raised unchanged: WARNING if it is one of ``rejected`` (invalid arguments or
    configuration, a query refused by the cost guard), ERROR for a ``QueryError`` or any other
    package error, ERROR with a traceback for an exception nobody anticipated. It is not logged
    if it, or an exception it was raised from, was already logged (by :func:`unexpected` or by a
    nested public call). ``KeyboardInterrupt`` is logged at WARNING. Only redacted
    text is logged (see :func:`._log.describe_exception`).

    Args:
        logger: The public function's module logger.
        name: The public name, e.g. ``"exec_query"``.
        rejected: Exception types that mean "refused", logged at WARNING.
        **fields: Facts about the call (never KQL, values or environment variable values).

    Yields:
        A dict to put result facts in (``call["rows"] = 10``).
    """
    result: dict[str, Any] = {}
    depth = getattr(_public, "depth", 0)
    start = time.perf_counter()
    try:
        _public.depth = depth + 1
        logger.info("%s: started%s", name, format_fields(fields))
        yield result
    except KeyboardInterrupt as exc:
        if not failure_logged(exc):
            mark_failure_logged(exc)
            logger.warning("%s: interrupted after %.2fs", name, time.perf_counter() - start)
        if depth == 0:
            clear_failure_mark(exc)
        raise
    except Exception as exc:
        if not failure_logged(exc):
            mark_failure_logged(exc)
            elapsed = time.perf_counter() - start
            if isinstance(exc, rejected):
                logger.warning(
                    "%s: rejected after %.2fs: %s", name, elapsed, describe_exception(exc, 300)
                )
            elif isinstance(exc, SecurityNotebookToolsError):
                logger.error(
                    "%s: failed after %.2fs: %s", name, elapsed, describe_exception(exc, 300)
                )
            else:
                log_unexpected(logger, name, exc)
        if depth == 0:
            clear_failure_mark(exc)
        raise
    else:
        logger.info(
            "%s: finished in %.2fs%s", name, time.perf_counter() - start, format_fields(result)
        )
    finally:
        _public.depth = depth
