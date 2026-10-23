"""Logging at the public boundaries, the cache and the batch, and that no KQL text is logged."""

import logging
from datetime import UTC, datetime, timedelta
from unittest import mock

import pandas as pd
import pytest
from azure.monitor.query import LogsTable

import security_notebook_tools as snt
from security_notebook_tools import _batch, _log
from security_notebook_tools._errors import public_call, unexpected
from security_notebook_tools._exceptions import (
    ConfigurationError,
    PartialResultError,
    QueryError,
    QueryThrottledError,
)
from security_notebook_tools._log import fingerprint

D0 = datetime(2026, 1, 1, tzinfo=UTC)
FILTER = "T | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd"
SECRET_KQL = (
    "T | where Email == 'bob.smith@contoso.com' and Ip == '203.0.113.77' "
    '| where Note == "topsecretvalue" | take 5'
)
LEAKS = ("bob.smith", "203.0.113.77", "topsecretvalue", "Email ==", "contoso")
child = logging.getLogger("security_notebook_tools.test")


@pytest.fixture(autouse=True)
def _info(caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")


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
        cls.return_value.query_tables.return_value = [pd.DataFrame({"a": [1, 2], "b": [3, 4]})]
        yield cls.return_value


@pytest.fixture
def adx(monkeypatch):
    monkeypatch.setenv("ADX_CLUSTER_URI", "https://env.kusto.windows.net")
    monkeypatch.setenv("ADX_DATABASE", "db1")
    with (
        mock.patch("security_notebook_tools._query.ADXClient") as cls,
        mock.patch("security_notebook_tools._query.get_credential"),
    ):
        cls.return_value.query_tables.return_value = [pd.DataFrame({"a": [1]})]
        yield cls.return_value


def _at_least(caplog, level):
    return [r for r in caplog.records if r.levelno >= level]


def _text(caplog, name=None):
    return "\n".join(
        r.getMessage() for r in caplog.records if name is None or r.name.endswith(name)
    )


def _only(records):
    assert len(records) == 1, [(r.levelname, r.name, r.getMessage()) for r in records]
    return records[0]


# --- public_call --------------------------------------------------------------------------------


def test_public_call_logs_entry_and_summary(caplog):
    with public_call(child, "thing", target="sentinel", kql=fingerprint("T | take 1")) as call:
        call.update(rows=2, columns=3)
    started, finished = caplog.records
    assert started.getMessage() == (
        f"thing: started target=sentinel kql={fingerprint('T | take 1')}"
    )
    assert finished.levelno == logging.INFO
    assert finished.getMessage().startswith("thing: finished in ")
    assert finished.getMessage().endswith("s rows=2 columns=3")


def test_public_call_rejection_is_one_warning(caplog):
    with pytest.raises(ConfigurationError), public_call(child, "thing"):
        raise ConfigurationError("bad value 'abc' for bob@contoso.com")
    record = _only(_at_least(caplog, logging.WARNING))
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert "thing: rejected after" in record.getMessage()
    assert "ConfigurationError: bad value '…' for <email>" in record.getMessage()


def test_public_call_failure_is_one_error_without_traceback(caplog):
    boom = QueryError("Sentinel query failed: 'select secret' nope")
    with pytest.raises(QueryError) as info, public_call(child, "thing"):
        raise boom
    assert info.value is boom
    record = _only(_at_least(caplog, logging.WARNING))
    assert record.levelno == logging.ERROR
    assert record.exc_info is None
    assert "thing: failed after" in record.getMessage()
    assert "secret" not in record.getMessage()


def test_public_call_other_exception_is_one_error_with_traceback(caplog):
    boom = KeyError("k")
    with pytest.raises(KeyError), public_call(child, "thing"):
        raise boom
    record = _only(_at_least(caplog, logging.WARNING))
    assert record.levelno == logging.ERROR
    assert record.exc_info[1] is boom


def test_public_call_does_not_log_an_unexpected_error_twice(caplog):
    with pytest.raises(QueryError, match="Unexpected RuntimeError"), public_call(child, "thing"):
        raise unexpected(child, "the thing", RuntimeError("boom"))
    record = _only(_at_least(caplog, logging.WARNING))
    assert record.exc_info is not None
    assert "please report it" in record.getMessage()


def test_public_call_nested_logs_the_failure_once(caplog):
    with pytest.raises(QueryError), public_call(child, "outer"), public_call(child, "inner"):
        raise QueryError("nope")
    record = _only(_at_least(caplog, logging.WARNING))
    assert record.getMessage().startswith("inner: failed")


def test_public_call_logs_a_reused_exception_each_time(caplog):
    boom = QueryError("nope")
    for _ in range(2):
        with pytest.raises(QueryError), public_call(child, "thing"):
            raise boom
    assert len(_at_least(caplog, logging.ERROR)) == 2


def test_public_call_rejected_types_can_be_extended(caplog):
    with pytest.raises(ValueError), public_call(child, "thing", rejected=(ValueError,)):
        raise ValueError("x")
    assert _only(_at_least(caplog, logging.WARNING)).levelno == logging.WARNING


def test_public_call_interrupt_is_a_warning(caplog):
    with pytest.raises(KeyboardInterrupt), public_call(child, "thing"):
        raise KeyboardInterrupt
    assert _only(_at_least(caplog, logging.WARNING)).getMessage().startswith("thing: interrupted")


# --- exec_query ---------------------------------------------------------------------------------


def test_exec_query_logs_entry_and_result_summary(sentinel, caplog):
    snt.exec_query(SECRET_KQL)
    text = _text(caplog, "query")
    assert "exec_query: started target=sentinel timeout=300 cache=True kql=hmac:" in text
    assert "exec_query: finished in " in text
    assert "tables=1 rows=2 columns=2" in text
    assert not _at_least(caplog, logging.WARNING)


def test_exec_query_rejection_is_one_warning(sentinel, caplog):
    with pytest.raises(ConfigurationError):
        snt.exec_query("T", timeout=0)
    record = _only(_at_least(caplog, logging.WARNING))
    assert (record.levelno, record.name) == (logging.WARNING, "security_notebook_tools.query")
    assert "exec_query: rejected after" in record.getMessage()
    sentinel.query_tables.assert_not_called()


@pytest.mark.parametrize(
    "call",
    [
        lambda: snt.exec_query("T", target="nope"),
        lambda: snt.exec_query("T", timedelta(days=1), target="adx"),
    ],
)
def test_exec_query_argument_rejections_are_warnings(sentinel, caplog, call):
    with pytest.raises(ConfigurationError):
        call()
    assert _only(_at_least(caplog, logging.WARNING)).levelno == logging.WARNING


def test_exec_query_environment_rejection_masks_the_value(sentinel, caplog, monkeypatch):
    monkeypatch.setenv("SENTINEL_WORKSPACE_ID", "not-a-guid-hunter2")
    with pytest.raises(ConfigurationError):
        snt.exec_query("T")
    record = _only(_at_least(caplog, logging.WARNING))
    assert "hunter2" not in record.getMessage()
    assert "SENTINEL_WORKSPACE_ID" in record.getMessage()


def test_exec_query_failure_is_one_error(sentinel, caplog):
    sentinel.query_tables.side_effect = QueryError("Sentinel query failed: near 'topsecretvalue'")
    with pytest.raises(QueryError):
        snt.exec_query(SECRET_KQL)
    record = _only(_at_least(caplog, logging.ERROR))
    assert record.name == "security_notebook_tools.query"
    assert "exec_query: failed after" in record.getMessage()
    assert "topsecretvalue" not in caplog.text


def test_exec_query_unexpected_error_is_one_error_with_traceback(sentinel, caplog):
    boom = RuntimeError("boom 'topsecretvalue'")
    sentinel.query_tables.side_effect = boom
    with pytest.raises(RuntimeError):
        snt.exec_query(SECRET_KQL)
    record = _only(_at_least(caplog, logging.ERROR))
    assert record.exc_info[1] is boom


@pytest.mark.parametrize("target", ["sentinel", "adx"])
def test_exec_query_never_logs_kql(request, caplog, target):
    caplog.set_level(logging.DEBUG, logger="security_notebook_tools")
    client = request.getfixturevalue(target)
    snt.exec_query(SECRET_KQL, target=target)
    snt.exec_query(SECRET_KQL, target=target)  # a cache hit
    snt.exec_query(SECRET_KQL, target=target, cache=False)
    method = "query_tables"
    getattr(client, method).side_effect = QueryError("failed near 'topsecretvalue' bob.smith@x.io")
    with pytest.raises(QueryError):
        snt.exec_query(SECRET_KQL + " | take 1", target=target)
    for leak in LEAKS:
        assert leak not in caplog.text
    assert "kql=hmac:" in caplog.text


def test_result_cache_hit_miss_and_bypass_are_logged(sentinel, caplog):
    snt.exec_query("T")
    assert "Result cache miss" in _text(caplog, "query")
    assert "cached" not in _text(caplog, "query")
    snt.exec_query("T")
    assert "Returning cached result" in _text(caplog, "query")
    snt.exec_query("T", cache=False)
    assert "Result cache bypassed (cache=False)" in _text(caplog, "query")


def test_partial_result_is_not_cached_and_says_so(sentinel, caplog):
    table = LogsTable(name="T", columns=["a"], columns_types=["long"], rows=[[1]])
    sentinel.query_tables.side_effect = PartialResultError("partial", partial_data=[table])
    with pytest.warns(UserWarning, match="partial"):
        snt.exec_query("T")
    assert "not stored in the result cache" in _text(caplog, "query")


def test_clear_cache_is_logged_at_info(sentinel, caplog):
    snt.exec_query("T")
    caplog.clear()
    snt.clear_cache()
    record = _only([r for r in caplog.records if "clear_cache" in r.getMessage()])
    assert record.levelno == logging.INFO
    assert "dropped 1 stored result(s)" in record.getMessage()


def test_cached_client_reuse_is_logged_at_debug(sentinel, caplog):
    caplog.set_level(logging.DEBUG, logger="security_notebook_tools")
    snt.exec_query("T")
    assert "Reusing the cached" not in caplog.text
    snt.exec_query("T", cache=False)
    assert "Reusing the cached" in caplog.text


# --- exec_batch_query ---------------------------------------------------------------------------


@pytest.fixture
def backoff(monkeypatch):
    monkeypatch.setattr(_batch, "_sleep", lambda delay, stop: False)
    monkeypatch.setattr(_batch.random, "uniform", lambda low, high: 0.0)


def test_batch_logs_entry_and_summary(sentinel, caplog):
    snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=2))
    text = _text(caplog, "batch")
    assert "exec_batch_query: started target=sentinel interval=1 day, 0:00:00" in text
    assert "kql=hmac:" in text
    assert "exec_batch_query: finished in" in text
    assert "windows=2 rows=4 columns=2" in text
    assert not _at_least(caplog, logging.WARNING)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"interval": timedelta(0)},
        {"timeout": 0},
        {"max_workers": 9},
        {"merge_on": "a"},
        {"target": "nope"},
    ],
)
def test_batch_rejections_are_one_warning(sentinel, caplog, kwargs):
    with pytest.raises(ConfigurationError):
        snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=2), **kwargs)
    record = _only(_at_least(caplog, logging.WARNING))
    assert (record.levelno, record.name) == (logging.WARNING, "security_notebook_tools.batch")
    assert "exec_batch_query: rejected" in record.getMessage()


