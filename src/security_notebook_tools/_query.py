"""The public ``exec_query`` entry point.

Reads connection settings from the environment on every call and dispatches to the Sentinel or
ADX client. Clients are created on first use and cached per workspace/cluster and tenant. Results
are cached in memory per resolved query (see :func:`exec_query`).
"""

from __future__ import annotations

import copy
import logging
import threading
import warnings
from collections import OrderedDict
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
from ._render import set_visualization
from ._sentinel import SentinelClient, _table_to_df
from ._tablepolicy import clear_policy_cache
from ._timerange import Timespan, resolve_timespan, validate_timespan

logger = logging.getLogger("security_notebook_tools.query")

Target = Literal["sentinel", "adx"]
DEFAULT_TIMEOUT = 300
RESULT_CACHE_SIZE = 32
# Batch windows are kept apart from the result cache, all windows of each of this many most
# recently used batch queries, so a long batch cannot evict its own windows (see _cached_call).
# Keyed by batch_key, then by the window's fetch arguments.
BATCH_CACHE_SIZE = 4
_batch_results: OrderedDict[tuple, dict[tuple, tuple[pd.DataFrame, ...]]] = OrderedDict()
_batch_lock = threading.Lock()

# Set by the cached fetch functions when they actually run, so the caller can tell a hit from a
# miss without racing on ``cache_info()``.
_fetch = threading.local()


class _PartialResult(Exception):  # noqa: N818 - internal control flow, not an error
    """Raised inside a cached fetch so a partial Sentinel result is returned but not cached."""

    def __init__(
        self, tables: tuple[pd.DataFrame, ...], message: str, error: object = None
    ) -> None:
        super().__init__(message)
        self.tables = tables
        self.message = message
        self.error = error


def _check_kql(kql: object) -> None:
    """Raise ``ConfigurationError`` unless ``kql`` is a non-blank string (never echoes it)."""
    if not isinstance(kql, str):
        raise ConfigurationError(f"kql must be a string, got {type(kql).__name__}.")
    if not kql.strip():
        raise ConfigurationError("kql must not be blank.")


def _check_timeout(timeout: object) -> None:
    """Raise ``ConfigurationError`` unless ``timeout`` is a positive ``int`` (not a bool)."""
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ConfigurationError(
            f"timeout must be a positive integer number of seconds, got {timeout!r}."
        )


def _check_bool(name: str, value: object) -> None:
    """Raise ``ConfigurationError`` unless ``value`` is a ``bool``."""
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a bool, got {value!r}.")


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
    client = _sentinel_client(workspace_id, resource_id, tenant_id)
    try:
        return tuple(
            client.query_tables(kql, timespan, raise_on_partial=True, server_timeout=timeout)
        )
    except PartialResultError as exc:
        # query_tables already logged it; hand the data back without caching it.
        with dependency_call(logger, "pandas DataFrame conversion", target="sentinel_partial"):
            tables = tuple(_table_to_df(t) for t in exc.partial_data or ())
            set_visualization(list(tables), exc.visualization)
        logger.info("Partial result is returned but not stored in the result cache")
        raise _PartialResult(tables, str(exc), exc.partial_error) from exc


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


# Windows of a batch that timed out, so a re-run goes straight to splitting them instead of
# waiting a full timeout again. Keyed like the result cache (target, connection, exact query text
# with the window's let-prefix, API time range, timeout); the value is how long the attempt ran.
# Oldest entries are dropped past this size. Cleared by clear_cache().
TIMED_OUT_SIZE = 512
_timed_out: dict[tuple, float] = {}
_timed_out_lock = threading.Lock()


def window_key(target: Target, kql: str, timespan: Timespan | None, timeout: int) -> tuple:
    """The identity of a batch window's query, as the result cache sees it (reads the env)."""
    if target == "sentinel":
        return (
            "sentinel",
            resolve_workspace_id(),
            resolve_workspace_resource_id(),
            resolve_tenant_id(),
            kql,
            timespan,
            timeout,
        )
    database = validate_database(require(None, DATABASE_VAR, "database"))
    return ("adx", resolve_cluster_uri(), resolve_tenant_id(), database, kql, timeout)


