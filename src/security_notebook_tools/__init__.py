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
clusters, and ``cluster()``, ``external_table()`` and ``externaldata`` are rejected in KQL.
Connection settings are validated and invalid ones raise an error.

Public API:

* ``exec_query``: run KQL against Sentinel or ADX and get a DataFrame.
* ``exec_batch_query``: run KQL once per time window and merge the results.
* ``format_dataframe``: display a result with values formatted by their KQL type.
* ``pivot_dataframe``: pivot a result into one row per key and one column per value of another.
* ``clear_cache``: forget cached query results (and clients, credentials and table plans).

In IPython notebooks, ``%load_ext security_notebook_tools`` adds a ``%%kql`` cell magic that
runs the cell body as KQL through ``exec_query`` and displays it with ``format_dataframe``
(see ``help(load_ipython_extension)``).

Logging: every record at INFO and above from every module is appended to ``notebooks.log`` in
the project root (see ``security_notebook_tools._log`` for the levels), and so are WARNING and
above from the ``azure`` SDK loggers; nothing is printed to the cell. Every line is redacted:
KQL text, result values, environment variable values, tokens and URL query strings are never
written. Gitignore the file.

Use ``help(function)`` for details. Submodules are private and may change.
"""

import logging

from ._batch import exec_batch_query
from ._format import format_dataframe
from ._log import describe_exception
from ._pivot import pivot_dataframe
from ._query import clear_cache, exec_query

__version__ = "3.0.0"

__all__ = [
    "clear_cache",
    "exec_batch_query",
    "exec_query",
    "format_dataframe",
    "pivot_dataframe",
]


def load_ipython_extension(ipython) -> None:
    """Register the ``%%kql`` cell magic (IPython calls this from ``%load_ext``).

    Run ``%load_ext security_notebook_tools`` in a notebook, then write raw KQL in a cell::

        %%kql [-t TIMESPAN] [--target {sentinel,adx}] [--timeout SECONDS] [--no-cache]

    The body is passed verbatim to ``exec_query``, which does all validation, and the result is
    displayed with ``format_dataframe``. The DataFrame is not kept; call ``exec_query`` directly
    for that. ``--timespan`` takes a duration such as ``1d``, ``12h``, ``30m``,
    ``1d12h`` or ``90s``, or a Python expression evaluated in the notebook namespace (a
    variable, ``(start, end)``, ``timedelta(hours=3)``). IPython must be installed (it is with
    the ``notebook`` extra).

    Args:
        ipython: The active ``InteractiveShell``.
    """
    log = logging.getLogger("security_notebook_tools.magic")
    try:
        from ._magic import KqlMagics

        ipython.register_magics(KqlMagics)
    except Exception as exc:
        log.error("Could not load the IPython extension: %s", describe_exception(exc))
        raise
    log.info("IPython extension loaded: the %%kql cell magic is registered")


def __dir__() -> list[str]:
    # Keep tab completion / dir() limited to the public API (hides private submodules).
    return [*__all__, "__version__"]
