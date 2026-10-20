"""Package logging and the ``verbose=True`` switch for notebook output.

All modules log to children of the ``security_notebook_tools`` logger. By default nothing is
printed (a ``NullHandler`` is attached); configure ``logging`` yourself, call
:func:`enable_logging` once per session, or pass ``verbose=True`` to a function to send its
logs to the current cell's output.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager

PACKAGE_LOGGER = "security_notebook_tools"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"

logger = logging.getLogger(PACKAGE_LOGGER)
logger.addHandler(logging.NullHandler())


class _NotebookHandler(logging.StreamHandler):
    """Stream handler that writes to the current ``sys.stdout`` (Jupyter swaps it per cell)."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


def _has_notebook_handler() -> bool:
    """True if a notebook handler is already attached to the package logger."""
    return any(isinstance(h, _NotebookHandler) for h in logger.handlers)


def enable_logging(level: int | str = logging.INFO) -> None:
    """Print package logs to the notebook for the rest of the session.

    Logs go to the current cell's output, formatted as ``HH:MM:SS LEVEL logger: message``.
    Calling it again only changes the level. Undo with :func:`disable_logging`.

    Args:
        level: Minimum level to show, as a ``logging`` constant or name such as ``"DEBUG"``.
            Defaults to ``logging.INFO``.

    Example:
        >>> from security_notebook_tools import enable_logging, disable_logging
        >>> enable_logging("DEBUG")
        >>> disable_logging()
    """
    if not _has_notebook_handler():
        logger.addHandler(_NotebookHandler())
    logger.setLevel(level)


def disable_logging() -> None:
    """Stop printing package logs to the notebook (undo :func:`enable_logging`).

    Also resets the package logger's level to ``NOTSET``. Handlers you attached yourself are
    left alone.
    """
    for h in [h for h in logger.handlers if isinstance(h, _NotebookHandler)]:
        logger.removeHandler(h)
    logger.setLevel(logging.NOTSET)


@contextmanager
def verbose_logging(enabled: bool = True, level: int = logging.INFO) -> Iterator[None]:
    """While active, print package logs at ``level`` and above to the notebook cell.

    A no-op when ``enabled`` is false or logs are already being printed (nested calls,
    or :func:`enable_logging` was used), so messages are never duplicated.

    Args:
        enabled: Whether to print logs at all.
        level: Minimum level to show while active.

    Yields:
        None.
    """
    if not enabled or _has_notebook_handler():
        yield
        return
    handler = _NotebookHandler()
    previous_level = logger.level
    logger.addHandler(handler)
    if logger.getEffectiveLevel() > level:
        logger.setLevel(level)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def shorten(text: str, max_len: int = 200) -> str:
    """Collapse whitespace and truncate ``text`` to ``max_len`` characters for log messages."""
    flat = " ".join(text.split())
    return flat if len(flat) <= max_len else flat[: max_len - 1] + "…"
