import math

import pandas as pd
import pytest

from security_notebook_tools import compare_performance
from security_notebook_tools._compare import BETTER_COLOR, WORSE_COLOR
from security_notebook_tools._exceptions import ConfigurationError

EXEC = ("Time", "Execution time (service)")
CPU = ("CPU", "Total")
MEMORY = ("Memory", "Peak per node")
ROWS = ("Data scanned", "Rows scanned")
HOT = ("Cache", "Hot shard hit")
WALL = ("Time", "Wall time (client)")


def _frame(rows=3, cols=2, **profile):
    out = pd.DataFrame({f"c{i}": range(rows) for i in range(cols)})
    out.attrs["profile"] = profile
    return out


def _profile(execution="00:00:01", cpu="00:00:02", peak=1000, scanned=100, hit=None, wall=1.5):
    profile = {
        "client_elapsed_seconds": wall,
        "ExecutionTime": execution,
        "resource_usage.cpu.total cpu": cpu,
        "resource_usage.memory.peak_per_node": peak,
        "input_dataset_statistics.rows.scanned": scanned,
    }
    if hit is not None:
        hot, miss = hit
        profile["resource_usage.cache.shards.hot.hitbytes"] = hot
        profile["resource_usage.cache.shards.hot.missbytes"] = miss
    return profile


def _css(styler):
    """CSS declarations per (row label, column label) of the styled table."""
    ctx = styler._compute().ctx
    rows, columns = styler.data.index, styler.data.columns
    return {
        (rows[r], columns[c]): " ".join(f"{k}: {v}" for k, v in props)
        for (r, c), props in ctx.items()
    }


@pytest.fixture
def pair():
    a = _frame(**_profile())
    b = _frame(**_profile(execution="00:00:00.75", cpu="00:00:03", peak=500, scanned=100))
    return a, b


@pytest.mark.parametrize("frames", [0, 1])
def test_needs_two_frames(pair, frames):
    with pytest.raises(ConfigurationError, match="at least 2") as err:
        compare_performance(*pair[:frames])
    assert isinstance(err.value, ValueError)


def test_non_dataframe_names_argument(pair):
    with pytest.raises(ConfigurationError, match="Argument 2"):
        compare_performance(pair[0], {"a": 1})


@pytest.mark.parametrize("attrs", [{}, {"profile": {}}])
def test_frame_without_profile(pair, attrs):
    bare = pd.DataFrame({"a": [1]})
    bare.attrs.update(attrs)
    with pytest.raises(ConfigurationError, match="Argument 2.*profile=True"):
        compare_performance(pair[0], bare)


@pytest.mark.parametrize("debug", [1, "yes", None])
def test_debug_must_be_bool(pair, debug):
    with pytest.raises(ConfigurationError):
        compare_performance(*pair, debug=debug)


def test_columns_for_three_frames(pair):
    data = compare_performance(*pair, pair[0]).data
    assert list(data.columns) == [
        ("Query 1", "Value"),
        ("Query 2", "Value"),
        ("Query 2", "vs Query 1"),
        ("Query 3", "Value"),
        ("Query 3", "vs Query 1"),
    ]
    assert data.index.names == [None, None]


def test_values_parsed_and_unreported_rows_dropped(pair):
    data = compare_performance(*pair).data
    assert data.loc[EXEC, ("Query 1", "Value")] == 1.0
    assert data.loc[EXEC, ("Query 2", "Value")] == 0.75
    assert data.loc[CPU, ("Query 2", "Value")] == 3.0
    assert data.loc[MEMORY, ("Query 2", "Value")] == 500
    assert ("Network", "Inter-cluster") not in data.index
    assert HOT not in data.index


def test_missing_value_in_one_query_is_nan(pair):
    partial = _frame(**{"ExecutionTime": "00:00:02"})
    data = compare_performance(pair[0], partial).data
    assert math.isnan(data.loc[CPU, ("Query 2", "Value")])
    assert math.isnan(data.loc[CPU, ("Query 2", "vs Query 1")])
    assert data.loc[EXEC, ("Query 2", "Value")] == 2.0


def test_relative_change(pair):
    data = compare_performance(*pair).data
    assert data.loc[EXEC, ("Query 2", "vs Query 1")] == pytest.approx(-25.0)
    assert data.loc[CPU, ("Query 2", "vs Query 1")] == pytest.approx(50.0)
    assert data.loc[ROWS, ("Query 2", "vs Query 1")] == 0.0


