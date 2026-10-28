"""The public ``merge_dataframe`` function: combine rows by key without running a query."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

import pandas as pd

from ._batch import (
    _DEFAULT_AGG,
    PARTIAL_ATTR,
    PARTIAL_WINDOWS_ATTR,
    _aggregate,
    _check_agg_args,
    _merge,
)
from ._errors import public_call
from ._exceptions import ConfigurationError
from ._query import _check_bool

logger = logging.getLogger("security_notebook_tools.merge")


def merge_dataframe(
    df: pd.DataFrame | Sequence[pd.DataFrame],
    merge_on: str | Sequence[str],
    agg_column: str | Sequence[str],
    agg_func: str | Callable[..., Any] = _DEFAULT_AGG,
    *,
    debug: bool = False,
) -> pd.DataFrame:
    """Merge rows that share a key: group by ``merge_on`` and aggregate ``agg_column``.

    This is the ``merge_on``/``agg_column``/``agg_func`` step of ``exec_batch_query`` on data
    you already have, for example several ``exec_query`` results over different periods or
    workspaces, a cached batch you want to combine another way, or any DataFrame. No query is
    run.

    A list of DataFrames is first concatenated in order, as the windows of a batch are: frames
    with no rows are skipped, and ``kql_types`` is combined (the first frame that has a column
    sets its type). The rows are then grouped by ``merge_on`` and every ``agg_column`` is
    aggregated with ``agg_func``. Null keys are kept as their own group, as in KQL. Other columns
    are dropped. ``df`` is not modified.

    The same caveat as for batches applies to rows that are already aggregates: re-combine them
    only with functions that compose (``"sum"`` of counts or sums, ``"min"`` of mins, ``"max"``
    of maxes). A mean of means, a sum of ``dcount`` or a ``nunique`` of per-period values is
    wrong; for an average, aggregate the sum and the count with ``"sum"`` and divide in pandas.

    Args:
        df: A DataFrame, or a list (any sequence) of DataFrames, usually ``exec_query`` or
            ``exec_batch_query`` results.
        merge_on: Column name or names to group the rows by. Must be non-empty and share no
            column with ``agg_column``.
        agg_column: Column name or names to aggregate.
        agg_func: How to aggregate every ``agg_column`` (default ``"sum"``): any pandas
            ``GroupBy.agg`` function name (``"sum"``, ``"min"``, ``"max"``, ``"mean"``,
            ``"count"``, ``"first"``, ``"last"`` ...) or a callable.
        debug: If true, also print this call's log records, at DEBUG and above, to the
            notebook cell's output while it runs. Default false. The lines are printed unredacted
            (debug output is not written to the file; be careful when sharing a notebook saved
            with debug output). See the logging section of the README.

    Returns:
        ``attrs["partial"]`` is True if any input had it True, else False (and
        ``attrs["partial_windows"]`` is carried over if an input had it).

        A new DataFrame with one row per distinct key sorted by the keys, with the ``merge_on``
        columns then the ``agg_column`` columns and a fresh index. If the (concatenated) input
        has no columns at all, an empty copy is returned as is. ``kql_types`` keeps the key
        types; an aggregated column keeps its type for ``sum``, ``min``, ``max``, ``first`` and
        ``last``, becomes ``long`` for ``count``, ``nunique`` and ``size``, ``real`` for
        ``mean``, ``median``, ``std`` and ``var``, and has no entry otherwise.

    Raises:
        ConfigurationError: ``df`` is not a DataFrame or a non-empty sequence of DataFrames;
            ``merge_on`` or ``agg_column`` is empty, not column names, repeated or overlapping;
            ``agg_func`` is not a name or callable; ``debug`` is not a bool; a named column is
            not in the DataFrame; or pandas cannot aggregate. It is also a ``ValueError``.

    Example:
        >>> from datetime import datetime
        >>> from security_notebook_tools import exec_query, merge_dataframe
        >>> kql = "SigninLogs | summarize n = count() by UserPrincipalName"
        >>> jan = exec_query(kql, timespan=(datetime(2026, 1, 1), datetime(2026, 2, 1)))
        >>> mar = exec_query(kql, timespan=(datetime(2026, 3, 1), datetime(2026, 4, 1)))
        >>> both = merge_dataframe([jan, mar], "UserPrincipalName", "n")  # one row per user
        >>> raw = exec_query("SigninLogs | project UserPrincipalName, AppDisplayName")
        >>> apps = merge_dataframe(raw, "UserPrincipalName", "AppDisplayName", "nunique")
    """
    frames = [df] if isinstance(df, pd.DataFrame) else df
    with public_call(
        logger,
        "merge_dataframe",
        debug=debug,
        frames=len(frames) if isinstance(frames, Sequence) else None,
        merge_on=merge_on,
        agg_column=agg_column,
        agg_func=agg_func if isinstance(agg_func, str) else getattr(agg_func, "__name__", None),
    ) as call:
        if (
            isinstance(frames, str)
            or not isinstance(frames, Sequence)
            or not frames
            or not all(isinstance(item, pd.DataFrame) for item in frames)
        ):
            raise ConfigurationError(
                "df must be a pandas DataFrame or a non-empty list of DataFrames, "
                f"got {type(df).__name__}."
            )
        keys, cols = _check_agg_args(merge_on, agg_column, agg_func)
        _check_bool("debug", debug)
        merged = _merge(list(frames)) if len(frames) > 1 else frames[0]
        if not len(merged.columns):
            out = merged.copy()
        else:
            out = _aggregate(merged, keys, cols, agg_func, log=logger, where="the DataFrame")
        # groupby does not carry attrs reliably: restate whether any input was partial
        out.attrs[PARTIAL_ATTR] = bool(merged.attrs.get(PARTIAL_ATTR))
        if PARTIAL_WINDOWS_ATTR in merged.attrs:
            out.attrs[PARTIAL_WINDOWS_ATTR] = list(merged.attrs[PARTIAL_WINDOWS_ATTR])
        logger.info(
            "Merged %d rows into %d by %s (%s of %s)",
            len(merged),
            len(out),
            keys,
            agg_func if isinstance(agg_func, str) else getattr(agg_func, "__name__", agg_func),
            cols,
        )
        call.update(rows=len(out), columns=len(out.columns))
        return out
