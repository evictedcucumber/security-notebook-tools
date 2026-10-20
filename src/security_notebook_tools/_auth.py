"""Azure CLI credential helper with per-tenant caching.

``AzureCliCredential`` is the only supported authentication method; it needs ``az login``.
"""

from __future__ import annotations

import logging
from typing import Any

from azure.identity import AzureCliCredential

from ._config import TENANT_ID_VAR, from_env
from ._log import verbose_logging

logger = logging.getLogger("security_notebook_tools.auth")

_cache: dict[str | None, AzureCliCredential] = {}


def get_credential(
    tenant_id: str | None = None, *, verbose: bool = False, **kwargs: Any
) -> AzureCliCredential:
    """Return a cached ``AzureCliCredential`` (run ``az login`` first).

    ``tenant_id`` defaults to ``$AZURE_TENANT_ID``; if neither is set, the CLI's current tenant
    is used. One credential is cached per tenant. Use :func:`clear_credential_cache` to force
    a new one.

    Args:
        tenant_id: Tenant to authenticate against. Defaults to ``$AZURE_TENANT_ID``.
        verbose: If true, print this call's logs to the cell output.
        **kwargs: Passed to the ``AzureCliCredential`` constructor (only used on first
            creation for that tenant).

    Returns:
        The cached credential for the tenant.
    """
    with verbose_logging(verbose):
        tenant_id = from_env(tenant_id, TENANT_ID_VAR)
        tenant_desc = tenant_id or "CLI default tenant"
        if tenant_id not in _cache:
            logger.info("Creating AzureCliCredential (%s)", tenant_desc)
            tenant_kw = {"tenant_id": tenant_id} if tenant_id else {}
            _cache[tenant_id] = AzureCliCredential(**tenant_kw, **kwargs)
        else:
            logger.debug("Reusing cached AzureCliCredential (%s)", tenant_desc)
        return _cache[tenant_id]


def clear_credential_cache() -> None:
    """Forget all cached credentials."""
    logger.debug("Clearing %d cached credential(s)", len(_cache))
    _cache.clear()
