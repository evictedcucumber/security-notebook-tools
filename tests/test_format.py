import math
import re
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from security_notebook_tools._format import KQL_TYPES_ATTR, format_dataframe


def _typed(values, kql_type, name="c"):
    df = pd.DataFrame({name: values})
    df.attrs[KQL_TYPES_ATTR] = {name: kql_type}
    return df


def _cells(df, **kwargs):
    html = format_dataframe(df, **kwargs).to_html()
    return re.findall(r"<td[^>]*>(.*?)</td>", html)


@pytest.mark.parametrize(
    ("values", "kql_type", "expected"),
    [
        ([1, -5, 10**15], "long", ["1", "-5", str(10**15)]),
        ([1.0, 2.0, float("nan")], "long", ["1", "2", ""]),
        ([1.0, 3.0], "int", ["1", "3"]),
        ([1.5, 1e-05, 100.0], "real", ["1.5", "1e-05", "100.0"]),
        ([float("inf"), float("-inf")], "real", ["Infinity", "-Infinity"]),
        (["1.2345678901234567890"], "decimal", ["1.2345678901234567890"]),
        ([True, False], "bool", ["true", "false"]),
        (["a b", "c"], "string", ["a b", "c"]),
    ],
)
def test_scalar_types(values, kql_type, expected):
    assert _cells(_typed(values, kql_type)) == expected


def test_nulls_are_empty():
    df = pd.DataFrame(
        {
            "s": ["x", None, pd.NA],
            "d": pd.to_datetime(["2024-01-01", None, None], utc=True),
            "t": pd.to_timedelta(["1s", None, None]),
            "f": [1.5, math.nan, None],
        }
    )
    cells = _cells(df)
    assert cells[0::4] == ["x", "", ""]
    assert cells[1::4][1:] == ["", ""]
    assert cells[2::4][1:] == ["", ""]
    assert cells[3::4] == ["1.5", "", ""]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (pd.Timestamp("2024-01-02T03:04:05", tz="UTC"), "2024-01-02T03:04:05Z"),
        (pd.Timestamp("2024-01-02T03:04:05.5", tz="UTC"), "2024-01-02T03:04:05.5Z"),
        (pd.Timestamp("2024-01-02T03:04:05.1234567", tz="UTC"), "2024-01-02T03:04:05.1234567Z"),
        (pd.Timestamp("2024-01-02T03:04:05.000100", tz="UTC"), "2024-01-02T03:04:05.0001Z"),
        (pd.Timestamp("2024-01-02T03:04:05"), "2024-01-02T03:04:05"),
        (pd.Timestamp("2024-01-02T03:04:05+02:00"), "2024-01-02T01:04:05Z"),
    ],
)
def test_datetime(value, expected):
    assert _cells(_typed(pd.Series([value]), "datetime")) == [expected]


def test_datetime_object_column_with_python_datetimes():
    df = _typed([datetime(2024, 1, 2, 3, 4, 5, 250000, tzinfo=UTC)], "datetime")
    assert _cells(df) == ["2024-01-02T03:04:05.25Z"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (timedelta(0), "00:00:00"),
        (timedelta(hours=1, minutes=2, seconds=3), "01:02:03"),
        (timedelta(days=2, hours=3), "2.03:00:00"),
        (timedelta(seconds=1, milliseconds=500), "00:00:01.5"),
        (timedelta(microseconds=1), "00:00:00.000001"),
        (-timedelta(days=1, minutes=5), "-1.00:05:00"),
        (pd.Timedelta(seconds=1, nanoseconds=700), "00:00:01.0000007"),
    ],
)
def test_timespan(value, expected):
    assert _cells(_typed(pd.Series([pd.Timedelta(value)]), "timespan")) == [expected]


def test_timespan_string_passthrough():
    assert _cells(_typed(["1.02:03:04"], "timespan")) == ["1.02:03:04"]


