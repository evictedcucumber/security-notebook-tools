import logging
from datetime import timedelta
from unittest import mock

import pandas as pd
import pytest

import security_notebook_tools as snt
from security_notebook_tools._exceptions import ConfigurationError

PUBLIC = {
    "clear_cache",
    "disable_logging",
    "enable_logging",
    "exec_batch_query",
    "exec_query",
    "format_dataframe",
    "pivot_dataframe",
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
    cls.return_value.query_tables.assert_called_with(
        "T", timedelta(days=1), raise_on_partial=True, server_timeout=300
    )
    snt.exec_query("T | where TimeGenerated > ago(7d)")
    cls.return_value.query_tables.assert_called_with(
        "T | where TimeGenerated > ago(7d)", None, raise_on_partial=True, server_timeout=300
    )
    snt.exec_query("T", timedelta(days=7), timeout=60)
    cls.return_value.query_tables.assert_called_with(
        "T", timedelta(days=7), raise_on_partial=True, server_timeout=60
    )
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
    assert all(c.kwargs == {"raise_on_partial": True, "server_timeout": 300} for c in calls)


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


def test_batch_adx_sends_prefixed_windows(adx):
    cls, _ = adx
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


@pytest.fixture(params=["sentinel", "adx"])
def either(request, sentinel, adx):
    """(target, query_tables mock) for each target."""
    cls, _ = sentinel if request.param == "sentinel" else adx
    return request.param, cls.return_value.query_tables


_ACCEPTED = [
    "T | where x >= BatchStart and x < BatchEnd",
    "T | where BatchStart <= x and BatchEnd > x",
    "T | where x >= BatchStart and BatchEnd > x",
    "T | where BatchStart <= x and x < BatchEnd",
    "T | where x>=BatchStart and x<BatchEnd",
    "T | where x >= BatchStart | where x < BatchEnd",
    "T | where bin(x, 1h) >= BatchStart and (x < BatchEnd)",
    FILTER + " | extend WindowStart = BatchStart, WindowEnd = BatchEnd",
    FILTER + " | summarize n = count() by bin(x, 1h), W = bin(BatchStart, 1d)",
    FILTER + " | where d <= BatchEnd - BatchStart",
    FILTER + " // where x > BatchStart or x == BatchEnd",
    FILTER + " | where s == 'x <= BatchEnd'",
    "T | where ['x'] >= BatchStart and ['x'] < BatchEnd",
]


@pytest.mark.parametrize("kql", _ACCEPTED)
def test_batch_filter_accepted(either, kql):
    target, query_tables = either
    snt.exec_batch_query(kql, D0, _days(2), target=target)
    assert query_tables.call_count == 2


_REJECTED = [
    ("T", "BatchStart"),
    ("T | where x >= BatchStart", "BatchEnd"),
    ("T | where x < BatchEnd", "BatchStart"),
    ("T | where BatchStart <= x", "BatchEnd"),
    ("T | where BatchEnd > x", "BatchStart"),
    ("T | where x > BatchStart and x < BatchEnd", r"`\.\.\. > BatchStart` is not allowed"),
    ("T | where x >= BatchStart and x <= BatchEnd", r"`\.\.\. <= BatchEnd` is not allowed"),
    ("T | where x == BatchStart and x < BatchEnd", r"`\.\.\. == BatchStart` is not allowed"),
    ("T | where x >= BatchStart and x != BatchEnd", r"`\.\.\. != BatchEnd` is not allowed"),
    ("T | where x >= BatchStart and x <> BatchEnd", r"`\.\.\. <> BatchEnd` is not allowed"),
    ("T | where x >= BatchEnd and x < BatchEnd", r"`\.\.\. >= BatchEnd` is not allowed"),
    ("T | where x < BatchStart and x < BatchEnd", r"`\.\.\. < BatchStart` is not allowed"),
    ("T | where BatchEnd >= x and x >= BatchStart", r"`BatchEnd >= \.\.\.` is not allowed"),
    ("T | where BatchStart < x and x < BatchEnd", r"`BatchStart < \.\.\.` is not allowed"),
    ("T | where BatchStart > x and x < BatchEnd", r"`BatchStart > \.\.\.` is not allowed"),
    ("T | where BatchEnd < x and x >= BatchStart", r"`BatchEnd < \.\.\.` is not allowed"),
    ("T | where BatchEnd == x and x >= BatchStart", r"`BatchEnd == \.\.\.` is not allowed"),
    ("T | where x between (BatchStart .. BatchEnd)", "`between` includes both ends"),
    ("T | where x !between (BatchStart .. BatchEnd)", "`between` includes both ends"),
    (FILTER + " | where x between (BatchStart .. 1d)", "`between` includes both ends"),
    ("let BatchStart = ago(1d); " + FILTER, "defines BatchStart itself"),
    ("let BatchEnd = now(); " + FILTER, "defines BatchEnd itself"),
    (FILTER + " | extend BatchEnd = now()", "defines BatchEnd itself"),
    ("let f = (BatchStart: datetime) { 1 }; " + FILTER, "defines BatchStart itself"),
    ("T // x >= BatchStart and x < BatchEnd", "BatchStart"),
    ("T | where s == 'x >= BatchStart and x < BatchEnd'", "BatchStart"),
    ('T | where s == "x >= BatchStart" and x < BatchEnd', "BatchStart"),
    ("T | where x >= BatchStart - 1h and x < BatchEnd", "BatchStart"),
    ("T | extend a = BatchStart, b = BatchEnd", "BatchStart"),
]


@pytest.mark.parametrize(("kql", "match"), _REJECTED)
def test_batch_filter_rejected(either, kql, match):
    target, query_tables = either
    with pytest.raises(ConfigurationError, match=match):
        snt.exec_batch_query(kql, D0, _days(2), target=target)
    query_tables.assert_not_called()


def test_batch_filter_error_shows_expected_form(either):
    target, _ = either
    with pytest.raises(ConfigurationError) as info:
        snt.exec_batch_query("T", D0, _days(2), target=target)
    assert "filter with `TimeGenerated >= BatchStart and TimeGenerated < BatchEnd`" in str(
        info.value
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


# --- exec_batch_query: parallel windows and throttling retries ----------------------------------

import errno  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from security_notebook_tools import _batch  # noqa: E402
from security_notebook_tools._exceptions import (  # noqa: E402
    QueryConnectionError,
    QueryThrottledError,
    QueryTransientError,
)


def _window_no(timespan) -> int:
    """The 1-based window number of a (start, end) timespan sent by the batch."""
    return (timespan[0] - D0).days + 1


def _days(n):
    return D0 + timedelta(days=n)


@pytest.fixture
def backoff():
    """Patch the retry wait and jitter: no real sleeping, jitter is half the cap."""
    sleeps, caps = [], []

    def uniform(low, high):
        caps.append(high)
        return high / 2

    def fake_sleep(delay, stop):
        sleeps.append(delay)
        return False

    with (
        mock.patch.object(_batch, "_sleep", side_effect=fake_sleep),
        mock.patch.object(_batch, "random") as rnd,
    ):
        rnd.uniform.side_effect = uniform
        yield sleeps, caps


def test_batch_max_workers_is_never_exceeded(sentinel):
    cls, _ = sentinel
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=10)  # proves 3 windows really run at once
    state = {"now": 0, "peak": 0}

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        if _window_no(timespan) > 1:  # window 1 runs alone, before the pool starts
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            barrier.wait()
            time.sleep(0.01)
            with lock:
                state["now"] -= 1
        return [pd.DataFrame({"a": [_window_no(timespan)]})]

    cls.return_value.query_tables.side_effect = query
    df = snt.exec_batch_query(FILTER, D0, _days(7), max_workers=3)
    assert state["peak"] == 3
    assert df["a"].tolist() == [1, 2, 3, 4, 5, 6, 7]


def test_batch_first_window_runs_alone_on_the_calling_thread(sentinel):
    cls, _ = sentinel
    events = []

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        n = _window_no(timespan)
        events.append(("start", n, threading.current_thread()))
        time.sleep(0.02)
        events.append(("end", n, threading.current_thread()))
        return [pd.DataFrame({"a": [n]})]

    cls.return_value.query_tables.side_effect = query
    snt.exec_batch_query(FILTER, D0, _days(5), max_workers=4)
    assert [e[:2] for e in events[:2]] == [("start", 1), ("end", 1)]
    assert events[0][2] is threading.main_thread() is events[1][2]
    assert {e[2] for e in events[2:]} != {threading.main_thread()}  # the rest ran in the pool


def test_batch_results_in_window_order_when_completion_is_reversed(sentinel):
    cls, _ = sentinel
    done = {n: threading.Event() for n in range(1, 5)}

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        n = _window_no(timespan)
        if n in (2, 3):  # finish after the window behind them
            assert done[n + 1].wait(10)
        done[n].set()
        return [pd.DataFrame({"a": [n]})]

    cls.return_value.query_tables.side_effect = query
    df = snt.exec_batch_query(FILTER, D0, _days(4), max_workers=3)
    assert df["a"].tolist() == [1, 2, 3, 4]
    assert df.index.tolist() == [0, 1, 2, 3]


def test_batch_default_is_sequential(sentinel):
    cls, _ = sentinel
    threads = set()

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        threads.add(threading.current_thread())
        return [pd.DataFrame({"a": [1]})]

    cls.return_value.query_tables.side_effect = query
    snt.exec_batch_query(FILTER, D0, _days(4))
    assert threads == {threading.main_thread()}


def test_batch_logs_max_workers(sentinel, caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    snt.exec_batch_query(FILTER, D0, _days(2), max_workers=2)
    assert any("max_workers=2" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("bad", [0, -1, 6, True, False, 1.5, "2", None])
def test_batch_max_workers_validation(sentinel, bad):
    cls, _ = sentinel
    with pytest.raises(ConfigurationError, match="max_workers"):
        snt.exec_batch_query(FILTER, D0, _days(2), max_workers=bad)
    cls.return_value.query_tables.assert_not_called()


@pytest.mark.parametrize("ok", [1, 2, 5])
def test_batch_max_workers_accepted(sentinel, ok):
    assert not snt.exec_batch_query(FILTER, D0, _days(3), max_workers=ok).empty


@pytest.mark.parametrize("workers", [1, 3])
def test_batch_throttled_window_is_retried(sentinel, backoff, caplog, workers):
    cls, _ = sentinel
    sleeps, caps = backoff
    throttled = []

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        n = _window_no(timespan)
        if n == 2 and len(throttled) < 2:
            throttled.append(n)
            raise QueryThrottledError("Sentinel query failed: slow down")
        return [pd.DataFrame({"a": [n]})]

    cls.return_value.query_tables.side_effect = query
    caplog.set_level(logging.WARNING, logger="security_notebook_tools")
    df = snt.exec_batch_query(FILTER, D0, _days(3), max_workers=workers)
    assert df["a"].tolist() == [1, 2, 3]
    assert caps == [2, 4]
    assert sleeps == [1.0, 2.0]  # jittered values, within each cap
    warnings_ = [r.getMessage() for r in caplog.records if "throttled" in r.getMessage()]
    assert len(warnings_) == 2
    assert "Batch 2/3 throttled; retrying in 1.0 s (attempt 1/4)" in warnings_[0]
    assert "(attempt 2/4)" in warnings_[1]


def test_batch_throttled_first_window_is_retried(sentinel, backoff):
    cls, _ = sentinel
    ok = [pd.DataFrame({"a": [1]})]
    cls.return_value.query_tables.side_effect = [QueryThrottledError("slow"), ok]
    assert snt.exec_batch_query(FILTER, D0, _days(1)).shape == (1, 1)
    assert backoff[0] == [1.0]


def test_batch_retries_are_exhausted(sentinel, backoff):
    cls, _ = sentinel
    sleeps, caps = backoff
    boom = QueryThrottledError("Sentinel query failed: 429")
    cls.return_value.query_tables.side_effect = boom
    with pytest.raises(QueryError, match=r"window 1/2 .*429") as info:
        snt.exec_batch_query(FILTER, D0, _days(2))
    assert info.value.__cause__ is boom
    assert caps == [2, 4, 8, 16]
    assert len(sleeps) == 4
    assert cls.return_value.query_tables.call_count == 5  # 1 try + 4 retries


def test_batch_connection_error_is_retried(sentinel, backoff, caplog):
    cls, _ = sentinel
    reset = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
    boom = QueryConnectionError("Sentinel query failed: could not reach the service: [Errno 104]")
    boom.__cause__ = reset
    ok = [pd.DataFrame({"a": [1]})]
    cls.return_value.query_tables.side_effect = [boom, ok]
    caplog.set_level(logging.WARNING, logger="security_notebook_tools")
    assert snt.exec_batch_query(FILTER, D0, _days(1)).shape == (1, 1)
    assert backoff[0] == [1.0]
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "Batch 1/1 failed with a transient error (connection reset by peer); retrying in 1.0 s"
        " (attempt 1/4)" in m
        for m in messages
    )


def test_batch_transient_error_is_retried_then_exhausted(sentinel, backoff):
    cls, _ = sentinel
    cls.return_value.query_tables.side_effect = QueryTransientError("503")
    with pytest.raises(QueryError, match=r"window 1/1 .*failed after 4 retries: 503"):
        snt.exec_batch_query(FILTER, D0, _days(1))
    assert cls.return_value.query_tables.call_count == 5


def test_batch_backoff_is_capped(sentinel, backoff):
    cls, _ = sentinel
    cls.return_value.query_tables.side_effect = QueryThrottledError("429")
    with (
        mock.patch.object(_batch, "_MAX_RETRIES", 7),
        pytest.raises(QueryError, match="window 1/1"),
    ):
        snt.exec_batch_query(FILTER, D0, _days(1))
    assert backoff[1] == [2, 4, 8, 16, 30, 30, 30]


def test_batch_other_errors_are_not_retried(sentinel, backoff):
    cls, _ = sentinel
    cls.return_value.query_tables.side_effect = QueryError("syntax error")
    with pytest.raises(QueryError, match="syntax error"):
        snt.exec_batch_query(FILTER, D0, _days(3), max_workers=2)
    assert cls.return_value.query_tables.call_count == 1
    assert backoff[0] == []


def test_batch_first_window_failure_stops_before_the_pool(sentinel):
    cls, _ = sentinel
    cls.return_value.query_tables.side_effect = QueryError("bad")
    with pytest.raises(QueryError, match="window 1/6"):
        snt.exec_batch_query(FILTER, D0, _days(6), max_workers=5)
    assert cls.return_value.query_tables.call_count == 1


def test_batch_fail_fast_cancels_pending_and_raises_lowest_index(sentinel):
    cls, _ = sentinel
    called = []
    three_failed = threading.Event()

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        n = _window_no(timespan)
        called.append(n)
        if n == 3:
            three_failed.set()
            raise QueryError("w3 failed")
        if n == 2:  # still running when 3 fails, then fails too: the lower index wins
            assert three_failed.wait(10)
            time.sleep(0.05)
            raise QueryError("w2 failed")
        if n > 3:
            time.sleep(0.2)  # keeps a freed worker busy while the batch cancels the rest
        return [pd.DataFrame({"a": [n]})]

    cls.return_value.query_tables.side_effect = query
    with pytest.raises(QueryError, match=r"window 2/20 .*w2 failed") as info:
        snt.exec_batch_query(FILTER, D0, _days(20), max_workers=2)
    assert isinstance(info.value.__cause__, QueryError)
    assert set(called) <= {1, 2, 3, 4}
    assert len(called) < 20


def test_batch_configuration_error_in_a_window_is_not_wrapped(sentinel):
    cls, _ = sentinel
    bad = ConfigurationError("no")

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        if _window_no(timespan) == 3:
            raise bad
        return [pd.DataFrame({"a": [1]})]

    cls.return_value.query_tables.side_effect = query
    with pytest.raises(ConfigurationError) as info:
        snt.exec_batch_query(FILTER, D0, _days(5), max_workers=3)
    assert info.value is bad


def test_batch_failure_interrupts_a_waiting_retry(sentinel):
    """A window waiting to retry gives up when another window fails; it does not sleep it out."""
    cls, _ = sentinel
    two_throttled = threading.Event()

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        n = _window_no(timespan)
        if n == 2:
            two_throttled.set()
            raise QueryThrottledError("429")
        if n == 3:
            assert two_throttled.wait(10)
            time.sleep(0.05)  # window 2 is now in its (30 s) backoff
            raise QueryError("w3 failed")
        return [pd.DataFrame({"a": [n]})]

    cls.return_value.query_tables.side_effect = query
    started = time.monotonic()
    with (
        mock.patch.object(_batch.random, "uniform", return_value=30.0),
        pytest.raises(QueryError, match=r"window 3/4 .*w3 failed"),
    ):
        snt.exec_batch_query(FILTER, D0, _days(4), max_workers=2)
    assert time.monotonic() - started < 10


def test_batch_keyboard_interrupt_propagates(sentinel):
    with (
        mock.patch.object(_batch, "wait", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        snt.exec_batch_query(FILTER, D0, _days(5), max_workers=3)


def test_batch_adx_throttling_is_retried(adx, backoff):
    cls, _ = adx
    ok = [pd.DataFrame({"a": [1]})]
    cls.return_value.query_tables.side_effect = [QueryThrottledError("ADX query failed: 429"), ok]
    kql = "T | where Timestamp >= BatchStart and Timestamp < BatchEnd"
    assert not snt.exec_batch_query(kql, D0, _days(1), target="adx").empty
    assert backoff[0] == [1.0]


# --- exec_batch_query: merge_on / agg_column / agg_func -----------------------------------------


def _win(users, counts, **types):
    df = pd.DataFrame({"User": users, "n": counts, "v": [float(c) for c in counts]})
    df.attrs["kql_types"] = {"User": "string", "n": "long", "v": "real", **types}
    return df


@pytest.fixture
def two_days(sentinel):
    cls, _ = sentinel
    cls.return_value.query_tables.side_effect = lambda *a, **k: [
        _win(["a", "b"], [1, 2])
        if len(cls.return_value.query_tables.call_args_list) == 1
        else _win(["b", "c"], [10, 20])
    ]
    return cls


@pytest.mark.parametrize("merge_on", ["User", ["User"], ("User",)])
@pytest.mark.parametrize("agg_column", ["n", ["n"]])
def test_batch_agg_sums_across_windows(two_days, merge_on, agg_column):
    df = snt.exec_batch_query(FILTER, D0, _days(2), merge_on=merge_on, agg_column=agg_column)
    assert list(df.columns) == ["User", "n"]
    assert df["User"].tolist() == ["a", "b", "c"]
    assert df["n"].tolist() == [1, 12, 20]
    assert list(df.index) == [0, 1, 2]
    assert df.attrs["kql_types"] == {"User": "string", "n": "long"}


def test_batch_agg_multiple_columns_and_func(two_days):
    df = snt.exec_batch_query(
        FILTER, D0, _days(2), merge_on="User", agg_column=["n", "v"], agg_func="max"
    )
    assert list(df.columns) == ["User", "n", "v"]
    assert df["n"].tolist() == [1, 10, 20]
    assert df["v"].tolist() == [1.0, 10.0, 20.0]
    assert df.attrs["kql_types"] == {"User": "string", "n": "long", "v": "real"}


def test_batch_agg_keeps_null_key(sentinel):
    cls, _ = sentinel
    cls.return_value.query_tables.side_effect = [
        [_win(["a", None], [1, 2])],
        [_win([None, "a"], [5, 7])],
    ]
    df = snt.exec_batch_query(FILTER, D0, _days(2), merge_on="User", agg_column="n")
    assert len(df) == 2
    assert df.loc[df["User"].isna(), "n"].item() == 7
    assert df.loc[df["User"] == "a", "n"].item() == 8


def test_batch_agg_callable(two_days):
    df = snt.exec_batch_query(
        FILTER, D0, _days(2), merge_on="User", agg_column="n", agg_func=lambda s: s.max() - s.min()
    )
    assert df["n"].tolist() == [0, 8, 0]
    assert "n" not in df.attrs["kql_types"]
    assert df.attrs["kql_types"]["User"] == "string"


@pytest.mark.parametrize(
    ("func", "expected"),
    [
        ("sum", "long"),
        ("count", "long"),
        ("nunique", "long"),
        ("mean", "real"),
        ("median", "real"),
        ("min", "long"),
        ("last", "long"),
        ("prod", None),
    ],
)
def test_batch_agg_kql_types(two_days, func, expected):
    df = snt.exec_batch_query(FILTER, D0, _days(2), merge_on="User", agg_column="n", agg_func=func)
    assert df.attrs["kql_types"].get("n") == expected


def test_batch_agg_logs(two_days, caplog):
    with caplog.at_level(logging.INFO, logger="security_notebook_tools.batch"):
        snt.exec_batch_query(FILTER, D0, _days(2), merge_on="User", agg_column="n")
    assert "Re-aggregated 4 rows into 3 by ['User'] (sum of ['n'])" in caplog.text


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"merge_on": "User"}, "together"),
        ({"agg_column": "n"}, "together"),
        ({"agg_func": "sum"}, "needs merge_on"),
        ({"merge_on": [], "agg_column": "n"}, "merge_on must not be empty"),
        ({"merge_on": "User", "agg_column": []}, "agg_column must not be empty"),
        ({"merge_on": "", "agg_column": "n"}, "non-empty column names"),
        ({"merge_on": "User", "agg_column": ["n", 3]}, "non-empty column names"),
        ({"merge_on": ["User", "User"], "agg_column": "n"}, "repeat"),
        ({"merge_on": ["User", "n"], "agg_column": "n"}, "both"),
        ({"merge_on": "User", "agg_column": "n", "agg_func": ""}, "agg_func"),
        ({"merge_on": "User", "agg_column": "n", "agg_func": 3}, "agg_func"),
    ],
)
def test_batch_agg_validation_before_any_query(sentinel, kwargs, match):
    cls, _ = sentinel
    with pytest.raises(ConfigurationError, match=match):
        snt.exec_batch_query(FILTER, D0, _days(3), **kwargs)
    cls.return_value.query_tables.assert_not_called()


