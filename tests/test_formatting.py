import pandas as pd

from security_notebook_tools import _formatting as f


def test_drop_empty_columns_does_not_mutate():
    df = pd.DataFrame(
        {"a": [1, 2], "n": [None, None], "s": ["", ""], "d": [{}, []], "k": ["x", ""]}
    )
    out = f.drop_empty_columns(df)
    assert list(out.columns) == ["a", "k"]
    assert list(df.columns) == ["a", "n", "s", "d", "k"]


def test_parse_json_columns():
    df = pd.DataFrame({"p": ['{"a": 1}', "[1, 2]", "not json", None], "x": [1, 2, 3, 4]})
    out = f.parse_json_columns(df)
    assert out["p"][0] == {"a": 1}
    assert out["p"][1] == [1, 2]
    assert out["p"][2] == "not json"
    assert df["p"][0] == '{"a": 1}'


def test_expand_column_preserves_index():
    df = pd.DataFrame({"id": [1, 2], "p": [{"a": 1, "b": {"c": 2}}, {"a": 3}]}, index=[10, 20])
    out = f.expand_column(df, "p")
    assert list(out.columns) == ["id", "p.a", "p.b.c"]
    assert list(out.index) == [10, 20]
    assert out.loc[10, "p.b.c"] == 2
    assert f.expand_column(df, "p", prefix="x").columns[1] == "x.a"


def test_truncate_strings():
    out = f.truncate_strings(pd.DataFrame({"a": ["abcdefghij", "ab"], "b": [1, 2]}), 5)
    assert out["a"].tolist() == ["abcd…", "ab"]


def test_format_timestamps_tz():
    df = pd.DataFrame({"t": pd.to_datetime(["2024-01-01 12:00:00"], utc=True)})
    assert f.format_timestamps(df)["t"][0] == "2024-01-01 12:00:00"
    assert f.format_timestamps(df, "%H:%M", tz="Asia/Tokyo")["t"][0] == "21:00"
    naive = pd.DataFrame({"t": pd.to_datetime(["2024-01-01 12:00:00"])})
    assert f.format_timestamps(naive, "%H:%M", tz="Asia/Tokyo")["t"][0] == "21:00"


def test_reorder_columns():
    df = pd.DataFrame(columns=["a", "b", "c"])
    assert list(f.reorder_columns(df, ["c", "zz", "a"]).columns) == ["c", "a", "b"]


def test_summarize_df():
    df = pd.DataFrame({"a": [1, 1, None], "d": [{"x": 1}, {"x": 1}, {"y": 2}]})
    s = f.summarize_df(df)
    assert s.loc["a", "nulls"] == 1
    assert s.loc["a", "unique"] == 1
    assert s.loc["d", "unique"] == 2


def test_display_df_runs(capsys):
    f.display_df(pd.DataFrame({"a": [1, 2, 3]}), max_rows=2)
    assert "a" in capsys.readouterr().out
