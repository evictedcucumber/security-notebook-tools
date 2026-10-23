import logging
import re

import pytest

from security_notebook_tools import _log
from security_notebook_tools._log import (
    dependency_call,
    describe_exception,
    fingerprint,
    log_file_path,
    mask_id,
    project_root,
    redact,
    redact_exception_text,
)

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


# --- redact -------------------------------------------------------------------------------------

JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlLWJ5dGVz"
GUID = "1a2b3c4d-5e6f-7a8b-9c0d-112233445566"


@pytest.mark.parametrize(
    ("text", "secret", "expected"),
    [
        (f"token {JWT} rejected", JWT, "token <token> rejected"),
        ("Authorization: Bearer abc.def-123", "abc.def-123", "Authorization: <redacted>"),
        ("{'Authorization': 'Bearer xyz987'}", "xyz987", "'Authorization': <redacted>"),
        ("sent Bearer q1w2e3r4t5y6 to arm", "q1w2e3r4t5y6", "Bearer <redacted> to arm"),
        ("SharedAccessSignature sr=a&sig=b", "sig=b", "SharedAccessSignature <redacted>"),
        (
            "GET https://acct.blob.core.windows.net/c/b?sv=1&sig=ZZZ%2F&se=2 failed",
            "ZZZ",
            "https://acct.blob.core.windows.net/c/b?<redacted> failed",
        ),
        (
            "next https://h.example/p?$skiptoken=SECRET#frag",
            "SECRET",
            "https://h.example/p?<redacted>",
        ),
        ("https://user:hunter2@host.example/x", "hunter2", "https://<redacted>@host.example/x"),
        (
            "management.azure.com/subscriptions/x?api-version=1",
            "api-version",
            "/subscriptions/<name>?<redacted>",
        ),
        ("failed: sig=abc123", "abc123", "sig=<redacted>"),
        ("code=AUTHCODE9", "AUTHCODE9", "code=<redacted>"),
        ("token=tok_123 and access_token=at_456", "tok_123", "token=<redacted>"),
        ("client_secret: s3cr3t!", "s3cr3t", "client_secret: <redacted>"),
        ("password='my pass phrase'", "pass phrase", "password=<redacted>"),
        ("AccountKey=aGVsbG8gd29ybGQ=;EndpointSuffix=x", "aGVsbG8", "AccountKey=<redacted>"),
        ("key=abc", "abc", "key=<redacted>"),
        ("mail bob.smith+x@contoso.co.uk now", "bob.smith", "<email>"),
        ("from 10.20.30.40 port 443", "10.20.30.40", "from <ip> port"),
        ("peer 2001:db8::8a2e:370:7334 up", "2001:db8", "peer <ip> up"),
        ("loopback ::1 and ::ffff:192.168.1.5", "192.168.1.5", "<ip> and <ip>"),
        (f"workspace {GUID} ok", GUID, "workspace 1a2b… ok"),
        (
            f"/subscriptions/{GUID}/resourceGroups/Prod-RG/providers/x",
            "Prod-RG",
            "/resourceGroups/<name>",
        ),
        ('File "/home/alice/proj/x.py", line 1', "alice", "/home/<user>/proj/x.py"),
        ("C:\\Users\\Bob Smith\\x", "Bob", "Users\\<user>"),
        ("C:\\\\Users\\\\carol\\\\x", "carol", "<user>"),
        ("/Users/dave/x", "dave", "/Users/<user>/x"),
        ("key: AbCdEfGh1234AbCdEfGh1234AbCdEfGh1234", "AbCdEfGh1234", "<secret>"),
    ],
)
def test_redact_masks(text, secret, expected):
    out = redact(text)
    assert secret not in out
    assert expected in out
    assert redact(out) == out  # safe to apply twice


