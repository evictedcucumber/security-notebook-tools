import errno
import logging
import time
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
import requests
from azure.core.credentials import AccessToken

from security_notebook_tools import _tablepolicy as tp
from security_notebook_tools._exceptions import (
    ConfigurationError,
    QueryConnectionError,
    QueryError,
)
from security_notebook_tools._kql import ARG_TABLE_NAMES
from security_notebook_tools._tablepolicy import TablePolicy, check_sentinel_query

WS = "11111111-1111-1111-1111-111111111111"
RID = (
    "/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/rg"
    "/providers/Microsoft.OperationalInsights/workspaces/sentinel-ws"
)
BASE = f"https://management.azure.com{RID}"
WORKSPACE_URL = f"{BASE}?api-version=2025-07-01"
TABLES_URL = f"{BASE}/tables?api-version=2025-07-01"
SEARCHES_URL = f"{BASE}/savedSearches?api-version=2020-08-01"

TABLES = [
    ("SigninLogs", "Analytics"),
    ("AuditLogs", "Analytics"),
    ("SecurityEvent", "Analytics"),
    ("DeviceEvents", "Analytics"),  # a Defender XDR table streamed into the workspace
    ("Basic_CL", "Basic"),
    ("Aux_CL", "Auxiliary"),
    ("AuxT", "Auxiliary"),
    ("Lake_CL", "DataLake"),
    ("NoPlan_CL", None),
]
FUNCTIONS = ["MyFunc"]  # built-in parsers such as _Im_NetworkSession are NOT saved searches


def _table_item(name, plan, extra=None):
    props = ({} if plan is None else {"plan": plan}) | (extra or {})
    return {"name": name, "properties": props}


def _arm(tables=TABLES, functions=FUNCTIONS, customer_id=WS, table_pages=None):
    """A fake ``_get_json``: URL -> payload. ``table_pages`` splits the tables over pages."""
    pages = table_pages or [tables]
    answers = {
        WORKSPACE_URL: {"properties": {"customerId": customer_id}},
        SEARCHES_URL: {
            "value": [{"properties": {"functionAlias": f}} for f in functions]
            + [{"properties": {"displayName": "plain saved search"}}]
        },
    }
    for n, page in enumerate(pages):
        url = TABLES_URL if n == 0 else f"{TABLES_URL}&$skiptoken={n}"
        payload = {"value": [_table_item(*t) for t in page]}
        if n + 1 < len(pages):
            payload["nextLink"] = f"{TABLES_URL}&$skiptoken={n + 1}"
        answers[url] = payload
    calls = []

    def get_json(client, url):
        calls.append(url)
        return answers[url]

    get_json.calls = calls
    return get_json


@pytest.fixture
def arm():
    fake = _arm()
    with (
        mock.patch.object(tp, "_get_json", fake),
        mock.patch.object(tp, "_arm_client") as client,
    ):
        fake.client = client
        yield fake


def _policy():
    with mock.patch.object(tp, "_get_json", _arm()), mock.patch.object(tp, "_arm_client"):
        return tp.get_policy(RID, WS, mock.Mock())


@pytest.fixture
def policy():
    return _policy()


# --- fetching, validation, caching --------------------------------------------------------------


def test_policy_contents_and_urls(arm):
    policy = tp.get_policy(RID, WS, mock.Mock())
    assert arm.calls == [WORKSPACE_URL, TABLES_URL, SEARCHES_URL]
    assert policy.customer_id == WS
    assert policy.tables["signinlogs"] == ("SigninLogs", "Analytics")
    assert policy.tables["aux_cl"] == ("Aux_CL", "Auxiliary")
    assert policy.tables["noplan_cl"] == ("NoPlan_CL", "")
    assert policy.functions == {"myfunc": "MyFunc"}


def test_paging_follows_next_link():
    fake = _arm(table_pages=[TABLES[:3], TABLES[3:6], TABLES[6:]])
    with mock.patch.object(tp, "_get_json", fake), mock.patch.object(tp, "_arm_client"):
        policy = tp.get_policy(RID, WS, mock.Mock())
    assert fake.calls == [
        WORKSPACE_URL,
        TABLES_URL,
        f"{TABLES_URL}&$skiptoken=1",
        f"{TABLES_URL}&$skiptoken=2",
        SEARCHES_URL,
    ]
    assert {name for name, _ in policy.tables.values()} == {n for n, _ in TABLES}


def test_customer_id_mismatch(arm):
    with pytest.raises(ConfigurationError, match="does not match"):
        tp.get_policy(RID, "33333333-3333-3333-3333-333333333333", mock.Mock())


def test_customer_id_compared_case_insensitively_and_on_cache_hit(arm):
    tp.get_policy(RID, WS.upper(), mock.Mock())
    other = "33333333-3333-3333-3333-333333333333"
    with pytest.raises(ConfigurationError, match="does not match"):
        tp.get_policy(RID, other, mock.Mock())  # cached policy, still compared
    assert arm.calls.count(WORKSPACE_URL) == 1


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("boom"),
        OSError("network down"),
        QueryError("HTTP 403 from Azure Resource Manager"),
        ValueError("bad json"),
    ],
)
def test_fails_closed_on_any_fetch_error(exc):
    with (
        mock.patch.object(tp, "_get_json", side_effect=exc),
        mock.patch.object(tp, "_arm_client"),
        pytest.raises(QueryError, match="Could not verify Sentinel table tiers.*not sent"),
    ):
        tp.get_policy(RID, WS, mock.Mock())
    assert not tp._cache  # nothing cached after a failure


def test_network_error_while_verifying_is_a_connection_error():
    reset = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
    exc = requests.ConnectionError("Connection aborted.")
    exc.__cause__ = reset
    with (
        mock.patch.object(tp, "_get_json", side_effect=exc),
        mock.patch.object(tp, "_arm_client"),
        pytest.raises(QueryConnectionError, match="reset by peer.*not sent") as ei,
    ):
        tp.get_policy(RID, WS, mock.Mock())
    assert isinstance(ei.value, QueryError)
    assert not tp._cache


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tables": []},  # no tables at all
        {"tables": [("", "Analytics")]},  # nameless table
        {"customer_id": None},
    ],
)
def test_malformed_responses_fail_closed(kwargs):
    with (
        mock.patch.object(tp, "_get_json", _arm(**kwargs)),
        mock.patch.object(tp, "_arm_client"),
        pytest.raises(QueryError, match="Could not verify"),
    ):
        tp.get_policy(RID, WS, mock.Mock())


