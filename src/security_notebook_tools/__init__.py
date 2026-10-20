"""Query Microsoft Sentinel and Azure Data Explorer into pandas, and tidy the results.

Run ``az login`` first, set the connection environment variables (``SENTINEL_WORKSPACE_ID``,
or ``ADX_CLUSTER_URI`` and ``ADX_DATABASE``; optionally ``AZURE_TENANT_ID``), then:

    >>> from security_notebook_tools import exec_query, drop_empty_columns
    >>> df = exec_query("SigninLogs | take 100").pipe(drop_empty_columns)

Public API:

* ``exec_query``: run KQL against Sentinel or ADX and get a DataFrame.
* ``drop_empty_columns``, ``parse_json_columns``, ``expand_column``, ``truncate_strings``,
  ``format_timestamps``, ``reorder_columns``: DataFrame tidying helpers that return new frames.
* ``summarize_df``, ``display_df``: inspect a DataFrame.
* ``enable_logging``, ``disable_logging``: show the package's logs in the notebook.

Use ``help(function)`` for details. Submodules are private and may change.
"""

from ._formatting import (
    display_df,
    drop_empty_columns,
    expand_column,
    format_timestamps,
    parse_json_columns,
    reorder_columns,
    summarize_df,
    truncate_strings,
)
from ._log import disable_logging, enable_logging
from ._query import exec_query

__version__ = "0.1.0"

__all__ = [
    "disable_logging",
    "display_df",
    "drop_empty_columns",
    "enable_logging",
    "exec_query",
    "expand_column",
    "format_timestamps",
    "parse_json_columns",
    "reorder_columns",
    "summarize_df",
    "truncate_strings",
]


def __dir__() -> list[str]:
    # Keep tab completion / dir() limited to the public API (hides private submodules).
    return [*__all__, "__version__"]