def test_batch_agg_missing_column_stops_after_first_window(sentinel):
    cls, _ = sentinel
    cls.return_value.query_tables.side_effect = lambda *a, **k: [_win(["a"], [1])]
    with pytest.raises(ConfigurationError, match=r"\['Nope'\].*available columns.*User"):
        snt.exec_batch_query(FILTER, D0, _days(6), max_workers=3, merge_on="Nope", agg_column="n")
    assert cls.return_value.query_tables.call_count == 1


def test_batch_agg_missing_column_found_after_merge(sentinel):
    cls, _ = sentinel
    cls.return_value.query_tables.side_effect = [[pd.DataFrame()], [_win(["a"], [1])]]
    with pytest.raises(ConfigurationError, match="Nope"):
        snt.exec_batch_query(FILTER, D0, _days(2), merge_on="Nope", agg_column="n")


def test_batch_agg_all_empty_is_returned_unaggregated(sentinel):
    cls, _ = sentinel
    cls.return_value.query_tables.side_effect = [[pd.DataFrame()], []]
    df = snt.exec_batch_query(FILTER, D0, _days(2), merge_on="User", agg_column="n")
    assert df.empty
    assert len(df.columns) == 0


def test_batch_agg_bad_function_is_wrapped(two_days):
    with pytest.raises(ConfigurationError, match="nonsense") as info:
        snt.exec_batch_query(
            FILTER, D0, _days(2), merge_on="User", agg_column="n", agg_func="nonsense"
        )
    assert info.value.__cause__ is not None


