import logging
from datetime import timedelta
from unittest import mock

import pandas as pd
import pytest

import security_notebook_tools as snt
from security_notebook_tools._exceptions import ConfigurationError

PUBLIC = {
    "disable_logging",
    "enable_logging",
    "exec_batch_query",
    "exec_query",
    "format_dataframe",
}


def test_public_surface():
    assert set(snt.__all__) == PUBLIC
    assert set(dir(snt)) == PUBLIC | {"__version__"}


RID = (
    "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/rg"
    "/providers/Microsoft.OperationalInsights/workspaces/sentinel-ws"
)


@pytest.fixture
def sentinel(monkeypatch):
    monkeypatch.setenv("SENTINEL_WORKSPACE_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("SENTINEL_SUBSCRIPTION_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("SENTINEL_RESOURCE_GROUP", "rg")
    monkeypatch.setenv("SENTINEL_WORKSPACE_NAME", "sentinel-ws")
    with (
        mock.patch("security_notebook_tools._query.SentinelClient") as cls,
        mock.patch("security_notebook_tools._query.get_credential") as cred,
    ):
        cls.return_value.query_tables.return_value = [pd.DataFrame({"a": [1]}), pd.DataFrame()]
        yield cls, cred


@pytest.fixture
def adx(monkeypatch):
    monkeypatch.setenv("ADX_CLUSTER_URI", "https://env.kusto.windows.net")
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
    cls.assert_called_once_with(
        "11111111-1111-1111-1111-111111111111",
        cred.return_value,
        resource_id=RID,
        tenant_id="env-tenant",
    )  # client reused
    cred.assert_called_once_with("env-tenant")


def test_sentinel_missing_workspace(sentinel, monkeypatch):
    monkeypatch.delenv("SENTINEL_WORKSPACE_ID")
    with pytest.raises(ConfigurationError, match="SENTINEL_WORKSPACE_ID"):
        snt.exec_query("T")


@pytest.mark.parametrize(
    "var", ["SENTINEL_SUBSCRIPTION_ID", "SENTINEL_RESOURCE_GROUP", "SENTINEL_WORKSPACE_NAME"]
)
def test_sentinel_missing_or_invalid_resource_part(sentinel, monkeypatch, var):
    cls, _ = sentinel
    monkeypatch.delenv(var)
    with pytest.raises(ConfigurationError, match=var):
        snt.exec_query("T")
    monkeypatch.setenv(var, "a/b")
    with pytest.raises(ConfigurationError, match=f"Invalid .{var}"):
        snt.exec_query("T")
    cls.assert_not_called()


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("SENTINEL_SUBSCRIPTION_ID", "33333333-3333-3333-3333-333333333333"),
        ("SENTINEL_RESOURCE_GROUP", "other-rg"),
        ("SENTINEL_WORKSPACE_NAME", "other-ws"),
    ],
)
def test_sentinel_changed_resource_var_gets_new_client(sentinel, monkeypatch, var, value):
    cls, _ = sentinel
    snt.exec_query("T")
    monkeypatch.setenv(var, value)
    snt.exec_query("T")
    assert cls.call_count == 2


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
    cls.assert_called_once_with("https://env.kusto.windows.net", credential=mock.ANY)


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


def test_adx_blocked_command_does_not_reach_service(monkeypatch):
    monkeypatch.setenv("ADX_CLUSTER_URI", "https://env.kusto.windows.net")
    monkeypatch.setenv("ADX_DATABASE", "db1")
    with (
        mock.patch("security_notebook_tools._adx.KustoClient") as kc,
        mock.patch("security_notebook_tools._adx.KustoConnectionStringBuilder"),
        mock.patch("security_notebook_tools._query.get_credential"),
    ):
        with pytest.raises(ConfigurationError, match=r"\.show"):
            snt.exec_query(".drop table X", target="adx")
        kc.return_value.execute_mgmt.assert_not_called()


