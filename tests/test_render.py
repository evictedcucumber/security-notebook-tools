import json
import sys
import warnings
from datetime import UTC, datetime, timedelta
from unittest import mock

import pandas as pd
import pytest
from azure.kusto.data.response import KustoResponseDataSetV2
from azure.monitor.query import LogsQueryPartialResult, LogsQueryResult, LogsTable

import security_notebook_tools as snt
from security_notebook_tools import _render
from security_notebook_tools._adx import ADXClient
from security_notebook_tools._exceptions import ConfigurationError
from security_notebook_tools._render import normalise_visualization, render_dataframe
from security_notebook_tools._sentinel import SentinelClient
from security_notebook_tools._tablepolicy import TablePolicy

go = pytest.importorskip("plotly.graph_objects")

WS = "11111111-1111-1111-1111-111111111111"
RID = (
    "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/rg"
    "/providers/Microsoft.OperationalInsights/workspaces/sentinel-ws"
)
POLICY = TablePolicy(WS, tables={"t": ("T", "Analytics")})
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _frame(spec, data=None, types=None):
    df = pd.DataFrame(
        data
        or {
            "TimeGenerated": [T0, T0 + timedelta(hours=1), T0, T0 + timedelta(hours=1)],
            "Result": ["ok", "ok", "fail", "fail"],
            "Count": [1, 2, 3, 4],
            "Other": [10, 20, 30, 40],
        }
    )
    df.attrs["visualization"] = spec
    if types:
        df.attrs["kql_types"] = types
    return df


# --- capture: normalisation ---------------------------------------------------------------------


def test_normalise_sentinel_camel_case():
    raw = {
        "visualization": "TimeChart",
        "title": "Sign-ins",
        "accumulate": False,
        "isQuerySorted": False,
        "kind": "Stacked",
        "legend": None,
        "series": "Result, App",
        "yMin": "NaN",
        "yMax": 10,
        "xAxis": None,
        "xColumn": "TimeGenerated",
        "xTitle": "",
        "yColumns": "Count,Other",
        "ySplit": None,
        "yTitle": "n",
        "anomalyColumns": None,
    }
    assert normalise_visualization(raw) == {
        "visualization": "timechart",
        "title": "Sign-ins",
        "kind": "stacked",
        "series": ["Result", "App"],
        "ymax": 10.0,
        "xcolumn": "TimeGenerated",
        "ycolumns": ["Count", "Other"],
        "ytitle": "n",
    }


def test_normalise_adx_pascal_case_json_text():
    raw = json.dumps(
        {"Visualization": "piechart", "Title": None, "YColumns": "A", "Accumulate": "true"}
    )
    assert normalise_visualization(raw) == {
        "visualization": "piechart",
        "ycolumns": ["A"],
        "accumulate": True,
    }


@pytest.mark.parametrize("raw", [None, {}, {"visualization": None}, {"visualization": " "}, "x", 3])
def test_normalise_no_render(raw):
    assert normalise_visualization(raw) is None


# --- capture: Sentinel --------------------------------------------------------------------------


@pytest.fixture
def sentinel_sdk():
    with (
        mock.patch("security_notebook_tools._sentinel.LogsQueryClient") as sdk,
        mock.patch("security_notebook_tools._sentinel.get_policy", return_value=POLICY),
    ):
        yield sdk.return_value


def _logs_table(name="T"):
    return LogsTable(
        name=name,
        columns=["Result", "Count"],
        columns_types=["string", "long"],
        rows=[["ok", 1], ["fail", 2]],
    )


def _sentinel():
    return SentinelClient(WS, credential=mock.Mock(), resource_id=RID)


RENDER = {"visualization": "piechart", "title": "T", "yColumns": "Count", "kind": None}


def test_sentinel_requests_visualization_and_stores_it(sentinel_sdk):
    sentinel_sdk.query_workspace.return_value = LogsQueryResult(
        tables=[_logs_table(), _logs_table("U")], visualization=RENDER
    )
    first, second = _sentinel().query_tables("T | render piechart")
    assert sentinel_sdk.query_workspace.call_args.kwargs["include_visualization"] is True
    assert first.attrs["visualization"] == {
        "visualization": "piechart",
        "title": "T",
        "ycolumns": ["Count"],
    }
    assert "visualization" not in second.attrs


def test_sentinel_no_render_leaves_attrs_unchanged(sentinel_sdk):
    sentinel_sdk.query_workspace.return_value = LogsQueryResult(tables=[_logs_table()])
    (df,) = _sentinel().query_tables("T")
    assert "visualization" not in df.attrs
    assert set(df.attrs) == {"kql_types"}


def test_sentinel_partial_result_keeps_visualization(sentinel_sdk):
    sentinel_sdk.query_workspace.return_value = LogsQueryPartialResult(
        partial_data=[_logs_table()], partial_error="boom", visualization=RENDER
    )
    with pytest.warns(UserWarning, match="partial"):
        (df,) = _sentinel().query_tables("T")
    assert df.attrs["visualization"]["visualization"] == "piechart"


