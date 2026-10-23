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


def test_credential_creation_is_logged_with_a_masked_tenant(caplog):
    caplog.set_level("INFO", logger="security_notebook_tools")
    guid = "1a2b3c4d-5e6f-7a8b-9c0d-112233445566"
    with mock.patch.object(auth, "AzureCliCredential"):
        auth.get_credential(tenant_id=guid)
        auth.get_credential(tenant_id="contoso.onmicrosoft.com")
        auth.get_credential()
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        m.startswith("AzureCliCredential creation: done in") and "tenant=1a2b…" in m
        for m in messages
    )
    assert any("tenant=<id>" in m for m in messages)
    assert any("tenant=CLI default tenant" in m for m in messages)
    assert guid not in caplog.text
    assert "contoso" not in caplog.text


def test_credential_reuse_and_failure(caplog):
    caplog.set_level("DEBUG", logger="security_notebook_tools")
    with mock.patch.object(auth, "AzureCliCredential"):
        auth.get_credential()
        auth.get_credential()
    assert "Reusing cached AzureCliCredential" in caplog.text
    with (
        mock.patch.object(auth, "AzureCliCredential", side_effect=OSError("no az")),
        pytest.raises(OSError),
    ):
        auth.get_credential(tenant_id="other.example")
    assert any(
        r.levelname == "WARNING" and "creation: failed" in r.getMessage() for r in caplog.records
    )
