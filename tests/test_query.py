import contextlib
import logging
import warnings
from datetime import UTC, date, datetime, timedelta
from unittest import mock

import pandas as pd
import pytest

import security_notebook_tools as snt
from security_notebook_tools._exceptions import ConfigurationError

PUBLIC = {
    "clear_cache",
    "exec_batch_query",
    "exec_query",
    "format_dataframe",
    "merge_dataframe",
    "pivot_dataframe",
    "render_dataframe",
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


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"kql": 5}, "kql must be a string"),
        ({"kql": None}, "kql must be a string"),
        ({"kql": "  \n"}, "blank"),
        ({"timeout": "60"}, "timeout"),
        ({"timeout": 1.5}, "timeout"),
        ({"timeout": True}, "timeout"),
        ({"timeout": float("nan")}, "timeout"),
        ({"timeout": -1}, "timeout"),
        ({"cache": 1}, "cache must be a bool"),
        ({"debug": "yes"}, "debug must be a bool"),
        ({"timespan": [datetime(2026, 1, 1), datetime(2026, 1, 2)]}, "timespan"),
        ({"timespan": timedelta(0)}, "timespan"),
        ({"timespan": (datetime(2026, 1, 1),)}, "timespan"),
        ({"timespan": "1d"}, "timespan"),
    ],
)
def test_exec_query_validation(sentinel, kwargs, match):
    cls, _ = sentinel
    args = {"kql": "T", **kwargs}
    with pytest.raises(ConfigurationError, match=match):
        snt.exec_query(**args)
    cls.return_value.query_tables.assert_not_called()


def test_invalid_arguments(adx):
    with pytest.raises(ConfigurationError, match="timespan"):
        snt.exec_query("T", timedelta(days=1), target="adx")
    with pytest.raises(ConfigurationError, match="timeout"):
        snt.exec_query("T", timeout=0)
    with pytest.raises(ConfigurationError, match="target"):
        snt.exec_query("T", target="splunk")


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
    # Relative to now: cut on the 2-hour grid from the epoch, first and last windows clipped.
    assert _sent_windows(cls) == [
        (datetime(2026, 1, 10, 1, tzinfo=UTC), datetime(2026, 1, 10, 2, tzinfo=UTC)),
        (datetime(2026, 1, 10, 2, tzinfo=UTC), datetime(2026, 1, 10, 4, tzinfo=UTC)),
        (datetime(2026, 1, 10, 4, tzinfo=UTC), fake_now),
    ]


def test_batch_timedelta_start_with_end_is_not_aligned(sentinel):
    cls, _ = sentinel
    end = datetime(2026, 1, 10, 6, tzinfo=UTC)
    snt.exec_batch_query(FILTER, timedelta(hours=5), end, interval=timedelta(hours=2))
    assert _sent_windows(cls) == [
        (datetime(2026, 1, 10, 1, tzinfo=UTC), datetime(2026, 1, 10, 3, tzinfo=UTC)),
        (datetime(2026, 1, 10, 3, tzinfo=UTC), datetime(2026, 1, 10, 5, tzinfo=UTC)),
        (datetime(2026, 1, 10, 5, tzinfo=UTC), end),
    ]


def test_batch_relative_rerun_reuses_all_but_the_edge_windows(sentinel):
    cls, _ = sentinel
    with mock.patch("security_notebook_tools._batch.datetime") as dt:
        dt.now.return_value = datetime(2026, 1, 10, 6, 15, 30, 123456, tzinfo=UTC)
        snt.exec_batch_query(FILTER, timedelta(days=30))
        assert _sdk_calls(cls) == 31  # 29 whole days plus the clipped first and last
        dt.now.return_value = datetime(2026, 1, 10, 6, 47, 2, 654321, tzinfo=UTC)
        snt.exec_batch_query(FILTER, timedelta(days=30))
    assert _sdk_calls(cls) == 33  # only the first and last windows ran again
    windows = _sent_windows(cls)
    assert windows[1] == (datetime(2025, 12, 12, tzinfo=UTC), datetime(2025, 12, 13, tzinfo=UTC))
    assert windows[-1] == (
        datetime(2026, 1, 10, tzinfo=UTC),
        datetime(2026, 1, 10, 6, 47, 2, 654321, tzinfo=UTC),
    )


def test_batch_relative_start_on_the_grid_has_no_sliver_window(sentinel):
    cls, _ = sentinel
    with mock.patch("security_notebook_tools._batch.datetime") as dt:
        dt.now.return_value = datetime(2026, 1, 3, tzinfo=UTC)
        snt.exec_batch_query(FILTER, timedelta(days=2))
    assert _sent_windows(cls) == [
        (D0, datetime(2026, 1, 2, tzinfo=UTC)),
        (datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 3, tzinfo=UTC)),
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
        ({"timeout": 1.5}, "timeout"),
        ({"timeout": True}, "timeout"),
        ({"interval": 86400}, "interval must be a timedelta"),
        ({"end": "2026-01-02"}, "end must be"),
        ({"end": date(2026, 1, 2)}, "end must be"),
        ({"cache": 0}, "cache must be a bool"),
        ({"debug": None}, "debug must be a bool"),
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


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ((5, D0), "kql must be a string"),
        (("  ", D0), "blank"),
        ((FILTER, "2026-01-01"), "start must be"),
        ((FILTER, date(2026, 1, 1)), "start must be"),
        ((FILTER, None), "start must be"),
    ],
)
def test_batch_argument_types(sentinel, args, match):
    cls, _ = sentinel
    with pytest.raises(ConfigurationError, match=match):
        snt.exec_batch_query(*args)
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
    QueryTimeoutError,
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


# --- exec_batch_query: timeouts and late connection resets --------------------------------------


