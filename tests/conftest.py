import pytest

from security_notebook_tools import _config as config
from security_notebook_tools import _log, _query


@pytest.fixture(autouse=True)
def _log_file(tmp_path, monkeypatch):
    """Send the package's log file to a temp dir so tests never write into the repo."""
    path = tmp_path / "logs" / _log.LOG_FILE_NAME
    path.parent.mkdir()
    monkeypatch.setattr(_log, "log_file_path", lambda: path)
    yield path
    _log.close_log_file()


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