def test_batch_without_merge_on_is_unchanged(two_days):
    df = snt.exec_batch_query(FILTER, D0, _days(2))
    assert len(df) == 4
    assert list(df.columns) == ["User", "n", "v"]


# --- result cache -------------------------------------------------------------------------------


def _sdk_calls(cls):
    return cls.return_value.query_tables.call_count


def test_sentinel_second_call_hits_cache(sentinel, caplog):
    cls, _ = sentinel
    with caplog.at_level(logging.INFO, logger="security_notebook_tools.query"):
        snt.exec_query("T")
        assert "cached" not in caplog.text
        snt.exec_query("T")
    assert _sdk_calls(cls) == 1
    assert "Returning cached result" in caplog.text


def test_adx_query_and_show_hit_cache(adx):
    cls, _ = adx
    for kql in ("T", ".show tables"):
        snt.exec_query(kql, target="adx")
        snt.exec_query(kql, target="adx")
    assert cls.return_value.query_tables.call_count == 1
    assert cls.return_value.show.call_count == 1


@pytest.mark.parametrize("fixture", ["sentinel", "adx"])
def test_cache_false_bypasses_and_keeps_cache(fixture, request):
    cls, _ = request.getfixturevalue(fixture)
    kw = {"target": "adx"} if fixture == "adx" else {}
    snt.exec_query("T", **kw)
    snt.exec_query("T", cache=False, **kw)
    assert _sdk_calls(cls) == 2
    snt.exec_query("T", cache=False, **kw)
    assert _sdk_calls(cls) == 3
    snt.exec_query("T", **kw)  # the first result is still cached
    assert _sdk_calls(cls) == 3


