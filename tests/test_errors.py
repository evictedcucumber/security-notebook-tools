import errno
import logging
import socket

import pytest
from azure.core.exceptions import HttpResponseError, ODataV4Format, ServiceResponseTimeoutError
from azure.kusto.data.exceptions import KustoApiError, KustoMultiApiError, KustoServiceError

from security_notebook_tools._errors import (
    TIMEOUT_STATUS,
    TRANSIENT_STATUS,
    azure_error_codes,
    describe_codes,
    describe_network_error,
    describe_status,
    hints_for,
    is_network_error,
    is_timeout,
    kusto_error_codes,
    unexpected,
    unexpected_errors,
)
from security_notebook_tools._exceptions import ConfigurationError, QueryError


def _wrapped(inner):
    try:
        try:
            try:
                raise inner
            except OSError as e:
                raise RuntimeError("urllib3 protocol error") from e
        except RuntimeError as e:
            raise ValueError("requests failed") from e
    except ValueError as e:
        return e


def test_connection_reset_found_in_a_chain():
    exc = _wrapped(ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"))
    text = describe_network_error(exc)
    assert "[Errno 104]" in text
    assert "reset by peer" in text
    assert "interval" in text


@pytest.mark.parametrize(
    ("inner", "fragment"),
    [
        (ConnectionRefusedError(errno.ECONNREFUSED, "refused"), "refused"),
        (TimeoutError(), "timed out"),
        (OSError(errno.ETIMEDOUT, "timed out"), "timed out"),
        (BrokenPipeError(errno.EPIPE, "pipe"), "broken pipe"),
        (ConnectionAbortedError(errno.ECONNABORTED, "abort"), "aborted"),
        (OSError(errno.ENETUNREACH, "net"), "network unreachable"),
        (OSError(errno.EHOSTUNREACH, "host"), "host unreachable"),
        (socket.gaierror(-2, "Name or service not known"), "DNS"),
        (OSError(errno.EIO, "disk"), "OSError"),
    ],
)
def test_errno_explanations(inner, fragment):
    assert fragment in describe_network_error(_wrapped(inner))


def test_errno_is_shown():
    assert "[Errno -2]" in describe_network_error(socket.gaierror(-2, "x"))


def test_cycle_in_the_chain_terminates():
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    assert describe_network_error(a) == "RuntimeError: b"


def test_no_oserror_falls_back_to_innermost():
    try:
        try:
            raise KeyError("deep")
        except KeyError as e:
            raise RuntimeError("outer") from e
    except RuntimeError as exc:
        assert describe_network_error(exc) == "KeyError: 'deep'"


def test_context_is_followed():
    try:
        try:
            raise ConnectionResetError(errno.ECONNRESET, "x")
        except OSError:
            raise RuntimeError("while handling")  # noqa: B904
    except RuntimeError as exc:
        assert "reset by peer" in describe_network_error(exc)


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (400, "bad request"),
        (401, "az login"),
        (403, "no read access"),
        (404, "not found"),
        (408, "timeout"),
        (413, "too large"),
        (429, "throttling"),
        (500, "internal"),
        (502, "unavailable"),
        (503, "unavailable"),
        (504, "timeout"),
        (507, "HTTP 507"),
    ],
)
def test_describe_status(status, fragment):
    assert fragment in describe_status(status)


@pytest.mark.parametrize("status", [None, 200, 418, True, "500"])
def test_describe_status_none(status):
    assert describe_status(status) is None


def test_transient_status_excludes_timeouts():
    assert {429, 502, 503} == TRANSIENT_STATUS
    assert {408, 504} == TIMEOUT_STATUS
    assert not TRANSIENT_STATUS & TIMEOUT_STATUS


@pytest.mark.parametrize(
    ("status", "codes", "expected"),
    [
        (504, [], True),
        (408, [], True),
        (500, ["GatewayTimeout"], True),
        (400, ["BadRequest", "QueryExecutionTimedOut"], True),
        (None, ["gatewaytimeout"], True),
        (429, ["RequestTimeout"], False),  # retried statuses keep their own class
        (502, ["GatewayTimeout"], False),
        (503, ["Timeout"], False),
        (504, ["BadRequest"], True),
        (500, [], False),
        (503, ["ServiceUnavailable"], False),
        (400, ["BadArgumentError", "SyntaxError"], False),
        (None, [], False),
    ],
)
def test_is_timeout(status, codes, expected):
    assert is_timeout(status, codes) is expected


