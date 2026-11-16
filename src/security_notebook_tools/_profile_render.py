"""Readable display of query profiles: a summary table with bars, and a batch window table."""

from __future__ import annotations

import logging
import math
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any, NamedTuple

import pandas as pd

from ._profile import _in_ipython, _parse_timespan

logger = logging.getLogger("security_notebook_tools.profile_render")

CAPTION = "Query profile"
MAX_WINDOW_ROWS = 200
BAR_COLOR = "#7fb3e6"
_NO_STATISTICS = "the service returned no statistics"
_FOOTER = "All values: df.attrs['profile']"


def _normalise(key: str) -> str:
    """Lowercase ``key`` without ``_`` and spaces and without a leading ``query.``."""
    text = key.lower().replace("_", "").replace(" ", "")
    return text.removeprefix("query.")


def fmt_count(value: float) -> str:
    """A number with digits grouped by spaces: ``1 234 567``."""
    return f"{round(value):,}".replace(",", " ")


def fmt_bytes(value: float) -> str:
    """Bytes scaled to B, KiB, MiB, GiB or TiB with one decimal."""
    if abs(round(value)) < 1024:
        return f"{round(value)} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if abs(round(value, 1)) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
    raise AssertionError  # pragma: no cover


def fmt_seconds(value: float) -> str:
    """A duration with three significant digits, in ms below one second."""
    if value * 1000 < 999.5:
        return f"{value * 1000:.3g} ms"
    return f"{value:.3g} s" if value < 100 else f"{fmt_count(value)} s"


class _Stats:
    """A flat profile looked up by normalised key, remembering which keys were read."""

    def __init__(self, profile: dict[str, Any], shape: tuple[int, int] | None = None) -> None:
        """Index ``profile`` (a flat dict) and the optional ``(rows, columns)`` of the result."""
        self.shape = shape
        self.used: set[str] = set()
        self._items: dict[str, tuple[str, Any]] = {}
        for key, value in profile.items():
            self._items.setdefault(_normalise(key), (key, value))

    def get(self, name: str) -> Any:
        """The value of the normalised key ``name``, or None."""
        if name not in self._items:
            return None
        key, value = self._items[name]
        self.used.add(key)
        return value

    def number(self, name: str) -> float | None:
        """A finite numeric value (not a bool), or None."""
        value = self.get(name)
        if not isinstance(value, int | float) or isinstance(value, bool):
            return None
        return float(value) if math.isfinite(value) else None

    def seconds(self, name: str) -> float | None:
        """A duration in seconds from a number or a Kusto timespan string, or None."""
        value = self.get(name)
        if (span := _parse_timespan(value)) is not None:
            return span.total_seconds()
        return self.number(name)

    def indexes(self, prefix: str) -> list[int]:
        """The indexes ``i`` of keys that start with ``prefix[i].``, in order."""
        found = [
            k.removeprefix(prefix + "[").split("]", 1)[0]
            for k in self._items
            if k.startswith(prefix + "[")
        ]
        return sorted({int(i) for i in found if i.isdigit()})


class Row(NamedTuple):
    """One summary row: section, metric, shown value and share (0-100, or None)."""

    section: str
    metric: str
    value: str
    share: float | None = None


Builder = Callable[[_Stats], "tuple[str, float | None] | None"]


def _value(name: str, fmt: Callable[[float], str], seconds: bool = False) -> Builder:
    def build(stats: _Stats):
        number = stats.seconds(name) if seconds else stats.number(name)
        return None if number is None else (fmt(number), None)

    return build


def _of_total(part: str, total: str, fmt: Callable[[float], str] = fmt_count) -> Builder:
    """``part of total`` with the share ``part / total``; nothing when the total is 0."""

    def build(stats: _Stats):
        a, b = stats.number(part), stats.number(total)
        if a is None or not b:
            return None
        return f"{fmt(a)} of {fmt(b)}", a / b * 100

    return build


