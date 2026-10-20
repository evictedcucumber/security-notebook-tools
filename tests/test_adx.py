from unittest import mock

import pytest
from azure.kusto.data.exceptions import KustoServiceError

from security_notebook_tools._adx import ADXClient
from security_notebook_tools._exceptions import ConfigurationError, QueryError


@pytest.fixture
def patched():
    with (
        mock.patch("security_notebook_tools._adx.KustoClient") as kc,
        mock.patch("security_notebook_tools._adx.KustoConnectionStringBuilder") as kcsb,
        mock.patch("security_notebook_tools._adx.dataframe_from_result_table") as conv,
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
    c = ADXClient("https://c", "db", credential=mock.Mock())
    assert c.query("T | take 1") == "df-t1"
    assert c.query_tables("T") == ["df-t1", "df-t2"]
    assert client.execute_query.call_args.args[0] == "db"
    assert c.query("T", database="other") == "df-t1"
    assert client.execute_query.call_args.args[0] == "other"


def test_properties_dict(patched):
    client, _, _ = patched
    client.execute_query.return_value.primary_results = []
    c = ADXClient("https://c", "db", credential=mock.Mock())
    assert c.query("T", properties={"servertimeout": "00:01:00"}).empty
    props = client.execute_query.call_args.args[2]
    assert props.get_option("servertimeout", None) == "00:01:00"


def test_show(patched):
    client, _, conv = patched
    client.execute_mgmt.return_value.primary_results = ["m"]
    conv.return_value = "df"
    assert ADXClient("https://c", "db", credential=mock.Mock()).show(".show tables") == "df"


def test_no_database(patched):
    c = ADXClient("https://c", credential=mock.Mock())
    with pytest.raises(ConfigurationError, match="database"):
        c.query("T")
    with pytest.raises(ConfigurationError):
        c.show(".show tables")


def test_service_error(patched):
    client, _, _ = patched
    client.execute_query.side_effect = KustoServiceError("bad", mock.Mock())
    client.execute_mgmt.side_effect = KustoServiceError("bad", mock.Mock())
    c = ADXClient("https://c", "db", credential=mock.Mock())
    with pytest.raises(QueryError):
        c.query("T")
    with pytest.raises(QueryError):
        c.show(".show tables")


def test_env_defaults(patched, monkeypatch):
    client, kcsb, _ = patched
    client.execute_query.return_value.primary_results = []
    monkeypatch.setenv("ADX_CLUSTER_URI", "https://env.kusto.windows.net")
    monkeypatch.setenv("ADX_DATABASE", "envdb")
    c = ADXClient(credential=mock.Mock())
    assert kcsb.with_azure_token_credential.call_args.args[0] == "https://env.kusto.windows.net"
    c.query("T")
    assert client.execute_query.call_args.args[0] == "envdb"
    assert ADXClient("https://explicit", "argdb", credential=mock.Mock()).database == "argdb"


def test_no_cluster(patched):
    with pytest.raises(ConfigurationError, match="ADX_CLUSTER_URI"):
        ADXClient(credential=mock.Mock())
