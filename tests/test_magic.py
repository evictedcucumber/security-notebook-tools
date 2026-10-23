import logging
import subprocess
import sys
from datetime import datetime, timedelta

import pandas as pd
import pytest
from pandas.io.formats.style import Styler

pytest.importorskip("IPython")

from IPython.core.error import UsageError  # noqa: E402
from IPython.core.interactiveshell import InteractiveShell  # noqa: E402

from security_notebook_tools import _magic  # noqa: E402
from security_notebook_tools._exceptions import ConfigurationError  # noqa: E402
from security_notebook_tools._query import DEFAULT_TIMEOUT  # noqa: E402

DF = pd.DataFrame({"a": [1]})


@pytest.fixture
def shell(monkeypatch):
    sh = InteractiveShell.instance()
    sh.user_ns.clear()
    sh.user_ns["timedelta"] = timedelta
    import security_notebook_tools as snt

    snt.load_ipython_extension(sh)
    calls = []

    def fake(kql, timespan=None, **kwargs):
        calls.append((kql, timespan, kwargs))
        return DF

    monkeypatch.setattr(_magic, "exec_query", fake)
    sh.calls = calls
    return sh


def run(sh, line, body):
    return sh.run_cell_magic("kql", line, body)


def test_returns_formatted_result(shell):
    out = run(shell, "", "T | take 1")
    assert isinstance(out, Styler)
    assert out.data.equals(DF)


def test_result_not_stored_in_namespace(shell):
    before = set(shell.user_ns)
    run(shell, "", "T")
    assert set(shell.user_ns) == before


def test_variable_argument_rejected(shell):
    with pytest.raises(UsageError):
        run(shell, "out", "T")
    assert not shell.calls


def test_defaults(shell):
    run(shell, "", "T")
    assert shell.calls == [
        (
            "T",
            None,
            {
                "target": "sentinel",
                "timeout": DEFAULT_TIMEOUT,
                "cache": True,
            },
        )
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1d", timedelta(days=1)),
        ("12h", timedelta(hours=12)),
        ("30m", timedelta(minutes=30)),
        ("90s", timedelta(seconds=90)),
        ("1d12h", timedelta(days=1, hours=12)),
        ("2H30M", timedelta(hours=2, minutes=30)),
    ],
)
def test_timespan_shorthand(shell, text, expected):
    run(shell, f"-t {text}", "T")
    assert shell.calls[0][1] == expected


def test_timespan_variable(shell):
    shell.user_ns["ts"] = timedelta(minutes=5)
    run(shell, "--timespan ts", "T")
    assert shell.calls[0][1] == timedelta(minutes=5)


def test_timespan_tuple_expression(shell):
    start, end = datetime(2026, 1, 1), datetime(2026, 1, 2)
    shell.user_ns.update(start=start, end=end)
    run(shell, '--timespan "(start, end)"', "T")
    assert shell.calls[0][1] == (start, end)


def test_timespan_call_expression(shell):
    run(shell, '--timespan "timedelta(hours=3)"', "T")
    assert shell.calls[0][1] == timedelta(hours=3)


def test_options_forwarded(shell):
    run(shell, "--target adx --timeout 60", "T")
    assert shell.calls[0][2] == {
        "target": "adx",
        "timeout": 60,
        "cache": True,
    }


def test_no_cache_flag(shell):
    run(shell, "--no-cache", "T")
    assert shell.calls[0][2]["cache"] is False


def test_body_verbatim(shell):
    body = "T\n| extend d = dynamic({'a': 1})\n| where x == '${y}'\n"
    run(shell, "", body)
    assert shell.calls[0][0] == body


def test_bad_timespan_expression(shell):
    with pytest.raises(UsageError, match="nope") as info:
        run(shell, "--timespan nope", "T")
    assert info.value.__cause__ is not None
    assert not shell.calls


@pytest.mark.parametrize("body", ["", "  \n\t\n"])
def test_empty_body(shell, body):
    with pytest.raises(UsageError):
        run(shell, "", body)
    assert not shell.calls


def test_import_does_not_import_ipython():
    code = "import sys, security_notebook_tools; assert 'IPython' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)


# --- logging ------------------------------------------------------------------------------------


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_extension_load_and_run_are_logged(shell, caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    import security_notebook_tools as snt

    snt.load_ipython_extension(shell)
    run(shell, "-t 1d --timeout 60", "T | where Email == 'bob@contoso.com'")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "IPython extension loaded: the %%kql cell magic is registered" in text
    assert "%%kql: started kql=hmac:" in text
    assert "IPython argument parsing: done in" in text
    assert "%%kql: finished in" in text
    assert "target=sentinel timeout=60 cache=True timespan=duration" in text
    assert "bob" not in text
    assert not _warnings(caplog)


def test_empty_body_is_one_warning(shell, caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    with pytest.raises(UsageError):
        run(shell, "", "  ")
    record = _warnings(caplog)[0]
    assert len(_warnings(caplog)) == 1
    assert record.name == "security_notebook_tools.magic"
    assert "%%kql: rejected after" in record.getMessage()


def test_bad_argument_is_a_dependency_warning_and_a_rejection(shell, caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    with pytest.raises(UsageError):
        run(shell, "--nope", "T")
    messages = [r.getMessage() for r in _warnings(caplog)]
    assert len(messages) == 2
    assert messages[0].startswith("IPython argument parsing: failed")
    assert messages[1].startswith("%%kql: rejected")


def test_timespan_expression_text_is_never_logged(shell, caplog, _log_file):
    caplog.set_level(logging.DEBUG, logger="security_notebook_tools")
    with pytest.raises(UsageError):
        run(shell, '--timespan "timedelta(hours=secret_marker_9)"', "T")
    logged = _log_file.read_text(encoding="utf-8") + "\n".join(
        r.getMessage() for r in caplog.records
    )
    assert "secret_marker_9" not in logged
    assert "timespan expression evaluation: failed" in logged


def test_nested_exec_query_failure_is_logged_once(shell, caplog, monkeypatch):
    from security_notebook_tools import _query

    monkeypatch.setattr(_magic, "exec_query", _query.exec_query)
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    with pytest.raises(ConfigurationError):
        run(shell, "--timeout 0", "T")
    record = _warnings(caplog)
    assert len(record) == 1
    assert record[0].getMessage().startswith("exec_query: rejected")
