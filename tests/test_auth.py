from unittest import mock

import pytest

from security_notebook_tools import _auth as auth
from security_notebook_tools._exceptions import ConfigurationError


@pytest.fixture(autouse=True)
def _clear():
    auth.clear_credential_cache()
    yield
    auth.clear_credential_cache()


def test_cli_credential_cached():
    with mock.patch.object(auth, "AzureCliCredential") as m:
        a = auth.get_credential()
        b = auth.get_credential()
    assert a is b
    m.assert_called_once_with()


def test_cache_per_tenant():
    with mock.patch.object(auth, "AzureCliCredential") as m:
        auth.get_credential(tenant_id="t1")
        auth.get_credential(tenant_id="t2")
        auth.get_credential(tenant_id="t1")
    assert m.call_count == 2
    m.assert_any_call(tenant_id="t1")


def test_tenant_from_env(monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "env-tenant")
    with mock.patch.object(auth, "AzureCliCredential") as m:
        auth.get_credential()
        auth.get_credential(tenant_id="arg-tenant")
    m.assert_any_call(tenant_id="env-tenant")
    m.assert_any_call(tenant_id="arg-tenant")


@pytest.mark.parametrize("tenant", ["a b", "t/../x", "https://x", "t;id", "-", "a..b", "é"])
def test_invalid_tenant(tenant):
    with mock.patch.object(auth, "AzureCliCredential") as m, pytest.raises(ConfigurationError):
        auth.get_credential(tenant_id=tenant)
    m.assert_not_called()


def test_invalid_tenant_from_env(monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "bad tenant")
    with pytest.raises(ConfigurationError, match="AZURE_TENANT_ID"):
        auth.get_credential()


def test_valid_tenants():
    with mock.patch.object(auth, "AzureCliCredential"):
        auth.get_credential(tenant_id="11111111-1111-1111-1111-111111111111")
        auth.get_credential(tenant_id="contoso.onmicrosoft.com")
