"""Cost guard for Sentinel queries: only Analytics-plan tables may be read.

Basic, Auxiliary and Sentinel data lake tables bill per GB scanned, and the Log Analytics query
API cannot refuse them, so this module enforces the rule client-side, before a query is sent:

1. :func:`get_policy` reads the workspace, its tables (with their plan) and its saved functions
   from Azure Resource Manager and caches the result for a few minutes.
2. :func:`check_sentinel_query` tokenizes the KQL (see :mod:`._kql`) and rejects it unless every
   tabular data source is a ``let``-bound name or an Analytics-plan table, and none of the known
   escape hatches (``search``, ``union *``, ``table()``, ``workspace()``, ``externaldata`` ...)
   appears.

Everything fails closed: if the tiers cannot be verified the query is not sent. This is a
client-side guard built on a tokenizer, not a KQL parser, so it is a safety net and not a
guarantee; see the README for the server-side controls that are.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from azure.core import PipelineClient
from azure.core.pipeline.policies import (
    BearerTokenCredentialPolicy,
    HeadersPolicy,
    RequestIdPolicy,
    RetryPolicy,
    UserAgentPolicy,
)
from azure.core.rest import HttpRequest
from azure.identity import AzureCliCredential

from ._exceptions import ConfigurationError, QueryError
from ._kql import (
    ALLOWED_PLUGINS,
    BLOCKED_PLUGINS,
    Token,
    arg_source_end,
    forbidden_construct,
    is_builtin_call,
    skip_params,
    tokenize,
)

logger = logging.getLogger("security_notebook_tools.tablepolicy")

ARM_HOST = "management.azure.com"
ARM_BASE_URL = f"https://{ARM_HOST}"
ARM_SCOPE = f"https://{ARM_HOST}/.default"
WORKSPACE_API_VERSION = "2025-07-01"
TABLES_API_VERSION = "2025-07-01"
SAVED_SEARCHES_API_VERSION = "2020-08-01"
CACHE_TTL_SECONDS = 300.0
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 30
_MAX_PAGES = 100
_ANALYTICS = "analytics"

_REASON = "Basic, Auxiliary and Sentinel data lake data bill per GB scanned"
_NOT_SENT = "The query was not sent."

# Keywords are case-sensitive, like the service: ``Print`` is a name, not the keyword.
# Statements that define things rather than produce a result; their start is not a data source.
_SKIP_STATEMENTS = frozenset({"let", "set", "declare", "alias", "pattern", "restrict"})
# Non-table things that can start a tabular expression.
_SOURCE_KEYWORDS = frozenset(
    {"print", "datatable", "range", "materialize", "toscalar", "view", "union", "evaluate"}
)
_LITERALS = frozenset({"true", "false", "null"})
# Operators that form a whole sub-query on their own (``fork (count)``); never table names.
_OPERATOR_WORDS = frozenset({"count", "getschema", "consume", "serialize"})
# The tabular operators allowed after a pipe (hyphenated names count as one operator). Every
# operator with a tabular operand is modelled below: join, lookup, union, invoke, evaluate,
# partition, fork, facet, mv-apply, make-graph. Anything else, including `graph-merge`,
# `search`, `find` and `macro-expand`, is rejected.
_OPERATORS = frozenset(
    """
    where filter project project-away project-keep project-rename project-reorder extend
    summarize count take limit top top-nested top-hitters sort order distinct join lookup union
    mv-expand mvexpand mv-apply mvapply parse parse-where parse-kv evaluate render sample
    sample-distinct serialize as getschema invoke make-series make-graph graph-match
    graph-to-table graph-shortest-paths graph-mark-components partition fork facet scan reduce
    consume
    """.split()  # noqa: SIM905 (a word list is easier to maintain than many quoted strings)
)
# Words that may be followed by "(" without being a function call. After them "(" opens a group
# or list rather than an argument list, so a lone identifier inside is checked as a possible
# table (``x in (T)``, ``join (T)``).
_PAREN_KEYWORDS = frozenset(
    {
        "in", "has_any", "has_all", "join", "lookup", "union", "fork", "on", "with", "declare",
        "to", "where", "filter", "and", "or", "not", "by", "of", "then", "else", "as", "between",
        "print", "project", "extend", "summarize", "distinct", "take", "limit", "top", "sort",
        "order", "parse", "scan", "restrict", "datatable", "materialize", "toscalar", "view",
    }
)  # fmt: skip
# Functions whose parenthesised argument may be a table: its first identifier is checked.
_TABULAR_ARG_CALLS = frozenset({"materialize", "toscalar", "view"})
# ``evaluate`` plugins whose first argument is a lookup table.
_LOOKUP_PLUGINS = frozenset({"ipv4_lookup", "ipv6_lookup", "geo_polygon_lookup", "geo_line_lookup"})


@dataclass(frozen=True)
class TablePolicy:
    """The verified table plans and function names of one workspace.

    Attributes:
        customer_id: The workspace GUID (``customerId``) reported by Azure Resource Manager.
        tables: Lower-case table name -> ``(name, plan)``. A missing plan is ``""``.
        functions: Lower-case saved-function alias -> alias.
    """

    customer_id: str
    tables: dict[str, tuple[str, str]] = field(default_factory=dict)
    functions: dict[str, str] = field(default_factory=dict)


def _is_analytics(plan: str) -> bool:
    """True for the Analytics plan (compared case-insensitively); anything else is forbidden."""
    return plan.lower() == _ANALYTICS


# --- Fetching ----------------------------------------------------------------------------------


def _arm_client(credential: AzureCliCredential) -> PipelineClient:
    """Build a pipeline client for Azure Resource Manager (bearer token, retries, no logging)."""
    return PipelineClient(
        ARM_BASE_URL,
        policies=[
            RequestIdPolicy(),
            HeadersPolicy(),
            UserAgentPolicy(),
            RetryPolicy(retry_total=3),
            BearerTokenCredentialPolicy(credential, ARM_SCOPE),
        ],
    )


def _get_json(client: PipelineClient, url: str) -> dict[str, Any]:
    """GET ``url`` and return the JSON object, with timeouts. Raises on any non-200 answer."""
    response = client.send_request(
        HttpRequest("GET", url),
        connection_timeout=_CONNECT_TIMEOUT,
        read_timeout=_READ_TIMEOUT,
    )
    if response.status_code != 200:
        raise QueryError(f"HTTP {response.status_code} from Azure Resource Manager")
    data = json.loads(response.content)
    if not isinstance(data, dict):
        raise QueryError("unexpected response from Azure Resource Manager (not a JSON object)")
    return data


def _check_next_link(link: str) -> str:
    """Return ``link`` if it points at Azure Resource Manager over https, else raise.

    The bearer token is attached to every request, so a ``nextLink`` must never lead elsewhere.
    """
    parts = urlsplit(link)
    if (
        parts.scheme != "https"
        or (parts.hostname or "").lower() != ARM_HOST
        or "@" in parts.netloc
        or parts.port not in (None, 443)
    ):
        raise QueryError("a nextLink points outside Azure Resource Manager")
    return link


def _get_all(client: PipelineClient, url: str) -> list[Any]:
    """Return the ``value`` items of every page of a list call, following ``nextLink``."""
    items: list[Any] = []
    for _ in range(_MAX_PAGES):
        page = _get_json(client, url)
        value = page.get("value")
        if not isinstance(value, list):
            raise QueryError("unexpected response from Azure Resource Manager (no 'value' list)")
        items.extend(value)
        next_link = page.get("nextLink")
        if not next_link:
            return items
        if not isinstance(next_link, str):
            raise QueryError("unexpected response from Azure Resource Manager ('nextLink')")
        url = _check_next_link(next_link)
    raise QueryError(f"more than {_MAX_PAGES} pages from Azure Resource Manager")


def _props(item: Any, what: str) -> dict[str, Any]:
    """Return ``item["properties"]`` or raise if ``item`` is not shaped like an ARM resource."""
    if not isinstance(item, dict):
        raise QueryError(f"unexpected {what} entry from Azure Resource Manager")
    props = item.get("properties", {})
    if not isinstance(props, dict):
        raise QueryError(f"unexpected {what} properties from Azure Resource Manager")
    return props


def _fetch_policy(resource_id: str, credential: AzureCliCredential) -> TablePolicy:
    """Read the workspace, its tables and its saved functions from ARM into a policy."""
    base = f"{ARM_BASE_URL}{resource_id}"
    with _arm_client(credential) as client:
        workspace = _get_json(client, f"{base}?api-version={WORKSPACE_API_VERSION}")
        tables = _get_all(client, f"{base}/tables?api-version={TABLES_API_VERSION}")
        searches = _get_all(
            client, f"{base}/savedSearches?api-version={SAVED_SEARCHES_API_VERSION}"
        )

    customer_id = _props(workspace, "workspace").get("customerId")
    if not isinstance(customer_id, str) or not customer_id:
        raise QueryError("the workspace has no customerId")

    table_map: dict[str, tuple[str, str]] = {}
    for item in tables:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name:
            raise QueryError("a table has no name")
        plan = _props(item, "table").get("plan")
        plan = plan if isinstance(plan, str) else ""
        old = table_map.get(name.lower())
        # A name listed twice keeps the stricter entry: anything but Analytics wins.
        if old is None or _is_analytics(old[1]):
            table_map[name.lower()] = (name, plan)
    if not table_map:
        raise QueryError("the workspace reports no tables")

    functions: dict[str, str] = {}
    for item in searches:
        alias = _props(item, "saved search").get("functionAlias")
        if isinstance(alias, str) and alias:
            functions[alias.lower()] = alias

    return TablePolicy(customer_id=customer_id, tables=table_map, functions=functions)


# --- Cache -------------------------------------------------------------------------------------

_cache: dict[tuple[str, str | None], tuple[float, TablePolicy]] = {}
_lock = threading.Lock()


def clear_policy_cache() -> None:
    """Forget every cached table policy so the next query fetches it again."""
    with _lock:
        logger.debug("Clearing %d cached table policy(ies)", len(_cache))
        _cache.clear()


def get_policy(
    resource_id: str,
    workspace_id: str,
    credential: AzureCliCredential,
    tenant_id: str | None = None,
) -> TablePolicy:
    """Return the verified table policy for a workspace, cached for five minutes.

    The cache is keyed by resource ID (case-insensitive) and tenant, and expires after
    :data:`CACHE_TTL_SECONDS` because table plans can change. The workspace's ``customerId`` is
    compared with ``workspace_id`` on every call, cached or not.

    Args:
        resource_id: ARM resource ID of the Log Analytics workspace (validated by the caller).
        workspace_id: The workspace GUID the query will be sent to.
        credential: Credential for ``https://management.azure.com/.default``.
        tenant_id: Tenant, used only as part of the cache key.

    Returns:
        The policy.

    Raises:
        ConfigurationError: The workspace behind ``resource_id`` has a different ID than
            ``workspace_id``.
        QueryError: The tiers could not be verified (network, authorisation or parse error).
            The query must not be sent.
    """
    key = (resource_id.lower(), tenant_id)
    with _lock:
        cached = _cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            logger.debug("Reusing cached table policy for %s", resource_id)
            policy = cached[1]
        else:
            logger.info("Reading table plans and functions for %s", resource_id)
            try:
                policy = _fetch_policy(resource_id, credential)
            except Exception as exc:
                logger.error("Could not read table plans: %s: %s", type(exc).__name__, exc)
                raise QueryError(
                    "Could not verify Sentinel table tiers "
                    f"({type(exc).__name__}: {str(exc)[:300]}), so the query was not sent. "
                    "Check `az login` and read access to the workspace's tables and saved "
                    "searches."
                ) from exc
            _cache[key] = (time.monotonic(), policy)
            logger.info(
                "Table policy ready: %d table(s), %d not on the Analytics plan, %d function(s)",
                len(policy.tables),
                sum(1 for _, plan in policy.tables.values() if not _is_analytics(plan)),
                len(policy.functions),
            )
    if policy.customer_id.lower() != workspace_id.lower():
        raise ConfigurationError(
            f"$SENTINEL_WORKSPACE_ID ({workspace_id}) does not match the workspace behind "
            f"$SENTINEL_WORKSPACE_RESOURCE_ID (customerId {policy.customer_id}). The two must "
            "name the same workspace. The query was not sent."
        )
    return policy


# --- Query analysis ----------------------------------------------------------------------------


def check_sentinel_query(kql: str, policy: TablePolicy) -> None:
    """Raise unless ``kql`` can only read Analytics-plan tables of the workspace.

    Rejected, with a message naming the culprit and the cost reason:

    * forbidden constructs: ``search``, ``find``, ``table()``, ``workspace()``, ``app()``,
      ``resource()``, ``adx()``, ``arg()``, ``cluster()``, ``database()``, ``external_table()``,
      ``externaldata`` and ``evaluate`` plugins that read external data;
    * ``union`` with a wildcard operand (``union *``, ``union Sec*``);
    * any identifier anywhere that is the name of a table not on the Analytics plan or of a saved
      function;
    * any tabular data source that is not a ``let``-bound name or an Analytics table: the start
      of each statement, the right-hand side of ``let``, ``union`` / ``join`` / ``lookup``
      operands, the first name inside ``(`` or ``{`` when followed by ``|``, ``)`` or ``}``
      (subqueries, ``materialize()``, ``toscalar()``, ``view()``) and ``evaluate`` lookup tables;
    * text that cannot be tokenized with certainty, unbalanced brackets, an empty query, or a
      query with no tabular statement.

    ``print``, ``datatable``, ``range`` and ordinary plugins such as ``bag_unpack`` are allowed.

    Args:
        kql: The KQL query.
        policy: The workspace's verified table policy.

    Raises:
        ConfigurationError: The query is rejected. It is not sent.
    """
    try:
        tokens = tokenize(kql)
        found = forbidden_construct(tokens, adx=False)
        if found:
            raise _reject(
                f"{found} is not allowed: it can read Basic, Auxiliary or Sentinel data lake "
                f"tables or other services, and {_REASON}."
            )
        _Analyzer(tokens, policy).run()
    except ConfigurationError as exc:
        logger.warning("Query rejected: %s", exc)
        raise


def _reject(message: str) -> ConfigurationError:
    """Build the error for a rejected query, with the common prefix and suffix."""
    return ConfigurationError(f"Cost guard: {message} {_NOT_SENT}")


class _Analyzer:
    """One pass over the tokens enforcing rules (b) and (c) of :func:`check_sentinel_query`."""

    def __init__(self, tokens: list[Token], policy: TablePolicy) -> None:
        self.t = tokens
        self.policy = policy
        # Names bound with `let` or as function parameters, one set per `{ }` body (innermost
        # last). A name is visible in its own scope and the ones inside it. A function defined
        # with `let f = (...) { ... }` also adds the marker "f(": only those can be called.
        self.scopes: list[set[str]] = [set()]
        self.as_names: list[tuple[int, str]] = []  # (bracket depth, name) from `| as name`
        self.stack: list[tuple[str, str]] = []  # open brackets: (char, kind)
        # `let` statements awaiting their `;`: (bracket depth, scope, name, defines a function)
        self.lets: list[tuple[int, int, str, bool]] = []
        self.noncall: set[int] = set()  # identifiers before "(" that are not function calls
        self.plugins: set[int] = set()  # evaluate plugin names (checked against ALLOWED_PLUGINS)
        self.pattern: set[int] = set()  # "(" of graph-match patterns: not sub-queries
        self.has_body = False
        # `arg("").Table` expressions (Azure Resource Graph, free): start index -> table index
        self.arg_sources = {
            i: end for i in range(len(tokens)) if (end := arg_source_end(tokens, i)) is not None
        }

    # -- helpers --

    def tok(self, i: int) -> Token | None:
        return self.t[i] if 0 <= i < len(self.t) else None

    def p(self, i: int, *chars: str) -> bool:
        t = self.tok(i)
        return t is not None and t.is_p(*chars)

    def is_bound(self, name: str) -> bool:
        return any(name in scope for scope in self.scopes) or any(
            name == n for _, n in self.as_names
        )

    def match(self, i: int) -> int:
        """Index of the bracket closing the one at ``i``."""
        depth = 0
        for j in range(i, len(self.t)):
            if self.t[j].is_p("(", "[", "{"):
                depth += 1
            elif self.t[j].is_p(")", "]", "}"):
                depth -= 1
                if depth == 0:
                    return j
        raise _reject("unbalanced brackets; the query cannot be analysed safely.")

    # -- names --

    def check_name(self, t: Token) -> None:
        """Reject restricted tables and functions, unknown names; accept bound/Analytics."""
        low = t.low
        entry = self.policy.tables.get(low)
        if entry is not None and not _is_analytics(entry[1]):
            plan = entry[1] or "unknown"
            raise _reject(
                f"table {entry[0]!r} is on the {plan} plan; only Analytics-plan tables can be "
                f"queried because {_REASON}."
            )
        if low in self.policy.functions:
            raise _reject(
                f"{self.policy.functions[low]!r} is a saved function or parser in this "
                "workspace. Functions are blocked because they can read tables whose plan "
                f"cannot be verified, and {_REASON}. Query the underlying Analytics tables "
                "instead."
            )
        if t.text in self.scopes_names():
            return
        if entry is not None:  # an Analytics table: table names are case-sensitive
            if entry[0] == t.text:
                return
            raise _reject(
                f"{t.text!r} does not match the case of the Analytics table {entry[0]!r}. "
                f"Table names are case-sensitive, so a different spelling might not be that "
                f"table; use {entry[0]!r}."
            )
        raise _reject(
            f"{t.text!r} is not a let-bound name or a known Analytics-plan table of this "
            f"workspace. Unknown names are rejected because they might resolve to data that "
            f"bills per GB scanned ({_REASON})."
        )

    def is_function(self, name: str) -> bool:
        """True if ``name`` was defined as a function with `let` and is still in scope."""
        return any(name + "(" in scope for scope in self.scopes)

    def scopes_names(self) -> set[str]:
        names = {n for _, n in self.as_names}
        for scope in self.scopes:
            names |= scope
        return names

    def forbidden_name(self, t: Token) -> None:
        """Rule (b): a non-Analytics table or function name is rejected wherever it appears."""
        entry = self.policy.tables.get(t.low)
        restricted = entry is not None and not _is_analytics(entry[1])
        if restricted or t.low in self.policy.functions:
            self.check_name(t)

    def check_source(self, j: int, *, allow_scalar: bool = False) -> None:
        """Rule (c): the token at ``j`` is in a tabular source position."""
        t = self.tok(j)
        if t is None:
            return
        if t.kind == "id":
            if t.is_kw(*_SOURCE_KEYWORDS) or j in self.arg_sources:
                return
            self.check_name(t)
        elif t.is_p("(") or (allow_scalar and (t.kind in ("num", "str") or t.is_p("-", "+"))):
            return  # a nested "(" is checked by the bracket rule when it is reached
        else:
            raise _reject(
                f"cannot tell what data source {t.text!r} is; the query cannot be analysed safely."
            )

    def check_call(self, i: int, t: Token) -> None:
        """Every ``name(`` must be a built-in KQL function, a keyword or a `let` function."""
        if i in self.noncall or i in self.plugins or t.is_kw(*_PAREN_KEYWORDS):
            return
        if i in self.arg_sources:  # arg("").Table: a source, not a function call
            return
        if t.quoted:
            raise _reject(f"a call through the escaped name ['{t.text}'] cannot be analysed.")
        if self.is_function(t.text):
            self.check_bare_arguments(i)
            return
        if is_builtin_call(t.text):
            return
        raise _reject(
            f"{t.text}() is not a built-in KQL function or a function defined with `let` in "
            "this query. Other functions (saved functions, the built-in Sentinel parsers "
            "such as _Im_* and _ASim_*, _GetWatchlist) are rejected because they can read "
            f"tables whose plan cannot be verified, and {_REASON}. If this is a scalar "
            "function that is missing from the built-in list, it is rejected too (fail closed)."
        )

    def check_bare_arguments(self, i: int) -> None:
        """Arguments of a `let` function: a leading name must be let-bound or an Analytics table.

        For every top-level argument, an optional ``name =`` is skipped and then an identifier
        that is not a call is checked as a table source, whatever follows it.
        """
        open_i = i + 1
        close = self.match(open_i)
        start, depth = open_i + 1, 0
        for k in range(open_i + 1, close + 1):
            tk = self.t[k]
            if tk.is_p("(", "[", "{"):
                depth += 1
            elif tk.is_p(")", "]", "}"):
                depth -= 1
            if (depth == 0 and tk.is_p(",")) or k == close:
                j = start
                named = self.tok(j) is not None and self.t[j].kind == "id"
                if named and self.p(j + 1, "=") and not self.p(j + 2, "="):
                    j += 2
                arg = self.tok(j)
                is_name = j < k and arg is not None and arg.kind == "id"
                if is_name and not self.p(j + 1, "(") and not arg.is_kw(*_LITERALS):
                    self.check_source(j)
                start = k + 1

    def statement_start(self, j: int, *, top: bool) -> None:
        t = self.tok(j)
        if t is None or t.is_p(";", ")", "}", "]"):
            return
        if t.is_kw(*_SKIP_STATEMENTS):
            return
        if top:
            self.has_body = True
        self.check_source(j, allow_scalar=True)

    # -- main loop --

    def run(self) -> None:
        if not self.t:
            raise _reject("the query is empty.")
        self.statement_start(0, top=True)
        for i, t in enumerate(self.t):
            if t.kind == "id":
                self.forbidden_name(t)
                prev = self.tok(i - 1)
                if self.p(i + 1, "("):
                    self.check_call(i, t)
                if not t.quoted and not (prev is not None and prev.is_p(".")):
                    self.keyword(i, t)
            elif t.is_p("(", "[", "{"):
                self.open_bracket(i, t)
            elif t.is_p(")", "]", "}"):
                self.close_bracket(t)
            elif t.is_p("|"):
                self.check_operator(i + 1)
            elif t.is_p(";"):
                self.commit_lets()
                depth = len(self.stack)
                self.as_names = [x for x in self.as_names if x[0] < depth]
                if not (self.stack and self.stack[-1][1] == "nostmt"):
                    self.statement_start(i + 1, top=not self.stack)
        if self.stack:
            raise _reject("unbalanced brackets; the query cannot be analysed safely.")
        self.commit_lets()
        if not self.has_body:
            raise _reject("the query has no tabular statement, only definitions.")

    def commit_lets(self) -> None:
        """Bind the names of `let` statements that end here (at this bracket depth or deeper)."""
        depth = len(self.stack)
        for d, scope, name, is_function in self.lets:
            if d >= depth:
                self.scopes[scope].add(name)
                if is_function:
                    self.scopes[scope].add(name + "(")
        self.lets = [x for x in self.lets if x[0] < depth]

    def open_bracket(self, i: int, t: Token) -> None:
        prev = self.tok(i - 1)
        kind = "group"
        if t.text == "(":
            if prev is not None and prev.kind == "id" and (i - 1) not in self.noncall:
                if prev.is_kw("with", "declare"):
                    kind = "nostmt"
                elif not (prev.is_kw(*_TABULAR_ARG_CALLS) or prev.is_kw(*_PAREN_KEYWORDS)):
                    kind = "call"
        elif t.text == "{":
            self.scopes.append(set())
            if prev is not None and prev.is_p(")"):
                self.bind_params(self.matching_open(i - 1), i - 1)
        self.stack.append((t.text, kind))
        if t.text == "[":
            return
        first, second = self.tok(i + 1), self.tok(i + 2)
        if i in self.pattern:
            return
        if first is None or first.kind != "id" or first.is_kw(*_LITERALS, *_OPERATOR_WORDS):
            return
        if second is None:
            return
        follows = ("|",) if kind == "call" else ("|", ")", "}")
        if second.is_p(*follows):
            self.check_source(i + 1)

    def matching_open(self, close: int) -> int:
        depth = 0
        for j in range(close, -1, -1):
            if self.t[j].is_p(")", "]", "}"):
                depth += 1
            elif self.t[j].is_p("(", "[", "{"):
                depth -= 1
                if depth == 0:
                    return j
        raise _reject("unbalanced brackets; the query cannot be analysed safely.")

    def bind_params(self, open_i: int, close_i: int) -> None:
        """Bind ``name:`` parameters of ``(a:int, t:(x:string)) { ... }`` in the body's scope."""
        depth = 0
        for j in range(open_i, close_i + 1):
            if self.t[j].is_p("(", "[", "{"):
                depth += 1
            elif self.t[j].is_p(")", "]", "}"):
                depth -= 1
            elif depth == 1 and self.t[j].kind == "id" and self.p(j + 1, ":"):
                self.scopes[-1].add(self.t[j].text)

    def close_bracket(self, t: Token) -> None:
        wanted = {")": "(", "]": "[", "}": "{"}[t.text]
        if not self.stack or self.stack[-1][0] != wanted:
            raise _reject("unbalanced brackets; the query cannot be analysed safely.")
        self.stack.pop()
        depth = len(self.stack)
        self.as_names = [x for x in self.as_names if x[0] <= depth]
        if t.text == "}":
            self.scopes.pop()
            self.lets = [x for x in self.lets if x[1] < len(self.scopes)]

    # -- keywords --

    def keyword(self, i: int, t: Token) -> None:
        """Handle words with their own syntax. Exact lower case only: `Union` is just a name."""
        word = t.text
        if word == "let":
            self.do_let(i)
        elif word == "union":
            self.do_union(i)
        elif word in ("join", "lookup"):
            self.do_join(i)
        elif word == "evaluate":
            self.do_evaluate(i)
        elif word == "invoke":
            self.do_invoke(i)
        elif word == "as":
            j, values = skip_params(self.t, i + 1)
            self.noncall |= values
            name = self.tok(j)
            if name is not None and name.kind == "id":
                self.as_names.append((len(self.stack), name.text))

    def do_let(self, i: int) -> None:
        name, eq = self.tok(i + 1), self.tok(i + 2)
        if name is None or name.kind != "id" or eq is None or not eq.is_p("="):
            raise _reject("a `let` statement could not be parsed.")
        j = i + 3
        t = self.tok(j)
        if t is None:
            raise _reject("a `let` statement has no value.")
        is_function = t.is_p("(") and self.p(self.match(j) + 1, "{")
        self.lets.append((len(self.stack), len(self.scopes) - 1, name.text, is_function))
        if t.kind == "id" and self.p(j + 1, "("):
            return  # a call: checked as one when the main loop reaches it
        if t.is_kw(*_LITERALS):
            return
        self.check_source(j, allow_scalar=True)

    def operand_params(self, i: int) -> int:
        """Skip ``kind=...`` / ``hint.x=...`` parameters after operator ``i``; return the index."""
        j, values = skip_params(self.t, i + 1)
        self.noncall |= values
        return j

    def do_union(self, i: int) -> None:
        j = self.operand_params(i)
        while True:
            t = self.tok(j)
            if t is None:
                raise _reject("`union` has no operands.")
            if t.is_p("*"):
                raise _reject(self._wildcard(""))
            if t.is_p("("):
                close = self.match(j)
                self.check_source(j + 1)
                depth = 0
                for k in range(j, close):
                    depth += self.t[k].is_p("(", "[", "{") - self.t[k].is_p(")", "]", "}")
                    if depth == 1 and self.t[k].is_p(","):
                        raise _reject("a `union` operand with several expressions is ambiguous.")
                j = close + 1
            elif t.kind == "id":
                if self.p(j + 1, "*"):
                    raise _reject(self._wildcard(t.text))
                self.check_source(j)
                if j in self.arg_sources:
                    j = self.arg_sources[j]  # skip to the table name
                j += 1
                if self.p(j, "("):
                    j = self.match(j) + 1  # a call of a let-bound function
                elif self.p(j, "."):
                    raise _reject("a dotted `union` operand is ambiguous.")
            else:
                raise _reject(f"cannot tell what `union` operand {t.text!r} is.")
            if self.p(j, ","):
                j += 1
                continue
            break
        end = self.tok(j)
        if end is not None and not end.is_p("|", ";", ")", "}"):
            raise _reject(f"cannot parse the `union` operands (unexpected {end.text!r}).")

    @staticmethod
    def _wildcard(prefix: str) -> str:
        return (
            f"`union` with a wildcard ({prefix}*) is not allowed: it matches tables of every "
            f"plan, and {_REASON}. Name the Analytics tables explicitly."
        )

    def do_join(self, i: int) -> None:
        j = self.operand_params(i)
        t = self.tok(j)
        if t is None:
            raise _reject(f"`{self.t[i].text}` has no operand.")
        if t.is_p("("):
            self.check_source(j + 1)
        elif t.kind == "id":
            self.check_source(j)
        else:
            raise _reject(f"cannot tell what `{self.t[i].text}` operand {t.text!r} is.")

    def do_evaluate(self, i: int) -> None:
        j = self.operand_params(i)
        plugin = self.tok(j)
        if plugin is None or plugin.kind != "id":
            raise _reject("`evaluate` is not followed by a plugin name.")
        if plugin.low in BLOCKED_PLUGINS:
            raise _reject(
                f"the `{plugin.low}` plugin is not allowed: it reads external data or calls "
                f"other services, and {_REASON}."
            )
        if plugin.quoted or plugin.text not in ALLOWED_PLUGINS:
            raise _reject(
                f"the `{plugin.text}` plugin is not on the list of plugins known to read only "
                f"their input, so it is rejected ({_REASON})."
            )
        self.plugins.add(j)
        if plugin.text in _LOOKUP_PLUGINS and self.p(j + 1, "("):
            self.check_source(j + 2)

    def do_invoke(self, i: int) -> None:
        target = self.tok(i + 1)
        if target is None or target.kind != "id" or not self.is_function(target.text):
            raise _reject(
                f"the target of `invoke` ({target.text if target else 'none'}) must be a function "
                "defined with `let` in this query (other functions can read tables whose plan "
                f"cannot be verified: {_REASON})."
            )

    # -- operators --

    def operator_at(self, j: int) -> tuple[str, int] | None:
        """The longest allowed operator name starting at ``j`` and the index after it."""
        t = self.tok(j)
        if t is None or t.kind != "id" or t.quoted:
            return None
        parts, ends, k = [t.text], [j + 1], j + 1
        while self.p(k, "-") and (n := self.tok(k + 1)) is not None:
            if n.kind != "id" or n.quoted:
                break
            parts.append(n.text)
            k += 2
            ends.append(k)
        for count in range(len(parts), 0, -1):
            name = "-".join(parts[:count])
            if name in _OPERATORS:
                return name, ends[count - 1]
        return None

    def check_operator(self, j: int) -> None:
        """The token after a pipe must be an allowed tabular operator; model its operands."""
        found = self.operator_at(j)
        if found is None:
            t = self.tok(j)
            shown = t.text if t is not None else "end of query"
            raise _reject(
                f"{shown!r} after a pipe is not a tabular operator the cost guard knows to be "
                "safe, so it is rejected (operators such as graph-merge, search, find and "
                f"macro-expand can read other data, and {_REASON})."
            )
        self.dispatch_operator(*found, j)

    def dispatch_operator(self, name: str, k: int, j: int) -> None:
        """Model the tabular operands of operator ``name`` (token ``j``, operands from ``k``)."""
        if self.p(k, "("):
            self.noncall.add(k - 1)  # `graph-match (a)-[e]->(b)`, `mv-expand (x)` ...
        if name == "partition":
            self.do_partition(k)
        elif name == "fork":
            self.do_fork(k)
        elif name == "facet":
            self.do_facet(k)
        elif name in ("mv-apply", "mvapply"):
            self.do_mv_apply(k)
        elif name == "make-graph":
            self.do_make_graph(k)
        elif name in ("graph-match", "graph-shortest-paths"):
            self.do_graph_match(k)

    def check_branch_start(self, j: int) -> None:
        """The first token of a sub-query without its own source: an operator or a table."""
        t = self.tok(j)
        if t is None or t.is_p(")", "}"):
            return
        found = self.operator_at(j)
        if found is not None:
            self.dispatch_operator(*found, j)
        else:
            self.check_source(j)

    def clause_end(self, k: int) -> int:
        """Index of the pipe, `;` or closing bracket that ends the clause starting at ``k``."""
        depth = 0
        for m in range(k, len(self.t)):
            tk = self.t[m]
            if tk.is_p("(", "[", "{"):
                depth += 1
            elif tk.is_p(")", "]", "}"):
                depth -= 1
                if depth < 0:
                    return m
            elif depth == 0 and tk.is_p("|", ";"):
                return m
        return len(self.t)

    def depth0(self, start: int, end: int) -> list[int]:
        """Indices in ``[start, end)`` that are not inside brackets."""
        depth, out = 0, []
        for m in range(start, end):
            tk = self.t[m]
            if tk.is_p(")", "]", "}"):
                depth -= 1
            if depth == 0:
                out.append(m)
            if tk.is_p("(", "[", "{"):
                depth += 1
        return out

    def do_partition(self, k: int) -> None:
        """``partition [hints] by col ( sub-query )``; the column is not a function call."""
        m, values = skip_params(self.t, k)
        col = self.tok(m + 1)
        by = self.tok(m)
        if by is None or by.text != "by" or col is None or col.kind != "id" or col.quoted:
            raise _reject("cannot parse `partition`: expected `by <column>` and a sub-query.")
        if not self.p(m + 2, "(", "{"):
            raise _reject("cannot parse `partition`: expected `by <column>` and a sub-query.")
        close = self.match(m + 2)
        after = self.tok(close + 1)
        if after is not None and not after.is_p("|", ";", ")", "}"):
            raise _reject("cannot parse `partition`: unexpected text after the sub-query.")
        if self.p(m + 3, ")", "}"):
            raise _reject("cannot parse `partition`: the sub-query is empty.")
        self.noncall |= values
        self.noncall.add(m + 1)
        self.check_branch_start(m + 3)

    def do_fork(self, k: int) -> None:
        """Check ``fork [name =] ( sub-query ) [name =] ( sub-query ) ...``."""
        j = k
        while True:
            if self.tok(j) is not None and self.t[j].kind == "id" and self.p(j + 1, "="):
                j += 2
            if not self.p(j, "("):
                raise _reject("cannot parse `fork`: expected a parenthesised sub-query.")
            self.check_branch_start(j + 1)
            j = self.match(j) + 1
            nxt = self.tok(j)
            if nxt is not None and (nxt.is_p("(") or (nxt.kind == "id" and self.p(j + 1, "="))):
                continue
            return

    def do_facet(self, k: int) -> None:
        """``facet by col[, col] with ( sub-query ) ...``: every `with` opens a sub-query."""
        for m in self.depth0(k, self.clause_end(k)):
            if self.t[m].is_kw("with"):
                if not self.p(m + 1, "("):
                    raise _reject("cannot parse `facet`: expected `with ( sub-query )`.")
                self.check_branch_start(m + 2)

    def do_mv_apply(self, k: int) -> None:
        """Check ``mv-apply [x =] expr on ( sub-query )``."""
        for m in self.depth0(k, self.clause_end(k)):
            if self.t[m].is_kw("on") and self.p(m + 1, "("):
                self.check_branch_start(m + 2)
                return
        raise _reject("cannot parse `mv-apply`: expected `on ( sub-query )`.")

    def do_make_graph(self, k: int) -> None:
        """``make-graph a --> b with Nodes on id, Nodes2 on id``: every table is a source."""
        end = self.clause_end(k)
        spots = self.depth0(k, end)
        withs = [m for m in spots if self.t[m].is_kw("with")]
        if not withs:
            return
        start = withs[0] + 1
        for m in [*[x for x in spots if x > withs[0] and self.t[x].is_p(",")], end]:
            first = self.tok(start)
            if first is None or start >= m:
                raise _reject("cannot parse `make-graph`: expected `<table> on <column>`.")
            self.check_source(start + 1 if first.is_p("(") else start)
            start = m + 1

    def do_graph_match(self, k: int) -> None:
        """Check ``graph-match`` and ``graph-shortest-paths`` patterns ``(a)-[e]->(b)``.

        The patterns are not sub-queries, so their parentheses are not table positions.
        """
        m, _ = skip_params(self.t, k)
        for x in self.depth0(m, self.clause_end(m)):
            tk = self.t[x]
            if tk.is_kw("where", "project"):
                return
            if tk.is_p("("):
                self.pattern.add(x)