def test_exec_query_partial_result_keeps_visualization(monkeypatch, sentinel_sdk):
    for var, value in {
        "SENTINEL_WORKSPACE_ID": WS,
        "SENTINEL_SUBSCRIPTION_ID": "22222222-2222-2222-2222-222222222222",
        "SENTINEL_RESOURCE_GROUP": "rg",
        "SENTINEL_WORKSPACE_NAME": "sentinel-ws",
    }.items():
        monkeypatch.setenv(var, value)
    sentinel_sdk.query_workspace.return_value = LogsQueryPartialResult(
        partial_data=[_logs_table()], partial_error="boom", visualization=RENDER
    )
    with (
        mock.patch("security_notebook_tools._query.get_credential"),
        pytest.warns(UserWarning, match="partial"),
    ):
        df = snt.exec_query("T | render piechart")
    assert df.attrs["partial"] is True
    assert df.attrs["visualization"]["visualization"] == "piechart"


# --- capture: ADX -------------------------------------------------------------------------------


def _columns(*cols):
    return [{"ColumnName": n, "ColumnType": t} for n, t in cols]


def _adx_response(visualization=None, table_id=1):
    frames = [{"FrameType": "DataSetHeader"}]
    rows = [] if visualization is None else [[table_id, "Visualization", visualization]]
    frames.append(
        {
            "FrameType": "DataTable",
            "TableId": 0,
            "TableKind": "QueryProperties",
            "TableName": "@ExtendedProperties",
            "Columns": _columns(("TableId", "int"), ("Key", "string"), ("Value", "dynamic")),
            "Rows": rows,
        }
    )
    for tid, name in ((1, "A"), (2, "B")):
        frames.append(
            {
                "FrameType": "DataTable",
                "TableId": tid,
                "TableKind": "PrimaryResult",
                "TableName": f"PrimaryResult{tid}",
                "Columns": _columns((name, "string"), ("N", "long")),
                "Rows": [["x", 1]],
            }
        )
    return KustoResponseDataSetV2(frames)


@pytest.fixture
def adx_sdk():
    with (
        mock.patch("security_notebook_tools._adx.KustoClient") as kc,
        mock.patch("security_notebook_tools._adx.KustoConnectionStringBuilder"),
    ):
        yield kc.return_value


def _adx():
    return ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())


def test_adx_visualization_is_mapped_by_table_id(adx_sdk):
    raw = json.dumps({"Visualization": "barchart", "Title": "t", "YColumns": "N", "Kind": None})
    adx_sdk.execute_query.return_value = _adx_response(raw, table_id=2)
    first, second = _adx().query_tables("T | render barchart")
    assert "visualization" not in first.attrs
    assert second.attrs["visualization"] == {
        "visualization": "barchart",
        "title": "t",
        "ycolumns": ["N"],
    }


def test_adx_unknown_table_id_falls_back_to_first_table(adx_sdk):
    raw = json.dumps({"Visualization": "card"})
    adx_sdk.execute_query.return_value = _adx_response(raw, table_id=99)
    first, second = _adx().query_tables("T")
    assert first.attrs["visualization"] == {"visualization": "card"}
    assert "visualization" not in second.attrs


def test_adx_without_render_has_no_key(adx_sdk):
    adx_sdk.execute_query.return_value = _adx_response(None)
    assert all("visualization" not in df.attrs for df in _adx().query_tables("T"))


def test_adx_malformed_value_is_ignored(adx_sdk):
    adx_sdk.execute_query.return_value = _adx_response("{broken")
    assert "visualization" not in _adx().query("T").attrs


# --- cache and batch ----------------------------------------------------------------------------


