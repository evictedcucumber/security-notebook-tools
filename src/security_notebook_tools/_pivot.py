"""The public ``pivot_dataframe`` function: cross-tabulate a query result."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

import pandas as pd

from ._batch import _DEFAULT_AGG, _agg_type, _names
from ._errors import public_call
from ._exceptions import ConfigurationError
from ._format import KQL_TYPES_ATTR, PIVOT_TOTALS_ATTR
from ._log import dependency_call

logger = logging.getLogger("security_notebook_tools.pivot")

_NULL_LABEL = "(null)"
_TOTAL = "Total"
# Internal column names; NUL cannot appear in a name a user would type.
_COUNT = "\0count"
_ALL = "\0all"


def pivot_dataframe(
    df: pd.DataFrame,
    index: str | Sequence[str],
    columns: str | Sequence[str],
    values: str | Sequence[str] | None = None,
    *,
    agg_func: str | Callable[..., Any] = _DEFAULT_AGG,
    fill_value: Any = None,
    totals: bool = False,
    max_columns: int | None = 100,
    debug: bool = False,
) -> pd.DataFrame:
    """Pivot a query result: one row per ``index`` key, one column per ``columns`` key.

    Without ``values`` the cells count the rows of each combination (missing combinations are 0,
    typed ``long``). With ``values`` the cells aggregate those columns with ``agg_func`` (default
    ``"sum"``); missing combinations are null unless ``fill_value`` is given. Null keys are kept
    as their own group and rows and columns are sorted by key.

    The result is a flat DataFrame with a new ``RangeIndex``: the ``index`` columns first (as
    regular columns), then one string-named column per distinct ``columns`` key. A header is the
    key as text (``"(null)"`` for a null key; several ``columns`` joined with ``"_"``), prefixed
    ``"<value>_"`` when several ``values`` are given. Its ``kql_types`` carry the original types
    of the ``index`` columns and the aggregated type of the cells (``count``/``nunique`` are
    ``long``, ``mean``/``median``/``std``/``var`` are ``real``, ``sum``/``min``/``max``/``first``/
    ``last`` keep the value's type; unknown for a callable), so it goes straight into
    ``format_dataframe``. With ``totals`` its ``pivot_totals`` attr records which column(s) and
    row are totals (``{"columns": [...], "row": <index label or None>, "label_column": <first
    index column>}``), so ``format_dataframe`` sets them apart from rows or columns that merely
    hold the text ``"Total"``. ``df`` is not modified.

    Args:
        df: The DataFrame to pivot, usually the result of ``exec_query``.
        index: Column name(s) whose distinct values become the rows.
        columns: Column name(s) whose distinct values become the new columns.
        values: Column name(s) to aggregate into the cells. ``None`` counts rows.
        agg_func: A pandas aggregation name (``"sum"``, ``"mean"``, ``"max"``, ``"nunique"``, ...)
            or a callable, applied to ``values``. Not allowed without ``values``.
        fill_value: Cell value for combinations with no rows. Not allowed without ``values``
            (which fills with 0).
        totals: Add a ``"Total"`` column (per row) and a ``"Total"`` row (per column, shown in the
            first ``index`` column), aggregated with the same function over the underlying rows.
            With several ``values`` the total columns are named ``"<value>_Total"``.
        max_columns: Raise instead of building more than this many value columns (totals not
            counted), as a guard against a high-cardinality ``columns`` key. ``None`` disables.
        debug: If true, also print this call's log records, at DEBUG and above, to the
            notebook cell's output while it runs. Default false. The lines are printed unredacted
            (debug output is not written to the file; be careful when sharing a notebook saved
            with debug output). See the
            logging section of the README.

    Returns:
        A new DataFrame as described above. If ``df`` has no rows, only the ``index`` columns (and
        the ``"Total"`` column if requested).

    Raises:
        ConfigurationError: An argument is invalid: a name is empty, repeated, used in more than
            one of ``index``, ``columns`` and ``values`` or missing from ``df``; ``agg_func`` or
            ``fill_value`` is given without ``values``; ``max_columns`` is not a positive integer
            or ``None``; there are more columns than ``max_columns``; a generated header equals an
            ``index`` column or another header; or pandas cannot aggregate.

    Example:
        >>> df = exec_query("SigninLogs | project UserPrincipalName, ResultType, Duration")
        >>> format_dataframe(pivot_dataframe(df, "UserPrincipalName", "ResultType"))
        >>> by_user = pivot_dataframe(
        ...     df, "UserPrincipalName", "ResultType", "Duration", agg_func="mean", totals=True
        ... )
        >>> format_dataframe(by_user)
    """
    shape = getattr(df, "shape", None)
    with public_call(
        logger,
        "pivot_dataframe",
        debug=debug,
        rows=shape[0] if shape else None,
        columns=shape[1] if shape else None,
        index=index,
        pivot_columns=columns,
        values=values,
        agg_func=agg_func if isinstance(agg_func, str) else getattr(agg_func, "__name__", None),
        totals=totals,
        max_columns=max_columns,
    ) as call:
        if not isinstance(df, pd.DataFrame):
            raise ConfigurationError(f"df must be a pandas DataFrame, got {type(df).__name__}.")
        idx = _names(_listable(index, "index"), "index")
        cols = _names(_listable(columns, "columns"), "columns")
        vals = None if values is None else _names(_listable(values, "values"), "values")
        if vals is None and (agg_func is not _DEFAULT_AGG or fill_value is not None):
            raise ConfigurationError(
                "agg_func and fill_value need values; without values the rows are counted."
            )
        if not callable(agg_func) and not (isinstance(agg_func, str) and agg_func):
            raise ConfigurationError(
                f"agg_func must be a non-empty string or a callable, got {agg_func!r}."
            )
        if max_columns is not None and (
            isinstance(max_columns, bool) or not isinstance(max_columns, int) or max_columns < 1
        ):
            raise ConfigurationError(
                f"max_columns must be a positive integer or None, got {max_columns!r}."
            )
        if not isinstance(totals, bool):
            raise ConfigurationError(f"totals must be a bool, got {totals!r}.")
        named = idx + cols + (vals or [])
        if len(set(named)) != len(named):
            shared = sorted({n for n in named if named.count(n) > 1})
            raise ConfigurationError(
                f"Column(s) {shared} are used more than once across index, columns and values."
            )
        missing = [n for n in named if n not in df.columns]
        if missing:
            raise ConfigurationError(
                f"Column(s) {missing} are not in the DataFrame; "
                f"available columns: {list(df.columns)}."
            )

        old = df.attrs.get(KQL_TYPES_ATTR, {})
        cell_names = vals or [None]
        prefixed = vals is not None and len(vals) > 1

        def header(value: str | None, key: tuple[Any, ...]) -> str:
            text = "_".join(_NULL_LABEL if pd.isna(k) else str(k) for k in key)
            return f"{value}_{text}" if prefixed else text

        def total_name(value: str | None) -> str:
            return f"{value}_{_TOTAL}" if prefixed else _TOTAL

        total_columns = [total_name(v) for v in cell_names] if totals else []

        if df.empty:
            out = df[idx].reset_index(drop=True)
            if totals:
                for value in cell_names:
                    out[total_name(value)] = pd.Series(dtype="int64" if vals is None else "float64")
            pairs = list(zip(total_columns, cell_names, strict=False))
            result = _finish(out, idx, pairs, old, agg_func, total_columns)
            call.update(rows=len(result), columns=len(result.columns))
            return result

        work = df[named].copy()
        work[_ALL] = 0
        try:
            with dependency_call(logger, "pandas distinct column keys", columns=cols):
                distinct = (
                    work[cols]
                    .drop_duplicates()
                    .sort_values(cols, na_position="last", kind="stable")
                )
        except Exception as exc:
            raise ConfigurationError(f"Cannot pivot on {cols}: {exc}") from exc
        keys = list(distinct.itertuples(index=False, name=None))
        if max_columns is not None and len(keys) * len(cell_names) > max_columns:
            raise ConfigurationError(
                f"{cols} has {len(keys)} distinct keys, which would create "
                f"{len(keys) * len(cell_names)} columns, more than max_columns={max_columns}. "
                "Filter the data or raise or disable (None) max_columns."
            )
        headers = {(v, k): header(v, k) for v in cell_names for k in keys}
        pairs = [(h, v) for (v, _), h in headers.items()]
        pairs += list(zip(total_columns, cell_names, strict=False))
        generated = [name for name, _ in pairs]
        clash = sorted({h for h in generated if generated.count(h) > 1 or h in idx})
        if clash:
            raise ConfigurationError(
                f"Generated column header(s) {clash} collide with an index column or each other; "
                "rename the index columns or filter the data."
            )

        def aggregate(by: list[str], level: int = logging.DEBUG) -> pd.DataFrame:
            try:
                with dependency_call(logger, "pandas groupby aggregation", level=level, by=by):
                    grouped = work.groupby(by, dropna=False, sort=True)
                    if vals is None:
                        out = grouped.size().rename(_COUNT).reset_index()
                    else:
                        out = grouped[vals].agg(agg_func).reset_index()
            except Exception as exc:
                name = (
                    agg_func
                    if isinstance(agg_func, str)
                    else getattr(agg_func, "__name__", repr(agg_func))
                )
                raise ConfigurationError(
                    f"Cannot aggregate {vals or 'row counts'} with {name!r} grouped by {by}: {exc}"
                ) from exc
            return out

        cells = aggregate(idx + cols, logging.INFO)
        cell_cols = [_COUNT] if vals is None else vals
        with dependency_call(logger, "pandas pivot reshape", keys=len(keys)):
            row_ids = cells.groupby(idx, dropna=False, sort=False).ngroup()
            rows = cells[idx].drop_duplicates().reset_index(drop=True)
            labels = [tuple(k) for k in cells[cols].itertuples(index=False, name=None)]
            parts = []
            for value, col in zip(cell_names, cell_cols, strict=True):
                long = pd.DataFrame(
                    {
                        "r": row_ids.to_numpy(),
                        "h": [header(value, k) for k in labels],
                        "v": cells[col],
                    }
                )
                wide = long.pivot(index="r", columns="h", values="v")
                parts.append(wide.reindex(columns=[headers[value, k] for k in keys]))
            body = pd.concat(parts, axis=1).reset_index(drop=True)
            if vals is None:
                body = body.fillna(0).astype("int64")
            elif fill_value is not None:
                body = body.fillna(fill_value)
            out = pd.concat([rows, body], axis=1)

        if totals:
            row_totals = aggregate(idx)  # same keys, same sort order as ``rows``
            col_totals = aggregate(cols)
            grand = aggregate([_ALL])
            extra = {}
            for value, col in zip(cell_names, cell_cols, strict=True):
                out[total_name(value)] = row_totals[col].to_numpy()
                for k, cell in zip(
                    col_totals[cols].itertuples(index=False, name=None),
                    col_totals[col],
                    strict=True,
                ):
                    extra[header(value, k)] = cell
                extra[total_name(value)] = grand[col].iloc[0]
            first = {n: (_TOTAL if n == idx[0] else None) for n in idx}
            total_row = pd.DataFrame([{**first, **extra}], columns=list(out.columns))
            out = pd.concat([out, total_row], ignore_index=True)
        result = _finish(out, idx, pairs, old, agg_func, total_columns)
        call.update(rows=len(result), columns=len(result.columns))
        return result


def _listable(value: Any, label: str) -> Any:
    """Return ``value`` if it is a string or a non-string iterable, else raise."""
    if isinstance(value, str) or (hasattr(value, "__iter__") and not isinstance(value, dict)):
        return value
    raise ConfigurationError(f"{label} must be a column name or a list of names, got {value!r}.")


def _finish(
    out: pd.DataFrame,
    idx: list[str],
    pairs: list[tuple[str, str | None]],
    old: dict[str, str],
    agg_func: str | Callable[..., Any],
    total_columns: list[str],
) -> pd.DataFrame:
    """Attach ``kql_types`` and ``pivot_totals`` to ``out``.

    ``pairs`` maps each cell column to its source value. If ``total_columns`` is non-empty, the
    last row of ``out`` (if it has rows) is the total row.
    """
    types = {n: old[n] for n in idx if n in old}
    for name, value in pairs:
        kql_type = "long" if value is None else _agg_type(agg_func, old.get(value))
        if kql_type is not None:
            types[name] = kql_type
    if types:
        out.attrs[KQL_TYPES_ATTR] = types
    out.attrs.pop(PIVOT_TOTALS_ATTR, None)  # never inherit a stale one from ``df``
    if total_columns:
        out.attrs[PIVOT_TOTALS_ATTR] = {
            "columns": total_columns,
            "row": len(out) - 1 if len(out) else None,  # ``out`` has a RangeIndex
            "label_column": idx[0],
        }
    return out
