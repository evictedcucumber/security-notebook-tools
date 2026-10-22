"""The public ``exec_batch_query`` entry point: one query per consecutive time window."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

import pandas as pd

from ._exceptions import ConfigurationError, QueryError
from ._kql import strip_noise
from ._log import verbose_logging
from ._query import DEFAULT_TIMEOUT, Target, _query_adx, _query_sentinel
from ._timerange import has_time_filter

logger = logging.getLogger("security_notebook_tools.batch")

_BATCH_VAR = re.compile(r"\bBatch(?:Start|End)\b")


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


def exec_batch_query(
    kql: str,
    start: datetime | timedelta,
    end: datetime | None = None,
    *,
    interval: timedelta = timedelta(days=1),
    target: Target = "sentinel",
    timeout: int = DEFAULT_TIMEOUT,
    verbose: bool = False,
) -> pd.DataFrame:
    """Run the same KQL once per consecutive time window and merge the results.

    Use it when a query is too big for one run or needs a longer period than one query allows.
    The period from ``start`` to ``end`` is split into windows of ``interval``:
    ``[start, start + interval)``, ``[start + interval, start + 2 * interval)`` and so on, with
    the last window clipped to ``end``. The windows run one after another, oldest first (Log
    Analytics throttles concurrent queries), and the first failure stops the batch with no
    retries. Each run goes through the same checks as ``exec_query``, so the Sentinel cost guard
    applies to every window.

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
        verbose: If true, print this call's logs (INFO and above) to the cell output.

    Returns:
        The window results concatenated in window order with a fresh index. Windows with no
        rows are skipped; if every window is empty, the first window's empty result is
        returned. ``df.attrs["kql_types"]`` is taken from the windows' results, adding columns
        that only later windows have.

    Raises:
        ConfigurationError: ``interval`` or ``timeout`` is not positive, ``start`` is not before
            ``end``, ``target`` is unknown, the ADX KQL is a management command or does not use
            both ``BatchStart`` and ``BatchEnd``, or any error ``exec_query`` raises for
            configuration or the cost guard. It is also a ``ValueError``.
        QueryError: A window failed. The message names the window (number, total and bounds)
            and the original error is chained.

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
        ...                       interval=timedelta(hours=6))
    """
    now = datetime.now(UTC)
    if interval <= timedelta(0):
        raise ConfigurationError(f"interval must be positive, got {interval}.")
    if timeout <= 0:
        raise ConfigurationError(f"timeout must be a positive number of seconds, got {timeout}.")
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

    results: list[pd.DataFrame] = []
    with verbose_logging(verbose):
        logger.info("%d windows of %s from %s to %s", total, interval, start_utc, end_utc)
        if warn_filter:
            logger.warning(
                "Query has its own time filter that does not use BatchStart/BatchEnd; it and "
                "each window's time range both apply, so the narrower range wins."
            )
        for index, (w_start, w_end) in enumerate(windows, start=1):
            prefix = f"let BatchStart = {_literal(w_start)}; let BatchEnd = {_literal(w_end)};\n"
            try:
                if target == "sentinel":
                    tables = _query_sentinel(prefix + kql, (w_start, w_end), timeout)
                else:
                    tables = _query_adx(prefix + kql, timeout)
            except QueryError as exc:
                raise QueryError(
                    f"Batch window {index}/{total} ({w_start} to {w_end}) failed: {exc}"
                ) from exc
            df = tables[0] if tables else pd.DataFrame()
            logger.info("Batch %d/%d: %s → %s, rows=%d", index, total, w_start, w_end, len(df))
            results.append(df)
        merged = _merge(results)
        logger.info("Batch finished: %d windows, total rows=%d", total, len(merged))
    return merged
