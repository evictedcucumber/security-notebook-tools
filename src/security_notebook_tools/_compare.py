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
from ._format import _TABLE_STYLES
from ._log import dependency_call
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

CAPTION = "Query performance comparison"
VS_BEST = "vs best"
BETTER_COLOR = "#2e7d32"
WORSE_COLOR = "#c62828"

_FORMATS: dict[str, Callable[[float], str]] = {
    "seconds": fmt_seconds,
    "bytes": fmt_bytes,
    "count": fmt_count,
    "percent": "{:.1f}%".format,
}
_LOWER, _HIGHER, _NEUTRAL = -1, 1, 0
_WEIGHTS = {"Time": 2, "CPU": 2, "Data scanned": 1, "Memory": 1, "Network": 1, "Cache": 0}


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


def _best(metric: _Metric, medians: list[float]) -> float:
    """The best median of a row; NaN for a neutral row or fewer than two values."""
    values = [m for m in medians if not pd.isna(m)]
    if metric.direction == _NEUTRAL or len(values) < 2:
        return float("nan")
    return max(values) if metric.direction == _HIGHER else min(values)


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
        best = _best(metric, medians)
        for k, (label, values, median) in enumerate(zip(labels, found, medians, strict=True)):
            row[(label, "Value")] = median
            if len(queries[k]) > 1:
                row[(label, "Spread")] = _spread(metric, values)
            row[(label, VS_BEST)] = _change(metric, best, median)
        metrics.append(metric)
        rows.append(row)
    index = pd.MultiIndex.from_tuples([(m.section, m.name) for m in metrics])
    data = pd.DataFrame(rows, index=index, dtype="float")
    data.columns = pd.MultiIndex.from_tuples(data.columns)
    return data, metrics, ranges


def _format_change(unit: str) -> Callable[[float], str]:
    suffix = " pp" if unit == "percent" else "%"
    return lambda x: f"0.0{suffix}" if x == 0 else f"{x:+.1f}{suffix}"


def _format_spread(unit: str) -> Callable[[float], str]:
    suffix = " pp" if unit == "percent" else "%"
    return lambda x: f"{x:.1f}{suffix}"


def _overlap(a: tuple[float, float] | None, b: tuple[float, float] | None) -> bool:
    """Whether two run ranges overlap; unknown ranges never do."""
    return a is not None and b is not None and not (a[1] < b[0] or b[1] < a[0])


def _styles(data: pd.DataFrame, metrics: list[_Metric], ranges: _Ranges) -> pd.DataFrame:
    """CSS per cell: bold best values, green clear winners, red changes outside the noise."""
    css = pd.DataFrame("", index=data.index, columns=data.columns)
    labels = list(dict.fromkeys(c[0] for c in data.columns))
    for position, metric in enumerate(metrics):
        if metric.direction == _NEUTRAL:
            continue
        values = data.iloc[position].xs("Value", level=1).dropna()
        best = values.max() if metric.direction == _HIGHER else values.min()
        winners = [labels.index(q) for q in values.index[values == best]]
        if values.nunique() > 1:
            for query in values.index[values == best]:
                css.iloc[position, css.columns.get_loc((query, "Value"))] = "font-weight: bold"
        if len(winners) == 1 and len(values) > 1:
            winner = winners[0]
            others = [labels.index(q) for q in values.index if labels.index(q) != winner]
            if not any(_overlap(ranges[position][winner], ranges[position][k]) for k in others):
                css.iloc[position, css.columns.get_loc((labels[winner], "Value"))] = (
                    f"font-weight: bold; color: {BETTER_COLOR}"
                )
        for k, label in enumerate(labels):
            change = data.iloc[position][(label, VS_BEST)]
            if pd.isna(change) or change == 0:
                continue
            if not any(_overlap(ranges[position][k], ranges[position][w]) for w in winners):
                css.iloc[position, css.columns.get_loc((label, VS_BEST))] = f"color: {WORSE_COLOR}"
    return css


_UNCOUNTED = " (not counted overall)"


class _Summary(NamedTuple):
    overall: str
    categories: list[tuple[str, str]]
    note: str | None


