import logging

import pandas as pd
import pytest

from security_notebook_tools import merge_dataframe
from security_notebook_tools._exceptions import ConfigurationError
from security_notebook_tools._format import KQL_TYPES_ATTR


def _frame(data, types=None):
    out = pd.DataFrame(data)
    if types is not None:
        out.attrs[KQL_TYPES_ATTR] = dict(types)
    return out


@pytest.fixture
def df():
    return _frame(
        {
            "User": ["a", "a", "b", "b", "a"],
            "App": ["x", "y", "x", "x", "x"],
            "N": [1, 2, 3, 4, 5],
            "M": [10, 20, 30, 40, 50],
            "Other": list("vwxyz"),
        },
        {"User": "string", "App": "string", "N": "long", "M": "long", "Other": "string"},
    )


def _rows(out):
    return [[None if pd.isna(c) else c for c in row] for row in out.to_numpy().tolist()]


def test_sum_single_key(df):
    out = merge_dataframe(df, "User", "N")
    assert list(out.columns) == ["User", "N"]
    assert _rows(out) == [["a", 8], ["b", 7]]
    assert isinstance(out.index, pd.RangeIndex)
    assert out.attrs[KQL_TYPES_ATTR] == {"User": "string", "N": "long"}


def test_sum_several_keys(df):
    out = merge_dataframe(df, ["User", "App"], "N")
    assert list(out.columns) == ["User", "App", "N"]
    assert _rows(out) == [["a", "x", 6], ["a", "y", 2], ["b", "x", 7]]
    assert out.attrs[KQL_TYPES_ATTR] == {"User": "string", "App": "string", "N": "long"}


@pytest.mark.parametrize("wrap", [str, list, tuple])
def test_str_list_tuple_arguments(df, wrap):
    def arg(name):
        return name if wrap is str else wrap([name])

    out = merge_dataframe(df, arg("User"), arg("N"))
    assert _rows(out) == [["a", 8], ["b", 7]]


def test_multiple_agg_columns_max(df):
    out = merge_dataframe(df, "User", ("N", "M"), "max")
    assert list(out.columns) == ["User", "N", "M"]
    assert _rows(out) == [["a", 5, 50], ["b", 4, 40]]
    assert out.attrs[KQL_TYPES_ATTR] == {"User": "string", "N": "long", "M": "long"}


def test_callable_agg_func(df):
    out = merge_dataframe(df, "User", "N", lambda s: s.max() - s.min())
    assert _rows(out) == [["a", 4], ["b", 1]]
    assert out.attrs[KQL_TYPES_ATTR] == {"User": "string"}


def test_null_key_is_own_group():
    frame = _frame({"K": ["a", None, "a", None], "V": [1, 2, 3, 4]}, {"K": "string", "V": "long"})
    out = merge_dataframe(frame, "K", "V")
    assert _rows(out) == [["a", 4], [None, 6]]


@pytest.mark.parametrize(
    ("func", "expected"),
    [
        ("sum", "int"),
        ("min", "int"),
        ("max", "int"),
        ("first", "int"),
        ("last", "int"),
        ("count", "long"),
        ("nunique", "long"),
        ("size", "long"),
        ("mean", "real"),
        ("median", "real"),
        ("std", "real"),
        ("var", "real"),
        ("prod", None),
        (len, None),
    ],
)
def test_kql_types_per_function(func, expected):
    frame = _frame(
        {"K": ["a", "a", "b"], "V": [1, 2, 3]},
        {"K": "string", "V": "int"},
    )
    out = merge_dataframe(frame, "K", "V", func)
    types = out.attrs[KQL_TYPES_ATTR]
    assert types["K"] == "string"
    if expected is None:
        assert "V" not in types
    else:
        assert types["V"] == expected


def test_no_kql_types_means_no_attr():
    out = merge_dataframe(pd.DataFrame({"K": ["a"], "V": [1]}), "K", "V")
    assert KQL_TYPES_ATTR not in out.attrs


