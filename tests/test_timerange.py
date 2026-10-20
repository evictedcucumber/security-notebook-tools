from datetime import UTC, datetime, timedelta

import pytest

from security_notebook_tools._timerange import DEFAULT_TIMESPAN, has_time_filter, resolve_timespan


@pytest.mark.parametrize(
    "kql",
    [
        "SigninLogs | where TimeGenerated > ago(7d)",
        "SigninLogs\n| where timegenerated between (datetime(2026-10-01) .. datetime(2026-10-02))",
        "SecurityEvent | filter Timestamp >= startofday(now())",
        "let start = ago(3d);\nSigninLogs | where TimeGenerated > start | take 10",
        "AuditLogs | where ActivityDateTime > ago(1h)",
        "SigninLogs | where UserPrincipalName == 'a' and TimeGenerated > ago(1d)",
        "T | join (U | where TimeGenerated > ago(1d)) on Id",
        "let lookback = ago(2d);\nAuditLogs | where ActivityDateTime > lookback",
    ],
)
def test_detects_time_filter(kql):
    assert has_time_filter(kql)


@pytest.mark.parametrize(
    "kql",
    [
        "SigninLogs | take 10",
        "SigninLogs | summarize count() by bin(TimeGenerated, 1h)",
        "SigninLogs | project TimeGenerated, UserPrincipalName",
        'SigninLogs | where Msg == "TimeGenerated > ago(1d)"',
        "SigninLogs | where Msg has 'ago(1d)'",
        "SigninLogs // | where TimeGenerated > ago(1d)\n| take 5",
        "SigninLogs | extend Age = ago(1d) | where ResultType == 0",
        "let users = dynamic(['a']);\nSigninLogs | where UserPrincipalName in (users)",
    ],
)
def test_no_time_filter(kql):
    assert not has_time_filter(kql)


def test_default_one_day_without_filter():
    assert resolve_timespan("SigninLogs | take 10", None) == DEFAULT_TIMESPAN == timedelta(days=1)


def test_set_in_query():
    assert resolve_timespan("SigninLogs | where TimeGenerated > ago(7d)", None) is None


def test_param_used_without_filter():
    span = (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC))
    assert resolve_timespan("SigninLogs | take 10", span) == span


def test_param_and_filter_warns(caplog):
    span = timedelta(days=30)
    assert resolve_timespan("T | where TimeGenerated > ago(7d)", span) == span
    assert "narrower range wins" in caplog.text