def test_malformed_saved_search_fails_closed():
    original = _arm()

    def get_json(client, url):
        if url == SEARCHES_URL:
            return {"value": ["not a dict"]}
        return original(client, url)

    with (
        mock.patch.object(tp, "_get_json", get_json),
        mock.patch.object(tp, "_arm_client"),
        pytest.raises(QueryError, match="Could not verify"),
    ):
        tp.get_policy(RID, WS, mock.Mock())


def test_cache_reuse_and_expiry(arm):
    clock = [1000.0]
    with mock.patch.object(tp.time, "monotonic", lambda: clock[0]):
        first = tp.get_policy(RID, WS, mock.Mock())
        clock[0] += tp.CACHE_TTL_SECONDS - 1
        assert tp.get_policy(RID.upper(), WS, mock.Mock()) is first  # case-insensitive key
        assert len(arm.calls) == 3
        clock[0] += 2  # now past the TTL
        second = tp.get_policy(RID, WS, mock.Mock())
        assert second is not first
        assert len(arm.calls) == 6
    assert tp.CACHE_TTL_SECONDS == 300


def test_cache_is_per_tenant_and_clearable(arm):
    tp.get_policy(RID, WS, mock.Mock(), "tenant-a")
    tp.get_policy(RID, WS, mock.Mock(), "tenant-a")
    assert len(arm.calls) == 3
    tp.get_policy(RID, WS, mock.Mock(), "tenant-b")
    assert len(arm.calls) == 6
    tp.clear_policy_cache()
    tp.get_policy(RID, WS, mock.Mock(), "tenant-a")
    assert len(arm.calls) == 9


def test_next_link_must_stay_on_arm():
    for link in (
        "https://evil.example/tables",
        "http://management.azure.com/tables",
        "https://management.azure.com.evil.example/tables",
        "https://user@management.azure.com/tables",
        "https://management.azure.com:8443/tables",
    ):
        pages = [{"value": [], "nextLink": link}]
        with (
            mock.patch.object(tp, "_get_json", side_effect=pages),
            pytest.raises(QueryError, match="outside Azure Resource Manager"),
        ):
            tp._get_all(mock.Mock(), TABLES_URL)
    ok = "https://MANAGEMENT.azure.com/subscriptions/x?api-version=1"
    assert tp._check_next_link(ok) == ok


@pytest.mark.parametrize("page", [{}, {"nextLink": TABLES_URL}, {"value": None}, {"value": {}}])
def test_page_without_value_list_fails_closed(page):
    with (
        mock.patch.object(tp, "_get_json", return_value=page),
        pytest.raises(QueryError, match="no 'value' list"),
    ):
        tp._get_all(mock.Mock(), TABLES_URL)


def test_paging_is_bounded():
    page = {"value": [], "nextLink": TABLES_URL}
    with (
        mock.patch.object(tp, "_get_json", return_value=page),
        pytest.raises(QueryError, match="pages"),
    ):
        tp._get_all(mock.Mock(), TABLES_URL)


def test_get_json_sets_timeouts_and_checks_status():
    client = mock.Mock()
    client.send_request.return_value = mock.Mock(status_code=200, content=b'{"a": 1}')
    assert tp._get_json(client, TABLES_URL) == {"a": 1}
    kwargs = client.send_request.call_args.kwargs
    assert kwargs["connection_timeout"] > 0
    assert kwargs["read_timeout"] > 0
    client.send_request.return_value = mock.Mock(status_code=403, content=b"{}")
    with pytest.raises(QueryError, match="403"):
        tp._get_json(client, TABLES_URL)
    client.send_request.return_value = mock.Mock(status_code=200, content=b"[1]")
    with pytest.raises(QueryError, match="not a JSON object"):
        tp._get_json(client, TABLES_URL)


def test_arm_client_sends_bearer_token_for_arm_scope():
    cred = mock.Mock(spec=["get_token"])
    cred.get_token.return_value = AccessToken("secret-token", int(time.time()) + 3600)
    sent = []

    def fake_request(self, method, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"value": []}'
        response.url = url
        response.raw = mock.Mock()
        response.raw.stream.side_effect = lambda *a, **k: iter([response._content])
        sent.append((method, url, kwargs))
        return response

    with (
        mock.patch.object(requests.Session, "request", fake_request),
        tp._arm_client(cred) as client,
    ):
        assert tp._get_json(client, TABLES_URL) == {"value": []}
    method, url, kwargs = sent[0]
    assert (method, url) == ("GET", TABLES_URL)
    assert kwargs["headers"]["Authorization"] == "Bearer secret-token"
    assert kwargs["timeout"] == (tp._CONNECT_TIMEOUT, tp._READ_TIMEOUT)
    assert kwargs["allow_redirects"] is False
    cred.get_token.assert_called_once_with("https://management.azure.com/.default")


def test_tokens_are_not_logged(arm, caplog):
    caplog.set_level("DEBUG", logger="security_notebook_tools")
    cred = mock.Mock()
    cred.get_token.return_value = AccessToken("secret-token", int(time.time()) + 3600)
    tp.get_policy(RID, WS, cred)
    assert "secret-token" not in caplog.text


# --- query checks -------------------------------------------------------------------------------

