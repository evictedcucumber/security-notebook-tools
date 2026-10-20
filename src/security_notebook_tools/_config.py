"""Environment-variable names and lookup helpers.

Variables are read at call time, never cached, so changing ``os.environ`` in a notebook takes
effect on the next call. An explicit value always takes precedence over the variable.
"""

from __future__ import annotations

import os

from ._exceptions import ConfigurationError

TENANT_ID_VAR = "AZURE_TENANT_ID"
WORKSPACE_ID_VAR = "SENTINEL_WORKSPACE_ID"
CLUSTER_URI_VAR = "ADX_CLUSTER_URI"
DATABASE_VAR = "ADX_DATABASE"


def from_env(value: str | None, var: str) -> str | None:
    """Return ``value`` if given, else the (non-empty) environment variable ``var``, else None."""
    return value or os.environ.get(var) or None


def require(value: str | None, var: str, what: str) -> str:
    """Like :func:`from_env`, but raise :class:`ConfigurationError` when neither is set."""
    resolved = from_env(value, var)
    if not resolved:
        raise ConfigurationError(f"No {what} configured: set the ${var} environment variable.")
    return resolved
