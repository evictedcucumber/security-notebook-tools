import logging
import re

from security_notebook_tools import _log
from security_notebook_tools._log import log_file_path, project_root, shorten

child = logging.getLogger("security_notebook_tools.test")

LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z (INFO|WARNING|ERROR) "
    r"security_notebook_tools\.test: .+$"
)


def test_project_root_git_marker(tmp_path):
    (tmp_path / ".git").mkdir()
    assert project_root(tmp_path) == tmp_path.resolve()


def test_project_root_pyproject_marker(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    assert project_root(tmp_path) == tmp_path.resolve()


def test_project_root_nested_finds_parent(tmp_path):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert project_root(sub) == tmp_path.resolve()


def test_project_root_falls_back_to_start(tmp_path):
    sub = tmp_path / "x"
    sub.mkdir()
    # tmp_path may sit under a repo on some machines; only assert when no marker is above.
    if not any(
        (d / m).exists()
        for d in (sub.resolve(), *sub.resolve().parents)
        for m in _log._ROOT_MARKERS
    ):
        assert project_root(sub) == sub.resolve()


def test_project_root_defaults_to_cwd(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("")
    sub = tmp_path / "nb"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert project_root() == tmp_path.resolve()


def test_log_file_path_real(tmp_path, monkeypatch):
    monkeypatch.undo()  # drop the conftest redirect
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    assert log_file_path() == tmp_path.resolve() / "notebooks.log"


def test_info_and_above_appended_debug_excluded(_log_file):
    child.debug("hidden")
    child.info("one")
    child.warning("two")
    child.error("three")
    lines = _log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [line.split(" ", 2)[1] for line in lines] == ["INFO", "WARNING", "ERROR"]
    assert "hidden" not in "".join(lines)


def test_appends_without_truncating(_log_file):
    _log_file.write_text("existing\n", encoding="utf-8")
    child.info("new")
    _log.close_log_file()
    child.info("newer")
    lines = _log_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "existing"
    assert lines[1].endswith("new")
    assert lines[2].endswith("newer")


def test_format(_log_file):
    child.info("Running %s", "query")
    child.warning("Careful")
    lines = _log_file.read_text(encoding="utf-8").splitlines()
    assert all(LINE.match(line) for line in lines), lines
    assert lines[0].endswith("security_notebook_tools.test: Running query")


def test_exception_traceback_follows(_log_file):
    try:
        raise ValueError("boom")
    except ValueError:
        child.error("failed", exc_info=True)
    text = _log_file.read_text(encoding="utf-8")
    assert "ERROR security_notebook_tools.test: failed\nTraceback" in text
    assert "ValueError: boom" in text


def test_nothing_printed(capsys, _log_file):
    child.info("quiet")
    child.error("still quiet")
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""


def test_no_file_until_logged(_log_file):
    child.debug("not written")
    assert not _log_file.exists()


def test_follows_path_change(tmp_path, monkeypatch):
    first, second = tmp_path / "1.log", tmp_path / "2.log"
    monkeypatch.setattr(_log, "log_file_path", lambda: first)
    child.info("a")
    monkeypatch.setattr(_log, "log_file_path", lambda: second)
    child.info("b")
    assert first.read_text().count("\n") == 1
    assert second.read_text().count("\n") == 1


def test_unwritable_path_is_silent(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(_log, "log_file_path", lambda: tmp_path / "missing" / "n.log")
    child.info("dropped")
    out = capsys.readouterr()
    assert out.out == out.err == ""


def test_shorten():
    assert shorten("a\n  b") == "a b"
    assert shorten("x" * 300, 10) == "x" * 9 + "…"
