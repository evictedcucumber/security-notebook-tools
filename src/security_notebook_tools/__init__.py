"""Query Microsoft Sentinel and Azure Data Explorer into pandas DataFrames.

Run ``az login`` first, set the connection environment variables (``SENTINEL_WORKSPACE_ID``,
or ``ADX_CLUSTER_URI`` and ``ADX_DATABASE``; optionally ``AZURE_TENANT_ID``), then:

    >>> from security_notebook_tools import exec_query
    >>> df = exec_query("SigninLogs | take 100")

Public API:

* ``exec_query``: run KQL against Sentinel or ADX and get a DataFrame.
* ``enable_logging``, ``disable_logging``: show the package's logs in the notebook.

Use ``help(function)`` for details. Submodules are private and may change.
"""

from ._log import disable_logging, enable_logging
from ._query import exec_query

__version__ = "1.0.0"

__all__ = [
    "disable_logging",
    "enable_logging",
    "exec_query",
]


def __dir__() -> list[str]:
    # Keep tab completion / dir() limited to the public API (hides private submodules).
    return [*__all__, "__version__"]