ALLOWED = [
    "SigninLogs | take 10",
    "DeviceEvents | where ActionType == 'x' | take 5",
    "SigninLogs\n| where TimeGenerated > ago(1d)\n"
    "| summarize count() by bin(TimeGenerated, 1h), UserPrincipalName\n| order by count_ desc",
    "let u = dynamic(['a']); SigninLogs | where UserPrincipalName in (u)",
    "let recent = SigninLogs | where TimeGenerated > ago(1d);"
    " recent | join kind=inner (AuditLogs | take 1) on Id",
    "let x = SigninLogs | where a == 1; let y = AuditLogs; x | join y on k",
    "SigninLogs | join kind=leftouter hint.strategy=broadcast AuditLogs on CorrelationId",
    "SigninLogs | join (AuditLogs | take 1) on $left.A == $right.B",
    "SigninLogs | lookup kind=leftouter (AuditLogs) on Id",
    "union SigninLogs, AuditLogs",
    "union kind=outer withsource=T SigninLogs, (AuditLogs | take 1)",
    "SigninLogs | union AuditLogs | count",
    "print now()",
    "print x = 1",
    "datatable(a:int, b:string) [1, 'x']",
    "range x from 1 to 10 step 1",
    "SigninLogs | evaluate bag_unpack(DeviceDetail)",
    "SigninLogs | evaluate hint.distribution=single bag_unpack(DeviceDetail, 'p_')",
    # a name that is only text inside a string or a comment
    "SigninLogs | where Msg == 'MyFunc' // Aux_CL MyFunc\n| take 1",
    'SigninLogs | where Msg == "Aux_CL" and Other == "union *"',
    'SigninLogs | where Msg == @"C:\\" | take 1',
    "SigninLogs | where Msg has ```Aux_CL``` | take 1",
    "let start = ago(7d); let n = 5; SigninLogs | where TimeGenerated > start | take n",
    "let f = (x:int) { SigninLogs | take x }; f(3)",
    "let f = (t:(a:string), n:int) { t | take n }; f(SigninLogs, 3)",
    "let T2 = materialize(SigninLogs | take 10); T2 | count",
    "SigninLogs | extend n = toscalar(AuditLogs | count)",
    "SigninLogs | where x in (AuditLogs | project y)",
    "SigninLogs | where tostring(Properties.a) has_any ('a', 'b') and isnotempty(Account)",
    "SigninLogs | project ['Some Col'], Properties['key']",
    "SigninLogs | as t | where true",
    "set notruncation; SigninLogs | take 1",
    "(SigninLogs | take 1) | count",
    "SigninLogs | where (a or b) and not (c == 1)",
    "SigninLogs | fork (take 1) (count)",
    "SigninLogs | parse-kv Col as (a:string, b:long) with (pair_delimiter=';')",
    "SigninLogs | where A !in (dynamic([1, 2])) | where B !in~ ('x')",
    "SigninLogs | partition by UserId ( top 2 by TimeGenerated )",
    "SigninLogs | extend a = column_ifexists('Aux_CL', '')",
    "SigninLogs | summarize count() by bin(TimeGenerated, 1h)",
    'SigninLogs | extend x = tostring(parse_json(Props).a) | where x has "b"',
    "SigninLogs | summarize arg_max(TimeGenerated, *) by UserPrincipalName",
    "let f = (n:int) { SigninLogs | take n }; f(5)",
    "DeviceEvents | join kind=leftouter (SigninLogs | project A) on A",
    "SigninLogs | summarize dcount(UserId), make_set(IPAddress, 5), percentile(Dur, 95)"
    " by bin(TimeGenerated, 1d)",
    "SigninLogs | extend r = row_number(), p = prev(UserId) | where array_length(Tags) > 0",
    "let f = (t:(a:string)) { t | take 1 }; let g = SigninLogs; f(g)",
    "let f = () { let h = 5; SigninLogs | take h }; f()",
    "SigninLogs | as T1 | join kind=inner (T1 | take 1) on UserId",
    "let f2 = () { 1 }; SigninLogs | partition by UserId ( top 2 by TimeGenerated )",
    "SigninLogs | partition hint.strategy=native by UserId { top 2 by TimeGenerated }",
    "SigninLogs | make-graph A --> B with SigninLogs on A",
    "let f = (t:(x:string)) { t | take 1 }; SigninLogs | invoke f()",
    "print 1 | evaluate pivot(x)",
    "SigninLogs | where x > 10m and y < 0x1F and z == 1e3 and w > 5min",
    # tabular operators on the allowlist
    "SigninLogs | top 5 by TimeGenerated desc | project-away Type | project-keep A, B",
    "SigninLogs | make-series c = count() default=0 on TimeGenerated step 1h by UserId",
    "SigninLogs | parse Msg with 'user=' User:string ' ip=' Ip:string | parse-where Msg with *",
    "SigninLogs | mv-expand Tags | mvexpand Other | project-rename U = UserId",
    "SigninLogs | summarize c = count() by bin(TimeGenerated, 1h) | render timechart",
    "SigninLogs | sort by TimeGenerated desc, UserId asc | order by UserId | take 5 | limit 3",
    "SigninLogs | distinct UserId | sample 5 | sample-distinct 3 of UserId | serialize | consume",
    "SigninLogs | top-nested 3 of UserId by c = count(), top-nested 2 of IPAddress by d = count()",
    "SigninLogs | top-hitters 5 of UserId by c = count() | project-reorder UserId",
    "SigninLogs | extend x = 1 | where x == 1 | filter x > 0 | getschema",
    "SigninLogs | mv-apply t = Tags on (where t has 'a' | take 1)",
    "SigninLogs | mv-apply t = Tags on (take 1)",
    "SigninLogs | fork (where a == 1) (summarize count()) F3 = (take 5)",
    "SigninLogs | fork (count) (getschema) (project a | take 1)",
    "SigninLogs | facet by UserId, Result with (take 10) with (summarize count() by UserId)",
    "SigninLogs | partition by UserId (SigninLogs | where a == 1 | take 1)",
    "SigninLogs | partition hint.strategy=shuffle by UserId { take 1 }",
    "SigninLogs | scan with_match_id=m declare (n:long = 0) with (step s1: Result == 0 => n = 1;)",
    "SigninLogs | reduce by Msg | as R | where 1 == 1",
    "SigninLogs | make-graph Src --> Dst with SigninLogs on UserId, AuditLogs on UserId"
    " | graph-to-table nodes",
    "SigninLogs | make-graph Src --> Dst | graph-match (a)-[e]->(b) where a.x == 1 project a.y"
    " | take 1",
    "SigninLogs | make-graph Src --> Dst | graph-shortest-paths (a)-[e*1..3]->(b) project a"
    " | graph-mark-components",
    "SigninLogs | lookup kind=leftouter (AuditLogs) on UserId | extend z = 1",
    "SigninLogs | extend x = Join, y = Partition, z = Union",  # mixed case: just column names
    # Azure Resource Graph: arg("").<table> is a free, read-only source
    'arg("").Resources | where type == "microsoft.compute/virtualmachines" | project id, name',
    "arg('').ResourceContainers | take 5",
    'arg("").resources | take 1',  # ARG table names are case-insensitive
    'arg ( "" ) . Resources | take 1',  # spacing does not matter
    'arg("") // c\n.SecurityResources | take 1',
    'SigninLogs | join hint.remote=left (arg("").Resources | project id)'
    " on $left.ResourceId == $right.id",
    'SigninLogs | join kind=inner hint.remote=right arg("").Resources on $left.A == $right.id',
    'SigninLogs | lookup kind=leftouter hint.remote=auto (arg("").Resources) on id',
    'SigninLogs | join hint.remote=local (arg("").Resources) on id',
    'union SigninLogs, (arg("").SecurityResources | take 1)',
    'union arg("").Resources, SigninLogs',
    'SigninLogs | union arg("").Resources | count',
    'let r = arg("").Resources | project id; SigninLogs | where ResourceId in (r)',
    'let r = arg("").Resources; r | take 1',
    'SigninLogs | extend x = toscalar(arg("").Resources | count)',
    'SigninLogs | where ResourceId in (arg("").Resources | project id)',
    'let f = (t:(*)) { t | take 1 }; f(t=arg("").Resources)',
    'SigninLogs | make-graph a --> b with arg("").Resources on id, SigninLogs on id',
    'SigninLogs | fork (take 1) (join hint.remote=left (arg("").Resources) on id)',
    *[f'arg("").{name} | take 1' for name in ARG_TABLE_NAMES],
]