def test_list_of_frames_concatenated():
    a = _frame({"K": ["a", "b"], "V": [1, 2]}, {"K": "string", "V": "long"})
    b = _frame({"K": ["b", "c"], "V": [10, 20]}, {"K": "string", "V": "long"})
    out = merge_dataframe([a, b], "K", "V")
    assert _rows(out) == [["a", 1], ["b", 12], ["c", 20]]
    assert isinstance(out.index, pd.RangeIndex)
    assert out.attrs[KQL_TYPES_ATTR] == {"K": "string", "V": "long"}


def test_list_skips_empty_frames_and_combines_types():
    empty = _frame({"K": [], "V": []}, {"K": "string", "V": "long"})
    a = _frame({"K": ["a"], "V": [1]})
    b = _frame({"K": ["a"], "V": [2]}, {"K": "string", "V": "real"})
    out = merge_dataframe([empty, a, b], "K", "V")
    assert _rows(out) == [["a", 3]]
    # the first frame that has a column sets its type, even an empty one
    assert out.attrs[KQL_TYPES_ATTR] == {"K": "string", "V": "long"}


def test_later_frame_types_a_column_the_first_lacks():
    a = _frame({"K": ["a"], "V": [1]}, {"K": "string"})
    b = _frame({"K": ["a"], "V": [2]}, {"K": "string", "V": "long"})
    out = merge_dataframe([a, b], "K", "V")
    assert out.attrs[KQL_TYPES_ATTR] == {"K": "string", "V": "long"}


def test_tuple_of_frames_accepted():
    a = pd.DataFrame({"K": ["a"], "V": [1]})
    out = merge_dataframe((a, a), "K", "V")
    assert _rows(out) == [["a", 2]]


def test_input_not_mutated(df):
    before = df.copy(deep=True)
    attrs_before = {k: dict(v) for k, v in df.attrs.items()}
    merge_dataframe(df, "User", "N", "count")
    pd.testing.assert_frame_equal(df, before)
    assert df.attrs == attrs_before


def test_list_input_not_mutated():
    a = _frame({"K": ["a"], "V": [1]}, {"K": "string"})
    b = _frame({"K": ["a"], "V": [2]}, {"K": "string", "V": "long"})
    merge_dataframe([a, b], "K", "V")
    assert a.attrs == {KQL_TYPES_ATTR: {"K": "string"}}
    assert b.attrs == {KQL_TYPES_ATTR: {"K": "string", "V": "long"}}
    assert list(a["V"]) == [1]


def test_all_empty_list_does_not_mutate_attrs():
    a = _frame({"K": [], "V": []}, {"K": "string"})
    b = _frame({"K": [], "V": []}, {"K": "string", "V": "long"})
    out = merge_dataframe([a, b], "K", "V")
    assert out.empty
    assert list(out.columns) == ["K", "V"]
    assert a.attrs == {KQL_TYPES_ATTR: {"K": "string"}}
    assert b.attrs == {KQL_TYPES_ATTR: {"K": "string", "V": "long"}}
    assert out.attrs[KQL_TYPES_ATTR] == {"K": "string", "V": "long"}


@pytest.mark.parametrize("many", [False, True])
def test_no_columns_returns_empty_copy(many):
    frame = pd.DataFrame()
    frame.attrs["note"] = "keep"
    out = merge_dataframe([frame, pd.DataFrame()] if many else frame, "K", "V")
    assert out.empty
    assert len(out.columns) == 0
    assert out is not frame


def test_no_columns_copy_is_independent():
    frame = pd.DataFrame()
    frame.attrs["note"] = "keep"
    out = merge_dataframe(frame, "K", "V")
    out.attrs["note"] = "changed"
    assert frame.attrs["note"] == "keep"


def test_zero_rows_with_columns():
    frame = _frame({"K": [], "V": []}, {"K": "string", "V": "long"})
    out = merge_dataframe(frame, "K", "V")
    assert out.empty
    assert list(out.columns) == ["K", "V"]
    assert isinstance(out.index, pd.RangeIndex)
    assert out.attrs[KQL_TYPES_ATTR] == {"K": "string", "V": "long"}


def test_fresh_index_and_column_order(df):
    shuffled = df.iloc[::-1]
    out = merge_dataframe(shuffled, ["App", "User"], ["M", "N"])
    assert list(out.columns) == ["App", "User", "M", "N"]
    assert isinstance(out.index, pd.RangeIndex)
    assert list(out.index) == list(range(len(out)))


