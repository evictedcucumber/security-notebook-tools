# API reference

The public API is exactly these names, importable from `security_notebook_tools`:

```python
from security_notebook_tools import (
    exec_query,
    format_dataframe,
    pivot_dataframe,
    enable_logging,
    disable_logging,
    clear_cache,
)
import security_notebook_tools

security_notebook_tools.__version__  # "2.0.0"
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
    verbose: bool = False,
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
| `verbose` | If true, print this call's logs (INFO and above) to the cell output. |

`target`, `timeout`, `cache` and `verbose` are keyword-only.

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

### Raises

- `ConfigurationError` (also a `ValueError`): `timeout` is not positive, `target` is unknown,
  `timespan` is combined with `target="adx"`, a required environment variable is unset or invalid,
  `SENTINEL_WORKSPACE_ID` and the workspace named by `SENTINEL_SUBSCRIPTION_ID`,
  `SENTINEL_RESOURCE_GROUP` and `SENTINEL_WORKSPACE_NAME` differ, an ADX
  management command is not `.show`, ADX KQL uses `cluster()`, `external_table()`,
  `externaldata` or a blocked plugin, or the Sentinel cost guard rejects the query.
- `QueryError`: the service rejected or failed the query (bad KQL, authorisation, timeout, ...),
  or the Sentinel table plans could not be verified (the query was then not sent).

Neither class is exported; catch `ValueError` for configuration problems, or `Exception`.

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
    target: Literal["sentinel", "adx"] = "sentinel",
    timeout: int = 300,
    max_workers: int = 1,
    merge_on: str | Sequence[str] | None = None,
    agg_column: str | Sequence[str] | None = None,
    agg_func: str | Callable = "sum",
    cache: bool = True,
    verbose: bool = False,
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

A window the service throttles (HTTP 429, "too many requests") is retried up to 4 times, waiting
a random time between 0 and 2, 4, 8 and 16 seconds (exponential backoff with full jitter, capped
at 30 s) and logging a warning each time; after that it fails like any other error. Other errors
are not retried.

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
| `target` | `"sentinel"` (default) or `"adx"`. |
| `timeout` | Server-side timeout in seconds for each window (default 300); must be positive. |
| `max_workers` | Windows to run at once (default 1: one after another); an integer from 1 to 5. The cap is the Log Analytics limit of 5 concurrent queries per user; more only queue on the server, and the wait counts against `timeout`. ADX's concurrency limit is shared by everyone using the cluster, so a high value can throttle other users. |
| `merge_on` | Column name or names to group the merged rows by. Give it together with `agg_column`; non-empty, no overlap with `agg_column`. |
| `agg_column` | Column name or names to aggregate. Give it together with `merge_on`. |
| `agg_func` | How to aggregate every `agg_column` (default `"sum"`): a pandas `GroupBy.agg` function name (`"sum"`, `"min"`, `"max"`, `"mean"`, `"count"`, `"first"`, `"last"` ...) or a callable. An error without `agg_column`. |
| `cache` | If true (default), reuse a cached result for the same query and send nothing; if false, always run it and neither read nor write the cache. See [Caching](#caching). |
| `verbose` | If true, print this call's logs (INFO and above) to the cell output. |

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

* `ConfigurationError`: `interval` or `timeout` is not positive, `max_workers` is not an integer
  from 1 to 5, `start` is not before `end`,
  `target` is unknown, the ADX KQL is a management command, the KQL lacks `>= BatchStart` or
  `< BatchEnd`, compares them in another form or defines them (checked before any query),
  `merge_on`/`agg_column` are not given together, empty, not column names, repeated or
  overlapping, `agg_func` is invalid or given without `agg_column`, a named column is missing from
  the result, the aggregation fails, or
  any configuration or cost guard error `exec_query` raises. It is also a `ValueError`.
* `QueryError`: a window failed (including one still throttled after its retries); the message
  names the window (number, total, bounds) and the original error is chained.

### Examples

```python
from datetime import datetime, timedelta
from security_notebook_tools import exec_batch_query

kql = "SigninLogs | where TimeGenerated >= BatchStart and TimeGenerated < BatchEnd | take 1000"
df = exec_batch_query(kql, timedelta(days=30))  # last 30 days, one query per day
df = exec_batch_query(kql, datetime(2026, 1, 1), datetime(2026, 2, 1), interval=timedelta(hours=6))
df = exec_batch_query(kql, timedelta(days=30), max_workers=3)  # up to 3 days at once
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

| Name | Description |
| --- | --- |
| `df` | The DataFrame to format, usually the result of `exec_query`. |
| `max_rows` | Show at most this many rows (default 1000), with a caption giving the total. `None` shows every row. Rendering very large tables can make a notebook slow. |

`max_rows` is keyword-only. It raises `ValueError` if it is not a positive integer or `None`.

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

`agg_func`, `fill_value`, `totals` and `max_columns` are keyword-only. It raises `ValueError`
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
%%kql [-t TIMESPAN] [--target {sentinel,adx}] [--timeout SECONDS] [--no-cache] [-v]
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
| `-v`, `--verbose` | Same as `verbose=True`. |

The cell body is passed verbatim as `kql`: no `{}` or `$` expansion. An empty body, a positional
argument or a timespan expression that fails to evaluate raises `UsageError` before any query runs.
All other validation and errors come from `exec_query` unchanged.

```text
%%kql --timespan 1d12h
SigninLogs | take 100
```

## enable_logging

```python
enable_logging(level: int | str = logging.INFO) -> None
```

Print package logs to the current cell's output for the rest of the session, formatted as
`HH:MM:SS LEVEL logger: message`. Calling it again only changes the level.

| Name | Description |
| --- | --- |
| `level` | Minimum level, as a `logging` constant or a name such as `"DEBUG"`. Default `logging.INFO`. |

```python
from security_notebook_tools import enable_logging

enable_logging("DEBUG")
```

## disable_logging

```python
disable_logging() -> None
```

Stop printing package logs to the notebook (undo `enable_logging`). Resets the package
logger's level to `NOTSET`. Handlers you attached yourself are left alone.

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

All logs go through the standard `logging` module. Nothing is printed by default (a
`NullHandler` is attached to the package logger).

| Logger | Emits |
| --- | --- |
| `security_notebook_tools` | Parent of all the below |
| `security_notebook_tools.sentinel` | Sentinel queries, row counts, timing, partial results |
| `security_notebook_tools.adx` | ADX queries and commands |
| `security_notebook_tools.auth` | Credential creation |
| `security_notebook_tools.timerange` | Time range decisions ("set in query", default, both applied) |
| `security_notebook_tools.tablepolicy` | Cost guard: table plan lookups, cache use, rejected queries |

Ways to see the logs:

- `exec_query(..., verbose=True)`: that call only, INFO and above.
- `enable_logging()` / `disable_logging()`: the whole session.
- Your own configuration, e.g. `logging.getLogger("security_notebook_tools").setLevel("DEBUG")`
  with your own handler.

If logs are already being printed (via `enable_logging`), `verbose=True` does not duplicate them.

## `__version__`

The installed version string, e.g. `"2.0.0"`.
