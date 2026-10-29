"""Charts for queries that end in ``| render <charttype>``.

``exec_query`` stores the render spec in ``df.attrs["visualization"]`` (see
:func:`normalise_visualization`); :func:`render_dataframe` draws it with Plotly, which is an
optional dependency (``pip install "security-notebook-tools[charts]"``) imported only when a chart
is drawn.

Everything that reaches Plotly as text (column names, category and series values, the render
title and axis titles) is HTML-escaped first: Plotly interprets a small HTML subset (``<a href>``,
``<b>``, ``<br>``, ``<span style>``) in titles, tick labels, legend entries and hover text, and
query results are untrusted.
"""

from __future__ import annotations

import html
import json
import logging
import math
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import pandas as pd

from ._errors import public_call
from ._exceptions import ConfigurationError
from ._format import KQL_TYPES_ATTR
from ._log import dependency_call

logger = logging.getLogger("security_notebook_tools.render")

VISUALIZATION_ATTR = "visualization"
INSTALL_HINT = 'pip install "security-notebook-tools[charts]"'

LINE_CHARTS = frozenset({"timechart", "linechart"})
CHART_TYPES = frozenset(
    {
        *LINE_CHARTS,
        "areachart",
        "stackedareachart",
        "columnchart",
        "barchart",
        "piechart",
        "scatterchart",
        "anomalychart",
        "card",
    }
)
SUPPORTED_TYPES = CHART_TYPES | {"table"}

_LIST_KEYS = ("ycolumns", "series", "anomalycolumns")
_TEXT_KEYS = ("title", "xtitle", "ytitle")
_ENUM_KEYS = ("kind", "legend", "xaxis", "yaxis", "ysplit")
_LIMIT_KEYS = ("ymin", "ymax", "xmin", "xmax")
_NUMERIC_KQL = {"int", "int32", "int64", "long", "real", "double", "decimal"}
_STACK_KINDS = {"stacked", "stacked100"}
MAX_TRACES = 100  # more series than this is unreadable and slow to draw: refuse, do not truncate


class PlotlyMissingError(ConfigurationError):
    """Plotly is not installed; install the ``charts`` extra."""


def _text(value: Any) -> str | None:
    """A stripped non-empty string, or ``None``."""
    if value is None or isinstance(value, bool | dict | list):
        return None
    text = str(value).strip()
    return text or None


def _names(value: Any) -> list[str]:
    """A comma-joined string or a list as a list of non-empty names."""
    items = value if isinstance(value, list | tuple) else str(value or "").split(",")
    return [t for t in (_text(i) for i in items) if t]


