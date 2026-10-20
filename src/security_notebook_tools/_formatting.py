"""Pure pandas helpers for tidying query results.

These are the public formatting functions re-exported by ``security_notebook_tools``. All of
them leave the input DataFrame untouched and return a new one, except ``summarize_df`` (returns
a summary table) and ``display_df`` (shows the frame and returns None), so they chain with
``DataFrame.pipe``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from typing import Any

import pandas as pd

from ._log import verbose_logging

logger = logging.getLogger("security_notebook_tools.formatting")


def _is_empty(value: Any) -> bool:
    """True for null values and empty strings, dicts, lists, tuples and sets."""
    if isinstance(value, dict | list | tuple | set | str):
        return len(value) == 0
    try:
        return bool(pd.isna(value))
    except TypeError, ValueError:
        return False


def drop_empty_columns(df: pd.DataFrame, *, verbose: bool = False) -> pd.DataFrame:
    """Drop columns whose values are all null, empty strings or empty dict/list/tuple/set.

    Args:
        df: The DataFrame to tidy.
        verbose: If true, print this call's logs to the cell output.

    Returns:
        A copy of ``df`` without the all-empty columns.

    Example:
        >>> df = drop_empty_columns(df)
    """
    with verbose_logging(verbose):
        keep = [c for c in df.columns if not df[c].map(_is_empty).all()]
        dropped = [c for c in df.columns if c not in keep]
        logger.info("Dropped %d empty column(s): %s", len(dropped), dropped)
        return df[keep].copy()


def _loads(value: Any) -> Any:
    """Parse ``value`` if it is a JSON object/array string; otherwise return it unchanged."""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "{[":
            try:
                return json.loads(text)
            except ValueError:
                return value
    return value


def parse_json_columns(
    df: pd.DataFrame, columns: Iterable[str] | None = None, *, verbose: bool = False
) -> pd.DataFrame:
    """Parse JSON-string cells (KQL ``dynamic`` values) into dicts and lists.

    Only strings that start with ``{`` or ``[`` are parsed; any cell that is not valid JSON is
    left as is.

    Args:
        df: The DataFrame to convert.
        columns: Columns to parse. Defaults to every object or string column.
        verbose: If true, print this call's logs to the cell output.

    Returns:
        A copy of ``df`` with the selected columns parsed.

    Example:
        >>> df = parse_json_columns(df, ["Properties"])
    """
    with verbose_logging(verbose):
        out = df.copy()
        cols = (
            list(columns)
            if columns is not None
            else [
                c
                for c in out
                if out[c].dtype == object or pd.api.types.is_string_dtype(out[c])  # noqa: E721
            ]
        )
        for col in cols:
            before = out[col]
            out[col] = before.map(_loads).astype(object)
            parsed = int((out[col].map(type) != before.map(type)).sum())
            if parsed:
                logger.info("Parsed %d JSON cell(s) in column %r", parsed, col)
        return out


def expand_column(
    df: pd.DataFrame, column: str, prefix: str | None = None, *, verbose: bool = False
) -> pd.DataFrame:
    """Flatten a dict column (or JSON-object strings) into ``<prefix>.<key>`` columns.

    Nested dicts are flattened too, with ``.`` between keys. Cells that are not dicts (or JSON
    objects) produce missing values, and a warning is logged if any non-empty ones were
    skipped. The original column is removed and the new columns are appended at the end; the
    index is preserved.

    Args:
        df: The DataFrame containing ``column``.
        column: Name of the column to expand.
        prefix: Prefix for the new column names. Defaults to ``column``; an empty string
            gives bare keys.
        verbose: If true, print this call's logs to the cell output.

    Returns:
        A new DataFrame with ``column`` replaced by the expanded columns.

    Raises:
        KeyError: ``column`` is not in ``df``.

    Example:
        >>> df = expand_column(df, "Properties")  # adds Properties.<key> columns
    """
    with verbose_logging(verbose):
        prefix = column if prefix is None else prefix
        values = df[column].map(_loads)
        records = [v if isinstance(v, dict) else {} for v in values]
        skipped = sum(1 for v in values if not isinstance(v, dict) and not _is_empty(v))
        if skipped:
            logger.warning("%d non-dict cell(s) in column %r were not expanded", skipped, column)
        flat = pd.json_normalize(records, sep=".") if records else pd.DataFrame()
        flat.index = df.index
        flat = flat.add_prefix(f"{prefix}." if prefix else "")
        logger.info("Expanded column %r into %d column(s)", column, len(flat.columns))
        return pd.concat([df.drop(columns=[column]), flat], axis=1)


def truncate_strings(
    df: pd.DataFrame, max_len: int = 100, *, verbose: bool = False
) -> pd.DataFrame:
    """Truncate string cells longer than ``max_len``, ending them with an ellipsis.

    Truncated cells are exactly ``max_len`` characters long, including the ``…``. Non-string
    cells are left alone.

    Args:
        df: The DataFrame to shorten.
        max_len: Maximum cell length in characters. Defaults to 100.
        verbose: If true, print this call's logs to the cell output.

    Returns:
        A copy of ``df`` with long strings truncated.

    Example:
        >>> df = truncate_strings(df, 80)
    """

    def cut(v: Any) -> Any:
        """Shorten ``v`` if it is a string longer than ``max_len``."""
        if isinstance(v, str) and len(v) > max_len:
            return v[: max(max_len - 1, 0)] + "…"
        return v

    with verbose_logging(verbose):
        out = df.copy()
        truncated = 0
        for col in out.columns:
            if out[col].dtype == object or pd.api.types.is_string_dtype(out[col]):
                truncated += int(
                    out[col].map(lambda v: isinstance(v, str) and len(v) > max_len).sum()
                )
                out[col] = out[col].map(cut)
        logger.info("Truncated %d cell(s) to %d characters", truncated, max_len)
        return out


def format_timestamps(
    df: pd.DataFrame,
    fmt: str = "%Y-%m-%d %H:%M:%S",
    tz: str | None = None,
    *,
    verbose: bool = False,
) -> pd.DataFrame:
    """Render datetime columns as strings using ``fmt``, optionally converted to a timezone.

    Naive datetimes are treated as UTC when ``tz`` is given. Without ``tz`` the values keep
    their own timezone. The formatted columns become strings.

    Args:
        df: The DataFrame to format.
        fmt: ``strftime`` format. Defaults to ``"%Y-%m-%d %H:%M:%S"``.
        tz: Timezone name to convert to (e.g. ``"Europe/London"``). Defaults to None (no
            conversion).
        verbose: If true, print this call's logs to the cell output.

    Returns:
        A copy of ``df`` with its datetime columns as strings.

    Example:
        >>> df = format_timestamps(df, tz="Europe/London")
    """
    with verbose_logging(verbose):
        out = df.copy()
        formatted = []
        for col in out.columns:
            if pd.api.types.is_datetime64_any_dtype(out[col]):
                s = out[col]
                if tz is not None:
                    if s.dt.tz is None:
                        s = s.dt.tz_localize("UTC")
                    s = s.dt.tz_convert(tz)
                out[col] = s.dt.strftime(fmt)
                formatted.append(col)
        logger.info("Formatted %d timestamp column(s) (tz=%s): %s", len(formatted), tz, formatted)
        return out


def reorder_columns(
    df: pd.DataFrame, first: Sequence[str], *, verbose: bool = False
) -> pd.DataFrame:
    """Move the ``first`` columns to the front; the others keep their order.

    Names in ``first`` that are not in ``df`` are ignored, with a warning logged.

    Args:
        df: The DataFrame to reorder.
        first: Column names to place first, in this order.
        verbose: If true, print this call's logs to the cell output.

    Returns:
        A copy of ``df`` with the columns reordered.

    Example:
        >>> df = reorder_columns(df, ["TimeGenerated", "UserPrincipalName"])
    """
    with verbose_logging(verbose):
        head = [c for c in first if c in df.columns]
        missing = [c for c in first if c not in df.columns]
        if missing:
            logger.warning("reorder_columns: ignoring missing column(s) %s", missing)
        rest = [c for c in df.columns if c not in head]
        return df[head + rest].copy()


def summarize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Summarise each column's dtype, null count and unique-value count.

    Args:
        df: The DataFrame to describe.

    Returns:
        A DataFrame indexed by column name with columns ``dtype``, ``nulls`` and ``unique``.
        Unhashable cells (dicts, lists) are counted by their ``repr``.

    Example:
        >>> summarize_df(df)
    """

    def nunique(s: pd.Series) -> int:
        """Unique count, falling back to ``repr`` for unhashable cells."""
        try:
            return int(s.nunique())
        except TypeError:  # unhashable cells (dict/list)
            return int(s.map(repr).nunique())

    return pd.DataFrame(
        {
            "dtype": [str(df[c].dtype) for c in df.columns],
            "nulls": [int(df[c].isna().sum()) for c in df.columns],
            "unique": [nunique(df[c]) for c in df.columns],
        },
        index=pd.Index(df.columns, name="column"),
    )


def display_df(df: pd.DataFrame, max_rows: int = 50, max_colwidth: int | None = 100) -> None:
    """Show the first ``max_rows`` rows of ``df`` in the notebook.

    Uses IPython's ``display`` for rich output when IPython is installed (the ``notebook``
    extra); otherwise prints a plain-text table.

    Args:
        df: The DataFrame to show.
        max_rows: Maximum number of rows to show. Defaults to 50.
        max_colwidth: Maximum characters per cell shown, or None for no limit. Defaults to 100.

    Example:
        >>> display_df(df, max_rows=20, max_colwidth=80)
    """
    with pd.option_context("display.max_rows", max_rows, "display.max_colwidth", max_colwidth):
        try:
            from IPython.display import display
        except ImportError:
            print(df.head(max_rows).to_string(max_colwidth=max_colwidth))
        else:
            display(df.head(max_rows))