@pytest.fixture
def sentinel_env(monkeypatch):
    monkeypatch.setenv("SENTINEL_WORKSPACE_ID", WS)
    monkeypatch.setenv("SENTINEL_SUBSCRIPTION_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("SENTINEL_RESOURCE_GROUP", "rg")
    monkeypatch.setenv("SENTINEL_WORKSPACE_NAME", "sentinel-ws")
    with (
        mock.patch("security_notebook_tools._query.SentinelClient") as cls,
        mock.patch("security_notebook_tools._query.get_credential"),
    ):
        yield cls.return_value


def _viz_frame(spec):
    df = pd.DataFrame({"a": ["x"], "n": [1]})
    if spec is not None:
        df.attrs["visualization"] = spec
    return df


def test_cached_repeat_cannot_be_changed_through_the_returned_spec(sentinel_env):
    spec = {"visualization": "columnchart", "ycolumns": ["n"]}
    sentinel_env.query_tables.return_value = [_viz_frame(spec)]
    first = snt.exec_query("T | render columnchart")
    first.attrs["visualization"]["ycolumns"].append("evil")
    first.attrs["visualization"]["title"] = "evil"
    second = snt.exec_query("T | render columnchart")
    assert sentinel_env.query_tables.call_count == 1
    assert second.attrs["visualization"] == spec
    assert spec["ycolumns"] == ["n"]


def test_no_render_has_no_key_through_exec_query(sentinel_env):
    sentinel_env.query_tables.return_value = [_viz_frame(None)]
    assert "visualization" not in snt.exec_query("T").attrs


BATCH_START = datetime(2026, 1, 1, tzinfo=UTC)
BATCH_KQL = "T | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd"


def _batch(sentinel_env, specs):
    frames = iter([[_viz_frame(s)] for s in specs])
    sentinel_env.query_tables.side_effect = lambda *a, **k: next(frames)
    return snt.exec_batch_query(
        BATCH_KQL + " | render columnchart", BATCH_START, BATCH_START + timedelta(days=len(specs))
    )


def test_batch_keeps_visualization_when_windows_agree(sentinel_env):
    spec = {"visualization": "columnchart", "ycolumns": ["n"]}
    df = _batch(sentinel_env, [spec, dict(spec), dict(spec)])
    assert df.attrs["visualization"] == spec
    df.attrs["visualization"]["ycolumns"].append("x")
    assert spec["ycolumns"] == ["n"]


def test_batch_drops_visualization_when_windows_disagree(sentinel_env):
    spec = {"visualization": "columnchart"}
    assert "visualization" not in _batch(sentinel_env, [spec, {"visualization": "barchart"}]).attrs
    assert "visualization" not in _batch(sentinel_env, [spec, None]).attrs
    assert "visualization" not in _batch(sentinel_env, [None, None]).attrs


def test_batch_ignores_windows_that_returned_no_table(sentinel_env):
    spec = {"visualization": "columnchart"}
    frames = iter([[_viz_frame(spec)], [], [_viz_frame(spec)]])
    sentinel_env.query_tables.side_effect = lambda *a, **k: next(frames)
    df = snt.exec_batch_query(BATCH_KQL, BATCH_START, BATCH_START + timedelta(days=3))
    assert df.attrs["visualization"] == spec


# --- cost guard ---------------------------------------------------------------------------------


def test_cost_guard_accepts_render_with_properties():
    from security_notebook_tools._tablepolicy import check_sentinel_query

    check_sentinel_query(
        "T | summarize c = count() by bin(TimeGenerated, 1h), Result | render timechart with "
        '(title="x y", xcolumn=TimeGenerated, ycolumns=c, kind=stacked, legend=hidden, '
        "ymin=0, xtitle='t', series=Result)",
        POLICY,
    )
    check_sentinel_query("T | render barchart with (ycolumns=A, B, kind=stacked)", POLICY)


# --- render_dataframe ---------------------------------------------------------------------------


def _spec(chart, **props):
    return {"visualization": chart, **props}


def test_validation():
    with pytest.raises(ValueError, match="DataFrame"):
        render_dataframe([1])
    with pytest.raises(ValueError, match="debug"):
        render_dataframe(_frame(_spec("timechart")), debug=1)


def test_timechart_defaults():
    fig = render_dataframe(_frame(_spec("timechart", ycolumns=["Count"])))
    assert [t.type for t in fig.data] == ["scatter", "scatter"]
    assert {t.name for t in fig.data} == {"ok", "fail"}  # series: the string column
    assert all(t.mode == "lines" for t in fig.data)
    ok = next(t for t in fig.data if t.name == "ok")
    assert list(ok.y) == [1.0, 2.0]
    assert fig.layout.xaxis.title.text == "TimeGenerated"


def test_defaults_use_all_numeric_columns_as_y():
    fig = render_dataframe(_frame(_spec("linechart")))
    assert len(fig.data) == 4  # 2 series x (Count, Other)
    assert {t.name for t in fig.data} == {"ok: Count", "ok: Other", "fail: Count", "fail: Other"}


def test_timechart_x_is_the_first_datetime_column():
    data = {"Name": ["a", "a"], "T": [T0, T0 + timedelta(1)], "n": [1, 2]}
    fig = render_dataframe(_frame(_spec("timechart"), data))
    assert list(fig.data[0].x) == [T0.isoformat(), (T0 + timedelta(1)).isoformat()]


def test_explicit_columns_and_series():
    spec = _spec("linechart", xcolumn="Count", ycolumns=["Other"], series=["Result"])
    fig = render_dataframe(_frame(spec))
    assert sorted(t.name for t in fig.data) == ["fail", "ok"]
    assert list(next(t for t in fig.data if t.name == "ok").x) == [1, 2]


def test_lines_are_sorted_by_time():
    data = {"T": [T0 + timedelta(hours=2), T0], "n": [2, 1]}
    fig = render_dataframe(_frame(_spec("timechart"), data))
    assert list(fig.data[0].y) == [1.0, 2.0]


def test_decimal_strings_are_plotted_as_numbers():
    df = _frame(
        _spec("columnchart"),
        {"k": ["a", "b"], "d": ["1.5", "2.5"]},
        {"k": "string", "d": "decimal"},
    )
    assert list(render_dataframe(df).data[0].y) == [1.5, 2.5]


def test_areachart_unstacked_and_stacked():
    fig = render_dataframe(_frame(_spec("areachart", ycolumns=["Count"])))
    assert all(t.fill == "tozeroy" and t.stackgroup is None for t in fig.data)
    fig = render_dataframe(_frame(_spec("areachart", ycolumns=["Count"], kind="stacked")))
    assert all(t.stackgroup == "one" for t in fig.data)
    fig = render_dataframe(_frame(_spec("areachart", ycolumns=["Count"], kind="stacked100")))
    assert all(t.stackgroup == "one" and t.groupnorm == "percent" for t in fig.data)


def test_stackedareachart_stacks():
    fig = render_dataframe(_frame(_spec("stackedareachart", ycolumns=["Count"])))
    assert all(t.stackgroup == "one" for t in fig.data)
    fig = render_dataframe(_frame(_spec("stackedareachart", ycolumns=["Count"], kind="stacked100")))
    assert all(t.groupnorm == "percent" for t in fig.data)


@pytest.mark.parametrize(
    ("kind", "mode", "norm"),
    [
        (None, "group", None),
        ("default", "group", None),
        ("unstacked", "group", None),
        ("stacked", "stack", None),
        ("stacked100", "stack", "percent"),
    ],
)
def test_columnchart_kinds(kind, mode, norm):
    props = {"ycolumns": ["Count"], "xcolumn": "Result"}
    if kind:
        props["kind"] = kind
    fig = render_dataframe(_frame(_spec("columnchart", **props)))
    assert all(t.type == "bar" and t.orientation in (None, "v") for t in fig.data)
    assert fig.layout.barmode == mode
    assert fig.layout.barnorm == norm


def test_barchart_is_horizontal_with_values_on_x():
    spec = _spec("barchart", xcolumn="Result", ycolumns=["Count"], kind="stacked", xtitle="Res")
    fig = render_dataframe(_frame(spec))
    assert fig.data[0].type == "bar"
    assert fig.data[0].orientation == "h"
    assert list(fig.data[0].y) == ["ok", "ok", "fail", "fail"]
    assert list(fig.data[0].x) == [1.0, 2.0, 3.0, 4.0]
    assert fig.layout.barmode == "stack"
    assert fig.layout.yaxis.title.text == "Res"
    assert fig.layout.xaxis.title.text == "Count"


def test_piechart():
    data = {"Result": ["ok", "fail"], "Count": [3, 1]}
    fig = render_dataframe(_frame(_spec("piechart", title="T", legend="hidden"), data))
    assert [t.type for t in fig.data] == ["pie"]
    assert list(fig.data[0].labels) == ["ok", "fail"]
    assert list(fig.data[0].values) == [3.0, 1.0]
    assert fig.layout.title.text == "T"
    assert fig.layout.showlegend is False


def test_scatterchart():
    fig = render_dataframe(_frame(_spec("scatterchart", xcolumn="Count", ycolumns=["Other"])))
    assert all(t.type == "scatter" and t.mode == "markers" for t in fig.data)


def test_anomalychart_marks_anomalies():
    data = {
        "T": [T0 + timedelta(hours=i) for i in range(4)],
        "n": [1, 2, 30, 2],
        "anomalies": [0, 0, 1, 0],
    }
    fig = render_dataframe(_frame(_spec("anomalychart", anomalycolumns=["anomalies"]), data))
    line, marks = fig.data
    assert line.mode == "lines" and len(line.y) == 4
    assert marks.mode == "markers"
    assert list(marks.y) == [30.0]
    assert len(marks.x) == 1


def test_card_number_and_text():
    fig = render_dataframe(_frame(_spec("card"), {"Total": [42, 7]}))
    assert fig.data[0].type == "indicator"
    assert fig.data[0].value == 42
    fig = render_dataframe(_frame(_spec("card"), {"Who": ["<b>bob</b>"]}))
    assert not fig.data
    assert "<b>" not in fig.layout.annotations[0].text.replace("<br>", "")


def test_card_without_rows_is_rejected():
    with pytest.raises(ConfigurationError, match="no rows"):
        render_dataframe(_frame(_spec("card"), {"Total": pd.Series([], dtype="int64")}))


def test_table_chart_returns_a_table_figure():
    fig = render_dataframe(_frame(_spec("table")))
    assert fig.data[0].type == "table"
    assert list(fig.data[0].header.values) == ["TimeGenerated", "Result", "Count", "Other"]


def test_properties_are_honoured():
    spec = _spec(
        "linechart",
        title="My title",
        xtitle="when",
        ytitle="how many",
        legend="hidden",
        ymin=0,
        ymax=100,
        yaxis="log",
        ycolumns=["Count"],
    )
    layout = render_dataframe(_frame(spec)).layout
    assert layout.title.text == "My title"
    assert layout.xaxis.title.text == "when"
    assert layout.yaxis.title.text == "how many"
    assert layout.showlegend is False
    assert layout.yaxis.type == "log"


def test_log_axis_with_zero_minimum_keeps_autorange():
    spec = _spec("linechart", ycolumns=["Count"], yaxis="log", ymin=0, ymax=100)
    assert render_dataframe(_frame(spec)).layout.yaxis.range is None
    spec = _spec("linechart", ycolumns=["Count"], yaxis="log", ymin=1, ymax=100)
    assert list(render_dataframe(_frame(spec)).layout.yaxis.range) == [0, 2]


def test_linear_limits_and_accumulate():
    spec = _spec("linechart", ycolumns=["Count"], ymin=0, ymax=9, xcolumn="Other", accumulate=True)
    fig = render_dataframe(_frame(spec))
    assert list(fig.layout.yaxis.range) == [0, 9]
    assert list(next(t for t in fig.data if t.name == "ok").y) == [1.0, 3.0]


def test_does_not_modify_the_frame():
    df = _frame(_spec("timechart"))
    before = df.copy(deep=True)
    attrs = json.dumps(df.attrs, default=str)
    render_dataframe(df)
    pd.testing.assert_frame_equal(df, before)
    assert json.dumps(df.attrs, default=str) == attrs


# --- errors -------------------------------------------------------------------------------------


def test_no_visualization_attr():
    with pytest.raises(ConfigurationError, match="render"):
        render_dataframe(pd.DataFrame({"a": [1]}))


@pytest.mark.parametrize(
    "chart",
    ["pivotchart", "ladderchart", "timepivot", "3Dchart", "treemap", "sankey", "plotly", "nope"],
)
def test_unsupported_chart_types(chart):
    with pytest.raises(ConfigurationError, match="cannot draw"):
        render_dataframe(_frame(_spec(chart)))


def test_plotly_figure_json_in_data_is_never_interpreted():
    payload = json.dumps({"data": [{"type": "scatter"}], "layout": {"title": "x"}})
    df = _frame(_spec("plotly"), {"json": [payload]})
    with pytest.raises(ConfigurationError, match="cannot draw"):
        render_dataframe(df)


@pytest.mark.parametrize(
    ("props", "label"),
    [
        ({"xcolumn": "Nope"}, "xcolumn"),
        ({"ycolumns": ["Count", "Nope"]}, "ycolumns"),
        ({"series": ["Nope"]}, "series"),
        ({"anomalycolumns": ["Nope"]}, "anomalycolumns"),
    ],
)
def test_missing_named_columns(props, label):
    with pytest.raises(ConfigurationError, match=label):
        render_dataframe(_frame(_spec("anomalychart", **props)))


def test_no_numeric_column_and_duplicate_columns():
    with pytest.raises(ConfigurationError, match="numeric"):
        render_dataframe(_frame(_spec("columnchart"), {"a": ["x"], "b": ["y"]}))
    df = _frame(_spec("columnchart"))
    df.columns = ["a", "a", "b", "c"]
    with pytest.raises(ConfigurationError, match="duplicate"):
        render_dataframe(df)


def test_missing_plotly(monkeypatch):
    monkeypatch.setitem(sys.modules, "plotly", None)
    monkeypatch.setitem(sys.modules, "plotly.graph_objects", None)
    with pytest.raises(
        _render.PlotlyMissingError, match=r"security-notebook-tools\[charts\]"
    ) as ei:
        render_dataframe(_frame(_spec("timechart")))
    assert isinstance(ei.value, ConfigurationError)


# --- escaping -----------------------------------------------------------------------------------

EVIL = '<a href="javascript:alert(1)">click</a>'
BOLD = "<b>bold</b>"


def _strings(value, skip=("template",)):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in skip:
                yield from _strings(item, skip)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item, skip)
    elif hasattr(value, "tolist"):
        yield from _strings(value.tolist(), skip)