@pytest.mark.parametrize(
    "text",
    [
        "Time range: last 1 day, 0:00:00 (default)",
        "2026-10-23T14:02:11.402Z INFO x: started at 14:02:11",
        "elapsed 0:00:01.5 and 12:34:56",
        "Python 3.14.0 and pandas 2.3.1 and version 1.2.3",
        "ordinary words: authorization failed; the code is fine; a token of thanks",
        "error code: BadRequest_SyntaxError (service error code: SyntaxError)",
        "status=403 status_code=403 rows=10 columns=3 kql=hmac:1a2b3c4d5e6f len=812",
        "Result cache miss; running the query on Sentinel",
        "Foo::Bar and std::vector and a :: b",
        "sentinel-ws pages=2 tables=120 SigninLogs",
        "10.0.19045.1234 is a build, 999.1.1.1 is not an address",
    ],
)
def test_redact_leaves_ordinary_text(text):
    assert redact(text) == text


def test_redact_exception_text_strips_quoted_literals_and_shortens():
    exc = ValueError("Failed near 'where Email == \"bob@x.com\"' and \"it's\"; didn't work\nmore")
    out = redact_exception_text(exc)
    assert out == "Failed near '…' and \"…\"; didn't work more"
    long = redact_exception_text("x" * 500, limit=50)
    assert len(long) == 50
    assert long.endswith("…")
    assert redact_exception_text(RuntimeError()) == ""


def test_redact_exception_text_redacts_what_is_left():
    out = redact_exception_text(f"workspace {GUID} for bob@contoso.com via https://h/p?sig=1")
    assert out == "workspace 1a2b… for <email> via https://h/p?<redacted>"


def test_describe_exception():
    assert describe_exception(KeyError("k")) == "KeyError: '…'"
    assert describe_exception(RuntimeError()) == "RuntimeError"


def test_fingerprint_hides_the_text():
    fp = fingerprint("T | where Email == 'bob@x.com'")
    assert re.fullmatch(r"hmac:[0-9a-f]{12} len=\d+", fp)
    assert "bob" not in fp
    assert fingerprint("a") == fingerprint("a") != fingerprint("b")
    assert fingerprint(None) == "<NoneType>"


def test_mask_id():
    assert mask_id(GUID) == "1a2b…"
    assert mask_id("contoso.onmicrosoft.com") == "<id>"
    assert mask_id(None) == "none"


# --- the file formatter ------------------------------------------------------------------------


def test_file_lines_are_redacted(_log_file):
    child.info("Calling %s with token=%s for %s", "arm", "tok-1", "bob@contoso.com")
    child.warning("see https://h.example/p?code=abc from 10.1.2.3")
    text = _log_file.read_text(encoding="utf-8")
    for leaked in ("tok-1", "bob@", "code=abc", "10.1.2.3"):
        assert leaked not in text
    assert "<email>" in text


def test_traceback_is_redacted_and_messages_lose_quoted_text(_log_file):
    try:
        try:
            raise OSError("cause: Bearer abcdef123456 for 'select secret_kql'")
        except OSError as inner:
            raise ValueError(f"outer {GUID} bob@contoso.com password=hunter2") from inner
    except ValueError:
        child.error("failed", exc_info=True)
    text = _log_file.read_text(encoding="utf-8")
    for leaked in ("abcdef123456", "secret_kql", "bob@", "hunter2", GUID):
        assert leaked not in text
    assert "OSError" in text
    assert "ValueError: outer 1a2b… <email> password=<redacted>" in text
    assert "The above exception was the direct cause of the following exception:" in text
    assert "Traceback (most recent call last):" in text
    assert "/home/<user>/" in text or "/home/" not in text


def test_formatter_does_not_change_the_record(_log_file):
    try:
        raise ValueError("secret 'x'")
    except ValueError:
        child.error("failed", exc_info=True)
    handler = _log.file_handler
    record = logging.LogRecord("n", logging.ERROR, "f", 1, "m", None, None)
    assert handler.format(record).endswith("n: m")
    assert record.exc_text is None


