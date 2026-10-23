"""Portal-style time range handling for Sentinel queries.

Like the Log Analytics portal: if the KQL filters on time itself, no API time range is sent
("set in query"); otherwise the caller's ``timespan`` or a default of one day is used.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from ._exceptions import ConfigurationError
from ._kql import strip_noise
from ._log import describe_timespan

logger = logging.getLogger("security_notebook_tools.timerange")

Timespan = timedelta | tuple[datetime, datetime] | tuple[datetime, timedelta]
DEFAULT_TIMESPAN = timedelta(days=1)

_FILTER_START = re.compile(r"\b(?:where|filter)\b", re.IGNORECASE)
_TIME_REF = re.compile(
    r"\b(?:TimeGenerated|Timestamp)\b"
    r"|\b(?:ago|now|datetime|todatetime|startof(?:day|week|month|year)"
    r"|endof(?:day|week|month|year))\s*\(",
    re.IGNORECASE,
)


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as UTC-aware; a naive datetime is taken to be UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def validate_timespan(timespan: object) -> None:
    """Check that ``timespan`` has one of the accepted shapes.

    Accepted: ``None``; a ``timedelta`` greater than zero; a ``(start, end)`` tuple of
    datetimes with ``end`` after ``start``; or a ``(start, timedelta)`` tuple with a positive
    duration. A naive datetime is taken to be UTC when comparing.

    Args:
        timespan: The value to check.

    Raises:
        ConfigurationError: ``timespan`` has any other shape (a list, a ``date`` that is not
            a datetime, a tuple of another length), a duration is not positive, or the end is
            not after the start.
    """
    if timespan is None:
        return
    shapes = "a timedelta, (start, end) datetimes or (start, timedelta)"
    if isinstance(timespan, timedelta):
        if timespan <= timedelta(0):
            raise ConfigurationError(f"timespan must be positive, got {timespan}.")
        return
    if not isinstance(timespan, tuple) or len(timespan) != 2:
        raise ConfigurationError(f"timespan must be {shapes}, got {type(timespan).__name__}.")
    start, end = timespan
    if not isinstance(start, datetime):
        raise ConfigurationError(f"timespan must be {shapes}; the start is not a datetime.")
    if isinstance(end, timedelta):
        if end <= timedelta(0):
            raise ConfigurationError(f"timespan duration must be positive, got {end}.")
    elif isinstance(end, datetime):
        if _as_utc(end) <= _as_utc(start):
            raise ConfigurationError("timespan end must be after its start.")
    else:
        raise ConfigurationError(f"timespan must be {shapes}; the second item is neither.")


def validate_window_args(start: object, end: object, interval: object) -> None:
    """Check the types of ``exec_batch_query``'s ``start``, ``end`` and ``interval``.

    Args:
        start: Must be a ``datetime`` or a ``timedelta``.
        end: Must be a ``datetime`` or ``None``.
        interval: Must be a ``timedelta`` (its sign is checked by the caller).

    Raises:
        ConfigurationError: Any of them has the wrong type.
    """
    if not isinstance(start, datetime | timedelta):
        raise ConfigurationError(
            f"start must be a datetime or a timedelta, got {type(start).__name__}."
        )
    if end is not None and not isinstance(end, datetime):
        raise ConfigurationError(f"end must be a datetime or None, got {type(end).__name__}.")
    if not isinstance(interval, timedelta):
        raise ConfigurationError(f"interval must be a timedelta, got {type(interval).__name__}.")


_LET = re.compile(r"\blet\s+(\w+)\s*=\s*([^;]*)", re.IGNORECASE)


def has_time_filter(kql: str) -> bool:
    """True if a ``where``/``filter`` clause in ``kql`` references a time column or function.

    ``let`` variables assigned from a time function (``let start = ago(1d);``) count too.
    This is a text heuristic: filters inside stored functions or saved queries are not seen.
    """
    # Comments and string literals are removed so text like `where Msg == "TimeGenerated"`
    # doesn't count as a time filter.
    text = strip_noise(kql)
    time_vars = [name for name, value in _LET.findall(text) if _TIME_REF.search(value)]
    var_ref = (
        re.compile(r"\b(?:" + "|".join(map(re.escape, time_vars)) + r")\b") if time_vars else None
    )
    for segment in re.split(r"[|;]", text):
        match = _FILTER_START.search(segment)
        if not match:
            continue
        if _TIME_REF.search(segment, match.end()) or (
            var_ref and var_ref.search(segment, match.end())
        ):
            return True
    return False


def resolve_timespan(kql: str, timespan: Timespan | None) -> Timespan | None:
    """Pick the API time range for ``kql`` the way the portal does (see module docstring).

    Returns ``timespan`` if given (warning when the query also filters on time), else ``None``
    if the query filters on time, else the one-day default.
    """
    in_query = has_time_filter(kql)
    if timespan is not None:
        if in_query:
            logger.warning(
                "Query has its own time filter and timespan=%s was also given; "
                "both apply, so the narrower range wins.",
                describe_timespan(timespan),
            )
        return timespan
    if in_query:
        logger.info("Time range: set in query")
        return None
    logger.info("Time range: last %s (default; no time filter found in query)", DEFAULT_TIMESPAN)
    return DEFAULT_TIMESPAN
