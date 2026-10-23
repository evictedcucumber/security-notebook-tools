"""Always-on file logging for the package.

All modules log to children of the ``security_notebook_tools`` logger. Every record at INFO and
above is appended to ``notebooks.log`` in the project root and nothing is printed to the notebook
cell. The project root is the first directory, walking up from the current working directory,
that contains ``.git`` or ``pyproject.toml``; if there is none, the working directory itself.
The path is resolved on every record, so ``os.chdir`` in a notebook is respected, and the file is
only created when the first record is written. Add ``notebooks.log`` to ``.gitignore``.

Each line is ``<UTC RFC 3339 timestamp> <LEVEL> <logger>: <message>``, for example::

    2026-10-23T14:03:12.345Z INFO security_notebook_tools.query: Running ...

Failures to open or write the file are ignored so logging never breaks a query.
"""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path
from typing import IO

PACKAGE_LOGGER = "security_notebook_tools"
LOG_FILE_NAME = "notebooks.log"
LOG_FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
_ROOT_MARKERS = (".git", "pyproject.toml")


def project_root(start: Path | None = None) -> Path:
    """Find the project root for ``start`` (default: the current working directory).

    Args:
        start: Directory to start from.

    Returns:
        The nearest ancestor (or ``start`` itself) containing ``.git`` or ``pyproject.toml``,
        or ``start`` if there is none.
    """
    start = (start or Path.cwd()).resolve()
    for directory in (start, *start.parents):
        if any((directory / marker).exists() for marker in _ROOT_MARKERS):
            return directory
    return start


def log_file_path() -> Path:
    """Return the path of ``notebooks.log`` in the current project root."""
    return project_root() / LOG_FILE_NAME


class _UtcFormatter(logging.Formatter):
    """Formatter that renders timestamps in UTC."""

    converter = time.gmtime


class _ProjectFileHandler(logging.Handler):
    """Append records to ``notebooks.log``, opening it lazily and following path changes."""

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level)
        self.setFormatter(_UtcFormatter(LOG_FORMAT, DATE_FORMAT))
        self._stream: IO[str] | None = None
        self._path: Path | None = None

    def emit(self, record: logging.LogRecord) -> None:
        """Append one formatted record, silently dropping it if the file is unusable."""
        try:
            path = log_file_path()
            if self._stream is None or path != self._path:
                self._close_stream()
                self._stream = path.open("a", encoding="utf-8")
                self._path = path
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self._close_stream()

    def _close_stream(self) -> None:
        stream, self._stream, self._path = self._stream, None, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()

    def close(self) -> None:
        """Close the open file, if any."""
        self._close_stream()
        super().close()


logger = logging.getLogger(PACKAGE_LOGGER)
logger.setLevel(logging.INFO)
file_handler = _ProjectFileHandler()
logger.addHandler(file_handler)


def close_log_file() -> None:
    """Close the log file's stream; it is reopened on the next record."""
    file_handler._close_stream()


def shorten(text: str, max_len: int = 200) -> str:
    """Collapse whitespace and truncate ``text`` to ``max_len`` characters for log messages."""
    flat = " ".join(text.split())
    return flat if len(flat) <= max_len else flat[: max_len - 1] + "…"
