"""The public ``compare_performance`` function: query profiles side by side."""

from __future__ import annotations

import html
import logging
import statistics
from collections.abc import Callable, Sequence
from typing import NamedTuple

import pandas as pd
from pandas.io.formats.style import Styler

from ._errors import public_call
from ._exceptions import ConfigurationError
from ._profile import PROFILE_ATTR, total_profile
from ._profile_render import (
    _INPUT,
    _SHARDS,
    _USAGE,
    _ratio,
    _Stats,
    fmt_bytes,
    fmt_count,
    fmt_seconds,
)
from ._query import _check_bool

logger = logging.getLogger("security_notebook_tools.compare")

CAPTION = "Query performance comparison (baseline: {})"
BETTER_COLOR = "#2e7d32"
WORSE_COLOR = "#c62828"

_FORMATS: dict[str, Callable[[float], str]] = {
    "seconds": fmt_seconds,
    "bytes": fmt_bytes,
    "count": fmt_count,
    "percent": "{:.1f}%".format,
}
_LOWER, _HIGHER, _NEUTRAL = -1, 1, 0


class _Metric(NamedTuple):
    section: str
    name: str
    unit: str
    direction: int
    get: Callable[[_Stats], float | None]


def _number(key: str) -> Callable[[_Stats], float | None]:
    return lambda s: s.number(key)


def _seconds(key: str) -> Callable[[_Stats], float | None]:
    return lambda s: s.seconds(key)


def _share(hit: str, total: str) -> Callable[[_Stats], float | None]:
    def get(stats: _Stats) -> float | None:
        a, b = stats.number(hit), stats.number(total)
        return None if a is None or not b else a / b * 100

    return get


def _hit_ratio(prefix: str) -> Callable[[_Stats], float | None]:
    return lambda s: _ratio(s.number(prefix + "hitbytes"), s.number(prefix + "missbytes"))


def _shape(index: int) -> Callable[[_Stats], float | None]:
    return lambda s: None if s.shape is None else float(s.shape[index])


_METRICS = [
    _Metric("Time", "Wall time (client)", "seconds", _LOWER, _number("clientelapsedseconds")),
    _Metric("Time", "Execution time (service)", "seconds", _LOWER, _seconds("executiontime")),
    _Metric("CPU", "Total", "seconds", _LOWER, _seconds(_USAGE + "cpu.totalcpu")),
    _Metric("CPU", "User", "seconds", _LOWER, _seconds(_USAGE + "cpu.user")),
    _Metric("CPU", "Kernel", "seconds", _LOWER, _seconds(_USAGE + "cpu.kernel")),
    _Metric("Memory", "Peak per node", "bytes", _LOWER, _number(_USAGE + "memory.peakpernode")),
    _Metric("Data scanned", "Rows scanned", "count", _LOWER, _number(_INPUT + "rows.scanned")),
    _Metric(
        "Data scanned", "Extents scanned", "count", _LOWER, _number(_INPUT + "extents.scanned")
    ),
    _Metric(
        "Data scanned", "Rowstore rows", "count", _LOWER, _number(_INPUT + "rowstores.scannedrows")
    ),
    _Metric(
        "Data scanned",
        "Rowstore size",
        "bytes",
        _LOWER,
        _number(_INPUT + "rowstores.scannedvaluessize"),
    ),
    _Metric(
        "Cache",
        "Memory cache hit",
        "percent",
        _HIGHER,
        _share(_USAGE + "cache.memory.hits", _USAGE + "cache.memory.total"),
    ),
    _Metric(
        "Cache",
        "Disk cache hit",
        "percent",
        _HIGHER,
        _share(_USAGE + "cache.disk.hits", _USAGE + "cache.disk.total"),
    ),
    _Metric("Cache", "Hot shard hit", "percent", _HIGHER, _hit_ratio(_SHARDS + "hot.")),
    _Metric("Cache", "Cold shard hit", "percent", _HIGHER, _hit_ratio(_SHARDS + "cold.")),
    _Metric("Cache", "Bypass", "bytes", _LOWER, _number(_SHARDS + "bypassbytes")),
    _Metric(
        "Network",
        "Inter-cluster",
        "bytes",
        _LOWER,
        _number(_USAGE + "network.interclustertotalbytes"),
    ),
    _Metric(
        "Network",
        "Cross-cluster",
        "bytes",
        _LOWER,
        _number(_USAGE + "network.crossclustertotalbytes"),
    ),
    _Metric("Result", "Rows returned", "count", _NEUTRAL, _shape(0)),
    _Metric("Result", "Columns returned", "count", _NEUTRAL, _shape(1)),
]
_RUNS = _Metric("Result", "Runs", "count", _NEUTRAL, lambda s: None)


def _stats(frame: pd.DataFrame) -> _Stats:
    """The statistics of one frame; a batch is totalled and has no wall time."""
    profile = frame.attrs[PROFILE_ATTR]
    if isinstance(profile, list):
        profile = {
            k: v for k, v in total_profile(profile, 0).items() if k != "client_elapsed_seconds"
        }
    return _Stats(profile, (frame.shape[0], frame.shape[1]))


