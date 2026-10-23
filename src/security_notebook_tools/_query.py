"""The public ``exec_query`` entry point.

Reads connection settings from the environment on every call and dispatches to the Sentinel or
ADX client. Clients are created on first use and cached per workspace/cluster and tenant. Results
are cached in memory per resolved query (see :func:`exec_query`).
"""

from __future__ import annotations

import logging
import threading
import warnings
from datetime import timedelta
from functools import cache, lru_cache
from typing import Literal

import pandas as pd
from azure.kusto.data import ClientRequestProperties

from ._adx import ADXClient
from ._auth import clear_credential_cache, get_credential
from ._config import (
    DATABASE_VAR,
    require,
    resolve_cluster_uri,
    resolve_tenant_id,
    resolve_workspace_id,
    resolve_workspace_resource_id,
    validate_database,
)
from ._errors import public_call, unexpected_errors
from ._exceptions import ConfigurationError, PartialResultError
from ._log import dependency_call, fingerprint
from ._sentinel import SentinelClient, _table_to_df
from ._tablepolicy import clear_policy_cache
from ._timerange import Timespan, resolve_timespan

logger = logging.getLogger("security_notebook_tools.query")

Target = Literal["sentinel", "adx"]
DEFAULT_TIMEOUT = 300
RESULT_CACHE_SIZE = 32

# Set by the cached fetch functions when they actually run, so the caller can tell a hit from a
# miss without racing on ``cache_info()``.
_fetch = threading.local()


class _PartialResult(Exception):  # noqa: N818 - internal control flow, not an error
    """Raised inside a cached fetch so a partial Sentinel result is returned but not cached."""

    def __init__(self, tables: tuple[pd.DataFrame, ...], message: str) -> None:
        super().__init__(message)
        self.tables = tables
        self.message = message


@cache
def _sentinel_client(workspace_id: str, resource_id: str, tenant_id: str | None) -> SentinelClient:
    """Return the cached Sentinel client for this workspace, resource ID and tenant."""
    with unexpected_errors(logger, "Sentinel client setup"):
        credential = get_credential(tenant_id)
    return SentinelClient(workspace_id, credential, resource_id=resource_id, tenant_id=tenant_id)


@cache
def _adx_client(cluster_uri: str, tenant_id: str | None) -> ADXClient:
    """Return the cached ADX client for this cluster and tenant."""
    # The database is chosen per call, so one client serves every database on the cluster.
    with unexpected_errors(logger, "ADX client setup"):
        credential = get_credential(tenant_id)
    return ADXClient(cluster_uri, credential=credential)


def _client_for(factory, *args):
    """Return ``factory(*args)`` (a ``functools.cache`` function), noting at DEBUG when reused."""
    before = factory.cache_info().currsize
    client = factory(*args)
    if factory.cache_info().currsize == before:
        logger.debug("Reusing the cached %s", type(client).__name__)
    return client


def _clear_clients() -> None:
    """Drop the cached Sentinel and ADX clients so the next call creates new ones."""
    _sentinel_client.cache_clear()
    _adx_client.cache_clear()


@lru_cache(maxsize=RESULT_CACHE_SIZE)
def _sentinel_tables(
    workspace_id: str,
    resource_id: str,
    tenant_id: str | None,
    kql: str,
    timespan: Timespan | None,
    timeout: int,
) -> tuple[pd.DataFrame, ...]:
    """Fetch (and cache) all tables of a Sentinel query; partial results are not cached."""
    _fetch.ran = True
    logger.info("Result cache miss; running the query on Sentinel")
    client = _client_for(_sentinel_client, workspace_id, resource_id, tenant_id)
    try:
        return tuple(
            client.query_tables(kql, timespan, raise_on_partial=True, server_timeout=timeout)
        )
    except PartialResultError as exc:
        # query_tables already logged it; hand the data back without caching it.
        with dependency_call(logger, "pandas DataFrame conversion", target="sentinel_partial"):
            tables = tuple(_table_to_df(t) for t in exc.partial_data or ())
        logger.info("Partial result is returned but not stored in the result cache")
        raise _PartialResult(tables, str(exc)) from exc


@lru_cache(maxsize=RESULT_CACHE_SIZE)
def _adx_tables(
    cluster_uri: str, tenant_id: str | None, database: str, kql: str, timeout: int
) -> tuple[pd.DataFrame, ...]:
    """Fetch (and cache) all result tables of an ADX query or ``.show`` command."""
    _fetch.ran = True
    logger.info("Result cache miss; running the query on ADX")
    client = _client_for(_adx_client, cluster_uri, tenant_id)
    props = ClientRequestProperties()
    props.set_option(
        ClientRequestProperties.request_timeout_option_name, timedelta(seconds=timeout)
    )
    if kql.lstrip().startswith("."):
        return (client.show(kql, database, properties=props),)
    return tuple(client.query_tables(kql, database, properties=props))


def _copies(tables: tuple[pd.DataFrame, ...] | list[pd.DataFrame]) -> list[pd.DataFrame]:
    """Return copies (attrs included) so callers cannot change a cached frame."""
    copies = []
    for df in tables:
        copy = df.copy()
        copy.attrs = {
            key: value.copy() if hasattr(value, "copy") else value
            for key, value in df.attrs.items()
        }
        copies.append(copy)
    return copies