def _all_strings(fig):
    return list(_strings(fig.to_plotly_json()))


@pytest.mark.parametrize("chart", ["columnchart", "barchart", "linechart", "scatterchart"])
def test_data_column_names_and_properties_are_escaped(chart):
    data = {EVIL: [EVIL, BOLD], BOLD: [1, 2], "n2": [3, 4]}
    spec = _spec(chart, title=BOLD, xtitle=EVIL, ytitle=BOLD, xcolumn="n2", ycolumns=[BOLD])
    texts = _all_strings(render_dataframe(_frame(spec, data)))
    assert texts
    assert not [t for t in texts if "<" in t or ">" in t]
    assert "&lt;b&gt;bold&lt;/b&gt;" in texts


def test_category_values_and_series_are_escaped():
    data = {"cat": [EVIL, BOLD], "ser": [BOLD, EVIL], "n": [1, 2]}
    fig = render_dataframe(_frame(_spec("columnchart", xcolumn="cat", series=["ser"]), data))
    texts = _all_strings(fig)
    assert not [t for t in texts if "<" in t]
    assert any("&lt;a href=" in t for t in texts)
    assert {t.name for t in fig.data} == {_render._esc(BOLD), _render._esc(EVIL)}


def test_default_axis_titles_from_column_names_are_escaped():
    data = {EVIL: ["a"], BOLD: [1]}
    texts = _all_strings(render_dataframe(_frame(_spec("columnchart"), data)))
    assert not [t for t in texts if "<" in t]