def _limit(value: Any) -> float | str | None:
    """A finite number, or a non-empty string (a datetime bound), or ``None`` (NaN, null)."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    except TypeError, ValueError:
        return _text(value)
    return number if math.isfinite(number) else None


def normalise_visualization(raw: Any) -> dict[str, Any] | None:
    """Turn a render spec from Sentinel or ADX into the shape stored in ``attrs``.

    Both services return the same properties under different spellings (Sentinel camelCase such as
    ``yColumns``, ADX PascalCase such as ``YColumns``); keys are lower-cased here. ``raw`` may be
    the dict or its JSON text.

    Returns:
        A dict with ``visualization`` (the chart type, lower case) and, only when set, ``title``,
        ``xtitle``, ``ytitle``, ``xcolumn``, ``ycolumns`` / ``series`` / ``anomalycolumns``
        (lists of column names), ``kind``, ``legend``, ``xaxis``, ``yaxis``, ``ysplit`` (lower
        case), ``accumulate`` (only when true), and ``ymin``, ``ymax``, ``xmin``, ``xmax``.
        ``None`` if there is no chart type (the query had no ``render``).
    """
    if isinstance(raw, str | bytes):
        try:
            raw = json.loads(raw)
        except ValueError, RecursionError:
            return None
    if not isinstance(raw, dict):
        return None
    spec = {str(k).lower(): v for k, v in raw.items()}
    chart = _text(spec.get("visualization"))
    if chart is None:
        return None
    out: dict[str, Any] = {VISUALIZATION_ATTR: chart.lower()}
    for key in _TEXT_KEYS:
        if (text := _text(spec.get(key))) is not None:
            out[key] = text
    if (column := _text(spec.get("xcolumn"))) is not None:
        out["xcolumn"] = column
    for key in _LIST_KEYS:
        if names := _names(spec.get(key)):
            out[key] = names
    for key in _ENUM_KEYS:
        if (text := _text(spec.get(key))) is not None:
            out[key] = text.lower()
    for key in _LIMIT_KEYS:
        if (limit := _limit(spec.get(key))) is not None:
            out[key] = limit
    accumulate = spec.get("accumulate")
    if accumulate is True or str(accumulate).lower() == "true":
        out["accumulate"] = True
    return out


def set_visualization(tables: list[pd.DataFrame], raw: Any, index: int = 0) -> None:
    """Store the normalised ``raw`` render spec in ``attrs`` of ``tables[index]``, if it has one.

    Fails safe: a spec that cannot be read is dropped (logged at DEBUG) and never raises, so a
    chart problem cannot lose the query's data.
    """
    try:
        spec = normalise_visualization(raw)
        if spec is not None and tables:
            tables[min(max(index, 0), len(tables) - 1)].attrs[VISUALIZATION_ATTR] = spec
    except Exception:
        logger.debug("Dropping a render spec that could not be read", exc_info=True)


def _esc(value: Any) -> str:
    """Escape text for Plotly, which would otherwise read ``<a href>`` and friends as markup.

    ``$`` is escaped too: plotly.js hands ``$...$`` to MathJax (TeX) when that is loaded, before it
    handles tags and entities, and turns ``&#36;`` back into a literal ``$`` afterwards.
    """
    return html.escape(str(value), quote=False).replace("$", "&#36;")


def _load_plotly() -> Any:
    """Import ``plotly.graph_objects`` or raise :class:`PlotlyMissingError`."""
    try:
        import plotly.graph_objects as go
    except ImportError as exc:
        raise PlotlyMissingError(
            f"Plotly is not installed, so the chart cannot be drawn. Install: {INSTALL_HINT}."
        ) from exc
    return go


def check_spec(df: Any) -> dict[str, Any]:
    """Return ``df.attrs["visualization"]``, or raise ``ConfigurationError`` if unusable."""
    spec = df.attrs.get(VISUALIZATION_ATTR)
    if not isinstance(spec, dict) or not isinstance(spec.get(VISUALIZATION_ATTR), str):
        raise ConfigurationError(
            'The DataFrame has no chart to draw: df.attrs["visualization"] is not set. End the '
            "KQL query with `| render <charttype>` (timechart, linechart, areachart, "
            "stackedareachart, columnchart, barchart, piechart, scatterchart, anomalychart, card, "
            "table) and run it with exec_query."
        )
    chart = spec[VISUALIZATION_ATTR].lower()
    if chart not in SUPPORTED_TYPES:
        raise ConfigurationError(
            f"render_dataframe cannot draw `render {chart[:40]}`; supported chart types are "
            f"{', '.join(sorted(SUPPORTED_TYPES))}."
        )
    return spec


def _kind_of(df: pd.DataFrame, position: int) -> str:
    """``"datetime"``, ``"numeric"`` or ``"other"`` for a column."""
    column = df.iloc[:, position]
    recorded = (df.attrs.get(KQL_TYPES_ATTR) or {}).get(df.columns[position])
    if pd.api.types.is_datetime64_any_dtype(column.dtype) or recorded in ("datetime", "date"):
        return "datetime"
    if recorded in _NUMERIC_KQL:
        return "numeric"
    if pd.api.types.is_bool_dtype(column.dtype):
        return "other"
    return "numeric" if pd.api.types.is_numeric_dtype(column.dtype) else "other"


def _need(df: pd.DataFrame, names: Iterable[str], label: str) -> None:
    """Raise ``ConfigurationError`` if a column named by the render properties is missing."""
    missing = [n for n in names if n not in df.columns]
    if missing:
        raise ConfigurationError(
            f"The render property {label} names column(s) {[m[:80] for m in missing]} that "
            f"are not in the result; available columns: {[str(c)[:80] for c in df.columns]}."
        )


def _labels(series: pd.Series, kind: str) -> list[Any]:
    """Axis or legend values: numbers and datetimes as they are, everything else escaped text."""
    if kind == "datetime":
        values = pd.to_datetime(series, utc=True, errors="coerce")
        return [None if pd.isna(v) else v.isoformat() for v in values]
    if kind == "numeric":
        return [None if pd.isna(v) else v for v in pd.to_numeric(series, errors="coerce")]
    return ["(null)" if _is_missing(v) else _esc(v) for v in series]


def _is_missing(value: Any) -> bool:
    """Whether a scalar cell is null (containers are not)."""
    return not isinstance(value, dict | list | tuple | set) and bool(pd.isna(value))


def _numbers(series: pd.Series) -> pd.Series:
    """The series as floats (Log Analytics decimals arrive as strings); unparsable become NaN."""
    return pd.to_numeric(series, errors="coerce")


def _as_list(value: Any) -> list[Any] | None:
    """A cell as a list if it is an array (or JSON text of one), else ``None``."""
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:
            value = json.loads(value)
        except ValueError, RecursionError:
            return None
    if isinstance(value, list | tuple):
        return list(value)
    return value.tolist() if hasattr(value, "tolist") and getattr(value, "ndim", 0) == 1 else None


def _expand_arrays(df: pd.DataFrame, spec: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Expand ``make-series`` array columns into rows; scalar frames are returned unchanged.

    ``make-series`` returns one row per series with the x axis and each aggregate as an array of
    the same length. The x, y and anomaly arrays of every row are exploded together (so a row with
    ``n`` points becomes ``n`` rows), scalar columns such as the ``by`` key are repeated, and the
    spec gets the resolved ``xcolumn`` and ``ycolumns`` so the plan finds them.
    """
    if not df.columns.is_unique:
        return df, spec
    arrays: dict[Any, pd.Series] = {}
    for name in df.columns:
        cells = [_as_list(v) for v in df[name]]
        if (
            cells
            and any(c is not None for c in cells)
            and all(c is not None or _is_missing(v) for c, v in zip(cells, df[name], strict=True))
        ):
            arrays[name] = pd.Series(cells, index=df.index, dtype=object)
    if not arrays:
        return df, spec

    def first_item(name: Any) -> Any:
        return next((i for cell in arrays[name] if cell for i in cell if i is not None), None)

    def is_number(item: Any) -> bool:
        return isinstance(item, int | float) and not isinstance(item, bool)

    def is_time(item: Any) -> bool:
        if isinstance(item, datetime):  # pd.Timestamp is a datetime too
            return True
        try:
            return isinstance(item, str) and not pd.isna(pd.Timestamp(item))
        except ValueError, TypeError:
            return False

    anomaly = [c for c in spec.get("anomalycolumns") or [] if c in arrays]
    x = spec.get("xcolumn")
    if x is None:
        x = next((c for c in arrays if is_time(first_item(c)) or is_number(first_item(c))), None)
    if x not in arrays:
        return df, spec
    if spec.get("ycolumns"):
        y = [c for c in spec["ycolumns"] if c in arrays]
    else:
        y = [c for c in arrays if c != x and c not in anomaly and is_number(first_item(c))]
    expand = list(dict.fromkeys([x, *y, *anomaly]))
    if len(expand) < 2:
        return df, spec  # a lone list column (say, a dynamic tag list) is not a make-series
    out = df.copy()
    for name in expand:
        out[name] = arrays[name]
    out = out.drop(columns=[c for c in arrays if c not in expand])
    try:
        out = out.explode(expand, ignore_index=True)
    except ValueError as exc:
        raise ConfigurationError(
            "The array columns of the result (make-series) differ in length within a row."
        ) from exc
    types = dict(df.attrs.get(KQL_TYPES_ATTR) or {})
    if out[x].dropna().map(is_time).all() and out[x].notna().any():
        out[x] = pd.to_datetime(out[x], utc=True, errors="coerce")
        types[x] = "datetime"
    else:
        numeric = pd.to_numeric(out[x], errors="coerce")
        if numeric.notna().sum() == out[x].notna().sum():
            out[x] = numeric
            types[x] = "real"
    for name in [*y, *anomaly]:
        out[name] = pd.to_numeric(out[name], errors="coerce")
        types[name] = "real"
    out.attrs = {**df.attrs, KQL_TYPES_ATTR: types}
    return out, {**spec, "xcolumn": x, "ycolumns": y or spec.get("ycolumns")}


