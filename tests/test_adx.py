import errno
import logging
from unittest import mock

import pandas as pd
import pytest
from azure.identity import CredentialUnavailableError
from azure.kusto.data import ClientRequestProperties
from azure.kusto.data.exceptions import (
    KustoApiError,
    KustoAuthenticationError,
    KustoClientError,
    KustoClosedError,
    KustoError,
    KustoMultiApiError,
    KustoNetworkError,
    KustoServiceError,
    KustoThrottlingError,
)

from security_notebook_tools._adx import ADXClient
from security_notebook_tools._exceptions import (
    ConfigurationError,
    QueryConnectionError,
    QueryError,
    QueryThrottledError,
    QueryTransientError,
)


@pytest.fixture
def patched():
    with (
        mock.patch("security_notebook_tools._adx.KustoClient") as kc,
        mock.patch("security_notebook_tools._adx.KustoConnectionStringBuilder") as kcsb,
        mock.patch("security_notebook_tools._adx._table_to_df") as conv,
    ):
        yield kc.return_value, kcsb, conv


def test_connection_string_uses_credential(patched):
    _, kcsb, _ = patched
    cred = mock.Mock()
    ADXClient("https://c.kusto.windows.net", "db", credential=cred)
    kcsb.with_azure_token_credential.assert_called_once_with("https://c.kusto.windows.net", cred)


def test_query(patched):
    client, _, conv = patched
    client.execute_query.return_value.primary_results = ["t1", "t2"]
    conv.side_effect = lambda t: f"df-{t}"
    c = ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())
    assert c.query("T | take 1") == "df-t1"
    assert c.query_tables("T") == ["df-t1", "df-t2"]
    assert client.execute_query.call_args.args[0] == "db"
    assert c.query("T", database="other") == "df-t1"
    assert client.execute_query.call_args.args[0] == "other"


def test_properties_dict(patched):
    client, _, _ = patched
    client.execute_query.return_value.primary_results = []
    c = ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())
    assert c.query("T", properties={"servertimeout": "00:01:00"}).empty
    props = client.execute_query.call_args.args[2]
    assert props.get_option("servertimeout", None) == "00:01:00"


def test_show(patched):
    client, _, conv = patched
    client.execute_mgmt.return_value.primary_results = ["m"]
    conv.return_value = "df"
    assert (
        ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock()).show(".show tables")
        == "df"
    )


def test_no_database(patched):
    c = ADXClient("https://c.kusto.windows.net", credential=mock.Mock())
    with pytest.raises(ConfigurationError, match="database"):
        c.query("T")
    with pytest.raises(ConfigurationError):
        c.show(".show tables")


def test_service_error(patched):
    client, _, _ = patched
    client.execute_query.side_effect = KustoServiceError("bad", mock.Mock())
    client.execute_mgmt.side_effect = KustoServiceError("bad", mock.Mock())
    c = ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())
    with pytest.raises(QueryError):
        c.query("T")
    with pytest.raises(QueryError):
        c.show(".show tables")


@pytest.mark.parametrize(
    "error",
    [
        KustoThrottlingError("Too many requests"),
        KustoServiceError("Query execution has exceeded the allowed limits: TooManyRequests"),
        KustoServiceError("Request was throttled (QueryThrottledException)"),
        KustoServiceError("the request is THROTTLED"),
    ],
)
def test_throttling_maps_to_query_throttled_error(patched, error):
    client, _, _ = patched
    client.execute_query.side_effect = error
    client.execute_mgmt.side_effect = error
    c = ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())
    with pytest.raises(QueryThrottledError, match="ADX query failed") as ei:
        c.query("T")
    assert isinstance(ei.value, QueryError)
    assert ei.value.__cause__ is error
    with pytest.raises(QueryThrottledError, match="ADX command failed"):
        c.show(".show tables")


def test_other_service_errors_are_not_throttling(patched):
    client, _, _ = patched
    client.execute_query.side_effect = KustoServiceError("Semantic error: Failed to resolve T")
    c = ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())
    with pytest.raises(QueryError) as ei:
        c.query("T")
    assert not isinstance(ei.value, QueryThrottledError)


def test_network_error_maps_to_connection_error(patched):
    client, _, _ = patched
    error = KustoNetworkError("https://c.kusto.windows.net", "id")
    error.__cause__ = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
    client.execute_query.side_effect = error
    client.execute_mgmt.side_effect = error
    c = ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())
    with pytest.raises(QueryConnectionError, match="ADX query failed.*reset by peer") as ei:
        c.query("T")
    assert isinstance(ei.value, QueryTransientError)
    assert ei.value.__cause__ is error
    with pytest.raises(QueryConnectionError, match="ADX command failed"):
        c.show(".show tables")


