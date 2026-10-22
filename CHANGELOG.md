# Changelog

## Unreleased

### Breaking

- `SENTINEL_WORKSPACE_RESOURCE_ID` is removed, with no fallback. Set three plain values instead,
  all required for `target="sentinel"`: `SENTINEL_SUBSCRIPTION_ID` (GUID),
  `SENTINEL_RESOURCE_GROUP` and `SENTINEL_WORKSPACE_NAME`. The package assembles the ARM
  resource ID itself. To migrate, split your old value
  `/subscriptions/<subscription-id>/resourceGroups/<group>/providers/Microsoft.OperationalInsights/workspaces/<name>`
  into those three parts. They must still name the same workspace as `SENTINEL_WORKSPACE_ID`.
  Errors now name the specific variable that is missing or invalid.

### Added

- `exec_batch_query(kql, start, end=None, *, interval=timedelta(days=1), target, timeout,
  verbose)` runs the same KQL once per consecutive time window and merges the results into one
  DataFrame, for queries too big for one run or needing a longer period. Each window defines
  `BatchStart` and `BatchEnd` in the query and, on Sentinel, is also sent as the API time range.
  ADX queries must use both variables. Aggregations run per window; re-aggregate in pandas.
- `%%kql` IPython cell magic (`%load_ext security_notebook_tools`) for quick queries: write raw
  KQL in a cell and see the result rendered with `format_dataframe`. The DataFrame is not kept;
  use `exec_query` when you need it. Supports every `exec_query` option (`--timespan`,
  `--target`, `--timeout`, `--verbose`); `--timespan` accepts `1d`/`12h`/`30m`/`90s`/`1d12h` or
  a Python expression. IPython is only needed when the extension is loaded.
- `format_dataframe(df, *, max_rows=1000)` returns a pandas `Styler` that shows a query result
  the way Kusto returns it: values by KQL type, nulls blank, text left-aligned and numbers
  right-aligned. It shows at most 1000 rows with a caption; `max_rows=None` shows all of them.
- `exec_query` records each column's lowercase KQL type in `df.attrs["kql_types"]`, which
  `format_dataframe` uses.
- New runtime dependency `jinja2>=3.1`, which pandas' `Styler` needs.

### Docs

- `docs/notebook-setup.md` has a "Find the values with the Azure CLI" section with a command per
  environment variable and a snippet that prints ready-to-paste `.env` lines.

## 2.0.0 - 2026-10-22

### Breaking