def _change(metric: _Metric, base: float, value: float) -> float:
    """Percentage points for percent metrics, else the relative change in percent."""
    if pd.isna(base) or pd.isna(value):
        return float("nan")
    if metric.unit == "percent":
        return value - base
    if base == 0:
        return 0.0 if value == 0 else float("nan")
    return (value - base) / base * 100


def _spread(metric: _Metric, values: list[float]) -> float:
    """Max minus min of the runs: percentage points, else percent of the median."""
    if len(values) < 2:
        return float("nan")
    width = max(values) - min(values)
    if metric.unit == "percent":
        return width
    median = statistics.median(values)
    if median == 0:
        return 0.0 if width == 0 else float("nan")
    return width / abs(median) * 100


_Ranges = list[list[tuple[float, float] | None]]


def _build(
    queries: list[list[pd.DataFrame]], labels: list[str]
) -> tuple[pd.DataFrame, list[_Metric], _Ranges]:
    """The numeric frame, the metrics of its rows and each row's per-query (min, max) runs."""
    stats = [[_stats(f) for f in runs] for runs in queries]
    metrics, rows, ranges = [], [], []
    for metric in [*_METRICS, _RUNS]:
        if metric is _RUNS:
            if all(len(runs) == 1 for runs in queries):
                continue
            found = [[float(len(runs))] for runs in queries]
        else:
            found = [[v for s in runs if (v := metric.get(s)) is not None] for runs in stats]
            if not any(found):
                continue
        ranges.append([(min(v), max(v)) if len(v) > 1 else None for v in found])
        medians = [statistics.median(v) if v else float("nan") for v in found]
        row: dict[tuple[str, str], float] = {}
        for k, (label, values, median) in enumerate(zip(labels, found, medians, strict=True)):
            row[(label, "Value")] = median
            if len(queries[k]) > 1:
                row[(label, "Spread")] = _spread(metric, values)
            if k > 0:
                row[(label, f"vs {labels[0]}")] = (
                    float("nan") if metric is _RUNS else _change(metric, medians[0], median)
                )
        metrics.append(metric)
        rows.append(row)
    index = pd.MultiIndex.from_tuples([(m.section, m.name) for m in metrics])
    data = pd.DataFrame(rows, index=index, dtype="float")
    data.columns = pd.MultiIndex.from_tuples(data.columns)
    return data, metrics, ranges


def _format_change(unit: str) -> Callable[[float], str]:
    suffix = " pp" if unit == "percent" else "%"
    return lambda x: f"{x:+.1f}{suffix}"


def _format_spread(unit: str) -> Callable[[float], str]:
    suffix = " pp" if unit == "percent" else "%"
    return lambda x: f"{x:.1f}{suffix}"


def _overlap(a: tuple[float, float] | None, b: tuple[float, float] | None) -> bool:
    """Whether two run ranges overlap; unknown ranges never do."""
    return a is not None and b is not None and not (a[1] < b[0] or b[1] < a[0])


def _styles(data: pd.DataFrame, metrics: list[_Metric], ranges: _Ranges) -> pd.DataFrame:
    """CSS per cell: bold best values, green or red changes outside the run-to-run noise."""
    css = pd.DataFrame("", index=data.index, columns=data.columns)
    for position, metric in enumerate(metrics):
        if metric.direction == _NEUTRAL:
            continue
        row = data.iloc[position]
        values = row.xs("Value", level=1).dropna()
        if values.nunique() > 1:
            best = values.max() if metric.direction == _HIGHER else values.min()
            for query in values.index[values == best]:
                css.iloc[position, css.columns.get_loc((query, "Value"))] = "font-weight: bold"
        labels = list(dict.fromkeys(c[0] for c in data.columns))
        for column, change in row.items():
            if column[1] not in ("Value", "Spread") and pd.notna(change) and change != 0:
                if _overlap(ranges[position][0], ranges[position][labels.index(column[0])]):
                    continue
                color = BETTER_COLOR if change * metric.direction > 0 else WORSE_COLOR
                css.iloc[position, css.columns.get_loc(column)] = f"color: {color}"
    return css


def _style(data: pd.DataFrame, metrics: list[_Metric], ranges: _Ranges, caption: str) -> Styler:
    styler = (
        data.style.hide(names=True, axis="index")
        .format_index(escape="html", axis=1)
        .set_caption(html.escape(caption))
    )
    value_columns = [c for c in data.columns if c[1] == "Value"]
    spread_columns = [c for c in data.columns if c[1] == "Spread"]
    change_columns = [c for c in data.columns if c[1] not in ("Value", "Spread")]
    for (section, name), metric in zip(data.index, metrics, strict=True):
        row = [(section, name)]
        styler = (
            styler.format(_FORMATS[metric.unit], subset=(row, value_columns), na_rep="")
            .format(_format_spread(metric.unit), subset=(row, spread_columns), na_rep="")
            .format(_format_change(metric.unit), subset=(row, change_columns), na_rep="")
        )
    return styler.apply(_styles, metrics=metrics, ranges=ranges, axis=None)