class FakeClock:
    """A clock the test moves forward by hand: queries "run" for as long as the test says."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    fake = FakeClock()
    with mock.patch.object(_batch, "_clock", fake):
        yield fake


def _reset(message="Sentinel query failed: could not reach the service: [Errno 104]"):
    error = QueryConnectionError(message)
    error.__cause__ = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
    return error


def test_batch_late_connection_reset_is_a_timeout_and_not_retried(sentinel, backoff, clock, caplog):
    cls, _ = sentinel
    reset = _reset()

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        clock.now += 270
        raise reset

    cls.return_value.query_tables.side_effect = query
    caplog.set_level(logging.WARNING, logger="security_notebook_tools")
    with pytest.raises(
        QueryTimeoutError, match=r"window 1/2 .*ran 270 s before the connection"
    ) as ei:
        snt.exec_batch_query(FILTER, D0, _days(2))
    message = str(ei.value)
    assert "smaller `interval`" in message
    assert "`min_interval`" in message
    assert "raise `timeout`" in message
    assert ei.value.__cause__ is reset
    assert isinstance(ei.value, QueryError)
    assert not isinstance(ei.value, QueryTransientError)
    assert cls.return_value.query_tables.call_count == 1
    assert backoff[0] == []  # nothing was waited for
    warnings_ = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Batch 1/2 was cut off after 270 s" in m and "not retried" in m for m in warnings_)
    assert not any("retrying" in m for m in warnings_)


def test_batch_early_connection_reset_is_still_retried(sentinel, backoff, clock):
    cls, _ = sentinel
    ok = [pd.DataFrame({"a": [1]})]
    calls = []

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        calls.append(1)
        clock.now += 5
        if len(calls) == 1:
            raise _reset()
        return ok

    cls.return_value.query_tables.side_effect = query
    assert snt.exec_batch_query(FILTER, D0, _days(1)).shape == (1, 1)
    assert len(calls) == 2
    assert backoff[0] == [1.0]


def test_batch_late_reset_threshold_is_half_of_timeout(sentinel, backoff, clock):
    assert _batch._LATE_RESET_FRACTION == 0.5
    cls, _ = sentinel
    ok = [pd.DataFrame({"a": [1]})]
    ran = {"for": 149.9}

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        clock.now += ran["for"]
        if cls.return_value.query_tables.call_count == 1:
            raise _reset()
        return ok

    cls.return_value.query_tables.side_effect = query
    snt.exec_batch_query(FILTER, D0, _days(1), timeout=300)  # 149.9 s < 150 s: retried
    assert cls.return_value.query_tables.call_count == 2
    cls.return_value.query_tables.reset_mock()
    ran["for"] = 150
    with pytest.raises(QueryTimeoutError):
        snt.exec_batch_query(FILTER, D0, _days(1), timeout=300, cache=False)
    assert cls.return_value.query_tables.call_count == 1  # exactly half: not retried


def test_batch_late_reset_threshold_follows_the_timeout_argument(sentinel, backoff, clock):
    cls, _ = sentinel

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        clock.now += 40
        raise _reset()

    cls.return_value.query_tables.side_effect = query
    with pytest.raises(QueryTimeoutError):
        snt.exec_batch_query(FILTER, D0, _days(1), timeout=60)  # 40 s >= 30 s
    assert cls.return_value.query_tables.call_count == 1
    cls.return_value.query_tables.reset_mock()
    with pytest.raises(QueryError, match="after 4 retries") as ei:
        snt.exec_batch_query(FILTER, D0, _days(1), timeout=100)  # 40 s < 50 s: retried
    assert not isinstance(ei.value, QueryTimeoutError)
    assert cls.return_value.query_tables.call_count == 5


def test_batch_reset_after_an_early_retry_can_still_be_late(sentinel, backoff, clock):
    cls, _ = sentinel
    durations = iter([2, 280])

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        clock.now += next(durations)
        raise _reset()

    cls.return_value.query_tables.side_effect = query
    with pytest.raises(QueryTimeoutError, match="ran 280 s"):
        snt.exec_batch_query(FILTER, D0, _days(1))
    assert cls.return_value.query_tables.call_count == 2
    assert backoff[0] == [1.0]


@pytest.mark.parametrize("workers", [1, 3])
def test_batch_late_reset_in_a_later_window_stops_the_batch(sentinel, backoff, clock, workers):
    cls, _ = sentinel

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        if _window_no(timespan) == 3:
            clock.now += 270
            raise _reset()
        return [pd.DataFrame({"a": [1]})]

    cls.return_value.query_tables.side_effect = query
    with pytest.raises(QueryTimeoutError, match="window 3/4"):
        snt.exec_batch_query(FILTER, D0, _days(4), max_workers=workers)
    assert backoff[0] == []


def test_batch_timeout_error_is_not_retried_and_names_the_window(sentinel, backoff):
    cls, _ = sentinel
    boom = QueryTimeoutError("Sentinel query failed: GatewayTimeout (set `min_interval`)")
    cls.return_value.query_tables.side_effect = [[pd.DataFrame({"a": [1]})], boom]
    with pytest.raises(QueryTimeoutError, match=r"window 2/3 .*2026-01-02.*GatewayTimeout") as ei:
        snt.exec_batch_query(FILTER, D0, _days(3))
    assert ei.value.__cause__ is boom
    assert isinstance(ei.value, QueryError)
    assert cls.return_value.query_tables.call_count == 2
    assert backoff[0] == []


# --- exec_batch_query: min_interval splits windows that time out -------------------------------

HOUR = timedelta(hours=1)


def _hourly_rows(timespan):
    """The hours (since D0) that start inside the window, so any split has the same rows."""
    start, end = timespan
    first = -(-(start - D0) // HOUR)  # ceiling
    last = -(-(end - D0) // HOUR)
    df = pd.DataFrame({"h": list(range(first, last))})
    df.attrs["kql_types"] = {"h": "long"}
    return df


def _slow_over(limit, seen=None):
    """A query_tables fake: a window longer than ``limit`` times out, the rest returns rows."""

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        if seen is not None:
            seen.append((timespan, kql))
        if timespan[1] - timespan[0] > limit:
            raise QueryTimeoutError("Sentinel query failed: GatewayTimeout (ADX query timed out)")
        return [_hourly_rows(timespan)]

    return query


def _unsplit(cls, days, **kwargs):
    """The reference result: the same rows from a service that never times out."""
    cls.return_value.query_tables.side_effect = _slow_over(timedelta(days=999))
    return snt.exec_batch_query(FILTER, D0, _days(days), cache=False, **kwargs)


@pytest.mark.parametrize("workers", [1, 3])
def test_batch_min_interval_splits_a_timed_out_window_and_matches_the_unsplit_result(
    sentinel, workers
):
    cls, _ = sentinel
    expected = _unsplit(cls, 8, interval=timedelta(days=4), max_workers=workers)
    seen = []
    cls.return_value.query_tables.side_effect = _slow_over(timedelta(days=1), seen)
    df = snt.exec_batch_query(
        FILTER,
        D0,
        _days(8),
        interval=timedelta(days=4),
        min_interval=timedelta(hours=6),
        max_workers=workers,
        cache=False,
    )
    pd.testing.assert_frame_equal(df, expected)
    assert df["h"].tolist() == list(range(8 * 24))
    assert df.attrs["kql_types"] == {"h": "long"}
    assert list(df.index) == list(range(8 * 24))
    spans = sorted((span[1] - span[0] for span, _ in seen), reverse=True)
    assert spans[0] == timedelta(days=4)
    assert spans[-1] == timedelta(days=1)
    assert len(seen) == 2 * (1 + 2 + 4)  # two 4-day windows, each tried as 4d, 2d, 2d, 1d x 4


def test_batch_min_interval_halves_run_in_time_order_with_their_own_bounds(sentinel):
    cls, _ = sentinel
    seen = []
    cls.return_value.query_tables.side_effect = _slow_over(timedelta(days=1), seen)
    snt.exec_batch_query(
        FILTER, D0, _days(2), interval=timedelta(days=2), min_interval=timedelta(hours=12)
    )
    assert [span for span, _ in seen] == [
        (D0, _days(2)),  # times out
        (D0, _days(1)),  # first half
        (_days(1), _days(2)),  # second half
    ]
    for (start, end), kql in seen:
        assert kql.startswith(
            f'let BatchStart = datetime("{start:%Y-%m-%dT%H:%M:%S.%fZ}"); '
            f'let BatchEnd = datetime("{end:%Y-%m-%dT%H:%M:%S.%fZ}");\n'
        )
        assert kql.endswith(FILTER)


def test_batch_min_interval_uneven_window_splits_at_the_middle_without_a_gap(sentinel):
    cls, _ = sentinel
    seen = []
    cls.return_value.query_tables.side_effect = _slow_over(timedelta(hours=5), seen)
    end = D0 + timedelta(hours=9)  # the last window is clipped to 9 h
    df = snt.exec_batch_query(
        FILTER, D0, end, interval=timedelta(hours=9), min_interval=timedelta(hours=1), cache=False
    )
    assert df["h"].tolist() == list(range(9))
    spans = [span for span, _ in seen]
    assert spans[0] == (D0, end)
    assert spans[1][1] == spans[2][0]  # the halves share the middle: no gap, no overlap
    assert spans[1][0] == D0
    assert spans[2][1] == end


def test_batch_min_interval_stops_at_the_minimum_and_names_the_smallest_window(sentinel, caplog):
    cls, _ = sentinel
    seen = []
    cls.return_value.query_tables.side_effect = _slow_over(timedelta(0), seen)
    caplog.set_level(logging.WARNING, logger="security_notebook_tools")
    with pytest.raises(QueryTimeoutError, match=r"window 1/1\.1\.1 \(2026-01-01") as ei:
        snt.exec_batch_query(
            FILTER, D0, _days(4), interval=timedelta(days=4), min_interval=timedelta(days=1)
        )
    message = str(ei.value)
    assert "smallest window tried was 1 day, 0:00:00" in message
    assert "min_interval=1 day, 0:00:00" in message
    assert "Use a smaller `min_interval` or raise `timeout`" in message
    assert "set `min_interval`" not in message  # the inner hint would contradict the user
    assert "use a smaller `interval`" not in message
    inner = ei.value.__cause__.__cause__  # _WindowTimeout, then the service's error
    assert "GatewayTimeout" in str(inner)
    assert isinstance(ei.value, QueryError)
    # 4 days -> 2 days -> 1 day; the first half failed, so the second was never tried
    assert [span[1] - span[0] for span, _ in seen] == [timedelta(days=d) for d in (4, 2, 1)]
    assert [m for m in (r.getMessage() for r in caplog.records) if "cannot be split" in m]


def test_batch_min_interval_equal_to_interval_never_splits(sentinel):
    cls, _ = sentinel
    cls.return_value.query_tables.side_effect = _slow_over(timedelta(0))
    with pytest.raises(QueryTimeoutError, match="smallest window tried was 1 day"):
        snt.exec_batch_query(FILTER, D0, _days(2), min_interval=timedelta(days=1))
    assert cls.return_value.query_tables.call_count == 1


def test_batch_without_min_interval_a_timeout_fails_the_batch_as_before(sentinel):
    cls, _ = sentinel
    cls.return_value.query_tables.side_effect = _slow_over(timedelta(0))
    with pytest.raises(QueryTimeoutError, match=r"window 1/2 \(2026-01-01.*GatewayTimeout") as ei:
        snt.exec_batch_query(FILTER, D0, _days(2))
    assert "smallest window" not in str(ei.value)
    assert cls.return_value.query_tables.call_count == 1


def test_batch_min_interval_logs_each_split_without_kql(sentinel, caplog):
    cls, _ = sentinel
    cls.return_value.query_tables.side_effect = _slow_over(timedelta(days=1))
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    snt.exec_batch_query(
        FILTER, D0, _days(4), interval=timedelta(days=4), min_interval=timedelta(days=1)
    )
    splits = [r for r in caplog.records if "splitting it into two" in r.getMessage()]
    assert [r.levelno for r in splits] == [logging.WARNING] * 3  # 4d, then each 2d half
    first = splits[0].getMessage()
    assert "Batch 1/1 (2026-01-01 00:00:00+00:00 to 2026-01-05 00:00:00+00:00)" in first
    assert "timed out after" in first
    assert "two windows of 2 days, 0:00:00" in first
    assert "Batch 1/1.1 " in splits[1].getMessage()
    assert "Batch 1/1.2 " in splits[2].getMessage()
    assert not any("TimeGenerated" in r.getMessage() for r in caplog.records)
    assert not any("BatchStart" in r.getMessage() for r in caplog.records)


def test_batch_min_interval_late_connection_reset_splits_too(sentinel, backoff, clock):
    cls, _ = sentinel

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        if timespan[1] - timespan[0] > timedelta(days=1):
            clock.now += 270
            raise _reset()
        return [_hourly_rows(timespan)]

    cls.return_value.query_tables.side_effect = query
    df = snt.exec_batch_query(
        FILTER, D0, _days(2), interval=timedelta(days=2), min_interval=timedelta(days=1)
    )
    assert df["h"].tolist() == list(range(48))
    assert backoff[0] == []  # the late reset was not retried, it was split


def test_batch_min_interval_early_reset_is_retried_not_split(sentinel, backoff, clock):
    cls, _ = sentinel
    seen = []
    ok = [_hourly_rows((D0, _days(1)))]

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        seen.append(timespan)
        if len(seen) == 1:
            clock.now += 3
            raise _reset()
        return ok

    cls.return_value.query_tables.side_effect = query
    snt.exec_batch_query(FILTER, D0, _days(1), min_interval=timedelta(hours=1))
    assert seen == [(D0, _days(1)), (D0, _days(1))]  # same window again after the backoff
    assert backoff[0] == [1.0]


def test_batch_min_interval_other_errors_are_not_split(sentinel):
    cls, _ = sentinel
    cls.return_value.query_tables.side_effect = QueryError("syntax error")
    with pytest.raises(QueryError, match="syntax error") as ei:
        snt.exec_batch_query(FILTER, D0, _days(2), min_interval=timedelta(hours=1))
    assert not isinstance(ei.value, QueryTimeoutError)
    assert cls.return_value.query_tables.call_count == 1


def test_batch_min_interval_error_in_a_half_names_that_half(sentinel):
    cls, _ = sentinel

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        if timespan[1] - timespan[0] > timedelta(days=1):
            raise QueryTimeoutError("slow")
        if timespan[0] >= _days(1):
            raise QueryError("boom")
        return [_hourly_rows(timespan)]

    cls.return_value.query_tables.side_effect = query
    with pytest.raises(QueryError, match=r"window 1/1\.2 \(2026-01-02.*boom") as ei:
        snt.exec_batch_query(
            FILTER, D0, _days(2), interval=timedelta(days=2), min_interval=timedelta(hours=6)
        )
    assert not isinstance(ei.value, QueryTimeoutError)


def test_batch_min_interval_a_half_is_retried_when_throttled(sentinel, backoff):
    cls, _ = sentinel
    calls = []

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        calls.append(timespan)
        if timespan[1] - timespan[0] > timedelta(days=1):
            raise QueryTimeoutError("slow")
        if timespan[0] == _days(1) and calls.count(timespan) == 1:
            raise QueryThrottledError("429")
        return [_hourly_rows(timespan)]

    cls.return_value.query_tables.side_effect = query
    df = snt.exec_batch_query(
        FILTER, D0, _days(2), interval=timedelta(days=2), min_interval=timedelta(days=1)
    )
    assert df["h"].tolist() == list(range(48))
    assert backoff[0] == [1.0]


def test_batch_min_interval_stop_is_honoured_between_halves(sentinel):
    stop = threading.Event()
    seen = []

    def attempt(label, bounds, *args):
        seen.append(label)
        if label == "1/1":
            raise _batch._WindowTimeout("slow", 100.0)
        stop.set()  # another window failed while the first half ran
        return pd.DataFrame({"h": [1]})

    with (
        mock.patch.object(_batch, "_attempt_window", side_effect=attempt),
        pytest.raises(_batch._Stopped),
    ):
        _batch._run_window(
            1,
            1,
            (D0, _days(2)),
            FILTER,
            "sentinel",
            300,
            True,
            stop,
            min_interval=timedelta(hours=1),
        )
    assert seen == ["1/1", "1/1.1"]  # the second half never started


def test_batch_min_interval_stop_set_before_the_first_half(sentinel):
    stop = threading.Event()
    stop.set()
    with (
        mock.patch.object(_batch, "_attempt_window", side_effect=_batch._WindowTimeout("x", 1.0)),
        pytest.raises(_batch._Stopped),
    ):
        _batch._run_window(
            1, 1, (D0, _days(2)), FILTER, "sentinel", 300, True, stop, timedelta(hours=1)
        )


def test_batch_min_interval_parallel_failure_of_one_window_stops_the_others(sentinel):
    cls, _ = sentinel

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        if timespan[0] == D0 and timespan[1] - timespan[0] > timedelta(days=1):
            return [_hourly_rows(timespan)]
        if timespan[1] - timespan[0] > timedelta(days=1):
            raise QueryTimeoutError("slow")
        if timespan[0] == _days(2):
            raise QueryError("boom")
        return [_hourly_rows(timespan)]

    cls.return_value.query_tables.side_effect = query
    with pytest.raises(QueryError, match="boom"):
        snt.exec_batch_query(
            FILTER,
            D0,
            _days(8),
            interval=timedelta(days=2),
            min_interval=timedelta(days=1),
            max_workers=3,
        )


def test_batch_min_interval_halves_with_no_rows_and_mixed_types(sentinel):
    cls, _ = sentinel
    first = pd.DataFrame({"a": [1]})
    first.attrs["kql_types"] = {"a": "long"}
    second = pd.DataFrame({"a": [2], "b": ["x"]})
    second.attrs["kql_types"] = {"a": "long", "b": "string"}
    cls.return_value.query_tables.side_effect = [
        QueryTimeoutError("slow"),
        [pd.DataFrame()],
        [second],
        [first],
    ]
    df = snt.exec_batch_query(
        FILTER, D0, _days(2), interval=timedelta(days=2), min_interval=timedelta(days=1)
    )
    assert df["a"].tolist() == [2]
    assert df.attrs["kql_types"] == {"a": "long", "b": "string"}
    # all windows empty: still the first (empty) frame, as for an unsplit batch
    cls.return_value.query_tables.side_effect = [QueryTimeoutError("slow"), [], []]
    assert snt.exec_batch_query(
        FILTER,
        D0,
        _days(2),
        interval=timedelta(days=2),
        min_interval=timedelta(days=1),
        cache=False,
    ).empty


def test_batch_min_interval_halves_are_cached_like_windows(sentinel):
    cls, _ = sentinel
    cls.return_value.query_tables.side_effect = _slow_over(timedelta(days=1))
    args = (FILTER, D0, _days(2))
    kwargs = {"interval": timedelta(days=2), "min_interval": timedelta(days=1)}
    df = snt.exec_batch_query(*args, **kwargs)
    assert cls.return_value.query_tables.call_count == 3  # timed out, then two halves
    again = snt.exec_batch_query(*args, **kwargs)
    # The window that timed out is remembered: it is not tried whole (and not waited for) again.
    assert cls.return_value.query_tables.call_count == 3
    pd.testing.assert_frame_equal(again, df)


def test_batch_a_rerun_goes_straight_to_the_halves_of_a_timed_out_window(sentinel, caplog):
    cls, _ = sentinel
    seen = []
    cls.return_value.query_tables.side_effect = _slow_over(timedelta(days=1), seen)
    kwargs = {"interval": timedelta(days=2), "min_interval": timedelta(days=1)}
    with pytest.raises(QueryTimeoutError):  # no min_interval: the timeout is only remembered
        snt.exec_batch_query(FILTER, D0, _days(2), interval=timedelta(days=2))
    assert [span for span, _ in seen] == [(D0, _days(2))]
    seen.clear()
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    df = snt.exec_batch_query(FILTER, D0, _days(2), **kwargs)
    assert [span for span, _ in seen] == [(D0, _days(1)), (_days(1), _days(2))]
    assert df["h"].tolist() == list(range(48))
    messages = [r.getMessage() for r in caplog.records]
    assert any("in an earlier run; splitting it without trying it whole" in m for m in messages)
    assert any("splitting it into two windows" in m for m in messages)


def test_batch_timed_out_windows_are_only_remembered_for_the_same_query_and_timeout(sentinel):
    cls, _ = sentinel
    seen = []
    cls.return_value.query_tables.side_effect = _slow_over(timedelta(days=1), seen)
    kwargs = {"interval": timedelta(days=2), "min_interval": timedelta(days=1)}
    snt.exec_batch_query(FILTER, D0, _days(2), **kwargs)
    seen.clear()
    snt.exec_batch_query(FILTER, D0, _days(2), timeout=200, **kwargs)  # other timeout
    assert (D0, _days(2)) in [span for span, _ in seen]
    seen.clear()
    snt.exec_batch_query(FILTER + " | take 5", D0, _days(2), **kwargs)  # other query
    assert (D0, _days(2)) in [span for span, _ in seen]
    seen.clear()
    snt.exec_batch_query(FILTER, D0, _days(2), **kwargs)
    assert (D0, _days(2)) not in [span for span, _ in seen]


def test_batch_cache_false_neither_remembers_nor_consults_timed_out_windows(sentinel):
    cls, _ = sentinel
    seen = []
    cls.return_value.query_tables.side_effect = _slow_over(timedelta(days=1), seen)
    kwargs = {"interval": timedelta(days=2), "min_interval": timedelta(days=1)}
    snt.exec_batch_query(FILTER, D0, _days(2), cache=False, **kwargs)
    seen.clear()
    snt.exec_batch_query(FILTER, D0, _days(2), cache=False, **kwargs)
    assert (D0, _days(2)) in [span for span, _ in seen]  # tried whole again
    seen.clear()
    snt.exec_batch_query(FILTER, D0, _days(2), **kwargs)  # cache=True: nothing was remembered
    assert (D0, _days(2)) in [span for span, _ in seen]


def test_clear_cache_forgets_timed_out_windows(sentinel):
    cls, _ = sentinel
    seen = []
    cls.return_value.query_tables.side_effect = _slow_over(timedelta(days=1), seen)
    kwargs = {"interval": timedelta(days=2), "min_interval": timedelta(days=1)}
    snt.exec_batch_query(FILTER, D0, _days(2), **kwargs)
    snt.clear_cache()
    seen.clear()
    snt.exec_batch_query(FILTER, D0, _days(2), **kwargs)
    assert (D0, _days(2)) in [span for span, _ in seen]


def test_batch_remembered_timeout_at_the_minimum_fails_without_a_request(sentinel):
    cls, _ = sentinel
    cls.return_value.query_tables.side_effect = _slow_over(timedelta(0))
    kwargs = {"min_interval": timedelta(days=1)}
    with pytest.raises(QueryTimeoutError, match="smallest window tried was 1 day"):
        snt.exec_batch_query(FILTER, D0, _days(1), **kwargs)
    cls.return_value.query_tables.reset_mock()
    with pytest.raises(QueryTimeoutError, match="timed out after .* s in an earlier run"):
        snt.exec_batch_query(FILTER, D0, _days(1), **kwargs)
    cls.return_value.query_tables.assert_not_called()


def test_batch_adx_timed_out_windows_are_remembered_too(adx):
    cls, _ = adx
    calls = []

    def query(kql, database, properties=None):
        calls.append(kql)
        if 'BatchStart = datetime("2026-01-01' in kql and 'BatchEnd = datetime("2026-01-03' in kql:
            raise QueryTimeoutError("ADX query failed: Request_Timeout")
        return [pd.DataFrame({"a": [len(calls)]})]

    cls.return_value.query_tables.side_effect = query
    kwargs = {"interval": timedelta(days=2), "min_interval": timedelta(days=1), "target": "adx"}
    snt.exec_batch_query(FILTER, D0, _days(2), **kwargs)
    assert len(calls) == 3  # whole (timed out), then the two halves
    snt.exec_batch_query(FILTER, D0, _days(2), **kwargs)
    assert len(calls) == 3  # the halves come from the result cache, the whole is not retried


@pytest.mark.parametrize(
    ("min_interval", "match"),
    [
        (timedelta(0), "min_interval must be a positive"),
        (timedelta(hours=-1), "min_interval must be a positive"),
        (3600, "min_interval must be a positive"),
        ("1h", "min_interval must be a positive"),
        (timedelta(days=2), r"min_interval .* must not be longer than interval"),
    ],
)
def test_batch_min_interval_validation(sentinel, min_interval, match):
    cls, _ = sentinel
    with pytest.raises(ConfigurationError, match=match):
        snt.exec_batch_query(
            FILTER, D0, _days(3), interval=timedelta(days=1), min_interval=min_interval
        )
    cls.return_value.query_tables.assert_not_called()


def test_batch_min_interval_equal_to_interval_is_valid(sentinel):
    snt.exec_batch_query(
        FILTER, D0, _days(1), interval=timedelta(days=1), min_interval=timedelta(days=1)
    )


def test_batch_min_interval_is_keyword_only_and_defaults_to_none():
    import inspect

    param = inspect.signature(snt.exec_batch_query).parameters["min_interval"]
    assert param.default is None
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_batch_min_interval_works_for_adx_late_resets(adx, backoff, clock):
    # ADX raises no QueryTimeoutError itself, but a late reset is a timeout there too.
    cls, _ = adx
    calls = []

    def query(kql, database, properties=None):
        calls.append(kql)
        if len(calls) == 1:
            clock.now += 280
            raise _reset("ADX query failed: could not reach the service")
        return [pd.DataFrame({"a": [len(calls)]})]

    cls.return_value.query_tables.side_effect = query
    df = snt.exec_batch_query(
        FILTER,
        D0,
        _days(2),
        interval=timedelta(days=2),
        min_interval=timedelta(days=1),
        target="adx",
    )
    assert df["a"].tolist() == [2, 3]


# --- retention warning --------------------------------------------------------------------------


@pytest.fixture
def retention(monkeypatch):
    from security_notebook_tools import _sentinel
    from security_notebook_tools._tablepolicy import TablePolicy

    monkeypatch.setenv("SENTINEL_WORKSPACE_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("SENTINEL_SUBSCRIPTION_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("SENTINEL_RESOURCE_GROUP", "rg")
    monkeypatch.setenv("SENTINEL_WORKSPACE_NAME", "sentinel-ws")
    policy = TablePolicy(
        customer_id="11111111-1111-1111-1111-111111111111",
        tables={"syslog": ("Syslog", "Analytics")},
        retention={"syslog": (90, 365)},
    )
    with (
        mock.patch.object(_sentinel, "get_policy", return_value=policy),
        mock.patch.object(_sentinel, "check_sentinel_query"),
        mock.patch("security_notebook_tools._query.get_credential"),
        mock.patch.object(
            _sentinel.SentinelClient, "query_tables", return_value=[pd.DataFrame({"a": [1]})]
        ),
    ):
        yield


def _retention_warnings(func, *args, **kwargs):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        func(*args, **kwargs)
    return [str(w.message) for w in caught if "Analytics" in str(w.message)]


def test_exec_query_warns_beyond_retention(retention, caplog):
    with caplog.at_level(logging.WARNING):
        got = _retention_warnings(snt.exec_query, "Syslog", timedelta(days=120))
    assert len(got) == 1
    assert got[0].startswith("Syslog keeps 90 days in Analytics (365 in total): rows before ")
    assert "Syslog" in caplog.text
    assert "\nSyslog\n" not in caplog.text


def test_exec_query_does_not_warn_within_retention(retention):
    assert _retention_warnings(snt.exec_query, "Syslog", timedelta(days=30)) == []


def test_exec_query_does_not_warn_without_timespan(retention):
    kql = "Syslog | where TimeGenerated > ago(400d)"
    assert _retention_warnings(snt.exec_query, kql) == []


def test_exec_query_warns_that_data_is_gone_beyond_total(retention):
    got = _retention_warnings(snt.exec_query, "Syslog", timedelta(days=400))
    assert len(got) == 1
    assert "gone entirely" in got[0]


def test_retention_warning_repeats_for_cached_results(retention):
    start = datetime.now(UTC) - timedelta(days=120)
    span = (start, start + timedelta(days=1))
    assert len(_retention_warnings(snt.exec_query, "Syslog", span)) == 1
    assert len(_retention_warnings(snt.exec_query, "Syslog", span)) == 1


def test_retention_check_failure_does_not_break_the_query(retention):
    from security_notebook_tools import _sentinel

    with mock.patch.object(_sentinel, "get_policy", side_effect=RuntimeError("boom")):
        # The query itself (check_sentinel_query is mocked here) still runs.
        df = snt.exec_query("Syslog", timedelta(days=120))
    assert list(df.columns) == ["a"]


def test_batch_warns_once_for_the_whole_range(retention):
    kql = "Syslog | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd"
    got = _retention_warnings(
        snt.exec_batch_query, kql, timedelta(days=120), interval=timedelta(days=30)
    )
    assert len(got) == 1


def test_batch_does_not_warn_within_retention(retention):
    kql = "Syslog | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd"
    assert (
        _retention_warnings(
            snt.exec_batch_query, kql, timedelta(days=60), interval=timedelta(days=20)
        )
        == []
    )


def test_exact_retention_range_does_not_warn(retention):
    kql = "Syslog | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd"
    assert _retention_warnings(snt.exec_query, "Syslog", timedelta(days=90)) == []
    assert _retention_warnings(snt.exec_batch_query, kql, timedelta(days=90)) == []
    assert len(_retention_warnings(snt.exec_query, "Syslog", timedelta(days=92))) == 1
    assert len(_retention_warnings(snt.exec_batch_query, kql, timedelta(days=92))) == 1


def test_retention_warning_points_at_the_caller(retention):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        snt.exec_query("Syslog", timedelta(days=120))
        kql = "Syslog | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd"
        snt.exec_batch_query(kql, timedelta(days=120), interval=timedelta(days=60))
    files = [w.filename for w in caught if "Analytics" in str(w.message)]
    assert files == [__file__, __file__]


def test_batch_min_interval_splits_adx_server_timeouts(adx):
    cls, _ = adx
    calls = []

    def query(kql, database, properties=None):
        calls.append(kql)
        if len(calls) == 1:
            raise QueryTimeoutError("ADX query failed: Request_Timeout")
        return [pd.DataFrame({"a": [len(calls)]})]

    cls.return_value.query_tables.side_effect = query
    df = snt.exec_batch_query(
        FILTER,
        D0,
        _days(2),
        interval=timedelta(days=2),
        min_interval=timedelta(days=1),
        target="adx",
    )
    assert df["a"].tolist() == [2, 3]


# --- exec_batch_query: the cost guard runs once per batch ---------------------------------------


@pytest.mark.parametrize("workers", [1, 4])
def test_batch_checks_the_cost_guard_once(monkeypatch, workers):
    from azure.monitor.query import LogsQueryResult, LogsTable

    from security_notebook_tools import _sentinel
    from security_notebook_tools._tablepolicy import TablePolicy, check_sentinel_query

    monkeypatch.setenv("SENTINEL_WORKSPACE_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("SENTINEL_SUBSCRIPTION_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("SENTINEL_RESOURCE_GROUP", "rg")
    monkeypatch.setenv("SENTINEL_WORKSPACE_NAME", "sentinel-ws")
    policy = TablePolicy(
        customer_id="11111111-1111-1111-1111-111111111111", tables={"t": ("T", "Analytics")}
    )
    table = LogsTable(name="T", columns=["a"], columns_types=["long"], rows=[[1]])
    with (
        mock.patch("security_notebook_tools._query.get_credential"),
        mock.patch.object(_sentinel, "LogsQueryClient") as sdk,
        mock.patch.object(_sentinel, "get_policy", return_value=policy) as get,
        mock.patch.object(_sentinel, "check_sentinel_query", wraps=check_sentinel_query) as check,
    ):
        sdk.return_value.query_workspace.return_value = LogsQueryResult(tables=[table])
        df = snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=5), max_workers=workers)
        assert len(df) == 5
        assert sdk.return_value.query_workspace.call_count == 5
        assert check.call_count == 1
        assert get.call_count == 2  # the retention warning and the first window
        # A new batch checks again (table plans may have changed in between).
        snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=5), cache=False)
        assert check.call_count == 2


def test_batch_cost_guard_rejection_still_stops_the_batch(monkeypatch):
    from security_notebook_tools import _sentinel
    from security_notebook_tools._tablepolicy import TablePolicy

    monkeypatch.setenv("SENTINEL_WORKSPACE_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("SENTINEL_SUBSCRIPTION_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("SENTINEL_RESOURCE_GROUP", "rg")
    monkeypatch.setenv("SENTINEL_WORKSPACE_NAME", "sentinel-ws")
    policy = TablePolicy(
        customer_id="11111111-1111-1111-1111-111111111111", tables={"t": ("T", "Basic")}
    )
    with (
        mock.patch("security_notebook_tools._query.get_credential"),
        mock.patch.object(_sentinel, "LogsQueryClient") as sdk,
        mock.patch.object(_sentinel, "get_policy", return_value=policy),
        pytest.raises(ConfigurationError, match="T"),
    ):
        snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=3), max_workers=4)
    sdk.return_value.query_workspace.assert_not_called()


def _partial_sentinel(monkeypatch):
    """Patch the SDK so every query_workspace call returns a partial result."""
    from azure.monitor.query import LogsQueryPartialResult, LogsTable

    monkeypatch.setenv("SENTINEL_WORKSPACE_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("SENTINEL_SUBSCRIPTION_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("SENTINEL_RESOURCE_GROUP", "rg")
    monkeypatch.setenv("SENTINEL_WORKSPACE_NAME", "sentinel-ws")
    table = LogsTable(name="T", columns=["a"], columns_types=["long"], rows=[[1]])
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch("security_notebook_tools._query.get_credential"))
    sdk = stack.enter_context(mock.patch("security_notebook_tools._sentinel.LogsQueryClient"))
    stack.enter_context(mock.patch("security_notebook_tools._sentinel.get_policy"))
    stack.enter_context(mock.patch("security_notebook_tools._sentinel.check_sentinel_query"))
    sdk.return_value.query_workspace.return_value = LogsQueryPartialResult(
        partial_data=[table], partial_error="boom"
    )
    return stack, sdk


def test_partial_result_is_marked_in_attrs(monkeypatch):
    stack, _ = _partial_sentinel(monkeypatch)
    with stack, pytest.warns(UserWarning, match="partial"):
        df = snt.exec_query("T")
    assert df.attrs["partial"] is True


def test_complete_results_are_marked_not_partial(sentinel, adx):
    assert snt.exec_query("T").attrs["partial"] is False
    assert snt.exec_query("T", target="adx").attrs["partial"] is False
    cls, _ = sentinel
    cls.return_value.query_tables.return_value = []
    assert snt.exec_query("T2").attrs["partial"] is False


def test_raise_on_partial_raises_with_data_and_is_not_cached(monkeypatch):
    from security_notebook_tools._exceptions import PartialResultError

    stack, sdk = _partial_sentinel(monkeypatch)
    with stack:
        for _ in range(2):
            with pytest.raises(PartialResultError, match="partial results: boom") as ei:
                snt.exec_query("T", raise_on_partial=True)
            assert ei.value.partial_error == "boom"
            assert ei.value.partial_data[0].loc[0, "a"] == 1
        assert sdk.return_value.query_workspace.call_count == 2


def test_raise_on_partial_must_be_a_bool(sentinel):
    with pytest.raises(ConfigurationError, match="raise_on_partial"):
        snt.exec_query("T", raise_on_partial="yes")
    with pytest.raises(ConfigurationError, match="raise_on_partial"):
        snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=1), raise_on_partial=1)


def test_batch_partial_windows_are_summarised_and_marked(monkeypatch):
    stack, sdk = _partial_sentinel(monkeypatch)
    with stack, warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("default")  # the default filter dedupes repeats
        df = snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=3))
    assert sdk.return_value.query_workspace.call_count == 3
    assert df.attrs["partial"] is True
    assert df.attrs["partial_windows"] == [
        (D0 + timedelta(days=i), D0 + timedelta(days=i + 1)) for i in range(3)
    ]
    summary = [str(w.message) for w in caught if "Batch query returned partial" in str(w.message)]
    assert len(summary) == 1
    assert "3 of 3 windows" in summary[0]


def test_batch_partial_survives_aggregation(monkeypatch):
    from azure.monitor.query import LogsQueryPartialResult, LogsTable

    stack, sdk = _partial_sentinel(monkeypatch)
    table = LogsTable(
        name="T", columns=["k", "n"], columns_types=["string", "long"], rows=[["x", 2]]
    )
    sdk.return_value.query_workspace.return_value = LogsQueryPartialResult(
        partial_data=[table], partial_error="boom"
    )
    with stack, pytest.warns(UserWarning, match="2 of 2 windows"):
        df = snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=2), merge_on="k", agg_column="n")
    assert df["n"].tolist() == [4]
    assert df.attrs["partial"] is True
    assert len(df.attrs["partial_windows"]) == 2


def test_batch_complete_is_marked_not_partial(sentinel):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        df = snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=2))
    assert df.attrs["partial"] is False
    assert df.attrs["partial_windows"] == []


def test_batch_raise_on_partial_names_the_window_and_does_not_retry(monkeypatch):
    from security_notebook_tools._exceptions import PartialResultError

    stack, sdk = _partial_sentinel(monkeypatch)
    with stack, pytest.raises(PartialResultError, match="window 1/2") as ei:
        snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=2), raise_on_partial=True)
    assert sdk.return_value.query_workspace.call_count == 1
    assert ei.value.partial_error == "boom"
    assert ei.value.partial_data[0].loc[0, "a"] == 1


def test_mutating_nested_dynamic_values_does_not_change_the_cache(sentinel):
    cls, _ = sentinel
    frame = pd.DataFrame({"Props": [{"x": {"y": 1}}, ["l"], None, "s"], "n": [1, 2, 3, 4]})
    cls.return_value.query_tables.return_value = [frame]
    first = snt.exec_query("T")
    first.loc[0, "Props"]["x"]["y"] = 99
    first.loc[1, "Props"].append("evil")
    second = snt.exec_query("T")
    assert _sdk_calls(cls) == 1  # the second call came from the cache
    assert second.loc[0, "Props"] == {"x": {"y": 1}}
    assert second.loc[1, "Props"] == ["l"]
    assert second.loc[3, "Props"] == "s"
    assert second.loc[0, "Props"] is not first.loc[0, "Props"]
    # and the very first result is not the cached object either
    assert frame.loc[0, "Props"] == {"x": {"y": 1}}


def test_batch_nested_dynamic_values_are_protected_on_cache_hits(sentinel):
    cls, _ = sentinel
    cls.return_value.query_tables.return_value = [pd.DataFrame({"P": [{"a": 1}]})]
    first = snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=1))
    first.loc[0, "P"]["a"] = 2
    second = snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=1))
    assert second.loc[0, "P"] == {"a": 1}


def test_partial_result_with_no_tables(sentinel):
    from security_notebook_tools import _query

    with mock.patch.object(_query, "_cached_call", side_effect=_query._PartialResult((), "boom")):
        with pytest.warns(UserWarning, match="boom"):
            df = snt.exec_query("T")
        assert df.empty
        assert df.attrs["partial"] is True


def test_raise_on_partial_is_ignored_on_adx(adx):
    assert snt.exec_query("T", target="adx", raise_on_partial=True).attrs["partial"] is False


def test_detach_containers_with_a_non_range_index_and_kql_types():
    from security_notebook_tools._query import _copies

    frame = pd.DataFrame(
        {"P": [{"a": 1}, {"a": 2}], "S": ["x", "y"], "U": [{"u": 1}, {"u": 2}]},
        index=[10, 20],
    )
    frame.attrs["kql_types"] = {"P": "dynamic", "S": "string"}  # U unknown: still scanned
    (copy,) = _copies([frame])
    copy.loc[10, "P"]["a"] = 99
    copy.loc[20, "U"]["u"] = 99
    assert frame.loc[10, "P"] == {"a": 1}
    assert frame.loc[20, "U"] == {"u": 2}
    assert copy.index.tolist() == [10, 20]


# --- batch window store -------------------------------------------------------------------------


def _fail_window(cls, n, error=None):
    """Make the SDK fail every call for the 1-based window ``n`` of a daily batch from D0."""
    ok = cls.return_value.query_tables.return_value

    def query_tables(kql, timespan, **kwargs):
        if _window_no(timespan) == n:
            raise error or QueryError("boom")
        return ok

    cls.return_value.query_tables.side_effect = query_tables
    return ok


def _heal(cls, ok):
    cls.return_value.query_tables.side_effect = None
    cls.return_value.query_tables.return_value = ok


def _batch_n(n, days=2, **kwargs):
    """Run a distinct batch query (its KQL ends in a comment ``n``) over ``days`` daily windows."""
    return snt.exec_batch_query(f"{FILTER} // {n}", D0, _days(days), **kwargs)


def test_long_batch_rerun_after_late_failure_sends_only_the_rest(sentinel):
    from security_notebook_tools._query import RESULT_CACHE_SIZE

    cls, _ = sentinel
    days = RESULT_CACHE_SIZE + 8
    ok = _fail_window(cls, days - 4)
    with pytest.raises(QueryError, match=f"window {days - 4}/{days}"):
        snt.exec_batch_query(FILTER, D0, _days(days))
    assert _sdk_calls(cls) == days - 4
    _heal(cls, ok)
    snt.exec_batch_query(FILTER, D0, _days(days))
    assert [_window_no(w) for w in _sent_windows(cls)[days - 4 :]] == list(
        range(days - 4, days + 1)
    )
    assert _sdk_calls(cls) == days - 4 + 5


def test_long_adx_batch_rerun_after_late_failure_sends_only_the_rest(adx):
    from security_notebook_tools._query import RESULT_CACHE_SIZE

    cls, _ = adx
    days = RESULT_CACHE_SIZE + 8
    kql = "T | where Timestamp >= BatchStart and Timestamp < BatchEnd"
    ok = cls.return_value.query_tables.return_value
    cls.return_value.query_tables.side_effect = [ok] * (days - 5) + [QueryError("boom")]
    with pytest.raises(QueryError, match=f"window {days - 4}/{days}"):
        snt.exec_batch_query(kql, D0, _days(days), target="adx")
    cls.return_value.query_tables.side_effect = None
    before = cls.return_value.query_tables.call_count
    snt.exec_batch_query(kql, D0, _days(days), target="adx")
    assert cls.return_value.query_tables.call_count - before == 5


def test_batch_windows_survive_many_intervening_queries(sentinel):
    from security_notebook_tools._query import RESULT_CACHE_SIZE

    cls, _ = sentinel
    _batch_n(0, days=3)
    for i in range(RESULT_CACHE_SIZE + 5):
        snt.exec_query(f"T | take {i + 1}")
    sent = _sdk_calls(cls)
    _batch_n(0, days=3)
    assert _sdk_calls(cls) == sent


def test_batch_store_keeps_the_four_most_recent_batch_queries(sentinel, caplog):
    cls, _ = sentinel
    for n in range(4):
        _batch_n(n, days=3)
    assert _sdk_calls(cls) == 12
    with caplog.at_level(logging.INFO, logger="security_notebook_tools.query"):
        _batch_n(4, days=3)  # drops batch 0, the least recently used
    assert "Dropped 3 stored window(s) of an older batch query" in caplog.text
    assert _sdk_calls(cls) == 15
    for n in (1, 2, 3, 4):
        _batch_n(n, days=3)
    assert _sdk_calls(cls) == 15
    _batch_n(0, days=3)
    assert _sdk_calls(cls) == 18


def test_reusing_a_batch_query_makes_it_the_most_recent(sentinel):
    cls, _ = sentinel
    for n in range(4):
        _batch_n(n)
    _batch_n(0)  # batch 1 is now the least recently used
    _batch_n(4)
    assert _sdk_calls(cls) == 10
    _batch_n(0)
    assert _sdk_calls(cls) == 10
    _batch_n(1)
    assert _sdk_calls(cls) == 12


def test_batch_cache_false_neither_reads_nor_writes_the_store(sentinel):
    cls, _ = sentinel
    _batch_n(0, cache=False)
    assert _sdk_calls(cls) == 2
    _batch_n(0)  # nothing was written
    assert _sdk_calls(cls) == 4
    _batch_n(0, cache=False)  # stored windows are not read
    assert _sdk_calls(cls) == 6
    _batch_n(0)
    assert _sdk_calls(cls) == 6


def test_clear_cache_empties_the_batch_store(sentinel, caplog):
    cls, _ = sentinel
    _batch_n(0, days=3)
    with caplog.at_level(logging.INFO, logger="security_notebook_tools"):
        snt.clear_cache()
    assert "clear_cache: dropped 3 stored result(s)" in caplog.text
    _batch_n(0, days=3)
    assert _sdk_calls(cls) == 6


def test_batch_hit_is_logged_and_returns_independent_copies(sentinel, caplog):
    _batch_n(0)
    with caplog.at_level(logging.INFO, logger="security_notebook_tools.query"):
        first = _batch_n(0)
    assert "Returning cached window" in caplog.text
    first.loc[0, "a"] = 99
    assert _batch_n(0).loc[0, "a"] == 1


def test_batch_partial_window_is_not_kept(monkeypatch):
    from azure.monitor.query import LogsQueryPartialResult, LogsQueryResult, LogsTable

    monkeypatch.setenv("SENTINEL_WORKSPACE_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("SENTINEL_SUBSCRIPTION_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("SENTINEL_RESOURCE_GROUP", "rg")
    monkeypatch.setenv("SENTINEL_WORKSPACE_NAME", "sentinel-ws")
    table = LogsTable(name="T", columns=["a"], columns_types=["long"], rows=[[1]])
    full = LogsQueryResult(tables=[table])
    partial = LogsQueryPartialResult(partial_data=[table], partial_error="boom")

    def query_workspace(workspace_id, kql, timespan=None, **kwargs):
        return partial if _window_no(timespan) == 2 else full

    with (
        mock.patch("security_notebook_tools._query.get_credential"),
        mock.patch("security_notebook_tools._sentinel.LogsQueryClient") as sdk,
        mock.patch("security_notebook_tools._sentinel.get_policy"),
        mock.patch("security_notebook_tools._sentinel.check_sentinel_query"),
        warnings.catch_warnings(),
    ):
        warnings.simplefilter("ignore")
        sdk.return_value.query_workspace.side_effect = query_workspace
        snt.exec_batch_query(FILTER, D0, _days(3))
        assert sdk.return_value.query_workspace.call_count == 3
        snt.exec_batch_query(FILTER, D0, _days(3))
        sent = [
            _window_no(c.kwargs.get("timespan") or c.args[2])
            for c in sdk.return_value.query_workspace.call_args_list
        ]
    assert sent[3:] == [2]  # only the partial window ran again


def test_batch_parallel_failure_keeps_the_windows_that_finished(sentinel):
    cls, _ = sentinel
    days = 6
    ok = cls.return_value.query_tables.return_value
    others_done = threading.Semaphore(0)

    def query_tables(kql, timespan, **kwargs):
        n = _window_no(timespan)
        if n == days:
            for _ in range(days - 2):  # windows 2..5 have all finished
                assert others_done.acquire(timeout=10)
            raise QueryError("boom")
        if n > 1:
            others_done.release()
        return ok

    cls.return_value.query_tables.side_effect = query_tables
    with pytest.raises(QueryError, match=f"window {days}/{days}"):
        snt.exec_batch_query(FILTER, D0, _days(days), max_workers=3)
    assert _sdk_calls(cls) == days
    _heal(cls, ok)
    snt.exec_batch_query(FILTER, D0, _days(days), max_workers=3)
    assert [_window_no(w) for w in _sent_windows(cls)[days:]] == [days]


def test_a_successful_batch_keeps_only_the_windows_of_its_run(sentinel):
    cls, _ = sentinel
    snt.exec_batch_query(FILTER, D0, _days(2))  # windows 1-2
    snt.exec_batch_query(FILTER, _days(2), _days(4))  # windows 3-4, same query: 1-2 dropped
    assert _sdk_calls(cls) == 4
    snt.exec_batch_query(FILTER, _days(2), _days(4))
    assert _sdk_calls(cls) == 4
    snt.exec_batch_query(FILTER, D0, _days(2))
    assert _sdk_calls(cls) == 6


def test_a_failed_batch_does_not_drop_earlier_windows_of_the_query(sentinel):
    cls, _ = sentinel
    ok = cls.return_value.query_tables.return_value
    snt.exec_batch_query(FILTER, D0, _days(2))  # windows 1-2
    _fail_window(cls, 4)
    with pytest.raises(QueryError, match="window 2/2"):
        snt.exec_batch_query(FILTER, _days(2), _days(4))
    _heal(cls, ok)
    sent = _sdk_calls(cls)
    snt.exec_batch_query(FILTER, D0, _days(2))
    assert _sdk_calls(cls) == sent  # windows 1-2 still stored