def test_cached_exc_text_from_another_handler_is_not_reused(_log_file):
    record = child.makeRecord(
        child.name, logging.ERROR, "f", 1, "boom", (), (ValueError, ValueError("v"), None)
    )
    record.exc_text = "Traceback: password=hunter2 leaked"
    line = _log.file_handler.format(record)
    assert "hunter2" not in line
    assert record.exc_text == "Traceback: password=hunter2 leaked"


# --- the azure loggers --------------------------------------------------------------------------


def test_azure_warnings_and_errors_reach_the_file_but_not_info(_log_file):
    sdk = logging.getLogger("azure.core.pipeline.policies.test")
    old = sdk.level
    sdk.setLevel(logging.DEBUG)
    try:
        sdk.debug("debug chatter")
        sdk.info("info chatter Authorization: Bearer abcdefgh1234")
        sdk.warning("retrying after 10.0.0.1 failed token=abc")
        sdk.error("gave up")
    finally:
        sdk.setLevel(old)
    text = _log_file.read_text(encoding="utf-8")
    assert "chatter" not in text
    assert (
        "WARNING azure.core.pipeline.policies.test: retrying after <ip> failed token=<redacted>"
        in text
    )
    assert "ERROR azure.core.pipeline.policies.test: gave up" in text


def test_other_loggers_are_not_written(_log_file):
    logging.getLogger("somethingelse").warning("not ours")
    assert not _log_file.exists()


def test_handlers_are_not_duplicated(_log_file):
    def ours(name):
        return [h for h in logging.getLogger(name).handlers if getattr(h, "snt_file_handler", 0)]

    names = (_log.PACKAGE_LOGGER, *_log.THIRD_PARTY_LOGGERS)
    assert "azure" in names
    original = _log.file_handler
    try:
        _log._install(original)
        _log._install(original)
        assert all(ours(n) == [original] for n in names)
        replacement = _log._ProjectFileHandler()
        _log._install(replacement)  # what a reload of the module does
        assert all(ours(n) == [replacement] for n in names)
    finally:
        _log._install(original)
    assert all(ours(n) == [original] for n in names)


# --- dependency_call ----------------------------------------------------------------------------


def test_dependency_call_logs_start_success_and_result(caplog):
    caplog.set_level(logging.DEBUG, logger="security_notebook_tools.test")
    with dependency_call(child, "Thing", host="h.example", token=None) as call:
        call["rows"] = 3
    debug, info = caplog.records
    assert (debug.levelno, info.levelno) == (logging.DEBUG, logging.INFO)
    assert debug.getMessage() == "Thing: calling host=h.example"
    assert re.fullmatch(r"Thing: done in \d+\.\d\ds host=h\.example rows=3", info.getMessage())


def test_dependency_call_level_for_success(caplog):
    caplog.set_level(logging.DEBUG, logger="security_notebook_tools.test")
    with dependency_call(child, "Thing", level=logging.DEBUG):
        pass
    assert [r.levelno for r in caplog.records] == [logging.DEBUG, logging.DEBUG]


def test_dependency_call_failure_is_one_warning_with_redacted_text(caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools.test")
    boom = OSError("cannot reach 'select secret' at https://h/p?sig=1 from 10.0.0.9")
    with pytest.raises(OSError) as info, dependency_call(child, "Thing", a=1):
        raise boom
    assert info.value is boom
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    message = record.getMessage()
    assert re.fullmatch(
        r"Thing: failed after \d+\.\d\ds a=1: "
        r"OSError: cannot reach '…' at https://h/p\?<redacted> from <ip>",
        message,
    )


def test_dependency_call_nested_failure_is_reported_once(caplog):
    caplog.set_level(logging.DEBUG, logger="security_notebook_tools.test")
    boom = RuntimeError("x")
    for _ in range(2):  # the same instance twice: each failure is reported on its own
        with (
            pytest.raises(RuntimeError),
            dependency_call(child, "outer"),
            dependency_call(child, "inner"),
        ):
            raise boom
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage().split(":")[0] for r in warnings] == ["inner", "inner"]


