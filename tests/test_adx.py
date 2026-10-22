from unittest import mock

import pandas as pd
import pytest
from azure.kusto.data import ClientRequestProperties
from azure.kusto.data.exceptions import KustoServiceError, KustoThrottlingError

from security_notebook_tools._adx import ADXClient
from security_notebook_tools._exceptions import (
    ConfigurationError,
    QueryError,
    QueryThrottledError,
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
