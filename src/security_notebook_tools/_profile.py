"""Query performance statistics for ``exec_query(..., profile=True)``."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import timedelta
from typing import Any

import pandas as pd

logger = logging.getLogger("security_notebook_tools.profile")

STATISTICS_ATTR = "_statistics"
PROFILE_ATTR = "profile"
_CAPTION = "Query profile"


def _in_ipython() -> bool:
    """Whether an IPython shell (a notebook kernel) is running, without importing IPython."""
    get_ipython = getattr(sys.modules.get("IPython"), "get_ipython", None)
    return get_ipython is not None and get_ipython() is not None


def adx_statistics(response: Any) -> dict | None:
    """The ``QueryResourceConsumption`` payload of an ADX response, or None if it has none."""
    tables = getattr(response, "tables", None)
    if not isinstance(tables, list | tuple):
        return None
    for table in tables:
        kind = getattr(table, "table_kind", None)
        if getattr(kind, "value", kind) != "QueryCompletionInformation":
            continue
        for row in table.rows:
            try:
                if row["EventTypeName"] == "QueryResourceConsumption":
                    payload = row["Payload"]
                    return json.loads(payload) if isinstance(payload, str) else payload
            except KeyError, IndexError, TypeError, ValueError:
                logger.debug("Could not read a QueryCompletionInformation row", exc_info=True)
    return None


def _flatten(value: Any, path: str = "") -> list[tuple[str, Any]]:
    """Every leaf of nested dicts and lists as ``(dotted.path[0], value)``."""
    if isinstance(value, dict):
        return [leaf for k, v in value.items() for leaf in _flatten(v, f"{path}.{k}".lstrip("."))]
    if isinstance(value, list | tuple):
        return [leaf for i, v in enumerate(value) for leaf in _flatten(v, f"{path}[{i}]")]
    return [(path, value)]


def profile_dict(statistics: dict | None, elapsed: float) -> dict[str, Any]:
    """The flat profile: the client-measured time, then every service statistic by dotted path."""
    profile: dict[str, Any] = {"client_elapsed_seconds": round(elapsed, 3)}
    try:
        profile.update(_flatten(statistics) if statistics else [])
    except Exception:
        logger.debug("Could not flatten the query statistics", exc_info=True)
    return profile


_TIMESPAN = re.compile(r"(?:(\d+)\.)?(\d+):(\d{2}):(\d{2})(?:\.(\d{1,7}))?")


def _parse_timespan(value: Any) -> timedelta | None:
    """A Kusto timespan string (``[d.]hh:mm:ss[.fffffff]``) as a ``timedelta``, else None."""
    match = _TIMESPAN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    days, hours, minutes, seconds, fraction = match.groups()
    return timedelta(
        days=int(days or 0),
        hours=int(hours),
        minutes=int(minutes),
        seconds=int(seconds),
        microseconds=int((fraction or "0").ljust(7, "0")) // 10,
    )


def _format_timespan(value: timedelta) -> str:
    """``value`` as ``[d.]hh:mm:ss[.fffffff]``."""
    seconds = value.seconds
    text = f"{seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02}"
    if value.days:
        text = f"{value.days}.{text}"
    if value.microseconds:
        text += f".{value.microseconds * 10:07}"
    return text


def total_profile(windows: list[dict[str, Any]], elapsed: float) -> dict[str, Any]:
    """Combine the per-window profiles of a batch into one flat dict.

    Numbers are summed and Kusto timespans are summed as durations, except metrics with "peak"
    in their name, which take the maximum. Bools and other text are left out.
    """
    total: dict[str, Any] = {"windows": len(windows), "client_elapsed_seconds": round(elapsed, 3)}
    skipped = {"window_start", "window_end", "client_elapsed_seconds"}
    names = dict.fromkeys(k for w in windows for k in w if k not in skipped)
    for name in names:
        values = [w[name] for w in windows if name in w]
        numbers = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
        spans = [t for v in values if (t := _parse_timespan(v)) is not None]
        peak = "peak" in name.lower()
        if numbers:
            total[name] = max(numbers) if peak else sum(numbers)
        elif spans:
            total[name] = _format_timespan(max(spans) if peak else sum(spans, timedelta()))
    return total


def profile_frame(profile: dict[str, Any]) -> pd.DataFrame:
    """A ``Metric`` / ``Value`` table of a flat profile dict (values shown as text)."""
    rows = list(profile.items())
    if len(rows) == 1:
        rows.append(("statistics", "the service returned no statistics"))
    return pd.DataFrame(rows, columns=["Metric", "Value"]).astype({"Value": "string"})


def show_profile(frame: pd.DataFrame, caption: str = _CAPTION) -> None:
    """Display ``frame`` in the notebook cell, or write it to stderr outside IPython."""
    try:
        if _in_ipython():
            from IPython.display import display

            display(frame.style.hide(axis="index").set_caption(caption))
        else:
            sys.stderr.write(f"{caption}\n{frame.to_string(index=False)}\n")
    except Exception:
        logger.debug("Could not show the query profile", exc_info=True)