def _hit_miss(hit: str, miss: str) -> Builder:
    """Bytes hit and missed, with the hit ratio as share."""

    def build(stats: _Stats):
        a, b = stats.number(hit), stats.number(miss)
        if a is None or b is None:
            return None
        return f"{fmt_bytes(a)} hit · {fmt_bytes(b)} miss", (a / (a + b) * 100 if a + b else None)

    return build


def _time_range(stats: _Stats):
    low, high = (
        stats.get("inputdatasetstatistics.extents.scannedmindatetime"),
        stats.get("inputdatasetstatistics.extents.scannedmaxdatetime"),
    )
    return None if low is None or high is None else (f"{low} → {high}", None)


def _returned(index: int) -> Builder:
    def build(stats: _Stats):
        return None if stats.shape is None else (fmt_count(stats.shape[index]), None)

    return build


_USAGE = "resourceusage."
_INPUT = "inputdatasetstatistics."
_SHARDS = _USAGE + "cache.shards."
_SPEC: list[tuple[str, str, Builder]] = [
    ("Time", "Wall time (client)", _value("clientelapsedseconds", fmt_seconds)),
    ("Time", "Execution time (service)", _value("executiontime", fmt_seconds, seconds=True)),
    ("CPU", "Total", _value(_USAGE + "cpu.totalcpu", fmt_seconds, seconds=True)),
    ("CPU", "User", _value(_USAGE + "cpu.user", fmt_seconds, seconds=True)),
    ("CPU", "Kernel", _value(_USAGE + "cpu.kernel", fmt_seconds, seconds=True)),
    ("Memory", "Peak per node", _value(_USAGE + "memory.peakpernode", fmt_bytes)),
    ("Data scanned", "Rows scanned", _of_total(_INPUT + "rows.scanned", _INPUT + "rows.total")),
    (
        "Data scanned",
        "Extents",
        _of_total(_INPUT + "extents.scanned", _INPUT + "extents.total"),
    ),
    ("Data scanned", "Scanned time range", _time_range),
    ("Data scanned", "Rowstore rows", _value(_INPUT + "rowstores.scannedrows", fmt_count)),
    ("Data scanned", "Rowstore size", _value(_INPUT + "rowstores.scannedvaluessize", fmt_bytes)),
    (
        "Cache",
        "Memory cache hits",
        _of_total(_USAGE + "cache.memory.hits", _USAGE + "cache.memory.total"),
    ),
    (
        "Cache",
        "Disk cache hits",
        _of_total(_USAGE + "cache.disk.hits", _USAGE + "cache.disk.total"),
    ),
    ("Cache", "Hot shard hit/miss", _hit_miss(_SHARDS + "hot.hitbytes", _SHARDS + "hot.missbytes")),
    (
        "Cache",
        "Cold shard hit/miss",
        _hit_miss(_SHARDS + "cold.hitbytes", _SHARDS + "cold.missbytes"),
    ),
    ("Cache", "Bypass", _value(_SHARDS + "bypassbytes", fmt_bytes)),
    ("Network", "Inter-cluster", _value(_USAGE + "network.interclustertotalbytes", fmt_bytes)),
    ("Network", "Cross-cluster", _value(_USAGE + "network.crossclustertotalbytes", fmt_bytes)),
    ("Result", "Rows returned", _returned(0)),
    ("Result", "Columns returned", _returned(1)),
]


def _table_rows(stats: _Stats) -> list[Row]:
    """One ``Tables`` row per dataset statistics entry."""
    rows = []
    for i in stats.indexes("datasetstatistics"):
        count = stats.number(f"datasetstatistics[{i}].tablerowcount")
        size = stats.number(f"datasetstatistics[{i}].tablesize")
        if count is not None and size is not None:
            rows.append(
                Row("Tables", f"Table {i + 1}", f"{fmt_count(count)} rows · {fmt_bytes(size)}")
            )
    return rows


