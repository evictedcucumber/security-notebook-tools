# API reference

The public API is exactly these names, importable from `security_notebook_tools`:

```python
from security_notebook_tools import exec_query, enable_logging, disable_logging
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
| `verbose` | If true, print this call's logs (INFO and above) to the cell output. |

`target`, `timeout` and `verbose` are keyword-only.

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
tables. Sentinel datetime columns are UTC-aware. If Sentinel returns only a partial result, a
warning is logged and the data received is returned.

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
