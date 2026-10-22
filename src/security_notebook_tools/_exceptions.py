"""Exception types raised by security_notebook_tools.

They are not exported from the package; catch ``ValueError`` for configuration problems or
``Exception`` generally. ``QueryError`` and ``ConfigurationError`` derive from
``SecurityNotebookToolsError``.
"""

from __future__ import annotations

from typing import Any


class SecurityNotebookToolsError(Exception):
    """Base class for all package errors."""


class QueryError(SecurityNotebookToolsError):
    """A query failed (service error, bad KQL, authorisation failure, ...)."""


class QueryThrottledError(QueryError):
    """The service throttled the request (HTTP 429 or a "too many requests" error).

    ``exec_batch_query`` retries a window that fails with this; otherwise it is a ``QueryError``.
    """


class PartialResultError(QueryError):
    """A query returned only partial results and ``raise_on_partial`` was set.

    ``partial_error`` holds the service error; ``partial_data`` the tables returned so far.
    """

    def __init__(self, message: str, partial_error: Any = None, partial_data: Any = None) -> None:
        """Store the service error and partial tables alongside the message."""
        super().__init__(message)
        self.partial_error = partial_error
        self.partial_data = partial_data


class ConfigurationError(SecurityNotebookToolsError, ValueError):
    """Invalid arguments or missing configuration (e.g. an unset env var, a bad timeout)."""