@pytest.mark.parametrize("kql", ALLOWED)
def test_allowed(policy, kql):
    check_sentinel_query(kql, policy)


# (query, text the message must contain)
REJECTED = [
    # tables not on the Analytics plan, unknown names, functions
    ("Basic_CL | take 1", "Basic_CL"),
    ("Aux_CL | take 1", "Aux_CL"),
    ("Lake_CL | take 1", "Lake_CL"),
    ("NoPlan_CL | take 1", "NoPlan_CL"),
    ("aux_cl | take 1", "Aux_CL"),
    ("Unknown | take 1", "Unknown"),
    ("MyFunc | take 1", "MyFunc"),
    ("MyFunc() | take 1", "MyFunc"),
    ("let r = myfunc(1); SigninLogs", "MyFunc"),
    ("_Im_NetworkSession(starttime=ago(1d))", "_Im_NetworkSession"),
    ("_Im_NetworkSession | take 1", "_Im_NetworkSession"),
    ("SigninLogs | invoke MyFunc()", "MyFunc"),
    ("SigninLogs | take 1; Unknown | take 1", "Unknown"),
    # escaped table names
    ("['Aux_CL'] | take 1", "Aux_CL"),
    ('["Aux_CL"] | take 1', "Aux_CL"),
    ("[ // c\n 'Aux_CL' ] | take 1", "Aux_CL"),
    # the name anywhere, not just in a source position
    ("SigninLogs | extend x = Aux_CL", "Aux_CL"),
    ("let Aux_CL = SigninLogs; Aux_CL", "Aux_CL"),
    ("SigninLogs | extend x = MyFunc", "MyFunc"),
    # escape hatches
    ("search *", "search"),
    ('search "x"', "search"),
    ("Search 'x'", "search"),
    ("find 'x'", "find"),
    ("find in (SigninLogs) where a == 1", "find"),
    ("union *", "wildcard"),
    ("union Sec*", "wildcard"),
    ("union SigninLogs, Sec*", "wildcard"),
    ("SigninLogs | union *", "wildcard"),
    ("union withsource=T *", "wildcard"),
    ("UNION *", "UNION"),
    ("table('Aux_CL')", "table()"),
    ('SigninLogs | union table("X")', "table()"),
    ("workspace('w').SigninLogs", "workspace()"),
    ("app('a').requests", "app()"),
    ("resource('/subscriptions/x').T", "resource()"),
    ("adx('https://c.kusto.windows.net/db').T", "adx()"),
    ("cluster('c').database('d').T", "cluster()"),
    ("database('d').T", "database()"),
    ("external_table('T')", "external_table()"),
    ("externaldata(a:string)[@'https://x/y.csv']", "externaldata"),
    ("evaluate sql_request('Server=x', 'select 1')", "sql_request"),
    ("SigninLogs | evaluate http_request('https://x')", "http_request"),
    ("evaluate hint.x=1 cosmosdb_sql_request('a', 'b')", "cosmosdb_sql_request"),
    ("evaluate azure_digital_twins_query_request('a', 'b')", "azure_digital_twins"),
    ("evaluate mysql_request('a', 'b')", "mysql_request"),
    ("evaluate postgresql_request('a', 'b')", "postgresql_request"),
    ("evaluate http_request_post('a')", "http_request_post"),
    # a restricted or unknown table hidden in a subquery
    ("SigninLogs | join (Aux_CL | take 1) on x", "Aux_CL"),
    ("SigninLogs | join (Unknown | take 1) on x", "Unknown"),
    ("SigninLogs | join Unknown on x", "Unknown"),
    ("SigninLogs | join kind=inner hint.remote=left Aux_CL on x", "Aux_CL"),
    ("SigninLogs | lookup (Unknown) on x", "Unknown"),
    ("SigninLogs | extend n = toscalar(Aux_CL | count)", "Aux_CL"),
    ("SigninLogs | extend n = toscalar(Unknown | count)", "Unknown"),
    ("SigninLogs | extend n = toscalar(Unknown)", "Unknown"),
    ("SigninLogs | where x in (Aux_CL | project y)", "Aux_CL"),
    ("SigninLogs | where x in (Unknown | project y)", "Unknown"),
    ("SigninLogs | where x in (Unknown)", "Unknown"),
    ("let v = view() { Aux_CL }; v", "Aux_CL"),
    ("let v = view() { Unknown }; v", "Unknown"),
    ("let v = Unknown | take 1; v", "Unknown"),
    ("let m = materialize(Unknown); m", "Unknown"),
    ("SigninLogs | union (Unknown)", "Unknown"),
    ("SigninLogs | union (SigninLogs, Unknown)", "ambiguous"),
    ("SigninLogs | evaluate ipv4_lookup(Unknown, ip, ip)", "Unknown"),
    ("let x = unknown_fn(1); SigninLogs", "unknown_fn"),
    ("let x = toscalar(print v = custom_fn(1)); SigninLogs", "custom_fn"),
    # text that cannot be analysed with certainty
    ("SigninLogs | where a == 'unterminated", "unterminated"),
    ("SigninLogs | where a == 'x\ny'", "unterminated"),
    ("SigninLogs\u200b| take 1", "unexpected character"),
    ("SigninLogs // c\x85 Aux_CL", "line-separator"),
    ("SigninLogs | take (1", "unbalanced"),
    ("SigninLogs | take 1)", "unbalanced"),
    ("", "empty"),
    ("// only a comment", "empty"),
    ("let x = 1;", "no tabular statement"),
    (".show tables", "cannot tell"),
    ("$left", "cannot tell"),
    # built-in parsers and other calls that are not saved searches
    ("(_Im_NetworkSession(starttime=ago(1h)) | take 10)", "_Im_NetworkSession"),
    ("let m = materialize(_Im_NetworkSession()); m | take 10", "_Im_NetworkSession"),
    ("let x = (_Im_NetworkSession()); x", "_Im_NetworkSession"),
    ("SigninLogs | where 1 in (_Im_NetworkSession() | count)", "_Im_NetworkSession"),
    ("SigninLogs | extend n = toscalar(_Im_NetworkSession())", "_Im_NetworkSession"),
    ("let v = view() { _Im_NetworkSession() }; v", "_Im_NetworkSession"),
    ("_GetWatchlist('x') | take 1", "_GetWatchlist"),
    ("SigninLogs | where x in (_ASim_Dns() | project y)", "_ASim_Dns"),
    ("SigninLogs | extend y = Tostring(x)", "Tostring"),
    # scope leaks
    (
        "let f = (_Im_NetworkSession:string) { print 1 }; _Im_NetworkSession() | take 10",
        "_Im_NetworkSession",
    ),
    ("let f = (p:string) { print 1 }; p | take 1", "'p'"),
    ("let f = () { let h = 1; print h }; h | take 1", "'h'"),
    ("SigninLogs | as X; X | take 1", "'X'"),
    ("SigninLogs | take (1 | as X); X | take 1", "'X'"),
    # only functions defined with `let f = (...) { ... }` are callable
    ("SigninLogs | as _Im_Dns | extend y = _Im_Dns()", "_Im_Dns"),
    ("let _Im_Dns = print 1; _Im_Dns() | take 1", "_Im_Dns"),
    ("let _Im_Dns = () { _Im_Dns() }; _Im_Dns()", "_Im_Dns"),
    ("let f = (t:string) { print 1 }; SigninLogs | extend y = t()", "t()"),
    # arg() in any other shape (Azure Resource Graph is only allowed as arg("").<table>)
    ('arg("sub").Resources', "arg()"),
    ('arg("").Foo | take 1', "Resources"),
    ('arg("")', "arg()"),
    ('arg("") | take 1', "arg()"),
    ("arg()", "arg()"),
    ("arg().Resources", "arg()"),
    ('arg("", "").Resources', "arg()"),
    ('arg(" ").Resources', "arg()"),
    ('arg(@"").Resources', "arg()"),
    ("arg(@'').Resources", "arg()"),
    ("arg(``````).Resources", "arg()"),
    ("arg(x).Resources", "arg()"),
    ('arg("").["Resources"]', "arg()"),
    ('ARG("").Resources', "arg()"),
    ('Arg("").Resources', "arg()"),
    ('x.arg("").Resources', "arg()"),
    ("['arg']('').Resources", "escaped name"),
    ('SigninLogs | extend x = arg("sub").Resources', "arg()"),
    ('SigninLogs | extend x = toscalar(arg("x").Resources | count)', "arg()"),
    ('arg("").Resources | join (AuxT) on id', "AuxT"),
    ('SigninLogs | join hint.remote=left (arg("").Resources) on id | join AuxT on x', "AuxT"),
    ('union arg("").Resources, AuxT', "AuxT"),
    ('arg("").Resources | where id in (Foo)', "Foo"),
    ('arg("").Resources | invoke Foo()', "Foo"),
    ('let r = arg("").Resources; r | join (Unknown) on id', "Unknown"),
    # review round 2: tabular arguments of `let` functions
    ("let f = (t:(*)) { t | take 10 }; f(t=_Im_NetworkSession)", "_Im_NetworkSession"),
    (
        "let f = (n:int, t:(*)) { t | take n }; f(10, _Im_NetworkSession | take 10)",
        "_Im_NetworkSession",
    ),
    (
        "let f = (T:(*), t:(*)) { t }; SigninLogs | invoke f(t=_Im_NetworkSession)",
        "_Im_NetworkSession",
    ),
    ("let f = (t:(*)) { t }; f(t=MyFunc)", "MyFunc"),
    ("let f = (t:(*)) { t }; f(n=1, t=Unknown)", "Unknown"),
    # make-graph with several tables, operators that are not allowed, partition shapes
    (
        "SigninLogs | make-graph Src --> Dst with SigninLogs on Id, _Im_NetworkSession on Id"
        " | graph-to-table nodes | take 10",
        "_Im_NetworkSession",
    ),
    ("SigninLogs | make-graph a --> b with SigninLogs on c, (Foo) on c", "Foo"),
    (
        "let G = SigninLogs | make-graph Src --> Dst with SigninLogs on Id;"
        " G | graph-merge _Im_NetworkSession | graph-to-table nodes",
        "graph-merge",
    ),
    ("SigninLogs | macro-expand x as y (take 1)", "macro-expand"),
    ("SigninLogs | frobnicate x", "frobnicate"),
    ("SigninLogs | Where x == 1", "Where"),
    ("SigninLogs | print x = 1", "print"),
    ("SigninLogs | ", "end of query"),
    ("SigninLogs | | take 1", "'|'"),
    ("SigninLogs | 5", "'5'"),
    ("SigninLogs | partition by _Im_NetworkSession() (take 1)", "partition"),
    ("SigninLogs | partition by _Im_NetworkSession(count) (take 1)", "partition"),
    ("SigninLogs | partition by a.b (take 1)", "partition"),
    ("SigninLogs | partition (take 1)", "partition"),
    ("SigninLogs | fork (Foo | take 1)", "Foo"),
    ("SigninLogs | fork (take 1) (_Im_NetworkSession())", "_Im_NetworkSession"),
    ("SigninLogs | fork take 1", "fork"),
    ("SigninLogs | facet by x with (Foo)", "Foo"),
    ("SigninLogs | facet by x with take 1", "facet"),
    ("SigninLogs | mv-apply t = Tags on (Foo)", "Foo"),
    ("SigninLogs | mv-apply t = Tags", "mv-apply"),
    ("SigninLogs | fork (partition by x { Foo })", "Foo"),
    # keyword exemptions need exact lower case; names are case-sensitive
    ("signinlogs | take 1", "SigninLogs"),
    ("SIGNINLOGS | take 1", "SigninLogs"),
    ("SigninLogs | join kind=inner signinlogs on x", "SigninLogs"),
    ("SigninLogs | Union Foo", "Union"),
    ("SigninLogs | Partition by x (Foo)", "Partition"),
    ("Union Foo", "Union"),
    ("Evaluate bag_unpack(x)", "Evaluate"),
    # more tabular operand positions
    ("SigninLogs | make-graph a --> b with _Im_NetworkSession on c", "_Im_NetworkSession"),
    ("SigninLogs | make-graph a --> b with Foo on c", "Foo"),
    ("SigninLogs | partition by x (_Im_NetworkSession)", "_Im_NetworkSession"),
    ("SigninLogs | partition by x ( Foo )", "Foo"),
    ("SigninLogs | partition by x { Foo }", "Foo"),
    ("SigninLogs | invoke _Im_NetworkSession()", "_Im_NetworkSession"),
    ("SigninLogs | invoke ago(1d)", "invoke"),
    ("let f = (T:(x:string)) { T | take 1 }; f(_Im_NetworkSession)", "_Im_NetworkSession"),
    ("let f = (T:(x:string)) { T | take 1 }; f(Foo, 1)", "Foo"),
    # escaped call names, number suffixes, keyword case
    ("(['table']('AuxT') | take 10)", "escaped name"),
    ("SigninLogs | union ['cluster']('c').database('d').T", "escaped name"),
    ("SigninLogs | where x > 10AuxT", "AuxT"),
    ("SigninLogs | where x > 1.5AuxT or y == 0x1FAuxT", "AuxT"),
    ("Print x=1", "Print"),
    ("Let x = 1; SigninLogs", "Let"),
    ("SigninLogs | extend x = 1 | Evaluate sql_request('a', 'b')", "sql_request"),
    # plugins
    ("SigninLogs | evaluate python(typeof(*), 'code')", "python"),
    ("SigninLogs | evaluate hint.x=1 r(typeof(*), 'code')", "evaluate r"),
    ("SigninLogs | evaluate ai_embeddings(x, 'm')", "ai_embeddings"),
    ("SigninLogs | evaluate ai_chat_completion(x, 'm')", "ai_chat_completion"),
    ("SigninLogs | evaluate ai_chat_completion_prompt(x, 'm')", "ai_chat_completion_prompt"),
    ("SigninLogs | evaluate some_new_plugin(x)", "some_new_plugin"),
    ("SigninLogs | evaluate ['bag_unpack'](x)", "escaped name"),
    # control characters and line breaks anywhere
    ("// x\x0b```\nAuxT | take 10 // ```", "control or line-separator"),
    ("SigninLogs | where a == 'x\x0cy'", "control or line-separator"),
    ("SigninLogs // c\u2028 AuxT", "control or line-separator"),
    ("SigninLogs\x00| take 1", "control or line-separator"),
    ("SigninLogs | where a == 'x\x9fy'", "control or line-separator"),
    # string-literal confusion must not hide a table
    ('SigninLogs | where a == @"C:\\" ; Aux_CL | take 1 ; print @"x"', "Aux_CL"),
    ('print ```a"``` ; Aux_CL ; print "```"', "Aux_CL"),
    ('SigninLogs | where a == @h"C:\\" ; Aux_CL ; "x"', "unterminated"),
    ("SigninLogs // x\r Aux_CL", "Aux_CL"),
]


