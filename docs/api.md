# API reference

The public API is exactly these names, importable from `security_notebook_tools`:

```python
from security_notebook_tools import (
    exec_query,
    format_dataframe,
    pivot_dataframe,
    clear_cache,
)
import security_notebook_tools

security_notebook_tools.__version__  # "4.0.0"
```

Submodules are private and may change. The exception classes are not exported.

## exec_query

```python
exec_query(
    kql: str,
    timespan: Timespan | None = None,
    *,
    target: Literal["sentinel", "adx"] = "sentinel",
    timeout: int = 300,
    cache: bool = True,
    debug: bool = False,
) -> pandas.DataFrame
```

Run KQL against Microsoft Sentinel (default) or Azure Data Explorer (ADX). Authentication uses
the Azure CLI, so run `az login` first. Sentinel queries pass the [cost guard](#cost-guard-sentinel-only).

`Timespan` is `timedelta | tuple[datetime, datetime] | tuple[datetime, timedelta]`.

### Parameters

| Name | Description |
| --- | --- |
| `kql` | The KQL query. With `target="adx"`, text starting with `.` (after leading whitespace) is run as a management command, but only `.show` commands (such as `.show tables`, case-insensitive) are allowed; any other command raises `ConfigurationError` without contacting the service. Every ADX request is sent read-only (`request_readonly`), and ADX text using `cluster()`, `external_table()`, `externaldata` or a data-reading `evaluate` plugin (`sql_request`, `http_request`, `python`, `r` ...) is rejected, as is text with an unterminated string, control characters or non-ASCII characters outside strings and comments. With `target="sentinel"` it must pass the cost guard. |
| `timespan` | Sentinel only. A `timedelta` (the period ending now), a `(start, end)` tuple of datetimes, or a `(start, timedelta)` tuple. Passing it with `target="adx"` raises `ConfigurationError`; filter on time in the KQL instead. |
| `target` | `"sentinel"` (default) or `"adx"`. |
| `timeout` | Server-side timeout in seconds (default 300); must be positive. Applies to both targets. |
| `cache` | If true (default), reuse a cached result for the same query and send nothing; if false, always run it and neither read nor write the cache. See [Caching](#caching). |
| `debug` | If true, also print this call's log records, at DEBUG and above, to the notebook cell's output while it runs, including the calls it makes (clients, credentials, table policy, the query itself). Default false. The lines are printed unredacted (debug output is not written to the file; be careful when sharing a notebook saved with debug output). See [Debug output](#debug-output-in-the-cell). |

`target`, `timeout`, `cache` and `debug` are keyword-only.

### Environment variables

Read on every call (not cached), so changing `os.environ` takes effect immediately.

| Variable | Used for | Required |
| --- | --- | --- |
| `SENTINEL_WORKSPACE_ID` | Sentinel workspace ID (a GUID) | `target="sentinel"` |
| `SENTINEL_SUBSCRIPTION_ID` | Subscription ID (a GUID) of the workspace (used by the cost guard) | `target="sentinel"` |
| `SENTINEL_RESOURCE_GROUP` | Resource group name of the workspace (used by the cost guard) | `target="sentinel"` |
| `SENTINEL_WORKSPACE_NAME` | Log Analytics workspace name (used by the cost guard) | `target="sentinel"` |
| `ADX_CLUSTER_URI` | ADX cluster URI (`https://<host>`) | `target="adx"` |
| `ADX_DATABASE` | ADX database | `target="adx"` |
| `AZURE_TENANT_ID` | Tenant for the Azure CLI credential (GUID or domain name) | No (defaults to the CLI's current tenant) |

[notebook-setup.md](notebook-setup.md) shows Azure CLI commands that print every value.

An empty variable counts as unset. Values are validated on every call; an invalid one raises
`ConfigurationError` naming the variable:

- Workspace ID: a GUID in the 8-4-4-4-12 hex form.
- Subscription ID: a GUID in the 8-4-4-4-12 hex form.
- Resource group: 1-90 ASCII letters, digits and `_ - . ( )`, not ending in `.`.
- Workspace name: 4-63 ASCII letters, digits and `-`.
- The package assembles these three into the workspace's ARM resource ID. They must name the
  same workspace as `SENTINEL_WORKSPACE_ID` (checked against the workspace's `customerId`).
- Tenant ID: a GUID, or a DNS-style domain (dot-separated labels of letters, digits and `-`).
- Cluster URI: scheme exactly `https`, a DNS host name, no user info, no port other than 443,
  no query, fragment or path (a trailing `/` is dropped). The host must end in a Kusto
  cluster domain, taken from `azure-kusto-data`'s trusted endpoints: `.kusto.windows.net`,
  `.kustomfa.windows.net`, `.kusto.azuresynapse.net`, `.kusto.fabric.microsoft.com`,
  `.kusto.data.microsoft.com`, and the US government, China, and other sovereign-cloud Kusto
  domains. The Sentinel data lake (`*.securityplatform.microsoft.com`) and the Log Analytics /
  Application Insights / Azure Monitor ADX proxy (`ade.loganalytics.io`,
  `ade.applicationinsights.io`, `adx.*`) are therefore rejected: they bill per GB scanned.
- Database: not blank and no control characters; otherwise any name.

Clients are created on first use and cached per
workspace/cluster and tenant.

### Cost guard (Sentinel only)

Basic, Auxiliary and Sentinel data lake tables bill per GB scanned, and the Log Analytics query
API cannot refuse them, so a Sentinel query may only read tables on the **Analytics** plan
(Defender XDR tables streamed into the workspace count when they are on that plan). The check
runs client-side before the query is sent, in `exec_query` and in the Sentinel client:

1. The workspace, its tables (with their plan) and its saved functions are read from Azure
   Resource Manager for the workspace named by `SENTINEL_SUBSCRIPTION_ID`,
   `SENTINEL_RESOURCE_GROUP` and `SENTINEL_WORKSPACE_NAME`, using the Azure CLI credential
   (needs `Microsoft.OperationalInsights/workspaces/tables/read` and `.../savedSearches/read`,
   for example Log Analytics Reader). The workspace's `customerId` must equal
   `SENTINEL_WORKSPACE_ID`. The result is cached for 5 minutes per workspace and tenant.
2. If that fails for any reason the query is not sent (`QueryError`: the tiers could not be
   verified). There is no fallback to "allow".
3. The KQL is tokenized (comments and string literals are ignored) and rejected if it contains:
   - `search`, `find`, `table()`, `workspace()`, `app()`, `resource()`, `adx()`, `arg()`,
     `cluster()`, `database()`, `external_table()`, `externaldata`, a call through an escaped
     name (`['table']('x')`), or the `evaluate` plugins `sql_request`, `cosmosdb_sql_request`,
     `http_request`, `http_request_post`, `mysql_request`, `postgresql_request`,
     `azure_digital_twins_query_request`, `infer_storage_schema`, `ai_embeddings`,
     `ai_chat_completion`, `ai_chat_completion_prompt`, `python`, `r`, or any plugin not known
     to read only its input;
   - `union` with a wildcard operand (`union *`, `union Sec*`);
   - any identifier, anywhere, equal to a table that is not on the Analytics plan (a missing
     plan counts as not Analytics) or to a saved function alias;
   - **an operator after a pipe that is not on the allowlist** of tabular operators:
     `where`/`filter`, `project`, `project-away`, `project-keep`, `project-rename`,
     `project-reorder`, `extend`, `summarize`, `count`, `take`/`limit`, `top`, `top-nested`,
     `top-hitters`, `sort`/`order`, `distinct`, `join`, `lookup`, `union`, `mv-expand`,
     `mv-apply`, `parse`, `parse-where`, `parse-kv`, `evaluate`, `render`, `sample`,
     `sample-distinct`, `serialize`, `as`, `getschema`, `invoke`, `make-series`, `make-graph`,
     `graph-match`, `graph-to-table`, `graph-shortest-paths`, `graph-mark-components`,
     `partition`, `fork`, `facet`, `scan`, `reduce`, `consume`. Hyphenated names are one
     operator. Others (`graph-merge`, `macro-expand`, ...) are rejected. The tabular operands of
     `join`, `lookup`, `union`, `invoke`, `evaluate`, `make-graph` (every `with` table), `fork`,
     `facet`, `partition` and `mv-apply` are checked as sources;
   - **a call `name(...)` where `name` is not a built-in KQL scalar, aggregate or window
     function and not a function defined with `let f = (...) { ... }` that is in scope.** This
     rejects saved functions and the built-in Sentinel parsers (`_Im_*`, `_ASim_*`, which are
     not saved searches) and `_GetWatchlist()`. The target of `invoke` must be a `let`
     function; a lone name passed to one must be `let`-bound or an Analytics table;
   - a tabular source that is not a `let`-bound name or an Analytics table. Sources are the
     start of every statement (other than `let`, `set`, `declare`, `alias`, `pattern`,
     `restrict`), the value of `let x =`, `union`/`join`/`lookup` operands, the first name
     inside `(` or `{` when followed by `|`, `)` or `}` (subqueries, `materialize()`,
     `toscalar()`, `view()`, `x in (T)`), sub-queries of `partition`, `fork`, `facet` and
     `mv-apply`, the `with` tables of `make-graph`, and a lone name at the start of an argument
     to a `let` function (`f(t=Name)`). A `let` name is visible after its statement, parameters
     only inside their body, `as` names only within their statement. Keywords and function
     names are case-sensitive (`Print` is a name). Table names must match the workspace's
     spelling exactly (`signinlogs` is rejected with a hint to use `SigninLogs`);
   - text it cannot analyse with certainty (unterminated strings, control characters or line
     separators anywhere, non-ASCII characters outside strings and comments, a name glued to a
     number such as `10AuxT`, unbalanced brackets, no query body).

   `print`, `datatable`, `range`, `materialize`, `toscalar` and ordinary `evaluate` plugins
   such as `bag_unpack` are allowed.

**Azure Resource Graph.** `arg("").<table>` is allowed as a table source everywhere a source is
checked (statement start, `let` values, `union`/`join`/`lookup` operands, sub-queries, `let`
function arguments, `make-graph` tables ...), because Azure Resource Graph is free and
cross-service queries are read-only. Only an empty plain string (`""` or `''`) is accepted,
followed by `.` and one of: Resources, ResourceContainers, SecurityResources, AdvisorResources,
AlertsManagementResources, AuthorizationResources, ComputeResources, HealthResources,
PolicyResources, ResourceChanges, ResourceContainerChanges, ServiceHealthResources,
TagResources, AppServiceResources, NetworkResources, KubernetesConfigurationResources,
ExtendedLocationResources, GuestConfigurationResources, MaintenanceResources,
PatchAssessmentResources, PatchInstallationResources, DesktopVirtualizationResources,
IoTSecurityResources, InsightsResources, RecoveryServicesResources (matched case-insensitively).
Any other `arg(...)` form, or another table after `arg("").`, is rejected with this list.
Whitespace and comments between the tokens are ignored. It is an Azure Monitor Preview feature:
results are capped at 1,000 rows, and a `join` with a Log Analytics table needs
`hint.remote=left|right` (`union` needs no hint). `target="adx"` is unchanged.

Limits: this is a guard, not a guarantee. It is a tokenizer-based text check, not a KQL parser.
It can reject harmless KQL (a column named like a non-Analytics table or a saved function; a
column named `search`, `find`, `join`, `lookup` or `union`; `x in (SomeColumn)`; a scalar
function missing from the built-in list; a tabular operator not on the allowlist; a table name in the wrong case; ASIM parsers and `_GetWatchlist()`), and could miss a
construct it does not know of: a table created or moved to another plan during the 5-minute
cache, a name used only as an argument of an ordinary built-in call, graph-match patterns (not modelled as sources), or a difference between its
tokenizer and the service's lexer in rare literal forms. For a server-enforced guarantee use an
identity with table-level RBAC limited to the Analytics tables and no Sentinel data lake roles,
and set Azure budget alerts on the Basic, Auxiliary and data lake meters. For ADX, whose stored
functions the client cannot see, add a callout policy denying the data lake and proxy hosts.

### Time range (Sentinel only)

Works like the Log Analytics portal's time picker:

| KQL has a time filter | `timespan` given | Result |
| --- | --- | --- |
| Yes | No | No API time range is sent ("set in query") |
| No | No | Last 1 day |
| No | Yes | `timespan` is used |
| Yes | Yes | Both apply (the narrower range wins) and a warning is logged |

A query counts as having a time filter when a `where` or `filter` clause references
`TimeGenerated` or `Timestamp`, a time function (`ago()`, `now()`, `datetime()`,
`todatetime()`, `startofday()` and the other `startof*`/`endof*` functions), or a `let` variable
assigned from one of those. Comments and string literals are ignored.

This is a heuristic. A filter it cannot see, such as one inside a stored function or saved
query, is not detected, so the 1-day default is applied on top of it. Pass `timespan`
explicitly in that case.

### Returns

The first result table as a `pandas.DataFrame`, or an empty DataFrame if the query returned no
tables. Sentinel datetime columns are UTC-aware. `df.attrs["kql_types"]` maps each column name
to its lowercase KQL type (`"long"`, `"dynamic"` ...), which `format_dataframe` uses. If Sentinel
returns only a partial result, a warning is logged and the data received is returned.

On Sentinel, a `UserWarning` (and a WARNING log line) is also issued when the time range starts
before the Analytics retention (`retentionInDays`) of a table the query reads, for example a
120-day range on a table that keeps 90 days. Older rows are in long-term retention / the Sentinel
data lake, which these queries cannot read, so that part of the range returns no rows; the warning
names the table, the days kept and the cutoff date, and says when the data is beyond the total
retention too. It is advice only: nothing is skipped or raised, and it is not issued when the
time range is set in the query (`timespan` is `None`), when the retention is unknown, or on ADX.
It is issued before the result cache, so a cached repeat warns again. `exec_batch_query` warns
once per call for its whole range, not once per window.

### Errors

Known failures are raised as `QueryError` (or a subclass) with a message that explains the cause:

- Network failures (reset by peer, refused, timed out, DNS) raise `QueryConnectionError` and say
  what they usually mean.
- HTTP statuses (400, 401, 403, 404, 408, 413, 429, 5xx) and service error codes (Log Analytics
  `SyntaxError`, `SemanticError`, `PathNotFoundError`, `InsufficientAccessError`,
  `ResponsePayloadTooLarge`, timeouts; Kusto `BadRequest_SyntaxError`, `BadRequest_EntityNotFound`,
  limits exceeded, ...) add a hint. Partial Sentinel results get the same hint.
- A missing Azure CLI or an expired login says to install it or run `az login`.
- A query that ran out of time raises `QueryTimeoutError`: HTTP 504 or 408 (`GatewayTimeout`), or a
  Sentinel request that has not been answered within `timeout` plus 30 seconds in total (the
  request is then abandoned). It is not retried, as the same query would time out again.
- `exec_batch_query` retries `QueryTransientError` (throttling, connection errors, HTTP 502/503).

An error nobody anticipated is logged at ERROR with its traceback to the `security_notebook_tools`
logger, and raised as a `QueryError` that names the operation and chains the original exception
(its traceback shows in the notebook). It is not retried. The log record is appended to
`notebooks.log` (see [Logging](#logging)). Configuration and cost guard errors (`ConfigurationError`), `KeyboardInterrupt`
and `SystemExit` are never wrapped.

### Raises

- `ConfigurationError` (also a `ValueError`): `timeout` is not positive, `target` is unknown,
  `timespan` is combined with `target="adx"`, a required environment variable is unset or invalid,
  `SENTINEL_WORKSPACE_ID` and the workspace named by `SENTINEL_SUBSCRIPTION_ID`,
  `SENTINEL_RESOURCE_GROUP` and `SENTINEL_WORKSPACE_NAME` differ, an ADX
  management command is not `.show`, ADX KQL uses `cluster()`, `external_table()`,
  `externaldata` or a blocked plugin, or the Sentinel cost guard rejects the query.
- `QueryError`: the service rejected or failed the query (bad KQL, authorisation, timeout, ...),
  or the Sentinel table plans could not be verified (the query was then not sent). The message
  adds a hint for common HTTP statuses (401, 403, 504, ...). Two subclasses mark failures that
  may succeed if retried: `QueryThrottledError` (HTTP 429) and `QueryConnectionError` (the
  connection failed before a response arrived: reset by peer, refused, timed out, DNS; the
  message explains the likely cause). HTTP 502/503 raise `QueryTransientError`, their base
  class. `QueryTimeoutError` (HTTP 504/408, or no answer within `timeout` plus 30 seconds on
  Sentinel) is a `QueryError` that is not transient.

None of these classes is exported; catch `ValueError` for configuration problems, or `Exception`.

### Examples

```python
from datetime import datetime, timedelta
from security_notebook_tools import exec_query

df = exec_query("SigninLogs | take 100")  # last 1 day by default
df = exec_query("SigninLogs | where TimeGenerated > ago(7d)")  # set in query
df = exec_query("SigninLogs | take 100", timedelta(days=7))  # explicit range
df = exec_query("SigninLogs | take 100", (datetime(2026, 10, 1), timedelta(hours=6)))
df = exec_query("Events | take 100", target="adx", timeout=600)
df = exec_query(".show tables", target="adx")  # management command
```

## exec_batch_query

```python
exec_batch_query(
    kql: str,
    start: datetime | timedelta,
    end: datetime | None = None,
    *,
    interval: timedelta = timedelta(days=1),
    min_interval: timedelta | None = None,
    target: Literal["sentinel", "adx"] = "sentinel",
    timeout: int = 300,
    max_workers: int = 1,
    merge_on: str | Sequence[str] | None = None,
    agg_column: str | Sequence[str] | None = None,
    agg_func: str | Callable = "sum",
    cache: bool = True,
    debug: bool = False,
) -> pandas.DataFrame
```

Run the same KQL once per consecutive time window and merge the results. The period is split
into `[start, start + interval)`, `[start + interval, start + 2 * interval)` ... with the last
window clipped to `end`. By default windows run sequentially, oldest first. With `max_workers`
above 1 the first window still runs alone (a bad query or configuration fails once, and the
client, credential and table-plan caches are warm), then the rest run in parallel on up to
`max_workers` threads; results are always in window order. The first failure stops the batch:
windows not yet started are skipped, running ones are waited for, and the error of the
lowest-numbered failed window is raised.

A window that fails with a transient error is retried up to 4 times: the service throttles it
(HTTP 429, "too many requests"), the connection drops before a response arrives (reset by peer,
timed out, DNS), or the service answers 502/503. The wait is a random time between 0 and 2, 4, 8
and 16 seconds (exponential backoff with full jitter, capped at 30 s), and a warning names the
reason each time; after that the window fails with an error that says it was retried. Other
errors (bad KQL, 401/403, a server-side timeout) are not retried. A connection that was reset or
timed out after the attempt had already run for at least half of `timeout` was cut off for being
too slow, not dropped by the network, so it counts as a timeout and is not retried either: the
error says how long the window ran and suggests a smaller `interval` or a larger `timeout`. A
timed-out window fails with a `QueryTimeoutError` (a `QueryError`).

With `min_interval` a window that times out (HTTP 504/408, no answer within `timeout` plus 30
seconds on Sentinel, or a connection reset after it ran for half of `timeout`) is not failed but
split into two halves that run one after another, oldest first, each as a window of its own with
its own `BatchStart`, `BatchEnd` and API time range. A half that times out is split again, until
a half would be shorter than `min_interval`; then the batch fails with a `QueryTimeoutError` that
names the smallest window tried. Each split is logged at WARNING (window bounds, how long it
ran, the new size), never the KQL. The halves' rows are concatenated in time order, so the result
has the same shape as an unsplit run and goes through `merge_on`/`agg_column` unchanged. Use it
for a few heavy windows (old data is slower to scan) without shrinking `interval` for all of them.
Without `min_interval` a window that times out fails the batch.

Each window's bounds are defined for the query by `let BatchStart = datetime("..."); let
BatchEnd = datetime("...");` added in front of your KQL. The window is half-open, so the KQL must
filter with `TimeGenerated >= BatchStart and TimeGenerated < BatchEnd` (any column or expression
instead of `TimeGenerated`; `BatchStart <= x` and `BatchEnd > x` also work), which counts a
boundary row once. This is checked on both targets before any query is sent: both comparisons
must appear (outside comments and strings), and any other comparison of `BatchStart` or
`BatchEnd` (`>`, `<=`, `==`, `!=`, `between`, or the wrong variable) is rejected, as is defining
either name yourself. Other uses, such as `extend WindowStart = BatchStart`, are fine. On Sentinel
each window is also sent as the API time range, so the 1-day default and the "set in query" rules
of `exec_query` do not apply. The Sentinel cost guard applies to every window.

Aggregations (`summarize`, `count`, `dcount`, `top` ...) run per window and by default are not
combined. Pass `merge_on` and `agg_column` (and optionally `agg_func`) to group the merged rows and
aggregate them, replacing the manual pandas step. This is correct for raw rows with any function.
For per-window `summarize` output use only functions that can be re-combined: `"sum"` of counts or
sums, `"min"` of mins, `"max"` of maxes. A mean of means, a sum of `dcount` or a `nunique` of
per-window values is wrong; `summarize` the sum and the count per window, re-aggregate both with
`"sum"`, then divide. Null keys are kept as their own group, as in KQL. The columns are checked
after the first window, so a typo fails before the other windows run.

### Parameters

| Name | Description |
| --- | --- |
| `kql` | The KQL query. Do not define `BatchStart` or `BatchEnd` yourself. It must filter with `>= BatchStart` and `< BatchEnd` (see above), and with `target="adx"` it may not start with `.` (management commands). |
| `start` | Start of the period, or a `timedelta` meaning that long before `end`. Naive datetimes are taken to be UTC. |
| `end` | End of the period (exclusive); defaults to now in UTC. Naive datetimes are taken to be UTC. |
| `interval` | Window length (default 1 day); must be positive. |
| `min_interval` | `None` (default): a window that times out fails the batch. A `timedelta`: split a window that times out into two halves, recursively, until a half would be shorter than this; then fail, naming the smallest window tried. Must be positive and not longer than `interval`. |
| `target` | `"sentinel"` (default) or `"adx"`. |
| `timeout` | Server-side timeout in seconds for each window (default 300); must be positive. |
| `max_workers` | Windows to run at once (default 1: one after another); an integer from 1 to 5. The cap is the Log Analytics limit of 5 concurrent queries per user; more only queue on the server, and the wait counts against `timeout`. ADX's concurrency limit is shared by everyone using the cluster, so a high value can throttle other users. |
| `merge_on` | Column name or names to group the merged rows by. Give it together with `agg_column`; non-empty, no overlap with `agg_column`. |
| `agg_column` | Column name or names to aggregate. Give it together with `merge_on`. |
| `agg_func` | How to aggregate every `agg_column` (default `"sum"`): a pandas `GroupBy.agg` function name (`"sum"`, `"min"`, `"max"`, `"mean"`, `"count"`, `"first"`, `"last"` ...) or a callable. An error without `agg_column`. |
| `cache` | If true (default), reuse a cached result for the same query and send nothing; if false, always run it and neither read nor write the cache. See [Caching](#caching). |
| `debug` | If true, also print this call's log records, at DEBUG and above, to the notebook cell's output while it runs, including the calls it makes (every window, also those run on worker threads). Default false. The lines are printed unredacted (debug output is not written to the file; be careful when sharing a notebook saved with debug output). See [Debug output](#debug-output-in-the-cell). |

### Returns

With `merge_on` and `agg_column`: one row per distinct key sorted by the keys, with the `merge_on`
columns then the `agg_column` columns and a fresh index (if every window returned no columns, that
empty result is returned as is). `kql_types` keeps the key types; an aggregated column keeps its
type for `sum`, `min`, `max`, `first` and `last`, becomes `long` for `count`, `nunique` and `size`,
`real` for `mean`, `median`, `std` and `var`, and has no entry otherwise.

Otherwise, the non-empty window results concatenated in window order with a fresh index; if every
window is empty, the first window's empty result. `df.attrs["kql_types"]` comes from the windows'
results, adding columns that only later windows have.

### Raises

* `ConfigurationError`: `interval` or `timeout` is not positive, `min_interval` is not a positive
  `timedelta` or is longer than `interval`, `max_workers` is not an integer from 1 to 5, `start` is not before `end`,
  `target` is unknown, the ADX KQL is a management command, the KQL lacks `>= BatchStart` or
  `< BatchEnd`, compares them in another form or defines them (checked before any query),
  `merge_on`/`agg_column` are not given together, empty, not column names, repeated or
  overlapping, `agg_func` is invalid or given without `agg_column`, a named column is missing from
  the result, the aggregation fails, or
  any configuration or cost guard error `exec_query` raises. It is also a `ValueError`.
* `QueryError`: a window failed (including one that still failed transiently after its retries,
  which the message says); the message names the window (number, total, bounds) and the original
  error is chained. A window that timed out (HTTP 504/408, no answer within `timeout` plus 30
  seconds, or a connection reset after the window ran for half of `timeout`) fails with the
  subclass `QueryTimeoutError`, without retries (unless `min_interval` splits it; the final error
  then names the smallest window tried).

### Examples

```python
from datetime import datetime, timedelta
from security_notebook_tools import exec_batch_query

kql = "SigninLogs | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd | take 1000"
df = exec_batch_query(kql, timedelta(days=30))  # last 30 days, one query per day
df = exec_batch_query(kql, datetime(2026, 1, 1), datetime(2026, 2, 1), interval=timedelta(hours=6))
df = exec_batch_query(kql, timedelta(days=30), max_workers=3)  # up to 3 days at once
df = exec_batch_query(  # halve windows that time out, down to 1 hour
    kql, timedelta(days=90), interval=timedelta(days=5), min_interval=timedelta(hours=1)
)
kql = (
    "SigninLogs | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd"
    " | summarize n = count() by UserPrincipalName"
)
df = exec_batch_query(
    kql, timedelta(days=30), merge_on="UserPrincipalName", agg_column="n"
)  # one row per user
```

## format_dataframe

```python
format_dataframe(
    df: pandas.DataFrame,
    *,
    max_rows: int | None = 1000,
    debug: bool = False,
) -> pandas.io.formats.style.Styler
```

Format a query result for display in a notebook. Jupyter renders the returned `Styler` as an
HTML table when it is the last expression in a cell. Values are shown the way Kusto returns
them, by each column's KQL type from `df.attrs["kql_types"]` (set by `exec_query`):

- `long` and `int` as integers, also when nulls made pandas store them as floats.
- `real` as the shortest float that round-trips.
- `bool` as `true` or `false`.
- `datetime` as ISO 8601, with `Z` for UTC.
- `timespan` as `[d.]hh:mm:ss[.fffffff]`.
- `dynamic` as single-line JSON (a list stays a list, an object an object).

Columns with no recorded type (for example ones you added) are typed from their dtype and
values. Nulls are empty cells. Text is left-aligned and numbers (`int`, `long`, `real`,
`decimal`) are right-aligned, headers included. Cells never wrap and columns have no maximum
width, so wide tables scroll horizontally. All text is HTML-escaped and an unnamed index is
hidden. The DataFrame is not modified.

The `Total` row and columns of a `pivot_dataframe(..., totals=True)` result are bold and set off
by a rule, the row also by a shaded background. They are found from `df.attrs["pivot_totals"]`,
not by name, so an ordinary row or column that is called or contains `Total` looks like any
other. The marking survives filtering, sorting and selecting columns; the row loses it once its
index label changes (for example after `reset_index`) or `max_rows` cuts it off.

Numbers (`int`, `long`, `real`, `decimal`) have the integer part split into groups of three
digits with spaces, so `1000000000.00000` is shown as `1 000 000 000.00000`. The fraction is not
grouped, and a `real` shown in exponent form (`1e+20`) is left as it is.

| Name | Description |
| --- | --- |
| `df` | The DataFrame to format, usually the result of `exec_query`. |
| `max_rows` | Show at most this many rows (default 1000), with a caption giving the total. `None` shows every row. Rendering very large tables can make a notebook slow. |
| `debug` | If true, also print this call's log records, at DEBUG and above, to the notebook cell's output while it runs. Default false. The lines are printed unredacted (debug output is not written to the file; be careful when sharing a notebook saved with debug output). See [Debug output](#debug-output-in-the-cell). |

`max_rows` and `debug` are keyword-only. `ValueError` is raised if `max_rows` is not a positive
integer or `None`.

```python
from security_notebook_tools import exec_query, format_dataframe

df = exec_query("SigninLogs | take 100")
format_dataframe(df)  # at most 1000 rows, with a caption
format_dataframe(df, max_rows=None)  # every row
```

## pivot_dataframe

```python
pivot_dataframe(
    df: pandas.DataFrame,
    index: str | Sequence[str],
    columns: str | Sequence[str],
    values: str | Sequence[str] | None = None,
    *,
    agg_func: str | Callable[..., Any] = "sum",
    fill_value: Any = None,
    totals: bool = False,
    max_columns: int | None = 100,
    debug: bool = False,
) -> pandas.DataFrame
```

Pivot a query result: one row per distinct `index` key, one column per distinct `columns` key.
Without `values` the cells count the rows of each combination (missing combinations are 0, typed
`long`). With `values` the cells aggregate those columns with `agg_func`; missing combinations are
null unless `fill_value` is given. Null keys are kept as their own group and rows and columns are
sorted by key.

The result is a flat DataFrame with a new `RangeIndex`: the `index` columns first (as regular
columns), then one column per distinct `columns` key. Headers are strings: the key as text, `(null)`
for a null key, several `columns` joined with `_`, and prefixed `<value>_` when there are several
`values`. A header that equals an `index` column or another header raises an error. The result's
`kql_types` carry the `index` columns' original types and the cells' aggregated type (`count` and
`nunique` are `long`, `mean`, `median`, `std` and `var` are `real`, `sum`, `min`, `max`, `first`
and `last` keep the value's type, and a callable is left untyped), so it goes straight into
`format_dataframe`. With `totals=True`, `df.attrs["pivot_totals"]` records the total columns, the
total row's index label (`None` if there are no rows) and the column holding its `Total` label,
as `{"columns": [...], "row": ..., "label_column": ...}`; `format_dataframe` uses it to make the
totals stand out. `df` is not modified, and a `df` with no rows gives just the `index` columns
(and `Total` if requested).

| Name | Description |
| --- | --- |
| `df` | The DataFrame to pivot, usually the result of `exec_query`. |
| `index` | Column name or names whose distinct values become the rows. |
| `columns` | Column name or names whose distinct values become the new columns. |
| `values` | Column name or names to aggregate. `None` (default) counts rows. |
| `agg_func` | A pandas aggregation name (`"sum"` by default, `"mean"`, `"max"`, `"nunique"`, ...) or a callable. Not allowed without `values`. |
| `fill_value` | Cell value for combinations with no rows. Not allowed without `values`. |
| `totals` | Add a `Total` column and a `Total` row (named in the first `index` column), aggregated with the same function over the underlying rows. With several `values` the total columns are `<value>_Total`. |
| `max_columns` | Raise instead of building more than this many value columns (totals not counted). Default 100; `None` disables. |
| `debug` | If true, also print this call's log records, at DEBUG and above, to the notebook cell's output while it runs. Default false. The lines are printed unredacted (debug output is not written to the file; be careful when sharing a notebook saved with debug output). See [Debug output](#debug-output-in-the-cell). |

`agg_func`, `fill_value`, `totals`, `max_columns` and `debug` are keyword-only. It raises `ValueError`
(`ConfigurationError`) if a name is empty, repeated, used in more than one of `index`, `columns` and
`values` or missing from `df` (the message lists the available columns); if `agg_func` or
`fill_value` is given without `values`; if `agg_func` is not a non-empty string or a callable; if
`max_columns` is not a positive integer or `None`, or the pivot would exceed it; if headers
collide; or if pandas cannot aggregate.

```python
from security_notebook_tools import exec_query, format_dataframe, pivot_dataframe

df = exec_query("SigninLogs | project UserPrincipalName, ResultType, AppDisplayName")
format_dataframe(pivot_dataframe(df, "UserPrincipalName", "ResultType"))  # sign-in counts
format_dataframe(
    pivot_dataframe(df, "UserPrincipalName", "ResultType", "AppDisplayName", agg_func="nunique")
)  # distinct apps per user and result
```

## %%kql

```text
%%kql [-t TIMESPAN] [--target {sentinel,adx}] [--timeout SECONDS] [--no-cache] [--debug]
<KQL>
```

IPython cell magic for quick queries: runs the cell body with `exec_query` and displays the
result with `format_dataframe` (default `max_rows`). The DataFrame is not assigned anywhere; call
`exec_query` directly when you need it. Enable it with
`%load_ext security_notebook_tools` (`load_ipython_extension`, not part of `__all__`). Importing
the package does not import IPython; it is only needed when the extension is loaded.

| Argument | Description |
| --- | --- |
| `-t`, `--timespan` | Duration shorthand (`<number><unit>` parts, units `d`, `h`, `m`, `s`, case-insensitive: `1d`, `12h`, `30m`, `1d12h`, `90s`) giving a `timedelta`, otherwise a Python expression evaluated in the notebook namespace (`ts`, `"(start, end)"`, `"timedelta(hours=3)"`). The value is passed to `exec_query` as `timespan`. |
| `--target` | `sentinel` (default) or `adx`. |
| `--timeout` | Seconds, an integer. Default 300 (`exec_query`'s default). |
| `--no-cache` | Same as `cache=False`: skip the [result cache](#caching) for this cell. |
| `--debug` | Same as `debug=True` for the whole run (`exec_query` and `format_dataframe`): print the run's log records, at DEBUG and above, below the cell. See [Debug output](#debug-output-in-the-cell). |

The cell body is passed verbatim as `kql`: no `{}` or `$` expansion. An empty body, a positional
argument or a timespan expression that fails to evaluate raises `UsageError` before any query runs.
All other validation and errors come from `exec_query` unchanged.

```text
%%kql --timespan 1d12h
SigninLogs | take 100
```

## clear_cache

```python
clear_cache() -> None
```

Forget every cached query result, and also the cached clients, credentials and table plans, so
the next query starts fresh (new sign-in check, new cost guard lookup). See [Caching](#caching).

```python
from security_notebook_tools import clear_cache

clear_cache()
```

## Caching

Query results are cached in memory (`functools.lru_cache`), on by default, for `exec_query` and
for each window of `exec_batch_query`. Up to 32 distinct queries are kept; the least recently
used are dropped first.

- **Key:** the target, the connection settings (Sentinel: workspace ID, resource ID and tenant;
  ADX: cluster, tenant and database), the exact KQL text, the resolved time range and the timeout.
  Changing any of them is a miss.
- **Relative time ranges are not re-evaluated.** A `timedelta` (including the default last 1 day)
  is resolved when the query first runs; a hit returns the data from then, not from now. Pass
  absolute datetimes, or `cache=False`, when you need current data.
- **Not cached:** failures and partial Sentinel results. ADX `.show` commands are cached.
- **Copies:** each call gets its own copy, so changing the returned DataFrame does not affect the
  cache.
- **A hit sends no request**, so nothing is billed and the cost guard check is not needed. An
  INFO log line says the result came from the cache.
- **Batch:** each window is cached separately, so re-running `exec_batch_query` with the same
  absolute `start` and `end` after a failure reuses the windows that already succeeded. With a
  relative `start` (a `timedelta`) the windows move with "now" and do not hit.

Use `cache=False` (or `--no-cache` in `%%kql`) for one fresh call, or `clear_cache()` to drop
everything. Large results stay in memory until evicted or cleared, so use `cache=False` or call
`clear_cache()` for very large pulls.

```python
df = exec_query("SigninLogs | take 100")  # runs the query
df = exec_query("SigninLogs | take 100")  # served from the cache, no request
df = exec_query("SigninLogs | take 100", cache=False)  # runs again
```

## Logging

Every log record at INFO and above is appended to `notebooks.log` in your project root. Nothing
is printed to the notebook cell unless you ask for it with `debug=True` (see
[Debug output](#debug-output-in-the-cell)); errors that cannot be recovered from still reach the cell as
exceptions. The project root is the first directory, walking up from the current working
directory, that contains `.git` or `pyproject.toml`. The walk stops at your home directory (it
never looks above it), and the working directory itself is used if no marker is found by then.
The path follows `os.chdir`, and the file is created, with permissions 0600, when the first record
is written. It is opened without following symbolic links, and records are dropped silently
unless it is a regular file you own with a single link. Add `notebooks.log` to your `.gitignore`.

Each line is an RFC 3339 UTC timestamp with milliseconds, the level, the logger name and the
message; tracebacks follow on the next lines:

```
2026-10-23T14:03:12.345Z INFO security_notebook_tools.sentinel: Running Sentinel query ...
```

If the file cannot be opened or written, the record is dropped silently; logging never breaks a
query. The records come from these loggers:

| Logger | Emits |
| --- | --- |
| `security_notebook_tools` | Parent of all the below |
| `security_notebook_tools.query` | `exec_query` entry and result summary, result cache hit, miss and bypass, `clear_cache` |
| `security_notebook_tools.batch` | `exec_batch_query` entry and summary, each window, retries, fail-fast, pandas merge and aggregation |
| `security_notebook_tools.sentinel` | Client creation, the Log Analytics call and the pandas conversion (with durations), partial results |
| `security_notebook_tools.adx` | Kusto client creation, the Kusto call and the pandas conversion (with durations) |
| `security_notebook_tools.auth` | Credential creation (tenant masked) |
| `security_notebook_tools.config` | Where each setting came from (DEBUG only; never the value) |
| `security_notebook_tools.timerange` | Time range decisions ("set in query", default, both applied) |
| `security_notebook_tools.tablepolicy` | Cost guard: Azure Resource Manager requests (method, host, path), table plan fetch with page count, tables read by an accepted query |
| `security_notebook_tools.format` | `format_dataframe` entry and summary, the pandas Styler |
| `security_notebook_tools.pivot` | `pivot_dataframe` entry and summary, the pandas steps |
| `security_notebook_tools.magic` | `%%kql` runs, argument parsing, extension loading |
| `azure` (and `azure.core`, `azure.identity` ...) | WARNING and above from the Azure SDKs, written by the same handler; their levels are not changed |

Levels:

| Level | Meaning |
| --- | --- |
| DEBUG | Internal detail: cached client, credential or table policy reused, the start of each dependency call. Never written to the file. |
| INFO | An operation starting or completing: a public function's entry and result summary (rows, columns, duration), result cache hit, miss or bypass, client or credential created, each dependency call completing with its duration, cost guard passed, table policy fetched, `clear_cache`, a `%%kql` run, the extension loaded. |
| WARNING | Recoverable or refused: a dependency call that failed (exception type and redacted message, no traceback), each batch retry, partial results, a call rejected by validation or the cost guard (`ConfigurationError`, `%%kql` `UsageError`). |
| ERROR | The operation failed and an exception reaches you. Logged once per failed public call, at the public function. An unanticipated error is logged once, with a traceback. |

Warnings and errors from the Azure SDK loggers (`azure.*`) now go only to `notebooks.log`; the
notebook cell's stderr no longer shows them.

What is never logged: KQL text (only `kql=hmac:<12 hex> len=<n>`, an HMAC under a random key that
exists only for the running process, so fingerprints compare within a session but cannot be
guessed from), result values and DataFrame contents, environment variable values, HTTP headers,
bodies and query strings, tokens, credentials, request properties, the notebook namespace, and a
`timespan` or `%%kql` `--timespan` value (only a duration or dates are logged, otherwise
`<TypeName>`). Workspace, cluster host and database names are masked (`co…(len 12)`), and fields
are written only as plain numbers, dates and short identifier-like strings. SDK error messages
are best effort: shortened to 200 characters, with quoted text (`'…'`, `"…"`, backticks, `b'…'`),
known secret shapes and identifiers removed; unquoted fragments of a message may remain.
The file formatter redacts every line as a backstop: JWTs and bearer tokens, `Authorization`
values, URL query strings, `sig=`, `code=`, `token=`, `access_token=`, `client_secret=`,
`password=`, `key=` and `AccountKey=` values, email addresses, IPv4 and IPv6 addresses, GUIDs
(only the first four hex digits are kept), the subscription, resource group and workspace
segments of Azure resource paths, cookies and API key headers, and user names in home
directories. Line breaks in a message are escaped, so one record is one line. Tracebacks list files and lines but no source text, and their messages get the same
treatment as above.

## `__version__`

The installed version string, e.g. `"4.0.0"`.

### Debug output in the cell

`exec_query`, `exec_batch_query`, `format_dataframe` and `pivot_dataframe` take a keyword-only
`debug=False`, and `%%kql` takes `--debug`. With it, the package's log records at DEBUG and above
are also printed to that notebook cell's output for as long as the call runs, in the same format
as the file but unredacted: KQL text, workspace IDs and cluster URIs are shown in full. Debug
output is not written to the file; be careful when sharing a notebook saved with debug output:

```python
df = exec_query("SigninLogs | take 100", debug=True)
```

```
2026-10-23T14:02:11.402Z INFO security_notebook_tools.query: exec_query: started target=sentinel timeout=300 cache=True kql=hmac:1a2b3c4d5e6f len=24
2026-10-23T14:02:11.403Z DEBUG security_notebook_tools.config: SENTINEL_WORKSPACE_ID: read from the environment
2026-10-23T14:02:11.404Z INFO security_notebook_tools.query: Result cache miss; running the query on Sentinel
...
2026-10-23T14:02:12.247Z INFO security_notebook_tools.query: exec_query: finished in 0.85s tables=1 rows=10 columns=4
```

- **Scope.** The call and everything it calls are covered: nested public calls (`%%kql --debug`
  covers `exec_query` and `format_dataframe`; `exec_batch_query` covers every window, including
  those run on worker threads with `max_workers`). A nested call with `debug=False` does not
  switch it off, and a nested `debug=True` inside a call without it covers just that inner call.
- **Only that call.** Other calls, including ones running at the same time in other threads, print
  nothing, and nothing is printed once the call has returned or raised.
- **Where it goes.** To the output stream the cell had when the call started (`sys.stdout`).
  Only the package's own records are printed, not the `azure` loggers'.
- **Never in the file.** DEBUG records are not written to `notebooks.log`; what the file gets at
  INFO and above is the same with or without `debug`.
- **Cost.** Off by default; the package logger itself is at DEBUG so the records can be created,
  but nothing is formatted or printed unless a call asked for it.
