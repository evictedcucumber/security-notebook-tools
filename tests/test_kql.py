import pytest

from security_notebook_tools import _kql as kql
from security_notebook_tools._exceptions import ConfigurationError


def _texts(text):
    return [(t.kind, t.text) for t in kql.tokenize(text)]


def test_tokenize_basics():
    assert _texts("T | where a == 'x' // c\n| take 5") == [
        ("id", "T"),
        ("p", "|"),
        ("id", "where"),
        ("id", "a"),
        ("p", "="),
        ("p", "="),
        ("str", "x"),
        ("p", "|"),
        ("id", "take"),
        ("num", "5"),
    ]


def test_strings_hide_their_content():
    for text in [
        'print "a // b | union *"',
        "print 'it\\'s'",
        'print @"C:\\"',  # verbatim: a backslash is not an escape
        "print @'it''s'",
        "print ```multi\nline \" ' //```",
        "print ~~~x ``` y~~~",
        'print h"obfuscated"',
    ]:
        assert [t.text for t in kql.tokenize(text)][0] == "print", text
        assert all(t.kind != "id" or t.text in ("print", "h") for t in kql.tokenize(text)), text


def test_verbatim_backslash_does_not_swallow_following_code():
    tokens = kql.tokenize('print @"C:\\" ; Aux_CL ; print @"x"')
    assert "Aux_CL" in [t.text for t in tokens]


def test_escaped_identifiers():
    tokens = kql.tokenize("['My Table'] | project [\"A b\"], x['key']")
    quoted = [t.text for t in tokens if t.quoted]
    assert quoted == ["My Table", "A b", "key"]


@pytest.mark.parametrize(
    "text",
    [
        "print 'open",
        'print "open',
        'print "line\nbreak"',
        "print @'open",
        "print ```open",
        'print @h"x"',
        "print \u00e9",
        "print \x00",
        "T // c\x85 U",
        "T | where ['a\\b'] == 1",
        "T | where [''] == 1",
    ],
)
def test_tokenize_rejects_what_it_cannot_read_with_certainty(text):
    with pytest.raises(ConfigurationError):
        kql.tokenize(text)


def test_comment_may_contain_non_ascii():
    assert [t.text for t in kql.tokenize("T // caf\u00e9\n| take 1")][0] == "T"


def test_strip_noise_never_raises():
    assert kql.strip_noise("a 'x' \"x\" @'x' ```x``` // x").split() == ["a"]
    assert kql.strip_noise("a 'unterminated").startswith("a ")
    assert kql.strip_noise("T | where Msg == 'TimeGenerated'").count("TimeGenerated") == 0


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("search *", "`search`"),
        ("T | union table('x')", "`table()`"),
        ("workspace('w').T", "`workspace()`"),
        ("cluster('c').database('d').T", "`cluster()`"),
        ("externaldata (a:string) [h'x']", "`externaldata`"),
        ("evaluate sql_request('a', 'b')", "`sql_request`"),
        ("T | evaluate hint.x=1 http_request('u')", "`http_request`"),
    ],
)
def test_forbidden_construct_sentinel(text, found):
    assert kql.forbidden_construct(kql.tokenize(text), adx=False) == found


@pytest.mark.parametrize(
    "text",
    [
        "T | where Msg == 'search * workspace(x) cluster(y)'",
        "T // find table(x)\n| take 1",
        "T | project ['search'], tablename, clusters",
        "T | evaluate bag_unpack(Props)",
        "T | extend table = 1",  # `table` without a call is just a name
    ],
)
def test_forbidden_construct_not_found(text):
    assert kql.forbidden_construct(kql.tokenize(text), adx=False) is None


def test_adx_only_blocks_data_lake_escape_hatches():
    # search, find, table() and database() are ordinary ADX; cluster() reaches the lake.
    for ok in ["search *", "find 'x'", "table('T')", "database('d').T", "T | take 1"]:
        kql.reject_adx_constructs(ok)
    for bad in [
        "cluster('c').database('d').T",
        "external_table('T')",
        "externaldata(a:string)[h'x']",
        "T | evaluate sql_request('a', 'b')",
        ".show tables | evaluate http_request('u')",
    ]:
        with pytest.raises(ConfigurationError, match="per GB scanned"):
            kql.reject_adx_constructs(bad)
    kql.reject_adx_constructs("T | where Msg == 'cluster(\"x\")' // external_table('T')")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0x1F", [("num", "0x1F")]),
        ("1e3", [("num", "1e3")]),
        ("5min", [("num", "5min")]),
        ("1d", [("num", "1d")]),
        ("30ms", [("num", "30ms")]),
        ("1.5 x", [("num", "1.5"), ("id", "x")]),
    ],
)
def test_number_literals(text, expected):
    assert _texts(text) == expected


@pytest.mark.parametrize("text", ["10AuxT", "1.5AuxT", "0x1FAuxT", "10mAuxT", "5minutesx", "1_a"])
def test_identifier_glued_to_a_number_is_refused(text):
    with pytest.raises(ConfigurationError, match="ambiguous number literal"):
        kql.tokenize(text)


def test_keywords_are_case_sensitive_for_allowing():
    tokens = kql.tokenize("print Print")
    assert tokens[0].is_kw("print")
    assert not tokens[1].is_kw("print")


def test_forbidden_construct_through_escaped_names():
    for text in ["['table']('x')", "T | union ['cluster']('c')", "[\"workspace\"]('w').T"]:
        found = kql.forbidden_construct(kql.tokenize(text), adx=False)
        assert found and "escaped name" in found
    assert kql.forbidden_construct(kql.tokenize("['cluster']('c').T"), adx=True)
    assert (
        kql.forbidden_construct(kql.tokenize("T | project ['table'], ['a b']"), adx=False) is None
    )


@pytest.mark.parametrize(
    "char", ["\x00", "\x08", "\x0b", "\x0c", "\x1f", "\x7f", "\x85", "\x9f", " ", " "]
)
def test_control_characters_refused_everywhere(char):
    for text in [f"T{char}| take 1", f"T // c{char}x", f"T | where a == 'x{char}y'"]:
        with pytest.raises(ConfigurationError, match="control or line-separator"):
            kql.tokenize(text)


def test_evaluate_python_and_r_blocked_only_as_plugins():
    for text in ["T | evaluate python(typeof(*), 'c')", "T | evaluate hint.a=1 r(typeof(*), 'c')"]:
        assert kql.forbidden_construct(kql.tokenize(text), adx=True)
    assert (
        kql.forbidden_construct(kql.tokenize("T | project r, python | where r > 1"), adx=True)
        is None
    )


def test_arg_source_end():
    def end(text):
        return kql.arg_source_end(kql.tokenize(text), 0)

    assert end('arg("").Resources') == 5
    assert end("arg('').resourcecontainers | take 1") == 5
    for bad in [
        'arg("x").Resources',
        'arg("").Nope',
        'arg("")',
        'arg(@"").Resources',
        'ARG("").Resources',
    ]:
        assert end(bad) is None


def test_arg_is_only_forbidden_for_sentinel_outside_the_exact_form():
    ok = kql.tokenize('arg("").Resources | take 1')
    assert kql.forbidden_construct(ok, adx=False) is None
    bad = kql.tokenize('arg("x").Resources')
    assert "arg()" in kql.forbidden_construct(bad, adx=False)
    # ADX is unchanged: arg() is not special there
    assert kql.forbidden_construct(bad, adx=True) is None