def _runs_of(number: int, query: object) -> list[pd.DataFrame]:
    """The profiled frames of argument ``number``: one DataFrame or a non-empty list of them."""
    if isinstance(query, pd.DataFrame):
        frames, where = [query], [f"Argument {number}"]
    elif isinstance(query, (list, tuple)):
        if not query:
            raise ConfigurationError(f"Argument {number} is an empty list; give at least one run.")
        frames = list(query)
        where = [f"Argument {number}, run {run}" for run in range(1, len(frames) + 1)]
    else:
        raise ConfigurationError(
            f"Argument {number} must be a pandas DataFrame or a list of them, "
            f"got {type(query).__name__}."
        )
    for frame, name in zip(frames, where, strict=True):
        if not isinstance(frame, pd.DataFrame):
            raise ConfigurationError(
                f"{name} must be a pandas DataFrame, got {type(frame).__name__}."
            )
        if not frame.attrs.get(PROFILE_ATTR):
            raise ConfigurationError(
                f"{name} has no query profile; run its query with profile=True."
            )
    return frames


def _labels_for(labels: Sequence[str] | None, count: int) -> list[str]:
    """The validated labels, or "Query 1" ... "Query N"."""
    if labels is None:
        return [f"Query {k}" for k in range(1, count + 1)]
    if isinstance(labels, str) or not isinstance(labels, Sequence):
        raise ConfigurationError(
            f"labels must be a sequence of strings, got {type(labels).__name__}."
        )
    if len(labels) != count:
        raise ConfigurationError(
            f"labels has {len(labels)} item(s) for {count} query(ies); pass one label per query."
        )
    for number, label in enumerate(labels, start=1):
        if not isinstance(label, str) or not label.strip():
            raise ConfigurationError(f"Label {number} must be a non-empty string, got {label!r}.")
    if len(set(labels)) != len(labels):
        raise ConfigurationError("labels must be unique.")
    return list(labels)


def compare_performance(
    *queries: pd.DataFrame | Sequence[pd.DataFrame],
    labels: Sequence[str] | None = None,
    debug: bool = False,
) -> Styler:
    """Compare the query-performance statistics of two or more queries side by side.

    Run each query with ``profile=True`` first, or with ``exec_query_runs`` for several runs. Each
    argument is one query: a DataFrame (one run) or a list of DataFrames (several runs of the
    same query). The table has one row per metric (time, CPU, memory, data scanned, cache,
    network, result size) and a "Value" column per query: the median over its runs. A query given
    as two or more runs also has a "Spread" column, the range of the runs (max minus min) as a
    percent of the median (percentage points for cache hit rates), and a "Runs" row shows the run
    counts. The first query is the baseline: every later query has a "vs <first label>" column
    with the relative change in percent of the median (percentage points for cache hit rates).
    Green is better, red is worse, but only when the two queries' run ranges (min to max) do not
    overlap; an overlapping change is within run-to-run noise and stays uncoloured (a query with
    fewer than two runs has no range, so its changes are coloured by sign). The best value of a
    row is bold. Rows no query reports are left out. A batch result is totalled over its
    windows and has no wall time. The frames are not modified.

    Args:
        *queries: Two or more queries. Each is a DataFrame or a non-empty list or tuple of
            DataFrames from ``exec_query``, ``exec_query_file``, ``exec_query_runs`` or
            ``exec_batch_query`` called with ``profile=True``.
        labels: Optional column names, one non-empty, distinct string per query in argument
            order. Default "Query 1" ... "Query N".
        debug: If true, also print this call's log records, at DEBUG and above, to the
            notebook cell's output while it runs. Default false. The lines are printed unredacted
            (debug output is not written to the file; be careful when sharing a notebook saved
            with debug output). See the logging section of the README.

    Returns:
        A pandas ``Styler``; its ``.data`` is the numeric DataFrame with a (Section, Metric) row
        index and (Label, Value / Spread / vs <first label>) columns.

    Raises:
        ConfigurationError: fewer than two queries, an argument that is neither a DataFrame nor a
            list of them, an empty list, a frame without a profile (run it with
            ``profile=True``; the message names the argument and run), ``labels`` that are not a
            sequence of non-empty distinct strings or do not match the number of queries, or
            ``debug`` is not a bool. It is also a ``ValueError``.

    Example:
        >>> from security_notebook_tools import compare_performance, exec_query_runs
        >>> a = exec_query_runs("SigninLogs | take 100", 5)
        >>> b = exec_query_runs("SigninLogs | where ResultType == 0 | take 100", 5)
        >>> compare_performance(a, b, labels=["all", "success"])
    """
    with public_call(logger, "compare_performance", debug=debug, queries=len(queries)) as call:
        _check_bool("debug", debug)
        if len(queries) < 2:
            raise ConfigurationError(
                f"compare_performance needs at least 2 queries, got {len(queries)}."
            )
        runs = [_runs_of(number, q) for number, q in enumerate(queries, start=1)]
        names = _labels_for(labels, len(queries))
        data, metrics, ranges = _build(runs, names)
        logger.debug("Compared %d queries over %d metrics", len(queries), len(metrics))
        call.update(metrics=len(metrics))
        return _style(data, metrics, ranges, CAPTION.format(names[0]))
