"""The public ``exec_batch_query`` entry point: one query per consecutive time window."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import pandas as pd

from ._errors import describe_network_error, log_unexpected, public_call
from ._exceptions import (
    ConfigurationError,
    QueryConnectionError,
    QueryError,
    QueryThrottledError,
    QueryTimeoutError,
    QueryTransientError,
    SecurityNotebookToolsError,
)
from ._format import KQL_TYPES_ATTR
from ._kql import Token, tokenize
from ._log import (
    dependency_call,
    describe_exception,
    fingerprint,
    in_current_context,
    mark_failure_logged,
    redact_exception_text,
)
from ._query import (
    DEFAULT_TIMEOUT,
    Target,
    _check_bool,
    _check_kql,
    _check_timeout,
    _query_adx,
    _query_sentinel,
    _warn_retention,
    remember_timed_out,
    timed_out_before,
    window_key,
)
from ._timerange import validate_window_args

logger = logging.getLogger("security_notebook_tools.batch")

_BATCH_VARS = ("BatchStart", "BatchEnd")
_EXPECTED = "filter with `TimeGenerated >= BatchStart and TimeGenerated < BatchEnd`"
_COMPARISONS = frozenset({"<", ">", "<=", ">=", "==", "!=", "<>", "=~", "!~"})
_OP_CHARS = frozenset("<>=!~")
_ARITHMETIC = frozenset("+-*/%")

# Log Analytics runs 5 queries per user at once and queues the rest on the server, where the
# wait counts against the timeout; more workers than that gain nothing. ADX shares its limit
# with everyone using the cluster.
MAX_WORKERS = 5
# A window that fails transiently (throttled, connection dropped) is retried this many times,
# waiting a random time (full jitter) between 0 and
# min(_BACKOFF_CAP, _BACKOFF_BASE * 2 ** retries_so_far) seconds.
_MAX_RETRIES = 4
_BACKOFF_BASE = 2.0
_BACKOFF_CAP = 30.0
# A connection that was reset or timed out after the attempt ran for at least this fraction of
# ``timeout`` was cut off for being too slow (a proxy or the gateway giving up on a long query),
# not dropped by a network blip: it is treated as a timeout and not retried.
_LATE_RESET_FRACTION = 0.5
_clock = time.monotonic  # tests replace it


class _DefaultAgg(str):
    """The default ``agg_func``; a ``str`` that equals "sum" but tells "not passed" by identity."""

    __slots__ = ()


_DEFAULT_AGG = _DefaultAgg("sum")
# Result type of an aggregation: None keeps the column's own type, a string replaces it.
_SAME_TYPE = {"sum", "min", "max", "first", "last"}
_LONG_TYPE = {"count", "nunique", "size"}
_REAL_TYPE = {"mean", "median", "std", "var"}


class _Stopped(Exception):
    """A worker gave up waiting to retry because the batch is already failing."""


class _WindowTimeout(QueryTimeoutError):
    """A window's query timed out (the service said so, or the connection was cut off late).

    ``elapsed`` is how long the last attempt ran, in seconds; ``detail`` says in a few words what
    happened (without advice), for the message of a window that cannot be split further.
    """

    def __init__(self, message: str, elapsed: float, detail: str = "") -> None:
        """Store how long the attempt ran, and what happened, alongside the message."""
        super().__init__(message)
        self.elapsed = elapsed
        self.detail = detail or f"the query timed out after {elapsed:.0f} s"


def _utc(value: datetime) -> datetime:
    """Return ``value`` in UTC; a naive datetime is taken to be UTC already."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _literal(value: datetime) -> str:
    """Format a UTC datetime as a KQL ``datetime("...")`` literal.

    The string form is used because the bare form ``datetime(2026-01-01T00:00:00Z)`` is rejected
    by the KQL tokenizer behind the cost guard (ambiguous number literal).
    """
    return f'datetime("{value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}")'