class _Comparison(Styler):
    """A Styler that renders its analysis after the table in a notebook."""

    summary: _Summary | None = None

    def _repr_html_(self) -> str | None:
        table = super()._repr_html_()
        if table is None or self.summary is None:
            return table
        return table + _summary_html(self.summary)


def _summary_html(summary: _Summary) -> str:
    """The analysis as HTML, every text escaped."""
    items = "".join(
        f"<li><strong>{html.escape(section)}:</strong> {html.escape(text)}</li>"
        for section, text in summary.categories
    )
    note = f"<p>{html.escape(summary.note)}</p>" if summary.note else ""
    return (
        f"<div><p><strong>Best overall:</strong> {html.escape(summary.overall)}</p>"
        f"<ul>{items}</ul>{note}</div>"
    )


def _join(names: list[str]) -> str:
    """Names as "A", "A and B" or "A, B and C"."""
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _beats(metric: _Metric, ranges: list, medians: list[float], q: int, r: int) -> bool:
    """Whether query q clearly beats r: a strictly better median outside the noise."""
    if pd.isna(medians[q]) or pd.isna(medians[r]) or _overlap(ranges[q], ranges[r]):
        return False
    return medians[q] > medians[r] if metric.direction == _HIGHER else medians[q] < medians[r]


def _winner_detail(
    rows: list[int], metrics: list[_Metric], ranges: _Ranges, medians: list[list[float]], q: int
) -> str:
    """The rows where q has the best median and clearly beats the runner-up, with the change."""
    parts = []
    for i in rows:
        metric, row = metrics[i], medians[i]
        others = [k for k in range(len(row)) if k != q and not pd.isna(row[k])]
        if pd.isna(row[q]) or not others:
            continue
        pick = max if metric.direction == _HIGHER else min
        runner_up = pick(others, key=lambda k, row=row: row[k])
        if _beats(metric, ranges[i], row, q, runner_up):
            change = _format_change(metric.unit)(_change(metric, row[runner_up], row[q]))
            parts.append(f"{metric.name} {change}")
    return f" ({', '.join(parts)} vs next best)" if parts else ""


def _category(
    rows: list[int],
    metrics: list[_Metric],
    ranges: _Ranges,
    medians: list[list[float]],
    labels: list[str],
) -> tuple[int | None, str]:
    """The winning query of a section (or None) and the line describing the result."""
    count = len(labels)
    wins = [0] * count
    losses = [0] * count
    for i in rows:
        for q in range(count):
            for r in range(count):
                if q != r and _beats(metrics[i], ranges[i], medians[i], q, r):
                    wins[q] += 1
                    losses[r] += 1
    if not any(wins):
        return None, "no clear winner (within run-to-run noise)"
    scores = [w - lost for w, lost in zip(wins, losses, strict=True)]
    leaders = [q for q in range(count) if scores[q] == max(scores)]
    if len(leaders) > 1 or not wins[leaders[0]]:
        return None, f"tie between {_join([labels[q] for q in leaders])}"
    winner = leaders[0]
    return winner, labels[winner] + _winner_detail(rows, metrics, ranges, medians, winner)


def _overall(winners: dict[str, int | None], labels: list[str]) -> str:
    """The best query overall by weighted category wins, with trade-offs of the others."""
    won = {q: [s for s, w in winners.items() if w == q] for q in range(len(labels))}
    points = {q: sum(_WEIGHTS[s] for s in sections) for q, sections in won.items()}
    top = max(points.values(), default=0)
    if top == 0:
        return (
            "No clear best overall: the differences in the weighted categories are within "
            "run-to-run noise."
        )
    leaders = [q for q, p in points.items() if p == top]
    total = sum(_WEIGHTS[s] for s in winners)
    if len(leaders) > 1:
        names = _join([labels[q] for q in leaders])
        return f"No single best overall: {names} each have {top} of {total} points."
    best = leaders[0]
    shown = ", ".join(s + (f" \u00d7{_WEIGHTS[s]}" if _WEIGHTS[s] > 1 else "") for s in won[best])
    sentences = [f"{labels[best]}, {top} of {total} points ({shown})"]
    sentences += [
        f"{labels[q]} is best in {_join(sections)}"
        for q, sections in won.items()
        if q != best and sections
    ]
    return ". ".join(sentences) + ("." if len(sentences) > 1 else "")


