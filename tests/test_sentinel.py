import errno
import logging
from datetime import UTC, datetime
from unittest import mock

import pytest
from azure.core.exceptions import (
    AzureError,
    ClientAuthenticationError,
    HttpResponseError,
    ODataV4Format,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity import CredentialUnavailableError
from azure.monitor.query import (
    LogsQueryError,
    LogsQueryPartialResult,
    LogsQueryResult,
    LogsTable,
)

from security_notebook_tools._exceptions import (
    ConfigurationError,
    PartialResultError,
    QueryConnectionError,
    QueryError,
    QueryThrottledError,
    QueryTransientError,
)
from security_notebook_tools._sentinel import SentinelClient
from security_notebook_tools._tablepolicy import TablePolicy

WS = "11111111-1111-1111-1111-111111111111"
RID = (
    "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/rg"
    "/providers/Microsoft.OperationalInsights/workspaces/sentinel-ws"
)
POLICY = TablePolicy(
    WS,
    tables={"t": ("T", "Analytics"), "u": ("U", "Analytics"), "aux_cl": ("Aux_CL", "Auxiliary")},
)


def _table(name="T"):
    return LogsTable(
        name=name,
        columns=["TimeGenerated", "Msg"],
        columns_types=["datetime", "string"],
        rows=[["2024-01-01T00:00:00Z", "hi"], ["2024-01-02T00:00:00Z", "yo"]],
    )


@pytest.fixture
def client():
    with (
        mock.patch("security_notebook_tools._sentinel.LogsQueryClient") as m,
        mock.patch("security_notebook_tools._sentinel.get_policy", return_value=POLICY) as gp,
    ):
        c = SentinelClient(WS, credential=mock.Mock(), resource_id=RID)
        c._mock = m.return_value
        c._get_policy = gp
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
    assert len(client.query_tables("T")) == 2


def test_timespan_tuple_and_timeout(client):
    client._mock.query_workspace.return_value = LogsQueryResult(tables=[_table()])
    span = (datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
    client.query("T", span, server_timeout=60)
    kwargs = client._mock.query_workspace.call_args.kwargs
    assert kwargs["timespan"] == span
    assert kwargs["server_timeout"] == 60


def test_no_tables(client):
    client._mock.query_workspace.return_value = LogsQueryResult(tables=[])
    assert client.query("T").empty


def test_partial_warns(client):
    client._mock.query_workspace.return_value = LogsQueryPartialResult(
        partial_data=[_table()], partial_error="boom"
    )
    with pytest.warns(UserWarning, match="partial"):
        df = client.query("T")
    assert len(df) == 2


def test_partial_raises(client):
    client._mock.query_workspace.return_value = LogsQueryPartialResult(
        partial_data=[_table()], partial_error="boom"
    )
    with pytest.raises(PartialResultError) as ei:
        client.query("T", raise_on_partial=True)
    assert ei.value.partial_error == "boom"


def test_failure(client):
    client._mock.query_workspace.side_effect = HttpResponseError(message="bad kql")
    with pytest.raises(QueryError, match="bad kql"):
        client.query("T")


def test_throttled_maps_to_query_throttled_error(client):
    response = mock.Mock(status_code=429, reason="Too Many Requests")
    client._mock.query_workspace.side_effect = HttpResponseError(
        message="Too many requests", response=response
    )
    with pytest.raises(QueryThrottledError, match="Sentinel query failed: Too many requests") as ei:
        client.query("T")
    assert isinstance(ei.value, QueryError)


@pytest.mark.parametrize("status", [None, 400, 403, 500, 503])
def test_other_http_errors_are_not_throttling(client, status):
    response = mock.Mock(status_code=status, reason="x") if status else None
    client._mock.query_workspace.side_effect = HttpResponseError(message="nope", response=response)
    with pytest.raises(QueryError, match="nope") as ei:
        client.query("T")
    assert not isinstance(ei.value, QueryThrottledError)


def _reset_error(cls):
    exc = cls("Connection aborted.")
    exc.__cause__ = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
    return exc


@pytest.mark.parametrize("cls", [ServiceResponseError, ServiceRequestError])
def test_connection_reset_maps_to_connection_error(client, cls):
    error = _reset_error(cls)
    client._mock.query_workspace.side_effect = error
    with pytest.raises(QueryConnectionError, match="reset by peer") as ei:
        client.query("T")
    assert isinstance(ei.value, QueryTransientError)
    assert "[Errno 104]" in str(ei.value)
    assert ei.value.__cause__ is error


def test_503_is_transient_but_not_throttled(client):
    response = mock.Mock(status_code=503, reason="x")
    client._mock.query_workspace.side_effect = HttpResponseError(message="down", response=response)
    with pytest.raises(QueryTransientError, match="temporarily unavailable") as ei:
        client.query("T")
    assert not isinstance(ei.value, QueryThrottledError | QueryConnectionError)


def test_403_has_a_hint(client):
    response = mock.Mock(status_code=403, reason="x")
    client._mock.query_workspace.side_effect = HttpResponseError(message="no", response=response)
    with pytest.raises(QueryError, match="no read access") as ei:
        client.query("T")
    assert not isinstance(ei.value, QueryTransientError)


def test_504_is_not_transient(client):
    response = mock.Mock(status_code=504, reason="x")
    client._mock.query_workspace.side_effect = HttpResponseError(message="late", response=response)
    with pytest.raises(QueryError, match="raise `timeout`") as ei:
        client.query("T")
    assert not isinstance(ei.value, QueryTransientError)


def test_authentication_error_is_not_a_connection_error(client):
    client._mock.query_workspace.side_effect = ClientAuthenticationError(message="expired")
    with pytest.raises(QueryError, match="az login") as ei:
        client.query("T")
    assert not isinstance(ei.value, QueryTransientError)


def test_workspace_from_env(monkeypatch):
    with mock.patch("security_notebook_tools._sentinel.LogsQueryClient"):
        monkeypatch.setenv("SENTINEL_WORKSPACE_ID", WS)
        monkeypatch.setenv("SENTINEL_SUBSCRIPTION_ID", "22222222-2222-2222-2222-222222222222")
        monkeypatch.setenv("SENTINEL_RESOURCE_GROUP", "rg")
        monkeypatch.setenv("SENTINEL_WORKSPACE_NAME", "sentinel-ws")
        c = SentinelClient(credential=mock.Mock())
        assert (c.workspace_id, c.resource_id) == (WS, RID)
        other = "22222222-2222-2222-2222-222222222222"
        assert SentinelClient(other, credential=mock.Mock()).workspace_id == other


def test_workspace_missing():
    with (
        mock.patch("security_notebook_tools._sentinel.LogsQueryClient"),
        pytest.raises(ConfigurationError, match="SENTINEL_WORKSPACE_ID"),
    ):
        SentinelClient(credential=mock.Mock(), resource_id=RID)


def test_resource_id_missing_or_invalid(monkeypatch):
    with (
        mock.patch("security_notebook_tools._sentinel.LogsQueryClient"),
        pytest.raises(ConfigurationError, match="SENTINEL_SUBSCRIPTION_ID"),
    ):
        SentinelClient(WS, credential=mock.Mock())
    monkeypatch.setenv("SENTINEL_SUBSCRIPTION_ID", "nope")
    with (
        mock.patch("security_notebook_tools._sentinel.LogsQueryClient"),
        pytest.raises(ConfigurationError, match="Invalid .SENTINEL_SUBSCRIPTION_ID"),
    ):
        SentinelClient(WS, credential=mock.Mock())


def test_policy_fetched_with_workspace_and_resource(client):
    client._mock.query_workspace.return_value = LogsQueryResult(tables=[_table()])
    client.query("T")
    args = client._get_policy.call_args.args
    assert args[:2] == (RID, WS)


def test_rejected_query_is_not_sent(client):
    with pytest.raises(ConfigurationError, match="Aux_CL"):
        client.query("Aux_CL | take 1")
    with pytest.raises(ConfigurationError, match="search"):
        client.query_tables("search *")
    client._mock.query_workspace.assert_not_called()


def test_unverifiable_tiers_do_not_send(client):
    client._get_policy.side_effect = QueryError("Could not verify Sentinel table tiers")
    with pytest.raises(QueryError, match="Could not verify"):
        client.query("T")
    client._mock.query_workspace.assert_not_called()


def test_table_to_df_records_kql_types():
    from security_notebook_tools._sentinel import _table_to_df

    table = LogsTable(
        name="T",
        columns=["A", "B", "C"],
        columns_types=["Datetime", "LONG", "string"],
        rows=[["2024-01-01T00:00:00Z", 1, "x"]],
    )
    df = _table_to_df(table)
    assert df.attrs["kql_types"] == {"A": "datetime", "B": "long", "C": "string"}
    assert df["A"].dt.tz is not None


def _coded_error(status, code, inner=None):
    exc = HttpResponseError(message="svc message", response=mock.Mock(status_code=status))
    body = {"code": code, "message": "m"}
    if inner:
        body["details"] = [{"code": inner, "message": "i"}]
    exc.error = ODataV4Format(body)
    return exc


@pytest.mark.parametrize(
    ("code", "inner", "fragment"),
    [
        ("BadArgumentError", "SyntaxError", "syntax error"),
        ("BadArgumentError", "SemanticError", "semantic error"),
        ("PathNotFoundError", None, "table or function was not found"),
        ("WorkspaceNotFoundError", None, "workspace not found"),
        ("InsufficientAccessError", None, "reader role"),
        ("GatewayTimeout", None, "timed out"),
        ("ResponsePayloadTooLarge", None, "too large"),
    ],
)
def test_service_error_code_hints(client, code, inner, fragment):
    client._mock.query_workspace.side_effect = _coded_error(400, code, inner)
    with pytest.raises(QueryError, match=fragment) as ei:
        client.query("T")
    assert f"service error code: {code}" in str(ei.value)


def test_credential_unavailable_says_to_install_and_log_in(client):
    client._mock.query_workspace.side_effect = CredentialUnavailableError("az not found")
    with pytest.raises(QueryError, match="Install the Azure CLI and run `az login`") as ei:
        client.query("T")
    assert not isinstance(ei.value, QueryTransientError)


def test_other_azure_errors_are_explained_not_unexpected(client, caplog):
    client._mock.query_workspace.side_effect = AzureError("odd")
    with pytest.raises(QueryError, match="AzureError: odd"):
        client.query("T")
    assert not any(r.exc_info for r in caplog.records)


def test_partial_result_gets_a_code_hint(client):
    client._mock.query_workspace.return_value = LogsQueryPartialResult(
        partial_data=[_table()],
        partial_error=LogsQueryError(code="ResponsePayloadTooLarge", message="big"),
    )
    with pytest.warns(UserWarning, match="partial results.*too large"):
        client.query("T")


def test_unexpected_error_is_wrapped_and_logged_with_traceback(client, caplog):
    boom = RuntimeError("boom")
    client._mock.query_workspace.side_effect = boom
    with (
        caplog.at_level(logging.ERROR, logger="security_notebook_tools"),
        pytest.raises(QueryError, match="Unexpected RuntimeError during Sentinel query") as ei,
    ):
        client.query("T")
    assert ei.value.__cause__ is boom
    records = [r for r in caplog.records if r.exc_info]
    assert records
    assert records[0].exc_info[1] is boom


def test_unexpected_error_converting_the_result(client):
    client._mock.query_workspace.return_value = LogsQueryResult(tables=[_table()])
    with (
        mock.patch("security_notebook_tools._sentinel._table_to_df", side_effect=ValueError("x")),
        pytest.raises(QueryError, match="Unexpected ValueError during converting"),
    ):
        client.query("T")


def test_unexpected_error_building_the_client():
    with (
        mock.patch("security_notebook_tools._sentinel.LogsQueryClient", side_effect=OSError("x")),
        pytest.raises(QueryError, match="Unexpected OSError during Sentinel client setup"),
    ):
        SentinelClient(WS, credential=mock.Mock(), resource_id=RID)


def test_package_errors_and_interrupts_are_not_wrapped(client):
    config = ConfigurationError("nope")
    client._mock.query_workspace.side_effect = config
    with pytest.raises(ConfigurationError) as ei:
        client.query("T")
    assert ei.value is config
    client._mock.query_workspace.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        client.query("T")


def test_cost_guard_errors_are_not_wrapped(client):
    with pytest.raises(ConfigurationError, match="Cost guard"):
        client.query("Aux_CL | take 1")
    client._mock.query_workspace.assert_not_called()
