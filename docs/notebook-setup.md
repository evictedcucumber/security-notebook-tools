# Notebook setup (Jupyter and VS Code)

How to use `security-notebook-tools` from notebooks in your own (consumer) project. For
developing this repository itself, see the README and `.vscode/` in the repo.

## 1. Create a project and install the package

```sh
uv init my-sec-notebooks
cd my-sec-notebooks
uv add git+https://github.com/evictedcucumber/security-notebook-tools@<full-commit-sha>
uv add --dev ipykernel
```

Pinning to a full commit SHA is the recommended form, and install with `uv sync --locked`. A tag
is less strict, because tags can be moved, and an install without a ref is the least strict (it
takes whatever `main` is):

```sh
uv add git+https://github.com/evictedcucumber/security-notebook-tools@v7.2.0
```

The project needs Python 3.14 or newer. Other install options (latest main, editable local
checkout, pip) are in the README.

If you install the package with its `notebook` extra, `ipykernel` comes with it:
`uv add "security-notebook-tools[notebook] @ git+https://github.com/evictedcucumber/security-notebook-tools"`.

## 2. Pick a kernel

**VS Code (simplest).** Open the `.ipynb`, click "Select Kernel", choose "Python Environments",
then the project's `.venv`. No registration needed. Install the Python and Jupyter extensions.

**Jupyter / other front ends.** Register the venv as a named kernel:

```sh
uv run python -m ipykernel install --user --name my-sec-notebooks --display-name "Security notebooks"
```

Then select "Security notebooks" in the kernel picker. Without adding the dependency:
`uv run --with ipykernel python -m ipykernel install --user --name my-sec-notebooks`.

## 3. Authenticate and configure

```sh
az login
```

The package reads `SENTINEL_WORKSPACE_ID`, `SENTINEL_SUBSCRIPTION_ID`,
`SENTINEL_RESOURCE_GROUP`, `SENTINEL_WORKSPACE_NAME`, `ADX_CLUSTER_URI`, `ADX_DATABASE` and
optionally `AZURE_TENANT_ID` on every call.

`SENTINEL_SUBSCRIPTION_ID`, `SENTINEL_RESOURCE_GROUP` and `SENTINEL_WORKSPACE_NAME` identify the
workspace in Azure (the name, not the ID). Sentinel queries use them to read the workspace's table
plans and saved functions, so that only Analytics-plan tables are ever queried (Basic, Auxiliary
and data lake tables bill per GB scanned). They must be the same workspace as
`SENTINEL_WORKSPACE_ID`. Your identity needs `Microsoft.OperationalInsights/workspaces/tables/read`
and `.../savedSearches/read` on the workspace (Log Analytics Reader includes both). If the plans
cannot be read, queries are not sent. See the README's Cost guard section for what is rejected.