class _Plan:
    """Which columns a chart reads, resolved from the render properties and KQL's defaults."""

    def __init__(self, df: pd.DataFrame, spec: dict[str, Any], chart: str) -> None:
        if not df.columns.is_unique:
            raise ConfigurationError("The result has duplicate column names; rename them first.")
        if not len(df.columns):
            raise ConfigurationError("The result has no columns to draw.")
        columns = list(df.columns)
        kinds = {c: _kind_of(df, i) for i, c in enumerate(columns)}
        self.anomaly = list(spec.get("anomalycolumns") or [])
        self.y_given = list(spec.get("ycolumns") or [])
        series_given = list(spec.get("series") or [])
        x_given = spec.get("xcolumn")
        _need(df, [x_given] if x_given else [], "xcolumn")
        _need(df, self.y_given, "ycolumns")
        _need(df, series_given, "series")
        _need(df, self.anomaly, "anomalycolumns")
        if x_given:
            self.x = x_given
        elif chart == "timechart":
            self.x = next((c for c in columns if kinds[c] == "datetime"), columns[0])
        else:
            self.x = columns[0]
        taken = {self.x, *self.anomaly}
        if self.y_given:
            self.y = self.y_given
        else:
            skip = taken | set(series_given)
            self.y = [c for c in columns if c not in skip and kinds[c] == "numeric"]
        if series_given:
            self.series = series_given
        else:
            skip = taken | set(self.y)
            self.series = [c for c in columns if c not in skip and kinds[c] == "other"]
        for name in self.y_given:
            if len(df) and not _numbers(df[name]).notna().any():
                raise ConfigurationError(
                    f"The render property ycolumns names column {str(name)[:80]!r}, which has no "
                    "numeric values to plot."
                )
        if not self.y and chart != "card":
            raise ConfigurationError(
                "The result has no numeric column to plot besides the x column; name one with "
                "`ycolumns=` in the render, or summarize into a number."
            )
        self.kinds = kinds
        self.x_kind = kinds[self.x]


