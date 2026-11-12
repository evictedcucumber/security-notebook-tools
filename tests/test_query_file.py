from datetime import timedelta
from unittest import mock

import pandas as pd
import pytest

import security_notebook_tools as snt
from security_notebook_tools._exceptions import ConfigurationError


@pytest.fixture
def sentinel(monkeypatch):
    monkeypatch.setenv("SENTINEL_WORKSPACE_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("SENTINEL_SUBSCRIPTION_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("SENTINEL_RESOURCE_GROUP", "rg")
    monkeypatch.setenv("SENTINEL_WORKSPACE_NAME", "sentinel-ws")
    with (
        mock.patch("security_notebook_tools._query.SentinelClient") as cls,
        mock.patch("security_notebook_tools._query.get_credential"),
    ):
        cls.return_value.query_tables.return_value = [pd.DataFrame({"a": [1]}), pd.DataFrame()]
        yield cls


@pytest.mark.parametrize("as_str", [False, True])
def test_runs_file_text_unchanged(sentinel, tmp_path, as_str):
    kql = "T\n| where X == 'é'\n| take 5\n"
    file = tmp_path / "q.kql"
    file.write_bytes(kql.encode())
    df = snt.exec_query_file(str(file) if as_str else file)
    assert list(df.columns) == ["a"]
    args = sentinel.return_value.query_tables.call_args.args
    assert args[0] == kql


def test_strips_utf8_bom(sentinel, tmp_path):
    file = tmp_path / "q.kql"
    file.write_bytes(b"\xef\xbb\xbfT | take 1")
    snt.exec_query_file(file)
    assert sentinel.return_value.query_tables.call_args.args[0] == "T | take 1"


def test_uppercase_suffix_accepted(sentinel, tmp_path):
    file = tmp_path / "q.KQL"
    file.write_text("T")
    snt.exec_query_file(file)
    sentinel.return_value.query_tables.assert_called_once()


def test_forwards_arguments(tmp_path):
    file = tmp_path / "q.kql"
    file.write_text("T | take 1")
    span = timedelta(days=3)
    with mock.patch("security_notebook_tools._query.exec_query") as eq:
        result = snt.exec_query_file(
            file,
            span,
            target="adx",
            timeout=60,
            cache=False,
            raise_on_partial=True,
            debug=True,
        )
    assert result is eq.return_value
    eq.assert_called_once_with(
        "T | take 1",
        span,
        target="adx",
        timeout=60,
        cache=False,
        raise_on_partial=True,
        debug=True,
    )


def test_default_arguments_forwarded(tmp_path):
    file = tmp_path / "q.kql"
    file.write_text("T")
    with mock.patch("security_notebook_tools._query.exec_query") as eq:
        snt.exec_query_file(file)
    kwargs = eq.call_args.kwargs
    assert eq.call_args.args == ("T", None)
    assert kwargs["target"] == "sentinel"
    assert kwargs["cache"] is True
    assert kwargs["raise_on_partial"] is False
    assert kwargs["debug"] is False


@pytest.mark.parametrize("name", ["q.txt", "q", "q.kql.txt", "q.kqlx"])
def test_wrong_suffix(sentinel, tmp_path, name):
    file = tmp_path / name
    file.write_text("T")
    with pytest.raises(ConfigurationError, match=r"\.kql"):
        snt.exec_query_file(file)
    sentinel.return_value.query_tables.assert_not_called()


@pytest.mark.parametrize("bad", [1, None, b"q.kql"])
def test_non_path_type(sentinel, bad):
    with pytest.raises(ConfigurationError, match="str or os.PathLike"):
        snt.exec_query_file(bad)
    sentinel.return_value.query_tables.assert_not_called()


def test_missing_file(sentinel, tmp_path):
    with pytest.raises(ConfigurationError, match="Could not read") as info:
        snt.exec_query_file(tmp_path / "missing.kql")
    assert isinstance(info.value.__cause__, OSError)
    assert isinstance(info.value, ValueError)
    sentinel.return_value.query_tables.assert_not_called()


def test_directory(sentinel, tmp_path):
    (tmp_path / "x.kql").mkdir()
    with pytest.raises(ConfigurationError, match="Could not read") as info:
        snt.exec_query_file(tmp_path / "x.kql")
    assert isinstance(info.value.__cause__, OSError)
    sentinel.return_value.query_tables.assert_not_called()


def test_invalid_utf8(sentinel, tmp_path):
    file = tmp_path / "q.kql"
    file.write_bytes(b"T | where x == '\xff\xfe'")
    with pytest.raises(ConfigurationError, match="not valid UTF-8") as info:
        snt.exec_query_file(file)
    assert isinstance(info.value.__cause__, UnicodeDecodeError)
    sentinel.return_value.query_tables.assert_not_called()


@pytest.mark.parametrize("content", ["", "  \n\t\n", "﻿"])
def test_blank_file(sentinel, tmp_path, content):
    file = tmp_path / "q.kql"
    file.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        snt.exec_query_file(file)
    sentinel.return_value.query_tables.assert_not_called()


def test_crlf_normalised_to_lf(sentinel, tmp_path):
    file = tmp_path / "q.kql"
    file.write_bytes(b"T\r\n| take 1\r\n")
    snt.exec_query_file(file)
    assert sentinel.return_value.query_tables.call_args.args[0] == "T\n| take 1\n"