def test_dynamic():
    values = ['{"a": [1,  2], "b": null}', {"k": "v"}, [1, "x"], "not json {", '"str"']
    assert _cells(_typed(values, "dynamic")) == [
        "{&quot;a&quot;: [1, 2], &quot;b&quot;: null}",
        "{&quot;k&quot;: &quot;v&quot;}",
        "[1, &quot;x&quot;]",
        "not json {",
        "&quot;str&quot;",
    ]


def test_dynamic_single_line():
    cell = _cells(_typed(['{\n  "a": 1,\n  "b": 2\n}'], "dynamic"))[0]
    assert "\n" not in cell


def test_html_is_escaped_in_strings():
    cells = _cells(_typed(["<script>alert(1)</script>", "a&b"], "string"))
    assert cells == ["&lt;script&gt;alert(1)&lt;/script&gt;", "a&amp;b"]


def test_html_is_escaped_in_dynamic():
    html = format_dataframe(_typed([{"x": "<script>alert(1)</script>"}], "dynamic")).to_html()
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_html_is_escaped_for_untyped_and_unknown_type():
    df = pd.DataFrame({"a": ["<script>x</script>"], "b": ["<img src=x>"]})
    df.attrs[KQL_TYPES_ATTR] = {"b": "guid"}
    html = format_dataframe(df).to_html()
    assert "<script>" not in html
    assert "<img" not in html


def test_numeric_columns_right_aligned():
    df = pd.DataFrame({"n": [1], "s": ["a"], "r": [1.5], "g": [2]})
    df.attrs[KQL_TYPES_ATTR] = {"n": "long", "s": "string", "r": "real", "g": "int32"}
    html = format_dataframe(df).to_html()
    for col in (0, 2, 3):
        assert re.search(rf"\.col{col}\s*\{{[^}}]*text-align:\s*right", html) or re.search(
            rf"th\.col{col}[^{{]*\{{[^}}]*text-align:\s*right", html
        )
    assert not re.search(r"(th|td)\.col1\b[^{]*\{[^}]*right", html)
    assert "th.col0" in html
    assert "td.col0" in html


@pytest.mark.parametrize("kql_type", ["int", "int32", "int64", "long", "real", "double", "decimal"])
def test_every_numeric_type_right_aligned(kql_type):
    html = format_dataframe(_typed([1], kql_type)).to_html()
    assert re.search(r"td\.col0[^{]*\{[^}]*text-align:\s*right", html)


@pytest.mark.parametrize("kql_type", ["string", "bool", "datetime", "timespan", "dynamic", "guid"])
def test_non_numeric_types_not_right_aligned(kql_type):
    html = format_dataframe(_typed(["x"], kql_type)).to_html()
    assert not re.search(r"col0[^{]*\{[^}]*right", html)


@pytest.mark.parametrize("tag", ["th", "td"])
def test_nowrap_and_left_alignment(tag):
    html = format_dataframe(_typed(["x"], "string")).to_html()
    match = re.search(rf"#T_\w+\s+{tag} \{{([^}}]*)\}}", html)
    assert match
    assert "white-space: nowrap" in match.group(1)
    assert "text-align: left" in match.group(1)


def test_unnamed_index_hidden():
    html = format_dataframe(pd.DataFrame({"a": [1, 2]})).to_html()
    assert "row_heading" not in html
    assert "blank" not in html


def test_named_index_shown():
    df = pd.DataFrame({"a": [1, 2]}, index=pd.Index(["x", "y"], name="key"))
    html = format_dataframe(df).to_html()
    assert "row_heading" in html
    assert ">key<" in html
    assert ">x<" in html


def test_max_rows_truncates_with_caption():
    df = pd.DataFrame({"a": range(10)})
    styler = format_dataframe(df, max_rows=3)
    html = styler.to_html()
    assert _cells(df, max_rows=3) == ["0", "1", "2"]
    assert "Showing the first 3 of 10 rows." in html
    assert len(df) == 10


def test_caption_uses_thousands_separator():
    df = pd.DataFrame({"a": range(1500)})
    html = format_dataframe(df).to_html()
    assert "Showing the first 1,000 of 1,500 rows." in html
    assert len(_cells(df)) == 1000


