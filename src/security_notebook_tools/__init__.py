"""Query Microsoft Sentinel and Azure Data Explorer into pandas DataFrames.

Run ``az login`` first, set the connection environment variables (``SENTINEL_WORKSPACE_ID`` and
``SENTINEL_WORKSPACE_RESOURCE_ID``, or ``ADX_CLUSTER_URI`` and ``ADX_DATABASE``; optionally
``AZURE_TENANT_ID``), then:

    >>> from security_notebook_tools import exec_query
    >>> df = exec_query("SigninLogs | take 100")

Sentinel queries are cost-guarded: only tables on the Log Analytics Analytics plan may be read
(Basic, Auxiliary and Sentinel data lake data bill per GB scanned), checked client-side against
the workspace's table plans before the query is sent. If the plans cannot be verified, nothing is
sent.

ADX access is read-only: only ``.show`` management commands are allowed and requests are sent
with ``request_readonly``. The Sentinel data lake and Log Analytics proxy hosts are rejected as
clusters, and ``cluster()``, ``external_table()`` and ``externaldata`` are rejected in KQL.
Connection settings are validated and invalid ones raise an error.

Public API:

* ``exec_query``: run KQL against Sentinel or ADX and get a DataFrame.
* ``enable_logging``, ``disable_logging``: show the package's logs in the notebook.

Use ``help(function)`` for details. Submodules are private and may change.
"""

from ._log import disable_logging, enable_logging
from ._query import exec_query

__version__ = "2.0.0"

__all__ = [
    "disable_logging",
    "enable_logging",
    "exec_query",
]


def __dir__() -> list[str]:
    # Keep tab completion / dir() limited to the public API (hides private submodules).
    return [*__all__, "__version__"]
