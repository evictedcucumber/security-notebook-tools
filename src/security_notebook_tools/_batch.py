"""The public ``exec_batch_query`` entry point: one query per consecutive time window."""

from __future__ import annotations

import logging
import random
import re
import threading
from collections.abc import Callable
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from functools import partial

import pandas as pd

from ._exceptions import ConfigurationError, QueryError, QueryThrottledError
from ._kql import strip_noise
from ._log import verbose_logging
from ._query import DEFAULT_TIMEOUT, Target, _query_adx, _query_sentinel
from ._timerange import has_time_filter

logger = logging.getLogger("security_notebook_tools.batch")

_BATCH_VAR = re.compile(r"\bBatch(?:Start|End)\b")

# Log Analytics runs 5 queries per user at once and queues the rest on the server, where the
# wait counts against the timeout; more workers than that gain nothing. ADX shares its limit
# with everyone using the cluster.
MAX_WORKERS = 5
# A throttled window is retried this many times, waiting a random time (full jitter) between 0
# and min(_BACKOFF_CAP, _BACKOFF_BASE * 2 ** retries_so_far) seconds.
_MAX_RETRIES = 4
_BACKOFF_BASE = 2.0
_BACKOFF_CAP = 30.0


class _Stopped(Exception):
    """A worker gave up waiting to retry because the batch is already failing."""


def _utc(value: datetime) -> datetime:
    """Return ``value`` in UTC; a naive datetime is taken to be UTC already."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _literal(value: datetime) -> str:
    """Format a UTC datetime as a KQL ``datetime("...")`` literal.

    The string form is used because the bare form ``datetime(2026-01-01T00:00:00Z)`` is rejected
    by the KQL tokenizer behind the cost guard (ambiguous number literal).
    """
    return f'datetime("{value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}")'


def _windows(
    start: datetime, end: datetime, interval: timedelta
) -> list[tuple[datetime, datetime]]:
    """Split ``[start, end)`` into consecutive windows of ``interval``; the last is clipped."""
    windows = []
    cursor = start
    while cursor < end:
        windows.append((cursor, min(cursor + interval, end)))
        cursor += interval
    return windows


def _merge(results: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate the non-empty window results and combine their ``kql_types``."""
    types: dict[str, str] = {}
    for df in results:
        for name, kql_type in df.attrs.get("kql_types", {}).items():
            types.setdefault(name, kql_type)
    non_empty = [df for df in results if not df.empty]
    if non_empty:
        merged = pd.concat(non_empty, ignore_index=True)
    elif results:
        merged = results[0]
    else:
        merged = pd.DataFrame()
    if types:
        merged.attrs["kql_types"] = types
    return merged


def _sleep(delay: float, stop: threading.Event) -> bool:
    """Wait ``delay`` seconds; return True if ``stop`` was set meanwhile (tests patch this)."""
    return stop.wait(delay)


def _run_window(
    index: int,
    total: int,
    bounds: tuple[datetime, datetime],
    kql: str,
    target: Target,
    timeout: int,
    stop: threading.Event,
) -> pd.DataFrame:
    """Run one window, retrying throttled attempts; wrap a failure with the window's name.

    Raises:
        QueryError: The window failed, or was still throttled after ``_MAX_RETRIES`` retries.
        _Stopped: ``stop`` was set while waiting to retry.
    """
    w_start, w_end = bounds
    prefix = f"let BatchStart = {_literal(w_start)}; let BatchEnd = {_literal(w_end)};\n"
    retries = 0
    while True:
        try:
            if target == "sentinel":
                tables = _query_sentinel(prefix + kql, bounds, timeout)
            else:
                tables = _query_adx(prefix + kql, timeout)
            break
        except QueryError as exc:
            if not isinstance(exc, QueryThrottledError) or retries >= _MAX_RETRIES:
                raise QueryError(
                    f"Batch window {index}/{total} ({w_start} to {w_end}) failed: {exc}"
                ) from exc
            delay = random.uniform(0, min(_BACKOFF_CAP, _BACKOFF_BASE * 2**retries))
            retries += 1
            logger.warning(
                "Batch %d/%d throttled; retrying in %.1f s (attempt %d/%d)",
                index,
                total,
                delay,
                retries,
                _MAX_RETRIES,
            )
            if _sleep(delay, stop):
                raise _Stopped from exc
    df = tables[0] if tables else pd.DataFrame()
    logger.info("Batch %d/%d: %s → %s, rows=%d", index, total, w_start, w_end, len(df))
    return df


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
        futures = [pool.submit(job) for job in jobs]
        wait(futures, return_when=FIRST_EXCEPTION)
        failed = any(not f.cancelled() and f.exception() is not None for f in futures if f.done())
        if failed:
            stop.set()
            pool.shutdown(wait=True, cancel_futures=True)
    except BaseException:
        stop.set()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    for future in futures:  # lowest index first; all are finished or cancelled by now
        error = None if future.cancelled() else future.exception()
        if error is not None and not isinstance(error, _Stopped):
            raise error
    return [future.result() for future in futures]


