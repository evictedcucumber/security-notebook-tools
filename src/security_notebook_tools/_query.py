"""The public ``exec_query`` entry point.

Reads connection settings from the environment on every call and dispatches to the Sentinel or
ADX client. Clients are created on first use and cached per workspace/cluster and tenant.
"""

from __future__ import annotations

from datetime import timedelta
from functools import cache
from typing import Literal

import pandas as pd
from azure.kusto.data import ClientRequestProperties

from ._adx import ADXClient
from ._auth import get_credential
from ._config import (
    CLUSTER_URI_VAR,
    DATABASE_VAR,
    TENANT_ID_VAR,
    WORKSPACE_ID_VAR,
    from_env,
    require,
)
from ._exceptions import ConfigurationError
from ._log import verbose_logging
from ._sentinel import SentinelClient
from ._timerange import Timespan, resolve_timespan

Target = Literal["sentinel", "adx"]
DEFAULT_TIMEOUT = 300


@cache
def _sentinel_client(workspace_id: str, tenant_id: str | None) -> SentinelClient:
    """Return the cached Sentinel client for this workspace and tenant."""
    return SentinelClient(workspace_id, get_credential(tenant_id))


@cache
def _adx_client(cluster_uri: str, tenant_id: str | None) -> ADXClient:
    """Return the cached ADX client for this cluster and tenant."""
    # The database is chosen per call, so one client serves every database on the cluster.
    return ADXClient(cluster_uri, credential=get_credential(tenant_id))


def _clear_clients() -> None:
    """Drop the cached Sentinel and ADX clients so the next call creates new ones."""
    _sentinel_client.cache_clear()
    _adx_client.cache_clear()


def exec_query(
    kql: str,
    timespan: Timespan | None = None,
    *,
    target: Target = "sentinel",
    timeout: int = DEFAULT_TIMEOUT,
    verbose: bool = False,
) -> pd.DataFrame:
    """Run KQL against Microsoft Sentinel (default) or Azure Data Explorer (ADX).

    Authentication uses the Azure CLI, so run ``az login`` first. Connection details come from
    environment variables, read on every call:

    * Sentinel: ``SENTINEL_WORKSPACE_ID``.
    * ADX: ``ADX_CLUSTER_URI`` and ``ADX_DATABASE``.
    * Both: ``AZURE_TENANT_ID`` (optional; defaults to the Azure CLI's current tenant).

    Time range (Sentinel only) works like the Log Analytics portal's time picker:

    * The KQL has its own time filter and no ``timespan`` is given: no API time range is sent
      ("set in query").
    * The KQL has no time filter and no ``timespan`` is given: the last 1 day is used.
    * ``timespan`` is given and the KQL has no time filter: ``timespan`` is used.
    * ``timespan`` is given and the KQL also has a time filter: both apply (so the narrower
      range wins) and a warning is logged.

    A query counts as having a time filter when a ``where`` or ``filter`` clause references
    ``TimeGenerated`` or ``Timestamp``, a time function (``ago()``, ``now()``, ``datetime()``,
    ``todatetime()``, ``startofday()`` and the other ``startof*``/``endof*`` functions), or a
    ``let`` variable assigned from one of those. Comments and string literals are ignored.
    This is a heuristic: a filter it cannot see, such as one inside a stored function or saved
    query, is not detected, so the 1-day default is applied on top of it. Pass ``timespan``
    explicitly in that case.

    Args:
        kql: The KQL query. With ``target="adx"``, text starting with ``.`` (after leading
            whitespace) is run as a management command such as ``.show tables``.
        timespan: Sentinel only. A ``timedelta`` (the period ending now), a
            ``(start, end)`` tuple of datetimes, or a ``(start, timedelta)`` tuple. See the
            time range rules above. Passing it with ``target="adx"`` raises an error; filter
            on time in the KQL instead.
        target: ``"sentinel"`` (default) or ``"adx"``.
        timeout: Server-side timeout in seconds (default 300); must be positive. Applies to
            both targets.
        verbose: If true, print this call's logs (INFO and above) to the cell output.

    Returns:
        The first result table as a DataFrame, or an empty DataFrame if the query returned no
        tables. Sentinel datetime columns are UTC-aware. If Sentinel returns only a partial
        result, a warning is emitted and the data received is returned.

    Raises:
        ConfigurationError: ``timeout`` is not positive, ``target`` is unknown, ``timespan``
            is combined with ``target="adx"``, or a required environment variable is unset.
            It is also a ``ValueError``.
        QueryError: The service rejected or failed the query (bad KQL, authorisation, ...).

    Example:
        >>> from datetime import timedelta
        >>> from security_notebook_tools import exec_query
        >>> df = exec_query("SigninLogs | take 100")  # last 1 day by default
        >>> df = exec_query("SigninLogs | where TimeGenerated > ago(7d)")  # set in query
        >>> df = exec_query("SigninLogs | take 100", timedelta(days=7))  # explicit range
        >>> df = exec_query("Events | take 100", target="adx", timeout=600)
        >>> tables = exec_query(".show tables", target="adx")  # management command
    """
    if timeout <= 0:
        raise ConfigurationError(f"timeout must be a positive number of seconds, got {timeout}.")
    with verbose_logging(verbose):
        tenant_id = from_env(None, TENANT_ID_VAR)
        if target == "sentinel":
            client = _sentinel_client(require(None, WORKSPACE_ID_VAR, "workspace_id"), tenant_id)
            tables = client.query_tables(
                kql, resolve_timespan(kql, timespan), server_timeout=timeout
            )
        elif target == "adx":
            if timespan is not None:
                raise ConfigurationError(
                    "timespan only applies to Sentinel; filter on time in the KQL for ADX."
                )
            client = _adx_client(require(None, CLUSTER_URI_VAR, "cluster_uri"), tenant_id)
            database = require(None, DATABASE_VAR, "database")
            props = ClientRequestProperties()
            props.set_option(
                ClientRequestProperties.request_timeout_option_name, timedelta(seconds=timeout)
            )
            if kql.lstrip().startswith("."):
                tables = [client.show(kql, database, properties=props)]
            else:
                tables = client.query_tables(kql, database, properties=props)
        else:
            raise ConfigurationError(f"Unknown target {target!r}; use 'sentinel' or 'adx'.")
    return tables[0] if tables else pd.DataFrame()
