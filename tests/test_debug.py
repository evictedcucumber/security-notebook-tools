"""Per-call ``debug=True``: package logs printed to the notebook cell, never to the file."""

import io
import logging
import re
import sys
import threading
from datetime import UTC, datetime, timedelta
from unittest import mock

import pandas as pd
import pytest
from azure.monitor.query import LogsQueryResult, LogsTable

import security_notebook_tools as snt
from security_notebook_tools import _log
from security_notebook_tools._errors import public_call
from security_notebook_tools._exceptions import ConfigurationError, QueryError

D0 = datetime(2026, 1, 1, tzinfo=UTC)
FILTER = "T | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd"
WS = "11111111-1111-1111-1111-111111111111"
SECRET_KQL = "T | where Email == 'bob.smith@contoso.com' and Ip == '203.0.113.77' | take 5"
LEAKS = ("bob.smith", "203.0.113.77", "contoso", "Email ==")
LINE = re.compile(
    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z (DEBUG|INFO|WARNING|ERROR) security_notebook_tools"
)
child = logging.getLogger("security_notebook_tools.test")


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


def _lines(text):
    return [line for line in text.splitlines() if line]


def _days(n):
    return D0 + timedelta(days=n)


# --- off by default -----------------------------------------------------------------------------


def test_package_logger_is_debug_but_the_file_handler_is_info():
    assert _log.logger.level == logging.DEBUG
    assert _log.file_handler.level == logging.INFO
    assert _log.cell_handler.level == logging.DEBUG


def test_nothing_is_printed_by_default(sentinel, capsys):
    df = snt.exec_query("T")
    snt.format_dataframe(df)
    snt.pivot_dataframe(df, "a", "b")
    snt.exec_batch_query(FILTER, D0, _days(3), max_workers=2)
    snt.exec_query("T", debug=False)
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""
    assert not _log.debug_active()


def test_nothing_is_printed_for_a_failing_call_by_default(capsys):
    with pytest.raises(ConfigurationError):
        snt.exec_query("T", timeout=0)
    assert capsys.readouterr().out == ""


# --- what is printed ----------------------------------------------------------------------------


def test_debug_prints_debug_and_info_lines_of_the_call_and_its_dependencies(sentinel, capsys):
    snt.exec_query("T", debug=True)
    lines = _lines(capsys.readouterr().out)
    assert lines
    assert all(LINE.match(line) for line in lines), lines
    text = "\n".join(lines)
    assert " INFO security_notebook_tools.query: exec_query: started " in text
    assert " INFO security_notebook_tools.query: exec_query: finished in " in text
    assert " DEBUG security_notebook_tools." in text  # internal detail
    assert " DEBUG security_notebook_tools.config: SENTINEL_WORKSPACE_ID: read from" in text
    assert " INFO security_notebook_tools.query: Result cache miss" in text
    assert "11111111-1111" not in text  # identifiers are not written


def test_debug_output_after_a_warm_cache_shows_the_reuse(sentinel, capsys):
    snt.exec_query("T")
    capsys.readouterr()
    snt.exec_query("T", debug=True)
    text = capsys.readouterr().out
    assert " INFO security_notebook_tools.query: Returning cached result" in text


def test_format_and_pivot_print_with_debug(capsys):
    df = pd.DataFrame({"a": ["x", "y"], "b": ["p", "q"]})
    snt.format_dataframe(df, debug=True)
    out = capsys.readouterr().out
    assert " INFO security_notebook_tools.format: format_dataframe: started " in out
    assert "pandas Styler: calling" in out
    snt.pivot_dataframe(df, "a", "b", debug=True)
    out = capsys.readouterr().out
    assert " INFO security_notebook_tools.pivot: pivot_dataframe: started " in out


def test_the_cell_stream_is_the_stdout_at_entry(sentinel):
    cell = io.StringIO()
    elsewhere = io.StringIO()
    with mock.patch.object(sys, "stdout", cell), _log.debug_scope(True):
        with mock.patch.object(sys, "stdout", elsewhere):
            child.info("late line")
        child.debug("early line")
    assert "late line" in cell.getvalue()
    assert "early line" in cell.getvalue()
    assert elsewhere.getvalue() == ""


def test_azure_records_are_not_printed(capsys):
    sdk = logging.getLogger("azure.core.test")
    with _log.debug_scope(True):
        sdk.warning("sdk warning")
        child.warning("ours")
    out = capsys.readouterr().out
    assert "ours" in out
    assert "sdk warning" not in out


# --- the scope ----------------------------------------------------------------------------------