def test_zero_baseline():
    a = _frame(**_profile(scanned=0, peak=0))
    b = _frame(**_profile(scanned=5, peak=0))
    data = compare_performance(a, b).data
    assert math.isnan(data.loc[ROWS, ("Query 2", "vs Query 1")])
    assert data.loc[MEMORY, ("Query 2", "vs Query 1")] == 0.0


def test_percent_rows_use_percentage_points():
    a = _frame(**_profile(hit=(50, 50)))
    b = _frame(**_profile(hit=(90, 10)))
    data = compare_performance(a, b).data
    assert data.loc[HOT, ("Query 1", "Value")] == pytest.approx(50.0)
    assert data.loc[HOT, ("Query 2", "Value")] == pytest.approx(90.0)
    assert data.loc[HOT, ("Query 2", "vs Query 1")] == pytest.approx(40.0)


def test_batch_frame_is_totalled_without_wall_time():
    windows = [
        {**_profile(execution="00:00:01", peak=100, scanned=10), "window_start": "a"},
        {**_profile(execution="00:00:02", peak=300, scanned=20), "window_start": "b"},
    ]
    batch = pd.DataFrame({"x": [1, 2]})
    batch.attrs["profile"] = windows
    data = compare_performance(batch, _frame(**_profile())).data
    assert data.loc[EXEC, ("Query 1", "Value")] == 3.0
    assert data.loc[ROWS, ("Query 1", "Value")] == 30
    assert data.loc[MEMORY, ("Query 1", "Value")] == 300
    assert math.isnan(data.loc[WALL, ("Query 1", "Value")])
    assert data.loc[WALL, ("Query 2", "Value")] == 1.5


def test_result_rows_come_from_frame_shape(pair):
    a = _frame(rows=4, cols=2, **_profile())
    b = _frame(rows=6, cols=3, **_profile())
    data = compare_performance(a, b).data
    assert data.loc[("Result", "Rows returned")].tolist() == [4, 6, 50.0]
    assert data.loc[("Result", "Columns returned")].tolist() == [2, 3, 50.0]


def test_best_value_bold_in_lower_is_better_row(pair):
    css = _css(compare_performance(*pair))
    assert "bold" in css[EXEC, ("Query 2", "Value")]
    assert "bold" not in css.get((EXEC, ("Query 1", "Value")), "")
    assert "bold" in css[CPU, ("Query 1", "Value")]
    assert (ROWS, ("Query 1", "Value")) not in css or "bold" not in css[ROWS, ("Query 1", "Value")]


def test_best_cache_hit_is_highest():
    a = _frame(**_profile(hit=(50, 50)))
    b = _frame(**_profile(hit=(90, 10)))
    css = _css(compare_performance(a, b))
    assert "bold" in css[HOT, ("Query 2", "Value")]
    assert "bold" not in css.get((HOT, ("Query 1", "Value")), "")
    assert BETTER_COLOR in css[HOT, ("Query 2", "vs Query 1")]


def test_change_colours(pair):
    css = _css(compare_performance(*pair))
    assert BETTER_COLOR in css[EXEC, ("Query 2", "vs Query 1")]
    assert WORSE_COLOR in css[CPU, ("Query 2", "vs Query 1")]
    assert "color" not in css.get((ROWS, ("Query 2", "vs Query 1")), "")


def test_result_rows_are_not_styled():
    a = _frame(rows=4, **_profile())
    b = _frame(rows=6, **_profile())
    css = _css(compare_performance(a, b))
    assert not [k for k in css if k[0][0] == "Result" and css[k]]


def test_change_formats():
    a = _frame(**_profile(hit=(50, 50)))
    b = _frame(**_profile(hit=(90, 10), cpu="00:00:03"))
    html = compare_performance(a, b).to_html()
    assert "+40.0 pp" in html
    assert "+50.0%" in html


def test_input_attrs_not_modified(pair):
    before = [f.attrs["profile"].copy() for f in pair]
    compare_performance(*pair)
    assert [f.attrs["profile"] for f in pair] == before


def _runs(*executions, **kwargs):
    return [_frame(**_profile(execution=f"00:00:{e:05.2f}", **kwargs)) for e in executions]


