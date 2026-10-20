import logging
from datetime import timedelta
from unittest import mock

import pandas as pd
import pytest

import security_notebook_tools as snt
from security_notebook_tools._exceptions import ConfigurationError

PUBLIC = {
    "disable_logging",
    "display_df",
    "drop_empty_columns",
    "enable_logging",
    "exec_query",
    "expand_column",
    "format_timestamps",
    "parse_json_columns",
    "reorder_columns",
    "summarize_df",
    "truncate_strings",
}


def test_public_surface():
    assert set(snt.__all__) == PUBLIC
    assert set(dir(snt)) == PUBLIC | {"__version__"}


@pytest.fixture
def sentinel(monkeypatch):
    monkeypatch.setenv("SENTINEL_WORKSPACE_ID", "env-ws")
    with (
        mock.patch("security_notebook_tools._query.SentinelClient") as cls,
        mock.patch("security_notebook_tools._query.get_credential") as cred,
    ):
        cls.return_value.query_tables.return_value = [pd.DataFrame({"a": [1]}), pd.DataFrame()]
        yield cls, cred


@pytest.fixture
def adx(monkeypatch):
    monkeypatch.setenv("ADX_CLUSTER_URI", "https://env")
    monkeypatch.setenv("ADX_DATABASE", "db1")
    with (
        mock.patch("security_notebook_tools._query.ADXClient") as cls,
        mock.patch("security_notebook_tools._query.get_credential") as cred,
    ):
        cls.return_value.query_tables.return_value = [pd.DataFrame({"a": [1]})]
        cls.return_value.show.return_value = pd.DataFrame({"t": ["x"]})
        yield cls, cred


def test_defaults_to_sentinel(sentinel, monkeypatch):
    cls, cred = sentinel
    monkeypatch.setenv("AZURE_TENANT_ID", "env-tenant")
    df = snt.exec_query("T")
    assert list(df.columns) == ["a"]
    cls.return_value.query_tables.assert_called_with("T", timedelta(days=1), server_timeout=300)
    snt.exec_query("T | where TimeGenerated > ago(7d)")
    cls.return_value.query_tables.assert_called_with(
        "T | where TimeGenerated > ago(7d)", None, server_timeout=300
    )
    snt.exec_query("T", timedelta(days=7), timeout=60)
    cls.return_value.query_tables.assert_called_with("T", timedelta(days=7), server_timeout=60)
    cls.assert_called_once_with("env-ws", cred.return_value)  # client reused
    cred.assert_called_once_with("env-tenant")


def test_sentinel_missing_workspace(sentinel, monkeypatch):
    monkeypatch.delenv("SENTINEL_WORKSPACE_ID")
    with pytest.raises(ConfigurationError, match="SENTINEL_WORKSPACE_ID"):
        snt.exec_query("T")


def test_adx_query_and_command(adx, monkeypatch):
    cls, _ = adx
    assert list(snt.exec_query("T", target="adx").columns) == ["a"]
    args, kwargs = cls.return_value.query_tables.call_args
    assert args == ("T", "db1")
    assert kwargs["properties"].get_option("servertimeout", None) == timedelta(seconds=300)

    monkeypatch.setenv("ADX_DATABASE", "db2")  # read per call, not pinned by the cached client
    assert list(snt.exec_query("  .show tables", target="adx", timeout=30).columns) == ["t"]
    args, kwargs = cls.return_value.show.call_args
    assert args == ("  .show tables", "db2")
    assert kwargs["properties"].get_option("servertimeout", None) == timedelta(seconds=30)
    cls.assert_called_once_with("https://env", credential=mock.ANY)


def test_adx_missing_config(adx, monkeypatch):
    monkeypatch.delenv("ADX_DATABASE")
    with pytest.raises(ConfigurationError, match="ADX_DATABASE"):
        snt.exec_query("T", target="adx")
    monkeypatch.delenv("ADX_CLUSTER_URI")
    with pytest.raises(ConfigurationError, match="ADX_CLUSTER_URI"):
        snt.exec_query("T", target="adx")


def test_invalid_arguments(adx):
    with pytest.raises(ConfigurationError, match="timespan"):
        snt.exec_query("T", timedelta(days=1), target="adx")
    with pytest.raises(ConfigurationError, match="timeout"):
        snt.exec_query("T", timeout=0)
    with pytest.raises(ConfigurationError, match="target"):
        snt.exec_query("T", target="splunk")


def test_verbose(sentinel, capsys):
    cls, _ = sentinel

    def fake_query_tables(*args, **kwargs):
        logging.getLogger("security_notebook_tools.sentinel").info("ran")
        return []

    cls.return_value.query_tables.side_effect = fake_query_tables
    assert snt.exec_query("T", verbose=True).empty
    assert "ran" in capsys.readouterr().out
    snt.exec_query("T")
    assert capsys.readouterr().out == ""
