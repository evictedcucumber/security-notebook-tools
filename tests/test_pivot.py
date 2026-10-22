import re

import pandas as pd
import pytest

from security_notebook_tools import format_dataframe, pivot_dataframe
from security_notebook_tools._exceptions import ConfigurationError
from security_notebook_tools._format import KQL_TYPES_ATTR, PIVOT_TOTALS_ATTR


@pytest.fixture
def df():
    out = pd.DataFrame(
        {
            "User": ["a", "a", "a", "b", "b", None],
            "Result": ["0", "0", "50126", "0", None, "0"],
            "App": ["x", "y", "x", "x", "x", "y"],
            "Bytes": [1, 2, 3, 4, 5, 6],
        }
    )
    out.attrs[KQL_TYPES_ATTR] = {
        "User": "string",
        "Result": "string",
        "App": "string",
        "Bytes": "long",
    }
    return out


def _rows(out):
    return [[None if pd.isna(c) else c for c in row] for row in out.to_numpy().tolist()]


def test_count(df):
    out = pivot_dataframe(df, "User", "Result")
    assert list(out.columns) == ["User", "0", "50126", "(null)"]
    assert _rows(out) == [["a", 2, 1, 0], ["b", 1, 0, 1], [None, 1, 0, 0]]
    assert isinstance(out.index, pd.RangeIndex)
    assert out.attrs[KQL_TYPES_ATTR] == {
        "User": "string",
        "0": "long",
        "50126": "long",
        "(null)": "long",
    }


def test_sum_values_and_missing_null(df):
    out = pivot_dataframe(df, "User", "Result", "Bytes")
    assert list(out.columns) == ["User", "0", "50126", "(null)"]
    assert _rows(out) == [["a", 3, 3, None], ["b", 4, None, 5], [None, 6, None, None]]
    assert out.attrs[KQL_TYPES_ATTR]["0"] == "long"


def test_fill_value(df):
    out = pivot_dataframe(df, "User", "Result", "Bytes", fill_value=0)
    assert _rows(out) == [["a", 3, 3, 0], ["b", 4, 0, 5], [None, 6, 0, 0]]


def test_mean_is_real(df):
    out = pivot_dataframe(df, "User", "App", "Bytes", agg_func="mean")
    assert out.attrs[KQL_TYPES_ATTR]["x"] == "real"
    assert out.attrs[KQL_TYPES_ATTR]["User"] == "string"
    assert _rows(out)[0] == ["a", 2.0, 2.0]


def test_callable_agg_has_no_type(df):
    out = pivot_dataframe(df, "User", "App", "Bytes", agg_func=lambda s: s.max())
    assert _rows(out)[0] == ["a", 3, 2]
    assert out.attrs[KQL_TYPES_ATTR] == {"User": "string"}


def test_multiple_values_and_columns(df):
    df["N"] = df["Bytes"] * 10
    df.attrs[KQL_TYPES_ATTR]["N"] = "long"
    out = pivot_dataframe(df, "User", ["App", "Result"], ["Bytes", "N"])
    assert list(out.columns)[:3] == ["User", "Bytes_x_0", "Bytes_x_50126"]
    assert "Bytes_x_0" in out.columns
    assert "N_y_0" in out.columns
    assert "Bytes_x_(null)" in out.columns
    assert out.loc[0, "N_x_50126"] == 30
    assert out.attrs[KQL_TYPES_ATTR]["N_x_0"] == "long"


def test_null_keys_kept(df):
    out = pivot_dataframe(df, "User", "Result")
    assert out["User"].isna().sum() == 1
    assert "(null)" in out.columns


def test_totals_count(df):
    out = pivot_dataframe(df, "User", "Result", totals=True)
    assert list(out.columns) == ["User", "0", "50126", "(null)", "Total"]
    assert _rows(out) == [
        ["a", 2, 1, 0, 3],
        ["b", 1, 0, 1, 2],
        [None, 1, 0, 0, 1],
        ["Total", 4, 1, 1, 6],
    ]
    assert out.attrs[KQL_TYPES_ATTR]["Total"] == "long"


def test_totals_mean_uses_underlying_rows(df):
    out = pivot_dataframe(df, ["User", "App"], "Result", "Bytes", agg_func="mean", totals=True)
    last = out.iloc[-1]
    assert last["User"] == "Total"
    assert pd.isna(last["App"])
    assert last["Total"] == pytest.approx(df["Bytes"].mean())
    assert last["0"] == pytest.approx(df.loc[df["Result"] == "0", "Bytes"].mean())
    assert out.loc[0, "Total"] == pytest.approx(2.0)


def test_totals_multiple_values(df):
    df["N"] = 1
    df.attrs[KQL_TYPES_ATTR]["N"] = "long"
    out = pivot_dataframe(df, "User", "App", ["Bytes", "N"], totals=True)
    assert out.iloc[-1]["Bytes_Total"] == 21
    assert out.iloc[-1]["N_Total"] == 6
    assert out.attrs[KQL_TYPES_ATTR]["N_Total"] == "long"
    assert out.attrs[PIVOT_TOTALS_ATTR]["columns"] == ["Bytes_Total", "N_Total"]


