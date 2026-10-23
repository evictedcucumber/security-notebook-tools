"""Microsoft Sentinel / Log Analytics queries via ``azure-monitor-query``.

Results are converted to DataFrames, with ``datetime`` columns as UTC-aware timestamps. Every
query passes the cost guard (:mod:`._tablepolicy`) first: only Analytics-plan tables may be read.
"""

from __future__ import annotations

import logging
import time
import warnings

import pandas as pd
from azure.core.exceptions import (
    AzureError,
    ClientAuthenticationError,
    DecodeError,
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
    TooManyRedirectsError,
)
from azure.identity import AzureCliCredential, CredentialUnavailableError
from azure.monitor.query import (
    LogsQueryClient,
    LogsQueryPartialResult,
    LogsQueryResult,
    LogsQueryStatus,
    LogsTable,
)

from ._auth import get_credential
from ._config import resolve_tenant_id, resolve_workspace_id, resolve_workspace_resource_id
from ._errors import (
    TRANSIENT_STATUS,
    azure_error_codes,
    describe_codes,
    describe_network_error,
    hints_for,
    unexpected_errors,
)
from ._exceptions import (
    PartialResultError,
    QueryConnectionError,
    QueryError,
    QueryThrottledError,
    QueryTransientError,
)
from ._format import KQL_TYPES_ATTR
from ._log import (
    dependency_call,
    describe_timespan,
    fingerprint,
    mask_id,
    redact_exception_text,
)
from ._tablepolicy import check_sentinel_query, get_policy
from ._timerange import Timespan

logger = logging.getLogger("security_notebook_tools.sentinel")

_TOO_MANY_REQUESTS = 429


def _table_to_df(table: LogsTable) -> pd.DataFrame:
    """Convert a result table to a DataFrame, parsing datetime columns as UTC.

    Each column's KQL type is recorded in ``df.attrs["kql_types"]`` for :func:`format_dataframe`.
    """
    df = pd.DataFrame([list(row) for row in table.rows], columns=list(table.columns))
    kql_types = {}
    for name, typ in zip(table.columns, table.columns_types, strict=False):
        kql_types[name] = str(typ).lower()
        if kql_types[name] == "datetime":
            df[name] = pd.to_datetime(df[name], utc=True)
    df.attrs[KQL_TYPES_ATTR] = kql_types
    return df