@pytest.mark.parametrize("status", [408, 504])
def test_timeout_status_hint_mentions_min_interval(status):
    assert "min_interval" in describe_status(status)


def test_is_network_error_ignores_non_socket_os_errors():
    try:
        try:
            raise FileNotFoundError(errno.ENOENT, "az not found")
        except FileNotFoundError as inner:
            raise RuntimeError("credential failed") from inner
    except RuntimeError as exc:
        assert not is_network_error(exc)
    assert is_network_error(ConnectionResetError(errno.ECONNRESET, "reset"))
    assert is_network_error(socket.gaierror(-2, "Name or service not known"))


def test_describe_codes_matches_case_insensitively_and_dedupes():
    text = describe_codes(["BadRequest_SyntaxError", "SyntaxError", "Nope"])
    assert text.count("syntax error") == 1
    assert describe_codes(["Nope"]) is None
    assert "timed out" in describe_codes(["GatewayTimeout"])
    assert "too large" in describe_codes(["ResponsePayloadTooLarge"])
    assert "workspace not found" in describe_codes(["WorkspaceNotFoundError"])


def test_azure_error_codes_reads_nested_codes():
    error = ODataV4Format(
        {
            "code": "BadArgumentError",
            "message": "m",
            "details": [{"code": "SyntaxError", "message": "s"}],
            "innererror": {"code": "Inner", "innererror": {"code": "Innermost"}},
        }
    )
    exc = HttpResponseError(message="x")
    exc.error = error
    assert azure_error_codes(exc) == ["BadArgumentError", "SyntaxError", "Inner", "Innermost"]
    assert azure_error_codes(ValueError("x")) == []
    exc.error = object()
    assert azure_error_codes(exc) == []


def test_kusto_error_codes():
    one = KustoApiError({"error": {"code": "BadRequest_SyntaxError", "message": "m"}})
    assert kusto_error_codes(one) == ["BadRequest_SyntaxError"]
    multi = KustoMultiApiError(
        [{"OneApiErrors": [{"error": {"code": "A", "message": "m", "@message": "d"}}]}]
    )
    assert kusto_error_codes(multi) == ["A"]
    assert kusto_error_codes(KustoServiceError("x")) == []


def test_hints_for_joins_status_and_codes():
    text = hints_for(403, ["InsufficientAccessError"])
    assert "reader role" in text
    assert "no read access" in text
    assert "InsufficientAccessError" in text
    assert hints_for(None, []) == ""


def test_unexpected_logs_with_traceback_and_returns_query_error(caplog):
    log = logging.getLogger("security_notebook_tools.test")
    exc = RuntimeError("boom")
    with caplog.at_level(logging.ERROR, logger="security_notebook_tools"):
        error = unexpected(log, "a test", exc)
    assert isinstance(error, QueryError)
    assert "Unexpected RuntimeError during a test: boom" in str(error)
    assert "report" in str(error)
    (record,) = caplog.records
    assert record.exc_info[1] is exc
    assert "please report it with the traceback" in record.getMessage()


def test_unexpected_errors_wraps_exception_and_chains():
    log = logging.getLogger("security_notebook_tools.test")
    original = KeyError("k")
    with pytest.raises(QueryError, match="Unexpected KeyError during x") as ei:  # noqa: SIM117
        with unexpected_errors(log, "x"):
            raise original
    assert ei.value.__cause__ is original


def test_unexpected_errors_lets_package_errors_and_interrupts_through():
    log = logging.getLogger("security_notebook_tools.test")
    config = ConfigurationError("cost guard")
    with pytest.raises(ConfigurationError) as ei:  # noqa: SIM117
        with unexpected_errors(log, "x"):
            raise config
    assert ei.value is config
    for exc in (KeyboardInterrupt, SystemExit):
        with pytest.raises(exc):  # noqa: SIM117
            with unexpected_errors(log, "x"):
                raise exc


def test_timeout_without_oserror_says_timed_out():
    text = describe_network_error(ServiceResponseTimeoutError("Read timed out"))
    assert "timed out" in text
    assert "ServiceResponseTimeoutError" in text