- `SENTINEL_WORKSPACE_RESOURCE_ID` is now required for `target="sentinel"`: the workspace's ARM
  resource ID (`/subscriptions/<GUID>/resourceGroups/<group>/providers/
  Microsoft.OperationalInsights/workspaces/<name>`, validated strictly and case-insensitively).
  Add it to your `.env` or notebook configuration; `.env.example` shows the form. It must name
  the same workspace as `SENTINEL_WORKSPACE_ID` (checked against the workspace's `customerId`).
- Sentinel queries are restricted to tables on the Log Analytics Analytics plan (see Security),
  so queries that read Basic, Auxiliary or data lake tables, call any function that is neither
  a built-in KQL function nor defined with `let` (saved functions, ASIM parsers, `_Im_*`,
  `_ASim_*`, `_GetWatchlist()`), or use `search`, `find`, `union` with wildcards or
  cross-workspace functions are now rejected. The identity needs `tables/read` and
  `savedSearches/read` on the workspace (for example Log Analytics Reader).
- `ADX_CLUSTER_URI` must now be on a Kusto cluster domain (`*.kusto.windows.net`,
  `*.kusto.fabric.microsoft.com`, `*.kusto.azuresynapse.net` and the sovereign-cloud Kusto
  domains from `azure-kusto-data`'s trusted endpoints). The Sentinel data lake and the Log
  Analytics / Application Insights / Azure Monitor proxy hosts are rejected. ADX queries and
  commands using `cluster()`, `external_table()`, `externaldata`, escaped-name calls or
  data-reading `evaluate` plugins (including `python`, `r`, `ai_*`) are rejected.

### Security

- Cost guard for Sentinel: `exec_query` can only read Analytics-plan tables, so queries cause no
  per-GB-scanned charges from Basic, Auxiliary or Sentinel data lake data. Table plans and saved
  functions are read from Azure Resource Manager (cached for 5 minutes) and the KQL is checked
  client-side before it is sent. It fails closed: if the plans cannot be verified, nothing is
  sent. It is a conservative text analysis, so a guard rather than a guarantee; the README
  recommends table-level RBAC and budget alerts for a server-enforced limit.
- Cost guard details: only a list of known tabular operators may follow a pipe (anything else,
  such as `graph-merge`, is rejected), the tabular operands of every operator that has one are
  checked, table names must match the workspace's case exactly, and built-in functions are
  allowed by exact name.
- Azure Resource Graph stays usable: `arg("").<table>` (for example `arg("").Resources`) is
  accepted as a table source for `target="sentinel"` because Azure Resource Graph is free and
  read-only. Only an empty plain string and the documented ARG tables are allowed (matched
  case-insensitively); any other `arg(...)` is rejected. Azure Monitor documents this as a
  Preview feature with a 1,000-row cap; joins with Log Analytics tables need
  `hint.remote=left|right`.
- A shared KQL tokenizer now reads strings and comments for the guard and the time-range
  heuristic: verbatim strings (`@"C:\"`), multi-line strings and escaped names (`['name']`) are
  handled, and text it cannot read with certainty is refused.
- New `security_notebook_tools.tablepolicy` logger.
- ADX is strictly read-only. Only `.show` management commands are allowed (case-insensitive);
  any other command (`.drop`, `.set-or-replace`, `.ingest`, ...) now raises `ConfigurationError`
  before any request is made. This is enforced in `exec_query` and in the ADX client.
- Every ADX request, query or command, is sent with `request_readonly=True`; caller-supplied
  request properties cannot turn it off.
- Connection settings are validated on every call, raising `ConfigurationError` that names the
  variable: workspace ID must be a GUID; tenant ID a GUID or domain name; cluster URI an
  `https://<host>` URI with no user info, non-443 port, path, query or fragment; database name
  non-blank without control characters.
- README gains a Security section; install docs recommend pinning to a full commit SHA.

## 1.0.0

### Removed (breaking)

- The pandas formatting helpers and their `verbose` option: `drop_empty_columns`,
  `parse_json_columns`, `expand_column`, `truncate_strings`, `format_timestamps`,
  `reorder_columns`, `summarize_df`, `display_df`. The `security_notebook_tools.formatting`
  logger no longer exists. The public API is now `exec_query`, `enable_logging` and
  `disable_logging`.

### Changed

- The `notebook` extra now installs only `ipykernel` (for registering a Jupyter kernel).
- Licensed under MIT; added `LICENSE` and project URLs to the package metadata.
- Time range messages now log under `security_notebook_tools.timerange` instead of
  `security_notebook_tools.sentinel`.
- README install instructions point at the `security-notebook-tools` GitHub repo.

### Added

- `CLAUDE.md` guide for AI agents.
- `docs/api.md` (API reference) and `docs/notebook-setup.md` (Jupyter and VS Code setup).
- Workspace config in `.vscode/` and an `.env.example` listing the connection variables.

## 0.1.0

Initial release.

- `exec_query` runs KQL against Microsoft Sentinel (default) or Azure Data Explorer and returns
  the first result table as a pandas DataFrame. ADX management commands (KQL starting with `.`)
  are supported.
- Portal-style time range for Sentinel: a time filter in the KQL means no API time range is
  sent; otherwise `timespan` is used, defaulting to the last 1 day.
- Server-side `timeout` (default 300 seconds) for both targets.
- Azure CLI authentication, configured through `AZURE_TENANT_ID`, `SENTINEL_WORKSPACE_ID`,
  `ADX_CLUSTER_URI` and `ADX_DATABASE`.
- Formatting helpers: `drop_empty_columns`, `parse_json_columns`, `expand_column`,
  `truncate_strings`, `format_timestamps`, `reorder_columns`, `summarize_df`, `display_df`.
- Logging: `enable_logging`, `disable_logging` and `verbose=True` for notebook-friendly output.
