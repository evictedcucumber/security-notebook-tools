# Changelog

## 4.0.0 - 2026-10-23

### Breaking

- `format_dataframe` no longer takes `column_styles`; passing it raises `TypeError`. Its one
  style, `"readable_numbers"`, is now always on: every numeric (`int`, `long`, `real`,
  `decimal`) column, recorded or inferred type alike, has its integer digits grouped with spaces
  (`1 000 000 000.00000`). The fraction is not grouped, and a `real` in exponent form (`1e+20`)
  is left as it is.

### Added

- `debug=False` keyword-only argument on `exec_query`, `exec_batch_query`, `format_dataframe` and
  `pivot_dataframe`, and `--debug` on `%%kql`. With it, the package's log records at DEBUG and
  above are also printed to that notebook cell's output while the call runs, unredacted (debug output is
  not written to the file; be careful when sharing a notebook saved with it), for the call and everything it calls (nested public calls and `exec_batch_query`'s
  worker threads); other calls, including concurrent ones, print nothing. DEBUG records are
  never written to `notebooks.log`. Off by default; the public API is otherwise unchanged.

### Changed

- The `security_notebook_tools` logger is now at DEBUG (the file handler stays at INFO), so a
  handler or `caplog` you attach to it sees DEBUG records too.

- Logging now covers every module with the same levels: DEBUG internal detail (never written),
  INFO an operation starting or completing, WARNING recoverable or refused, ERROR a failed
  operation. New child loggers: `config`, `format`, `pivot`, `magic`.
- Public functions (`exec_query`, `exec_batch_query`, `format_dataframe`, `pivot_dataframe`,
  `%%kql`) log their entry, a result summary (rows, columns, duration), and a failure exactly
  once: WARNING when the call was rejected (`ConfigurationError`, `UsageError`), ERROR when it
  failed. The ERROR lines that `_sentinel`, `_adx` and `_tablepolicy` wrote before every raise
  are gone, replaced by a WARNING for the failed dependency call. An unexpected error is still
  logged once, with its traceback.
- Every dependency call (Azure credential and clients, Log Analytics, Kusto, Azure Resource
  Manager, pandas, IPython) is logged with its duration; result cache hit, miss, bypass and
  uncached partial results, batch retries, exhausted retries and fail-fast cancellation, the cost
  guard passing (with the tables read) and `clear_cache` are logged at INFO or WARNING.
- WARNING and above from the `azure` SDK loggers are written to `notebooks.log` too. They now
  appear only there, not in the cell's stderr.
- `notebooks.log` is created with mode 0600, opened without following symbolic links, and
  written only if it is a regular file you own with a single link. The project root search no
  longer goes above your home directory and is cached per working directory.

### Security

- `notebooks.log` never contains KQL text: queries are logged as `kql=hmac:<12 hex> len=<n>`
  (HMAC-SHA256 under a random per-process key). Result values, environment variable values,
  headers, tokens, URL query strings, `timespan` values (only durations and dates), the full ARM
  resource ID and the workspace, cluster host and database names (masked) are not logged either.
  Log fields are written only as plain numbers, dates and short identifier-like strings.
- Every line is redacted by the file formatter (tokens, `Authorization` values, URL query
  strings, key-like parameters, emails, IP addresses, GUIDs, home directory user names).
  Cookie and API key headers, camelCase and prefixed secret keys, and Azure resource path
  segments are masked too. Redaction input is capped at 4 KB per text, and line breaks in a
  message are escaped so a record stays on one line. SDK error messages are best effort:
  shortened, with quoted text (including `b'..'`, backticks and unbalanced quotes), known secret
  shapes and identifiers removed. Tracebacks no longer include source lines and their messages
  get the same treatment.

## 3.0.0 - 2026-10-23

### Breaking

- Logging is now always on and goes to a file. Every record at INFO and above from the
  `security_notebook_tools` logger is appended to `notebooks.log` in the project root (the nearest
  directory above the working directory with `.git` or `pyproject.toml`, else the working
  directory), one line each: `2026-10-23T14:03:12.345Z INFO security_notebook_tools.query: ...`.
  Nothing is printed to the notebook cell any more. `enable_logging`, `disable_logging`, the
  `verbose` parameter of every function and client method, and the `%%kql -v` flag are removed.
  Add `notebooks.log` to your `.gitignore`.
- `SENTINEL_WORKSPACE_RESOURCE_ID` is removed, with no fallback. Set three plain values instead,
  all required for `target="sentinel"`: `SENTINEL_SUBSCRIPTION_ID` (GUID),
  `SENTINEL_RESOURCE_GROUP` and `SENTINEL_WORKSPACE_NAME`. The package assembles the ARM
  resource ID itself. To migrate, split your old value
  `/subscriptions/<subscription-id>/resourceGroups/<group>/providers/Microsoft.OperationalInsights/workspaces/<name>`
  into those three parts. They must still name the same workspace as `SENTINEL_WORKSPACE_ID`.
  Errors now name the specific variable that is missing or invalid.