def _groups(df: pd.DataFrame, plan: _Plan) -> list[tuple[str | None, pd.DataFrame]]:
    """Split the rows into one frame per series value (a single ``None`` group without series).

    Raises:
        ConfigurationError: There would be more than ``MAX_TRACES`` traces.
    """
    if not plan.series:
        return [(None, df)]
    labels = None
    for name in plan.series:
        column = df[name]
        part = column.astype(object).where(column.notna(), "(null)").astype(str)
        labels = part if labels is None else labels + ", " + part
    grouped = df.groupby(labels, sort=False)
    traces = grouped.ngroups * max(len(plan.y), 1)
    if traces > MAX_TRACES:
        raise ConfigurationError(
            f"The chart would have {traces} traces ({grouped.ngroups} series values x "
            f"{max(len(plan.y), 1)} y columns); the limit is {MAX_TRACES}. "
            "Reduce the series with `top` or `where` in the query."
        )
    return [(_esc(label), frame) for label, frame in grouped]


def _trace_name(group: str | None, column: str, many_columns: bool) -> str:
    """Legend text for a series value and a y column (both escaped)."""
    if group is None:
        return _esc(column)
    return f"{group}: {_esc(column)}" if many_columns else group


def _stack_kind(spec: dict[str, Any], chart: str) -> str:
    """``"default"``, ``"stacked"`` or ``"stacked100"`` from ``kind`` and the chart type."""
    kind = spec.get("kind") or "default"
    if chart == "stackedareachart":
        return "stacked100" if kind == "stacked100" else "stacked"
    return kind if kind in _STACK_KINDS else "default"