class BatchRun:
    """One run of a batch query: its key in the window store and the windows it has used."""

    def __init__(self, target: Target, kql: str, timeout: int) -> None:
        """Key the run by the batch query: ``kql`` without a window's prefix (reads the env)."""
        self.key = window_key(target, kql, None, timeout)
        self.used: set[tuple] = set()

    def keep_only_used(self) -> None:
        """Drop the stored windows of this batch query that this run did not use.

        Called when the run succeeds, so the store holds one run per batch query, not every
        range it was ever run over; a failed run keeps them all for the re-run.
        """
        with _batch_lock:
            windows = _batch_results.get(self.key, {})
            for args in windows.keys() - self.used:
                del windows[args]


def remember_timed_out(key: tuple, elapsed: float) -> None:
    """Note that the window with this :func:`window_key` timed out after ``elapsed`` seconds."""
    with _timed_out_lock:
        _timed_out.pop(key, None)
        _timed_out[key] = elapsed
        while len(_timed_out) > TIMED_OUT_SIZE:
            del _timed_out[next(iter(_timed_out))]


def timed_out_before(key: tuple) -> float | None:
    """Seconds the window ran before it timed out in an earlier run, or None if unknown."""
    with _timed_out_lock:
        return _timed_out.get(key)


_CONTAINERS = (dict, list, set)


def _detach_containers(df: pd.DataFrame) -> None:
    """Replace dict, list and set cells of object columns of ``df`` by deep copies, in place.

    KQL ``dynamic`` values arrive as Python containers, which ``DataFrame.copy()`` would share
    with the cached frame. Only object columns are scanned (when ``attrs["kql_types"]`` knows the
    column, only a ``dynamic`` one), and only container cells are copied.
    """
    types = df.attrs.get("kql_types") or {}
    for position in range(df.shape[1]):
        kql_type = types.get(df.columns[position])
        if kql_type is not None and kql_type != "dynamic":
            continue
        column = df.iloc[:, position]
        if column.dtype != object:
            continue
        values = column.to_numpy(copy=True)
        changed = False
        for i, value in enumerate(values):
            if isinstance(value, _CONTAINERS):
                values[i] = copy.deepcopy(value)
                changed = True
        if changed:
            df.isetitem(position, pd.Series(values, index=df.index, dtype=object))


def _copies(tables: tuple[pd.DataFrame, ...] | list[pd.DataFrame]) -> list[pd.DataFrame]:
    """Return copies (attrs and nested dict/list/set cells too) so callers cannot change a cache."""
    copies = []
    for df in tables:
        frame = df.copy()
        _detach_containers(frame)
        frame.attrs = {key: copy.deepcopy(value) for key, value in df.attrs.items()}
        copies.append(frame)
    return copies


def _flag_partial(tables: list[pd.DataFrame], partial: bool) -> list[pd.DataFrame]:
    """Set ``attrs["partial"]`` on every table and return them."""
    for df in tables:
        df.attrs["partial"] = partial
    return tables


def _batch_lookup(run: BatchRun, args: tuple) -> tuple[pd.DataFrame, ...] | None:
    """The stored tables of a batch window, or None; marks the batch as most recently used."""
    with _batch_lock:
        run.used.add(args)
        windows = _batch_results.get(run.key)
        if windows is None:
            return None
        _batch_results.move_to_end(run.key)
        return windows.get(args)


def _batch_store(run: BatchRun, args: tuple, tables: tuple[pd.DataFrame, ...]) -> None:
    """Keep the tables of a batch window, dropping the least recently used batch past the limit."""
    with _batch_lock:
        _batch_results.setdefault(run.key, {})[args] = tables
        _batch_results.move_to_end(run.key)
        while len(_batch_results) > BATCH_CACHE_SIZE:
            dropped = len(_batch_results.popitem(last=False)[1])
            logger.info("Dropped %d stored window(s) of an older batch query", dropped)


def _cached_call(
    fetch, args: tuple, *, cache: bool, batch: BatchRun | None = None
) -> list[pd.DataFrame]:
    """Call the cached ``fetch`` (or its uncached function), logging a cache hit.

    With ``batch`` the window is read from and kept in the store of that batch query instead of
    the ``lru_cache`` of ``fetch``: the windows of the :data:`BATCH_CACHE_SIZE` most recent batch
    queries are kept, however many there are, so a re-run after a failure or interrupt finds all
    the windows that succeeded.
    """
    if not cache:
        logger.info("Result cache bypassed (cache=False); running the query")
        return _copies(fetch.__wrapped__(*args))
    if batch is not None:
        tables = _batch_lookup(batch, args)
        if tables is not None:
            logger.info("Returning cached window (clear_cache() or cache=False for fresh data)")
            return _copies(tables)
        tables = fetch.__wrapped__(*args)  # a partial result raises and is not stored
        _batch_store(batch, args, tables)
        return _copies(tables)
    _fetch.ran = False
    tables = fetch(*args)
    if not _fetch.ran:
        logger.info("Returning cached result (clear_cache() or cache=False for fresh data)")
    return _copies(tables)