def summary_rows(stats: _Stats) -> list[Row]:
    """The summary rows with data, in section order; a note if the service sent no statistics."""
    rows = []
    for section, metric, build in _SPEC:
        if (built := build(stats)) is not None:
            rows.append(Row(section, metric, *built))
    rows += _table_rows(stats)
    if not {r.section for r in rows} - {"Time", "Result"}:
        rows.append(Row("Note", "Statistics", _NO_STATISTICS))
    return rows


def _summary_styler(rows: list[Row], caption: str):
    index = pd.MultiIndex.from_tuples([(r.section, r.metric) for r in rows])
    frame = pd.DataFrame(
        {"Value": [r.value for r in rows], "Share": [r.share for r in rows]}, index=index
    ).astype({"Share": "float"})
    return (
        frame.style.hide(names=True, axis="index")
        .format({"Share": "{:.1f}%"}, na_rep="")
        .bar(subset=["Share"], vmin=0, vmax=100, color=BAR_COLOR)
        .set_caption(caption)
    )


def _text_table(header: list[str], body: list[list[str]]) -> str:
    """Rows of cells as left-aligned columns."""
    widths = [max(len(row[i]) for row in [header, *body]) for i in range(len(header))]
    return "\n".join(
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip()
        for row in [header, *body]
    )


def _summary_text(rows: list[Row], caption: str) -> str:
    body, previous = [], None
    for r in rows:
        share = "" if r.share is None else f"{r.share:.1f}%"
        body.append([r.section if r.section != previous else "", r.metric, r.value, share])
        previous = r.section
    return f"{caption}\n" + _text_table(["Section", "Metric", "Value", "Share"], body)


def _unused_html(profile: dict[str, Any], stats: _Stats) -> str | None:
    """A collapsed table of the keys no summary row used, or None if there are none."""
    unused = [(k, str(v)) for k, v in profile.items() if k not in stats.used]
    if not unused:
        return None
    table = pd.DataFrame(unused, columns=["Metric", "Value"]).to_html(index=False)
    return f"<details><summary>All statistics ({len(unused)})</summary>{table}</details>"


def _span_label(start: datetime, end: datetime) -> str:
    """``start → end`` without the clock times when both are midnight."""
    midnight = all(d.hour == d.minute == d.second == 0 for d in (start, end))
    fmt = "%Y-%m-%d" if midnight else "%Y-%m-%d %H:%M"
    return f"{start:{fmt}} → {end:{fmt}}"


_WINDOW_COLUMNS: dict[str, Callable[[_Stats], float | None]] = {
    "Execution time": lambda s: s.seconds("executiontime"),
    "CPU total": lambda s: s.seconds(_USAGE + "cpu.totalcpu"),
    "Peak memory": lambda s: s.number(_USAGE + "memory.peakpernode"),
    "Rows scanned": lambda s: s.number(_INPUT + "rows.scanned"),
    "Hot cache hit": lambda s: _ratio(
        s.number(_SHARDS + "hot.hitbytes"), s.number(_SHARDS + "hot.missbytes")
    ),
}
_WINDOW_FORMAT: dict[str, Callable[[float], str]] = {
    "Execution time": fmt_seconds,
    "CPU total": fmt_seconds,
    "Peak memory": fmt_bytes,
    "Rows scanned": fmt_count,
    "Hot cache hit": "{:.1f}%".format,
}


def _ratio(hit: float | None, miss: float | None) -> float | None:
    if hit is None or miss is None or hit + miss == 0:
        return None
    return hit / (hit + miss) * 100


def window_table(windows: list[dict[str, Any]]) -> tuple[pd.DataFrame, str]:
    """A numeric table with one row per window (the slowest 200 above that) and its caption."""
    rows = []
    for w in windows:
        stats = _Stats(w)
        rows.append(
            {"Window": _span_label(w["window_start"], w["window_end"])}
            | {name: get(stats) for name, get in _WINDOW_COLUMNS.items()}
        )
    frame = pd.DataFrame(rows, columns=["Window", *_WINDOW_COLUMNS]).astype(
        dict.fromkeys(_WINDOW_COLUMNS, "float")
    )
    caption = "Windows"
    if len(frame) > MAX_WINDOW_ROWS:
        frame = frame.sort_values(
            "Execution time", ascending=False, na_position="last", kind="stable"
        )
        caption = f"Windows ({MAX_WINDOW_ROWS} slowest of {len(frame)}, by execution time)"
        frame = frame.head(MAX_WINDOW_ROWS)
    return frame.reset_index(drop=True), caption