def test_cache_false_does_not_store(sentinel):
    cls, _ = sentinel
    snt.exec_query("T", cache=False)
    snt.exec_query("T")
    assert _sdk_calls(cls) == 2


def test_clear_cache_forces_refetch(sentinel):
    cls, _ = sentinel
    snt.exec_query("T")
    snt.clear_cache()
    snt.exec_query("T")
    assert _sdk_calls(cls) == 2


def test_different_inputs_miss(sentinel, monkeypatch):
    cls, _ = sentinel
    snt.exec_query("T")
    snt.exec_query("T2")
    snt.exec_query("T", timedelta(days=2))
    snt.exec_query("T", timeout=60)
    monkeypatch.setenv("SENTINEL_WORKSPACE_ID", "33333333-3333-3333-3333-333333333333")
    monkeypatch.setenv("SENTINEL_WORKSPACE_NAME", "other")
    monkeypatch.setenv("SENTINEL_RESOURCE_GROUP", "rg")
    rid = RID.replace("sentinel-ws", "other")
    monkeypatch.setattr("security_notebook_tools._query.resolve_workspace_resource_id", lambda: rid)
    snt.exec_query("T")
    assert _sdk_calls(cls) == 5


def test_adx_different_database_and_cluster_miss(adx, monkeypatch):
    cls, _ = adx
    snt.exec_query("T", target="adx")
    monkeypatch.setenv("ADX_DATABASE", "db2")
    snt.exec_query("T", target="adx")
    monkeypatch.setenv("ADX_CLUSTER_URI", "https://other.kusto.windows.net")
    snt.exec_query("T", target="adx")
    assert cls.return_value.query_tables.call_count == 3