def test_batch_filter_rejection_is_one_warning(sentinel, caplog):
    with pytest.raises(ConfigurationError):
        snt.exec_batch_query("T | take 1", D0, D0 + timedelta(days=2))
    assert _only(_at_least(caplog, logging.WARNING)).levelno == logging.WARNING


def test_batch_window_failure_is_a_window_warning_and_one_error(sentinel, caplog, backoff):
    sentinel.query_tables.side_effect = QueryError("Sentinel query failed: nope")
    with pytest.raises(QueryError, match="window 1/2"):
        snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=2))
    (error,) = _at_least(caplog, logging.ERROR)
    assert "exec_batch_query: failed after" in error.getMessage()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage().split(" and is")[0] for r in warnings] == ["Batch 1/2 failed"]


def test_batch_retries_and_exhaustion_are_warnings_with_one_error(sentinel, caplog, backoff):
    sentinel.query_tables.side_effect = QueryThrottledError("Sentinel query failed: 429")
    with pytest.raises(QueryError, match="failed after 4 retries"):
        snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=1))
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert sum("throttled; retrying" in m for m in messages) == 4
    assert sum("still failing after 4 retries" in m for m in messages) == 1
    assert len(_at_least(caplog, logging.ERROR)) == 1


def test_batch_unexpected_error_is_logged_once(sentinel, caplog, backoff):
    boom = RuntimeError("boom")
    sentinel.query_tables.side_effect = boom
    with pytest.raises(QueryError, match="unexpected RuntimeError"):
        snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=2))
    record = _only(_at_least(caplog, logging.ERROR))
    assert record.exc_info[1] is boom