def _cached_call(fetch, args: tuple, *, cache: bool) -> list[pd.DataFrame]:
    """Call the cached ``fetch`` (or its uncached function), logging a cache hit."""
    if not cache:
        logger.info("Result cache bypassed (cache=False); running the query")
        return _copies(fetch.__wrapped__(*args))
    _fetch.ran = False
    tables = fetch(*args)
    if not _fetch.ran:
        logger.info("Returning cached result (clear_cache() or cache=False for fresh data)")
    return _copies(tables)


def _query_sentinel(
    kql: str, timespan: Timespan | None, timeout: int, *, cache: bool = True
) -> list[pd.DataFrame]:
    """Run ``kql`` on Sentinel with the API time range ``timespan`` and return all tables."""
    args = (
        resolve_workspace_id(),
        resolve_workspace_resource_id(),
        resolve_tenant_id(),
        kql,
        timespan,
        timeout,
    )
    try:
        return _cached_call(_sentinel_tables, args, cache=cache)
    except _PartialResult as partial:
        warnings.warn(partial.message, stacklevel=2)
        return _copies(partial.tables)


def _query_adx(kql: str, timeout: int, *, cache: bool = True) -> list[pd.DataFrame]:
    """Run ``kql`` (a query or ``.show`` command) on ADX and return all result tables."""
    tenant_id = resolve_tenant_id()
    cluster_uri = resolve_cluster_uri()
    database = validate_database(require(None, DATABASE_VAR, "database"))
    return _cached_call(_adx_tables, (cluster_uri, tenant_id, database, kql, timeout), cache=cache)


def clear_cache() -> None:
    """Forget cached query results, and the cached clients, credentials and table plans.

    The next query fetches fresh data, creates new clients and credentials, and re-reads the
    Sentinel table plans.

    Example:
        >>> from security_notebook_tools import clear_cache
        >>> clear_cache()
    """
    results = _sentinel_tables.cache_info().currsize + _adx_tables.cache_info().currsize
    _sentinel_tables.cache_clear()
    _adx_tables.cache_clear()
    _clear_clients()
    clear_credential_cache()
    clear_policy_cache()
    logger.info(
        "clear_cache: dropped %d stored result(s), the clients, credentials and table plans",
        results,
    )


def exec_query(
    kql: str,
    timespan: Timespan | None = None,
    *,
    target: Target = "sentinel",
    timeout: int = DEFAULT_TIMEOUT,
    cache: bool = True,
    debug: bool = False,
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

    Caching: results are kept in memory for up to 32 distinct queries (the least recently used
    is dropped), keyed by target, connection settings, exact KQL text, resolved time range and
    timeout. A relative time range (a ``timedelta``, including the default 1 day) is not
    re-evaluated on a hit, so the data is from when the query first ran. Failures and partial
    results are not cached, and a copy is returned each time. On a hit no request is sent, so
    the cost guard check is not re-run and nothing is billed. Use ``cache=False`` or
    ``clear_cache()`` for fresh data.

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
        cache: If true (default), return a cached result for an identical earlier query and
            keep this one for later. Pass false to always run the query (the result is not
            stored and does not replace a cached one). See Caching above.
        debug: If true, also print this call's log records, at DEBUG and above, to the
            notebook cell's output while it runs, including the calls it makes (clients,
            credentials, table policy, the query itself). Default false. The lines are printed
            unredacted (debug output is not written to the file; be careful when sharing a
            notebook saved with debug output). See
            the logging section of the README.

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
            The message adds a hint for common HTTP statuses (401, 403, 504, ...). Subclasses
            mark failures that may succeed if retried: ``QueryThrottledError`` (HTTP 429) and
            ``QueryConnectionError`` (the connection failed before a response arrived: reset,
            refused, timed out, DNS), both under ``QueryTransientError`` (also raised for HTTP
            502/503). None of them is exported. An error nobody anticipated is logged at ERROR
            with its traceback (to the ``security_notebook_tools`` logger) and raised as a
            ``QueryError`` that chains the original.

    Example:
        >>> from datetime import timedelta
        >>> from security_notebook_tools import exec_query
        >>> df = exec_query("SigninLogs | take 100")  # last 1 day by default
        >>> df = exec_query("SigninLogs | where TimeGenerated > ago(7d)")  # set in query
        >>> df = exec_query("SigninLogs | take 100", timedelta(days=7))  # explicit range
        >>> df = exec_query("Events | take 100", target="adx", timeout=600)
        >>> tables = exec_query(".show tables", target="adx")  # management command
    """
    with public_call(
        logger,
        "exec_query",
        target=target,
        timeout=timeout,
        cache=cache,
        debug=debug,
        kql=fingerprint(kql),
    ) as call:
        if timeout <= 0:
            raise ConfigurationError(
                f"timeout must be a positive number of seconds, got {timeout}."
            )
        if target == "sentinel":
            tables = _query_sentinel(kql, resolve_timespan(kql, timespan), timeout, cache=cache)
        elif target == "adx":
            if timespan is not None:
                raise ConfigurationError(
                    "timespan only applies to Sentinel; filter on time in the KQL for ADX."
                )
            tables = _query_adx(kql, timeout, cache=cache)
        else:
            raise ConfigurationError(f"Unknown target {target!r}; use 'sentinel' or 'adx'.")
        df = tables[0] if tables else pd.DataFrame()
        call.update(tables=len(tables), rows=len(df), columns=len(df.columns))
        return df