def test_matches_batch_aggregation_semantics(df):
    from security_notebook_tools._batch import _aggregate

    expected = _aggregate(df, ["User"], ["N"], "sum")
    out = merge_dataframe(df, "User", "N")
    pd.testing.assert_frame_equal(out, expected)
    assert out.attrs == {**expected.attrs, "partial": False}  # merge_dataframe always sets it


@pytest.mark.parametrize("bad", [None, "text", 5, {"K": [1]}, [], (), [1], ["x", None]])
def test_bad_df(bad):
    with pytest.raises(ConfigurationError, match="df must be"):
        merge_dataframe(bad, "K", "V")


def test_list_with_non_dataframe():
    with pytest.raises(ConfigurationError, match="df must be"):
        merge_dataframe([pd.DataFrame({"K": [1], "V": [1]}), "oops"], "K", "V")


@pytest.mark.parametrize(
    ("merge_on", "agg_column"),
    [
        ([], "V"),
        ("K", []),
        ("", "V"),
        ("K", [""]),
        (["K", "K"], "V"),
        ("K", ["V", "V"]),
        ("K", ["K", "V"]),
        ([1], "V"),
        ("K", [None]),
    ],
)
def test_bad_names(merge_on, agg_column):
    frame = pd.DataFrame({"K": [1], "V": [1]})
    with pytest.raises(ConfigurationError):
        merge_dataframe(frame, merge_on, agg_column)


@pytest.mark.parametrize("func", ["", 3, None])
def test_bad_agg_func(func):
    frame = pd.DataFrame({"K": [1], "V": [1]})
    with pytest.raises(ConfigurationError, match="agg_func"):
        merge_dataframe(frame, "K", "V", func)


@pytest.mark.parametrize("debug", [1, "yes", None])
def test_debug_must_be_bool(debug):
    frame = pd.DataFrame({"K": [1], "V": [1]})
    with pytest.raises(ConfigurationError, match="debug"):
        merge_dataframe(frame, "K", "V", debug=debug)


def test_debug_true_works():
    frame = pd.DataFrame({"K": [1], "V": [1]})
    assert _rows(merge_dataframe(frame, "K", "V", debug=True)) == [[1, 1]]


def test_missing_column_names_the_dataframe(df):
    with pytest.raises(ConfigurationError, match="the DataFrame") as info:
        merge_dataframe(df, "Nope", "N")
    assert "Nope" in str(info.value)
    assert "query result" not in str(info.value)


def test_pandas_failure_wrapped(df):
    with pytest.raises(ConfigurationError, match="nonsense") as info:
        merge_dataframe(df, "User", "N", "nonsense")
    assert isinstance(info.value, ValueError)


def test_configuration_error_is_value_error():
    with pytest.raises(ValueError):
        merge_dataframe("x", "K", "V")


def test_logs_info_line(df, caplog):
    with caplog.at_level(logging.INFO, logger="security_notebook_tools"):
        merge_dataframe(df, "User", "N")
    records = [r for r in caplog.records if r.name == "security_notebook_tools.merge"]
    messages = [r.getMessage() for r in records if r.levelno == logging.INFO]
    assert any(m.startswith("Merged 5 rows into 2 by ['User']") for m in messages)
    # no row values in the log
    assert not any("Other" in m or "vwxyz" in m for m in messages)


def test_merge_carries_the_partial_flag():
    a = _frame({"K": ["x"], "N": [1]})
    b = _frame({"K": ["x"], "N": [2]})
    a.attrs["partial"] = False
    b.attrs["partial"] = True
    out = merge_dataframe([a, b], "K", "N")
    assert out["N"].tolist() == [3]
    assert out.attrs["partial"] is True
    assert "partial_windows" not in out.attrs
    a.attrs["partial"] = False
    b.attrs["partial"] = False
    assert merge_dataframe([a, b], "K", "N").attrs["partial"] is False
    b.attrs["partial"] = True
    assert merge_dataframe(b, "K", "N").attrs["partial"] is True  # single-frame path
    assert merge_dataframe(_frame({"K": ["x"], "N": [1]}), "K", "N").attrs["partial"] is False
