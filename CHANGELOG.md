# Changelog

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
