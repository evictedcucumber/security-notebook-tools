"""Microsoft Sentinel / Log Analytics queries via ``azure-monitor-query``.

Results are converted to DataFrames, with ``datetime`` columns as UTC-aware timestamps.
"""

from __future__ import annotations

import logging
import time
import warnings

import pandas as pd
from azure.core.exceptions import HttpResponseError
from azure.identity import AzureCliCredential
from azure.monitor.query import LogsQueryClient, LogsQueryStatus, LogsTable

from ._auth import get_credential
from ._config import WORKSPACE_ID_VAR, require
from ._exceptions import PartialResultError, QueryError
from ._log import shorten, verbose_logging
from ._timerange import Timespan

logger = logging.getLogger("security_notebook_tools.sentinel")


def _table_to_df(table: LogsTable) -> pd.DataFrame:
    """Convert a result table to a DataFrame, parsing datetime columns as UTC."""
    df = pd.DataFrame([list(row) for row in table.rows], columns=list(table.columns))
    for name, typ in zip(table.columns, table.columns_types, strict=False):
        if str(typ).lower() == "datetime":
            df[name] = pd.to_datetime(df[name], utc=True)
    return df


class SentinelClient:
    """Run KQL against a Log Analytics workspace (Microsoft Sentinel)."""

    def __init__(
        self,
        workspace_id: str | None = None,
        credential: AzureCliCredential | None = None,
        *,
        verbose: bool = False,
    ) -> None:
        """Create a client.

        Args:
            workspace_id: Log Analytics workspace ID. Defaults to ``$SENTINEL_WORKSPACE_ID``.
            credential: Credential to use. Defaults to :func:`get_credential`.
            verbose: Default for this client's methods; each method can override it.

        Raises:
            ConfigurationError: No workspace ID was given or set.
        """
        self.verbose = verbose
        with verbose_logging(verbose):
            self.workspace_id = require(workspace_id, WORKSPACE_ID_VAR, "workspace_id")
            self.credential = credential or get_credential()
            self._client = LogsQueryClient(self.credential)
            logger.info("Sentinel client ready for workspace %s", self.workspace_id)

    def query_tables(
        self,
        kql: str,
        timespan: Timespan | None = None,
        *,
        raise_on_partial: bool = False,
        server_timeout: int | None = None,
        verbose: bool | None = None,
    ) -> list[pd.DataFrame]:
        """Run ``kql`` and return every result table as a DataFrame.

        Args:
            kql: The KQL query.
            timespan: A ``timedelta``, a ``(start, end)`` / ``(start, timedelta)`` tuple, or
                ``None`` (no time restriction beyond the query itself).
            raise_on_partial: Raise instead of warning when the result is partial.
            server_timeout: Server-side timeout in seconds.
            verbose: Override the client's ``verbose`` setting for this call.

        Returns:
            One DataFrame per result table.

        Raises:
            QueryError: The service returned an error.
            PartialResultError: The result was partial and ``raise_on_partial`` is set.
        """
        with verbose_logging(self.verbose if verbose is None else verbose):
            logger.info("Running Sentinel query (timespan=%s): %s", timespan, shorten(kql))
            start = time.perf_counter()
            try:
                result = self._client.query_workspace(
                    self.workspace_id, kql, timespan=timespan, server_timeout=server_timeout
                )
            except HttpResponseError as exc:
                logger.error("Sentinel query failed: %s", exc.message or exc)
                raise QueryError(f"Sentinel query failed: {exc.message or exc}") from exc

            if result.status == LogsQueryStatus.SUCCESS:
                tables = result.tables
            elif result.status == LogsQueryStatus.PARTIAL:
                err = result.partial_error
                msg = f"Sentinel query returned partial results: {err}"
                logger.warning(msg)
                if raise_on_partial:
                    raise PartialResultError(
                        msg, partial_error=err, partial_data=result.partial_data
                    )
                warnings.warn(msg, stacklevel=2)
                tables = result.partial_data
            else:  # pragma: no cover - the SDK raises for failures, kept as a safeguard
                raise QueryError(f"Sentinel query failed: {getattr(result, 'error', result)}")

            dfs = [_table_to_df(t) for t in tables]
            logger.info(
                "Sentinel query returned %d table(s), rows=%s in %.2fs",
                len(dfs),
                [len(df) for df in dfs],
                time.perf_counter() - start,
            )
            return dfs

    def query(
        self,
        kql: str,
        timespan: Timespan | None = None,
        *,
        raise_on_partial: bool = False,
        server_timeout: int | None = None,
        verbose: bool | None = None,
    ) -> pd.DataFrame:
        """Run ``kql`` and return the first result table (empty DataFrame if none).

        Takes the same arguments and raises the same errors as :meth:`query_tables`.
        """
        tables = self.query_tables(
            kql,
            timespan,
            raise_on_partial=raise_on_partial,
            server_timeout=server_timeout,
            verbose=verbose,
        )
        return tables[0] if tables else pd.DataFrame()