def test_pie_labels_title_and_table_are_escaped():
    data = {"k": [EVIL], BOLD: [1]}
    fig = render_dataframe(_frame(_spec("piechart", title=EVIL), data))
    assert not [t for t in _all_strings(fig) if "<" in t]
    fig = render_dataframe(_frame(_spec("table", title=BOLD), {EVIL: [BOLD]}))
    assert not [t for t in _all_strings(fig) if "<" in t]


def test_ampersand_is_escaped_too():
    fig = render_dataframe(_frame(_spec("piechart"), {"k": ["a&b"], "n": [1]}))
    assert list(fig.data[0].labels) == ["a&amp;b"]


# --- logging ------------------------------------------------------------------------------------


def test_no_data_values_in_the_log(caplog):
    caplog.set_level("INFO", logger="security_notebook_tools")
    render_dataframe(
        _frame(_spec("columnchart", title="secret-title"), {"k": ["secret"], "n": [1]})
    )
    assert "render_dataframe" in caplog.text
    assert "secret" not in caplog.text


# --- %%kql --------------------------------------------------------------------------------------


@pytest.fixture
def shell(monkeypatch):
    pytest.importorskip("IPython")
    from IPython.core.interactiveshell import InteractiveShell

    from security_notebook_tools import _magic

    sh = InteractiveShell.instance()
    sh.user_ns.clear()
    snt.load_ipython_extension(sh)
    sh.frame = _frame(_spec("columnchart", xcolumn="Result", ycolumns=["Count"]))
    monkeypatch.setattr(_magic, "exec_query", lambda *a, **k: sh.frame)
    return sh


def test_magic_returns_a_figure_for_a_render(shell):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        out = shell.run_cell_magic("kql", "", "T | render columnchart")
    assert isinstance(out, go.Figure)


def test_magic_table_flag_returns_the_styler(shell):
    from pandas.io.formats.style import Styler

    assert isinstance(shell.run_cell_magic("kql", "--table", "T | render columnchart"), Styler)


