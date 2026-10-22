# Notebook setup (Jupyter and VS Code)

How to use `security-notebook-tools` from notebooks in your own (consumer) project. For
developing this repository itself, see the README and `.vscode/` in the repo.

## 1. Create a project and install the package

```sh
uv init my-sec-notebooks
cd my-sec-notebooks
uv add git+https://github.com/evictedcucumber/security-notebook-tools@v1.0.0
uv add --dev ipykernel
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

The package reads `SENTINEL_WORKSPACE_ID`, `ADX_CLUSTER_URI`, `ADX_DATABASE` and optionally
`AZURE_TENANT_ID` on every call.

**Option A: `.env` file.** Create `.env` in the project root (add it to `.gitignore`):

```sh
AZURE_TENANT_ID=
SENTINEL_WORKSPACE_ID=<workspace-id>
ADX_CLUSTER_URI=https://<cluster>.<region>.kusto.windows.net
ADX_DATABASE=<database>
```

VS Code loads it into notebook kernels through the `python.envFile` setting (see below).
Restart the kernel after editing `.env`. Jupyter outside VS Code does not read it.

**Option B: a cell.**

```python
import os

os.environ["SENTINEL_WORKSPACE_ID"] = "<workspace-id>"
```

Variables are read per call, so changing them takes effect on the next query.

## 4. Run a query

```python
from security_notebook_tools import exec_query

df = exec_query("SigninLogs | take 100")
df.head()
```

Add `verbose=True` or call `enable_logging()` to see what is being run.

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