def test_batch_fail_fast_cancellation_is_logged(sentinel, caplog, backoff):
    ok = [pd.DataFrame({"a": [1]})]

    def query(kql, timespan, raise_on_partial=False, server_timeout=None):
        if timespan[0] == D0 + timedelta(days=1):
            raise QueryError("Sentinel query failed: nope")
        return ok

    sentinel.query_tables.side_effect = query
    with pytest.raises(QueryError, match="window 2/6"):
        snt.exec_batch_query(FILTER, D0, D0 + timedelta(days=6), max_workers=1 + 1)
    messages = _text(caplog, "batch")
    assert "Batch 2/6 failed and is not retried" in messages
    assert len(_at_least(caplog, logging.ERROR)) == 1


def test_batch_never_logs_kql(sentinel, caplog, backoff):
    caplog.set_level(logging.DEBUG, logger="security_notebook_tools")
    kql = FILTER + " | where Email == 'bob.smith@contoso.com' and Ip == '203.0.113.77'"
    kql += ' | where Note == "topsecretvalue"'
    snt.exec_batch_query(kql, D0, D0 + timedelta(days=2))
    sentinel.query_tables.side_effect = QueryError("failed near 'topsecretvalue' bob.smith@x.io")
    with pytest.raises(QueryError):
        snt.exec_batch_query(kql, D0, D0 + timedelta(days=3), cache=False)
    for leak in LEAKS:
        assert leak not in caplog.text


