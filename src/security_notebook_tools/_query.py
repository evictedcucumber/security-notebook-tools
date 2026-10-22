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
    DATABASE_VAR,
    require,
    resolve_cluster_uri,
    resolve_tenant_id,
    resolve_workspace_id,
    resolve_workspace_resource_id,
    validate_database,
)
from ._exceptions import ConfigurationError
from ._log import verbose_logging
from ._sentinel import SentinelClient
from ._timerange import Timespan, resolve_timespan

Target = Literal["sentinel", "adx"]
DEFAULT_TIMEOUT = 300


@cache
def _sentinel_client(workspace_id: str, resource_id: str, tenant_id: str | None) -> SentinelClient:
    """Return the cached Sentinel client for this workspace, resource ID and tenant."""
    return SentinelClient(
        workspace_id, get_credential(tenant_id), resource_id=resource_id, tenant_id=tenant_id
    )


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

    * Sentinel: ``SENTINEL_WORKSPACE_ID``, ``SENTINEL_SUBSCRIPTION_ID``,
      ``SENTINEL_RESOURCE_GROUP`` and ``SENTINEL_WORKSPACE_NAME`` (the last three locate the
      workspace in Azure for the cost guard below).
    * ADX: ``ADX_CLUSTER_URI`` and ``ADX_DATABASE``.
    * Both: ``AZURE_TENANT_ID`` (optional; defaults to the Azure CLI's current tenant).

    Cost guard (Sentinel only): Basic, Auxiliary and Sentinel data lake tables bill per GB
    scanned, so a query may only read tables on the Log Analytics Analytics plan (this includes
    Defender XDR tables streamed into the workspace). Before each query the table plans and
    saved functions of the workspace are read from Azure Resource Manager (cached for 5
    minutes; needs ``tables/read`` and ``savedSearches/read``, e.g. Log Analytics Reader) and
    the KQL is checked client-side; a query that fails the check is never sent. Rejected:

    * Any table that is not on the Analytics plan, wherever its name appears, and unknown names.
      Table names are case-sensitive: ``signinlogs`` is rejected, use ``SigninLogs``.
    * Every tabular source (start of a statement, ``let`` value, ``union``/``join``/``lookup``
      operand, subquery, ``materialize()``/``toscalar()``/``view()`` argument) must be a
      ``let``-bound name or an Analytics table.
    * Any call to a function that is not a built-in KQL scalar, aggregate or window function
      and not defined with ``let f = (...) { ... }`` in the query. This includes saved
      functions and parsers, the built-in Sentinel parsers (``_Im_*``, ``_ASim_*``) and
      ``_GetWatchlist()``.
    * Any operator after a pipe that is not a known tabular operator (``where``, ``project``,
      ``extend``, ``summarize``, ``top``, ``sort``, ``join``, ``union``, ``mv-expand``,
      ``parse``, ``make-series``, ``render``, ``partition``, ``fork``, ``graph-match`` ...; for
      example ``graph-merge`` and ``macro-expand`` are rejected). The tabular operands of
      ``join``, ``lookup``, ``union``, ``invoke``, ``make-graph``, ``fork``, ``facet``,
      ``partition`` and ``mv-apply`` are checked like any other source.
    * ``search``, ``find``, ``union`` with a wildcard, ``table()``, ``workspace()``, ``app()``,
      ``resource()``, ``adx()``, ``arg()`` (other than the form above), ``cluster()``,
      ``database()``, ``external_table()``, ``externaldata``, calls through escaped names, and
      ``evaluate`` plugins that read external data or run code (``sql_request``,
      ``http_request``, ``python`` ...). Ordinary plugins such as ``bag_unpack`` are fine.
    * KQL it cannot analyse with certainty (unterminated strings, control characters,
      non-ASCII outside strings and comments, unbalanced brackets).

    Azure Resource Graph is allowed (it is free and read-only): ``arg("").<table>`` with an
    empty plain string and one of the documented ARG tables (``Resources``,
    ``ResourceContainers``, ``SecurityResources`` ...; case-insensitive) is a valid table
    source. It is an Azure Monitor Preview feature capped at 1,000 rows, and a ``join`` with a
    Log Analytics table needs ``hint.remote=left`` or ``hint.remote=right``. Any other
    ``arg()`` form is rejected.

    If the table plans cannot be read, the query is not sent (it fails closed). The check is a
    conservative text analysis (it may reject unusual but harmless KQL) and is a guard, not a
    guarantee: use an identity limited to Analytics tables for a server-enforced limit.

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
            whitespace) is run as a management command, but only ``.show`` commands (such as
            ``.show tables``, case-insensitive) are allowed; any other command raises
            ``ConfigurationError`` without contacting the service. Every ADX request is sent
            read-only (``request_readonly``), so the server also refuses writes. ADX text
            using ``cluster()``, ``external_table()``, ``externaldata`` or a data-reading
            ``evaluate`` plugin is rejected too, and the cluster URI may not be the Sentinel
            data lake or the Log Analytics proxy. With ``target="sentinel"`` the query must
            pass the cost guard above.
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
        tables. Sentinel datetime columns are UTC-aware. ``df.attrs["kql_types"]`` maps each
        column to its KQL type (``"long"``, ``"dynamic"`` ...), which ``format_dataframe`` uses.
        If Sentinel returns only a partial result, a warning is emitted and the data received is
        returned.

    Raises:
        ConfigurationError: ``timeout`` is not positive, ``target`` is unknown, ``timespan``
            is combined with ``target="adx"``, a required environment variable is unset or
            invalid (workspace ID not a GUID, workspace resource ID not an
            ``/subscriptions/.../workspaces/<name>`` ID, tenant not a GUID or domain name,
            cluster URI not a plain ``https://host`` URI on a Kusto cluster domain, blank
            database), the workspace ID and resource ID name different workspaces, an ADX
            management command is not ``.show``, ADX KQL uses ``cluster()``,
            ``external_table()``, ``externaldata`` or a data-reading plugin, or the Sentinel
            cost guard rejects the query. It is also a ``ValueError``.
        QueryError: The service rejected or failed the query (bad KQL, authorisation, ...), or
            the Sentinel table plans could not be verified (the query was then not sent).

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
        tenant_id = resolve_tenant_id()
        if target == "sentinel":
            client = _sentinel_client(
                resolve_workspace_id(), resolve_workspace_resource_id(), tenant_id
            )
            tables = client.query_tables(
                kql, resolve_timespan(kql, timespan), server_timeout=timeout
            )
        elif target == "adx":
            if timespan is not None:
                raise ConfigurationError(
                    "timespan only applies to Sentinel; filter on time in the KQL for ADX."
                )
            client = _adx_client(resolve_cluster_uri(), tenant_id)
            database = validate_database(require(None, DATABASE_VAR, "database"))
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
