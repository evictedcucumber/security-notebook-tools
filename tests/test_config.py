import pytest

from security_notebook_tools import _config as config
from security_notebook_tools._exceptions import ConfigurationError

GUID = "11111111-1111-1111-1111-111111111111"
RID = (
    f"/subscriptions/{GUID}/resourceGroups/my-rg_1.(x)"
    "/providers/Microsoft.OperationalInsights/workspaces/Sentinel-ws1"
)


def test_guid():
    assert config.validate_guid(f"  {GUID.upper()} ", "V") == GUID.upper()
    for bad in [
        "",
        "env-ws",
        GUID[:-1],
        GUID + "0",
        "{" + GUID + "}",
        GUID.replace("-", ""),
        "g" * 36,
    ]:
        with pytest.raises(ConfigurationError, match="V"):
            config.validate_guid(bad, "V")


@pytest.mark.parametrize("ok", [GUID, "contoso.onmicrosoft.com", "contoso", "a-b.c-d.io"])
def test_tenant_valid(ok):
    assert config.validate_tenant_id(ok) == ok


@pytest.mark.parametrize(
    "bad", ["", "a b", "a/b", "a\\b", "a..b", ".a", "a.", "a@b", "a:b", "a?b", "a\nb", "é.com"]
)
def test_tenant_invalid(bad):
    with pytest.raises(ConfigurationError, match="AZURE_TENANT_ID"):
        config.validate_tenant_id(bad)


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("https://c.kusto.windows.net", "https://c.kusto.windows.net"),
        ("https://c.kusto.windows.net/", "https://c.kusto.windows.net"),
        ("  https://c.region.kusto.windows.net:443/ ", "https://c.region.kusto.windows.net:443"),
    ],
)
def test_cluster_uri_valid(uri, expected):
    assert config.validate_cluster_uri(uri) == expected


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "c.kusto.windows.net",
        "http://c.kusto.windows.net",
        "HTTPS://c.kusto.windows.net",
        "ftp://c.kusto.windows.net",
        "https://",
        "https://user@c.kusto.windows.net",
        "https://user:pw@c.kusto.windows.net",
        "https://c.kusto.windows.net:8080",
        "https://c.kusto.windows.net:",
        "https://c.kusto.windows.net:abc",
        "https://c.kusto.windows.net?x=1",
        "https://c.kusto.windows.net/?",
        "https://c.kusto.windows.net#frag",
        "https://c.kusto.windows.net/path",
        "https://c.kusto.windows.net//",
        "https://c.kusto.windows.net\\@evil.com",
        "https://c.kusto.windows.net evil.com",
        "https://[::1]",
        "https://localhost:9999",
        "https://localhost",
        "https://kusto",
    ],
)
def test_cluster_uri_invalid(uri):
    with pytest.raises(ConfigurationError, match="ADX_CLUSTER_URI"):
        config.validate_cluster_uri(uri)


@pytest.mark.parametrize(
    "ok",
    [
        RID,
        RID.lower(),
        RID.upper(),
        f"/subscriptions/{GUID.upper()}/resourcegroups/r/providers/microsoft.operationalinsights"
        "/workspaces/abcd",
        f"/subscriptions/{GUID}/resourceGroups/{'r' * 90}/providers/Microsoft.OperationalInsights"
        f"/workspaces/{'w' * 63}",
        f"  {RID}  ",
    ],
)
def test_workspace_resource_id_valid(ok):
    assert config.validate_workspace_resource_id(ok) == ok.strip()