def _windows_styler(frame: pd.DataFrame, caption: str):
    return (
        frame.style.hide(axis="index")
        .format(_WINDOW_FORMAT, na_rep="")
        .bar(subset=[c for c in _WINDOW_COLUMNS if frame[c].max() > 0], vmin=0, color=BAR_COLOR)
        .set_caption(caption)
    )


def _windows_text(frame: pd.DataFrame, caption: str) -> str:
    body = [
        [row["Window"]]
        + ["" if pd.isna(row[c]) else _WINDOW_FORMAT[c](row[c]) for c in _WINDOW_COLUMNS]
        for _, row in frame.iterrows()
    ]
    return f"{caption}\n" + _text_table(["Window", *_WINDOW_COLUMNS], body)


def window_chart(windows: list[dict[str, Any]]) -> Any:
    """A timechart of execution and CPU time per window, or ``None`` below 2 timed windows."""
    from ._render import PlotlyMissingError, build_figure

    columns = ["Execution time", "CPU total"]
    stats = [_Stats(w) for w in windows]
    frame = pd.DataFrame(
        {"Window start": [w["window_start"] for w in windows]}
        | {c: pd.Series([_WINDOW_COLUMNS[c](s) for s in stats], dtype="float") for c in columns}
    )
    frame = frame.dropna(subset=["Execution time"]).dropna(axis=1, how="all")
    if len(frame) < 2:
        return None
    spec = {
        "visualization": "timechart",
        "title": "Execution and CPU time per window",
        "ytitle": "seconds",
    }
    try:
        return build_figure(frame, spec)
    except PlotlyMissingError:
        logger.debug("Plotly is not installed; skipping the window chart")
        return None


class _Html:
    """Raw HTML that IPython's ``display`` renders."""

    def __init__(self, html: str) -> None:
        """Keep ``html``."""
        self.html = html

    def _repr_html_(self) -> str:
        return self.html


def _emit(items: list[Any], text: list[str]) -> None:
    """Display ``items`` in IPython, or write ``text`` and the footer to stderr."""
    if _in_ipython():
        from IPython.display import display

        for item in items:
            display(item)
    else:
        sys.stderr.write("\n\n".join([*text, _FOOTER]) + "\n")


def show_profile(profile: dict[str, Any], shape: tuple[int, int] | None = None) -> None:
    """Show the profile of one query; never raises."""
    try:
        stats = _Stats(profile, shape)
        rows = summary_rows(stats)
        items: list[Any] = [_summary_styler(rows, CAPTION)] if _in_ipython() else []
        if (details := _unused_html(profile, stats)) and items:
            items.append(_Html(details))
        _emit(items, [_summary_text(rows, CAPTION)])
    except Exception:
        logger.debug("Could not show the query profile", exc_info=True)


def show_batch_profile(
    windows: list[dict[str, Any]], total: dict[str, Any], shape: tuple[int, int] | None = None
) -> None:
    """Show the totals of a batch and a table of its windows; never raises."""
    try:
        caption = f"{CAPTION} ({total['windows']} windows)"
        rows = summary_rows(_Stats(total, shape))
        frame, windows_caption = window_table(windows)
        items = []
        if _in_ipython():
            items = [_summary_styler(rows, caption), _windows_styler(frame, windows_caption)]
            if (chart := window_chart(windows)) is not None:
                items.append(chart)
        _emit(items, [_summary_text(rows, caption), _windows_text(frame, windows_caption)])
    except Exception:
        logger.debug("Could not show the query profile", exc_info=True)
