import pytest

from security_notebook_tools import _auth, _query
from security_notebook_tools import _config as config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Keep the developer's real env vars and cached clients out of tests."""
    for var in (
        config.TENANT_ID_VAR,
        config.WORKSPACE_ID_VAR,
        config.CLUSTER_URI_VAR,
        config.DATABASE_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    _query._clear_clients()
    _auth.clear_credential_cache()
    yield
    _query._clear_clients()
    _auth.clear_credential_cache()