@pytest.mark.parametrize(("kql", "fragment"), REJECTED)
def test_rejected(policy, kql, fragment):
    with pytest.raises(ConfigurationError) as info:
        check_sentinel_query(kql, policy)
    assert fragment.lower() in str(info.value).lower()


@pytest.mark.parametrize(
    "kql", ["Basic_CL | take 1", "Unknown | take 1", "MyFunc() | take 1", "search *", "union *"]
)
def test_message_explains_cost_and_that_nothing_was_sent(policy, kql):
    with pytest.raises(ConfigurationError, match="per GB scanned") as info:
        check_sentinel_query(kql, policy)
    assert "not sent" in str(info.value)


def test_message_names_the_plan(policy):
    with pytest.raises(ConfigurationError, match="Auxiliary plan"):
        check_sentinel_query("Aux_CL | take 1", policy)
    with pytest.raises(ConfigurationError, match="DataLake plan"):
        check_sentinel_query("Lake_CL | take 1", policy)


def test_duplicate_table_names_keep_the_stricter_plan():
    fake = _arm(tables=[("Dup_CL", "Analytics"), ("DUP_CL", "Basic"), ("SigninLogs", "Analytics")])
    with mock.patch.object(tp, "_get_json", fake), mock.patch.object(tp, "_arm_client"):
        policy = tp.get_policy(RID, WS, mock.Mock())
    with pytest.raises(ConfigurationError, match="Basic plan"):
        check_sentinel_query("Dup_CL | take 1", policy)


