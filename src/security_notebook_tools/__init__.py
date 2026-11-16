"""Query Microsoft Sentinel and Azure Data Explorer into pandas DataFrames.

Run ``az login`` first, set the connection environment variables (``SENTINEL_WORKSPACE_ID``,
``SENTINEL_SUBSCRIPTION_ID``, ``SENTINEL_RESOURCE_GROUP`` and ``SENTINEL_WORKSPACE_NAME``, or
``ADX_CLUSTER_URI`` and ``ADX_DATABASE``; optionally ``AZURE_TENANT_ID``), then:

    >>> from security_notebook_tools import clear_cache, exec_query
    >>> df = exec_query("SigninLogs | take 100")

Sentinel queries are cost-guarded: only tables on the Log Analytics Analytics plan may be read
(Basic, Auxiliary and Sentinel data lake data bill per GB scanned), checked client-side against
the workspace's table plans before the query is sent. If the plans cannot be verified, nothing is
sent.

ADX access is read-only: only ``.show`` management commands are allowed and requests are sent
with ``request_readonly``. The Sentinel data lake and Log Analytics proxy hosts are rejected as
clusters, and ``cluster()``, ``external_table()`` and ``externaldata`` are rejected in KQL. The one
exception is ``ADX_CLUSTER_URI`` set to the URL of a single workspace on ``adx.monitor.azure.com``
or ``ade.loganalytics.io``, which runs ADX-only KQL such as ``render anomalychart`` on Sentinel
data; ``ADX_DATABASE`` is then not needed, and every query passes the Sentinel cost guard.
Connection settings are validated and invalid ones raise an error.

Public API:

* ``exec_query``: run KQL against Sentinel or ADX and get a DataFrame.
* ``exec_query_file``: run the KQL in a ``.kql`` file with ``exec_query``.
* ``exec_batch_query``: run KQL once per time window and merge the results.
* ``format_dataframe``: display a result with values formatted by their KQL type.
* ``format_dataframe_by``: display one headed, formatted table per distinct value of a column.
* ``merge_dataframe``: group a DataFrame (or several) by key columns and aggregate columns.
* ``pivot_dataframe``: pivot a result into one row per key and one column per value of another.
* ``render_dataframe``: draw the chart a query asked for with ``| render <charttype>`` (Plotly,
  optional: ``pip install "security-notebook-tools[charts]"``); ``title=`` sets the chart's title,
  ``hidden=`` starts legend entries switched off, to be clicked on.
* ``clear_cache``: forget cached query results (and clients, credentials and table plans).

Logging: every record at INFO and above from every module is appended to ``notebooks.log`` in
the project root (see ``security_notebook_tools._log`` for the levels), and so are WARNING and
above from the ``azure`` SDK loggers; nothing is printed to the cell by default. Every line is
redacted: KQL text, result values, environment variable values, tokens and URL query strings are
never written. Gitignore the file. ``exec_query``, ``exec_query_file``, ``exec_batch_query``,
``format_dataframe``, ``format_dataframe_by``, ``merge_dataframe``, ``pivot_dataframe`` and
``render_dataframe`` take ``debug=True``: for that call, and everything it calls, the records at
DEBUG and above are also printed to the cell's output, unredacted (raw KQL, workspace IDs,
cluster URIs).
DEBUG records are never written to the file. The ``security_notebook_tools`` logger does not
propagate to the root logger (a ``logging.basicConfig`` handler does not see these records); a
handler you attach to it sees every record, DEBUG included, unredacted. Control and bidirectional
override characters are written to the file as visible escapes.

Frames from ``exec_query`` and ``exec_batch_query`` have ``attrs["partial"]`` (True if Sentinel
returned only a partial result); pass ``raise_on_partial=True`` to raise ``PartialResultError``.
If the query ends in ``| render <charttype>``, ``attrs["visualization"]`` holds the render spec for
``render_dataframe``.

Use ``help(function)`` for details. Submodules are private and may change.
"""

from ._batch import exec_batch_query
from ._format import format_dataframe, format_dataframe_by
from ._merge import merge_dataframe
from ._pivot import pivot_dataframe
from ._query import clear_cache, exec_query, exec_query_file
from ._render import render_dataframe

__version__ = "10.2.0"

__all__ = [
    "clear_cache",
    "exec_batch_query",
    "exec_query",
    "exec_query_file",
    "format_dataframe",
    "format_dataframe_by",
    "merge_dataframe",
    "pivot_dataframe",
    "render_dataframe",
]


def __dir__() -> list[str]:
    # Keep tab completion / dir() limited to the public API (hides private submodules).
    return [*__all__, "__version__"]