def test_no_caption_when_everything_shown():
    df = pd.DataFrame({"a": range(3)})
    assert "Showing the first" not in format_dataframe(df, max_rows=3).to_html()
    assert "<caption" not in format_dataframe(df).to_html()


def test_max_rows_none_shows_all():
    df = pd.DataFrame({"a": range(1200)})
    html = format_dataframe(df, max_rows=None).to_html()
    assert len(re.findall(r"<td", html)) == 1200
    assert "Showing the first" not in html


@pytest.mark.parametrize("bad", [0, -1, True, False, 1.5, "10"])
def test_max_rows_validation(bad):
    with pytest.raises(ValueError, match="max_rows"):
        format_dataframe(pd.DataFrame({"a": [1]}), max_rows=bad)


def test_input_not_modified():
    df = _typed([1.0, float("nan")], "long")
    before = df.copy(deep=True)
    attrs = {k: dict(v) for k, v in df.attrs.items()}
    format_dataframe(df, max_rows=1).to_html()
    pd.testing.assert_frame_equal(df, before)
    assert df.attrs == attrs


def test_inference_fallback():
    df = pd.DataFrame(
        {
            "i": [1, 2],
            "f": [1.5, 2.0],
            "b": [True, False],
            "d": pd.to_datetime(["2024-01-01T00:00:00.0", "2024-01-01T00:00:01.5"]),
            "t": pd.to_timedelta(["1h", "2D"]),
            "o": [{"a": 1}, [1, 2]],
            "s": ["x", "y"],
        }
    )
    cells = _cells(df)
    rows = [cells[i : i + 7] for i in range(0, 14, 7)]
    assert rows[0] == [
        "1",
        "1.5",
        "true",
        "2024-01-01T00:00:00",
        "01:00:00",
        "{&quot;a&quot;: 1}",
        "x",
    ]
    assert rows[1] == ["2", "2.0", "false", "2024-01-01T00:00:01.5", "2.00:00:00", "[1, 2]", "y"]


def test_inference_object_columns():
    df = pd.DataFrame(
        {
            "i": pd.Series([1, None], dtype=object),
            "b": pd.Series([True, None], dtype=object),
            "n": pd.Series([None, None], dtype=object),
        }
    )
    assert _cells(df) == ["1", "true", "", "", "", ""]


def test_inferred_numeric_is_right_aligned():
    html = format_dataframe(pd.DataFrame({"a": [1], "b": ["x"]})).to_html()
    assert re.search(r"td\.col0[^{]*\{[^}]*text-align:\s*right", html)
    assert not re.search(r"col1[^{]*\{[^}]*right", html)


def test_partial_attrs_mix_recorded_and_inferred():
    df = pd.DataFrame({"a": [1.0], "b": [2]})
    df.attrs[KQL_TYPES_ATTR] = {"a": "long"}
    assert _cells(df) == ["1", "2"]


def test_attrs_for_absent_columns_ignored():
    df = pd.DataFrame({"a": [1]})
    df.attrs[KQL_TYPES_ATTR] = {"ghost": "bool", "a": "long"}
    assert _cells(df) == ["1"]


def test_type_names_case_insensitive():
    assert _cells(_typed([1.0], "LONG")) == ["1"]
    assert _cells(_typed([True], "Bool")) == ["true"]
    html = format_dataframe(_typed([1], "Long")).to_html()
    assert re.search(r"td\.col0[^{]*\{[^}]*text-align:\s*right", html)


def test_unknown_type_falls_back_to_str():
    assert _cells(_typed(["abc-guid"], "guid")) == ["abc-guid"]


def test_empty_dataframe():
    df = pd.DataFrame({"a": pd.Series([], dtype="int64")})
    assert _cells(df) == []


def test_returns_styler():
    from pandas.io.formats.style import Styler

    assert isinstance(format_dataframe(pd.DataFrame({"a": [1]})), Styler)