def test_arg_message_lists_the_allowed_tables(policy):
    with pytest.raises(ConfigurationError) as info:
        check_sentinel_query('arg("").Foo', policy)
    assert all(name in str(info.value) for name in ARG_TABLE_NAMES)


def test_arg_table_clashing_with_a_restricted_workspace_table_is_rejected():
    policy = TablePolicy(
        WS,
        tables={"signinlogs": ("SigninLogs", "Analytics"), "resources": ("Resources", "Basic")},
    )
    with pytest.raises(ConfigurationError, match="Basic plan"):
        check_sentinel_query('arg("").Resources | take 1', policy)
    check_sentinel_query('arg("").ResourceContainers | take 1', policy)


def test_policy_without_functions():
    policy = TablePolicy(WS, tables={"t": ("T", "Analytics")})
    check_sentinel_query("T | take 1", policy)
    with pytest.raises(ConfigurationError, match="'Other'"):
        check_sentinel_query("Other | take 1", policy)


@pytest.mark.parametrize(
    ("exc", "traceback_logged"),
    [
        (RuntimeError("boom"), True),
        (QueryError("HTTP 403"), False),
        (ValueError("bad json"), False),
    ],
)
def test_unknown_fetch_errors_are_logged_with_traceback_and_still_fail_closed(
    caplog, exc, traceback_logged
):
    with (
        caplog.at_level(logging.ERROR, logger="security_notebook_tools"),
        mock.patch.object(tp, "_get_json", side_effect=exc),
        mock.patch.object(tp, "_arm_client"),
        pytest.raises(QueryError, match="Could not verify Sentinel table tiers.*not sent") as ei,
    ):
        tp.get_policy(RID, WS, mock.Mock())
    assert ei.value.__cause__ is exc
    assert ("please report it" in str(ei.value)) is traceback_logged
    assert any(r.exc_info for r in caplog.records) is traceback_logged
    assert not tp._cache