### Added

- Network and HTTP failures become package errors that explain the cause, instead of raw SDK
  tracebacks. A dropped connection (for example "[Errno 104] Connection reset by peer") raises
  `QueryConnectionError`, and the message says what it usually means and what to try. HTTP error
  messages gain a hint for 400, 401, 403, 404, 408, 413, 429, 5xx. ADX authentication and client
  errors are now `QueryError` too. The new classes derive from `QueryError`; they are not
  exported.
- Every SDK boundary (client setup, the query call, result conversion, the table-plan check) now
  maps its known errors to an explaining `QueryError`: service error codes (Log Analytics
  `SyntaxError`, `PathNotFoundError`, `InsufficientAccessError`, ...; Kusto
  `BadRequest_SyntaxError`, `BadRequest_EntityNotFound`, ...), a missing Azure CLI or login, and
  Sentinel partial results get hints. Any other exception is logged at ERROR with its traceback
  (to the `security_notebook_tools` logger), raised as a `QueryError` that chains it, and not
  retried by `exec_batch_query`. `ConfigurationError`, `KeyboardInterrupt` and `SystemExit` pass
  through unchanged.
- `exec_batch_query` retries any transient failure (connection errors, HTTP 429/502/503), not
  only throttling, with the same backoff and limit. The warning names the reason, and a window
  that still fails says it was retried.
- `pivot_dataframe(df, index, columns, values=None, *, agg_func="sum", fill_value=None,
  totals=False, max_columns=100)` pivots a result into one row per `index` key and one column per
  `columns` key, counting rows or aggregating `values`. The flat result keeps `kql_types` and goes
  straight into `format_dataframe`.
- `format_dataframe` shows the `Total` row and columns of a `pivot_dataframe(..., totals=True)`
  result in bold, set off by a rule. They are recorded in `df.attrs["pivot_totals"]`, so a row or
  column that merely contains or is named `Total` is not styled as a total.
- `format_dataframe` gains a keyword `column_styles: Mapping[str, str] | None = None` mapping
  column names to a display style. The one style, `"readable_numbers"`, groups the integer digits
  of a numeric (`int`, `long`, `real`, `decimal`) column with spaces: `1 000 000 000.00000`.
  Unknown styles or columns raise `ValueError`; a style on a column of a type it does not fit
  (such as a `string`) raises `TypeError`.
- Query result cache, on by default: `exec_query` and `exec_batch_query` gain a keyword
  `cache: bool = True` (before `verbose`), and `%%kql` gains `--no-cache`. Results are kept in
  memory for up to 32 distinct queries (least recently used dropped), keyed by target,
  connection settings, exact KQL, resolved time range and timeout. A hit sends no request. A
  relative time range (a `timedelta`, including the default last 1 day) is not re-evaluated, so a
  hit returns the data from when the query first ran. Failures and partial Sentinel results are
  not cached, and each call gets a copy. `exec_batch_query` caches each window separately, so a
  re-run with the same absolute `start`/`end` after a failure reuses the windows that succeeded.
  Pass `cache=False` for one fresh call.
- `clear_cache()` forgets cached results and also the cached clients, credentials and table
  plans, so the next query starts fresh.
- `exec_batch_query(kql, start, end=None, *, interval=timedelta(days=1), target, timeout,
  max_workers=1, merge_on=None, agg_column=None, agg_func="sum", verbose)` runs the same KQL once
  per consecutive time window and merges the results into one DataFrame, for queries too big for one
  run or needing a longer period. Each window defines `BatchStart` and `BatchEnd` in the query and,
  on Sentinel, is also sent as the API time range. The KQL must filter with `>= BatchStart` and
  `< BatchEnd` (half-open, so a boundary row is counted once); this is checked before any query, on
  both targets, and other comparisons of the two names are rejected. Aggregations
  run per window; the optional `merge_on`, `agg_column` and `agg_func` (default `"sum"`) keywords
  then group the merged rows and re-aggregate them (for example sum per-day counts by user),
  validated up front and against the first window's columns. `max_workers` (1 to 5, default 1) runs
  windows in parallel after the first one has run alone; 5 is the Log Analytics per-user concurrency
  limit, and ADX's limit is shared with other users. The first failure cancels the windows not yet
  started. A throttled window (HTTP 429) is retried up to 4 times with exponential backoff and
  jitter; other errors are not retried.
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

### Fixed

- ADX queries that the service throttled (HTTP 429) escaped as a raw `KustoThrottlingError`
  instead of `QueryError`. They now raise a `QueryError` (the private subclass
  `QueryThrottledError`); Sentinel 429 responses raise it too.

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
