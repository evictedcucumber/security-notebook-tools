# CLAUDE.md

Guide for AI agents working in this repository.

## Purpose

`security-notebook-tools` is a Python package (>=3.14) for running KQL against Microsoft
Sentinel and Azure Data Explorer from Jupyter notebooks and getting pandas DataFrames back.
Version 4.0.0. MIT licence.

## Layout

- `src/security_notebook_tools/`: the package. Every module is private (`_query.py`, `_batch.py`,
  `_pivot.py`, `_log.py`, `_config.py`, `_timerange.py`, `_sentinel.py`, `_adx.py`, `_auth.py`,
  `_exceptions.py`, `_errors.py`, `_kql.py`, `_tablepolicy.py`, `_format.py`, `_magic.py`). `_magic.py` is the
  `%%kql` cell magic, registered by `load_ipython_extension` in `__init__.py`.
- `_kql.py` (tokenizer) and `_tablepolicy.py` (ARM table-plan check) are the cost guard: Sentinel
  queries may only read Analytics-plan tables. They fail closed; treat any loosening as a
  security change and add a regression test for every bypass.
- Public API is only what `__init__.py` lists in `__all__`: `exec_query`, `exec_batch_query`,
  `format_dataframe`, `pivot_dataframe`, `clear_cache` (plus `__version__`).
  `tests/test_query.py::test_public_surface` pins it; changing the API means
  updating that test deliberately.
- `tests/`: pytest. `docs/`: `api.md`, `notebook-setup.md`. `.vscode/`, `.env.example`: workspace
  config for this repo.

## Commands

uv is not on PATH outside the Nix dev shell, so prefix with `nix develop -c`:

```sh
nix develop -c uv sync
nix develop -c uv run pytest
nix develop -c uv run ruff check . && nix develop -c uv run ruff format --check .
nix develop -c uv build
```

Flakes only see git-tracked files; `git add` new files before relying on `nix develop`.

## Conventions

- Google-style docstrings, enforced by ruff `D` rules (tests exempt). Line length 100.
  Ruff rules: E, F, I, UP, B, SIM, C4, D.
- No `print`. Log through child loggers of `security_notebook_tools` (for example
  `logging.getLogger("security_notebook_tools.sentinel")`). Everything at INFO and above goes to
  `notebooks.log` in the project root, redacted (`_log.py` documents the levels). DEBUG is never
  written there; it is shown, unredacted, in the notebook cell only for a call made with
  `debug=True`
  (`_log.debug_scope`, a `ContextVar`, opened by `public_call(debug=...)`; submit worker-thread
  jobs through `_log.in_current_context`, keep DEBUG-only work cheap). Never log KQL
  text at INFO and above (use `fingerprint`; raw details go in `logger.debug` companions), result values, env var values or headers; wrap dependency calls in
  `dependency_call`, public functions in `_errors.public_call`, and pass SDK messages through
  `redact_exception_text`.
- Environment variables are read on every call (`_config.py`), never at import time. Clients and
  credentials are cached, table plans for 5 minutes (`_tablepolicy.py`), and query results in
  `functools.lru_cache` functions in `_query.py` (on by default, `cache=False` per call).
  `clear_cache()` resets all of them; `tests/conftest.py` calls it around every test. A new
  cache must be cleared by `clear_cache()` too.
- Docstrings are the source of truth for behaviour; keep README and docs in line with them.

## Keeping docs in sync

When the public API changes, update in the same change: `README.md`, `docs/api.md`,
the `__init__.py` module docstring, and `CHANGELOG.md`. The version lives in two places,
`pyproject.toml` and `src/security_notebook_tools/__init__.py` (`__version__`); change both.
Do not commit unless asked.