def test_dependency_call_interrupt_is_logged_and_propagates(caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools.test")
    with pytest.raises(KeyboardInterrupt), dependency_call(child, "Thing"):
        raise KeyboardInterrupt
    assert caplog.records[0].levelno == logging.WARNING


# --- review fixes: more patterns, quoting, limits, values, files ----------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('{"accessToken":"abc123"}', "abc123"),
        ("clientSecret=abc", "=abc"),
        ("DbPassword=hunter2", "hunter2"),
        ("secretKey=abc", "=abc"),
        ("refreshToken=x1y2", "x1y2"),
        ("MY_API_KEY: s3cr3t", "s3cr3t"),
        ('{"key":"AbC"}', "AbC"),
        ("{'code': 'ABC'}", "ABC"),
        ("Cookie: session=abcd; other=zz", "abcd"),
        ("Set-Cookie: sid=q9; Path=/", "q9"),
        ("Ocp-Apim-Subscription-Key: k3y", "k3y"),
        ("x-functions-key: k4y", "k4y"),
        ("Authorization: Foo a=b, c=d", "c=d"),
        ("range 10.0.0.1-10.0.0.9 end", "10.0.0.9"),
        ("host-10.0.0.1 up", "10.0.0.1"),
        ("access_token%3Dabc123 next", "abc123"),
        ("/workspaces/contoso-prod/tables", "contoso-prod"),
        ("/subscriptions/mysub/resourceGroups/myrg/x", "myrg"),
    ],
)
def test_redact_more_shapes(text, secret):
    out = redact(text)
    assert secret not in out, out
    assert redact(out) == out


def test_redact_keeps_weak_keys_out_of_prose():
    for text in ("the key: to success", "error code: 12", "status_code=403", "keys=3 monkeys"):
        assert redact(text) == text


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        ("bad b'alice-prod-db'", "alice"),
        ("near h'hunter2'", "hunter2"),
        ("'user's name'", "user"),
        ("ran `select secret` today", "secret"),
        ("unbalanced 'tail with secrets and more", "secrets"),
        ('r"C:\\path\\x" and @"verbatim"', "verbatim"),
    ],
)
def test_quoted_literals_are_stripped_in_all_forms(text, gone):
    out = redact_exception_text(text)
    assert gone not in out
    assert "…" in out


def test_apostrophes_in_words_are_kept():
    assert redact_exception_text("it doesn't work, Bob's data") == "it doesn't work, Bob's data"


def test_redact_is_bounded_on_adversarial_input():
    import time

    texts = ("a-" * 100_000, "password=" * 25_000, "'" * 200_000, "a." * 100_000, "x" * 200_000)
    for text in texts:
        start = time.perf_counter()
        redact(text)
        redact_exception_text(text)
        assert time.perf_counter() - start < 0.5


def test_message_line_breaks_cannot_forge_records(_log_file):
    child.info("first\nsecond 2026-10-23T00:00:00.000Z ERROR fake: boom\r\nthird")
    lines = _log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "first\\nsecond" in lines[0]


def test_handler_filter_calls_the_base_filter(_log_file):
    class Veto(logging.Filter):
        def filter(self, record):
            return False

    veto = Veto()
    _log.file_handler.addFilter(veto)
    try:
        child.info("dropped")
    finally:
        _log.file_handler.removeFilter(veto)
    assert not _log_file.exists()


