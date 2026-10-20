from unittest import mock

import pytest

from security_notebook_tools import _auth as auth


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
