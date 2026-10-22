# security-notebook-tools

KQL helpers for Jupyter notebooks: run queries against Microsoft Sentinel (Log Analytics) and
Azure Data Explorer (Kusto) and get pandas DataFrames back.

Prerequisites:

- Python 3.14 or newer.
- The [Azure CLI](https://learn.microsoft.com/cli/azure/) signed in with `az login`.
  Authentication uses the Azure CLI credential only.
- Read access to the Sentinel workspace and/or ADX database you want to query. For Sentinel you
  also need `Microsoft.OperationalInsights/workspaces/tables/read` and
  `Microsoft.OperationalInsights/workspaces/savedSearches/read` on the workspace (for example
  the Log Analytics Reader role), which the cost guard uses (see Security).

## Install

The package is not on PyPI. Install it from GitHub or a local checkout.

Into a uv-managed project:

```sh
# Latest main
uv add git+https://github.com/evictedcucumber/security-notebook-tools

# A specific release
uv add git+https://github.com/evictedcucumber/security-notebook-tools@v2.0.0

# A full commit SHA (recommended for sensitive environments, see Security)
uv add git+https://github.com/evictedcucumber/security-notebook-tools@<full-commit-sha>

# Local checkout, editable (changes to the source apply without reinstalling)
uv add --editable ../security-notebook-tools
```

With pip:

```sh
pip install git+https://github.com/evictedcucumber/security-notebook-tools
```

To register a Jupyter kernel from the project's environment, add the `notebook` extra
(it installs `ipykernel`): `uv add "security-notebook-tools[notebook] @ git+https://github.com/evictedcucumber/security-notebook-tools"`.
See [docs/notebook-setup.md](docs/notebook-setup.md) for kernel and VS Code setup.

## Import and quick start

The public API is three names, all importable from the top-level package:

```python
from security_notebook_tools import exec_query, enable_logging, disable_logging
```

Submodules (`security_notebook_tools._query` and so on) are private; do not import from them.

A minimal notebook:

```sh
az login
```

```python
import os

from security_notebook_tools import exec_query

os.environ["SENTINEL_WORKSPACE_ID"] = "<workspace-id>"  # or load from a .env file
os.environ["SENTINEL_SUBSCRIPTION_ID"] = "<subscription-id>"  # for the cost guard
os.environ["SENTINEL_RESOURCE_GROUP"] = "<resource-group>"
os.environ["SENTINEL_WORKSPACE_NAME"] = "<workspace-name>"

df = exec_query("SigninLogs | take 100")  # last 1 day by default
df.head()
```

Instead of setting variables in a cell, put them in a `.env` file (copy
[.env.example](.env.example)) and let VS Code load it into the kernel; see
[docs/notebook-setup.md](docs/notebook-setup.md), which also shows how to look up every value with
the Azure CLI.

## Configuration

| Env var | Used for | Required |
| --- | --- | --- |
| `AZURE_TENANT_ID` | Tenant passed to `AzureCliCredential` | No (defaults to the CLI's current tenant) |
| `SENTINEL_WORKSPACE_ID` | Sentinel / Log Analytics workspace ID (the GUID) | For `target="sentinel"` (the default) |
| `SENTINEL_SUBSCRIPTION_ID` | Azure subscription ID (GUID) of the workspace; the cost guard builds the workspace's ARM resource ID from this and the next two | For `target="sentinel"` (the default) |
| `SENTINEL_RESOURCE_GROUP` | Resource group name of the workspace | For `target="sentinel"` (the default) |
| `SENTINEL_WORKSPACE_NAME` | Log Analytics workspace name (not the ID); must be the same workspace as `SENTINEL_WORKSPACE_ID` | For `target="sentinel"` (the default) |
| `ADX_CLUSTER_URI` | ADX cluster URI | For `target="adx"` |
| `ADX_DATABASE` | ADX database | For `target="adx"` |

Env vars are read on every call, so changing `os.environ` in a cell takes effect on the next
query. Clients are created on first use and reused. Every value can be looked up with the Azure
CLI; see "Find the values with the Azure CLI" in [docs/notebook-setup.md](docs/notebook-setup.md).

## Usage

```python
from datetime import datetime, timedelta
from security_notebook_tools import exec_query

# Sentinel is the default target.
df = exec_query("SigninLogs | take 100")  # no time filter -> last 1 day
df = exec_query("SigninLogs | where TimeGenerated > ago(7d)")  # time filter in the KQL
df = exec_query("SigninLogs | take 100", timedelta(days=7))  # explicit range
df = exec_query("SigninLogs | take 100", (datetime(2026, 10, 1), datetime(2026, 10, 2)))

# Azure Data Explorer (timespan isn't supported; filter on time in the KQL).
df = exec_query("Events | take 100", target="adx")
df = exec_query(".show tables", target="adx")  # only .show commands are allowed
```

`exec_query(kql, timespan=None, *, target="sentinel", timeout=300, verbose=False)` returns the
first result table as a DataFrame (empty if the query returned no tables). Sentinel datetime
columns are UTC-aware. A partial Sentinel result emits a warning and returns the data received.
Full reference: [docs/api.md](docs/api.md), or `help(exec_query)` in a notebook.

### Time range (Sentinel)

The time range works like the portal's time picker.

| KQL has a time filter | `timespan` given | Result |
| --- | --- | --- |
| Yes | No | No API time range is sent ("set in query") |
| No | No | Last 1 day |
| No | Yes | `timespan` is used |
| Yes | Yes | Both apply (the narrower range wins) and a warning is logged |

`timespan` is a `timedelta`, a `(start, end)` tuple of datetimes, or a `(start, timedelta)`
tuple. Passing it with `target="adx"` raises an error.

Detection is a text heuristic: a time filter it can't see (for example inside a stored function
or saved query) isn't detected, so the 1-day default is applied on top. Pass `timespan`
explicitly in that case. The exact rules are in [docs/api.md](docs/api.md).

### Timeout

`timeout` is the server-side timeout in seconds for both targets. Default 300; must be positive.

```python
df = exec_query("SigninLogs | summarize count() by UserPrincipalName", timeout=600)
```

## Logging

Everything logs through the standard `logging` module under the `security_notebook_tools`
logger (children: `sentinel`, `adx`, `auth`, `timerange`, `tablepolicy`). Nothing is printed by default.

- `exec_query(..., verbose=True)` prints that call's logs (INFO and above) in the cell output.
- `enable_logging()` (optionally `enable_logging("DEBUG")`) does so for the whole session;
  `disable_logging()` undoes it.
- Or configure `logging.getLogger("security_notebook_tools")` yourself.

```python
df = exec_query("SigninLogs | take 10", timedelta(days=1), verbose=True)
# 14:02:11 INFO security_notebook_tools.sentinel: Running Sentinel query (timespan=1 day, 0:00:00): SigninLogs | take 10
# 14:02:12 INFO security_notebook_tools.sentinel: Sentinel query returned 1 table(s), rows=[10] in 0.84s
```

## Security

- **Authentication** is only via the Azure CLI (`az login`). The package stores no secrets;
  tokens are cached by the Azure CLI and `azure-identity` in memory.
- **ADX is read-only.** Only `.show` management commands are accepted (anything else raises
  `ConfigurationError` before any request), and every ADX request is sent with the
  server-enforced `request_readonly` option, which callers cannot turn off.
- **Sentinel** queries use the Log Analytics query API, which is read-only, and pass the cost
  guard described below.
- **ADX cannot reach the Sentinel data lake or Log Analytics workspaces.** The cluster URI host
  must be a Kusto cluster domain (`*.kusto.windows.net`, `*.kusto.fabric.microsoft.com`,
  `*.kusto.azuresynapse.net`, the US government, China and other sovereign-cloud Kusto domains;
  the list is taken from `azure-kusto-data`'s trusted endpoints). The Sentinel data lake
  (`*.securityplatform.microsoft.com`) and the Log Analytics / Application Insights / Azure
  Monitor ADX proxy (`ade.loganalytics.io`, `ade.applicationinsights.io`, `adx.*`) are rejected.
  ADX queries and `.show` commands using `cluster()`, `external_table()`, `externaldata`, calls
  through escaped names (`['cluster'](...)`) or data-reading `evaluate` plugins (`sql_request`,
  `http_request`, `python`, `r`, `ai_*` ...) are rejected before any request is made, as is ADX
  text that cannot be tokenized with certainty (an unterminated string, control characters, or
  non-ASCII characters outside strings and comments). The client cannot see functions stored in
  the ADX database, so a stored function that calls `cluster()` is not caught: for that, add an
  ADX callout policy on the cluster that denies the data lake and proxy hosts.
- **Configuration is validated** on every call: workspace ID must be a GUID, workspace resource
  ID a strict `/subscriptions/<GUID>/resourceGroups/<group>/providers/Microsoft.OperationalInsights/workspaces/<name>`
  ID, tenant ID a GUID or domain name, cluster URI a plain `https://<host>` URI (no user info, port other than 443,
  path, query or fragment), and the database name non-blank without control characters.
- **Logs are off by default.** When enabled, INFO logs include the first 200 characters of each
  KQL query, which may contain sensitive indicators. Treat log output accordingly.
- **Supply chain.** In sensitive environments install pinned to a full commit SHA rather than a
  tag (tags can be moved), and install with `uv sync --locked`; `uv.lock` records hashes.

### Cost guard

Basic, Auxiliary and Sentinel data lake tables bill per GB scanned, and the Log Analytics query
API cannot refuse them. So `exec_query` only reads tables on the **Analytics** plan (Defender
XDR tables streamed into the workspace count when they are on the Analytics plan), and checks
this client-side before every Sentinel query is sent.

How it works:

- It reads the workspace, its tables (with their plan) and its saved functions from Azure
  Resource Manager (the workspace named by `SENTINEL_SUBSCRIPTION_ID`, `SENTINEL_RESOURCE_GROUP`
  and `SENTINEL_WORKSPACE_NAME`), using the Azure CLI credential. Needs
  `Microsoft.OperationalInsights/workspaces/tables/read` and `.../savedSearches/read` (for
  example Log Analytics Reader on the workspace). The workspace's `customerId` must equal
  `SENTINEL_WORKSPACE_ID`, otherwise the query is refused.
- The result is cached for 5 minutes (plans can change), per workspace and tenant.
- **It fails closed.** If the plans cannot be read (no network, no access, unexpected response)
  the query is not sent and a `QueryError` says the tiers could not be verified.
- Any plan other than exactly `Analytics`, or a missing plan, counts as forbidden.

Allowed: Analytics tables (exact case); `let`-bound names; `join`, `lookup` and `union` of Analytics tables
(named explicitly); subqueries over them; `print`, `datatable`, `range`; the built-in KQL
scalar, aggregate and window functions (`ago`, `tostring`, `count`, `dcount`, `arg_max`,
`row_number`, the `array_*`, `bag_*`, `set_*`, `series_*`, `geo_*`, `ipv4_*`, `datetime_*`,
`parse_*`, `format_*`, `url_*` families, listed by exact name ...); functions you define with `let f = (...) { ... }`;
and ordinary `evaluate` plugins such as `bag_unpack`, `pivot` and `narrow`. Names that appear
only inside string literals or comments are ignored.

Rejected:

- Any table not on the Analytics plan, wherever its name appears in the query.
- **Any call to a function that is not a built-in KQL function or defined with `let` in the
  same query.** That includes saved functions and parsers, the built-in Sentinel parsers
  (`_Im_*`, `_ASim_*`, which are not listed as saved searches) and `_GetWatchlist()`. A scalar
  function missing from the built-in list is rejected too (fail closed). The target of `invoke`
  must be a `let` function, and a lone name passed to one must be `let`-bound or an Analytics
  table.
- **Any operator after a pipe that is not on the list of tabular operators**, which is:
  `where`/`filter`, `project`, `project-away`, `project-keep`, `project-rename`,
  `project-reorder`, `extend`, `summarize`, `count`, `take`/`limit`, `top`, `top-nested`,
  `top-hitters`, `sort`/`order`, `distinct`, `join`, `lookup`, `union`, `mv-expand`, `mv-apply`,
  `parse`, `parse-where`, `parse-kv`, `evaluate`, `render`, `sample`, `sample-distinct`,
  `serialize`, `as`, `getschema`, `invoke`, `make-series`, `make-graph`, `graph-match`,
  `graph-to-table`, `graph-shortest-paths`, `graph-mark-components`, `partition`, `fork`,
  `facet`, `scan`, `reduce`, `consume`. Multi-word operators such as `project-away` count as
  one. Everything else is rejected, for example `graph-merge`, `macro-expand`, `search` and
  `find`. Operators with a tabular operand have it checked as a source: `join`, `lookup`,
  `union`, `invoke`, `evaluate`, `make-graph` (every `with` table), `fork`, `facet`,
  `partition`, `mv-apply`.
- Unknown names. Every tabular source must be a `let`-bound name or an Analytics table: the
  start of each statement, the value of `let`, `union`/`join`/`lookup` operands, subqueries,
  `materialize()`/`toscalar()`/`view()` arguments, the sub-queries of `partition`, `fork`,
  `facet` and `mv-apply`, the `with` tables of `make-graph`, and a lone name at the start of an
  argument to a `let` function (`f(t=Name)`). A `let`, parameter or `as` name is visible only
  where KQL allows it.
- **Case.** Keywords and function names are case-sensitive, like the service (`Print` and
  `Union` are plain names). Table names must match the case of the workspace's table exactly:
  `signinlogs` is rejected with a hint to use `SigninLogs`, because a different spelling might
  not be that table. Rejection of non-Analytics tables and saved functions stays
  case-insensitive.
- `search`, `find`, `union *` or any `union` wildcard, `table()`, `workspace()`, `app()`,
  `resource()`, `adx()`, `arg()` in any form other than the one allowed below, `cluster()`, `database()`, `external_table()`, `externaldata`,
  calls through escaped names (`['table']('x')`), and the `evaluate` plugins `sql_request`,
  `cosmosdb_sql_request`, `http_request`, `http_request_post`, `mysql_request`,
  `postgresql_request`, `azure_digital_twins_query_request`, `infer_storage_schema`,
  `ai_embeddings`, `ai_chat_completion`, `ai_chat_completion_prompt`, `python`, `r`, and any
  plugin not known to read only its input.
- KQL it cannot analyse with certainty: unterminated strings, control characters or line
  separators anywhere, non-ASCII characters outside strings and comments, a name glued to a
  number (`10AuxT`), unbalanced brackets, an empty query or one with only definitions.

**Azure Resource Graph.** `arg("").<table>` (for example `arg("").Resources`) is allowed as a
table source anywhere a source is allowed, because Azure Resource Graph is free and
cross-service queries are read-only. Only an empty plain string (`""` or `''`) and these tables
(matched case-insensitively) are accepted: Resources, ResourceContainers, SecurityResources,
AdvisorResources, AlertsManagementResources, AuthorizationResources, ComputeResources,
HealthResources, PolicyResources, ResourceChanges, ResourceContainerChanges,
ServiceHealthResources, TagResources, AppServiceResources, NetworkResources,
KubernetesConfigurationResources, ExtendedLocationResources, GuestConfigurationResources,
MaintenanceResources, PatchAssessmentResources, PatchInstallationResources,
DesktopVirtualizationResources, IoTSecurityResources, InsightsResources,
RecoveryServicesResources. Anything else (`arg("sub")`, `arg()`, `arg(@"")`, a bare `arg("")`
without `.<table>`, `['arg']("")`, an unknown table) is rejected with the list of tables.
Whitespace and comments between the tokens are ignored. This is a Preview feature of Azure
Monitor: results are capped at 1,000 rows, and a `join` with a Log Analytics table needs
`hint.remote=left` or `hint.remote=right` (a `union` needs no hint). If a workspace table has
the same name as an ARG table and is not on the Analytics plan, the query is rejected.

Limits: the check is a conservative text analysis (a tokenizer, not a full KQL parser), so it
is a guard and not a guarantee.

- It can reject harmless KQL: a column or variable named like a non-Analytics table or a saved
  function, a column named `search`, `find`, `join`, `lookup` or `union`, `x in (SomeColumn)`,
  a column passed to a `let` function, a scalar function that is not in the built-in list, a
  tabular operator that is not on the list, a table name in the wrong case, and ASIM parsers
  and `_GetWatchlist()` by design.
- It can miss things it does not know about: a table that is not in the cached policy because
  it was created or moved to another plan within the 5-minute cache; a name used only as a
  lone argument of an ordinary built-in call (rule (b) still catches every known non-Analytics
  table and saved function by name), or in a position of a listed operator that it models
  imperfectly (graph patterns are skipped, for example);
  differences between its tokenizer and the service's lexer in rare literal forms.
- It cannot see inside functions stored in the workspace other than by alias, which it rejects.

For a server-enforced guarantee, run with an identity that has table-level RBAC limited to the
Analytics tables and no Sentinel data lake roles, and set Azure budget alerts on the Basic,
Auxiliary and Sentinel data lake meters.

## Errors

The exception classes are not part of the public API; read the message. Messages you may see:

- `No workspace_id configured: set the $SENTINEL_WORKSPACE_ID environment variable.` (similarly for
  `cluster_uri` / `$ADX_CLUSTER_URI` and `database` / `$ADX_DATABASE`): set the env var.
- `No ... configured: set the $SENTINEL_RESOURCE_GROUP environment variable.` (similarly
  `$SENTINEL_SUBSCRIPTION_ID` and `$SENTINEL_WORKSPACE_NAME`): the cost guard needs all three to
  locate the workspace.
- `Invalid $SENTINEL_WORKSPACE_ID: ...` (similarly `$SENTINEL_SUBSCRIPTION_ID`,
  `$SENTINEL_RESOURCE_GROUP`, `$SENTINEL_WORKSPACE_NAME`, `$AZURE_TENANT_ID`, `$ADX_CLUSTER_URI`,
  `$ADX_DATABASE`): the value failed validation; see Security. An `$ADX_CLUSTER_URI` that is
  not a Kusto cluster domain (the Sentinel data lake and the Log Analytics proxy included) says
  so.
- `$SENTINEL_WORKSPACE_ID (...) does not match the workspace behind $SENTINEL_SUBSCRIPTION_ID /
  $SENTINEL_RESOURCE_GROUP / $SENTINEL_WORKSPACE_NAME (customerId ...)`: the ID and the three
  name variables point at different workspaces.
- `Could not verify Sentinel table tiers (...), so the query was not sent.`: the cost guard
  could not read table plans from Azure Resource Manager; check `az login` and the
  `tables/read` and `savedSearches/read` permissions. Nothing was sent.
- `Cost guard: table 'X' is on the Basic plan; only Analytics-plan tables can be queried ...`,
  `Cost guard: X() is not a built-in KQL function or a function defined with `let` ...`,
  `Cost guard: 'X' is a saved function or parser ...`, `Cost guard: 'X' is not a let-bound name
  or a known Analytics-plan table ...`, `Cost guard: `search` is not allowed ...`, and similar:
  the query was rejected and not sent; see Cost guard.
- `` `cluster()` is not allowed with target='adx' ... ``: an ADX query or command used a
  construct that could reach the data lake or a workspace.
- `Only '.show' management commands are allowed; ADX access is read-only.`
- `timespan only applies to Sentinel; filter on time in the KQL for ADX.`
- `timeout must be a positive number of seconds, got ...`
- `Unknown target ...; use 'sentinel' or 'adx'.`
- `Sentinel query failed: ...` / `ADX query failed: ...` / `ADX command failed: ...`: the
  service rejected the query (bad KQL, no access, timeout); the service's message follows.
- `Sentinel query returned partial results: ...`: a warning, not an error.

The configuration errors above are also `ValueError`s.

## Development

Python 3.14 (see `.python-version`). With Nix (flakes), `nix develop` provides Python, uv and
ruff and activates `.venv`; with direnv, `direnv allow` (uses `.envrc`). Flakes only see
git-tracked files, so `git add` new files first. Without Nix, use plain uv:

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv build
```

Docstrings use Google style, checked by ruff's `D` rules. Contributor and AI-agent notes are in
[CLAUDE.md](CLAUDE.md); release notes in [CHANGELOG.md](CHANGELOG.md).

## Documentation

- [docs/api.md](docs/api.md): API reference.
- [docs/notebook-setup.md](docs/notebook-setup.md): Jupyter and VS Code setup.

## License

MIT. See [LICENSE](LICENSE).
