import pytest

from security_notebook_tools import _config as config
from security_notebook_tools import _query


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Keep the developer's real env vars and cached clients out of tests."""
    for var in (
        config.TENANT_ID_VAR,
        config.WORKSPACE_ID_VAR,
        config.SUBSCRIPTION_ID_VAR,
        config.RESOURCE_GROUP_VAR,
        config.WORKSPACE_NAME_VAR,
        config.CLUSTER_URI_VAR,
        config.DATABASE_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    _query.clear_cache()
    yield
    _query.clear_cache()