def test_invalid_config_values(sentinel, adx, monkeypatch):
    monkeypatch.setenv("SENTINEL_WORKSPACE_ID", "not-a-guid")
    with pytest.raises(ConfigurationError, match="SENTINEL_WORKSPACE_ID"):
        snt.exec_query("T")
    monkeypatch.setenv("AZURE_TENANT_ID", "bad tenant")
    with pytest.raises(ConfigurationError, match="AZURE_TENANT_ID"):
        snt.exec_query("T", target="adx")
    monkeypatch.delenv("AZURE_TENANT_ID")
    monkeypatch.setenv("ADX_CLUSTER_URI", "http://env.kusto.windows.net")
    with pytest.raises(ConfigurationError, match="ADX_CLUSTER_URI"):
        snt.exec_query("T", target="adx")


# --- exec_batch_query ---------------------------------------------------------------------------

from datetime import UTC, datetime  # noqa: E402

from security_notebook_tools._exceptions import QueryError  # noqa: E402

D0 = datetime(2026, 1, 1, tzinfo=UTC)
FILTER = "T | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd"


def _sent_windows(cls):
    return [c.args[1] for c in cls.return_value.query_tables.call_args_list]


def test_batch_windows_and_prefix(sentinel):
    cls, _ = sentinel
    end = datetime(2026, 1, 3, 12, tzinfo=UTC)
    snt.exec_batch_query(FILTER, D0, end)
    calls = cls.return_value.query_tables.call_args_list
    assert _sent_windows(cls) == [
        (D0, datetime(2026, 1, 2, tzinfo=UTC)),
        (datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)),
        (datetime(2026, 1, 3, tzinfo=UTC), end),  # clipped
    ]
    assert calls[0].args[0] == (
        'let BatchStart = datetime("2026-01-01T00:00:00.000000Z"); '
        'let BatchEnd = datetime("2026-01-02T00:00:00.000000Z");\n' + FILTER
    )
    assert all(c.kwargs == {"server_timeout": 300} for c in calls)


def test_batch_timedelta_start_and_default_end(sentinel):
    cls, _ = sentinel
    fake_now = datetime(2026, 1, 10, 6, tzinfo=UTC)
    with mock.patch("security_notebook_tools._batch.datetime") as dt:
        dt.now.return_value = fake_now
        snt.exec_batch_query(FILTER, timedelta(hours=5), interval=timedelta(hours=2))
    assert _sent_windows(cls) == [
        (datetime(2026, 1, 10, 1, tzinfo=UTC), datetime(2026, 1, 10, 3, tzinfo=UTC)),
        (datetime(2026, 1, 10, 3, tzinfo=UTC), datetime(2026, 1, 10, 5, tzinfo=UTC)),
        (datetime(2026, 1, 10, 5, tzinfo=UTC), fake_now),
    ]


def test_batch_naive_datetimes_are_utc(sentinel):
    cls, _ = sentinel
    snt.exec_batch_query(FILTER, datetime(2026, 1, 1), datetime(2026, 1, 2))
    assert _sent_windows(cls) == [(D0, datetime(2026, 1, 2, tzinfo=UTC))]


def test_batch_warns_once_for_own_time_filter(sentinel, caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    snt.exec_batch_query("T | where TimeGenerated > ago(7d)", D0, D0 + timedelta(days=3))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    caplog.clear()
    snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=3))
    snt.exec_batch_query("T | take 1", D0, D0 + timedelta(days=3))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("Batch 3/3" in r.getMessage() for r in caplog.records)


