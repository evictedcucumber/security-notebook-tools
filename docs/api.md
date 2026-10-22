# API reference

The public API is exactly these names, importable from `security_notebook_tools`:

```python
from security_notebook_tools import exec_query, enable_logging, disable_logging
import security_notebook_tools

security_notebook_tools.__version__  # "1.0.0"
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
the Azure CLI, so run `az login` first.

`Timespan` is `timedelta | tuple[datetime, datetime] | tuple[datetime, timedelta]`.

### Parameters

| Name | Description |
| --- | --- |
| `kql` | The KQL query. With `target="adx"`, text starting with `.` (after leading whitespace) is run as a management command such as `.show tables`. |
| `timespan` | Sentinel only. A `timedelta` (the period ending now), a `(start, end)` tuple of datetimes, or a `(start, timedelta)` tuple. Passing it with `target="adx"` raises `ConfigurationError`; filter on time in the KQL instead. |
| `target` | `"sentinel"` (default) or `"adx"`. |
| `timeout` | Server-side timeout in seconds (default 300); must be positive. Applies to both targets. |
| `verbose` | If true, print this call's logs (INFO and above) to the cell output. |

`target`, `timeout` and `verbose` are keyword-only.

### Environment variables

Read on every call (not cached), so changing `os.environ` takes effect immediately.

| Variable | Used for | Required |
| --- | --- | --- |
| `SENTINEL_WORKSPACE_ID` | Sentinel workspace ID | `target="sentinel"` |
| `ADX_CLUSTER_URI` | ADX cluster URI | `target="adx"` |
| `ADX_DATABASE` | ADX database | `target="adx"` |
| `AZURE_TENANT_ID` | Tenant for the Azure CLI credential | No (defaults to the CLI's current tenant) |

An empty variable counts as unset. Clients are created on first use and cached per
workspace/cluster and tenant.

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
  `timespan` is combined with `target="adx"`, or a required environment variable is unset.
- `QueryError`: the service rejected or failed the query (bad KQL, authorisation, timeout, ...).

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

Ways to see the logs:

- `exec_query(..., verbose=True)`: that call only, INFO and above.
- `enable_logging()` / `disable_logging()`: the whole session.
- Your own configuration, e.g. `logging.getLogger("security_notebook_tools").setLevel("DEBUG")`
  with your own handler.

If logs are already being printed (via `enable_logging`), `verbose=True` does not duplicate them.

## `__version__`

The installed version string, e.g. `"1.0.0"`.