def test_totals_metadata(df):
    out = pivot_dataframe(df, ["User", "App"], "Result", totals=True)
    assert out.attrs[PIVOT_TOTALS_ATTR] == {
        "columns": ["Total"],
        "row": len(out) - 1,
        "label_column": "User",
    }
    assert out.loc[out.attrs[PIVOT_TOTALS_ATTR]["row"], "User"] == "Total"


def test_no_totals_metadata_without_totals(df):
    df.attrs[PIVOT_TOTALS_ATTR] = {"columns": ["Bytes"], "row": 0, "label_column": "User"}
    assert PIVOT_TOTALS_ATTR not in pivot_dataframe(df, "User", "Result").attrs


def test_totals_metadata_empty(df):
    out = pivot_dataframe(df.iloc[0:0], "User", "Result", totals=True)
    assert out.attrs[PIVOT_TOTALS_ATTR] == {
        "columns": ["Total"],
        "row": None,
        "label_column": "User",
    }


def test_max_columns(df):
    with pytest.raises(ConfigurationError, match=r"3 distinct keys.*max_columns=2"):
        pivot_dataframe(df, "User", "Result", max_columns=2)
    assert pivot_dataframe(df, "User", "Result", max_columns=3).shape[1] == 4
    assert pivot_dataframe(df, "User", "Result", max_columns=None).shape[1] == 4


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "3"])
def test_max_columns_invalid(df, bad):
    with pytest.raises(ConfigurationError, match="max_columns"):
        pivot_dataframe(df, "User", "Result", max_columns=bad)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"index": ""}, "index"),
        ({"index": []}, "index must not be empty"),
        ({"index": ["User", "User"]}, "repeat"),
        ({"index": 3}, "index"),
        ({"columns": "User"}, "more than once"),
        ({"values": "User"}, "more than once"),
        ({"values": "Nope"}, "available columns"),
        ({"index": "Nope"}, "available columns"),
        ({"values": "Bytes", "agg_func": ""}, "agg_func"),
        ({"values": "Bytes", "agg_func": 3}, "agg_func"),
        ({"values": "Bytes", "agg_func": "nonsense"}, "'nonsense'"),
        ({"agg_func": "sum"}, "need values"),
        ({"fill_value": 0}, "need values"),
        ({"totals": "yes"}, "totals"),
    ],
)
def test_validation(df, kwargs, match):
    args = {"index": "User", "columns": "Result"} | kwargs
    with pytest.raises(ConfigurationError, match=match):
        pivot_dataframe(df, **args)


def test_aggregation_failure_wrapped(df):
    with pytest.raises(ConfigurationError, match="'mean'"):
        pivot_dataframe(df, "User", "Result", "App", agg_func="mean")
    with pytest.raises(ConfigurationError, match="<lambda>"):
        pivot_dataframe(df, "User", "Result", "App", agg_func=lambda s: 1 / 0)


def test_df_must_be_dataframe():
    with pytest.raises(ConfigurationError, match="DataFrame"):
        pivot_dataframe([1], "a", "b")


def test_input_not_mutated(df):
    before = df.copy()
    attrs = {k: dict(v) for k, v in df.attrs.items()}
    pivot_dataframe(df, "User", "Result", "Bytes", totals=True)
    pivot_dataframe(df, "User", "Result", totals=True)
    pd.testing.assert_frame_equal(df, before)
    assert df.attrs == attrs


def test_empty(df):
    empty = df.iloc[0:0]
    out = pivot_dataframe(empty, "User", "Result")
    assert list(out.columns) == ["User"]
    assert len(out) == 0
    out = pivot_dataframe(empty, "User", "Result", "Bytes", totals=True)
    assert list(out.columns) == ["User", "Total"]
    assert out.attrs[KQL_TYPES_ATTR]["User"] == "string"


def test_header_collision_with_index(df):
    df = df.rename(columns={"Result": "R"})
    df["R2"] = "User"
    with pytest.raises(ConfigurationError, match="collide"):
        pivot_dataframe(df, "User", "R2")


def test_header_collision_between_headers():
    df = pd.DataFrame({"u": [1, 1], "a": ["x_y", "x"], "b": ["z", "y_z"]})
    with pytest.raises(ConfigurationError, match="collide"):
        pivot_dataframe(df, "u", ["a", "b"])


def test_null_label_collides_with_text():
    df = pd.DataFrame({"u": [1, 1], "c": ["(null)", None]})
    with pytest.raises(ConfigurationError, match="collide"):
        pivot_dataframe(df, "u", "c")


def test_total_collides_with_key():
    df = pd.DataFrame({"u": [1, 1], "c": ["Total", "b"]})
    with pytest.raises(ConfigurationError, match="collide"):
        pivot_dataframe(df, "u", "c", totals=True)


@pytest.mark.parametrize("totals", [False, True])
@pytest.mark.parametrize("values", [None, "Bytes", ["Bytes"]])
def test_renders_through_format_dataframe(df, totals, values):
    out = pivot_dataframe(df, "User", "Result", values, totals=totals)
    html = format_dataframe(out).to_html()
    assert "<table" in html
    assert re.search(r"<td[^>]*>a</td>", html)
