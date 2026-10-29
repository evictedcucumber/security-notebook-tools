"""IPython ``%%kql`` cell magic for quick queries, shown with ``format_dataframe`` or a chart.

Logging (``security_notebook_tools.magic``): a run is logged like a public function (entry,
result summary, one WARNING if the cell is rejected). The cell body is logged only as a
fingerprint, and the ``--timespan`` text never at INFO and above: only whether it was a duration
or an expression. ``--debug`` prints the run's log records, at DEBUG and above, below the cell,
unredacted and never to the file, including the raw ``--timespan`` text and its evaluated value
(see :func:`security_notebook_tools._log.debug_scope`).
"""

import logging
import os
import re
import warnings
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from IPython.core.error import UsageError
from IPython.core.magic import Magics, cell_magic, magics_class
from IPython.core.magic_arguments import argument, magic_arguments, parse_argstring

from ._errors import public_call
from ._exceptions import ConfigurationError
from ._format import format_dataframe
from ._log import dependency_call, fingerprint
from ._query import DEFAULT_TIMEOUT, exec_query
from ._render import VISUALIZATION_ATTR, check_spec, render_dataframe

if TYPE_CHECKING:
    from pandas.io.formats.style import Styler
    from plotly.graph_objects import Figure

logger = logging.getLogger("security_notebook_tools.magic")

_SHORTHAND = re.compile(r"(?:\d+[dhms])+", re.IGNORECASE)
_PART = re.compile(r"(\d+)([dhms])", re.IGNORECASE)
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_UNITS = {"d": "days", "h": "hours", "m": "minutes", "s": "seconds"}


def _timespan_kind(text: str) -> str:
    """``"duration"`` for a shorthand such as ``1d12h``, else ``"expression"`` (for logging)."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return "duration" if _SHORTHAND.fullmatch(text) else "expression"


def _parse_timespan(text: str, user_ns: dict[str, Any]) -> Any:
    """Turn the ``--timespan`` text into a value for ``exec_query``.

    Args:
        text: A duration shorthand such as ``1d12h``, or a Python expression.
        user_ns: Namespace the expression is evaluated in.

    Returns:
        A ``timedelta`` for shorthand, otherwise the expression's value (not validated here).

    Raises:
        UsageError: If the expression cannot be evaluated.
    """
    logger.debug("Raw --timespan text: %r", text)
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        # IPython keeps the shell-style quotes around an argument; they only group words.
        text = text[1:-1].strip()
    if _SHORTHAND.fullmatch(text):
        parts: dict[str, int] = {}
        for number, unit in _PART.findall(text):
            name = _UNITS[unit.lower()]
            parts[name] = parts.get(name, 0) + int(number)
        value = timedelta(**parts)
        logger.debug("Evaluated --timespan %r to %r", text, value)
        return value
    try:
        with dependency_call(logger, "timespan expression evaluation"):
            value = eval(text, user_ns)
        logger.debug("Evaluated --timespan %r to %r", text, value)
        return value
    except Exception as exc:
        raise UsageError(f"Could not evaluate --timespan {text!r}: {exc!r}") from exc


def _chart(df: Any, debug: bool) -> Figure | None:
    """The chart for a result with a render spec, or ``None`` to show the table.

    Warns once, naming the reason, if the chart cannot be drawn; never raises for that.
    """
    if not isinstance(df.attrs.get(VISUALIZATION_ATTR), dict):
        return None
    try:
        spec = check_spec(df)
        if spec[VISUALIZATION_ATTR].lower() == "table":
            return None  # `render table` asks for the table itself
        return render_dataframe(df, debug=debug)
    except ConfigurationError as exc:
        warnings.warn(f"{exc} Showing the table instead.", skip_file_prefixes=(_PACKAGE_DIR,))
        return None
    except Exception as exc:
        # A query you paid for must never lose its result to a chart bug (no values in the text).
        logger.debug("Chart drawing failed", exc_info=True)
        warnings.warn(
            f"Could not draw the chart: {type(exc).__name__}. Showing the table instead.",
            skip_file_prefixes=(_PACKAGE_DIR,),
        )
        return None


@magics_class
class KqlMagics(Magics):
    """Magics provided by security_notebook_tools."""

    def _wants_debug(self, line: str) -> bool:
        """Whether ``line`` has ``--debug``, so the scope covers the whole run (never raises)."""
        try:
            return bool(parse_argstring(self.kql, line).debug)
        except Exception, SystemExit:
            return False  # the real parse in kql() reports the problem

    @magic_arguments()
    @argument(
        "-t",
        "--timespan",
        help="Duration such as 1d, 12h, 30m, 1d12h, 90s, or a Python expression "
        "(a variable, a (start, end) tuple, timedelta(...)). Sentinel only.",
    )
    @argument(
        "--target", choices=["sentinel", "adx"], default="sentinel", help="Default: sentinel."
    )
    @argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Server-side timeout in seconds. Default: {DEFAULT_TIMEOUT}.",
    )
    @argument(
        "--no-cache",
        action="store_true",
        help="Run the query even if an identical result is cached.",
    )
    @argument(
        "--table",
        action="store_true",
        help="Show the table even if the query ends in `| render <charttype>`.",
    )
    @argument(
        "--debug",
        action="store_true",
        help="Also print this run's log records, at DEBUG and above, below the cell.",
    )
    @cell_magic
    def kql(self, line: str, cell: str) -> Styler | Figure:
        """Run the cell body as KQL with exec_query and display the result.

        The body is passed verbatim (no ``{}`` or ``$`` expansion). The result is shown with
        format_dataframe and not kept; call exec_query directly to work with the DataFrame.
        If the query ends in ``| render <charttype>`` the chart is shown instead (render_dataframe,
        needs Plotly); ``--table`` shows the table. If the chart cannot be drawn (Plotly missing,
        unsupported chart type) one warning says why and the table is shown.
        With ``--debug`` the run's log records, at DEBUG and above, are also printed below the
        cell.
        """
        with public_call(
            logger,
            "%%kql",
            rejected=(UsageError,),
            debug=self._wants_debug(line),
            kql=fingerprint(cell),
        ) as call:
            with dependency_call(logger, "IPython argument parsing"):
                args = parse_argstring(self.kql, line)
            if not cell.strip():
                raise UsageError("The cell body is empty: write a KQL query below %%kql.")
            user_ns = self.shell.user_ns
            timespan = None if args.timespan is None else _parse_timespan(args.timespan, user_ns)
            call.update(
                target=args.target,
                timeout=args.timeout,
                cache=not args.no_cache,
                timespan="none" if args.timespan is None else _timespan_kind(args.timespan),
            )
            df = exec_query(
                cell,
                timespan,
                target=args.target,
                timeout=args.timeout,
                cache=not args.no_cache,
                debug=args.debug,
            )
            if not args.table:
                figure = _chart(df, args.debug)
                if figure is not None:
                    call.update(chart=True)
                    return figure
            return format_dataframe(df, debug=args.debug)
