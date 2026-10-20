# security-notebook-tools

KQL helpers for Jupyter notebooks: run queries against Microsoft Sentinel (Log Analytics) and
Azure Data Explorer (Kusto), get pandas DataFrames back, and tidy them with small formatting
helpers.

Prerequisites:

- Python 3.14 or newer.
- The [Azure CLI](https://learn.microsoft.com/cli/azure/) signed in with `az login`.
  Authentication uses the Azure CLI credential only.
- Read access to the Sentinel workspace and/or ADX database you want to query.

## Install

Into another uv-managed project:

```sh
uv add --editable ../security_notebook_tools
# or
uv add git+https://github.com/<you>/security_notebook_tools
```

For `display_df` inside notebooks add the extra: `uv add "security-notebook-tools[notebook]"`.
The extra installs `ipython` and `ipykernel`.

### Jupyter kernel from a uv venv

In the consuming project (which has the package installed):

```sh
uv add --dev ipykernel
uv run python -m ipykernel install --user --name my-sec-notebooks --display-name "Security notebooks"
```

Then pick that kernel in Jupyter/VS Code. (One-off alternative without adding the dependency:
`uv run --with ipykernel python -m ipykernel install --user --name my-sec-notebooks`.)

## Configuration

| Env var | Used for | Required |
| --- | --- | --- |
| `AZURE_TENANT_ID` | Tenant passed to `AzureCliCredential` | No (defaults to the CLI's current tenant) |
| `SENTINEL_WORKSPACE_ID` | Sentinel / Log Analytics workspace ID | For `target="sentinel"` (the default) |
| `ADX_CLUSTER_URI` | ADX cluster URI | For `target="adx"` |
| `ADX_DATABASE` | ADX database | For `target="adx"` |

Env vars are read on every call, so you can set them in a notebook cell with `os.environ`.
Clients are created on first use and reused for later calls.

```python
import os

os.environ["SENTINEL_WORKSPACE_ID"] = "<workspace-id>"
os.environ["ADX_CLUSTER_URI"] = "https://<cluster>.<region>.kusto.windows.net"
os.environ["ADX_DATABASE"] = "<database>"
```

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
df = exec_query(".show tables", target="adx")  # management commands work too
```

`exec_query(kql, timespan=None, *, target="sentinel", timeout=300, verbose=False)` returns the
first result table as a DataFrame (empty if the query returned no tables). Sentinel datetime
columns are UTC-aware. A partial Sentinel result emits a warning and returns the data received.

Use `help(exec_query)` or Shift+Tab in a notebook for the full reference.

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

A query counts as having a time filter when a `where`/`filter` clause references
`TimeGenerated`/`Timestamp` or a time function (`ago()`, `now()`, `datetime()`,
`todatetime()`, `startofday()` and the other `startof*`/`endof*` functions), or a `let`
variable assigned from one. Comments and string literals are ignored.

Detection is a heuristic. A time filter it can't see (for example one inside a stored function
or saved query) isn't detected, so the 1-day default is applied on top of it. Pass `timespan`
explicitly in that case.

### Timeout

`timeout` is the server-side timeout in seconds, for both targets. The default is 300. It must
be positive.

```python
df = exec_query("SigninLogs | summarize count() by UserPrincipalName", timeout=600)
```

## Formatting functions

All are importable from `security_notebook_tools`. Each returns a new DataFrame and leaves the
input untouched, except `summarize_df` (returns a summary) and `display_df` (shows the frame).

| Function | What it does |
| --- | --- |
| `drop_empty_columns(df)` | Drop columns that are entirely null, empty strings or empty dict/list |
| `parse_json_columns(df, columns=None)` | Parse JSON strings (KQL `dynamic`) into dicts/lists; all string columns by default |
| `expand_column(df, column, prefix=None)` | Flatten a dict column into `<prefix>.<key>` columns; `prefix` defaults to the column name |
| `truncate_strings(df, max_len=100)` | Truncate long string cells, ending with an ellipsis |
| `format_timestamps(df, fmt="%Y-%m-%d %H:%M:%S", tz=None)` | Render datetime columns as strings, optionally in timezone `tz` |
| `reorder_columns(df, first)` | Move the `first` columns to the front |
| `summarize_df(df)` | DataFrame of each column's dtype, null count and unique count |
| `display_df(df, max_rows=50, max_colwidth=100)` | Show the first `max_rows` rows (IPython display, or plain text without IPython) |

The transforming functions also accept `verbose=True` (see Logging).

```python
from security_notebook_tools import (
    drop_empty_columns,
    parse_json_columns,
    expand_column,
    format_timestamps,
    reorder_columns,
    summarize_df,
    display_df,
)

df = (
    exec_query("SigninLogs | take 100")
    .pipe(drop_empty_columns)
    .pipe(parse_json_columns)
    .pipe(expand_column, "Properties")
    .pipe(format_timestamps, tz="Europe/London")
    .pipe(reorder_columns, ["TimeGenerated"])
)
summarize_df(df)
display_df(df, max_rows=20, max_colwidth=80)
```

## Logging

Everything logs through the standard `logging` module under the `security_notebook_tools`
logger (children: `sentinel`, `adx`, `auth`, `formatting`). Nothing is printed by default.

- Pass `verbose=True` to `exec_query` or a transforming format function to print that call's logs
  (INFO and above) in the notebook cell output.
- For the whole session use `enable_logging()` (optionally `enable_logging("DEBUG")`) and
  `disable_logging()`.
- Or configure the logger yourself via `logging.getLogger("security_notebook_tools")`.

```python
df = exec_query("SigninLogs | take 10", timedelta(days=1), verbose=True)
# 14:02:11 INFO security_notebook_tools.sentinel: Running Sentinel query (timespan=1 day, 0:00:00): SigninLogs | take 10
# 14:02:12 INFO security_notebook_tools.sentinel: Sentinel query returned 1 table(s), rows=[10] in 0.84s
df = drop_empty_columns(df, verbose=True)
```

## Errors

The exception classes are not part of the public API; read the message. Messages you may see:

- `No workspace_id configured: set the $SENTINEL_WORKSPACE_ID environment variable.` (similarly for
  `cluster_uri` / `$ADX_CLUSTER_URI` and `database` / `$ADX_DATABASE`): set the env var.
- `timespan only applies to Sentinel; filter on time in the KQL for ADX.`
- `timeout must be a positive number of seconds, got ...`
- `Unknown target ...; use 'sentinel' or 'adx'.`
- `Sentinel query failed: ...` / `ADX query failed: ...` / `ADX command failed: ...`: the
  service rejected the query (bad KQL, no access, timeout); the service's message follows.
- `Sentinel query returned partial results: ...`: a warning, not an error. The data received
  is returned.

The configuration errors above are also `ValueError`s.

## Development

Python 3.14 (see `.python-version`; the package requires `>=3.14`).

With Nix (flakes): `nix develop` gives Python 3.14, uv and ruff, runs `uv sync` if `.venv` is
missing and activates it. With direnv: `direnv allow` (uses `.envrc`). Note flakes only see
git-tracked files, so `git add` new files first.

Without Nix, plain uv:

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv build
```

Docstrings use Google style and are checked by ruff's `D` (pydocstyle) rules.
See [CHANGELOG.md](CHANGELOG.md) for release notes.