def test_magic_without_render_returns_the_styler(shell):
    from pandas.io.formats.style import Styler

    shell.frame = pd.DataFrame({"a": [1]})
    assert isinstance(shell.run_cell_magic("kql", "", "T"), Styler)


def test_magic_render_table_shows_the_table_without_warning(shell):
    from pandas.io.formats.style import Styler

    shell.frame = _frame(_spec("table"))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert isinstance(shell.run_cell_magic("kql", "", "T | render table"), Styler)


def test_magic_falls_back_with_one_warning_when_plotly_is_missing(shell, monkeypatch):
    from pandas.io.formats.style import Styler

    monkeypatch.setitem(sys.modules, "plotly", None)
    monkeypatch.setitem(sys.modules, "plotly.graph_objects", None)
    with pytest.warns(UserWarning, match=r"charts\].*Showing the table") as record:
        out = shell.run_cell_magic("kql", "", "T | render columnchart")
    assert isinstance(out, Styler)
    assert len(record) == 1


def test_magic_falls_back_with_one_warning_for_an_unsupported_chart(shell):
    from pandas.io.formats.style import Styler

    shell.frame = _frame(_spec("pivotchart"))
    with pytest.warns(UserWarning, match="pivotchart.*Showing the table") as record:
        out = shell.run_cell_magic("kql", "", "T | render pivotchart")
    assert isinstance(out, Styler)
    assert len(record) == 1


def test_magic_falls_back_when_a_named_column_is_missing(shell):
    from pandas.io.formats.style import Styler

    shell.frame = _frame(_spec("columnchart", xcolumn="Nope"))
    with pytest.warns(UserWarning, match="Nope"):
        assert isinstance(shell.run_cell_magic("kql", "", "T"), Styler)


# --- review round -------------------------------------------------------------------------------

TEX = r"$\href{javascript:alert(1)}{x}$"
HOSTILE = [EVIL, BOLD, TEX, "a&b"]


@pytest.mark.parametrize(
    "chart",
    [
        "timechart",
        "linechart",
        "areachart",
        "stackedareachart",
        "columnchart",
        "barchart",
        "piechart",
        "scatterchart",
        "anomalychart",
    ],
)
def test_no_raw_markup_or_tex_in_any_string(chart):
    data = {
        TEX: HOSTILE,  # x: hostile category values and column name
        EVIL: [1, 2, 3, 4],
        "ser": [TEX, EVIL, BOLD, TEX],
        BOLD: [0, 1, 0, 0],
    }
    spec = _spec(
        chart,
        title=TEX + EVIL,
        xtitle=EVIL,
        ytitle=TEX,
        xcolumn=TEX,
        ycolumns=[EVIL],
        series=["ser"],
        anomalycolumns=[BOLD],
    )
    texts = _all_strings(render_dataframe(_frame(spec, data)))
    assert texts
    assert not [t for t in texts if "<" in t or ">" in t or "$" in t]


def test_no_raw_markup_or_tex_in_card_and_table():
    for data in ({TEX: [EVIL]}, {TEX: [None]}):
        texts = _all_strings(render_dataframe(_frame(_spec("card", title=TEX), data)))
        assert texts
        assert not [t for t in texts if "<" in t or ">" in t or "$" in t]
    fig = render_dataframe(_frame(_spec("table", title=TEX), {TEX: [EVIL, BOLD], "n": [1, 2]}))
    assert not [t for t in _all_strings(fig) if "<" in t or ">" in t or "$" in t]


def test_dollar_is_escaped_to_an_entity():
    assert _render._esc("$x$") == "&#36;x&#36;"


def test_card_null_shows_null_marker():
    fig = render_dataframe(_frame(_spec("card"), {"v": [None]}))
    assert any(a.text == "(null)" for a in fig.layout.annotations)


# robustness of the spec


@pytest.mark.parametrize(
    "raw",
    [
        {"visualization": "card", "ymax": 10**400},
        '{"Visualization": "card", "Ymax": ' + "9" * 400 + "}",
        "[" * 100000,
    ],
)
def test_normalise_survives_hostile_specs(raw):
    out = normalise_visualization(raw)
    assert out is None or "ymax" not in out


def test_set_visualization_never_raises_and_drops_the_spec():
    frames = [pd.DataFrame({"a": [1]})]
    with mock.patch.object(_render, "normalise_visualization", side_effect=RecursionError):
        _render.set_visualization(frames, {"visualization": "card"})
    assert "visualization" not in frames[0].attrs
    _render.set_visualization(frames, {"visualization": "card"})
    assert frames[0].attrs["visualization"] == {"visualization": "card"}


def test_query_still_returns_data_when_the_spec_is_unreadable(sentinel_sdk):
    sentinel_sdk.query_workspace.return_value = LogsQueryResult(
        tables=[_logs_table()], visualization={"visualization": "card", "ymax": 10**400}
    )
    (df,) = _sentinel().query_tables("T | render card")
    assert len(df) == 2
    assert df.attrs["visualization"] == {"visualization": "card"}


# re-aggregation drops the spec


def test_merge_dataframe_drops_the_spec():
    a = _viz_frame({"visualization": "columnchart"})
    b = _viz_frame({"visualization": "columnchart"})
    assert "visualization" not in snt.merge_dataframe([a, b], "a", "n").attrs
    assert "visualization" not in snt.merge_dataframe(a, "a", "n").attrs
    empty = pd.DataFrame()
    empty.attrs["visualization"] = {"visualization": "card"}
    assert "visualization" not in snt.merge_dataframe(empty, "a", "n").attrs