def _series_traces(
    go: Any,
    groups: list[tuple[str | None, pd.DataFrame]],
    plan: _Plan,
    spec: dict[str, Any],
    chart: str,
) -> list[Any]:
    """Traces for every chart type that plots y columns against x, one per series and column."""
    stack = _stack_kind(spec, chart)
    accumulate = bool(spec.get("accumulate"))
    sort = chart in LINE_CHARTS | {"areachart", "stackedareachart", "anomalychart"}
    traces: list[Any] = []
    for group, frame in groups:
        if sort and plan.x_kind in ("datetime", "numeric"):
            order = frame[plan.x]
            order = (
                pd.to_datetime(order, utc=True, errors="coerce")
                if plan.x_kind == "datetime"
                else _numbers(order)
            )
            frame = frame.iloc[order.argsort(kind="stable").to_numpy()]
        x_values = _labels(frame[plan.x], plan.x_kind)
        for column in plan.y:
            y = _numbers(frame[column])
            if accumulate:
                y = y.cumsum()
            y_values = [None if pd.isna(v) else float(v) for v in y]
            name = _trace_name(group, column, len(plan.y) > 1)
            if chart == "columnchart":
                traces.append(go.Bar(x=x_values, y=y_values, name=name))
            elif chart == "barchart":
                traces.append(go.Bar(x=y_values, y=x_values, name=name, orientation="h"))
            elif chart == "scatterchart":
                traces.append(go.Scatter(x=x_values, y=y_values, name=name, mode="markers"))
            elif chart in ("areachart", "stackedareachart") and stack != "default":
                extra = {"groupnorm": "percent"} if stack == "stacked100" else {}
                traces.append(
                    go.Scatter(
                        x=x_values, y=y_values, name=name, mode="lines", stackgroup="one", **extra
                    )
                )
            elif chart == "areachart":
                traces.append(
                    go.Scatter(x=x_values, y=y_values, name=name, mode="lines", fill="tozeroy")
                )
            else:
                traces.append(go.Scatter(x=x_values, y=y_values, name=name, mode="lines"))
        if chart == "anomalychart" and plan.y:
            base = _numbers(frame[plan.y[0]])
            for column in plan.anomaly:
                flags = _numbers(frame[column]).fillna(0).to_numpy() != 0
                traces.append(
                    go.Scatter(
                        x=[v for v, f in zip(x_values, flags, strict=True) if f],
                        y=[float(v) for v, f in zip(base, flags, strict=True) if f],
                        name=_trace_name(group, f"{column} (anomaly)", True),
                        mode="markers",
                        marker={"symbol": "x", "size": 10},
                    )
                )
    return traces


def _pie_trace(go: Any, df: pd.DataFrame, plan: _Plan) -> Any:
    """A pie of the first y column by the x column (KQL: color axis, then values)."""
    if len(plan.y) > 1:
        logger.debug("piechart draws the first of %d y columns", len(plan.y))
    values = _numbers(df[plan.y[0]]).fillna(0)
    return go.Pie(labels=_labels(df[plan.x], "other"), values=[float(v) for v in values])


def _card(go: Any, df: pd.DataFrame) -> Any:
    """The first cell of the first row, as a number if it is one, else as text."""
    value = df.iloc[0, 0]
    title = _esc(df.columns[0])
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if _kind_of(df, 0) == "numeric" and not pd.isna(number):
        return go.Figure(go.Indicator(mode="number", value=float(number), title={"text": title}))
    fig = go.Figure()
    text = "(null)" if _is_missing(value) else _esc(value)
    fig.add_annotation(text=title, showarrow=False, font={"size": 14}, x=0.5, y=0.65)
    fig.add_annotation(text=text, showarrow=False, font={"size": 28}, x=0.5, y=0.45)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


def _table(go: Any, df: pd.DataFrame) -> Any:
    """A Plotly table of every column."""
    cells = [
        _labels(df.iloc[:, i], "numeric" if _kind_of(df, i) == "numeric" else "other")
        for i in range(df.shape[1])
    ]
    header = [_esc(c) for c in df.columns]
    return go.Figure(go.Table(header={"values": header}, cells={"values": cells}))