_P = "/providers/Microsoft.OperationalInsights/workspaces/"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        GUID,
        RID + "/",
        RID + "/tables",
        RID + "?api-version=1",
        RID + "#x",
        RID.replace("/workspaces", "\n/workspaces"),
        "x" + RID,
        RID.replace("/subscriptions/", "/subscription/"),
        RID.replace(GUID, "not-a-guid"),
        RID.replace(GUID, GUID[:-1]),
        RID.replace("Microsoft.OperationalInsights", "Microsoft.Storage"),
        RID.replace("workspaces", "clusters"),
        f"/subscriptions/{GUID}/resourceGroups/{'r' * 91}{_P}abcd",  # group too long
        f"/subscriptions/{GUID}/resourceGroups/{_P}abcd",  # empty group
        f"/subscriptions/{GUID}/resourceGroups/rg.{_P}abcd",  # group ends in '.'
        f"/subscriptions/{GUID}/resourceGroups/r g{_P}abcd",
        f"/subscriptions/{GUID}/resourceGroups/r/g{_P}abcd",
        f"/subscriptions/{GUID}/resourceGroups/r{_P}abc",  # name too short
        f"/subscriptions/{GUID}/resourceGroups/r{_P}{'w' * 64}",  # name too long
        f"/subscriptions/{GUID}/resourceGroups/r{_P}ab_cd",
        f"/subscriptions/{GUID}/resourceGroups/r{_P}ab.cd",
        f"/subscriptions/{GUID}/resourceGroups/r{_P}abc\u212a",  # Kelvin sign look-alike
        f"/subscriptions/{GUID}/resourceGroups/\u212a{_P}abcd",
        f"//subscriptions/{GUID}/resourceGroups/r{_P}abcd",
    ],
)
def test_workspace_resource_id_invalid(bad):
    with pytest.raises(ConfigurationError, match="Invalid workspace resource ID"):
        config.validate_workspace_resource_id(bad)


@pytest.mark.parametrize(
    "uri",
    [
        "https://api.securityplatform.microsoft.com/lake/kql",
        "https://api.securityplatform.microsoft.com",
        "https://API.SecurityPlatform.Microsoft.com",
        "https://securityplatform.microsoft.com",
        "https://x.y.securityplatform.microsoft.com:443",
    ],
)
def test_cluster_uri_rejects_the_lake(uri):
    with pytest.raises(ConfigurationError, match="ADX_CLUSTER_URI.*data lake"):
        config.validate_cluster_uri(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "https://ade.loganalytics.io",
        "https://ADE.LogAnalytics.io/",
        "https://ade.applicationinsights.io",
        "https://ade.eu.loganalytics.io",
        "https://api.loganalytics.io",
        "https://adx.monitor.azure.com",
        "https://adx.loganalytics.azure.com",
        "https://adx.applicationinsights.azure.com",
        "https://adx.int.monitor.azure.com",
        "https://adx.monitor.azure.us",
        "https://management.azure.com",
        "https://notsecurityplatform.microsoft.com",
        "https://securityplatform.microsoft.com.evil.net",
        "https://myloganalytics.io.example.com",
        "https://c.securityplatform.example.com",
        "https://kusto.windows.net",  # no cluster label
        "https://c.kusto.windows.net.evil.net",
        "https://evilkusto.windows.net.example.com",
        "https://c.example.com",
        "https://8.8.8.8",
    ],
)
def test_cluster_uri_must_be_a_kusto_domain(uri):
    with pytest.raises(ConfigurationError, match="ADX_CLUSTER_URI"):
        config.validate_cluster_uri(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "https://c.kusto.windows.net",
        "https://c.region.kusto.windows.net",
        "https://C.Kusto.Windows.NET",
        "https://c.kustomfa.windows.net",
        "https://c.kusto.azuresynapse.net",
        "https://c.kusto.fabric.microsoft.com",
        "https://c.kusto.usgovcloudapi.net",
        "https://c.kusto.chinacloudapi.cn",
        "https://c.kusto.sovcloud-api.de",
    ],
)
def test_cluster_uri_known_kusto_domains(uri):
    assert config.validate_cluster_uri(uri) == uri


def test_database():
    assert config.validate_database("  My DB-1 ") == "My DB-1"
    for bad in ["", "   ", "a\x00b", "a\nb", "a\tb", "\x7f"]:
        with pytest.raises(ConfigurationError, match="ADX_DATABASE"):
            config.validate_database(bad)


def test_resolvers(monkeypatch):
    assert config.resolve_tenant_id() is None
    assert config.resolve_database() is None
    monkeypatch.setenv("SENTINEL_WORKSPACE_ID", "nope")
    with pytest.raises(ConfigurationError, match="SENTINEL_WORKSPACE_ID"):
        config.resolve_workspace_id()
    assert config.resolve_workspace_id(GUID) == GUID  # explicit value wins
    assert config.resolve_workspace_resource_id(RID) == RID  # explicit value wins
    monkeypatch.setenv("ADX_CLUSTER_URI", "http://c.kusto.windows.net")
    with pytest.raises(ConfigurationError, match="ADX_CLUSTER_URI"):
        config.resolve_cluster_uri()


