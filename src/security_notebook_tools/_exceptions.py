"""Exception types raised by security_notebook_tools.

They are not exported from the package; catch ``ValueError`` for configuration problems or
``Exception`` generally. ``QueryError`` and ``ConfigurationError`` derive from
``SecurityNotebookToolsError``. ``QueryTransientError`` (a ``QueryError``) marks a failure that
may succeed if retried; ``QueryThrottledError`` and ``QueryConnectionError`` derive from it, and
``exec_batch_query`` retries them. ``QueryTimeoutError`` (also a ``QueryError``, but not a
transient one) marks a query the service or the client gave up on for taking too long; with
``min_interval`` ``exec_batch_query`` splits such a window instead of failing.
"""

from __future__ import annotations

from typing import Any


class SecurityNotebookToolsError(Exception):
    """Base class for all package errors."""


class QueryError(SecurityNotebookToolsError):
    """A query failed (service error, bad KQL, authorisation failure, ...)."""


class QueryTransientError(QueryError):
    """A failure that may succeed if retried (throttling, a dropped connection, HTTP 502/503).

    ``exec_batch_query`` retries a window that fails with this; otherwise it is a ``QueryError``.
    """


class QueryThrottledError(QueryTransientError):
    """The service throttled the request (HTTP 429 or a "too many requests" error)."""


class QueryConnectionError(QueryTransientError):
    """The connection failed before a full HTTP response arrived.

    Covers a reset, a refusal, a timeout and DNS failures. The message explains the cause.
    """


class QueryTimeoutError(QueryError):
    """A query ran out of time: the service timed it out or the client's deadline passed.

    Raised for HTTP 504 and 408 (``GatewayTimeout``), for a Sentinel request that took longer than
    ``server_timeout`` plus the client allowance, and by ``exec_batch_query`` for a connection
    reset after a window ran for at least half of ``timeout`` (the query was cut off for being
    too slow). It is not transient: the same query would time out again, so it is not retried.
    ``exec_batch_query`` splits such a window into halves when ``min_interval`` is given.
    """


class PartialResultError(QueryError):
    """A query returned only partial results and ``raise_on_partial`` was set.

    ``partial_error`` holds the service error; ``partial_data`` the tables returned so far.
    ``visualization`` is the raw ``| render`` spec the service sent with them, or ``None``.
    """

    def __init__(
        self,
        message: str,
        partial_error: Any = None,
        partial_data: Any = None,
        visualization: Any = None,
    ) -> None:
        """Store the service error, partial tables and render spec alongside the message."""
        super().__init__(message)
        self.partial_error = partial_error
        self.partial_data = partial_data
        self.visualization = visualization


class ConfigurationError(SecurityNotebookToolsError, ValueError):
    """Invalid arguments or missing configuration (e.g. an unset env var, a bad timeout)."""