def test_scope_ends_when_the_call_returns(sentinel, capsys):
    snt.exec_query("T", debug=True)
    capsys.readouterr()
    assert not _log.debug_active()
    snt.exec_query("T2")
    child.debug("later")
    child.warning("later warning")
    assert capsys.readouterr().out == ""


def test_scope_ends_when_the_call_raises(capsys):
    with pytest.raises(ConfigurationError):
        snt.exec_query("T", timeout=0, debug=True)
    out = capsys.readouterr().out
    assert " WARNING security_notebook_tools.query: exec_query: rejected after " in out
    assert not _log.debug_active()
    with pytest.raises(ConfigurationError):
        snt.exec_query("T", timeout=0)
    child.error("later")
    assert capsys.readouterr().out == ""


def test_scope_ends_on_keyboard_interrupt(capsys):
    with pytest.raises(KeyboardInterrupt), public_call(child, "thing", debug=True):
        raise KeyboardInterrupt
    assert "thing: interrupted after" in capsys.readouterr().out
    assert not _log.debug_active()


def test_inner_debug_false_does_not_turn_the_scope_off(capsys):
    with public_call(child, "outer", debug=True), public_call(child, "inner", debug=False):
        child.debug("deep inside")
    out = capsys.readouterr().out
    for expected in ("outer: started", "inner: started", "deep inside", "inner: finished"):
        assert expected in out
    assert "outer: finished" in out


def test_inner_debug_true_with_the_outer_off_is_only_for_the_inner_call(capsys):
    with public_call(child, "outer"):
        child.debug("before")
        with public_call(child, "inner", debug=True):
            child.debug("during")
        child.debug("after")
    out = capsys.readouterr().out
    assert "inner: started" in out
    assert "during" in out
    assert "inner: finished" in out
    for absent in ("outer", "before", "after"):
        assert absent not in out


def test_nested_debug_true_keeps_the_outer_stream(capsys):
    with public_call(child, "outer", debug=True), public_call(child, "inner", debug=True):
        pass
    out = capsys.readouterr().out
    assert out.count("outer: started") == 1
    assert out.count("inner: started") == 1  # one line, not one per scope


def test_magic_debug_covers_exec_query_and_format_dataframe(sentinel, capsys):
    pytest.importorskip("IPython")
    from IPython.core.interactiveshell import InteractiveShell

    shell = InteractiveShell.instance()
    snt.load_ipython_extension(shell)
    shell.run_cell_magic("kql", "--debug", "T | take 1")
    text = capsys.readouterr().out
    for expected in (
        "%%kql: started",
        "IPython argument parsing: calling",
        "exec_query: started",
        "exec_query: finished",
        "format_dataframe: started",
        "pandas Styler: calling",
        "format_dataframe: finished",
        "%%kql: finished",
    ):
        assert expected in text, expected
    assert not _log.debug_active()
    shell.run_cell_magic("kql", "--no-cache", "T | take 1")
    assert capsys.readouterr().out == ""


def test_magic_without_debug_prints_nothing_even_when_rejected(capsys):
    pytest.importorskip("IPython")
    from IPython.core.error import UsageError
    from IPython.core.interactiveshell import InteractiveShell

    shell = InteractiveShell.instance()
    snt.load_ipython_extension(shell)
    capsys.readouterr()
    with pytest.raises(UsageError):
        shell.run_cell_magic("kql", "", "  ")
    assert capsys.readouterr().out == ""
    with pytest.raises(UsageError):
        shell.run_cell_magic("kql", "--debug", "  ")
    assert "%%kql: rejected after" in capsys.readouterr().out


# --- threads ------------------------------------------------------------------------------------


def test_batch_worker_threads_print_into_the_scope(sentinel, capsys):
    threads = set()

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        threads.add(threading.current_thread().name)
        return [pd.DataFrame({"a": [1]})]

    sentinel.query_tables.side_effect = query
    snt.exec_batch_query(FILTER, D0, _days(5), max_workers=2, debug=True)
    text = capsys.readouterr().out
    assert any(name.startswith("snt") for name in threads), threads  # it did use the pool
    for n in range(1, 6):
        assert f"Batch {n}/5:" in text
    assert text.count("exec_batch_query: started") == 1
    assert "exec_batch_query: finished" in text
    assert not _log.debug_active()


def test_batch_without_debug_prints_nothing_from_worker_threads(sentinel, capsys):
    snt.exec_batch_query(FILTER, D0, _days(5), max_workers=3)
    assert capsys.readouterr().out == ""


