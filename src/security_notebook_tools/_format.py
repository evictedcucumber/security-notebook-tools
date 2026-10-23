"""Notebook display formatting for query results.

``exec_query`` records each column's KQL type in ``df.attrs["kql_types"]``; :func:`format_dataframe`
uses it to render values the way Kusto returns them, falling back to the column's dtype and
values for columns it has no type for.
"""

from __future__ import annotations

import html
import json
import logging
import math
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from numbers import Integral, Real
from typing import TYPE_CHECKING, Any

import pandas as pd

from ._errors import public_call
from ._log import dependency_call

if TYPE_CHECKING:
    from pandas.io.formats.style import Styler

logger = logging.getLogger("security_notebook_tools.format")

KQL_TYPES_ATTR = "kql_types"
PIVOT_TOTALS_ATTR = "pivot_totals"
_TOTAL_LABEL = "Total"
DEFAULT_MAX_ROWS = 1000

_INTEGER_TYPES = {"int", "int32", "int64", "long"}
_REAL_TYPES = {"real", "double", "decimal"}
_NUMERIC_TYPES = _INTEGER_TYPES | _REAL_TYPES
_TICKS_PER_SECOND = 10_000_000  # Kusto datetimes and timespans have 100 ns resolution.
_PLAIN_NUMBER = re.compile(r"(-?)(\d+)(\.\d+)?")

_TABLE_STYLES = [
    {
        "selector": "",
        "props": "border-collapse: collapse; font-variant-numeric: tabular-nums;",
    },
    {
        "selector": "th, td",
        "props": "white-space: nowrap; text-align: left; vertical-align: top; padding: 4px 10px;",
    },
    {"selector": "thead th", "props": "border-bottom: 1px solid; font-weight: 600;"},
    {"selector": "tbody tr:nth-child(even)", "props": "background: rgba(127, 127, 127, 0.08);"},
    {"selector": "tbody tr:hover", "props": "background: rgba(127, 127, 127, 0.18);"},
    {"selector": "caption", "props": "caption-side: top; text-align: left; padding: 4px 0;"},
]
_TOTAL_COLUMN_CSS = "font-weight: 600; border-left: 2px solid;"
_TOTAL_ROW_CSS = "font-weight: 600; border-top: 2px solid; background: rgba(127, 127, 127, 0.14);"


def _is_null(value: Any) -> bool:
    """True for ``None``, ``NA``, ``NaT`` and NaN (how missing values reach a DataFrame)."""
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    return isinstance(value, float) and math.isnan(value)


def _fraction(ticks: int) -> str:
    """Render sub-second 100 ns ticks as ``.fffffff`` with trailing zeros dropped."""
    return f".{ticks:07d}".rstrip("0") if ticks else ""


def _fmt_bool(value: Any) -> str:
    """Render a bool as Kusto does (``true`` / ``false``)."""
    if isinstance(value, bool) or (isinstance(value, Integral) and value in (0, 1)):
        return "true" if value else "false"
    return str(value)


def _fmt_int(value: Any) -> str:
    """Render an integer, including one stored as a float because its column has nulls."""
    if isinstance(value, Integral) and not isinstance(value, bool):
        return str(int(value))
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _fmt_real(value: Any) -> str:
    """Render a float with the shortest repr that round-trips (``1.5``, ``1e-05``)."""
    if isinstance(value, Real) and not isinstance(value, bool):
        number = float(value)
        if math.isinf(number):
            return "Infinity" if number > 0 else "-Infinity"
        return repr(number)
    return str(value)  # decimals from Log Analytics arrive as exact strings; keep them as-is


def _fmt_datetime(value: Any) -> str:
    """Render a datetime in Kusto's ISO 8601 form, ``Z`` marking UTC."""
    if not isinstance(value, datetime):
        return str(value)
    ts = pd.Timestamp(value)
    suffix = ""
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC")
        suffix = "Z"
    ticks = (ts.microsecond * 1000 + ts.nanosecond) // 100
    return f"{ts:%Y-%m-%dT%H:%M:%S}{_fraction(ticks)}{suffix}"


