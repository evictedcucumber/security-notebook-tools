import logging
from unittest import mock

import pandas as pd
import pytest

from security_notebook_tools import disable_logging, drop_empty_columns, enable_logging
from security_notebook_tools._log import _NotebookHandler, logger, shorten, verbose_logging
from security_notebook_tools._sentinel import SentinelClient


@pytest.fixture(autouse=True)
def _reset():
    disable_logging()
    yield
    disable_logging()


def _df():
    return pd.DataFrame({"a": [1, 2], "empty": [None, None]})


def test_quiet_by_default(capsys):
    drop_empty_columns(_df())
    assert capsys.readouterr().out == ""


def test_verbose_prints_to_stdout(capsys):
    drop_empty_columns(_df(), verbose=True)
    out = capsys.readouterr().out
    assert "Dropped 1 empty column(s): ['empty']" in out
    assert "security_notebook_tools.formatting" in out


def test_verbose_cleans_up():
    level = logger.level
    with verbose_logging(True):
        assert any(isinstance(h, _NotebookHandler) for h in logger.handlers)
        assert logger.getEffectiveLevel() <= logging.INFO
    assert not any(isinstance(h, _NotebookHandler) for h in logger.handlers)
    assert logger.level == level


def test_nested_verbose_not_duplicated(capsys):
    with verbose_logging(True), verbose_logging(True):
        logger.info("once")
    assert capsys.readouterr().out.count("once") == 1


def test_enable_logging_persists(capsys):
    enable_logging()
    drop_empty_columns(_df())
    drop_empty_columns(_df(), verbose=True)  # no duplicate handler
    assert capsys.readouterr().out.count("Dropped") == 2
    disable_logging()
    drop_empty_columns(_df())
    assert capsys.readouterr().out == ""


def test_client_verbose_default_and_override(capsys):
    with mock.patch("security_notebook_tools._sentinel.LogsQueryClient") as m:
        m.return_value.query_workspace.side_effect = RuntimeError("stop")
        client = SentinelClient("ws", credential=mock.Mock(), verbose=True)
        assert "workspace ws" in capsys.readouterr().out
        with pytest.raises(RuntimeError):
            client.query("T", verbose=False)
        assert capsys.readouterr().out == ""
        with pytest.raises(RuntimeError):
            client.query("T  |\n take 1")
        assert "Running Sentinel query" in capsys.readouterr().out


def test_shorten():
    assert shorten("a\n  b") == "a b"
    assert shorten("x" * 300, 10) == "x" * 9 + "…"