@pytest.mark.parametrize(
    ("status", "transient", "fragment"),
    [
        (503, True, "temporarily unavailable"),
        (403, False, "no read access"),
        (504, False, "timeout"),
    ],
)
def test_service_error_status_hint(patched, status, transient, fragment):
    client, _, _ = patched
    client.execute_query.side_effect = KustoServiceError("x", mock.Mock(status_code=status))
    c = ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())
    with pytest.raises(QueryError, match=fragment) as ei:
        c.query("T")
    assert isinstance(ei.value, QueryTransientError) is transient


def test_authentication_error_is_a_query_error(patched):
    client, _, _ = patched
    client.execute_query.side_effect = KustoAuthenticationError("az", ValueError("expired"))
    c = ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())
    with pytest.raises(QueryError, match="ADX query failed.*az login") as ei:
        c.query("T")
    assert not isinstance(ei.value, QueryTransientError)


def test_env_defaults(patched, monkeypatch):
    client, kcsb, _ = patched
    client.execute_query.return_value.primary_results = []
    monkeypatch.setenv("ADX_CLUSTER_URI", "https://env.kusto.windows.net")
    monkeypatch.setenv("ADX_DATABASE", "envdb")
    c = ADXClient(credential=mock.Mock())
    assert kcsb.with_azure_token_credential.call_args.args[0] == "https://env.kusto.windows.net"
    c.query("T")
    assert client.execute_query.call_args.args[0] == "envdb"
    assert (
        ADXClient("https://explicit.kusto.windows.net", "argdb", credential=mock.Mock()).database
        == "argdb"
    )


def test_no_cluster(patched):
    with pytest.raises(ConfigurationError, match="ADX_CLUSTER_URI"):
        ADXClient(credential=mock.Mock())


@pytest.mark.parametrize(
    "command",
    [
        ".drop table X",
        ".set-or-replace T <| Q",
        ".ingest inline into table T <| a,b",
        "  .DROP table X",
        ".showx tables",
        "show tables",
        "",
    ],
)
def test_blocked_commands(patched, command):
    client, _, _ = patched
    c = ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())
    with pytest.raises(ConfigurationError, match=r"\.show"):
        c.show(command)
    client.execute_mgmt.assert_not_called()
    client.execute_query.assert_not_called()


@pytest.mark.parametrize(
    "command", [".show tables", "  .SHOW databases", ".show", ".Show\ncluster"]
)
def test_allowed_commands(patched, command):
    client, _, _ = patched
    client.execute_mgmt.return_value.primary_results = []
    c = ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())
    assert c.show(command).empty
    client.execute_mgmt.assert_called_once()


def test_readonly_on_query_and_command(patched):
    client, _, _ = patched
    client.execute_query.return_value.primary_results = []
    client.execute_mgmt.return_value.primary_results = []
    c = ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())
    c.query("T")
    assert client.execute_query.call_args.args[2].get_option("request_readonly", None) is True
    c.show(".show tables")
    assert client.execute_mgmt.call_args.args[2].get_option("request_readonly", None) is True


def test_readonly_not_overridable(patched):
    client, _, _ = patched
    client.execute_query.return_value.primary_results = []
    client.execute_mgmt.return_value.primary_results = []
    c = ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())
    c.query("T", properties={"request_readonly": False})
    assert client.execute_query.call_args.args[2].get_option("request_readonly", None) is True
    props = ClientRequestProperties()
    props.set_option("request_readonly", False)
    c.show(".show tables", properties=props)
    assert client.execute_mgmt.call_args.args[2].get_option("request_readonly", None) is True


def test_invalid_config(patched):
    with pytest.raises(ConfigurationError, match="ADX_CLUSTER_URI"):
        ADXClient("http://c.kusto.windows.net", "db", credential=mock.Mock())
    with pytest.raises(ConfigurationError, match="ADX_DATABASE"):
        ADXClient("https://c.kusto.windows.net", " ", credential=mock.Mock())
    c = ADXClient("https://c.kusto.windows.net", credential=mock.Mock())
    with pytest.raises(ConfigurationError, match="ADX_DATABASE"):
        c.query("T", database="a\x00b")


@pytest.mark.parametrize(
    "uri",
    [
        "https://api.securityplatform.microsoft.com/lake/kql",
        "https://ade.loganalytics.io",
        "https://ade.applicationinsights.io",
    ],
)
def test_lake_and_proxy_clusters_rejected(patched, uri):
    with pytest.raises(ConfigurationError, match="ADX_CLUSTER_URI"):
        ADXClient(uri, "db", credential=mock.Mock())