def _prefix(bounds: tuple[datetime, datetime]) -> str:
    """The ``let`` statements that define ``BatchStart`` and ``BatchEnd`` for a window."""
    return f"let BatchStart = {_literal(bounds[0])}; let BatchEnd = {_literal(bounds[1])};\n"


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _windows(
    start: datetime, end: datetime, interval: timedelta, *, aligned: bool = False
) -> list[tuple[datetime, datetime]]:
    """Split ``[start, end)`` into consecutive windows of ``interval``; the last is clipped.

    With ``aligned`` the boundaries are multiples of ``interval`` from the Unix epoch (UTC)
    instead of ``start``, so the first window is clipped too and the windows in between are the
    same whenever the period ends.
    """
    windows = []
    cursor = start
    cut = _EPOCH + ((start - _EPOCH) // interval + 1) * interval if aligned else start + interval
    while cursor < end:
        windows.append((cursor, min(cut, end)))
        cursor = cut
        cut += interval
    return windows


def _merge(results: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate the non-empty window results and combine their ``kql_types``."""
    types: dict[str, str] = {}
    for df in results:
        for name, kql_type in df.attrs.get("kql_types", {}).items():
            types.setdefault(name, kql_type)
    non_empty = [df for df in results if not df.empty]
    with dependency_call(logger, "pandas merge", windows=len(results)) as call:
        if non_empty:
            merged = pd.concat(non_empty, ignore_index=True)
        elif results:
            merged = results[0].copy()  # no rows; a copy keeps the caller's attrs untouched
        else:
            merged = pd.DataFrame()
        call["rows"] = len(merged)
    if types:
        merged.attrs["kql_types"] = types
    return merged


def _names(value: str | Sequence[str], label: str) -> list[str]:
    """Return ``value`` as a list of column names, or raise ``ConfigurationError``."""
    items = [value] if isinstance(value, str) else list(value)
    if not items:
        raise ConfigurationError(f"{label} must not be empty.")
    for item in items:
        if not isinstance(item, str) or not item:
            raise ConfigurationError(f"{label} must be non-empty column names, got {item!r}.")
    if len(set(items)) != len(items):
        raise ConfigurationError(f"{label} must not repeat a column, got {items!r}.")
    return items


def _check_columns(df: pd.DataFrame, needed: list[str], where: str = "the query result") -> None:
    """Raise ``ConfigurationError`` if ``df`` lacks any of the ``needed`` columns."""
    missing = [name for name in needed if name not in df.columns]
    if missing:
        raise ConfigurationError(
            f"Column(s) {missing} named in merge_on/agg_column are not in {where}; "
            f"available columns: {list(df.columns)}."
        )


def _check_agg_args(
    merge_on: str | Sequence[str], agg_column: str | Sequence[str], agg_func: Any
) -> tuple[list[str], list[str]]:
    """Validate ``merge_on``, ``agg_column`` and ``agg_func``; return the key and value names."""
    keys = _names(merge_on, "merge_on")
    cols = _names(agg_column, "agg_column")
    both = [name for name in keys if name in cols]
    if both:
        raise ConfigurationError(f"{both} cannot be in both merge_on and agg_column.")
    if not callable(agg_func) and not (isinstance(agg_func, str) and agg_func):
        raise ConfigurationError(
            f"agg_func must be a function name or a callable, got {agg_func!r}."
        )
    return keys, cols


def _agg_type(func: str | Callable[..., Any], original: str | None) -> str | None:
    """The KQL type of an aggregated column, or None when it cannot be known."""
    if not isinstance(func, str):
        return None
    if func in _SAME_TYPE:
        return original
    if func in _LONG_TYPE:
        return "long"
    if func in _REAL_TYPE:
        return "real"
    return None


def _aggregate(
    merged: pd.DataFrame,
    keys: list[str],
    cols: list[str],
    func: str | Callable[..., Any],
    *,
    log: logging.Logger = logger,
    where: str = "the query result",
) -> pd.DataFrame:
    """Group ``merged`` by ``keys`` and aggregate ``cols`` with ``func``; keep ``kql_types``."""
    _check_columns(merged, keys + cols, where)
    try:
        with dependency_call(log, "pandas groupby aggregation", keys=keys, columns=cols):
            out = merged.groupby(keys, as_index=False, dropna=False, sort=True)[cols].agg(func)
    except Exception as exc:
        name = func if isinstance(func, str) else getattr(func, "__name__", repr(func))
        raise ConfigurationError(
            f"Cannot aggregate {cols} with {name!r} grouped by {keys}: {exc}"
        ) from exc
    old = merged.attrs.get(KQL_TYPES_ATTR, {})
    types = {k: old[k] for k in keys if k in old}
    for col in cols:
        kql_type = _agg_type(func, old.get(col))
        if kql_type is not None:
            types[col] = kql_type
    if types:
        out.attrs[KQL_TYPES_ATTR] = types
    return out


def _sleep(delay: float, stop: threading.Event) -> bool:
    """Wait ``delay`` seconds; return True if ``stop`` was set meanwhile (tests patch this)."""
    return stop.wait(delay)


def _reason(exc: QueryError) -> str:
    """A short cause for a retry warning: the network cause if known, else the redacted message."""
    cause = exc.__cause__
    if cause is not None:
        text = describe_network_error(cause)
        if text.startswith("[Errno"):
            return text.split(":", 1)[0].split(" ", 2)[-1]
    return redact_exception_text(str(exc).split("\n")[0], 120)


def _attempt_window(
    label: str,
    bounds: tuple[datetime, datetime],
    kql: str,
    target: Target,
    timeout: int,
    cache: bool,
    stop: threading.Event,
) -> pd.DataFrame:
    """Run one window, retrying transient failures; wrap a failure with the window's name.

    Args:
        label: The window's name in messages: ``"3/18"``, or ``"3/18.2"`` for the second half
            of window 3 after a split.
        bounds: The window, half-open.
        kql: The user's KQL, without the ``let`` prefix.
        target: ``"sentinel"`` or ``"adx"``.
        timeout: Server-side timeout in seconds.
        cache: Whether the result cache may serve and keep the window.
        stop: Set when the batch is failing; ends the wait before a retry.

    Raises:
        _WindowTimeout: The query timed out (HTTP 504/408, the client deadline), or the
            connection was reset or timed out after the attempt ran for at least
            ``_LATE_RESET_FRACTION`` of ``timeout``. A ``QueryTimeoutError``; not retried.
        QueryError: The window failed, or still failed transiently after ``_MAX_RETRIES``
            retries (the message then says it was retried).
        _Stopped: ``stop`` was set while waiting to retry.
    """
    w_start, w_end = bounds
    name = f"Batch window {label} ({w_start} to {w_end})"
    prefix = _prefix(bounds)
    retries = 0
    while True:
        attempt_start = _clock()
        try:
            if target == "sentinel":
                tables = _query_sentinel(
                    prefix + kql, bounds, timeout, cache=cache, check_retention=False
                )
            else:
                tables = _query_adx(prefix + kql, timeout, cache=cache)
            break
        except SecurityNotebookToolsError as exc:
            elapsed = _clock() - attempt_start
            if not isinstance(exc, QueryError):
                raise  # e.g. ConfigurationError from the cost guard: unchanged
            if isinstance(exc, QueryConnectionError) and elapsed >= timeout * _LATE_RESET_FRACTION:
                logger.warning(
                    "Batch %s was cut off after %.0f s (timeout %d s), a timeout rather than a "
                    "network blip, so it is not retried: %s",
                    label,
                    elapsed,
                    timeout,
                    describe_exception(exc),
                )
                raise _WindowTimeout(
                    f"{name} ran {elapsed:.0f} s before the connection was reset or timed out "
                    f"(timeout={timeout} s): the query was cut off for being too slow, not "
                    "dropped by the network, so it is not retried. Use a smaller `interval`, set "
                    f"`min_interval` to split such windows, or raise `timeout`: {exc}",
                    elapsed,
                    f"the connection was reset or timed out after {elapsed:.0f} s",
                ) from exc
            if isinstance(exc, QueryTimeoutError):
                logger.warning(
                    "Batch %s timed out after %.0f s and is not retried: %s",
                    label,
                    elapsed,
                    describe_exception(exc),
                )
                raise _WindowTimeout(f"{name} failed: {exc}", elapsed) from exc
            if not isinstance(exc, QueryTransientError):
                logger.warning(
                    "Batch %s failed and is not retried: %s", label, describe_exception(exc)
                )
                raise QueryError(f"{name} failed: {exc}") from exc
            if retries >= _MAX_RETRIES:
                logger.warning(
                    "Batch %s still failing after %d retries, giving up: %s",
                    label,
                    retries,
                    describe_exception(exc),
                )
                raise QueryError(f"{name} failed after {retries} retries: {exc}") from exc
            delay = random.uniform(0, min(_BACKOFF_CAP, _BACKOFF_BASE * 2**retries))
            retries += 1
            if isinstance(exc, QueryThrottledError):
                why = "throttled"
            else:
                why = f"failed with a transient error ({_reason(exc)})"
            logger.warning(
                "Batch %s %s; retrying in %.1f s (attempt %d/%d)",
                label,
                why,
                delay,
                retries,
                _MAX_RETRIES,
            )
            if _sleep(delay, stop):
                logger.warning(
                    "Batch %s is not retried: another window failed and the batch is stopping",
                    label,
                )
                raise _Stopped from exc
        except Exception as exc:
            log_unexpected(logger, name, exc)
            failure = QueryError(
                f"{name} failed with an unexpected {type(exc).__name__}: {exc}. This case is "
                "not handled yet and is not retried; the original exception and its traceback "
                "are chained below. Please report it."
            )
            mark_failure_logged(failure)
            raise failure from exc
    df = tables[0] if tables else pd.DataFrame()
    logger.info("Batch %s: %s → %s, rows=%d", label, w_start, w_end, len(df))
    return df


def _run_or_split(
    label: str,
    bounds: tuple[datetime, datetime],
    kql: str,
    target: Target,
    timeout: int,
    cache: bool,
    stop: threading.Event,
    min_interval: timedelta | None,
) -> pd.DataFrame:
    """Run a window; if it times out and ``min_interval`` is set, run its two halves instead.

    The halves ``[start, middle)`` and ``[middle, end)`` run one after another, oldest first,
    each as a window of its own (own ``BatchStart``/``BatchEnd`` and API time range, own
    retries), and are split again the same way, until a half would be shorter than
    ``min_interval``. Their frames are merged like the windows of a batch.

    With ``cache`` on, a window that times out is remembered (see ``remember_timed_out`` in
    :mod:`._query`; ``clear_cache()`` forgets it), and a later run with ``min_interval`` splits
    it at once instead of waiting for another timeout; the halves come from the result cache.

    Raises:
        QueryTimeoutError: The window timed out and cannot be split further (or splitting is
            off: ``min_interval`` is None).
        QueryError: A window failed for another reason.
        _Stopped: ``stop`` was set before a half started or while waiting to retry.
    """
    w_start, w_end = bounds
    key = None
    if cache:
        # The result cache keys on the same things: a window that timed out is not stored there.
        key = window_key(target, _prefix(bounds) + kql, bounds, timeout)
    earlier = timed_out_before(key) if key is not None and min_interval is not None else None
    if earlier is not None:
        logger.info(
            "Batch %s timed out after %.0f s in an earlier run; splitting it without trying it "
            "whole (clear_cache() or cache=False tries it whole again)",
            label,
            earlier,
        )
        timed_out = _WindowTimeout(
            f"Batch window {label} ({w_start} to {w_end}) timed out in an earlier run",
            earlier,
            f"it timed out after {earlier:.0f} s in an earlier run; clear_cache() or cache=False "
            "tries it whole again",
        )
    else:
        try:
            return _attempt_window(label, bounds, kql, target, timeout, cache, stop)
        except _WindowTimeout as exc:
            if key is not None:
                remember_timed_out(key, exc.elapsed)
            if min_interval is None:
                raise
            timed_out = exc
    size = w_end - w_start
    half = size / 2
    if half < min_interval:
        logger.warning(
            "Batch %s (%s to %s) timed out after %.0f s and cannot be split further: half of "
            "%s is shorter than min_interval %s",
            label,
            w_start,
            w_end,
            timed_out.elapsed,
            size,
            min_interval,
        )
        raise QueryTimeoutError(
            f"Batch window {label} ({w_start} to {w_end}) timed out ({timed_out.detail}) and was "
            f"not split further: the smallest window tried was {size} (half of it is shorter "
            f"than min_interval={min_interval}). Use a smaller `min_interval` or raise "
            "`timeout`; the original error is chained."
        ) from timed_out
    middle = w_start + half
    logger.warning(
        "Batch %s (%s to %s) timed out after %.0f s; splitting it into two windows of %s",
        label,
        w_start,
        w_end,
        timed_out.elapsed,
        half,
    )
    parts = []
    for number, part in enumerate(((w_start, middle), (middle, w_end)), start=1):
        if stop.is_set():
            logger.warning(
                "Batch %s.%d is not run: another window failed and the batch is stopping",
                label,
                number,
            )
            raise _Stopped from timed_out
        parts.append(
            _run_or_split(
                f"{label}.{number}", part, kql, target, timeout, cache, stop, min_interval
            )
        )
    return _merge(parts)


def _run_window(
    index: int,
    total: int,
    bounds: tuple[datetime, datetime],
    kql: str,
    target: Target,
    timeout: int,
    cache: bool,
    stop: threading.Event,
    min_interval: timedelta | None = None,
) -> pd.DataFrame:
    """Run window ``index`` of ``total``: retry transient failures, split on a timeout.

    See :func:`_attempt_window` and :func:`_run_or_split` for the details and the exceptions.
    """
    return _run_or_split(
        f"{index}/{total}", bounds, kql, target, timeout, cache, stop, min_interval
    )


def _run_parallel(
    jobs: list[Callable[[], pd.DataFrame]], max_workers: int, stop: threading.Event
) -> list[pd.DataFrame]:
    """Run ``jobs`` on up to ``max_workers`` threads and return their results in job order.

    Fail fast: when a job raises, ``stop`` is set, jobs not yet started are cancelled, running
    ones are waited for, and the exception of the lowest-indexed failed job is raised. On
    ``KeyboardInterrupt`` (or any other exception in the calling thread) the pending jobs are
    cancelled and the exception re-raised without waiting; queries already sent run to
    completion in their threads.
    """
    pool = ThreadPoolExecutor(max_workers=min(max_workers, len(jobs)), thread_name_prefix="snt")
    futures: list[Future[pd.DataFrame]] = []
    try:
        futures = [pool.submit(in_current_context(job)) for job in jobs]
        wait(futures, return_when=FIRST_EXCEPTION)
        failed = any(not f.cancelled() and f.exception() is not None for f in futures if f.done())
        if failed:
            stop.set()
            pool.shutdown(wait=True, cancel_futures=True)
            logger.warning(
                "A window failed; %d window(s) not yet started were cancelled",
                sum(f.cancelled() for f in futures),
            )
    except BaseException:
        stop.set()
        pool.shutdown(wait=False, cancel_futures=True)
        logger.warning("Batch interrupted; pending windows are cancelled")
        raise
    pool.shutdown()
    for future in futures:  # lowest index first; all are finished or cancelled by now
        error = None if future.cancelled() else future.exception()
        if error is not None and not isinstance(error, _Stopped):
            raise error
    return [future.result() for future in futures]


def _operator(tokens: list[Token], i: int, step: int) -> str:
    """The operator (at most 2 characters) right next to token ``i``, on one side.

    ``step`` is 1 for the operator after the token and -1 for the one before it. The tokenizer
    emits every punctuation character on its own, so ``>=`` is the two tokens ``>`` and ``=``.
    """
    chars: list[str] = []
    j = i + step
    while 0 <= j < len(tokens) and len(chars) < 2:
        tok = tokens[j]
        if tok.kind != "p" or tok.text not in _OP_CHARS:
            break
        chars.append(tok.text)
        j += step
    return "".join(chars if step > 0 else reversed(chars))


def _check_batch_filter(kql: str) -> None:
    """Check that the user's KQL filters on the window as ``x >= BatchStart`` and ``x < BatchEnd``.

    Only the user's own text is checked, never the ``let`` prefix. A comparison is a use of
    ``BatchStart`` or ``BatchEnd`` right next to ``<``, ``>``, ``<=``, ``>=``, ``==``, ``!=``,
    ``<>``, ``=~`` or ``!~``, or inside ``between (...)``. Other uses (``extend W = BatchStart``,
    ``bin(x, 1h)`` arguments) are not judged. A variable followed by arithmetic
    (``x >= BatchStart - 1h``) is part of an expression: it neither satisfies nor violates the rule.
    Comments and strings are ignored because the tokenizer drops or isolates them.

    Raises:
        ConfigurationError: A variable is defined by the KQL, is compared in a form other than
            ``x >= BatchStart`` / ``BatchStart <= x`` / ``x < BatchEnd`` / ``BatchEnd > x``, or
            one of the two required comparisons is missing.
    """
    tokens = tokenize(kql)
    found: set[str] = set()
    for i, tok in enumerate(tokens):
        if tok.low == "between" and tok.kind == "id" and not tok.quoted:
            depth = 0
            for later in tokens[i + 1 :]:
                depth += later.is_p("(") - later.is_p(")")
                if later.kind == "id" and later.text in _BATCH_VARS:
                    raise ConfigurationError(
                        "`between` includes both ends, so a row on a window boundary would be "
                        f"counted twice; {_EXPECTED}."
                    )
                if depth <= 0:
                    break
    for i, tok in enumerate(tokens):
        if tok.kind != "id" or tok.text not in _BATCH_VARS:
            continue
        if i and tokens[i - 1].is_p(".") and not (i > 1 and tokens[i - 2].is_p(".")):
            continue  # a member access (bag.BatchStart), not the variable
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        after = tokens[i + 2] if i + 2 < len(tokens) else None
        if (
            (nxt is not None and nxt.is_p(":"))
            or (i and tokens[i - 1].is_kw("let", "as"))
            or (nxt is not None and nxt.is_p("=") and not (after and after.is_p("=", "~")))
        ):
            raise ConfigurationError(
                f"The KQL defines {tok.text} itself; it is defined for every window by the batch. "
                f"Remove the definition and {_EXPECTED}."
            )
        is_start = tok.text == "BatchStart"
        before_op = _operator(tokens, i, -1)  # x OP var
        after_op = _operator(tokens, i, 1)  # var OP x
        shifted = nxt is not None and nxt.kind == "p" and nxt.text in _ARITHMETIC
        if shifted:
            continue  # part of an expression (BatchEnd - BatchStart), not the bound itself
        if before_op in _COMPARISONS:
            if before_op == (">=" if is_start else "<"):
                found.add(tok.text)
                continue
            raise ConfigurationError(
                f"`... {before_op} {tok.text}` is not allowed; {_EXPECTED} (the window is "
                "half-open: its start is included, its end is not)."
            )
        if after_op in _COMPARISONS:
            if after_op == ("<=" if is_start else ">"):
                found.add(tok.text)
                continue
            raise ConfigurationError(
                f"`{tok.text} {after_op} ...` is not allowed; {_EXPECTED} (the window is "
                "half-open: its start is included, its end is not)."
            )
    missing = [
        f"`<column> {op} {name}` (or `{name} {mirror} <column>`)"
        for name, op, mirror in (("BatchStart", ">=", "<="), ("BatchEnd", "<", ">"))
        if name not in found
    ]
    if missing:
        raise ConfigurationError(
            f"The KQL must compare with {' and '.join(missing)}, so that a row on a window "
            f"boundary is counted once: {_EXPECTED}. The query was not sent."
        )


def exec_batch_query(
    kql: str,
    start: datetime | timedelta,
    end: datetime | None = None,
    *,
    interval: timedelta = timedelta(days=1),
    min_interval: timedelta | None = None,
    target: Target = "sentinel",
    timeout: int = DEFAULT_TIMEOUT,
    max_workers: int = 1,
    merge_on: str | Sequence[str] | None = None,
    agg_column: str | Sequence[str] | None = None,
    agg_func: str | Callable[..., Any] = _DEFAULT_AGG,
    cache: bool = True,
    debug: bool = False,
) -> pd.DataFrame:
    """Run the same KQL once per consecutive time window and merge the results.

    Use it when a query is too big for one run or needs a longer period than one query allows.
    The period from ``start`` to ``end`` is split into windows of ``interval``:
    ``[start, start + interval)``, ``[start + interval, start + 2 * interval)`` and so on, with
    the last window clipped to ``end``. When the period is relative to now (``start`` is a
    ``timedelta`` and ``end`` is not given), the windows are instead cut at multiples of
    ``interval`` from 1970-01-01 UTC (midnight UTC for the default 1 day), so the first window
    is clipped to ``start`` as well and there may be one window more. Each run goes through the
    same checks as ``exec_query``, so the Sentinel cost guard applies to every window.

    Each window is cached separately (see ``exec_query``), so re-running a batch reuses the
    windows that already succeeded: all of them for the same absolute ``start`` and ``end``, and
    for a period relative to now every window except the first and last, which move with now.

    By default the windows run one after another, oldest first. With ``max_workers`` above 1 the
    first window still runs alone (so a bad query or configuration fails once, and the client,
    credential and table-plan caches are warm), then the rest run in parallel on up to
    ``max_workers`` threads. The result order does not depend on which window finishes first.
    The first failure stops the batch: windows not yet started are skipped, running ones are
    waited for, and the error of the lowest-numbered failed window is raised.

    A window that fails with a transient error is retried up to 4 times: throttling (HTTP 429,
    "too many requests"), a connection that dropped before a response arrived (reset by peer,
    timed out, DNS), or HTTP 502/503. The wait is a random time between 0 and 2, 4, 8 and 16
    seconds (full jitter, capped at 30 s), and a warning names the reason each time. After that
    the window fails with an error that says it was retried. Other errors (bad KQL, HTTP
    401/403, a server-side timeout) are not retried. A connection that was reset or timed out
    after the attempt had already run for at least half of ``timeout`` was cut off for being too
    slow, not dropped by the network: it counts as a timeout and is not retried either; its
    message says how long the window ran and suggests a smaller ``interval`` or a larger
    ``timeout``. A timeout fails with a ``QueryTimeoutError`` (a ``QueryError``) that names the
    window. An error nobody anticipated is logged at ERROR with its traceback, wrapped in a
    ``QueryError`` that names the window, and not retried.

    With ``min_interval`` a window that times out is not failed but split in two and each half
    run as a window of its own, again and again down to ``min_interval``; use it when a few
    windows (typically over old data, which is slower to scan) are too heavy for ``timeout``.
    Cached windows are reused, and the halves are cached like any window. With ``cache=True`` a
    window that timed out is also remembered (until ``clear_cache()``), so re-running the batch
    splits it at once instead of waiting for another timeout; ``cache=False`` tries every
    window whole again.

    Window bounds reach the query in two ways:

    * ``BatchStart`` and ``BatchEnd`` are defined as ``datetime`` values by ``let`` statements
      added in front of your KQL, so the query can use them. The window is half-open, so the
      KQL must filter with ``TimeGenerated >= BatchStart and TimeGenerated < BatchEnd`` (any
      column or expression in place of ``TimeGenerated``; ``BatchStart <= x`` and
      ``BatchEnd > x`` also work). This is checked before any query is sent, on both targets:
      each of the two comparisons must appear (outside comments and strings), and any other
      comparison of ``BatchStart`` or ``BatchEnd`` (``>``, ``<=``, ``==``, ``!=``, ``between``,
      or the wrong variable) is rejected, so that a row on a boundary is counted in one window
      only. Other uses, such as ``extend WindowStart = BatchStart``, are fine.
    * Sentinel also sends each window as the API time range. The 1-day default and the "set in
      query" rules of ``exec_query`` do not apply; the window is the time range.
    * ADX has no API time range; the filter above is what windows the query.

    Aggregations (``summarize``, ``count``, ``dcount``, ``top`` ...) run per window, so by
    default they are NOT combined: a ``count()`` gives one row per window, and ``dcount`` or
    ``top`` results can overlap between windows. To combine them, pass ``merge_on`` (the columns
    to group by), ``agg_column`` (the columns to aggregate) and optionally ``agg_func``; the
    merged rows are then grouped and aggregated, which replaces the manual pandas step. This is
    correct for raw rows with any function. For per-window ``summarize`` output use only
    functions that can be re-combined: ``"sum"`` of counts or sums, ``"min"`` of mins, ``"max"``
    of maxes. A mean of means, a sum of ``dcount`` or a ``nunique`` of per-window values is
    wrong. For an average, ``summarize`` the sum and the count per window, re-aggregate both with
    ``"sum"``, then divide in pandas. Null keys are kept as their own group, as in KQL.

    Args:
        kql: The KQL query, a non-blank string. It must not begin with ``.`` on ADX (management
            commands cannot be windowed). Do not define ``BatchStart`` or ``BatchEnd`` yourself.
            It must filter on the window as described above.
        start: A ``datetime`` (the start of the period), or a ``timedelta`` meaning that long
            before ``end``. A naive datetime is taken to be UTC. A ``timedelta`` without ``end``
            aligns the windows to ``interval`` as described above.
        end: The end of the period (exclusive); defaults to now in UTC. A naive datetime is
            taken to be UTC.
        interval: The length of each window (default 1 day); must be positive.
        min_interval: Split a window that times out instead of failing. ``None`` (default)
            keeps the behaviour: a window that times out fails the batch. With a value, a window
            that fails with a timeout (HTTP 504/408 or a timeout error code on either
            target, no answer within ``timeout`` plus 30 seconds on Sentinel only, or a
            connection reset after it ran for half of ``timeout``) is
            cut into two halves that run one after another, oldest first, each with its own
            ``BatchStart``/``BatchEnd`` and API time range; a half that times out is cut again,
            and so on, until a half would be shorter than ``min_interval``, where the batch
            fails and the error names the smallest window tried. Must be positive and not longer
            than ``interval``. Every split is logged at WARNING. With ``cache=True`` a window
            that timed out is remembered until ``clear_cache()``, so a re-run splits it at once.
            The result is the same as without splitting, as the halves are concatenated in time
            order.
        target: ``"sentinel"`` (default) or ``"adx"``.
        timeout: Server-side timeout in seconds for each window (default 300); must be a
            positive ``int``.
        max_workers: How many windows run at once (default 1: one after another); an integer
            from 1 to 5. The cap is the Log Analytics limit of 5 concurrent queries per user;
            further queries only queue on the server, and the wait counts against ``timeout``.
            ADX's concurrency limit is shared by everyone using the cluster, so a high value can
            throttle (and slow down) other users; raise it with care.
        merge_on: Column name or names to group the merged rows by. Give it together with
            ``agg_column``. Must be non-empty and share no column with ``agg_column``.
        agg_column: Column name or names to aggregate. Give it together with ``merge_on``.
        agg_func: How to aggregate every ``agg_column`` (default ``"sum"``): any pandas
            ``GroupBy.agg`` function name (``"sum"``, ``"min"``, ``"max"``, ``"mean"``,
            ``"count"``, ``"first"``, ``"last"`` ...) or a callable. Passing it without
            ``agg_column`` is an error.
        cache: If true (default), each window may be served from, and kept in, the in-memory
            result cache. Pass false to run every window.
        debug: If true, also print this call's log records, at DEBUG and above, to the
            notebook cell's output while it runs, including the calls it makes (every window,
            also those run on worker threads). Default false. The lines are printed unredacted
            (debug output is not written to the file; be careful when sharing a notebook saved
            with debug output). See the logging
            section of the README.

    Returns:
        The window results concatenated in window order with a fresh index. Windows with no
        rows are skipped; if every window is empty, the first window's empty result is
        returned. ``df.attrs["kql_types"]`` is taken from the windows' results, adding columns
        that only later windows have.

        With ``merge_on`` and ``agg_column``: one row per distinct key sorted by the keys, with
        the ``merge_on`` columns then the ``agg_column`` columns and a fresh index (unless every
        window returned no columns, which is returned as is). ``kql_types`` keeps the key types;
        an aggregated column keeps its type for ``sum``, ``min``, ``max``, ``first`` and
        ``last``, becomes ``long`` for ``count``, ``nunique`` and ``size``, ``real`` for
        ``mean``, ``median``, ``std`` and ``var``, and has no entry otherwise.

    Raises:
        ConfigurationError: ``kql`` is not a string or is blank, ``start`` is not a datetime or
            timedelta, ``end`` is not a datetime or ``None``, ``interval`` is not a positive
            ``timedelta``, ``min_interval`` is not a positive ``timedelta`` or is longer than
            ``interval``, ``timeout`` is not a positive integer, ``cache`` or ``debug`` is not
            a bool, ``max_workers`` is not an
            integer from 1 to 5, ``start`` is not before ``end``, ``target`` is unknown, the ADX
            KQL is a management command, the KQL does not filter with ``>= BatchStart`` and
            ``< BatchEnd`` (checked before any query, on both targets), defines ``BatchStart`` or
            ``BatchEnd``, compares them in any other form,
            ``merge_on`` and ``agg_column`` are not given together, are empty, not column names,
            repeated or overlapping, ``agg_func`` is not a name or callable or is given without
            ``agg_column``, a named column is not in the result (checked after the first
            window, before the rest run), or any error ``exec_query`` raises for configuration
            or the cost guard. It is also a ``ValueError``.
        QueryError: A window failed. The message names the window (number, total and bounds)
            and the original error is chained. A window that still failed transiently after
            its retries fails this way too, and the message says it was retried. A window that
            timed out (HTTP 504/408 or a timeout error code, no answer within ``timeout``
            plus 30 seconds on Sentinel, or a connection reset after it ran for half of
            ``timeout``) fails with its subclass
            ``QueryTimeoutError`` and is not retried (see ``min_interval``).

    Example:
        >>> from datetime import datetime, timedelta
        >>> from security_notebook_tools import exec_batch_query
        >>> df = exec_batch_query(
        ...     "SigninLogs"
        ...     " | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd"
        ...     " | summarize n = count() by UserPrincipalName",
        ...     timedelta(days=30),  # the last 30 days, one query per day
        ...     merge_on="UserPrincipalName",
        ...     agg_column="n",  # sum of the per-day counts: one row per user
        ... )
        >>> df = exec_batch_query(
        ...     "T | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd",
        ...     datetime(2026, 1, 1), datetime(2026, 2, 1),
        ...     interval=timedelta(hours=6), max_workers=3,
        ... )
        >>> df = exec_batch_query(  # windows that time out are halved, down to 1 hour
        ...     "T | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd",
        ...     timedelta(days=90), interval=timedelta(days=5), min_interval=timedelta(hours=1),
        ... )
    """
    with public_call(
        logger,
        "exec_batch_query",
        target=target,
        interval=interval,
        min_interval=min_interval,
        timeout=timeout,
        max_workers=max_workers,
        cache=cache,
        debug=debug,
        merge_on=merge_on,
        agg_column=agg_column,
        kql=fingerprint(kql),
    ) as call:
        now = datetime.now(UTC)
        _check_kql(kql)
        validate_window_args(start, end, interval)
        if interval <= timedelta(0):
            raise ConfigurationError(f"interval must be positive, got {interval}.")
        if min_interval is not None:
            if not isinstance(min_interval, timedelta) or min_interval <= timedelta(0):
                raise ConfigurationError(
                    f"min_interval must be a positive timedelta or None, got {min_interval!r}."
                )
            if min_interval > interval:
                raise ConfigurationError(
                    f"min_interval ({min_interval}) must not be longer than interval ({interval})."
                )
        _check_timeout(timeout)
        _check_bool("cache", cache)
        _check_bool("debug", debug)
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or not 1 <= max_workers <= MAX_WORKERS
        ):
            raise ConfigurationError(
                f"max_workers must be an integer from 1 to {MAX_WORKERS}, got {max_workers!r}."
            )
        if target not in ("sentinel", "adx"):
            raise ConfigurationError(f"Unknown target {target!r}; use 'sentinel' or 'adx'.")
        keys: list[str] = []
        cols: list[str] = []
        if merge_on is None or agg_column is None:
            if merge_on is not None or agg_column is not None:
                raise ConfigurationError("merge_on and agg_column must be given together.")
            if agg_func is not _DEFAULT_AGG:
                raise ConfigurationError("agg_func needs merge_on and agg_column.")
        else:
            keys, cols = _check_agg_args(merge_on, agg_column, agg_func)
        end_utc = _utc(end) if end is not None else now
        start_utc = end_utc - start if isinstance(start, timedelta) else _utc(start)
        if start_utc >= end_utc:
            raise ConfigurationError(f"start ({start_utc}) must be before end ({end_utc}).")

        if target == "adx" and kql.lstrip().startswith("."):
            raise ConfigurationError(
                "Management commands cannot be run in windows; use exec_query for them."
            )
        _check_batch_filter(kql)

        if target == "sentinel":
            # Once for the whole range, not per window (the windows skip the check).
            _warn_retention(kql, (start_utc, end_utc))

        # Relative to now: cut on a fixed grid so re-runs send the same windows and hit the cache.
        aligned = isinstance(start, timedelta) and end is None
        windows = _windows(start_utc, end_utc, interval, aligned=aligned)
        total = len(windows)

        stop = threading.Event()
        jobs = [
            partial(
                _run_window, index, total, bounds, kql, target, timeout, cache, stop, min_interval
            )
            for index, bounds in enumerate(windows, start=1)
        ]
        logger.info(
            "%d windows of %s%s from %s to %s (max_workers=%d)",
            total,
            interval,
            " (aligned to the interval)" if aligned else "",
            start_utc,
            end_utc,
            max_workers,
        )
        results = [jobs[0]()]  # alone first: fails once on a bad query or config, warms caches
        if keys and len(results[0].columns):
            _check_columns(results[0], keys + cols)  # before the remaining windows run
        if max_workers > 1 and total > 2:
            results += _run_parallel(jobs[1:], max_workers, stop)
        else:
            results += [job() for job in jobs[1:]]
        merged = _merge(results)
        logger.info("Batch finished: %d windows, total rows=%d", total, len(merged))
        if keys and len(merged.columns):
            rows = len(merged)
            merged = _aggregate(merged, keys, cols, agg_func)
            logger.info(
                "Re-aggregated %d rows into %d by %s (%s of %s)",
                rows,
                len(merged),
                keys,
                agg_func if isinstance(agg_func, str) else getattr(agg_func, "__name__", agg_func),
                cols,
            )
        call.update(windows=total, rows=len(merged), columns=len(merged.columns))
        return merged