RUNS = ("Result", "Runs")


@pytest.mark.parametrize(
    ("executions", "median"), [((1, 5, 3), 3.0), ((1, 2, 4, 9), 3.0), ((2,), 2.0)]
)
def test_list_value_is_median(executions, median):
    data = compare_performance(_runs(*executions), _runs(1)).data
    assert data.loc[EXEC, ("Query 1", "Value")] == median


def test_tuple_accepted_and_vs_uses_medians():
    data = compare_performance(tuple(_runs(1, 2, 9)), _runs(1)).data
    assert data.loc[EXEC, ("Query 1", "Value")] == 2.0
    assert data.loc[EXEC, ("Query 2", "vs Query 1")] == pytest.approx(-50.0)


def test_run_missing_a_metric_is_ignored_in_median():
    runs = [_frame(**_profile(cpu="00:00:02")), _frame(**{"ExecutionTime": "00:00:01"})]
    runs.append(_frame(**_profile(cpu="00:00:04")))
    data = compare_performance(runs, _runs(1)).data
    assert data.loc[CPU, ("Query 1", "Value")] == 3.0
    assert data.loc[CPU, ("Query 1", "Spread")] == pytest.approx(200 / 3)


def test_spread_is_range_as_percent_of_median():
    data = compare_performance(_runs(1, 2, 3), _runs(1)).data
    assert data.loc[EXEC, ("Query 1", "Spread")] == pytest.approx(100.0)
    assert data.loc[ROWS, ("Query 1", "Spread")] == 0.0


def test_spread_of_percent_row_is_percentage_points():
    runs = [_frame(**_profile(hit=h)) for h in [(50, 50), (70, 30), (60, 40)]]
    data = compare_performance(runs, _frame(**_profile(hit=(50, 50)))).data
    assert data.loc[HOT, ("Query 1", "Spread")] == pytest.approx(20.0)
    assert "20.0 pp" in compare_performance(runs, _frame(**_profile(hit=(1, 1)))).to_html()


def test_spread_with_zero_median():
    same = [_frame(**_profile(peak=0)) for _ in range(2)]
    differing = [_frame(**_profile(peak=p)) for p in (0, 0, 5)]
    data = compare_performance(same, differing).data
    assert data.loc[MEMORY, ("Query 1", "Spread")] == 0.0
    assert math.isnan(data.loc[MEMORY, ("Query 2", "Spread")])


def test_spread_nan_when_fewer_than_two_values():
    runs = [_frame(**_profile()), _frame(**{"ExecutionTime": "00:00:01"})]
    data = compare_performance(runs, _runs(1)).data
    assert math.isnan(data.loc[CPU, ("Query 1", "Spread")])


def test_spread_column_only_for_multi_run_queries():
    data = compare_performance(_runs(1, 2), _runs(1), _runs(1, 2, 3)).data
    assert list(data.columns) == [
        ("Query 1", "Value"),
        ("Query 1", "Spread"),
        ("Query 2", "Value"),
        ("Query 2", "vs Query 1"),
        ("Query 3", "Value"),
        ("Query 3", "Spread"),
        ("Query 3", "vs Query 1"),
    ]


def test_single_runs_have_no_spread_or_runs_row(pair):
    data = compare_performance([pair[0]], pair[1]).data
    assert RUNS not in data.index
    assert not [c for c in data.columns if c[1] == "Spread"]


def test_runs_row_when_any_query_has_several_runs():
    data = compare_performance(_runs(1, 2, 3), _runs(1)).data
    assert data.loc[RUNS, ("Query 1", "Value")] == 3
    assert data.loc[RUNS, ("Query 2", "Value")] == 1
    assert math.isnan(data.loc[RUNS, ("Query 2", "vs Query 1")])
    assert math.isnan(data.loc[RUNS, ("Query 1", "Spread")])


def test_spread_and_runs_cells_are_not_coloured():
    css = _css(compare_performance(_runs(1, 5), _runs(1, 2, 3)))
    assert not [k for k in css if k[1][1] == "Spread" and "color" in css[k]]
    assert not [k for k in css if k[0] == RUNS and css[k]]