_SUB_VARS = {
    "SENTINEL_SUBSCRIPTION_ID": GUID,
    "SENTINEL_RESOURCE_GROUP": "my-rg_1.(x)",
    "SENTINEL_WORKSPACE_NAME": "Sentinel-ws1",
}


def _set_resource_vars(monkeypatch, **overrides):
    for var, val in {**_SUB_VARS, **overrides}.items():
        monkeypatch.setenv(var, val)


def test_resolve_resource_id_from_parts(monkeypatch):
    _set_resource_vars(monkeypatch)
    assert config.resolve_workspace_resource_id() == RID
    _set_resource_vars(
        monkeypatch,
        SENTINEL_SUBSCRIPTION_ID=f"  {GUID}\n",
        SENTINEL_RESOURCE_GROUP=" my-rg_1.(x) ",
        SENTINEL_WORKSPACE_NAME="\tSentinel-ws1 ",
    )
    assert config.resolve_workspace_resource_id() == RID  # whitespace stripped


@pytest.mark.parametrize("missing", list(_SUB_VARS))
def test_resolve_resource_id_missing_var(monkeypatch, missing):
    _set_resource_vars(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(ConfigurationError, match=f"No .* configured: set the \\${missing} "):
        config.resolve_workspace_resource_id()
    monkeypatch.setenv(missing, "   ")  # blank counts as invalid, never as valid
    with pytest.raises(ConfigurationError, match=missing):
        config.resolve_workspace_resource_id()


def test_resolve_resource_id_all_unset():
    with pytest.raises(ConfigurationError, match="SENTINEL_SUBSCRIPTION_ID"):
        config.resolve_workspace_resource_id()


@pytest.mark.parametrize(
    ("var", "bad"),
    [
        ("SENTINEL_SUBSCRIPTION_ID", "not-a-guid"),
        ("SENTINEL_SUBSCRIPTION_ID", GUID + "/x"),
        ("SENTINEL_SUBSCRIPTION_ID", RID),
        ("SENTINEL_RESOURCE_GROUP", "a/b"),
        ("SENTINEL_RESOURCE_GROUP", "rg."),
        ("SENTINEL_RESOURCE_GROUP", "r g"),
        ("SENTINEL_RESOURCE_GROUP", "r" * 91),
        ("SENTINEL_RESOURCE_GROUP", "rg/providers/x/workspaces/y"),
        ("SENTINEL_RESOURCE_GROUP", "\u212a"),
        ("SENTINEL_WORKSPACE_NAME", "ws/x"),
        ("SENTINEL_WORKSPACE_NAME", ".."),
        ("SENTINEL_WORKSPACE_NAME", "ws/../x"),
        ("SENTINEL_WORKSPACE_NAME", "abc"),
        ("SENTINEL_WORKSPACE_NAME", "w" * 64),
        ("SENTINEL_WORKSPACE_NAME", "ab_cd"),
        ("SENTINEL_WORKSPACE_NAME", "abc\u212a"),
        ("SENTINEL_WORKSPACE_NAME", "abcd?x=1"),
    ],
)
def test_resolve_resource_id_invalid_var(monkeypatch, var, bad):
    _set_resource_vars(monkeypatch, **{var: bad})
    with pytest.raises(ConfigurationError, match=f"Invalid .{var}"):
        config.resolve_workspace_resource_id()


def test_resolve_resource_id_ignores_old_variable(monkeypatch):
    monkeypatch.setenv("SENTINEL_WORKSPACE_" + "RESOURCE_ID", RID)  # removed variable
    with pytest.raises(ConfigurationError, match="SENTINEL_SUBSCRIPTION_ID"):
        config.resolve_workspace_resource_id()


# --- Workspace URLs on the ADX proxy --------------------------------------------------------------

WS_GUID = "22222222-2222-2222-2222-222222222222"
WS_PATH = (
    f"/subscriptions/{WS_GUID}/resourcegroups/rg"
    "/providers/microsoft.operationalinsights/workspaces/sentinel-ws"
)


@pytest.mark.parametrize(
    "uri",
    [
        f"https://adx.monitor.azure.com{WS_PATH}",
        f"https://ade.loganalytics.io{WS_PATH}",
        f"https://ADX.Monitor.Azure.com{WS_PATH}",
        f"https://adx.monitor.azure.com:443{WS_PATH}",
        f"https://adx.monitor.azure.com/subscriptions/{WS_GUID}/resourceGroups/my_rg.(1)"
        "/providers/Microsoft.OperationalInsights/workspaces/sentinel-ws",
    ],
)
def test_cluster_uri_accepts_a_workspace_url(uri):
    assert config.validate_cluster_uri(f"  {uri}/  ") == uri


def _ws(path, host="adx.monitor.azure.com"):
    return f"https://{host}{path}"


@pytest.mark.parametrize(
    "uri",
    [
        # Not one workspace: the guard would not know which table plans apply.
        _ws(f"/subscriptions/{WS_GUID}"),
        _ws(f"/subscriptions/{WS_GUID}/resourcegroups/rg"),
        _ws(f"{WS_PATH}/tables/Aux_CL"),
        _ws(f"{WS_PATH}/v2/rest/query"),
        _ws(f"{WS_PATH}//"),
        _ws(f"/{WS_PATH}"),
        _ws(WS_PATH.replace("workspaces", "clusters")),
        _ws(WS_PATH.replace("microsoft.operationalinsights", "microsoft.insights")),
        _ws(WS_PATH.replace(WS_GUID, "not-a-guid")),
        # The path may not smuggle another location past the check.
        _ws(WS_PATH.replace("/rg/", "/rg/../other/")),
        _ws(WS_PATH.replace("/rg/", "/rg%2F..%2Fother/")),
        _ws(WS_PATH.replace("sentinel-ws", "sentinel-ws%2Fx")),
        _ws(f"{WS_PATH};x=1"),
        _ws(f"{WS_PATH}?x=1"),
        _ws(f"{WS_PATH}#x"),
        _ws(WS_PATH.replace("sentinel-ws", "sent\u0456nel-ws")),
        # Other hosts: no guard for sovereign clouds, Application Insights, or anything else.
        _ws(WS_PATH, "adx.monitor.azure.us"),
        _ws(WS_PATH, "adx.monitor.azure.cn"),
        _ws(WS_PATH, "adx.loganalytics.azure.com"),
        _ws(WS_PATH, "ade.applicationinsights.io"),
        _ws(WS_PATH, "adx.int.monitor.azure.com"),
        _ws(WS_PATH, "api.securityplatform.microsoft.com"),
        _ws(WS_PATH, "c.kusto.windows.net"),
        _ws(WS_PATH, "adx.monitor.azure.com.evil.net"),
        _ws(WS_PATH, "evil.net"),
        _ws(WS_PATH, "adx.monitor.azure.com:8443"),
        _ws(WS_PATH, "user@adx.monitor.azure.com"),
        f"http://adx.monitor.azure.com{WS_PATH}",
    ],
)
def test_cluster_uri_rejects_anything_but_one_workspace(uri):
    with pytest.raises(ConfigurationError, match="ADX_CLUSTER_URI"):
        config.validate_cluster_uri(uri)


def test_workspace_of_cluster():
    assert config.workspace_of_cluster(_ws(WS_PATH)) == WS_PATH
    assert config.workspace_of_cluster("https://c.kusto.windows.net") is None


def test_resolve_adx_database_on_a_real_cluster(monkeypatch):
    monkeypatch.delenv("ADX_DATABASE", raising=False)
    with pytest.raises(ConfigurationError, match="ADX_DATABASE"):
        config.resolve_adx_database("https://c.kusto.windows.net")
    assert config.resolve_adx_database("https://c.kusto.windows.net", " db ") == "db"
    monkeypatch.setenv("ADX_DATABASE", "env-db")
    assert config.resolve_adx_database("https://c.kusto.windows.net") == "env-db"


def test_resolve_adx_database_on_a_workspace_url(monkeypatch):
    monkeypatch.delenv("ADX_DATABASE", raising=False)
    assert config.resolve_adx_database(_ws(WS_PATH)) == "sentinel-ws"
    assert config.resolve_adx_database(_ws(WS_PATH), "SENTINEL-WS") == "sentinel-ws"
    with pytest.raises(ConfigurationError, match="not the workspace"):
        config.resolve_adx_database(_ws(WS_PATH), "other-ws")
    monkeypatch.setenv("ADX_DATABASE", "other-ws")
    with pytest.raises(ConfigurationError, match="not the workspace"):
        config.resolve_adx_database(_ws(WS_PATH))
