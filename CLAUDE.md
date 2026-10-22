# CLAUDE.md

Guide for AI agents working in this repository.

## Purpose

`security-notebook-tools` is a Python package (>=3.14) for running KQL against Microsoft
Sentinel and Azure Data Explorer from Jupyter notebooks and getting pandas DataFrames back.
Version 1.0.0. MIT licence.

## Layout

- `src/security_notebook_tools/`: the package. Every module is private (`_query.py`, `_log.py`,
  `_config.py`, `_timerange.py`, `_sentinel.py`, `_adx.py`, `_auth.py`, `_exceptions.py`).
- Public API is only what `__init__.py` lists in `__all__`: `exec_query`, `enable_logging`,
  `disable_logging` (plus `__version__`). `tests/test_query.py::test_public_surface` pins it;
  changing the API means updating that test deliberately.
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
  `logging.getLogger("security_notebook_tools.sentinel")`). Notebook output is via
  `verbose_logging` / `enable_logging`.
- Environment variables are read on every call (`_config.py`), never at import time. Clients and
  credentials are cached.
- Docstrings are the source of truth for behaviour; keep README and docs in line with them.

## Keeping docs in sync

When the public API changes, update in the same change: `README.md`, `docs/api.md`,
the `__init__.py` module docstring, and `CHANGELOG.md`. The version lives in two places,
`pyproject.toml` and `src/security_notebook_tools/__init__.py` (`__version__`); change both.
Do not commit unless asked.
