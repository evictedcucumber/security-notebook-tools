"""Azure Data Explorer (Kusto) queries via ``azure-kusto-data``.

Handles both KQL queries and management commands (``.show ...``).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd
from azure.identity import AzureCliCredential
from azure.kusto.data import ClientRequestProperties, KustoClient, KustoConnectionStringBuilder
from azure.kusto.data.exceptions import KustoServiceError
from azure.kusto.data.helpers import dataframe_from_result_table

from ._auth import get_credential
from ._config import CLUSTER_URI_VAR, DATABASE_VAR, from_env, require
from ._exceptions import QueryError
from ._log import shorten, verbose_logging

logger = logging.getLogger("security_notebook_tools.adx")

Properties = dict[str, Any] | ClientRequestProperties | None


class ADXClient:
    """Run KQL and management commands against an ADX cluster."""

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
            cluster_uri: Cluster URI. Defaults to ``$ADX_CLUSTER_URI``.
            database: Default database. Defaults to ``$ADX_DATABASE``; may be left unset if
                every call passes a database.
            credential: Credential to use. Defaults to :func:`get_credential`.
            verbose: Default for this client's methods; each method can override it.

        Raises:
            ConfigurationError: No cluster URI was given or set.
        """
        self.verbose = verbose
        with verbose_logging(verbose):
            self.cluster_uri = require(cluster_uri, CLUSTER_URI_VAR, "cluster_uri")
            self.database = from_env(database, DATABASE_VAR)
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
        return require(database or self.database, DATABASE_VAR, "database")

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
        """Run a ``"query"`` or ``"command"`` and return its primary result tables."""
        db = self._db(database)
        execute = self._client.execute_mgmt if kind == "command" else self._client.execute_query
        logger.info("Running ADX %s on %s: %s", kind, db, shorten(text))
        start = time.perf_counter()
        try:
            response = execute(db, text, self._props(properties))
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
            ConfigurationError: No database is available.
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
        """Run a management command (e.g. ``".show tables"``) and return its first table.

        Takes the same arguments and raises the same errors as :meth:`query_tables`.
        """
        with verbose_logging(self.verbose if verbose is None else verbose):
            tables = self._execute("command", command, database, properties)
        return tables[0] if tables else pd.DataFrame()