def test_batch_with_merge_on_drops_the_spec(sentinel_env):
    spec = {"visualization": "columnchart"}
    frames = iter([[_viz_frame(spec)], [_viz_frame(spec)]])
    sentinel_env.query_tables.side_effect = lambda *a, **k: next(frames)
    df = snt.exec_batch_query(
        BATCH_KQL, BATCH_START, BATCH_START + timedelta(days=2), merge_on="a", agg_column="n"
    )
    assert "visualization" not in df.attrs


# make-series


def _series_frame(**extra):
    hours = [(T0 + timedelta(hours=i)).isoformat() for i in range(3)]
    df = pd.DataFrame(
        {
            "Result": ["ok", "fail"],
            "TimeGenerated": [hours, hours],
            "c": [[1, 2, 3], [4, 5, 6]],
            **extra,
        }
    )
    df.attrs["kql_types"] = {
        "Result": "string",
        "TimeGenerated": "dynamic",
        "c": "dynamic",
        **dict.fromkeys(extra, "dynamic"),
    }
    df.attrs["visualization"] = _spec("timechart")
    return df


def test_make_series_arrays_are_expanded():
    fig = render_dataframe(_series_frame())
    assert {t.name for t in fig.data} == {"ok", "fail"}
    ok = next(t for t in fig.data if t.name == "ok")
    assert list(ok.y) == [1.0, 2.0, 3.0]
    assert list(ok.x) == [(T0 + timedelta(hours=i)).isoformat() for i in range(3)]
    assert fig.layout.xaxis.title.text == "TimeGenerated"


def test_make_series_datetime_object_arrays_are_expanded():
    df = _series_frame()
    df["TimeGenerated"] = [[T0 + timedelta(hours=i) for i in range(3)]] * 2
    fig = render_dataframe(df)
    assert {t.name for t in fig.data} == {"ok", "fail"}
    assert all(len(t.x) == 3 for t in fig.data)


def test_make_series_json_text_cells_and_columnchart():
    df = _series_frame()
    df["c"] = [json.dumps(v) for v in df["c"]]
    df.attrs["visualization"] = _spec("columnchart", ycolumns=["c"])
    fig = render_dataframe(df)
    assert [t.type for t in fig.data] == ["bar", "bar"]


def test_make_series_anomaly_arrays():
    df = _series_frame(anomalies=[[0, 0, 1], [0, 0, 0]])
    df.attrs["visualization"] = _spec("anomalychart", anomalycolumns=["anomalies"])
    fig = render_dataframe(df)
    marks = [t for t in fig.data if t.mode == "markers"]
    assert len(marks) == 2
    assert {m.name for m in marks} == {"ok: anomalies (anomaly)", "fail: anomalies (anomaly)"}
    ok = next(m for m in marks if m.name.startswith("ok"))
    assert list(ok.y) == [3.0]


def _wire_series_frame(chart, **props):
    # as Log Analytics returns make-series: by, aggregates, then the axis, as dynamic JSON text
    hours = json.dumps([f"2026-01-01T0{i}:00:00.0000000Z" for i in range(3)])
    df = pd.DataFrame(
        {
            "ResultType": ["0", "50126"],
            "count_": [json.dumps([1, 2, 30]), json.dumps([4, 5, 6])],
            "TimeGenerated": [hours, hours],
            "anomalies": [json.dumps([0, 0, 1]), json.dumps([0, 0, 0])],
        }
    )
    df.attrs["kql_types"] = {
        "ResultType": "string",
        "count_": "dynamic",
        "TimeGenerated": "dynamic",
        "anomalies": "dynamic",
    }
    df.attrs["visualization"] = _spec(chart, **props)
    return df


@pytest.mark.parametrize("chart", ["timechart", "linechart", "columnchart", "areachart"])
def test_make_series_axis_after_the_aggregates_is_the_x_axis(chart):
    df = _wire_series_frame(chart).drop(columns=["anomalies"])
    fig = render_dataframe(df)
    assert {t.name for t in fig.data} == {"0", "50126"}
    first = next(t for t in fig.data if t.name == "0")
    assert list(first.y) == [1.0, 2.0, 30.0]
    assert list(first.x) == [(T0 + timedelta(hours=i)).isoformat() for i in range(3)]
    assert fig.layout.xaxis.title.text == "TimeGenerated"


def test_make_series_wire_format_anomalychart():
    fig = render_dataframe(_wire_series_frame("anomalychart", anomalycolumns=["anomalies"]))
    marked = [t for t in fig.data if t.mode == "markers"]
    assert [len(t.x) for t in marked if len(t.x)] == [1]
    assert list(marked[0].y) == [30.0]


def test_make_series_on_a_numeric_axis():
    df = pd.DataFrame({"n": [json.dumps([5, 6, 7])], "bucket": [json.dumps([0, 10, 20])]})
    df.attrs["kql_types"] = {"n": "dynamic", "bucket": "dynamic"}
    df.attrs["visualization"] = _spec("linechart", xcolumn="bucket")
    fig = render_dataframe(df)
    assert list(fig.data[0].x) == [0, 10, 20]
    assert list(fig.data[0].y) == [5.0, 6.0, 7.0]