def exec_batch_query(
    kql: str,
    start: datetime | timedelta,
    end: datetime | None = None,
    *,
    interval: timedelta = timedelta(days=1),
    target: Target = "sentinel",
    timeout: int = DEFAULT_TIMEOUT,
    max_workers: int = 1,
    verbose: bool = False,
) -> pd.DataFrame:
    """Run the same KQL once per consecutive time window and merge the results.

    Use it when a query is too big for one run or needs a longer period than one query allows.
    The period from ``start`` to ``end`` is split into windows of ``interval``:
    ``[start, start + interval)``, ``[start + interval, start + 2 * interval)`` and so on, with
    the last window clipped to ``end``. Each run goes through the same checks as ``exec_query``,
    so the Sentinel cost guard applies to every window.

    By default the windows run one after another, oldest first. With ``max_workers`` above 1 the
    first window still runs alone (so a bad query or configuration fails once, and the client,
    credential and table-plan caches are warm), then the rest run in parallel on up to
    ``max_workers`` threads. The result order does not depend on which window finishes first.
    The first failure stops the batch: windows not yet started are skipped, running ones are
    waited for, and the error of the lowest-numbered failed window is raised.

    A window that the service throttles (HTTP 429, "too many requests") is retried up to 4
    times, waiting a random time between 0 and 2, 4, 8 and 16 seconds (full jitter, capped at
    30 s) and logging a warning each time. After that it fails like any other error. Other
    errors are not retried.

    Window bounds reach the query in two ways:

    * ``BatchStart`` and ``BatchEnd`` are defined as ``datetime`` values by ``let`` statements
      added in front of your KQL, so the query can use them. Filter with
      ``TimeGenerated >= BatchStart and TimeGenerated < BatchEnd`` so that a row on a boundary
      is counted in one window only.
    * Sentinel only: each window is also sent as the API time range, so the windowing holds
      even if the KQL ignores ``BatchStart`` and ``BatchEnd``. The 1-day default and the "set in
      query" rules of ``exec_query`` do not apply; the window is the time range. A warning is
      logged once if the KQL has its own time filter that does not use ``BatchStart`` or
      ``BatchEnd`` (both limits then apply).
    * ADX has no API time range, so the KQL must use both ``BatchStart`` and ``BatchEnd``
      (outside comments and strings); otherwise every window would return the same rows and a
      ``ConfigurationError`` is raised.

    Aggregations (``summarize``, ``count``, ``dcount``, ``top`` ...) run per window and are NOT
    combined: a ``count()`` gives one row per window, and ``dcount`` or ``top`` results can
    overlap between windows. Re-aggregate the merged DataFrame in pandas.

    Args:
        kql: The KQL query. It must not begin with ``.`` on ADX (management commands cannot be
            windowed). Do not define ``BatchStart`` or ``BatchEnd`` yourself.
        start: The start of the period, or a ``timedelta`` meaning that long before ``end``.
            A naive datetime is taken to be UTC.
        end: The end of the period (exclusive); defaults to now in UTC. A naive datetime is
            taken to be UTC.
        interval: The length of each window (default 1 day); must be positive.
        target: ``"sentinel"`` (default) or ``"adx"``.
        timeout: Server-side timeout in seconds for each window (default 300); must be
            positive.
        max_workers: How many windows run at once (default 1: one after another); an integer
            from 1 to 5. The cap is the Log Analytics limit of 5 concurrent queries per user;
            further queries only queue on the server, and the wait counts against ``timeout``.
            ADX's concurrency limit is shared by everyone using the cluster, so a high value can
            throttle (and slow down) other users; raise it with care.
        verbose: If true, print this call's logs (INFO and above) to the cell output.

    Returns:
        The window results concatenated in window order with a fresh index. Windows with no
        rows are skipped; if every window is empty, the first window's empty result is
        returned. ``df.attrs["kql_types"]`` is taken from the windows' results, adding columns
        that only later windows have.

    Raises:
        ConfigurationError: ``interval`` or ``timeout`` is not positive, ``max_workers`` is not an
            integer from 1 to 5, ``start`` is not before ``end``, ``target`` is unknown, the ADX
            KQL is a management command or does not use both ``BatchStart`` and ``BatchEnd``,
            or any error ``exec_query`` raises for configuration or the cost guard. It is also
            a ``ValueError``.
        QueryError: A window failed. The message names the window (number, total and bounds)
            and the original error is chained. A window still throttled after its retries fails
            this way too.

    Example:
        >>> from datetime import datetime, timedelta
        >>> from security_notebook_tools import exec_batch_query
        >>> df = exec_batch_query(
        ...     "SigninLogs"
        ...     " | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd"
        ...     " | summarize n = count() by UserPrincipalName",
        ...     timedelta(days=30),  # the last 30 days, one query per day
        ... )
        >>> df = df.groupby("UserPrincipalName", as_index=False)["n"].sum()  # re-aggregate
        >>> df = exec_batch_query("T", datetime(2026, 1, 1), datetime(2026, 2, 1),
        ...                       interval=timedelta(hours=6), max_workers=3)
    """
    now = datetime.now(UTC)
    if interval <= timedelta(0):
        raise ConfigurationError(f"interval must be positive, got {interval}.")
    if timeout <= 0:
        raise ConfigurationError(f"timeout must be a positive number of seconds, got {timeout}.")
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
    end_utc = _utc(end) if end is not None else now
    start_utc = end_utc - start if isinstance(start, timedelta) else _utc(start)
    if start_utc >= end_utc:
        raise ConfigurationError(f"start ({start_utc}) must be before end ({end_utc}).")

    if target == "adx":
        if kql.lstrip().startswith("."):
            raise ConfigurationError(
                "Management commands cannot be run in windows; use exec_query for them."
            )
        used = set(_BATCH_VAR.findall(strip_noise(kql)))
        if used != {"BatchStart", "BatchEnd"}:
            raise ConfigurationError(
                "With target='adx' the KQL must use both BatchStart and BatchEnd (for example "
                "`where Timestamp >= BatchStart and Timestamp < BatchEnd`); ADX has no API time "
                "range, so otherwise every window would return the same rows."
            )

    windows = _windows(start_utc, end_utc, interval)
    total = len(windows)
    warn_filter = target == "sentinel" and not _BATCH_VAR.search(strip_noise(kql))
    warn_filter = warn_filter and has_time_filter(kql)

    stop = threading.Event()
    jobs = [
        partial(_run_window, index, total, bounds, kql, target, timeout, stop)
        for index, bounds in enumerate(windows, start=1)
    ]
    with verbose_logging(verbose):
        logger.info(
            "%d windows of %s from %s to %s (max_workers=%d)",
            total,
            interval,
            start_utc,
            end_utc,
            max_workers,
        )
        if warn_filter:
            logger.warning(
                "Query has its own time filter that does not use BatchStart/BatchEnd; it and "
                "each window's time range both apply, so the narrower range wins."
            )
        results = [jobs[0]()]  # alone first: fails once on a bad query or config, warms caches
        if max_workers > 1 and total > 2:
            results += _run_parallel(jobs[1:], max_workers, stop)
        else:
            results += [job() for job in jobs[1:]]
        merged = _merge(results)
        logger.info("Batch finished: %d windows, total rows=%d", total, len(merged))
    return merged