def _summarize(
    data: pd.DataFrame,
    metrics: list[_Metric],
    ranges: _Ranges,
    labels: list[str],
    single_runs: list[str],
) -> _Summary:
    """The best query per category and overall, judged by clear wins outside the noise."""
    medians = data.xs("Value", axis=1, level=1)[labels].to_numpy().tolist()
    sections: dict[str, list[int]] = {}
    for i, metric in enumerate(metrics):
        if metric.direction != _NEUTRAL:
            sections.setdefault(metric.section, []).append(i)
    results = {s: _category(rows, metrics, ranges, medians, labels) for s, rows in sections.items()}
    winners = {s: winner for s, (winner, _) in results.items()}
    logger.debug(
        "Summarised %d categories, %d with a clear winner",
        len(winners),
        sum(w is not None for w in winners.values()),
    )
    note = None
    if single_runs:
        note = (
            f"{_join(single_runs)} ran once, so run-to-run noise cannot be judged and any "
            "difference counts; use exec_query_runs for 3 or more runs."
        )
    return _Summary(
        _overall(winners, labels),
        [(s, text + _UNCOUNTED if _WEIGHTS[s] == 0 else text) for s, (_, text) in results.items()],
        note,
    )


def _style(
    data: pd.DataFrame,
    metrics: list[_Metric],
    ranges: _Ranges,
    caption: str,
    summary: _Summary | None = None,
) -> Styler:
    value_columns = [c for c in data.columns if c[1] == "Value"]
    spread_columns = [c for c in data.columns if c[1] == "Spread"]
    change_columns = [c for c in data.columns if c[1] == VS_BEST]
    with dependency_call(logger, "pandas Styler", rows=len(data.index), columns=len(data.columns)):
        styler = _Comparison(data)
        styler.summary = summary
        styler = styler.hide(names=True, axis="index").set_caption(html.escape(caption))
        for (section, name), metric in zip(data.index, metrics, strict=True):
            row = [(section, name)]
            styler = (
                styler.format(_FORMATS[metric.unit], subset=(row, value_columns), na_rep="")
                .format(_format_spread(metric.unit), subset=(row, spread_columns), na_rep="")
                .format(_format_change(metric.unit), subset=(row, change_columns), na_rep="")
            )
        styler = (
            styler.format_index(escape="html", axis=0)
            .format_index(escape="html", axis=1)
            .format_index_names(escape="html", axis=1)
            .set_table_styles(_TABLE_STYLES)
            .set_table_styles(
                {
                    column: [
                        {"selector": sel, "props": "text-align: right;"} for sel in ("th", "td")
                    ]
                    for column in data.columns
                },
                overwrite=False,
            )
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
    counts. Every query has a "vs best" column: the change of its median from the best query's
    in that row (the lowest, or the highest for cache hit rates), in percent of the best
    (percentage points for cache hit rates); the best query shows 0, and neutral rows (result
    size, runs) are blank. A change is red only when the query's run range (min to max) overlaps
    that of none of the best queries; an overlapping change is within run-to-run noise and stays
    uncoloured (a query with fewer than two runs has no range, so its change is always red). The
    best value of a row is bold, and also green when it is the only best and its run range
    overlaps that of no other query. Rows no query reports are left out. In a notebook, a short
    analysis follows the table: the best query per category (a section with directional rows) and
    overall, by clear wins (a better median outside the other query's run range), a tie or "no
    clear winner" within the noise, and a note when a query ran once. Overall, each category won
    scores points: Time and CPU 2, Data scanned, Memory and Network 1, and Cache 0, as cache hits
    reflect earlier queries rather than this one. A batch result is totalled
    over its windows and has no wall time. The frames are not modified.

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
        index and (Label, Value / Spread / vs best) columns.

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
        single_runs = [n for n, r in zip(names, runs, strict=True) if len(r) == 1]
        summary = _summarize(data, metrics, ranges, names, single_runs)
        return _style(data, metrics, ranges, CAPTION, summary)