# --- format_dataframe, pivot_dataframe -----------------------------------------------------------


def test_format_dataframe_logs_entry_and_summary(caplog):
    snt.format_dataframe(pd.DataFrame({"a": [1, 2, 3]}), max_rows=2)
    text = _text(caplog, "format")
    assert "format_dataframe: started rows=3 columns=1 max_rows=2" in text
    assert "pandas Styler: done in" in text
    assert "format_dataframe: finished in" in text
    assert "shown_rows=2" in text
    assert not _at_least(caplog, logging.WARNING)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"max_rows": 0}, ValueError),
        ({"max_rows": -1}, ValueError),
    ],
)
def test_format_dataframe_rejections_are_one_warning(caplog, kwargs, error):
    with pytest.raises(error):
        snt.format_dataframe(pd.DataFrame({"a": [1]}), **kwargs)
    record = _only(_at_least(caplog, logging.WARNING))
    assert (record.levelno, record.name) == (logging.WARNING, "security_notebook_tools.format")
    assert "format_dataframe: rejected" in record.getMessage()


def test_format_dataframe_never_logs_values(caplog):
    caplog.set_level(logging.DEBUG, logger="security_notebook_tools")
    df = pd.DataFrame({"user": ["bob.smith@contoso.com"], "ip": ["203.0.113.77"]})
    snt.format_dataframe(df)
    for leak in ("bob.smith", "203.0.113.77"):
        assert leak not in caplog.text