def test_batch_adx_requires_both_variables(adx):
    cls, _ = adx
    for kql in [
        "T",
        "T | where Timestamp >= BatchStart",
        "T // BatchStart BatchEnd",
        "T 'BatchStart BatchEnd'",
    ]:
        with pytest.raises(ConfigurationError, match="BatchStart and BatchEnd"):
            snt.exec_batch_query(kql, D0, D0 + timedelta(days=2), target="adx")
    cls.return_value.query_tables.assert_not_called()
    kql = "T | where Timestamp >= BatchStart and Timestamp < BatchEnd"
    snt.exec_batch_query(kql, D0, D0 + timedelta(days=2), target="adx", timeout=60)
    calls = cls.return_value.query_tables.call_args_list
    assert len(calls) == 2
    assert (
        calls[1]
        .args[0]
        .startswith(
            'let BatchStart = datetime("2026-01-02T00:00:00.000000Z"); '
            'let BatchEnd = datetime("2026-01-03T00:00:00.000000Z");\n'
        )
    )


def test_batch_adx_rejects_management_command(adx):
    cls, _ = adx
    with pytest.raises(ConfigurationError, match="Management commands"):
        snt.exec_batch_query(" .show tables", D0, D0 + timedelta(days=1), target="adx")
    cls.return_value.show.assert_not_called()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"interval": timedelta(0)}, "interval"),
        ({"interval": timedelta(days=-1)}, "interval"),
        ({"timeout": 0}, "timeout"),
        ({"target": "other"}, "Unknown target"),
        ({"end": D0}, "before end"),
        ({"end": D0 - timedelta(days=1)}, "before end"),
    ],
)
def test_batch_validation(sentinel, kwargs, match):
    cls, _ = sentinel
    kwargs = {"end": D0 + timedelta(days=1), **kwargs}
    with pytest.raises(ConfigurationError, match=match):
        snt.exec_batch_query(FILTER, D0, **kwargs)
    cls.return_value.query_tables.assert_not_called()


def test_batch_timedelta_start_must_be_positive(sentinel):
    with pytest.raises(ConfigurationError, match="before end"):
        snt.exec_batch_query(FILTER, timedelta(0))


def test_batch_concat_order_types_and_index(sentinel):
    cls, _ = sentinel
    first = pd.DataFrame({"a": [1, 2]})
    first.attrs["kql_types"] = {"a": "long"}
    second = pd.DataFrame()
    third = pd.DataFrame({"a": [3], "b": ["x"]})
    third.attrs["kql_types"] = {"a": "long", "b": "string"}
    cls.return_value.query_tables.side_effect = [[first], [second], [third]]
    df = snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=3))
    assert df["a"].tolist() == [1, 2, 3]
    assert list(df.index) == [0, 1, 2]
    assert df.attrs["kql_types"] == {"a": "long", "b": "string"}


def test_batch_all_empty(sentinel):
    cls, _ = sentinel
    empty = pd.DataFrame({"a": pd.Series([], dtype="int64")})
    empty.attrs["kql_types"] = {"a": "long"}
    cls.return_value.query_tables.side_effect = [[empty], [pd.DataFrame()]]
    df = snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=2))
    assert df.empty
    assert list(df.columns) == ["a"]
    assert df.attrs["kql_types"] == {"a": "long"}
    cls.return_value.query_tables.side_effect = [[], []]
    assert snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=2)).empty


def test_batch_error_names_window(sentinel):
    cls, _ = sentinel
    boom = QueryError("bad things")
    cls.return_value.query_tables.side_effect = [[pd.DataFrame({"a": [1]})], boom]
    with pytest.raises(QueryError, match=r"window 2/3 .*2026-01-02.*bad things") as info:
        snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=3))
    assert info.value.__cause__ is boom
    assert cls.return_value.query_tables.call_count == 2  # fail fast


def test_batch_prefix_passes_cost_guard():
    from test_tablepolicy import _policy

    from security_notebook_tools._batch import _literal
    from security_notebook_tools._kql import reject_adx_constructs
    from security_notebook_tools._tablepolicy import check_sentinel_query

    prefix = f"let BatchStart = {_literal(D0)}; let BatchEnd = {_literal(D0)};\n"
    kql = prefix + "SigninLogs | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd"
    check_sentinel_query(kql, _policy())
    reject_adx_constructs(kql)
    with pytest.raises(ConfigurationError):
        check_sentinel_query(prefix + "Aux_CL | take 1", _policy())
