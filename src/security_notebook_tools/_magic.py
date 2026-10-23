"""IPython ``%%kql`` cell magic for quick queries, shown with ``format_dataframe``."""

import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from IPython.core.error import UsageError
from IPython.core.magic import Magics, cell_magic, magics_class
from IPython.core.magic_arguments import argument, magic_arguments, parse_argstring

from ._format import format_dataframe
from ._query import DEFAULT_TIMEOUT, exec_query

if TYPE_CHECKING:
    from pandas.io.formats.style import Styler

_SHORTHAND = re.compile(r"(?:\d+[dhms])+", re.IGNORECASE)
_PART = re.compile(r"(\d+)([dhms])", re.IGNORECASE)
_UNITS = {"d": "days", "h": "hours", "m": "minutes", "s": "seconds"}


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
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        # IPython keeps the shell-style quotes around an argument; they only group words.
        text = text[1:-1].strip()
    if _SHORTHAND.fullmatch(text):
        parts: dict[str, int] = {}
        for number, unit in _PART.findall(text):
            name = _UNITS[unit.lower()]
            parts[name] = parts.get(name, 0) + int(number)
        return timedelta(**parts)
    try:
        return eval(text, user_ns)
    except Exception as exc:
        raise UsageError(f"Could not evaluate --timespan {text!r}: {exc!r}") from exc


@magics_class
class KqlMagics(Magics):
    """Magics provided by security_notebook_tools."""

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
    @cell_magic
    def kql(self, line: str, cell: str) -> Styler:
        """Run the cell body as KQL with exec_query and display the result.

        The body is passed verbatim (no ``{}`` or ``$`` expansion). The result is shown with
        format_dataframe and not kept; call exec_query directly to work with the DataFrame.
        """
        args = parse_argstring(self.kql, line)
        if not cell.strip():
            raise UsageError("The cell body is empty: write a KQL query below %%kql.")
        user_ns = self.shell.user_ns
        timespan = None if args.timespan is None else _parse_timespan(args.timespan, user_ns)
        df = exec_query(
            cell,
            timespan,
            target=args.target,
            timeout=args.timeout,
            cache=not args.no_cache,
        )
        return format_dataframe(df)
