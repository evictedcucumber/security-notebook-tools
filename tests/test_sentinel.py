from datetime import UTC, datetime
from unittest import mock

import pytest
from azure.core.exceptions import HttpResponseError
from azure.monitor.query import LogsQueryPartialResult, LogsQueryResult, LogsTable

from security_notebook_tools._exceptions import PartialResultError, QueryError
from security_notebook_tools._sentinel import SentinelClient


def _table(name="T"):
    return LogsTable(
        name=name,
        columns=["TimeGenerated", "Msg"],
        columns_types=["datetime", "string"],
        rows=[["2024-01-01T00:00:00Z", "hi"], ["2024-01-02T00:00:00Z", "yo"]],
    )


@pytest.fixture
def client():
    with mock.patch("security_notebook_tools._sentinel.LogsQueryClient") as m:
        c = SentinelClient("ws", credential=mock.Mock())
        c._mock = m.return_value
        yield c


def test_success(client):
    client._mock.query_workspace.return_value = LogsQueryResult(tables=[_table(), _table("U")])
    df = client.query("T | take 2")
    assert list(df.columns) == ["TimeGenerated", "Msg"]
    assert len(df) == 2
    assert str(df["TimeGenerated"].dtype).startswith("datetime64")
    assert df["TimeGenerated"].dt.tz is not None
    kwargs = client._mock.query_workspace.call_args.kwargs
    assert kwargs["timespan"] is None
    assert len(client.query_tables("x")) == 2


def test_timespan_tuple_and_timeout(client):
    client._mock.query_workspace.return_value = LogsQueryResult(tables=[_table()])
    span = (datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
    client.query("x", span, server_timeout=60)
    kwargs = client._mock.query_workspace.call_args.kwargs
    assert kwargs["timespan"] == span
    assert kwargs["server_timeout"] == 60


def test_no_tables(client):
    client._mock.query_workspace.return_value = LogsQueryResult(tables=[])
    assert client.query("x").empty


def test_partial_warns(client):
    client._mock.query_workspace.return_value = LogsQueryPartialResult(
        partial_data=[_table()], partial_error="boom"
    )
    with pytest.warns(UserWarning, match="partial"):
        df = client.query("x")
    assert len(df) == 2


def test_partial_raises(client):
    client._mock.query_workspace.return_value = LogsQueryPartialResult(
        partial_data=[_table()], partial_error="boom"
    )
    with pytest.raises(PartialResultError) as ei:
        client.query("x", raise_on_partial=True)
    assert ei.value.partial_error == "boom"


def test_failure(client):
    client._mock.query_workspace.side_effect = HttpResponseError(message="bad kql")
    with pytest.raises(QueryError, match="bad kql"):
        client.query("x")


def test_workspace_from_env(monkeypatch):
    with mock.patch("security_notebook_tools._sentinel.LogsQueryClient"):
        monkeypatch.setenv("SENTINEL_WORKSPACE_ID", "env-ws")
        assert SentinelClient(credential=mock.Mock()).workspace_id == "env-ws"
        assert SentinelClient("arg-ws", credential=mock.Mock()).workspace_id == "arg-ws"


def test_workspace_missing():
    from security_notebook_tools._exceptions import ConfigurationError

    with (
        mock.patch("security_notebook_tools._sentinel.LogsQueryClient"),
        pytest.raises(ConfigurationError, match="SENTINEL_WORKSPACE_ID"),
    ):
        SentinelClient(credential=mock.Mock())