@pytest.mark.parametrize(
    "kql",
    [
        'cluster("x").database("y").T',
        "T | join (cluster('https://ade.loganalytics.io/s').database('w').Aux_CL) on k",
        "external_table('T')",
        'T | union external_table("Lake")',
        "externaldata(a:string)[h@'https://x.blob.core.windows.net/c/f.csv']",
        "T | evaluate sql_request('Server=x', 'select 1')",
        "T | evaluate http_request('https://x')",
        "T | evaluate mysql_request('a', 'b')",
        "T | evaluate azure_digital_twins_query_request('a', 'b')",
        "['cluster']('https://api.securityplatform.microsoft.com/lake/kql').database('d').T",
        "T | join (['external_table']('Lake')) on k",
        "T | evaluate python(typeof(*), 'code')",
        "T | evaluate hint.x=1 r(typeof(*), 'code')",
        "T | evaluate ai_embeddings(x, 'm')",
        "T | evaluate ai_chat_completion(x, 'm')",
        "T | evaluate ai_chat_completion_prompt(x, 'm')",
        "// x\x0b```\ncluster('c').database('d').T // ```",
        "T | where a == 'x\u2028y'",
        ".show tables | evaluate http_request_post('u')",
        ".show table T | join (cluster('c').database('d').U) on k",
        "T | where a == @\"C:\\\" ; cluster('c').database('d').U | take 1 ; print @\"x\"",
    ],
)
def test_data_lake_escape_hatches_rejected_for_query_and_show(patched, kql):
    client, _, _ = patched
    c = ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())
    with pytest.raises(ConfigurationError, match="not allowed|cannot be analysed|control"):
        c.query(kql)
    if kql.startswith("."):
        with pytest.raises(ConfigurationError, match="not allowed|cannot be analysed|control"):
            c.show(kql)
    else:
        with pytest.raises(ConfigurationError, match="not allowed|cannot be analysed|control"):
            c.show(".show tables | " + kql)
    client.execute_query.assert_not_called()
    client.execute_mgmt.assert_not_called()


@pytest.mark.parametrize(
    "kql",
    [
        "T | where Msg == 'cluster(\"x\") external_table(\"T\") externaldata' // cluster('c')",
        "T | project cluster, database | take 1",
        "search 'x'",
        "database('other').T | take 1",
        "T | evaluate bag_unpack(Props)",
    ],
)
def test_ordinary_adx_queries_still_allowed(patched, kql):
    client, _, _ = patched
    client.execute_query.return_value.primary_results = []
    c = ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())
    assert c.query(kql).empty
    client.execute_query.assert_called_once()


def test_show_with_escape_hatch_text_in_a_string_is_allowed(patched):
    client, _, _ = patched
    client.execute_mgmt.return_value.primary_results = []
    c = ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())
    assert c.show(".show tables | where TableName == 'cluster(x)'").empty


def test_table_to_df_records_kql_types():
    from security_notebook_tools._adx import _table_to_df

    table = mock.Mock()
    table.columns = [
        mock.Mock(column_name="A", column_type="Long"),
        mock.Mock(column_name="B", column_type="DateTime"),
    ]
    frame = pd.DataFrame({"A": [1], "B": ["x"]})
    with mock.patch("security_notebook_tools._adx.dataframe_from_result_table") as conv:
        conv.return_value = frame
        df = _table_to_df(table)
    conv.assert_called_once_with(table)
    assert df.attrs["kql_types"] == {"A": "long", "B": "datetime"}


def _adx():
    return ADXClient("https://c.kusto.windows.net", "db", credential=mock.Mock())


@pytest.mark.parametrize(
    ("code", "fragment"),
    [
        ("BadRequest_SyntaxError", "syntax error"),
        ("BadRequest_EntityNotFound", "was not found"),
        ("BadRequest_SemanticError", "semantic error"),
        ("LimitsExceeded", "service limit"),
        ("Request_Timeout", "timed out"),
    ],
)
def test_kusto_api_error_code_hints(patched, code, fragment):
    client, _, _ = patched
    client.execute_query.side_effect = KustoApiError(
        {"error": {"code": code, "message": "m", "@message": "details"}}
    )
    with pytest.raises(QueryError, match=fragment) as ei:
        _adx().query("T")
    assert f"service error code: {code}" in str(ei.value)


def test_kusto_multi_api_error_code_hint(patched):
    client, _, _ = patched
    client.execute_query.side_effect = KustoMultiApiError(
        [
            {
                "OneApiErrors": [
                    {"error": {"code": "BadRequest_SyntaxError", "message": "m", "@message": "d"}}
                ]
            }
        ]
    )
    with pytest.raises(QueryError, match="syntax error"):
        _adx().query("T")


def test_credential_unavailable(patched):
    client, _, _ = patched
    client.execute_query.side_effect = CredentialUnavailableError("az missing")
    with pytest.raises(QueryError, match="Install the Azure CLI and run `az login`"):
        _adx().query("T")


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (KustoClosedError(), "client was closed"),
        (KustoClientError("bad"), "client side"),
        (KustoError("odd"), "KustoError: odd"),
    ],
)
def test_other_kusto_errors_are_explained(patched, caplog, error, fragment):
    client, _, _ = patched
    client.execute_query.side_effect = error
    with pytest.raises(QueryError, match=fragment):
        _adx().query("T")
    assert not any(r.exc_info for r in caplog.records)