def test_batch_failure_in_a_worker_is_printed_and_nothing_prints_afterwards(sentinel, capsys):
    calls = []

    def query(kql, timespan, server_timeout=None, raise_on_partial=False):
        calls.append(1)
        if len(calls) == 3:
            raise QueryError("bad window")
        return [pd.DataFrame({"a": [1]})]

    sentinel.query_tables.side_effect = query
    with pytest.raises(QueryError, match="bad window"):
        snt.exec_batch_query(FILTER, D0, _days(5), max_workers=2, debug=True)
    text = capsys.readouterr().out
    assert "exec_batch_query: failed after" in text
    child.info("after")
    assert capsys.readouterr().out == ""


def test_a_job_that_outlives_its_scope_prints_nothing(capsys):
    with _log.debug_scope(True):
        job = _log.in_current_context(child.info, "straggler")
    job()
    assert capsys.readouterr().out == ""


def test_in_current_context_jobs_can_run_at_the_same_time(capsys):
    barrier = threading.Barrier(3, timeout=10)

    def work(n):
        barrier.wait()  # all three are inside their context at once
        child.info("job %d", n)

    with _log.debug_scope(True):
        jobs = [_log.in_current_context(work, n) for n in range(3)]
        threads = [threading.Thread(target=job) for job in jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    out = capsys.readouterr().out
    assert sorted(re.findall(r"job (\d)", out)) == ["0", "1", "2"]


def test_concurrent_calls_do_not_see_each_others_scope(capsys):
    both_in = threading.Barrier(2, timeout=10)
    errors = []

    def run(name, debug):
        try:
            with public_call(child, name, debug=debug):
                both_in.wait()  # now both calls are running at the same time
                for i in range(20):
                    child.debug("%s tick %d", name, i)
                both_in.wait()
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=("loud", True)),
        threading.Thread(target=run, args=("quiet", False)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    out = capsys.readouterr().out
    assert out.count("loud tick") == 20
    assert "loud: started" in out
    assert "loud: finished" in out
    assert "quiet" not in out


def test_a_plain_thread_started_inside_a_scope_is_not_in_it(capsys):
    with _log.debug_scope(True):
        thread = threading.Thread(target=child.info, args=("from a plain thread",))
        thread.start()
        thread.join()
        child.info("from the caller")
    out = capsys.readouterr().out
    assert "from the caller" in out
    assert "plain thread" not in out


# --- never in the file --------------------------------------------------------------------------


def test_debug_records_never_reach_the_log_file(sentinel, _log_file, capsys):
    snt.exec_query("T", debug=True)
    snt.exec_batch_query(FILTER, D0, _days(4), max_workers=2, debug=True)
    snt.format_dataframe(pd.DataFrame({"a": [1]}), debug=True)
    shown = capsys.readouterr().out
    assert " DEBUG " in shown
    text = _log_file.read_text(encoding="utf-8")
    assert " INFO security_notebook_tools.query: exec_query: started " in text
    assert " DEBUG " not in text
    assert "calling" not in text


def test_the_file_is_the_same_with_and_without_debug(sentinel, _log_file):
    def lines():
        text = _log_file.read_text(encoding="utf-8")
        return [
            re.sub(r"^\S+ ", "", re.sub(r"\d+\.\d\ds|hmac:\w+", "~", line))
            for line in text.splitlines()
        ]

    snt.exec_query("T", cache=False)
    plain = lines()
    _log_file.unlink()
    _log.close_log_file()
    snt.exec_query("T", cache=False, debug=True)
    assert lines() == plain


def test_debug_scope_does_not_create_the_file_for_debug_records(_log_file):
    with _log.debug_scope(True):
        child.debug("only debug")
    assert not _log_file.exists()


# --- redaction and robustness -------------------------------------------------------------------


@pytest.fixture
def real_sentinel(monkeypatch):
    """A real SentinelClient over a mocked Azure client, so its DEBUG lines are emitted."""
    from security_notebook_tools._tablepolicy import TablePolicy

    monkeypatch.setenv("SENTINEL_WORKSPACE_ID", WS)
    monkeypatch.setenv("SENTINEL_SUBSCRIPTION_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("SENTINEL_RESOURCE_GROUP", "rg")
    monkeypatch.setenv("SENTINEL_WORKSPACE_NAME", "sentinel-ws")
    table = LogsTable(
        name="T",
        columns=["a"],
        columns_types=["long"],
        rows=[[1], [2]],
    )
    with (
        mock.patch("security_notebook_tools._query.get_credential"),
        mock.patch("security_notebook_tools._sentinel.LogsQueryClient") as azure,
        mock.patch(
            "security_notebook_tools._sentinel.get_policy",
            return_value=TablePolicy(WS, tables={"t": ("T", "Analytics")}),
        ),
    ):
        azure.return_value.query_workspace.return_value = LogsQueryResult(tables=[table])
        yield azure.return_value


def test_the_cell_shows_raw_text_and_the_file_stays_redacted(real_sentinel, capsys, _log_file):
    snt.exec_query(SECRET_KQL, debug=True)
    out = capsys.readouterr().out
    assert SECRET_KQL in out
    assert "bob.smith@contoso.com" in out
    text = _log_file.read_text()
    assert text
    assert SECRET_KQL not in text
    for leaked in LEAKS:
        assert leaked not in text, leaked


def test_raw_debug_details_reach_the_cell_but_not_the_file(real_sentinel, capsys, _log_file):
    snt.exec_query(SECRET_KQL, debug=True)
    out = capsys.readouterr().out
    text = _log_file.read_text()
    workspace_id = WS
    assert f"Sentinel query text (workspace_id={workspace_id}" in out
    assert SECRET_KQL in out
    assert workspace_id not in text
    assert "DEBUG" not in text


def test_the_cell_shows_exception_text_and_tracebacks_unredacted(capsys):
    with _log.debug_scope(True):
        child.debug("password=hunter2 bob@contoso.com")
        try:
            raise ValueError("secret 'x' token=abc123")
        except ValueError:
            child.error("failed", exc_info=True)
    out = capsys.readouterr().out
    assert "password=hunter2 bob@contoso.com" in out
    assert "ValueError: secret 'x' token=abc123" in out
    assert "Traceback (most recent call last)" in out


def test_the_cell_line_format_is_the_file_line_format(sentinel, capsys, _log_file):
    with _log.debug_scope(True):
        child.info("same format")
    shown = capsys.readouterr().out.strip()
    assert shown.endswith("INFO security_notebook_tools.test: same format")
    assert LINE.match(shown)
    assert (
        shown.split(" ", 1)[1]
        == _log.file_handler.format(
            logging.LogRecord(
                "security_notebook_tools.test", logging.INFO, "f", 1, "same format", (), None
            )
        ).split(" ", 1)[1]
    )


def test_a_failing_stream_never_breaks_the_call(sentinel):
    class Broken:
        def write(self, text):
            raise OSError("closed")

        def flush(self):
            raise OSError("closed")

    with mock.patch.object(sys, "stdout", Broken()):
        df = snt.exec_query("T", debug=True)
    assert list(df.columns) == ["a", "b"]


def test_no_stdout_never_breaks_the_call(sentinel):
    with mock.patch.object(sys, "stdout", None):
        df = snt.exec_query("T", debug=True)
    assert list(df.columns) == ["a", "b"]


def test_results_are_the_same_with_debug(sentinel):
    plain = snt.exec_query("T", cache=False)
    shown = snt.exec_query("T", cache=False, debug=True)
    pd.testing.assert_frame_equal(plain, shown)


# --- handlers -----------------------------------------------------------------------------------


def test_handlers_are_not_duplicated_on_reinstall():
    def cell_handlers(name):
        return [h for h in logging.getLogger(name).handlers if getattr(h, "snt_cell_handler", 0)]

    original = _log.cell_handler
    try:
        _log._install(original, _log.file_handler)
        _log._install(original)
        assert cell_handlers(_log.PACKAGE_LOGGER) == [original]
        replacement = _log._CellHandler()
        _log._install(replacement)  # what a reload of the module does
        assert cell_handlers(_log.PACKAGE_LOGGER) == [replacement]
    finally:
        _log._install(original)
    assert cell_handlers(_log.PACKAGE_LOGGER) == [original]
    for name in _log.THIRD_PARTY_LOGGERS:
        assert cell_handlers(name) == []


def test_the_cell_handler_is_not_on_the_azure_logger_and_the_file_handler_still_is():
    azure = logging.getLogger("azure").handlers
    assert _log.file_handler in azure
    assert _log.cell_handler not in azure
    package = logging.getLogger(_log.PACKAGE_LOGGER).handlers
    assert _log.file_handler in package
    assert _log.cell_handler in package


def test_the_dependency_start_line_renders_its_fields_lazily():
    seen = []

    class Collect(logging.Handler):
        def emit(self, record):
            seen.append(record)

    handler = Collect(logging.DEBUG)
    child.addHandler(handler)
    try:
        with _log.dependency_call(child, "Thing", a=1):
            pass
    finally:
        child.removeHandler(handler)
    start = seen[0]
    # the fields are a lazy object that renders when a handler formats the record
    assert isinstance(start.args[1], _log._LazyFields)
    assert start.getMessage() == "Thing: calling a=1"
