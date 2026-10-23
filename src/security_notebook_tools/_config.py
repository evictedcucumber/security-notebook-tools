"""Environment-variable names and lookup helpers.

Variables are read at call time, never cached, so changing ``os.environ`` in a notebook takes
effect on the next call. An explicit value always takes precedence over the variable.

Every resolved value is validated before use (see the ``resolve_*`` functions), so a malformed
setting fails early with a :class:`ConfigurationError` instead of reaching the Azure SDKs.

Logging (``security_notebook_tools.config``) is DEBUG only and names the variable and where the
value came from, never the value. A rejected value is reported once, at the public function
that was called, with the value masked.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from urllib.parse import urlsplit

from ._exceptions import ConfigurationError

logger = logging.getLogger("security_notebook_tools.config")

TENANT_ID_VAR = "AZURE_TENANT_ID"
WORKSPACE_ID_VAR = "SENTINEL_WORKSPACE_ID"
SUBSCRIPTION_ID_VAR = "SENTINEL_SUBSCRIPTION_ID"
RESOURCE_GROUP_VAR = "SENTINEL_RESOURCE_GROUP"
WORKSPACE_NAME_VAR = "SENTINEL_WORKSPACE_NAME"
CLUSTER_URI_VAR = "ADX_CLUSTER_URI"
DATABASE_VAR = "ADX_DATABASE"

_GUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_DOMAIN_RE = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
# ASCII-only so IGNORECASE cannot match look-alike characters (such as U+212A KELVIN SIGN).
_RESOURCE_GROUP_PATTERN = r"[A-Za-z0-9_.()-]{0,89}[A-Za-z0-9_()-]"  # 1-90 chars, not ending in '.'
_WORKSPACE_NAME_PATTERN = r"[A-Za-z0-9-]{4,63}"
_RESOURCE_GROUP_RE = re.compile(_RESOURCE_GROUP_PATTERN, re.ASCII)
_WORKSPACE_NAME_RE = re.compile(_WORKSPACE_NAME_PATTERN, re.ASCII)
_RESOURCE_ID_RE = re.compile(
    rf"/subscriptions/{_GUID_RE.pattern}"
    rf"/resourceGroups/{_RESOURCE_GROUP_PATTERN}"
    r"/providers/Microsoft\.OperationalInsights"
    rf"/workspaces/{_WORKSPACE_NAME_PATTERN}",
    re.IGNORECASE | re.ASCII,
)
# Kusto cluster domains, taken from the trusted-endpoint list of azure-kusto-data
# (wellKnownKustoEndpoints.json): public, US government, China, and the other sovereign clouds.
# Only real clusters are accepted. The Sentinel data lake (``api.securityplatform.microsoft.com``)
# and the Log Analytics / Application Insights / Azure Monitor ADX proxies (``ade.*``,
# ``adx.*``) are on that list too but reach Basic, Auxiliary and data lake data, which bill per
# GB scanned, so they are deliberately absent here.
_KUSTO_SUFFIXES = (
    ".kusto.windows.net",
    ".kustomfa.windows.net",
    ".kusto.azuresynapse.net",
    ".kusto.fabric.microsoft.com",
    ".kusto.data.microsoft.com",
    ".kusto.usgovcloudapi.net",
    ".kustomfa.usgovcloudapi.net",
    ".kusto.chinacloudapi.cn",
    ".kustomfa.chinacloudapi.cn",
    ".kusto.azuresynapse.azure.cn",
    ".kusto.core.eaglex.ic.gov",
    ".kustomfa.core.eaglex.ic.gov",
    ".kusto.core.microsoft.scloud",
    ".kustomfa.core.microsoft.scloud",
    ".kusto.sovcloud-api.fr",
    ".kustomfa.sovcloud-api.fr",
    ".kusto.sovcloud-api.de",
    ".kustomfa.sovcloud-api.de",
    ".kusto.sovcloud-api.sg",
    ".kustomfa.sovcloud-api.sg",
)
_LAKE_DOMAIN = "securityplatform.microsoft.com"


def from_env(value: str | None, var: str) -> str | None:
    """Return ``value`` if given, else the (non-empty) environment variable ``var``, else None."""
    if value:
        logger.debug("%s: using the value passed in", var)
        return value
    resolved = os.environ.get(var) or None
    logger.debug("%s: %s", var, "read from the environment" if resolved else "not set")
    return resolved


def require(value: str | None, var: str, what: str) -> str:
    """Like :func:`from_env`, but raise :class:`ConfigurationError` when neither is set."""
    resolved = from_env(value, var)
    if not resolved:
        raise ConfigurationError(f"No {what} configured: set the ${var} environment variable.")
    return resolved


def validate_guid(value: str, var: str) -> str:
    """Check that ``value`` is a GUID in the canonical 8-4-4-4-12 hex form.

    Args:
        value: The candidate value; surrounding whitespace is ignored.
        var: Environment variable name, used in the error message.

    Returns:
        The value with surrounding whitespace removed.

    Raises:
        ConfigurationError: ``value`` is not a GUID.
    """
    value = value.strip()
    if not _GUID_RE.fullmatch(value):
        raise ConfigurationError(f"Invalid ${var}: expected a GUID, got {value!r}.")
    return value


def validate_resource_group(value: str, var: str = RESOURCE_GROUP_VAR) -> str:
    """Check that ``value`` is an Azure resource group name.

    The name is 1-90 ASCII letters, digits and ``_ - . ( )``, not ending in ``.``. Slashes,
    spaces and non-ASCII look-alike characters are rejected.

    Args:
        value: The candidate name; surrounding whitespace is ignored.
        var: Environment variable name, used in the error message.

    Returns:
        The name with surrounding whitespace removed.

    Raises:
        ConfigurationError: ``value`` is not a valid resource group name.
    """
    value = value.strip()
    if not _RESOURCE_GROUP_RE.fullmatch(value):
        raise ConfigurationError(
            f"Invalid ${var}: expected 1-90 letters, digits and '_', '-', '.', '(', ')' "
            f"(not ending in '.'), got {value!r}."
        )
    return value


def validate_workspace_name(value: str, var: str = WORKSPACE_NAME_VAR) -> str:
    """Check that ``value`` is a Log Analytics workspace name.

    The name is 4-63 ASCII letters, digits and ``-``. Slashes, dots and non-ASCII look-alike
    characters are rejected.

    Args:
        value: The candidate name; surrounding whitespace is ignored.
        var: Environment variable name, used in the error message.

    Returns:
        The name with surrounding whitespace removed.

    Raises:
        ConfigurationError: ``value`` is not a valid workspace name.
    """
    value = value.strip()
    if not _WORKSPACE_NAME_RE.fullmatch(value):
        raise ConfigurationError(
            f"Invalid ${var}: expected 4-63 letters, digits and '-', got {value!r}."
        )
    return value


def validate_workspace_resource_id(value: str, var: str | None = None) -> str:
    """Check that ``value`` is a Log Analytics workspace resource ID.

    The form is ``/subscriptions/<GUID>/resourceGroups/<group>/providers/
    Microsoft.OperationalInsights/workspaces/<name>``, compared case-insensitively. The resource
    group is 1-90 letters, digits and ``_ - . ( )``, not ending in ``.``; the workspace name is
    4-63 letters, digits and ``-``. No trailing slash, query string or other characters.

    Args:
        value: The candidate value; surrounding whitespace is ignored.
        var: Optional environment variable name to mention in the error message.

    Returns:
        The value with surrounding whitespace removed (its case is preserved).

    Raises:
        ConfigurationError: ``value`` is not a workspace resource ID.
    """
    value = value.strip()
    if not _RESOURCE_ID_RE.fullmatch(value):
        subject = f"${var}" if var else "workspace resource ID"
        raise ConfigurationError(
            f"Invalid {subject}: expected /subscriptions/<GUID>/resourceGroups/<group>/providers/"
            f"Microsoft.OperationalInsights/workspaces/<name>, got {value!r}."
        )
    return value


def validate_tenant_id(value: str, var: str = TENANT_ID_VAR) -> str:
    """Check that ``value`` is a tenant GUID or a DNS-style domain name.

    A domain is dot-separated labels of letters, digits and ``-`` (never at the start or end of
    a label): no spaces, slashes or other characters.

    Args:
        value: The candidate value; surrounding whitespace is ignored.
        var: Environment variable name, used in the error message.

    Returns:
        The value with surrounding whitespace removed.

    Raises:
        ConfigurationError: ``value`` is neither a GUID nor a domain name.
    """
    value = value.strip()
    if not _DOMAIN_RE.fullmatch(value):
        raise ConfigurationError(
            f"Invalid ${var}: expected a GUID or domain name (letters, digits, '-', '.'), "
            f"got {value!r}."
        )
    return value


def validate_cluster_uri(value: str, var: str = CLUSTER_URI_VAR) -> str:
    """Check that ``value`` is a plain ``https://<host>`` ADX cluster URI.

    The scheme must be exactly ``https`` and the host a fully qualified DNS name. Userinfo,
    ports other than 443, paths (other than ``/``), query strings and fragments are rejected.
    The host must end in a Kusto cluster domain (``.kusto.windows.net``, ``.kusto.fabric.
    microsoft.com``, the sovereign-cloud equivalents ...; see ``_KUSTO_SUFFIXES``). The Sentinel
    data lake (``*.securityplatform.microsoft.com``) and the Log Analytics / Application
    Insights ADX proxy (``ade.loganalytics.io``, ``ade.applicationinsights.io``, ``adx.*``) are
    rejected, because they reach Basic, Auxiliary and data lake data that bill per GB scanned.

    Args:
        value: The candidate URI; surrounding whitespace is ignored.
        var: Environment variable name, used in the error message.

    Returns:
        The URI without a trailing slash.

    Raises:
        ConfigurationError: ``value`` is not an acceptable cluster URI.
    """
    value = value.strip()

    def invalid(reason: str) -> ConfigurationError:
        return ConfigurationError(f"Invalid ${var}: {reason}, got {value!r}.")

    if not value.startswith("https://"):
        raise invalid("the scheme must be https")
    if any(unicodedata.category(c) in ("Cc", "Zs") or c == "\\" for c in value):
        raise invalid("whitespace, control characters and backslashes are not allowed")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise invalid("it could not be parsed") from None
    if "@" in parts.netloc:
        raise invalid("user info is not allowed")
    host = (parts.hostname or "").lower()
    if host == _LAKE_DOMAIN or host.endswith("." + _LAKE_DOMAIN):
        raise invalid(
            "the Sentinel data lake is not allowed (it bills per GB scanned); use a real ADX "
            "cluster"
        )
    if "?" in value or "#" in value:
        raise invalid("query strings and fragments are not allowed")
    if parts.path not in ("", "/"):
        raise invalid("a path is not allowed")
    if ":" in parts.netloc and port != 443:
        raise invalid("only the default port (443) is allowed")
    # A fully qualified name is required: single-label hosts such as ``localhost`` would bypass
    # azure-kusto-data's trusted-endpoint check, which exempts local addresses.
    if not parts.hostname or "." not in parts.hostname or not _DOMAIN_RE.fullmatch(parts.hostname):
        raise invalid("a fully qualified DNS host name is required")
    if not host.endswith(_KUSTO_SUFFIXES):
        raise invalid(
            "the host is not a Kusto cluster domain (such as *.kusto.windows.net). The Log "
            "Analytics / Application Insights / Azure Monitor ADX proxy and the Sentinel data "
            "lake are not allowed because they bill per GB scanned"
        )
    return value.rstrip("/") if parts.path == "/" else value


def validate_database(value: str, var: str = DATABASE_VAR) -> str:
    """Check that ``value`` is a usable ADX database name.

    Permissive: only empty or whitespace-only names and control characters are rejected.

    Args:
        value: The candidate name; surrounding whitespace is ignored.
        var: Environment variable name, used in the error message.

    Returns:
        The name with surrounding whitespace removed.

    Raises:
        ConfigurationError: The name is blank or contains control characters.
    """
    stripped = value.strip()
    if not stripped:
        raise ConfigurationError(f"Invalid ${var}: the database name is blank.")
    if any(unicodedata.category(c) == "Cc" for c in stripped):
        raise ConfigurationError(f"Invalid ${var}: control characters are not allowed.")
    return stripped


def resolve_workspace_id(value: str | None = None) -> str:
    """Return the validated workspace ID (argument, else ``$SENTINEL_WORKSPACE_ID``)."""
    return validate_guid(require(value, WORKSPACE_ID_VAR, "workspace_id"), WORKSPACE_ID_VAR)


def resolve_workspace_resource_id(value: str | None = None) -> str:
    """Return the validated workspace ARM resource ID.

    An explicit ``value`` (internal and test use) is validated as a full resource ID. Otherwise
    the ID is assembled from ``$SENTINEL_SUBSCRIPTION_ID``, ``$SENTINEL_RESOURCE_GROUP`` and
    ``$SENTINEL_WORKSPACE_NAME``, all three required and each validated; the assembled string is
    validated again as defense in depth.

    Raises:
        ConfigurationError: A variable is missing or invalid.
    """
    if value:
        return validate_workspace_resource_id(value)
    subscription = validate_guid(
        require(None, SUBSCRIPTION_ID_VAR, "subscription_id"), SUBSCRIPTION_ID_VAR
    )
    group = validate_resource_group(require(None, RESOURCE_GROUP_VAR, "resource_group"))
    name = validate_workspace_name(require(None, WORKSPACE_NAME_VAR, "workspace_name"))
    return validate_workspace_resource_id(
        f"/subscriptions/{subscription}/resourceGroups/{group}"
        f"/providers/Microsoft.OperationalInsights/workspaces/{name}"
    )


def resolve_tenant_id(value: str | None = None) -> str | None:
    """Return the validated tenant (argument, else ``$AZURE_TENANT_ID``), or None if unset."""
    resolved = from_env(value, TENANT_ID_VAR)
    return validate_tenant_id(resolved) if resolved is not None else None


def resolve_cluster_uri(value: str | None = None) -> str:
    """Return the validated cluster URI (argument, else ``$ADX_CLUSTER_URI``)."""
    return validate_cluster_uri(require(value, CLUSTER_URI_VAR, "cluster_uri"))


def resolve_database(value: str | None = None) -> str | None:
    """Return the validated database (argument, else ``$ADX_DATABASE``), or None if unset."""
    resolved = from_env(value, DATABASE_VAR)
    return validate_database(resolved) if resolved is not None else None