def _fmt_timespan(value: Any) -> str:
    """Render a duration in Kusto's ``[-][d.]hh:mm:ss[.fffffff]`` form."""
    if not isinstance(value, timedelta):
        return str(value)  # Log Analytics already returns timespans as Kusto-formatted strings
    total = pd.Timedelta(value).value // 100  # 100 ns ticks
    sign = "-" if total < 0 else ""
    seconds, ticks = divmod(abs(total), _TICKS_PER_SECOND)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    day_part = f"{days}." if days else ""
    return f"{sign}{day_part}{hours:02d}:{minutes:02d}:{seconds:02d}{_fraction(ticks)}"


def _fmt_dynamic(value: Any) -> str:
    """Render a dynamic value as single-line JSON (Log Analytics returns it as JSON text)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, ensure_ascii=False, default=str)


_FORMATTERS: dict[str, Callable[[Any], str]] = {
    "bool": _fmt_bool,
    "boolean": _fmt_bool,
    "datetime": _fmt_datetime,
    "date": _fmt_datetime,
    "timespan": _fmt_timespan,
    "time": _fmt_timespan,
    "dynamic": _fmt_dynamic,
    **dict.fromkeys(_INTEGER_TYPES, _fmt_int),
    **dict.fromkeys(_REAL_TYPES, _fmt_real),
}


def _group_digits(text: str) -> str:
    """Split the integer part of a plain decimal number into groups of three with spaces.

    ``1000000000.00000`` becomes ``1 000 000 000.00000``; the fraction is left alone. Anything
    else (exponent form such as ``1e+20``, ``Infinity``, text) is returned unchanged.
    """
    match = _PLAIN_NUMBER.fullmatch(text)
    if match is None:
        return text
    sign, whole, fraction = match.groups()
    return f"{sign}{int(whole):_}".replace("_", " ") + (fraction or "")


# Column styles for ``format_dataframe(column_styles=...)``: each name maps to the KQL types it
# may be applied to and a function that rewrites the cell text produced by the type's formatter.
_COLUMN_STYLES: dict[str, tuple[frozenset[str], Callable[[str], str]]] = {
    "readable_numbers": (frozenset(_NUMERIC_TYPES), _group_digits),
}


def _infer_type(column: pd.Series) -> str:
    """Guess the KQL type of a column with no recorded type from its dtype and values."""
    dtype = column.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "bool"
    if pd.api.types.is_integer_dtype(dtype):
        return "long"
    if pd.api.types.is_float_dtype(dtype):
        return "real"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "datetime"
    if pd.api.types.is_timedelta64_dtype(dtype):
        return "timespan"
    values = [v for v in column if not _is_null(v)]
    if not values:
        return "string"
    if all(isinstance(v, bool) for v in values):
        return "bool"
    if all(isinstance(v, Integral) and not isinstance(v, bool) for v in values):
        return "long"
    if all(isinstance(v, Real) and not isinstance(v, bool) for v in values):
        return "real"
    if all(isinstance(v, datetime) for v in values):
        return "datetime"
    if all(isinstance(v, timedelta) for v in values):
        return "timespan"
    if all(isinstance(v, dict | list) for v in values):
        return "dynamic"
    return "string"


def _cell_formatter(kql_type: str, style: str | None = None) -> Callable[[Any], str]:
    """Return a formatter that renders one cell as escaped HTML, nulls as empty."""
    fmt = _FORMATTERS.get(kql_type, str)
    restyle = _COLUMN_STYLES[style][1] if style is not None else None

    def format_cell(value: Any) -> str:
        if _is_null(value):
            return ""
        text = fmt(value)
        if restyle is not None:
            text = restyle(text)
        # Escape the final text: query results are untrusted and must not inject HTML.
        return html.escape(text)

    return format_cell


def _pivot_totals(df: pd.DataFrame, view: pd.DataFrame) -> tuple[list[Any], Any]:
    """Return the total columns and total row label of ``view`` recorded by ``pivot_dataframe``.

    Only what ``df.attrs["pivot_totals"]`` names is returned, never a row or column just because
    it holds ``"Total"``. Columns no longer in ``view`` are dropped, and the row only counts if its
    label is in ``view`` once and its ``label_column`` still reads ``"Total"`` (so a stale label
    left by, say, ``reset_index`` is ignored). The row is ``None`` when there is none.
    """
    meta = df.attrs.get(PIVOT_TOTALS_ATTR)
    if not isinstance(meta, dict):
        return [], None
    columns = meta.get("columns")
    columns = [c for c in columns if c in view.columns] if isinstance(columns, list) else []
    row, label_column = meta.get("row"), meta.get("label_column")
    try:
        found = (
            row is not None
            and label_column in view.columns
            and view.columns.is_unique
            and (view.index == row).sum() == 1
            and view.at[row, label_column] == _TOTAL_LABEL
        )
    except TypeError, ValueError:
        found = False
    return columns, row if found else None


def _check_column_styles(df: pd.DataFrame, column_styles: Any) -> dict[Any, str]:
    """Validate ``column_styles`` names and columns; types are checked once they are known."""
    if column_styles is None:
        return {}
    if not isinstance(column_styles, Mapping):
        raise TypeError(
            "column_styles must be a mapping of column name to style name, "
            f"got {type(column_styles).__name__}."
        )
    for column, style in column_styles.items():
        if column not in df.columns:
            raise ValueError(f"column_styles names column {column!r}, which is not in df.")
        if not isinstance(style, str) or style not in _COLUMN_STYLES:
            valid = ", ".join(repr(name) for name in _COLUMN_STYLES)
            raise ValueError(
                f"Unknown style {style!r} for column {column!r}; valid styles: {valid}."
            )
    return dict(column_styles)


def format_dataframe(
    df: pd.DataFrame,
    *,
    max_rows: int | None = DEFAULT_MAX_ROWS,
    column_styles: Mapping[Any, str] | None = None,
    debug: bool = False,
) -> Styler:
    """Format a query result for display in a notebook.

    Values are shown the way Kusto returns them, by each column's KQL type: ``long`` and
    ``int`` as integers (also when nulls made pandas store them as floats), ``real`` as the
    shortest float that round-trips, ``bool`` as ``true``/``false``, ``datetime`` as ISO 8601
    with ``Z`` for UTC, ``timespan`` as ``[d.]hh:mm:ss[.fffffff]`` and ``dynamic`` as
    single-line JSON (a list stays a list, an object an object). Nulls are shown as empty
    cells. The types come from ``df.attrs["kql_types"]``, which :func:`exec_query` sets;
    columns without a recorded type (for example ones you added) are typed from their dtype
    and values.

    Text is left-aligned and numbers (``int``, ``long``, ``real``, ``decimal``) right-aligned,
    headers included. Cells never wrap and columns have no maximum width; wide tables scroll
    horizontally. All text is HTML-escaped. An unnamed index is hidden.

    The total row and columns of a :func:`pivot_dataframe` result with ``totals=True`` are shown
    bold and set off by a rule (and the row by a shaded background). They are found from
    ``df.attrs["pivot_totals"]``, not by name, so an ordinary row or column that happens to be
    called or contain ``"Total"`` is formatted like any other. The marking survives filtering and
    sorting; the row is no longer marked once its index label changes (for example after
    ``reset_index``) or it is cut off by ``max_rows``.

    ``column_styles`` applies an extra display style to named columns. The only style is
    ``"readable_numbers"``, for numeric columns (``int``, ``long``, ``real``, ``decimal``): it
    splits the integer part into groups of three digits with spaces, so ``1000000000.00000`` is
    shown as ``1 000 000 000.00000``. The fraction is not grouped, and a ``real`` that Kusto
    shows in exponent form (``1e+20``) is left as it is. A style is checked against the column's
    KQL type (recorded or inferred), so it cannot be put on a column it does not fit.

    Args:
        df: The DataFrame to format, usually the result of :func:`exec_query`. It is not
            modified.
        max_rows: Show at most this many rows (default 1000), with a caption giving the total;
            ``None`` shows every row. Rendering very large tables can make a notebook slow.
        column_styles: Optional mapping of column name to style name, for example
            ``{"Bytes": "readable_numbers"}``. ``None`` (the default) styles no column.
        debug: If true, also print this call's log records, at DEBUG and above, to the
            notebook cell's output while it runs. Default false. The lines are printed unredacted
            (debug output is not written to the file; be careful when sharing a notebook saved
            with debug output). See the
            logging section of the README.

    Returns:
        A pandas ``Styler``. Jupyter renders it as an HTML table when it is the last
        expression in a cell; chain further ``Styler`` methods on it if you like.

    Raises:
        ValueError: ``max_rows`` is not a positive integer or ``None``; or ``column_styles``
            names a column that is not in ``df`` or a style that does not exist.
        TypeError: ``column_styles`` is not a mapping, or a style is given for a column whose
            KQL type it does not apply to (for example ``"readable_numbers"`` on a string).

    Example:
        >>> from security_notebook_tools import exec_query, format_dataframe
        >>> df = exec_query("SigninLogs | take 100")
        >>> format_dataframe(df)
        >>> format_dataframe(df, max_rows=None)  # every row
        >>> format_dataframe(df, column_styles={"Bytes": "readable_numbers"})
    """
    shape = getattr(df, "shape", None)
    with public_call(
        logger,
        "format_dataframe",
        rejected=(ValueError, TypeError),
        debug=debug,
        rows=shape[0] if shape else None,
        columns=shape[1] if shape else None,
        max_rows=max_rows,
        column_styles=len(column_styles) if isinstance(column_styles, Mapping) else None,
    ) as call:
        if max_rows is not None and (
            isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows <= 0
        ):
            raise ValueError(f"max_rows must be a positive integer or None, got {max_rows!r}.")
        styles = _check_column_styles(df, column_styles)
        view = df if max_rows is None or len(df) <= max_rows else df.head(max_rows)
        recorded = df.attrs.get(KQL_TYPES_ATTR) or {}

        kql_types: list[str] = []
        for position, name in enumerate(view.columns):
            kql_type = recorded.get(name)
            if not isinstance(kql_type, str):
                kql_type = _infer_type(view.iloc[:, position])
            kql_type = kql_type.lower()
            style = styles.get(name)
            if style is not None and kql_type not in _COLUMN_STYLES[style][0]:
                raise TypeError(
                    f"Style {style!r} does not apply to column {name!r} of type {kql_type!r}; "
                    f"it needs one of: {', '.join(sorted(_COLUMN_STYLES[style][0]))}."
                )
            kql_types.append(kql_type)

        with dependency_call(logger, "pandas Styler", rows=len(view), columns=len(view.columns)):
            styler = view.style
            numeric: list[Any] = []
            for name, kql_type in zip(view.columns, kql_types, strict=True):
                styler = styler.format(_cell_formatter(kql_type, styles.get(name)), subset=[name])
                if kql_type in _NUMERIC_TYPES:
                    numeric.append(name)

            styler = styler.set_table_styles(_TABLE_STYLES).set_table_styles(
                {
                    name: [{"selector": sel, "props": "text-align: right;"} for sel in ("th", "td")]
                    for name in numeric
                },
                overwrite=False,
            )
            total_columns, total_row = _pivot_totals(df, view)
            if total_columns:
                styler = styler.set_table_styles(
                    {
                        name: [
                            {"selector": sel, "props": _TOTAL_COLUMN_CSS} for sel in ("th", "td")
                        ]
                        for name in total_columns
                    },
                    overwrite=False,
                )
            if total_row is not None:
                styler = styler.set_table_styles(
                    {total_row: [{"selector": "td", "props": _TOTAL_ROW_CSS}]},
                    axis=1,
                    overwrite=False,
                )
            if all(name is None for name in df.index.names):
                styler = styler.hide(axis="index")
            if len(view) < len(df):
                styler = styler.set_caption(f"Showing the first {len(view):,} of {len(df):,} rows.")
        call.update(shown_rows=len(view))
        return styler