def test_pivot_dataframe_logs_entry_and_summary(caplog):
    df = pd.DataFrame({"u": ["a", "a", "b"], "k": ["x", "y", "x"], "n": [1, 2, 3]})
    snt.pivot_dataframe(df, "u", "k", "n", totals=True)
    text = _text(caplog, "pivot")
    assert "pivot_dataframe: started rows=3 columns=3 index=u pivot_columns=k values=n" in text
    assert "pandas groupby aggregation: done in" in text
    assert "pandas pivot reshape: done in" in text
    assert "pivot_dataframe: finished in" in text
    assert "rows=3 columns=4" in text
    assert not _at_least(caplog, logging.WARNING)


@pytest.mark.parametrize(
    "call",
    [
        lambda df: snt.pivot_dataframe(df, "missing", "k"),
        lambda df: snt.pivot_dataframe("not a frame", "u", "k"),
        lambda df: snt.pivot_dataframe(df, "u", "k", max_columns=0),
        lambda df: snt.pivot_dataframe(df, "u", "u"),
    ],
)
def test_pivot_dataframe_rejections_are_one_warning(caplog, call):
    df = pd.DataFrame({"u": ["a"], "k": ["x"], "n": [1]})
    with pytest.raises(ConfigurationError):
        call(df)
    record = _only(_at_least(caplog, logging.WARNING))
    assert (record.levelno, record.name) == (logging.WARNING, "security_notebook_tools.pivot")
    assert "pivot_dataframe: rejected" in record.getMessage()


def test_pivot_dataframe_aggregation_failure_is_a_warning_then_one_rejection(caplog):
    df = pd.DataFrame({"u": ["a"], "k": ["x"], "n": ["text"]})
    with pytest.raises(ConfigurationError, match="Cannot aggregate"):
        snt.pivot_dataframe(df, "u", "k", "n", agg_func="mean")
    warnings = _at_least(caplog, logging.WARNING)
    assert [r.name.rsplit(".", 1)[1] for r in warnings] == ["pivot", "pivot"]
    assert "pandas groupby aggregation: failed" in warnings[0].getMessage()
    assert "pivot_dataframe: rejected" in warnings[1].getMessage()
    assert not _at_least(caplog, logging.ERROR)


def test_logger_names_cover_every_module():
    import importlib
    import pkgutil

    import security_notebook_tools as pkg

    names = set()
    pytest.importorskip("IPython")  # _magic needs it
    for info in pkgutil.iter_modules(pkg.__path__):
        module = importlib.import_module(f"security_notebook_tools.{info.name}")
        names.add((info.name, getattr(module, "logger", None)))
    missing = {n for n, lg in names if lg is None}
    # no logger of their own: the exception types, the tokenizer, and the helpers that take the
    # caller's logger
    assert missing <= {"_errors", "_exceptions", "_kql"}, missing
    for name, lg in names:
        if lg is not None and name != "_log":
            assert lg.name == f"security_notebook_tools.{name.lstrip('_')}"
    assert _log.PACKAGE_LOGGER == "security_notebook_tools"


# --- values that must not reach the log ---------------------------------------------------------


class Secretive:
    def __repr__(self):
        return "topsecretvalue"

    __str__ = __repr__


def test_timespan_value_is_never_logged(sentinel, caplog, _log_file):
    caplog.set_level(logging.DEBUG, logger="security_notebook_tools")
    snt.exec_query("T | where TimeGenerated > ago(1d)", Secretive())  # warns: both apply
    snt.exec_query("T", Secretive(), cache=False)
    logged = _log_file.read_text(encoding="utf-8") + "\n".join(
        r.getMessage() for r in caplog.records
    )
    assert "topsecretvalue" not in logged
    assert "<Secretive>" in logged


def test_timespan_durations_are_logged_as_durations(sentinel, caplog):
    snt.exec_query("T | where TimeGenerated > ago(1d)", timedelta(hours=3))
    assert "timespan=3:00:00" in caplog.text


def test_public_call_survives_fields_that_cannot_be_printed(caplog):
    class Boom:
        def __str__(self):
            raise RuntimeError("no")

        __repr__ = __str__

    with public_call(child, "thing", bad=Boom(), worse=[Boom()]):
        pass
    assert caplog.records[0].getMessage() == "thing: started bad=<Boom> worse=[<Boom>]"
    from security_notebook_tools import _errors

    assert getattr(_errors._public, "depth", 0) == 0