def _warn_retention(kql: str, timespan: Timespan | None) -> None:
    """Warn if ``timespan`` is longer than the Analytics retention of the tables ``kql`` reads."""
    # This is the first client lookup of a call (and the only one on a result cache hit), so it
    # logs the reuse; _sentinel_tables then looks the client up without logging it again.
    client = _client_for(
        _sentinel_client,
        resolve_workspace_id(),
        resolve_workspace_resource_id(),
        resolve_tenant_id(),
    )
    client.warn_retention(kql, timespan)


def _query_sentinel(
    kql: str,
    timespan: Timespan | None,
    timeout: int,
    *,
    cache: bool = True,
    check_retention: bool = True,
    raise_on_partial: bool = False,
    warn_partial: bool = True,
    batch: BatchRun | None = None,
) -> list[pd.DataFrame]:
    """Run ``kql`` on Sentinel with the API time range ``timespan`` and return all tables.

    The retention warning is issued here, before the result cache, so a cached repeat warns too.
    A batch passes ``check_retention=False`` and warns once for its whole range instead, and
    ``warn_partial=False`` to summarise partial windows itself. Every returned table has
    ``attrs["partial"]``. A partial result raises ``PartialResultError`` with ``raise_on_partial``;
    otherwise it is returned (an empty table stands in if none arrived) and not cached. A batch
    window passes ``batch`` (see :func:`_cached_call`).
    """
    if check_retention:
        _warn_retention(kql, timespan)
    args = (
        resolve_workspace_id(),
        resolve_workspace_resource_id(),
        resolve_tenant_id(),
        kql,
        timespan,
        timeout,
    )
    try:
        tables = _cached_call(_sentinel_tables, args, cache=cache, batch=batch)
        return _flag_partial(tables, False)
    except _PartialResult as partial:
        # built fresh for this call and never cached, so no copy is needed
        tables = list(partial.tables) or [pd.DataFrame()]
        if raise_on_partial:
            raise PartialResultError(
                partial.message,
                partial.error,
                _flag_partial(tables, True),
                visualization=tables[0].attrs.get("visualization"),
            ) from partial
        if warn_partial:
            warnings.warn(partial.message, stacklevel=2)
        return _flag_partial(tables, True)


def _query_adx(
    kql: str, timeout: int, *, cache: bool = True, batch: BatchRun | None = None
) -> list[pd.DataFrame]:
    """Run ``kql`` (a query or ``.show`` command) on ADX and return all result tables.

    A batch window passes ``batch`` (see :func:`_cached_call`).
    """
    tenant_id = resolve_tenant_id()
    cluster_uri = resolve_cluster_uri()
    database = validate_database(require(None, DATABASE_VAR, "database"))
    args = (cluster_uri, tenant_id, database, kql, timeout)
    return _flag_partial(_cached_call(_adx_tables, args, cache=cache, batch=batch), False)


def clear_cache() -> None:
    """Forget cached query results, and the cached clients, credentials and table plans.

    The next query fetches fresh data, creates new clients and credentials, and re-reads the
    Sentinel table plans. It also forgets which batch windows timed out (see ``min_interval`` of
    ``exec_batch_query``), so they are tried whole again.

    Example:
        >>> from security_notebook_tools import clear_cache
        >>> clear_cache()
    """
    results = _sentinel_tables.cache_info().currsize + _adx_tables.cache_info().currsize
    _sentinel_tables.cache_clear()
    _adx_tables.cache_clear()
    with _batch_lock:
        results += sum(len(windows) for windows in _batch_results.values())
        _batch_results.clear()
    _clear_clients()
    clear_credential_cache()
    clear_policy_cache()
    with _timed_out_lock:
        _timed_out.clear()
    logger.info(
        "clear_cache: dropped %d stored result(s), the clients, credentials, table plans and "
        "the memory of timed-out batch windows",
        results,
    )


