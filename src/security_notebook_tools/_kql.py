"""A small KQL tokenizer and the checks shared by the Sentinel and ADX cost guards.

This is not a KQL parser. It splits text into identifiers, numbers, string literals and
punctuation, with comments removed, so the guards can reason about code without being fooled by
text inside strings or comments. It is deliberately strict: anything it cannot tokenize with
certainty (an unterminated string, a non-ASCII character outside a string, a newline inside a
quoted string) raises :class:`ConfigurationError` rather than guessing, because a string literal
the guard misreads could hide a table name from it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import NamedTuple

from ._exceptions import ConfigurationError

# Order matters: comments and strings are consumed before identifiers and punctuation so that
# their contents are never scanned as code. Verbatim strings (@"..." / @'...') double the quote
# instead of using backslash escapes; multi-line strings (``` ... ``` and ~~~ ... ~~~) have no
# escapes at all. Plain quoted strings may not span lines. ``bad`` matches an opening quote that
# no string alternative could close, and ``@h"`` (an ordering of the obfuscated-string prefix
# whose escape rules are unclear). The ``h`` prefix of ``h"..."`` is read as an identifier
# followed by an ordinary string, which is equivalent for scanning. A number takes only a valid
# literal suffix (``1d``, ``10min``, ``0x1F``, ``1e3``); ``tokenize`` refuses an identifier
# glued to a number (``10AuxT``), because the service might read it differently.
# Control characters and unusual line breaks are refused before scanning (see ``tokenize``).
_UNITS = (
    r"(?:microseconds?|micros?|milliseconds?|millis?|ticks?|ms|seconds?|secs?|minutes?|min"
    r"|hours?|hrs?|days?|d|h|m|s)"
)
_TOKEN = re.compile(
    r"(?P<ws>[ \t\r\n]+)"
    r"|(?P<comment>//[^\r\n]*)"
    r"|(?P<ml>(?s:```.*?```|~~~.*?~~~))"
    r"|(?P<str>@\"(?:[^\"\r\n]|\"\")*\"|@'(?:[^'\r\n]|'')*'"
    r"|\"(?:\\[^\r\n]|[^\"\\\r\n])*\"|'(?:\\[^\r\n]|[^'\\\r\n])*')"
    r"|(?P<bad>@?[\"']|@[hH][\"']|```|~~~)"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)"
    rf"|(?P<num>0[xX][0-9A-Fa-f]+|[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?{_UNITS}?)"
    r"|(?P<punct>[\x21-\x7e])"
)
# Refused anywhere in the text, strings and comments included: C0 and C1 controls other than
# tab, CR and LF (a lone CR/LF-like character could end a comment for the service but not
# for us), DEL, and the Unicode line and paragraph separators.
_REFUSED = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u2028\u2029]")

# Calls that reach other workspaces, services or the Sentinel data lake from Log Analytics KQL.
SENTINEL_CALLS = frozenset(
    {
        "table",
        "workspace",
        "app",
        "resource",
        "adx",
        "arg",
        "cluster",
        "database",
        "external_table",
    }
)
# Operators that scan tables of every plan (``search *``, ``find in (*)``).
SENTINEL_OPERATORS = frozenset({"search", "find"})
# Calls that ADX must not make: they reach the Sentinel data lake or Log Analytics workspaces.
ADX_CALLS = frozenset({"cluster", "external_table"})
# ``evaluate`` plugins that read data from outside the query's own tables. Blocked wherever the
# name appears, for both targets. (``infer_storage_schema`` reads external storage.)
BLOCKED_PLUGINS = frozenset(
    {
        "sql_request",
        "cosmosdb_sql_request",
        "http_request",
        "http_request_post",
        "mysql_request",
        "postgresql_request",
        "azure_digital_twins_query_request",
        "infer_storage_schema",
        "ai_embeddings",
        "ai_chat_completion",
        "ai_chat_completion_prompt",
    }
)
# Plugins that can run code or fetch external artifacts; blocked only as the plugin of an
# ``evaluate`` (``r`` and ``python`` are also ordinary column names).
BLOCKED_EVALUATE_PLUGINS = frozenset({"python", "r"})
_EXTERNALDATA = "externaldata"

# Ordinary ``evaluate`` plugins that read only their input. Any other plugin is refused by the
# Sentinel guard. (Case-sensitive, like the service.)
ALLOWED_PLUGINS = frozenset(
    """
    bag_unpack pivot narrow preview autocluster basket diffpatterns diffpatterns_text
    funnel_sequence funnel_sequence_completion new_activity_metrics activity_counts_metrics
    activity_engagement activity_metrics sequence_detect session_count sliding_window_counts
    rolling_percentile rows_near dcount_intersect schema_merge ipv4_lookup ipv6_lookup
    geo_polygon_lookup geo_line_lookup
    """.split()  # noqa: SIM905 (a word list is easier to maintain than many quoted strings)
)
# Built-in scalar, aggregate and window functions that may be called in Sentinel queries, by
# exact name. A call to any other name must be a function bound with ``let`` in the query. In
# particular the built-in Sentinel parsers (``_Im_*``, ``_ASim_*``), ``_GetWatchlist`` and saved
# functions are not on this list. Case-sensitive, like the service.
BUILTIN_CALLS = frozenset(
    """
    ago now datetime time timespan dynamic guid int long real double decimal bool boolean string
    todatetime totimespan todynamic toguid toint tolong toreal todouble todecimal tobool
    toboolean tostring tohex tolower toupper bin bin_at bin_auto floor ceiling round abs sign
    sqrt cbrt pow exp exp2 exp10 log log2 log10 sin cos tan asin acos atan atan2 sinh cosh tanh
    degrees radians pi gamma loggamma isinf isfinite isnan rand new_guid min_of max_of
    startofday startofweek startofmonth startofyear endofday endofweek endofmonth endofyear
    dayofweek dayofmonth dayofyear hourofday monthofyear weekofyear getyear getmonth
    week_of_year make_datetime make_timespan unixtime_seconds_todatetime
    unixtime_milliseconds_todatetime unixtime_microseconds_todatetime
    unixtime_nanoseconds_todatetime iff iif case coalesce ifnull isnull isnotnull isempty
    isnotempty notempty notnull isascii isutf8 strcat strcat_array strcat_delim strlen
    string_size substring split replace_string replace_strings replace_regex extract
    extract_all extractjson trim trim_start trim_end reverse strrep strcmp indexof
    indexof_regex countof translate has_ipv4 has_ipv4_prefix has_any_ipv4 has_any_ipv4_prefix
    has_any_index typeof gettype column_ifexists columnifexists pack pack_all pack_array
    pack_dictionary repeat zip range jaccard_index current_principal current_principal_details
    current_principal_is_member_of current_database ingestion_time binary_and binary_or
    binary_xor binary_not binary_shift_left binary_shift_right bitset_count_ones treepath
    welch_test in_range query_parameters assert hash
    array_concat array_extract array_iff array_iif array_index_of array_length array_reverse
    array_rotate_left array_rotate_right array_shift_left array_shift_right array_slice
    array_sort_asc array_sort_desc array_split array_strcat array_sum
    bag_has_key bag_keys bag_merge bag_pack bag_pack_columns bag_remove_keys bag_set_key
    bag_zip
    set_difference set_has_element set_intersect set_union
    base64_decode_toarray base64_decode_tostring base64_decodestring base64_encode_fromarray
    base64_encode_tostring base64_encodestring
    datetime_add datetime_diff datetime_list_timezones datetime_local_to_utc datetime_part
    datetime_utc_to_local
    format_bytes format_datetime format_ipv4 format_ipv4_mask format_timespan
    hash_combine hash_many hash_md5 hash_sha1 hash_sha256 hash_xxhash64
    url_decode url_encode url_encode_component
    ipv4_compare ipv4_is_in_any_range ipv4_is_in_range ipv4_is_match ipv4_is_private
    ipv4_netmask_suffix ipv4_range_to_cidr_list
    ipv6_compare ipv6_is_in_any_range ipv6_is_in_range ipv6_is_match
    parse_command_line parse_csv parse_ipv4 parse_ipv4_mask parse_ipv6 parse_ipv6_mask
    parse_json parse_path parse_url parse_urlquery parse_user_agent parse_version parse_xml
    geo_azimuth geo_closest_point_on_line geo_closest_point_on_polygon geo_distance_2points
    geo_distance_point_to_line geo_distance_point_to_polygon geo_from_wkt
    geo_geohash_neighbors geo_geohash_to_central_point geo_geohash_to_polygon
    geo_h3cell_children geo_h3cell_neighbors geo_h3cell_parent geo_h3cell_rings
    geo_h3cell_to_central_point geo_h3cell_to_polygon geo_info_from_ip_address
    geo_intersection_2lines geo_intersection_2polygons geo_intersection_line_with_polygon
    geo_intersects_2lines geo_intersects_2polygons geo_line_buffer geo_line_centroid
    geo_line_densify geo_line_interpolate_point geo_line_length geo_line_locate_point
    geo_line_simplify geo_line_to_s2cells geo_point_buffer geo_point_in_circle
    geo_point_in_polygon geo_point_to_geohash geo_point_to_h3cell geo_point_to_s2cell
    geo_polygon_area geo_polygon_buffer geo_polygon_centroid geo_polygon_densify
    geo_polygon_perimeter geo_polygon_simplify geo_polygon_to_h3cells geo_polygon_to_s2cells
    geo_s2cell_neighbors geo_s2cell_to_central_point geo_s2cell_to_polygon
    geo_simplify_polygons_array geo_to_wkt geo_union_lines_array geo_union_polygons_array
    series_abs series_acos series_add series_asin series_atan series_ceiling
    series_cosine_similarity series_cos series_decompose series_decompose_anomalies
    series_decompose_forecast series_divide series_dot_product series_equals series_exp
    series_fft series_fill_backward series_fill_const series_fill_forward series_fill_linear
    series_fir series_fit_2lines series_fit_2lines_dynamic series_fit_line
    series_fit_line_dynamic series_fit_poly series_floor series_greater series_greater_equals
    series_ifft series_iir series_less series_less_equals series_log series_magnitude
    series_multiply series_not_equals series_outliers series_pearson_correlation
    series_periods_detect series_periods_validate series_product series_seasonal series_sign
    series_sin series_stats series_stats_dynamic series_subtract series_sum series_tan
    count countif dcount dcountif sum sumif avg avgif min max minif maxif arg_max arg_min any
    anyif take_any take_anyif make_list make_list_if make_list_with_nulls make_set make_set_if
    make_bag make_bag_if percentile percentiles percentiles_array percentilew percentilesw
    percentilesw_array stdev stdevif stdevp variance varianceif variancep hll hll_if hll_merge
    dcount_hll tdigest tdigest_merge merge_tdigest percentile_tdigest percentrank_tdigest
    percentile_array_tdigest count_distinct count_distinctif buildschema covariance
    correlation row_number row_cumsum row_rank row_rank_min row_rank_dense row_window_session
    prev next rank rank_tdigest
    """.split()  # noqa: SIM905 (a word list is easier to maintain than many quoted strings)
)


def is_builtin_call(name: str) -> bool:
    """True if ``name`` may be called without a ``let`` definition (see BUILTIN_CALLS)."""
    return name in BUILTIN_CALLS


# Azure Resource Graph tables reachable as ``arg("").<table>`` (a free, read-only service).
# ARG resolves table names case-insensitively, so they are matched that way.
ARG_TABLE_NAMES = (
    "Resources",
    "ResourceContainers",
    "SecurityResources",
    "AdvisorResources",
    "AlertsManagementResources",
    "AuthorizationResources",
    "ComputeResources",
    "HealthResources",
    "PolicyResources",
    "ResourceChanges",
    "ResourceContainerChanges",
    "ServiceHealthResources",
    "TagResources",
    "AppServiceResources",
    "NetworkResources",
    "KubernetesConfigurationResources",
    "ExtendedLocationResources",
    "GuestConfigurationResources",
    "MaintenanceResources",
    "PatchAssessmentResources",
    "PatchInstallationResources",
    "DesktopVirtualizationResources",
    "IoTSecurityResources",
    "InsightsResources",
    "RecoveryServicesResources",
)
_ARG_TABLES = frozenset(name.lower() for name in ARG_TABLE_NAMES)


class Token(NamedTuple):
    """One lexical token. ``kind`` is ``"id"``, ``"num"``, ``"str"`` or ``"p"`` (punctuation)."""

    kind: str
    text: str
    quoted: bool = False  # an identifier written as ['name'] / ["name"]
    raw: str = ""  # for strings: the literal exactly as written, quotes and prefix included

    @property
    def low(self) -> str:
        """The lower-cased text, for case-insensitive comparison."""
        return self.text.lower()

    def is_p(self, *chars: str) -> bool:
        """True for a punctuation token whose text is one of ``chars``."""
        return self.kind == "p" and self.text in chars

    def is_kw(self, *names: str) -> bool:
        """True for an unquoted identifier exactly equal to one of ``names``.

        Case-sensitive on purpose, like the service: ``Print`` is a name, not the keyword. Use
        ``low`` where a case-insensitive match makes the guard stricter.
        """
        return self.kind == "id" and not self.quoted and self.text in names


def _scan(kql: str, *, strict: bool) -> Iterator[tuple[str, str]]:
    """Yield ``(kind, text)`` pieces covering ``kql``.

    Kinds are ``ws``, ``comment``, ``ml``, ``str``, ``ident``, ``num``, ``punct`` and, only when
    not ``strict``, ``bad``. In strict mode anything unrecognised raises ConfigurationError.
    """
    pos = 0
    while pos < len(kql):
        match = _TOKEN.match(kql, pos)
        if match is None:
            if strict:
                raise ConfigurationError(
                    f"The KQL contains an unexpected character (U+{ord(kql[pos]):04X}) at "
                    f"offset {pos}, so it cannot be analysed safely. Only printable ASCII is "
                    "allowed outside strings and comments. The query was not sent."
                )
            yield "punct", kql[pos]
            pos += 1
            continue
        kind = match.lastgroup or "punct"
        if kind == "bad" and strict:
            raise ConfigurationError(
                f"The KQL contains an unterminated string literal at offset {pos}, so it cannot "
                "be analysed safely (quoted strings may not span lines). The query was not sent."
            )
        yield kind, match.group()
        pos = match.end()


def strip_noise(kql: str) -> str:
    """Return ``kql`` with comments and string literals replaced by spaces (never raises)."""
    return "".join(
        " " if kind in ("comment", "ml", "str", "bad") else text
        for kind, text in _scan(kql, strict=False)
    )


def tokenize(kql: str) -> list[Token]:
    """Split ``kql`` into tokens, dropping whitespace and comments.

    String literals become ``"str"`` tokens holding their inner text. A string literal that is
    the only thing between ``[`` and ``]`` is an escaped identifier (``['My Table']``) and is
    returned as a quoted ``"id"`` token instead.

    Raises:
        ConfigurationError: The text cannot be tokenized with certainty (see module docstring).
    """
    refused = _REFUSED.search(kql)
    if refused:
        raise ConfigurationError(
            f"The KQL contains a control or line-separator character (U+{ord(refused.group()):04X})"
            f" at offset {refused.start()}, so it cannot be analysed safely. The query was not "
            "sent."
        )
    raw: list[Token] = []
    previous = ""
    for kind, text in _scan(kql, strict=True):
        if kind == "ident" and previous == "num":
            raise ConfigurationError(
                f"The KQL contains an ambiguous number literal ({raw[-1].text}{text}), so it "
                "cannot be analysed safely. The query was not sent."
            )
        previous = kind
        if kind in ("ws", "comment"):
            continue
        if kind == "ml":
            raw.append(Token("str", text[3:-3], raw=text))
        elif kind == "str":
            raw.append(Token("str", text[2:-1] if text[0] == "@" else text[1:-1], raw=text))
        elif kind == "ident":
            raw.append(Token("id", text))
        elif kind == "num":
            raw.append(Token("num", text))
        else:
            raw.append(Token("p", text))
    tokens: list[Token] = []
    i = 0
    while i < len(raw):
        tok = raw[i]
        if tok.is_p("[") and i + 2 < len(raw) and raw[i + 1].kind == "str" and raw[i + 2].is_p("]"):
            name = raw[i + 1].text
            if not name or any(c in name for c in "\\\"'`~\n"):
                raise ConfigurationError(
                    f"The escaped name [{name!r}] cannot be analysed safely, so the query "
                    "was not sent."
                )
            tokens.append(Token("id", name, quoted=True))
            i += 3
        else:
            tokens.append(tok)
            i += 1
    return tokens


def skip_params(tokens: list[Token], j: int) -> tuple[int, set[int]]:
    """Skip ``name = value`` / ``hint.x = value`` parameters starting at ``j``.

    Returns the index of the first token after them and the indices of the value tokens.
    """
    values: set[int] = set()
    while j < len(tokens) and tokens[j].kind == "id":
        k = j + 1
        while k + 1 < len(tokens) and tokens[k].is_p(".") and tokens[k + 1].kind == "id":
            k += 2
        if (
            k >= len(tokens)
            or not tokens[k].is_p("=")
            or (k + 1 < len(tokens) and tokens[k + 1].is_p("="))
        ):
            break
        k += 1
        if k < len(tokens) and tokens[k].is_p("-", "+"):  # a signed number
            k += 1
        values.add(k)
        j = k + 1
    return j, values


def arg_source_end(tokens: list[Token], i: int) -> int | None:
    """If ``tokens[i]`` starts ``arg("").<ARG table>``, return the table token's index.

    The only accepted form is the exact word ``arg``, ``(``, an empty plain string (``""`` or
    ``''``), ``)``, ``.`` and a name from :data:`ARG_TABLE_NAMES` (any case). Whitespace and
    comments between the tokens do not matter. Anything else returns None.
    """
    tok = tokens[i]
    if tok.kind != "id" or tok.quoted or tok.text != "arg" or (i > 0 and tokens[i - 1].is_p(".")):
        return None
    if i + 5 >= len(tokens):
        return None
    opening, string, closing, dot, table = tokens[i + 1 : i + 6]
    if (
        opening.is_p("(")
        and string.kind == "str"
        and string.raw in ('""', "''")
        and closing.is_p(")")
        and dot.is_p(".")
        and table.kind == "id"
        and not table.quoted
        and table.low in _ARG_TABLES
    ):
        return i + 5
    return None


def forbidden_construct(tokens: list[Token], *, adx: bool) -> str | None:
    """Return a description of the first forbidden construct in ``tokens``, or None.

    For Sentinel (``adx=False``): ``search``, ``find``, the cross-workspace and cross-service
    calls in :data:`SENTINEL_CALLS` (except ``arg("").<table>``, see :func:`arg_source_end`),
    ``externaldata`` and the plugins in
    :data:`BLOCKED_PLUGINS`. For ADX: ``cluster()``, ``external_table()``, ``externaldata`` and
    the blocked plugins. For both: ``evaluate python()`` / ``evaluate r()``, and any call
    through an escaped name (``['table']('x')``), which would hide a call from the checks above.
    """
    calls = ADX_CALLS if adx else SENTINEL_CALLS
    for i, tok in enumerate(tokens):
        if tok.kind != "id":
            continue
        followed_by_paren = i + 1 < len(tokens) and tokens[i + 1].is_p("(")
        if tok.quoted:
            if followed_by_paren:
                return f"a call through the escaped name ['{tok.text}']"
            continue
        low = tok.low
        if low in calls and followed_by_paren:
            if low == "arg" and not adx:
                if arg_source_end(tokens, i) is not None:
                    continue
                return (
                    '`arg()` in any form other than `arg("").<table>` (an empty plain string and '
                    "one of the Azure Resource Graph tables " + ", ".join(ARG_TABLE_NAMES) + ")"
                )
            return f"`{low}()`"
        if low == _EXTERNALDATA or low in BLOCKED_PLUGINS:
            return f"`{low}`"
        if not adx and low in SENTINEL_OPERATORS:
            return f"`{low}`"
        if low == "evaluate":
            j, _ = skip_params(tokens, i + 1)
            if (
                j < len(tokens)
                and tokens[j].kind == "id"
                and (tokens[j].low in BLOCKED_EVALUATE_PLUGINS)
            ):
                return f"`evaluate {tokens[j].low}()`"
    return None


def reject_adx_constructs(kql: str) -> None:
    """Raise ConfigurationError if an ADX query or command uses a data-lake escape hatch.

    Raises:
        ConfigurationError: The text uses ``cluster()``, ``external_table()``, ``externaldata``
            or a blocked ``evaluate`` plugin, or cannot be tokenized with certainty.
    """
    found = forbidden_construct(tokenize(kql), adx=True)
    if found:
        raise ConfigurationError(
            f"{found} is not allowed with target='adx': it could reach the Sentinel data lake or "
            "a Log Analytics workspace (including Basic and Auxiliary tables), which bill per GB "
            "scanned, or an external data source. The request was not sent."
        )