def _axis_range(spec: dict[str, Any], low: str, high: str, log: bool) -> list[Any] | None:
    """The ``[min, max]`` axis range from two render limits, or ``None`` unless both are set."""
    lo, hi = spec.get(low), spec.get(high)
    if lo is None or hi is None:
        return None
    if log:
        if isinstance(lo, str) or isinstance(hi, str) or lo <= 0 or hi <= 0:
            return None
        return [math.log10(lo), math.log10(hi)]
    return [lo, hi]


def _style(fig: Any, spec: dict[str, Any], plan: _Plan, chart: str) -> None:
    """Apply title, axis titles, legend, axis type and limits."""
    horizontal = chart == "barchart"
    layout: dict[str, Any] = {}
    if spec.get("title"):
        layout["title"] = {"text": _esc(spec["title"])}
    if spec.get("legend") == "hidden":
        layout["showlegend"] = False
    if chart in ("columnchart", "barchart"):
        stack = _stack_kind(spec, chart)
        layout["barmode"] = "stack" if stack in _STACK_KINDS else "group"
        if stack == "stacked100":
            layout["barnorm"] = "percent"
    x_title = _esc(spec.get("xtitle") or plan.x)
    y_title = _esc(spec["ytitle"]) if spec.get("ytitle") else None
    if y_title is None and len(plan.y) == 1:
        y_title = _esc(plan.y[0])
    # In a barchart the x column runs along the vertical axis.
    cat, val = ("yaxis", "xaxis") if horizontal else ("xaxis", "yaxis")
    cat_axis: dict[str, Any] = {"title": {"text": x_title}}
    if plan.x_kind == "other":
        cat_axis["type"] = "category"  # "0" or "2024-05" must not be autotyped as a number/date
    val_axis: dict[str, Any] = {}
    if y_title:
        val_axis["title"] = {"text": y_title}
    if spec.get("xaxis") in ("log", "linear") and plan.x_kind == "numeric":
        cat_axis["type"] = spec["xaxis"]
    if spec.get("yaxis") in ("log", "linear"):
        val_axis["type"] = spec["yaxis"]
    if (rng := _axis_range(spec, "ymin", "ymax", spec.get("yaxis") == "log")) is not None:
        val_axis["range"] = rng
    if plan.x_kind == "numeric" and (
        rng := _axis_range(spec, "xmin", "xmax", spec.get("xaxis") == "log")
    ):
        cat_axis["range"] = rng
    layout[cat] = cat_axis
    layout[val] = val_axis
    fig.update_layout(**layout)


