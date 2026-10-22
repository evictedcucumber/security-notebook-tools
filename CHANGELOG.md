# Changelog

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