def test_format_fields_writes_only_plain_values():
    from datetime import UTC, datetime, timedelta

    from security_notebook_tools._log import SafeText, format_fields

    class Boom:
        def __str__(self):
            raise RuntimeError("no")

    out = format_fields(
        {
            "a": 1,
            "b": 2.5,
            "c": True,
            "d": None,
            "e": timedelta(days=1),
            "f": datetime(2026, 1, 2, tzinfo=UTC),
            "g": "ok-name_1.x",
            "h": "has space\nnewline",
            "i": SafeText("masked value"),
            "j": ["a", "b c", 1],
            "k": list(range(30)),
            "l": {"x": 1},
            "m": Boom(),
            "n": pd_like(),
        }
    )
    assert out.startswith(" a=1 b=2.5 c=True e=1 day, 0:00:00 f=2026-01-02T00:00:00+00:00")
    assert " g=ok-name_1.x h=<str> i=masked value " in out
    assert "j=['a', <str>, 1]" in out
    assert out.count("…") == 1 and " k=[0, 1," in out
    assert "l=<dict> m=<Boom> n=<Frame>" in out
    assert "\n" not in out and " d=" not in out


class Frame:
    def __repr__(self):
        return "secret contents"


def pd_like():
    return Frame()


def test_describe_timespan_never_logs_values():
    from datetime import UTC, datetime, timedelta

    from security_notebook_tools._log import describe_timespan

    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert describe_timespan(None) == "none"
    assert describe_timespan(timedelta(days=2)) == "2 days, 0:00:00"
    assert describe_timespan((start, timedelta(hours=1))) == "(2026-01-01T00:00:00+00:00, 1:00:00)"
    assert describe_timespan(("bob@contoso.com", 1)) == "(<str>, 1)"
    assert describe_timespan({"secret": 1}) == "<dict>"
    assert describe_timespan(Frame()) == "<Frame>"


def test_mask_name():
    from security_notebook_tools._log import mask_name

    assert mask_name("contoso-prod") == "co…(len 12)"
    assert mask_name(None) == "none"
    assert mask_name("\n=") == "??…(len 2)"


def test_fingerprint_is_keyed_per_process():
    import hashlib

    text = "T | take 1"
    assert hashlib.sha256(text.encode()).hexdigest()[:12] not in fingerprint(text)
    assert fingerprint(text).startswith("hmac:")


def test_log_file_is_created_private_and_refuses_foreign_files(tmp_path):
    import os
    import stat

    path = tmp_path / "n.log"
    stream = _log._open_log(path)
    stream.close()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 & ~_umask()
    target = tmp_path / "real.log"
    target.write_text("")
    if hasattr(os, "symlink") and os.name == "posix":
        link = tmp_path / "link.log"
        link.symlink_to(target)
        with pytest.raises(OSError):
            _log._open_log(link)
        hard = tmp_path / "hard.log"
        os.link(target, hard)
        with pytest.raises(OSError):
            _log._open_log(hard)
    with pytest.raises(OSError):
        _log._open_log(tmp_path)  # a directory


def _umask():
    import os

    mask = os.umask(0)
    os.umask(mask)
    return mask


def test_handler_drops_records_for_a_symlinked_log(tmp_path, monkeypatch):
    import os

    if os.name != "posix":
        pytest.skip("symlinks")
    target = tmp_path / "victim.txt"
    target.write_text("keep\n")
    link = tmp_path / "notebooks.log"
    link.symlink_to(target)
    monkeypatch.setattr(_log, "log_file_path", lambda: link)
    child.info("must not be written")
    assert target.read_text() == "keep\n"


def test_project_root_stops_at_home(tmp_path, monkeypatch):
    home = tmp_path / "home" / "me"
    work = home / "notes" / "deep"
    work.mkdir(parents=True)
    (tmp_path / "home" / ".git").mkdir()  # above home: must not be found
    monkeypatch.setattr(_log.Path, "home", classmethod(lambda cls: home))
    assert project_root(work) == work.resolve()
    (home / "pyproject.toml").write_text("")
    assert project_root(work) == home.resolve()  # home itself is checked


def test_project_root_is_cached_per_working_directory(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "a"
    sub.mkdir()
    monkeypatch.chdir(sub)
    first = project_root()
    calls = []
    monkeypatch.setattr(_log.Path, "exists", lambda self: calls.append(self) or False)
    assert project_root() == first
    assert not calls