def test_labels_name_columns_vs_and_caption():
    styler = compare_performance(*_pair_frames(), labels=["old", "new"])
    assert list(styler.data.columns) == [
        ("old", "Value"),
        ("new", "Value"),
        ("new", "vs old"),
    ]
    assert styler.caption == "Query performance comparison (baseline: old)"


def _pair_frames():
    return _frame(**_profile()), _frame(**_profile(cpu="00:00:03"))


@pytest.mark.parametrize(
    ("labels", "match"),
    [
        ("ab", "sequence of strings"),
        (5, "sequence of strings"),
        (["a"], "1 item.*2 query"),
        (["a", "b", "c"], "3 item.*2 query"),
        (["a", 2], "Label 2"),
        (["a", None], "Label 2"),
        (["a", "  "], "Label 2"),
        ([""], "1 item"),
        (["a", "a"], "unique"),
    ],
)
def test_bad_labels(labels, match):
    with pytest.raises(ConfigurationError, match=match):
        compare_performance(*_pair_frames(), labels=labels)


def test_tuple_labels_accepted():
    assert compare_performance(*_pair_frames(), labels=("a", "b")).data.columns[0] == ("a", "Value")


def test_labels_and_caption_are_html_escaped():
    html = compare_performance(*_pair_frames(), labels=["<b>x</b>", "y&z"]).to_html()
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert "y&amp;z" in html
    assert "baseline: &lt;b&gt;x&lt;/b&gt;" in html


def test_list_argument_errors_name_the_run():
    good = _runs(1, 2)
    with pytest.raises(ConfigurationError, match="Argument 2, run 3.*DataFrame"):
        compare_performance(good, [*good, "x"])
    bare = pd.DataFrame({"a": [1]})
    with pytest.raises(ConfigurationError, match=r"Argument 2, run 2.*profile=True"):
        compare_performance(good, [good[0], bare])
    with pytest.raises(ConfigurationError, match="Argument 1 is an empty list"):
        compare_performance([], good)


def _verdict(*queries, row=EXEC, column="Query 2"):
    css = _css(compare_performance(*queries)).get((row, (column, "vs Query 1")), "")
    return "better" if BETTER_COLOR in css else "worse" if WORSE_COLOR in css else "none"


def _hit_runs(*hits):
    return [_frame(**_profile(hit=(h, 100 - h))) for h in hits]


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ((1, 3), (2, 4), "none"),
        ((1, 3), (3, 5), "none"),
        ((1, 3), (3.5, 5), "worse"),
        ((4, 6), (1, 3), "better"),
        ((1, 5), (2, 3), "none"),
        ((1,), (3, 4), "worse"),
        ((4, 5), (1,), "better"),
        ((1,), (1,), "none"),
    ],
)
def test_change_coloured_only_outside_run_noise(a, b, expected):
    assert _verdict(_runs(*a), _runs(*b)) == expected


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ((50, 70), (60, 80), "none"),
        ((50, 70), (70, 90), "none"),
        ((50, 52), (90, 92), "better"),
        ((90, 92), (50, 52), "worse"),
    ],
)
def test_cache_hit_change_coloured_only_outside_run_noise(a, b, expected):
    assert _verdict(_hit_runs(*a), _hit_runs(*b), row=HOT) == expected


def test_metric_reported_by_one_run_of_a_query_is_coloured_by_sign():
    sparse = [_frame(**_profile(cpu="00:00:02")), _frame(**{"ExecutionTime": "00:00:01"})]
    full = [_frame(**_profile(cpu="00:00:09")), _frame(**_profile(cpu="00:00:10"))]
    assert _verdict(sparse, full, row=CPU) == "worse"
    assert _verdict(full, sparse, row=CPU) == "better"
    assert _verdict(full, sparse, row=EXEC) == "none"


def test_bold_best_is_kept_when_ranges_overlap():
    css = _css(compare_performance(_runs(1, 3), _runs(2, 4)))
    assert "bold" in css[EXEC, ("Query 1", "Value")]
    assert "color" not in css.get((EXEC, ("Query 2", "vs Query 1")), "")


def test_each_vs_column_is_judged_against_the_baseline():
    styled = compare_performance(_runs(1, 3), _runs(2, 4), _runs(5, 6))
    css = _css(styled)
    assert "color" not in css.get((EXEC, ("Query 2", "vs Query 1")), "")
    assert WORSE_COLOR in css[EXEC, ("Query 3", "vs Query 1")]