def exec_query(
    kql: str,
    timespan: Timespan | None = None,
    *,
    target: Target = "sentinel",
    timeout: int = DEFAULT_TIMEOUT,
    cache: bool = True,
    raise_on_partial: bool = False,
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
    ``clear_cache()`` for fresh data. The windows of ``exec_batch_query`` are cached apart from
    these 32 (see there).

    Args:
        kql: The KQL query, a non-blank string. With ``target="adx"``, text starting with ``.``
            (after leading whitespace) is run as a management command, but only ``.show``
            commands (such as ``.show tables``, case-insensitive) are allowed; any other command
            raises ``ConfigurationError`` without contacting the service. Every ADX request is sent
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
        timeout: Server-side timeout in seconds (default 300); must be a positive
            ``int``. Applies to both targets.
        cache: If true (default), return a cached result for an identical earlier query and
            keep this one for later. Pass false to always run the query (the result is not
            stored and does not replace a cached one). See Caching above.
        raise_on_partial: Sentinel only. If true, a partial result (the service returned some
            data and an error) raises ``PartialResultError`` instead of being returned with a
            warning. Default false. Has no effect with ``target="adx"``.
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
        If the query ends in ``| render <charttype> [with (...)]``, ``df.attrs["visualization"]``
        is a dict of the render spec (and absent otherwise) for ``render_dataframe``: the key
        ``"visualization"`` is the lower-case chart type and, when the render sets them,
        ``"title"``, ``"xtitle"``, ``"ytitle"``, ``"xcolumn"``, ``"kind"``, ``"legend"``,
        ``"xaxis"``, ``"yaxis"``, ``"ysplit"`` (strings), ``"ycolumns"``, ``"series"``,
        ``"anomalycolumns"`` (lists of column names), ``"ymin"``, ``"ymax"``, ``"xmin"``,
        ``"xmax"`` and ``"accumulate"`` (only when true). It is a copy, also from the cache. It is
        set on the table it applies to (the first one on Sentinel), also for a partial result.
        ``df.attrs["partial"]`` is always a bool: True if Sentinel returned only a partial result
        (a warning is emitted and the data received is returned, so the frame may be incomplete),
        False otherwise, including on ADX. A partial result is never cached. Nested ``dynamic``
        values (dicts and lists) are copies: changing them does not alter the cached result. On
        Sentinel, a ``UserWarning`` is also emitted (on cached repeats too) when the time range
        starts before the Analytics retention of a table the query reads: the older rows are in
        long-term retention, which these queries cannot read. Nothing is skipped.

    Raises:
        ConfigurationError: ``kql`` is not a string or is blank, ``timeout`` is not a positive
            integer, ``cache``, ``raise_on_partial`` or ``debug`` is not a bool, ``target`` is
            unknown, ``timespan`` is not one of the accepted forms (a positive ``timedelta``, or
            a ``(start, end)`` / ``(start, timedelta)`` tuple of datetimes with a positive
            length), ``timespan``
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
        PartialResultError: Only with ``raise_on_partial=True``, on Sentinel: the result was
            partial. A ``QueryError``; ``partial_error`` holds the service error and
            ``partial_data`` the list of DataFrames received so far (each with
            ``attrs["partial"]`` True).

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
        raise_on_partial=raise_on_partial,
        debug=debug,
        kql=fingerprint(kql),
    ) as call:
        _check_kql(kql)
        _check_timeout(timeout)
        _check_bool("cache", cache)
        _check_bool("raise_on_partial", raise_on_partial)
        _check_bool("debug", debug)
        if target == "sentinel":
            validate_timespan(timespan)
            tables = _query_sentinel(
                kql,
                resolve_timespan(kql, timespan),
                timeout,
                cache=cache,
                raise_on_partial=raise_on_partial,
            )
        elif target == "adx":
            if timespan is not None:
                raise ConfigurationError(
                    "timespan only applies to Sentinel; filter on time in the KQL for ADX."
                )
            tables = _query_adx(kql, timeout, cache=cache)
        else:
            raise ConfigurationError(f"Unknown target {target!r}; use 'sentinel' or 'adx'.")
        df = tables[0] if tables else pd.DataFrame()
        df.attrs.setdefault("partial", False)
        call.update(tables=len(tables), rows=len(df), columns=len(df.columns))
        return df