Treat the `.env` file (loaded into the kernel by VS Code's `python.envFile`, see below) as trusted
input. It sets `ADX_CLUSTER_URI`, and can also set `HTTPS_PROXY`, `REQUESTS_CA_BUNDLE` /
`SSL_CERT_FILE` and `PATH` (which `az` runs from). The package only requires the ADX cluster to be
on a Microsoft Kusto domain (such as `*.kusto.windows.net`), so any cluster there, including one in
another tenant, is accepted and would receive the query and a Kusto access token. Review `.env`,
check the cluster host, and never use a `.env` from an untrusted source.

### Find the values with the Azure CLI

Sign in, optionally to a specific tenant, and pick the subscription that holds your workspace:

```sh
az login                               # or: az login --tenant <tenant>
az account list -o table
az account set --subscription <name-or-id>
```

| Variable | Command |
| --- | --- |
| `AZURE_TENANT_ID` | `az account show --query tenantId -o tsv` |
| `SENTINEL_SUBSCRIPTION_ID` | `az account show --query id -o tsv` |
| `SENTINEL_RESOURCE_GROUP`, `SENTINEL_WORKSPACE_NAME` | `az monitor log-analytics workspace list --query "[].{name:name, resourceGroup:resourceGroup, customerId:customerId}" -o table` |
| `SENTINEL_WORKSPACE_ID` | `az monitor log-analytics workspace show -g <rg> -n <workspace-name> --query customerId -o tsv` |
| `ADX_CLUSTER_URI` | `az kusto cluster list --query "[].{name:name, resourceGroup:resourceGroup, uri:uri}" -o table`, or `az kusto cluster show -g <rg> -n <cluster> --query uri -o tsv` |
| `ADX_DATABASE` | `az kusto database list -g <rg> --cluster-name <cluster> --query "[].name" -o tsv` |

Notes:

- The `az kusto` commands need the `kusto` extension. `az` offers to install it on first use, or
  run `az extension add --name kusto`.
- Database names come back as `<cluster>/<database>`; use only the part after the slash.
- Fabric Eventhouse URIs (`*.kusto.fabric.microsoft.com`) are not listed by `az`. Copy the
  Query URI from the Fabric portal.

To print ready-to-paste `.env` lines for Sentinel (set `RG` and `WS` from the workspace list):

```sh
RG=<resource-group>
WS=<workspace-name>
echo "AZURE_TENANT_ID=$(az account show --query tenantId -o tsv)"
echo "SENTINEL_WORKSPACE_ID=$(az monitor log-analytics workspace show -g "$RG" -n "$WS" --query customerId -o tsv)"
echo "SENTINEL_SUBSCRIPTION_ID=$(az account show --query id -o tsv)"
echo "SENTINEL_RESOURCE_GROUP=$RG"
echo "SENTINEL_WORKSPACE_NAME=$WS"
```

For ADX, add `ADX_CLUSTER_URI` and `ADX_DATABASE` from the last two rows of the table.

**Option A: `.env` file.** Create `.env` in the project root (add it to `.gitignore`):

```sh
AZURE_TENANT_ID=
SENTINEL_WORKSPACE_ID=<workspace-id>
SENTINEL_SUBSCRIPTION_ID=<subscription-id>
SENTINEL_RESOURCE_GROUP=<resource-group>
SENTINEL_WORKSPACE_NAME=<workspace-name>
ADX_CLUSTER_URI=https://<cluster>.<region>.kusto.windows.net
ADX_DATABASE=<database>
```

VS Code loads it into notebook kernels through the `python.envFile` setting (see below).
Restart the kernel after editing `.env`. Jupyter outside VS Code does not read it.

**Option B: a cell.**

```python
import os

os.environ["SENTINEL_WORKSPACE_ID"] = "<workspace-id>"
os.environ["SENTINEL_SUBSCRIPTION_ID"] = "<subscription-id>"
os.environ["SENTINEL_RESOURCE_GROUP"] = "<resource-group>"
os.environ["SENTINEL_WORKSPACE_NAME"] = "<workspace-name>"
```

Variables are read per call, so changing them takes effect on the next query.

## 4. Run a query

```python
from security_notebook_tools import exec_query

df = exec_query("SigninLogs | take 100")
df.head()
```

A query that reads a non-Analytics table (or uses `search`, `union *`, saved functions and so on)
raises an error naming the reason and is not sent.

What is being run is logged to `notebooks.log` in the project root. Add that file to `.gitignore`.

Notebook files store their outputs: query results, rendered tables, `debug=True` output and error
tracebacks (which may include raw service messages such as UPNs, resource IDs and query
fragments). Strip outputs (for example with `nbstripout` or "Clear All Outputs") before committing
or sharing a notebook, and avoid `%xmode Verbose` with these calls.

The result cache is keyed on tenant, workspace and query, not on the signed-in `az` account. After
`az login` as a different account, or a permission change, call `clear_cache()`.

## Workspace settings (`.vscode/settings.json`)

For a consumer project:

```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python",
  "python.envFile": "${workspaceFolder}/.env",
  "jupyter.notebookFileRoot": "${workspaceFolder}",
  "notebook.lineNumbers": "on",
  "notebook.output.scrolling": true
}
```

On Windows use `.venv/Scripts/python.exe` for the interpreter. The ruff entries below are
optional and need the Ruff extension (`charliermarsh.ruff`).

## Global VS Code user settings

Settings that make sense for every project go in your user `settings.json`
(Command Palette: "Preferences: Open User Settings (JSON)"):

```json
{
  "jupyter.notebookFileRoot": "${workspaceFolder}",

  "notebook.lineNumbers": "on",
  "notebook.formatOnSave.enabled": true,
  "notebook.codeActionsOnSave": {
    "notebook.source.fixAll": "explicit",
    "notebook.source.organizeImports": "explicit"
  },
  "notebook.output.scrolling": true,
  "notebook.output.textLineLimit": 200,

  "[python]": {
    "editor.defaultFormatter": "charliermarsh.ruff",
    "editor.formatOnSave": true
  }
}
```

Do not put `python.envFile` or the interpreter path in user settings; they are per project.
The `notebook.*` format and code-action settings need the Ruff extension to do anything.

Where the file lives:

| OS | Path |
| --- | --- |
| Linux | `~/.config/Code/User/settings.json` |
| macOS | `~/Library/Application Support/Code/User/settings.json` |
| Windows | `%APPDATA%\Code\User\settings.json` |

WSL: user settings live on the Windows side (the Windows path above) and apply to Remote-WSL
windows. To set values only for the WSL machine, run "Preferences: Open Remote Settings" from
the Command Palette while connected to WSL.

## Autoreload for editable installs

If you installed the package with `--editable` and are changing its source, reload edits
without restarting the kernel. Run this once at the top of the notebook:

```python
%load_ext autoreload
%autoreload 2
```

Re-run `from security_notebook_tools import exec_query` if a name was already imported and
does not pick up the change.
