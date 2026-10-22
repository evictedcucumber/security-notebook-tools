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

import pandas as pd
from azure.identity import AzureCliCredential
from azure.kusto.data import ClientRequestProperties, KustoClient, KustoConnectionStringBuilder
from azure.kusto.data.exceptions import KustoServiceError
from azure.kusto.data.helpers import dataframe_from_result_table

from ._auth import get_credential
from ._config import DATABASE_VAR, require, resolve_cluster_uri, resolve_database, validate_database
from ._exceptions import ConfigurationError, QueryError
from ._kql import reject_adx_constructs
from ._log import shorten, verbose_logging

logger = logging.getLogger("security_notebook_tools.adx")

Properties = dict[str, Any] | ClientRequestProperties | None

# Only read-only management commands are allowed; everything else is refused client-side.
_SHOW_RE = re.compile(r"\.show(\s|$)", re.IGNORECASE)
# Kusto's server-enforced read-only request option (the SDK has no constant for it).
_READONLY_OPTION = "request_readonly"


class ADXClient:
    """Run KQL and ``.show`` management commands against an ADX cluster, strictly read-only."""

    def __init__(
        self,
        cluster_uri: str | None = None,
        database: str | None = None,
        credential: AzureCliCredential | None = None,
        *,
        verbose: bool = False,
    ) -> None:
        """Create a client.

        Args:
            cluster_uri: Cluster URI (``https`` only). Defaults to ``$ADX_CLUSTER_URI``.
            database: Default database. Defaults to ``$ADX_DATABASE``; may be left unset if
                every call passes a database.
            credential: Credential to use. Defaults to :func:`get_credential`.
            verbose: Default for this client's methods; each method can override it.

        Raises:
            ConfigurationError: No cluster URI was given or set, or the cluster URI or
                database name is invalid.
        """
        self.verbose = verbose
        with verbose_logging(verbose):
            self.cluster_uri = resolve_cluster_uri(cluster_uri)
            self.database = resolve_database(database)
            self.credential = credential or get_credential()
            kcsb = KustoConnectionStringBuilder.with_azure_token_credential(
                self.cluster_uri, self.credential
            )
            self._client = KustoClient(kcsb)
            logger.info(
                "ADX client ready for %s (default database: %s)",
                self.cluster_uri,
                self.database or "none",
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
        logger.info("Running ADX %s on %s: %s", kind, db, shorten(text))
        start = time.perf_counter()
        try:
            response = execute(db, text, props)
        except KustoServiceError as exc:
            logger.error("ADX %s failed: %s", kind, exc)
            raise QueryError(f"ADX {kind} failed: {exc}") from exc
        dfs = [dataframe_from_result_table(t) for t in response.primary_results]
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
        verbose: bool | None = None,
    ) -> list[pd.DataFrame]:
        """Run ``kql`` and return all primary result tables as DataFrames.

        Args:
            kql: The KQL query.
            database: Database to run against. Defaults to the client's database.
            properties: Request options, as a dict or ``ClientRequestProperties``.
            verbose: Override the client's ``verbose`` setting for this call.

        Raises:
            ConfigurationError: No database is available or it is invalid, or the KQL uses
                ``cluster()``, ``external_table()``, ``externaldata`` or a blocked ``evaluate``
                plugin (it could reach the Sentinel data lake or a workspace).
            QueryError: The service returned an error.
        """
        with verbose_logging(self.verbose if verbose is None else verbose):
            return self._execute("query", kql, database, properties)

    def query(
        self,
        kql: str,
        database: str | None = None,
        *,
        properties: Properties = None,
        verbose: bool | None = None,
    ) -> pd.DataFrame:
        """Run ``kql`` and return the first primary result table (empty DataFrame if none).

        Takes the same arguments and raises the same errors as :meth:`query_tables`.
        """
        tables = self.query_tables(kql, database, properties=properties, verbose=verbose)
        return tables[0] if tables else pd.DataFrame()

    def show(
        self,
        command: str,
        database: str | None = None,
        *,
        properties: Properties = None,
        verbose: bool | None = None,
    ) -> pd.DataFrame:
        """Run a ``.show`` management command (e.g. ``".show tables"``), return its first table.

        Any other management command raises ``ConfigurationError`` before a request is made.

        Takes the same arguments and raises the same errors as :meth:`query_tables`.
        """
        with verbose_logging(self.verbose if verbose is None else verbose):
            tables = self._execute("command", command, database, properties)
        return tables[0] if tables else pd.DataFrame()