def test_mutating_result_does_not_change_cache(sentinel):
    cls, _ = sentinel
    cls.return_value.query_tables.return_value[0].attrs["kql_types"] = {"a": "long"}
    first = snt.exec_query("T")
    first.loc[0, "a"] = 99
    first.attrs["kql_types"]["a"] = "string"
    second = snt.exec_query("T")
    assert second.loc[0, "a"] == 1
    assert second.attrs["kql_types"] == {"a": "long"}
    assert _sdk_calls(cls) == 1


def test_errors_are_not_cached(sentinel):
    cls, _ = sentinel
    ok = cls.return_value.query_tables.return_value
    cls.return_value.query_tables.side_effect = [QueryError("boom"), ok]
    with pytest.raises(QueryError):
        snt.exec_query("T")
    assert snt.exec_query("T").loc[0, "a"] == 1
    assert _sdk_calls(cls) == 2


def test_partial_results_are_not_cached_and_warn(monkeypatch):
    import warnings

    from azure.monitor.query import LogsQueryPartialResult, LogsTable

    monkeypatch.setenv("SENTINEL_WORKSPACE_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("SENTINEL_SUBSCRIPTION_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("SENTINEL_RESOURCE_GROUP", "rg")
    monkeypatch.setenv("SENTINEL_WORKSPACE_NAME", "sentinel-ws")
    table = LogsTable(name="T", columns=["a"], columns_types=["long"], rows=[[1]])
    with (
        mock.patch("security_notebook_tools._query.get_credential"),
        mock.patch("security_notebook_tools._sentinel.LogsQueryClient") as sdk,
        mock.patch("security_notebook_tools._sentinel.get_policy"),
        mock.patch("security_notebook_tools._sentinel.check_sentinel_query"),
    ):
        sdk.return_value.query_workspace.return_value = LogsQueryPartialResult(
            partial_data=[table], partial_error="boom"
        )
        for _ in range(2):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                df = snt.exec_query("T")
            assert [str(w.message) for w in caught if "partial" in str(w.message)] == [
                "Sentinel query returned partial results: boom"
            ]
            assert df.loc[0, "a"] == 1
            assert df.attrs["kql_types"] == {"a": "long"}
        assert sdk.return_value.query_workspace.call_count == 2


def test_batch_rerun_reuses_succeeded_windows(sentinel):
    cls, _ = sentinel
    ok = cls.return_value.query_tables.return_value
    cls.return_value.query_tables.side_effect = [ok, QueryError("boom"), ok, ok, ok]
    with pytest.raises(QueryError, match="window 2/2"):
        snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=2))
    assert _sdk_calls(cls) == 2
    snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=2))
    assert _sdk_calls(cls) == 3  # only the failed window ran again
    snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=2), cache=False)
    assert _sdk_calls(cls) == 5


def test_batch_unexpected_error_is_wrapped_logged_and_not_retried(sentinel, backoff, caplog):
    cls, _ = sentinel
    boom = RuntimeError("boom")
    cls.return_value.query_tables.side_effect = boom
    caplog.set_level(logging.ERROR, logger="security_notebook_tools")
    with pytest.raises(QueryError, match=r"window 1/2 .*unexpected RuntimeError: boom") as ei:
        snt.exec_batch_query(FILTER, D0, _days(2))
    assert ei.value.__cause__ is boom
    assert cls.return_value.query_tables.call_count == 1
    assert backoff[0] == []
    records = [r for r in caplog.records if r.exc_info]
    assert records[0].exc_info[1] is boom
    assert "Batch window 1/2" in records[0].getMessage()


def test_batch_configuration_error_passes_through_unchanged(sentinel, backoff):
    cls, _ = sentinel
    config = ConfigurationError("cost guard")
    cls.return_value.query_tables.side_effect = config
    with pytest.raises(ConfigurationError) as ei:
        snt.exec_batch_query(FILTER, D0, _days(2))
    assert ei.value is config