# --- logging ------------------------------------------------------------------------------------


def test_arm_requests_log_method_host_and_path_but_never_the_query(caplog):
    caplog.set_level(logging.DEBUG, logger="security_notebook_tools")
    client = mock.Mock()
    client.send_request.return_value = mock.Mock(status_code=200, content=b'{"a": 1}')
    tp._get_json(client, f"{TABLES_URL}&$skiptoken=SECRETPAGE")
    text = caplog.text
    assert "ARM request: done in" in text
    assert "method=GET host=management.azure.com" in text
    assert "/resourceGroups/<name>/" in text
    assert "status=200" in text
    for leak in ("SECRETPAGE", "api-version", "skiptoken", "rg/providers", "22222222"):
        assert leak not in text
    assert {r.levelno for r in caplog.records if "ARM request" in r.getMessage()} == {logging.DEBUG}


def test_arm_http_failure_is_one_warning_without_the_query(caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    client = mock.Mock()
    client.send_request.return_value = mock.Mock(status_code=403, content=b"{}")
    with pytest.raises(QueryError):
        tp._get_json(client, f"{TABLES_URL}&$skiptoken=SECRETPAGE")
    (record,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "ARM request: failed after" in record.getMessage()
    assert "QueryError: HTTP 403 from Azure Resource Manager" in record.getMessage()
    assert "SECRETPAGE" not in caplog.text


def test_policy_fetch_is_logged_with_pages_and_without_the_resource_id(arm, caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    tp.get_policy(RID, WS, mock.Mock())
    done = next(
        r.getMessage() for r in caplog.records if "table policy fetch: done" in r.getMessage()
    )
    assert "workspace=se…(len 11) pages=2 tables=9 not_analytics=5 functions=1" in done
    assert "Reading table plans and functions for workspace se…(len 11)" in caplog.text
    for leak in ("2222", "resourceGroups", RID, WS, "sentinel-ws"):
        assert leak not in caplog.text
    caplog.clear()
    tp.get_policy(RID, WS, mock.Mock())  # reused: nothing at INFO
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]


def test_known_fetch_failure_is_a_warning_not_an_error(caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    with (
        mock.patch.object(tp, "_get_json", side_effect=QueryError("HTTP 403")),
        mock.patch.object(tp, "_arm_client"),
        pytest.raises(QueryError),
    ):
        tp.get_policy(RID, WS, mock.Mock())
    levels = [r.levelno for r in caplog.records if r.levelno >= logging.WARNING]
    assert levels == [logging.WARNING]


def test_unexpected_fetch_failure_is_one_error_with_traceback(caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    with (
        mock.patch.object(tp, "_get_json", side_effect=RuntimeError("boom")),
        mock.patch.object(tp, "_arm_client"),
        pytest.raises(QueryError) as info,
    ):
        tp.get_policy(RID, WS, mock.Mock())
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    from security_notebook_tools._log import failure_logged

    assert failure_logged(info.value)


def test_cost_guard_pass_names_tables_from_the_policy_not_the_query(policy, caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    check_sentinel_query(
        "SigninLogs | join (AuditLogs | take 1) on X | where Mail == 'bob@x.io'", policy
    )
    (record,) = [r for r in caplog.records if "Cost guard passed" in r.getMessage()]
    assert record.levelno == logging.INFO
    assert record.getMessage().endswith("[AuditLogs, SigninLogs]")
    assert "bob" not in caplog.text


def test_cost_guard_rejection_is_not_logged_here(policy, caplog):
    caplog.set_level(logging.INFO, logger="security_notebook_tools")
    with pytest.raises(ConfigurationError):
        check_sentinel_query("Basic_CL | take 1", policy)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# --- retention ----------------------------------------------------------------------------------


def _retention_of(extra):
    fake = _arm(tables=[("Syslog", "Analytics", extra)])
    with mock.patch.object(tp, "_get_json", fake), mock.patch.object(tp, "_arm_client"):
        return tp.get_policy(RID, WS, mock.Mock()).retention


def test_retention_is_parsed():
    assert _retention_of({"retentionInDays": 90, "totalRetentionInDays": 365}) == {
        "syslog": (90, 365)
    }


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({}, (None, None)),
        ({"retentionInDays": "90", "totalRetentionInDays": None}, (None, None)),
        ({"retentionInDays": True, "totalRetentionInDays": 1.5}, (None, None)),
        ({"retentionInDays": -1, "totalRetentionInDays": [365]}, (None, None)),
        ({"retentionInDays": 30}, (30, None)),
    ],
)
def test_bad_or_missing_retention_is_ignored_not_raised(extra, expected):
    assert _retention_of(extra) == {"syslog": expected}


def test_retention_does_not_touch_the_cost_guard(policy):
    assert policy.tables["signinlogs"] == ("SigninLogs", "Analytics")
    assert policy.retention["signinlogs"] == (None, None)
    with pytest.raises(ConfigurationError):
        check_sentinel_query("Basic_CL", policy)


NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


def _ret_policy():
    return TablePolicy(
        customer_id=WS,
        tables={"syslog": ("Syslog", "Analytics"), "other": ("Other", "Analytics")},
        retention={"syslog": (90, 365), "other": (None, None)},
    )


def test_retention_notice_beyond_analytics():
    message, names = tp.retention_notice("Syslog", _ret_policy(), NOW - timedelta(days=120), NOW)
    assert names == ["Syslog"]
    assert message.startswith(
        "Syslog keeps 90 days in Analytics (365 in total): rows before 2026-07-25"
    )
    assert "long-term retention" in message
    assert "gone" not in message


def test_retention_notice_beyond_total():
    message, _ = tp.retention_notice("Syslog", _ret_policy(), NOW - timedelta(days=400), NOW)
    assert "Rows before 2025-10-23 are gone entirely." in message


def test_retention_notice_none_within_or_unknown():
    policy = _ret_policy()
    assert tp.retention_notice("Syslog", policy, NOW - timedelta(days=89), NOW) is None
    assert tp.retention_notice("Other", policy, NOW - timedelta(days=900), NOW) is None
    assert tp.retention_notice("Unrelated", policy, NOW - timedelta(days=900), NOW) is None


def test_retention_notice_without_long_term_tier():
    policy = TablePolicy(
        customer_id=WS, tables={"t": ("T", "Analytics")}, retention={"t": (30, None)}
    )
    message, _ = tp.retention_notice("T", policy, NOW - timedelta(days=60), NOW)
    assert "in total" not in message
    assert "long-term" not in message


def test_retention_notice_grace_of_a_day():
    policy = _ret_policy()
    assert tp.retention_notice("Syslog", policy, NOW - timedelta(days=90, hours=23), NOW) is None
    assert tp.retention_notice("Syslog", policy, NOW - timedelta(days=92), NOW) is not None
    message, _ = tp.retention_notice("Syslog", policy, NOW - timedelta(days=365, hours=23), NOW)
    assert "gone" not in message
    message, _ = tp.retention_notice("Syslog", policy, NOW - timedelta(days=367), NOW)
    assert "gone" in message


# --- one check per batch -------------------------------------------------------------------------


def _window(kql, day=1):
    from security_notebook_tools._batch import _prefix

    start = datetime(2026, 1, day, tzinfo=UTC)
    return _prefix((start, start + timedelta(days=1))) + kql


def _head():
    from security_notebook_tools._batch import _PREFIX_RE

    return _PREFIX_RE


BATCH_KQL = "T | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd"
IDS = (RID, WS, None)


def test_batch_checked_is_false_outside_a_batch():
    tp.note_batch_checked(_window(BATCH_KQL), *IDS)  # no scope: nothing recorded
    assert not tp.batch_checked(_window(BATCH_KQL), *IDS)


def test_batch_check_skips_later_windows_only_after_a_pass():
    with tp.batch_check(BATCH_KQL, _head()):
        assert not tp.batch_checked(_window(BATCH_KQL, 1), *IDS)
        tp.note_batch_checked(_window(BATCH_KQL, 1), *IDS)
        assert tp.batch_checked(_window(BATCH_KQL, 2), *IDS)
        assert tp.batch_checked(_window(BATCH_KQL, 1), RID.upper(), WS.upper(), None)
    assert not tp.batch_checked(_window(BATCH_KQL, 2), *IDS)  # the scope ended


@pytest.mark.parametrize(
    "ids",
    [
        (RID.replace("sentinel-ws", "other-ws"), WS, None),
        (RID, "33333333-3333-3333-3333-333333333333", None),
        (RID, WS, "other-tenant"),
    ],
)
def test_batch_check_holds_only_for_the_workspace_it_passed(ids):
    with tp.batch_check(BATCH_KQL, _head()):
        tp.note_batch_checked(_window(BATCH_KQL), *IDS)
        assert not tp.batch_checked(_window(BATCH_KQL, 2), *ids)


@pytest.mark.parametrize(
    "kql",
    [
        "Aux_CL | take 1",  # another query entirely
        BATCH_KQL + " | union Aux_CL",  # the batch's query with more after it
        "Aux_CL; " + BATCH_KQL,  # ... or before it
        BATCH_KQL,  # no window prefix
        "let BatchStart = 1; let BatchEnd = 2;\n" + BATCH_KQL,  # not the generated prefix
        'let BatchStart = datetime("2026-01-01T00:00:00.000000Z"); Aux_CL; '
        'let BatchEnd = datetime("2026-01-02T00:00:00.000000Z");\n' + BATCH_KQL,
        "Aux_CL | take 1;\n" + _window(BATCH_KQL),  # text ahead of the prefix
        _window(BATCH_KQL).replace("2026-01-01", '2026-01-01") | union (Aux_CL'),
        _window(BATCH_KQL).replace("2026", "２０２６", 1),  # non-ASCII digits
        _window(BATCH_KQL).replace(";\n", ";\n\n", 1),
        _window("", 3) + _window(BATCH_KQL),  # two prefixes
    ],
)
def test_batch_check_does_not_cover_other_text(kql):
    with tp.batch_check(BATCH_KQL, _head()):
        tp.note_batch_checked(_window(BATCH_KQL), *IDS)
        tp.note_batch_checked(kql, *IDS)  # not a window: nothing recorded for it either
        assert not tp.batch_checked(kql, *IDS)


@pytest.mark.parametrize(
    "bounds",
    [
        (datetime(1000, 1, 1, tzinfo=UTC), datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC)),
    ],
)
def test_generated_prefix_matches_the_batch_head(bounds):
    from security_notebook_tools._batch import _prefix

    assert _head().fullmatch(_prefix(bounds))