def test_make_series_unequal_lengths_are_rejected():
    df = _series_frame()
    df["c"] = [[1, 2], [4, 5, 6]]
    with pytest.raises(ConfigurationError, match="differ in length"):
        render_dataframe(df)


def test_a_lone_list_column_is_not_a_make_series():
    df = _frame(_spec("columnchart"), {"k": ["a", "b"], "n": [1, 2], "tags": [["x"], ["y"]]})
    fig = render_dataframe(df)  # not expanded: the list column just splits the series
    assert len(fig.data) == 2
    assert all(len(t.y) == 1 for t in fig.data)


# category axes, y columns, traces


def test_string_categories_are_a_category_axis():
    data = {"k": ["0", "50126", "2024-05"], "n": [1, 2, 3]}
    assert render_dataframe(_frame(_spec("columnchart"), data)).layout.xaxis.type == "category"
    fig = render_dataframe(_frame(_spec("barchart"), data))
    assert fig.layout.yaxis.type == "category"
    assert fig.layout.xaxis.type is None


def test_ycolumns_without_numeric_values_is_rejected():
    data = {"k": ["a", "b"], "n": ["x", "y"]}
    with pytest.raises(ConfigurationError, match="no numeric values"):
        render_dataframe(_frame(_spec("columnchart", ycolumns=["n"]), data))


@pytest.mark.filterwarnings("ignore:Could not infer format")
def test_bad_datetimes_do_not_break_sorting():
    data = {"T": [T0, T0 + timedelta(hours=1)], "n": [1, 2]}
    df = _frame(_spec("timechart"), data)
    df["T"] = df["T"].astype(object)
    df.loc[0, "T"] = "garbage"
    df.attrs["kql_types"] = {"T": "datetime", "n": "long"}
    render_dataframe(df)


def test_anomaly_trace_name_keeps_group_and_column():
    data = {
        "T": [T0, T0 + timedelta(hours=1)] * 2,
        "s": ["a", "a", "b", "b"],
        "n": [1, 2, 3, 4],
        "an": [0, 1, 1, 0],
    }
    fig = render_dataframe(_frame(_spec("anomalychart", anomalycolumns=["an"]), data))
    assert {"a: an (anomaly)", "b: an (anomaly)"} <= {t.name for t in fig.data}


def test_series_cap(caplog):
    n = _render.MAX_TRACES + 1
    data = {"k": [str(i) for i in range(n)], "n": list(range(n))}
    spec = _spec("linechart", xcolumn="n", series=["k"], ycolumns=["n"])
    with pytest.raises(ConfigurationError, match=r"top.*where|where.*top"):
        render_dataframe(_frame(spec, data))
    assert caplog.text.count("plotly figure: failed") == 0  # validated before the dependency call
    ok = {"k": [str(i) for i in range(_render.MAX_TRACES)], "n": list(range(_render.MAX_TRACES))}
    assert len(render_dataframe(_frame(spec, ok)).data) == _render.MAX_TRACES


def test_user_error_is_logged_once(caplog):
    caplog.set_level("DEBUG", logger="security_notebook_tools")
    with pytest.raises(ConfigurationError):
        render_dataframe(_frame(_spec("columnchart", xcolumn="Nope")))
    assert "plotly figure" not in caplog.text


# partial error carries the spec


def test_partial_error_carries_visualization(monkeypatch, sentinel_sdk):
    from security_notebook_tools._exceptions import PartialResultError

    for var, value in {
        "SENTINEL_WORKSPACE_ID": WS,
        "SENTINEL_SUBSCRIPTION_ID": "22222222-2222-2222-2222-222222222222",
        "SENTINEL_RESOURCE_GROUP": "rg",
        "SENTINEL_WORKSPACE_NAME": "sentinel-ws",
    }.items():
        monkeypatch.setenv(var, value)
    sentinel_sdk.query_workspace.return_value = LogsQueryPartialResult(
        partial_data=[_logs_table()], partial_error="boom", visualization=RENDER
    )
    with mock.patch("security_notebook_tools._query.get_credential"):
        with pytest.raises(PartialResultError) as ei:
            snt.exec_query("T | render piechart", raise_on_partial=True)
        assert ei.value.visualization["visualization"] == "piechart"
        with pytest.raises(PartialResultError) as ei:
            _sentinel().query_tables("T", raise_on_partial=True)
        assert ei.value.visualization == RENDER


# %%kql never loses the table


def test_magic_falls_back_on_any_chart_exception(shell):
    from pandas.io.formats.style import Styler

    with (
        mock.patch(
            "security_notebook_tools._magic.render_dataframe", side_effect=KeyError("secret")
        ),
        pytest.warns(UserWarning, match="KeyError") as record,
    ):
        out = shell.run_cell_magic("kql", "", "T | render columnchart")
    assert isinstance(out, Styler)
    assert len(record) == 1
    assert "secret" not in str(record[0].message)


@pytest.mark.parametrize(
    "chart", ["timechart", "columnchart", "barchart", "piechart", "card", "table"]
)
def test_charts_are_dark_by_default(chart):
    import plotly.io as pio

    fig = render_dataframe(_frame(_spec(chart)))
    assert fig.layout.template == pio.templates["plotly_dark"]


def test_a_template_the_user_chose_is_kept(monkeypatch):
    import plotly.io as pio

    monkeypatch.setattr(pio.templates, "default", "seaborn")
    fig = render_dataframe(_frame(_spec("timechart")))
    assert fig.layout.template == pio.templates["seaborn"]
