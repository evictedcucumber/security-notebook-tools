"""Azure Data Explorer (Kusto) queries via ``azure-kusto-data``.

Handles both KQL queries and management commands (``.show ...``). Access is read-only and cannot
reach the Sentinel data lake or Log Analytics workspaces (which bill per GB scanned): such cluster
URIs are rejected on construction and ``cluster()``, ``external_table()``, ``externaldata`` and
data-reading ``evaluate`` plugins are rejected in queries and commands.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

import pandas as pd
from azure.identity import AzureCliCredential, CredentialUnavailableError
from azure.kusto.data import ClientRequestProperties, KustoClient, KustoConnectionStringBuilder
from azure.kusto.data.exceptions import (
    KustoAuthenticationError,
    KustoClientError,
    KustoClosedError,
    KustoError,
    KustoNetworkError,
    KustoServiceError,
    KustoThrottlingError,
)
from azure.kusto.data.helpers import dataframe_from_result_table

from ._auth import get_credential
from ._config import DATABASE_VAR, require, resolve_cluster_uri, resolve_database, validate_database
from ._errors import (
    TRANSIENT_STATUS,
    describe_network_error,
    hints_for,
    kusto_error_codes,
    unexpected_errors,
)
from ._exceptions import (
    ConfigurationError,
    QueryConnectionError,
    QueryError,
    QueryThrottledError,
    QueryTransientError,
)
from ._format import KQL_TYPES_ATTR
from ._kql import reject_adx_constructs
from ._log import dependency_call, fingerprint, mask_name

logger = logging.getLogger("security_notebook_tools.adx")


def _table_to_df(table: Any) -> pd.DataFrame:
    """Convert a primary result table, recording each column's KQL type in ``df.attrs``."""
    df = dataframe_from_result_table(table)
    df.attrs[KQL_TYPES_ATTR] = {c.column_name: str(c.column_type).lower() for c in table.columns}
    return df


Properties = dict[str, Any] | ClientRequestProperties | None

# Only read-only management commands are allowed; everything else is refused client-side.
_SHOW_RE = re.compile(r"\.show(\s|$)", re.IGNORECASE)
# A server-side throttle can arrive as a KustoServiceError rather than a KustoThrottlingError.
_THROTTLED_RE = re.compile(r"TooManyRequests|throttl", re.IGNORECASE)
# Kusto's server-enforced read-only request option (the SDK has no constant for it).
_READONLY_OPTION = "request_readonly"


