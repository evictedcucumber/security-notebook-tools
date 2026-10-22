import logging
from unittest import mock

import pytest

from security_notebook_tools import disable_logging, enable_logging
from security_notebook_tools._log import _NotebookHandler, logger, shorten, verbose_logging
from security_notebook_tools._sentinel import SentinelClient
from security_notebook_tools._tablepolicy import TablePolicy


@pytest.fixture(autouse=True)
def _reset():
    disable_logging()
    yield
    disable_logging()


child = logging.getLogger("security_notebook_tools.test")


def _log_something(verbose=False):
    with verbose_logging(verbose):
        child.info("Did something")


def test_quiet_by_default(capsys):
    _log_something()
    assert capsys.readouterr().out == ""


def test_verbose_prints_to_stdout(capsys):
    _log_something(verbose=True)
    out = capsys.readouterr().out
    assert "Did something" in out
    assert "security_notebook_tools.test" in out


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
    _log_something()
    _log_something(verbose=True)  # no duplicate handler
    assert capsys.readouterr().out.count("Did something") == 2
    disable_logging()
    _log_something()
    assert capsys.readouterr().out == ""


def test_client_verbose_default_and_override(capsys):
    policy = TablePolicy("11111111-1111-1111-1111-111111111111", {"t": ("T", "Analytics")})
    rid = (
        "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/rg"
        "/providers/Microsoft.OperationalInsights/workspaces/sentinel-ws"
    )
    with (
        mock.patch("security_notebook_tools._sentinel.LogsQueryClient") as m,
        mock.patch("security_notebook_tools._sentinel.get_policy", return_value=policy),
    ):
        m.return_value.query_workspace.side_effect = RuntimeError("stop")
        client = SentinelClient(
            "11111111-1111-1111-1111-111111111111",
            credential=mock.Mock(),
            resource_id=rid,
            verbose=True,
        )
        assert "workspace 11111111-1111-1111-1111-111111111111" in capsys.readouterr().out
        with pytest.raises(RuntimeError):
            client.query("T", verbose=False)
        assert capsys.readouterr().out == ""
        with pytest.raises(RuntimeError):
            client.query("T  |\n take 1")
        assert "Running Sentinel query" in capsys.readouterr().out


def test_shorten():
    assert shorten("a\n  b") == "a b"
    assert shorten("x" * 300, 10) == "x" * 9 + "…"