class SentinelClient:
    """Run KQL against a Log Analytics workspace (Microsoft Sentinel)."""

    def __init__(
        self,
        workspace_id: str | None = None,
        credential: AzureCliCredential | None = None,
        *,
        resource_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        """Create a client.

        Args:
            workspace_id: Log Analytics workspace ID. Defaults to ``$SENTINEL_WORKSPACE_ID``.
            credential: Credential to use. Defaults to :func:`get_credential`.
            resource_id: ARM resource ID of the same workspace, used to read table plans for
                the cost guard. Defaults to the ID built from
                ``$SENTINEL_SUBSCRIPTION_ID``, ``$SENTINEL_RESOURCE_GROUP`` and
                ``$SENTINEL_WORKSPACE_NAME``.
            tenant_id: Tenant, used for the credential and the policy cache. Defaults to
                ``$AZURE_TENANT_ID``.

        Raises:
            ConfigurationError: No workspace ID or resource ID was given or set, or one is
                malformed.
        """
        self.workspace_id = resolve_workspace_id(workspace_id)
        self.resource_id = resolve_workspace_resource_id(resource_id)
        self.tenant_id = resolve_tenant_id(tenant_id)
        with unexpected_errors(logger, "Sentinel client setup"):
            self.credential = credential or get_credential(self.tenant_id)
            with dependency_call(logger, "LogsQueryClient creation"):
                self._client = LogsQueryClient(self.credential)
        logger.info("Sentinel client ready for workspace %s", mask_id(self.workspace_id))
        logger.debug(
            "Sentinel client workspace_id=%s resource_id=%s", self.workspace_id, self.resource_id
        )

    def _run(
        self, kql: str, timespan: Timespan | None, server_timeout: int | None
    ) -> LogsQueryResult | LogsQueryPartialResult:
        """Send the query and map every known SDK failure to a ``QueryError`` that explains it.

        Anything not mapped here propagates; the caller turns it into an "unexpected" error.
        """
        try:
            with dependency_call(
                logger,
                "Log Analytics query",
                workspace=mask_id(self.workspace_id),
                kql=fingerprint(kql),
                timespan=describe_timespan(timespan),
                server_timeout=server_timeout,
            ) as call:
                result = self._client.query_workspace(
                    self.workspace_id, kql, timespan=timespan, server_timeout=server_timeout
                )
                call["status"] = getattr(result.status, "value", result.status)
                return result
        except CredentialUnavailableError as exc:
            message = (
                f"Sentinel query failed: no Azure credential available ({exc}). Install the "
                "Azure CLI and run `az login`"
            )
            raise QueryError(message) from exc
        except ClientAuthenticationError as exc:
            message = (
                f"Sentinel query failed: {exc.message or exc} (not authenticated: run "
                "`az login`; the token may have expired)"
            )
            raise QueryError(message) from exc
        except (ServiceRequestError, ServiceResponseError) as exc:
            message = (
                f"Sentinel query failed: could not reach the service: {describe_network_error(exc)}"
            )
            raise QueryConnectionError(message) from exc
        except HttpResponseError as exc:
            status = exc.status_code
            message = f"Sentinel query failed: {exc.message or exc}"
            if isinstance(exc, DecodeError):
                message += " (the service sent a response that could not be read; retry)"
            elif isinstance(exc, TooManyRedirectsError):
                message += " (too many redirects: check the endpoint and any proxy)"
            else:
                message += hints_for(status, azure_error_codes(exc))
            if status == _TOO_MANY_REQUESTS:
                error = QueryThrottledError
            elif status in TRANSIENT_STATUS:
                error = QueryTransientError
            else:
                error = QueryError
            raise error(message) from exc
        except AzureError as exc:
            message = f"Sentinel query failed: {type(exc).__name__}: {exc}"
            raise QueryError(message) from exc

    def query_tables(
        self,
        kql: str,
        timespan: Timespan | None = None,
        *,
        raise_on_partial: bool = False,
        server_timeout: int | None = None,
    ) -> list[pd.DataFrame]:
        """Run ``kql`` and return every result table as a DataFrame.

        Before anything is sent, the query is checked against the workspace's table plans: it
        may only read Analytics-plan tables (see :func:`check_sentinel_query`).

        Args:
            kql: The KQL query.
            timespan: A ``timedelta``, a ``(start, end)`` / ``(start, timedelta)`` tuple, or
                ``None`` (no time restriction beyond the query itself).
            raise_on_partial: Raise instead of warning when the result is partial.
            server_timeout: Server-side timeout in seconds.

        Returns:
            One DataFrame per result table.

        Raises:
            ConfigurationError: The query is rejected by the cost guard (it could read a table
                that is not on the Analytics plan), or the workspace ID does not match the
                resource ID.
            QueryError: The table plans could not be verified, or the service returned an error.
                The message adds hints for the HTTP status and the service error code
                (``SyntaxError``, ``PathNotFoundError``, ``InsufficientAccessError`` ...), and
                says to run `az login` for authentication problems. An error nobody anticipated
                is logged at ERROR with its traceback and raised as a ``QueryError`` that
                chains the original.
            QueryTransientError: A failure that may succeed if retried (HTTP 502/503). It is a
                ``QueryError``.
            QueryThrottledError: The service throttled the request (HTTP 429). It is a
                ``QueryTransientError``.
            QueryConnectionError: The connection failed before a response arrived (reset,
                refused, timed out, DNS); the message explains the cause. It is a
                ``QueryTransientError``.
            PartialResultError: The result was partial and ``raise_on_partial`` is set.
        """
        policy = get_policy(self.resource_id, self.workspace_id, self.credential, self.tenant_id)
        check_sentinel_query(kql, policy)
        logger.info(
            "Running Sentinel query (timespan=%s, kql=%s)",
            describe_timespan(timespan),
            fingerprint(kql),
        )
        logger.debug(
            "Sentinel query text (workspace_id=%s, timespan=%r):\n%s",
            self.workspace_id,
            timespan,
            kql,
        )
        start = time.perf_counter()
        with unexpected_errors(logger, "Sentinel query"):
            result = self._run(kql, timespan, server_timeout)

        if result.status == LogsQueryStatus.SUCCESS:
            tables = result.tables
        elif result.status == LogsQueryStatus.PARTIAL:
            err = result.partial_error
            hint = describe_codes([c for c in [getattr(err, "code", None)] if c])
            msg = f"Sentinel query returned partial results: {err}"
            if hint:
                msg += f" ({hint})"
            logger.warning(
                "Sentinel query returned partial results: %s%s",
                redact_exception_text(str(err)),
                f" ({hint})" if hint else "",
            )
            if raise_on_partial:
                raise PartialResultError(msg, partial_error=err, partial_data=result.partial_data)
            warnings.warn(msg, stacklevel=2)
            tables = result.partial_data
        else:  # pragma: no cover - the SDK raises for failures, kept as a safeguard
            raise QueryError(f"Sentinel query failed: {getattr(result, 'error', result)}")

        with (
            unexpected_errors(logger, "converting the Sentinel result to a DataFrame"),
            dependency_call(logger, "pandas DataFrame conversion", target="sentinel") as call,
        ):
            dfs = [_table_to_df(t) for t in tables]
            call["rows"] = [len(df) for df in dfs]
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
    ) -> pd.DataFrame:
        """Run ``kql`` and return the first result table (empty DataFrame if none).

        Takes the same arguments and raises the same errors as :meth:`query_tables`.
        """
        tables = self.query_tables(
            kql,
            timespan,
            raise_on_partial=raise_on_partial,
            server_timeout=server_timeout,
        )
        return tables[0] if tables else pd.DataFrame()