def _status(exc: KustoServiceError) -> int | None:
    """The HTTP status of a Kusto error's response, or None if absent or not an int."""
    status = getattr(getattr(exc, "http_response", None), "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


class ADXClient:
    """Run KQL and ``.show`` management commands against an ADX cluster, strictly read-only."""

    def __init__(
        self,
        cluster_uri: str | None = None,
        database: str | None = None,
        credential: AzureCliCredential | None = None,
    ) -> None:
        """Create a client.

        Args:
            cluster_uri: Cluster URI (``https`` only). Defaults to ``$ADX_CLUSTER_URI``.
            database: Default database. Defaults to ``$ADX_DATABASE``; may be left unset if
                every call passes a database.
            credential: Credential to use. Defaults to :func:`get_credential`.

        Raises:
            ConfigurationError: No cluster URI was given or set, or the cluster URI or
                database name is invalid.
        """
        self.cluster_uri = resolve_cluster_uri(cluster_uri)
        self.database = resolve_database(database)
        host = mask_name(urlsplit(self.cluster_uri).hostname)
        with unexpected_errors(logger, "ADX client setup"):
            self.credential = credential or get_credential()
            with dependency_call(logger, "Kusto client creation", host=host):
                kcsb = KustoConnectionStringBuilder.with_azure_token_credential(
                    self.cluster_uri, self.credential
                )
                self._client = KustoClient(kcsb)
        logger.info(
            "ADX client ready for %s (default database: %s)", host, mask_name(self.database)
        )

    def _db(self, database: str | None) -> str:
        """Resolve the database: argument, then client default, then ``$ADX_DATABASE``."""
        return validate_database(require(database or self.database, DATABASE_VAR, "database"))

    @staticmethod
    def _props(properties: Properties) -> ClientRequestProperties | None:
        """Turn an options dict into ``ClientRequestProperties`` (pass-through otherwise)."""
        if properties is None or isinstance(properties, ClientRequestProperties):
            return properties
        props = ClientRequestProperties()
        for key, value in properties.items():
            props.set_option(key, value)
        return props

    @staticmethod
    def _call(kind: str, execute: Any, db: str, text: str, props: Any) -> Any:
        """Send the request and map every known SDK failure to a ``QueryError`` that explains it.

        Anything not mapped here propagates; the caller turns it into an "unexpected" error.
        """
        try:
            with dependency_call(
                logger, f"Kusto {kind}", database=mask_name(db), kql=fingerprint(text)
            ):
                return execute(db, text, props)
        except CredentialUnavailableError as exc:
            message = (
                f"ADX {kind} failed: no Azure credential available ({exc}). Install the Azure "
                "CLI and run `az login`"
            )
            raise QueryError(message) from exc
        except KustoNetworkError as exc:
            message = (
                f"ADX {kind} failed: could not reach the service: {describe_network_error(exc)}"
            )
            raise QueryConnectionError(message) from exc
        except (KustoServiceError, KustoThrottlingError) as exc:
            # KustoApiError / KustoMultiApiError are KustoServiceErrors with error codes.
            status = _status(exc) if isinstance(exc, KustoServiceError) else None
            message = f"ADX {kind} failed: {exc}{hints_for(status, kusto_error_codes(exc))}"
            if isinstance(exc, KustoThrottlingError) or _THROTTLED_RE.search(str(exc)):
                error = QueryThrottledError
            elif status in TRANSIENT_STATUS:
                error = QueryTransientError
            else:
                error = QueryError
            raise error(message) from exc
        except KustoAuthenticationError as exc:
            message = (
                f"ADX {kind} failed: authentication failed: {exc} (run `az login`, and check "
                "that your account may access the cluster)"
            )
            raise QueryError(message) from exc
        except KustoClosedError as exc:
            message = f"ADX {kind} failed: the client was closed; create a new one"
            raise QueryError(message) from exc
        except KustoClientError as exc:
            message = f"ADX {kind} failed on the client side: {type(exc).__name__}: {exc}"
            raise QueryError(message) from exc
        except KustoError as exc:
            message = f"ADX {kind} failed: {type(exc).__name__}: {exc}"
            raise QueryError(message) from exc

    def _execute(
        self, kind: str, text: str, database: str | None, properties: Properties
    ) -> list[pd.DataFrame]:
        """Run a ``"query"`` or ``"command"`` and return its primary result tables.

        Every request is sent with ``request_readonly`` set, commands must be ``.show``, and
        text that could reach the data lake or other services is refused before any request.
        """
        if kind == "command" and not _SHOW_RE.match(text.strip()):
            raise ConfigurationError(
                "Only '.show' management commands are allowed; ADX access is read-only."
            )
        reject_adx_constructs(text)
        db = self._db(database)
        props = self._props(properties) or ClientRequestProperties()
        props.set_option(_READONLY_OPTION, True)
        execute = self._client.execute_mgmt if kind == "command" else self._client.execute_query
        logger.info("Running ADX %s on %s (kql=%s)", kind, mask_name(db), fingerprint(text))
        start = time.perf_counter()
        with unexpected_errors(logger, f"ADX {kind}"):
            response = self._call(kind, execute, db, text, props)
            with dependency_call(logger, "pandas DataFrame conversion", target="adx") as call:
                dfs = [_table_to_df(t) for t in response.primary_results]
                call["rows"] = [len(df) for df in dfs]
        logger.info(
            "ADX %s returned %d table(s), rows=%s in %.2fs",
            kind,
            len(dfs),
            [len(df) for df in dfs],
            time.perf_counter() - start,
        )
        return dfs

    def query_tables(
        self,
        kql: str,
        database: str | None = None,
        *,
        properties: Properties = None,
    ) -> list[pd.DataFrame]:
        """Run ``kql`` and return all primary result tables as DataFrames.

        Args:
            kql: The KQL query.
            database: Database to run against. Defaults to the client's database.
            properties: Request options, as a dict or ``ClientRequestProperties``.

        Raises:
            ConfigurationError: No database is available or it is invalid, or the KQL uses
                ``cluster()``, ``external_table()``, ``externaldata`` or a blocked ``evaluate``
                plugin (it could reach the Sentinel data lake or a workspace).
            QueryError: The service returned an error. The message adds hints for the HTTP
                status and the Kusto error code (``BadRequest_SyntaxError`` ...). An error
                nobody anticipated is logged at ERROR with its traceback and raised as a
                ``QueryError`` that chains the original.
            QueryConnectionError: The connection failed before a response arrived; it is a
                ``QueryTransientError``.
            QueryThrottledError: The service throttled the request (HTTP 429 or a throttling
                error). It is a ``QueryError``.
        """
        return self._execute("query", kql, database, properties)

    def query(
        self,
        kql: str,
        database: str | None = None,
        *,
        properties: Properties = None,
    ) -> pd.DataFrame:
        """Run ``kql`` and return the first primary result table (empty DataFrame if none).

        Takes the same arguments and raises the same errors as :meth:`query_tables`.
        """
        tables = self.query_tables(kql, database, properties=properties)
        return tables[0] if tables else pd.DataFrame()

    def show(
        self,
        command: str,
        database: str | None = None,
        *,
        properties: Properties = None,
    ) -> pd.DataFrame:
        """Run a ``.show`` management command (e.g. ``".show tables"``), return its first table.

        Any other management command raises ``ConfigurationError`` before a request is made.

        Takes the same arguments and raises the same errors as :meth:`query_tables`.
        """
        tables = self._execute("command", command, database, properties)
        return tables[0] if tables else pd.DataFrame()
