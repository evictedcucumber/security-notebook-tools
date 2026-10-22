# security-notebook-tools

KQL helpers for Jupyter notebooks: run queries against Microsoft Sentinel (Log Analytics) and
Azure Data Explorer (Kusto) and get pandas DataFrames back.

Prerequisites:

- Python 3.14 or newer.
- The [Azure CLI](https://learn.microsoft.com/cli/azure/) signed in with `az login`.
  Authentication uses the Azure CLI credential only.
- Read access to the Sentinel workspace and/or ADX database you want to query.

## Install

The package is not on PyPI. Install it from GitHub or a local checkout.

Into a uv-managed project:

```sh
# Latest main
uv add git+https://github.com/evictedcucumber/security-notebook-tools

# A specific release
uv add git+https://github.com/evictedcucumber/security-notebook-tools@v1.0.0

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

df = exec_query("SigninLogs | take 100")  # last 1 day by default
df.head()
```

Instead of setting variables in a cell, put them in a `.env` file (copy
[.env.example](.env.example)) and let VS Code load it into the kernel; see
[docs/notebook-setup.md](docs/notebook-setup.md).

## Configuration

| Env var | Used for | Required |
| --- | --- | --- |
| `AZURE_TENANT_ID` | Tenant passed to `AzureCliCredential` | No (defaults to the CLI's current tenant) |
| `SENTINEL_WORKSPACE_ID` | Sentinel / Log Analytics workspace ID | For `target="sentinel"` (the default) |
| `ADX_CLUSTER_URI` | ADX cluster URI | For `target="adx"` |
| `ADX_DATABASE` | ADX database | For `target="adx"` |

Env vars are read on every call, so changing `os.environ` in a cell takes effect on the next
query. Clients are created on first use and reused.

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
logger (children: `sentinel`, `adx`, `auth`, `timerange`). Nothing is printed by default.

- `exec_query(..., verbose=True)` prints that call's logs (INFO and above) in the cell output.
- `enable_logging()` (optionally `enable_logging("DEBUG")`) does so for the whole session;
  `disable_logging()` undoes it.
- Or configure `logging.getLogger("security_notebook_tools")` yourself.

```python
df = exec_query("SigninLogs | take 10", timedelta(days=1), verbose=True)
# 14:02:11 INFO security_notebook_tools.sentinel: Running Sentinel query (timespan=1 day, 0:00:00): SigninLogs | take 10
# 14:02:12 INFO security_notebook_tools.sentinel: Sentinel query returned 1 table(s), rows=[10] in 0.84s
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