def render_dataframe(df: pd.DataFrame, *, debug: bool = False) -> Any:
    """Draw the chart a query asked for with ``| render <charttype>``.

    :func:`exec_query` and :func:`exec_batch_query` store the render operator's chart type and
    properties in ``df.attrs["visualization"]``; this draws them with Plotly. It needs the optional
    ``charts`` extra: ``pip install "security-notebook-tools[charts]"``.

    Supported chart types, with KQL's defaults when the render does not name columns (x is
    ``xcolumn`` or the first column, for ``timechart`` the first datetime column; y is
    ``ycolumns`` or every numeric column that is not x; ``series`` or the remaining non-numeric
    columns split the data into one line or bar group per value):

    * ``timechart``, ``linechart``: lines.
    * ``areachart``: filled lines; ``kind=stacked`` or ``stacked100`` stacks them.
      ``stackedareachart``: stacked areas (``kind=stacked100`` for percentages).
    * ``columnchart`` (vertical bars) and ``barchart`` (horizontal bars): ``kind`` ``default`` or
      ``unstacked`` groups the bars, ``stacked`` stacks them, ``stacked100`` stacks to 100%.
    * ``piechart``: the x column labels the slices, the first y column sizes them.
    * ``scatterchart``: markers.
    * ``anomalychart``: lines, with a marker on the first y column wherever an ``anomalycolumns``
      column of the same row (or array element, for ``make-series``) is not zero.
    * ``card``: the first column of the first row as a single value.
    * ``table``: a Plotly table of the whole frame.

    A ``make-series`` result (one row per series, with the x axis and every aggregate as an array)
    is expanded first: the x, y and anomaly arrays of each row are exploded together and scalar
    columns such as the ``by`` key become the series. Arrays in a row must have equal lengths.
    A chart is limited to ``MAX_TRACES`` (100) traces, series values times y columns; more raises
    ``ConfigurationError`` suggesting ``top`` or ``where`` to reduce the series.

    Honoured properties: ``title``, ``xtitle``, ``ytitle``, ``legend=hidden``, ``ymin`` and
    ``ymax`` (both needed), ``xmin`` and ``xmax`` (numeric x, both needed), ``xaxis`` and ``yaxis``
    (``log`` or ``linear``; in a ``barchart`` ``xaxis`` is the category axis), ``accumulate``.
    ``ysplit`` is ignored. Any other chart type (``pivotchart``, ``ladderchart``, ``timepivot``,
    ``3Dchart``, ``treemap``, ``sankey``, ``plotly`` ...) is refused; figure JSON in the data is
    never interpreted.

    Every string that comes from the data, the column names or the render properties is
    HTML-escaped before Plotly sees it, so Plotly shows ``<a href=...>`` and ``<b>`` as text
    rather than markup.

    Args:
        df: A result with ``attrs["visualization"]``. It is not modified.
        debug: If true, also print this call's log records, at DEBUG and above, to the
            notebook cell's output while it runs. Default false. The lines are printed unredacted
            (debug output is not written to the file; be careful when sharing a notebook saved
            with debug output). See the logging section of the README.

    Returns:
        A ``plotly.graph_objects.Figure``. Jupyter renders it when it is the last expression in a
        cell; call ``.show()`` elsewhere.

    Raises:
        ValueError: ``df`` is not a DataFrame or ``debug`` is not a bool.
        ConfigurationError: ``df`` has no ``attrs["visualization"]``, its chart type is not
            supported, a column the render names is not in ``df``, no column can be plotted, or
            Plotly is not installed (the message gives the install command).

    Example:
        >>> from security_notebook_tools import exec_query, render_dataframe
        >>> df = exec_query(
        ...     "SigninLogs | summarize n = count() by bin(TimeGenerated, 1h) | render timechart"
        ... )
        >>> render_dataframe(df)
    """
    shape = getattr(df, "shape", None)
    with public_call(
        logger,
        "render_dataframe",
        rejected=(ValueError,),
        debug=debug is True,
        rows=shape[0] if shape else None,
        columns=shape[1] if shape else None,
    ) as call:
        if not isinstance(df, pd.DataFrame):
            raise ValueError(f"df must be a pandas DataFrame, got {type(df).__name__}.")
        if not isinstance(debug, bool):
            raise ValueError(f"debug must be a bool, got {debug!r}.")
        spec = check_spec(df)
        chart = spec[VISUALIZATION_ATTR].lower()
        call.update(chart=chart)
        go = _load_plotly()
        # Everything that can fail on bad input is checked before the dependency call, so a
        # user error is logged once (by public_call) and not also as a plotly failure.
        frame, spec = (df, spec) if chart in ("card", "table") else _expand_arrays(df, spec)
        plan = groups = None
        if chart == "card" and df.empty:
            raise ConfigurationError("The result has no rows to show in a card.")
        if chart not in ("card", "table"):
            plan = _Plan(frame, spec, chart)
            logger.debug("Chart columns: x=%r y=%r series=%r", plan.x, plan.y, plan.series)
            if chart != "piechart":
                groups = _groups(frame, plan)
        with dependency_call(logger, "plotly figure", chart=chart) as dep:
            if chart == "card":
                fig = _card(go, df)
            elif chart == "table":
                fig = _table(go, df)
            elif chart == "piechart":
                fig = go.Figure(_pie_trace(go, frame, plan))
                if spec.get("legend") == "hidden":
                    fig.update_layout(showlegend=False)
            else:
                fig = go.Figure(_series_traces(go, groups, plan, spec, chart))
                _style(fig, spec, plan, chart)
            if chart in ("card", "table", "piechart") and spec.get("title"):
                fig.update_layout(title={"text": _esc(spec["title"])})  # _style does the others
            dep["traces"] = len(fig.data)
        call.update(traces=len(fig.data))
        return fig