def test_unexpected_error_is_wrapped_and_logged(patched, caplog):
    client, _, _ = patched
    boom = RuntimeError("boom")
    client.execute_query.side_effect = boom
    with (
        caplog.at_level(logging.ERROR, logger="security_notebook_tools"),
        pytest.raises(QueryError, match="Unexpected RuntimeError during ADX query") as ei,
    ):
        _adx().query("T")
    assert ei.value.__cause__ is boom
    assert [r for r in caplog.records if r.exc_info][0].exc_info[1] is boom


def test_unexpected_error_converting_and_constructing(patched):
    client, _, conv = patched
    client.execute_query.return_value = mock.Mock(primary_results=[object()])
    conv.side_effect = ValueError("x")
    with pytest.raises(QueryError, match="Unexpected ValueError during ADX query"):
        _adx().query("T")
    with (
        mock.patch("security_notebook_tools._adx.KustoClient", side_effect=OSError("x")),
        pytest.raises(QueryError, match="ADX client setup"),
    ):
        _adx()


def test_configuration_errors_and_interrupts_are_not_wrapped(patched):
    client, _, _ = patched
    with pytest.raises(ConfigurationError):
        _adx().show("drop table T")
    client.execute_query.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        _adx().query("T")


# --- logging ------------------------------------------------------------------------------------

SECRET_KQL = (
    "T | where Email == 'bob.smith@contoso.com' and Ip == '203.0.113.77' "
    '| where Note == "topsecretvalue" | take 5'
)
LEAKS = ("bob.smith", "203.0.113.77", "topsecretvalue", "Email ==", "contoso")


def test_client_creation_and_query_are_logged(patched, caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    client, _, conv = patched
    client.execute_query.return_value.primary_results = ["t1"]
    conv.return_value = pd.DataFrame({"a": [1, 2]})
    c = ADXClient("https://c.westeurope.kusto.windows.net", "db", credential=mock.Mock())
    c.query(SECRET_KQL)
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("Kusto client creation: done in ") for m in messages)
    assert "ADX client ready for c?…(len 30) (default database: db…(len 2))" in messages
    done = next(m for m in messages if m.startswith("Kusto query: done in "))
    assert "database=db…(len 2) kql=hmac:" in done
    assert any(
        m.startswith("pandas DataFrame conversion: done in ") and "rows=[2]" in m for m in messages
    )
    assert "https://" not in caplog.text


def test_known_failure_is_one_dependency_warning_and_no_error(patched, caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    client, _, _ = patched
    client.execute_query.side_effect = KustoServiceError("Bad near 'topsecretvalue' by bob@x.io")
    with pytest.raises(QueryError):
        _adx().query(SECRET_KQL)
    (record,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert "Kusto query: failed after" in record.getMessage()
    assert "KustoServiceError: Bad near '…' by <email>" in record.getMessage()


def test_unexpected_error_is_one_error_and_a_dependency_warning(patched, caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    client, _, _ = patched
    client.execute_query.side_effect = RuntimeError("boom")
    with pytest.raises(QueryError):
        _adx().query("T")
    levels = [r.levelno for r in caplog.records if r.levelno >= logging.WARNING]
    assert levels == [logging.WARNING, logging.ERROR]


def test_command_is_logged_as_a_command(patched, caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    client, _, conv = patched
    client.execute_mgmt.return_value.primary_results = ["m"]
    conv.return_value = pd.DataFrame()
    _adx().show(".show tables")
    assert "Kusto command: done in" in caplog.text


def test_kql_and_service_text_are_never_logged(patched, caplog, _log_file):
    caplog.set_level(logging.DEBUG, logger="security_notebook_tools")
    client, _, conv = patched
    echo = "near 'topsecretvalue' for bob.smith@contoso.com from 203.0.113.77"
    client.execute_query.return_value.primary_results = ["t"]
    conv.return_value = pd.DataFrame({"a": [1]})
    _adx().query(SECRET_KQL)
    for error in (KustoServiceError(echo), KustoError(echo), KustoNetworkError("u"), OSError(echo)):
        client.execute_query.side_effect = error
        with pytest.raises(QueryError):
            _adx().query(SECRET_KQL)
    # the file (redacted), and every message (caplog.text would add its own raw tracebacks)
    logged = _log_file.read_text(encoding="utf-8") + "\n".join(
        r.getMessage() for r in caplog.records
    )
    for leak in LEAKS:
        assert leak not in logged
    assert "kql=hmac:" in logged
