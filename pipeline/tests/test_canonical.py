"""Unit tests for :mod:`signalpipe.canonical`.

The module is fully pure (no network, no clock, no import-time side effects), so
every test here is a straight table/parametrize against derived expected values.
Expected outputs are read off the REAL code path in ``canonical.py`` — not the
docstrings — per the writer guide.

Notable behavioral facts pinned by these tests (verified against the source):
* ``canonicalize`` wraps only ``urlsplit`` in try/except → an *unparseable* URL
  like ``http://[::1`` returns ``None``. A *bad port* (``http://x:notaport/``)
  is NOT guarded (``.port`` is read outside the try) and raises; that latent
  behavior is deliberately not exercised as "passing" here.
* ``is_aggregator`` catches the same ``ValueError`` and returns ``False``.
* ``registered_domain`` is a naive last-two-labels heuristic; ``co.uk`` degrades
  to ``co.uk`` on purpose — asserted as-is.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import pytest

from signalpipe import canonical as c


# --------------------------------------------------------------------------- #
# Module constants (public API surface documented in the briefing)
# --------------------------------------------------------------------------- #
def test_module_constants_are_as_documented():
    assert "utm_source" in c.TRACKER_PARAMS
    assert "gclid" in c.TRACKER_PARAMS and "fbclid" in c.TRACKER_PARAMS
    assert "source" in c.TRACKER_PARAMS and "ref" in c.TRACKER_PARAMS
    assert c.TRACKER_PREFIXES == ("utm_", "pk_")
    assert c.HOST_SPECIFIC["youtube.com"] == {"si", "feature"}
    assert c.HOST_SPECIFIC["youtu.be"] == {"si"}
    assert c.HOST_SPECIFIC["twitter.com"] == {"s", "t"}
    assert c.HOST_SPECIFIC["x.com"] == {"s", "t"}
    assert {"news.ycombinator.com", "lobste.rs", "reddit.com"} <= c.AGGREGATOR_HOSTS


# --------------------------------------------------------------------------- #
# _host_base
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "host, expected",
    [
        ("Example.COM", "example.com"),
        ("WWW.Example.com", "example.com"),
        ("www.example.com", "example.com"),
        ("example.com", "example.com"),
        # only a literal leading "www." is stripped, not "wwwx"
        ("wwwx.com", "wwwx.com"),
        # strips exactly one leading www.
        ("www.www.example.com", "www.example.com"),
        ("", ""),
    ],
)
def test_host_base(host, expected):
    assert c._host_base(host) == expected


# --------------------------------------------------------------------------- #
# canonicalize — tracker param stripping
# --------------------------------------------------------------------------- #
def test_canonicalize_strips_utm_and_click_ids_keeps_content_key():
    out = c.canonicalize("https://ex.com/a?utm_source=x&id=3&gclid=y")
    assert out == "https://ex.com/a?id=3"


def test_canonicalize_drops_every_exact_tracker_param():
    keys = sorted(c.TRACKER_PARAMS)
    query = "&".join("%s=v%d" % (k, i) for i, k in enumerate(keys)) + "&keep=1"
    out = c.canonicalize("https://ex.com/a?" + query)
    assert out == "https://ex.com/a?keep=1"


def test_canonicalize_drops_tracker_prefixes():
    out = c.canonicalize("https://ex.com/a?utm_anything=1&pk_ref=2&keep=3")
    assert out == "https://ex.com/a?keep=3"


def test_canonicalize_tracker_matching_is_case_insensitive():
    out = c.canonicalize("https://ex.com/a?UTM_Source=x&GCLID=y&PK_Foo=z&Keep=1")
    assert out == "https://ex.com/a?Keep=1"


def test_canonicalize_sorts_surviving_params():
    out = c.canonicalize("https://ex.com/a?z=1&a=2&m=3")
    assert out == "https://ex.com/a?a=2&m=3&z=1"


def test_canonicalize_keeps_blank_valued_survivor():
    out = c.canonicalize("https://ex.com/a?b=2&a=")
    assert out == "https://ex.com/a?a=&b=2"


# --------------------------------------------------------------------------- #
# canonicalize — host / scheme / port normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url, expected",
    [
        # www stripped, host lowercased, :80 dropped, https forced, path case kept
        ("http://WWW.Ex.com:80/Path/", "https://ex.com/Path"),
        # :443 dropped
        ("https://ex.com:443/x", "https://ex.com/x"),
        # non-default port kept
        ("https://ex.com:8080/x", "https://ex.com:8080/x"),
        # http upgraded to https
        ("http://ex.com/x", "https://ex.com/x"),
        # uppercase host lowercased but path preserved
        ("https://EXAMPLE.COM/AbC", "https://example.com/AbC"),
        # leading/trailing whitespace stripped
        ("  https://ex.com/x  ", "https://ex.com/x"),
    ],
)
def test_canonicalize_host_scheme_port(url, expected):
    assert c.canonicalize(url) == expected


def test_canonicalize_combined_port_www_query_fragment():
    out = c.canonicalize("http://www.EXAMPLE.com:8080//A//?utm_source=x&Keep=1#frag")
    assert out == "https://example.com:8080/A?Keep=1"


# --------------------------------------------------------------------------- #
# canonicalize — slash / trailing-slash / root
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://ex.com//a///b/", "https://ex.com/a/b"),
        ("https://ex.com/", "https://ex.com/"),  # root slash preserved
        ("https://ex.com", "https://ex.com/"),  # empty path -> "/"
        ("https://ex.com/a/", "https://ex.com/a"),  # trailing slash trimmed
        ("https://ex.com/a//b//c/", "https://ex.com/a/b/c"),
    ],
)
def test_canonicalize_slash_handling(url, expected):
    assert c.canonicalize(url) == expected


# --------------------------------------------------------------------------- #
# canonicalize — AMP (path suffix + CDN unwrap + amp query)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://ex.com/story/amp/", "https://ex.com/story"),
        ("https://ex.com/story/amp", "https://ex.com/story"),
        # "/amp" or "/amp/" as the whole path collapses to root
        ("https://ex.com/amp", "https://ex.com/"),
        ("https://ex.com/amp/", "https://ex.com/"),
        # AMP CDN unwrap (c / with s/)
        ("https://cdn.ampproject.org/c/s/ex.com/story", "https://ex.com/story"),
        # AMP CDN unwrap (c / without s/)
        ("https://cdn.ampproject.org/c/ex.com/story", "https://ex.com/story"),
        # AMP CDN unwrap (v variant)
        ("https://cdn.ampproject.org/v/s/ex.com/story", "https://ex.com/story"),
        # combined: dup slashes + CDN whitespace
        ("  https://cdn.ampproject.org/c/s/ex.com//story//amp/  ", "https://ex.com/story"),
        # amp=1 query dropped
        ("https://ex.com/x?amp=1", "https://ex.com/x"),
        # outputtype=amp dropped
        ("https://ex.com/x?outputtype=amp", "https://ex.com/x"),
        # amp=0 is falsy -> kept
        ("https://ex.com/x?amp=0", "https://ex.com/x?amp=0"),
    ],
)
def test_canonicalize_amp(url, expected):
    assert c.canonicalize(url) == expected


def test_canonicalize_amp_cdn_unwrap_preserves_embedded_scheme():
    # rest already starts with a scheme -> used verbatim (not re-prefixed).
    out = c.canonicalize("https://cdn.ampproject.org/c/https://ex.com/story")
    assert out == "https://ex.com/story"


def test_canonicalize_non_cdn_amp_path_not_unwrapped():
    # A slash before "cdn.ampproject.org" means the CDN regex must NOT match;
    # the URL is treated as an ordinary host (google) with an /amp/... path.
    out = c.canonicalize("https://www.google.com/amp/s/ex.com/story")
    assert out == "https://google.com/amp/s/ex.com/story"


# --------------------------------------------------------------------------- #
# canonicalize — fragments
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://ex.com/x#section", "https://ex.com/x"),  # plain fragment dropped
        ("https://ex.com/x#!bang", "https://ex.com/x#!bang"),  # hashbang kept
        ("https://ex.com/x?a=1#!bang", "https://ex.com/x?a=1#!bang"),
        ("https://ex.com/x#", "https://ex.com/x"),  # empty fragment dropped
    ],
)
def test_canonicalize_fragment(url, expected):
    assert c.canonicalize(url) == expected


# --------------------------------------------------------------------------- #
# canonicalize — rejection of non-http(s)/empty/unparseable
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "   ",
        "ftp://x/y",
        "mailto:a@b",
        "javascript:alert(1)",
        "https://",  # no host
        "http:///path",  # empty host
        "//ex.com/x",  # scheme-relative -> empty scheme
        "http://[::1",  # urlsplit raises ValueError -> caught -> None
        "not a url at all",  # no scheme
    ],
)
def test_canonicalize_rejects_returns_none(url):
    assert c.canonicalize(url) is None


# --------------------------------------------------------------------------- #
# canonicalize — host-specific param stripping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://youtube.com/watch?v=abc&si=xyz&feature=share", "https://youtube.com/watch?v=abc"),
        ("https://www.youtube.com/watch?v=abc&si=xyz", "https://youtube.com/watch?v=abc"),
        ("https://youtu.be/abc?si=xyz", "https://youtu.be/abc"),
        ("https://open.spotify.com/track/123?si=xyz", "https://open.spotify.com/track/123"),
        ("https://twitter.com/u/status/1?s=20&t=abc", "https://twitter.com/u/status/1"),
        ("https://x.com/u/status/1?s=20&t=abc", "https://x.com/u/status/1"),
    ],
)
def test_canonicalize_host_specific_stripping(url, expected):
    assert c.canonicalize(url) == expected


def test_canonicalize_host_specific_only_applies_to_listed_hosts():
    # example.com is not in HOST_SPECIFIC, so si/s/t/feature all survive & sort.
    out = c.canonicalize("https://example.com/x?si=1&s=2&t=3&feature=4")
    assert out == "https://example.com/x?feature=4&s=2&si=1&t=3"


# --------------------------------------------------------------------------- #
# _filter_params (direct)
# --------------------------------------------------------------------------- #
def test_filter_params_empty_query_returns_empty():
    assert c._filter_params("ex.com", "") == ""


@pytest.mark.parametrize(
    "host, query, expected",
    [
        # host-specific base match works through www.
        ("www.youtube.com", "v=1&si=2&feature=3", "v=1"),
        # tracker exact + prefix + host-none
        ("ex.com", "utm_medium=x&pk_x=y&keep=1", "keep=1"),
        # sorting + blank values preserved
        ("ex.com", "z=1&a=&m=3", "a=&m=3&z=1"),
        # amp truthy variants dropped, falsy kept
        ("ex.com", "amp=1", ""),
        ("ex.com", "amp=true", ""),
        ("ex.com", "amp=AMP", ""),
        ("ex.com", "amp=2", "amp=2"),
        ("ex.com", "outputtype=amp", ""),
        ("ex.com", "outputtype=html", "outputtype=html"),
        # "amp"/"outputtype" only special-cased for those keys, not e.g. "ramp"
        ("ex.com", "ramp=1", "ramp=1"),
    ],
)
def test_filter_params_table(host, query, expected):
    assert c._filter_params(host, query) == expected


# --------------------------------------------------------------------------- #
# _strip_amp (direct)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url, expected",
    [
        # scheme-less rest gets https:// prepended
        ("https://cdn.ampproject.org/c/s/ex.com/story", "https://ex.com/story"),
        ("http://cdn.ampproject.org/v/ex.com/story", "https://ex.com/story"),
        # embedded scheme in rest kept verbatim
        ("https://cdn.ampproject.org/c/https://ex.com/story", "https://ex.com/story"),
        ("https://cdn.ampproject.org/c/http://ex.com/story", "http://ex.com/story"),
        # subdomain before cdn still matches ([^/]* absorbs it)
        ("https://amp.cdn.ampproject.org/c/s/ex.com/x", "https://ex.com/x"),
        # case-insensitive host match
        ("HTTPS://CDN.AMPPROJECT.ORG/C/S/ex.com/story", "https://ex.com/story"),
        # non-amp URL passes through untouched
        ("https://ex.com/x", "https://ex.com/x"),
        # slash before cdn means no match -> unchanged
        (
            "https://www.google.com/amp/s/cdn.ampproject.org/c/s/ex.com/x",
            "https://www.google.com/amp/s/cdn.ampproject.org/c/s/ex.com/x",
        ),
    ],
)
def test_strip_amp(url, expected):
    assert c._strip_amp(url) == expected


# --------------------------------------------------------------------------- #
# registered_domain
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://www.a.b.example.com/x", "example.com"),
        ("https://example.com/path", "example.com"),
        ("example.com", "example.com"),
        ("www.example.com", "example.com"),
        ("Example.COM", "example.com"),
        ("localhost", "localhost"),
        ("", ""),
        # naive heuristic: multi-part public suffix degrades to last two labels
        ("x.co.uk", "co.uk"),
        ("https://sub.example.co.uk/x", "co.uk"),
        ("a.b.c.d.com", "d.com"),
        # hostname property strips the port for URL inputs
        ("https://www.example.com:8080/x", "example.com"),
    ],
)
def test_registered_domain(value, expected):
    assert c.registered_domain(value) == expected


# --------------------------------------------------------------------------- #
# is_aggregator
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://news.ycombinator.com/item?id=1", True),
        ("https://lobste.rs/s/x", True),
        ("https://reddit.com/r/x", True),
        ("https://old.reddit.com/r/x", True),
        ("https://www.reddit.com/r/x", True),  # www stripped -> reddit.com
        ("https://REDDIT.COM/r/x", True),  # host lowercased
        ("https://foo.reddit.com/x", True),  # endswith .reddit.com
        ("https://news.google.com/topstories", True),
        ("https://techmeme.com/", True),
        ("https://example.com/x", False),
        ("https://sub.techmeme.com/", False),  # only exact techmeme.com listed
        ("http://[::1", False),  # ValueError caught -> False
        ("not a url", False),  # no host -> "" -> False
        ("", False),
    ],
)
def test_is_aggregator(url, expected):
    assert c.is_aggregator(url) is expected


# --------------------------------------------------------------------------- #
# dedup_key delegates to canonicalize
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url, expected",
    [
        (None, None),
        ("", None),
        # full pipeline: www strip + utm drop + fragment drop + surviving key
        ("https://www.EXAMPLE.com/a?utm_source=x&id=1#frag", "https://example.com/a?id=1"),
        # http->https, :80 drop, dedup slashes, /amp/ suffix collapse, trailing-slash trim
        ("http://ex.com:80//p//amp/", "https://ex.com/p"),
        ("ftp://x/y", None),
        ("https://cdn.ampproject.org/c/s/ex.com/story", "https://ex.com/story"),
    ],
)
def test_dedup_key_equals_canonicalize(url, expected):
    # Pin the concrete canonical form (not just delegation): a bug in canonicalize
    # would break this even though dedup_key mirrors it exactly.
    assert c.dedup_key(url) == expected
    # ...and lock the delegation contract itself.
    assert c.dedup_key(url) == c.canonicalize(url)


# --------------------------------------------------------------------------- #
# Property-based (hypothesis optional; skipped cleanly if absent)
# --------------------------------------------------------------------------- #
pytestmark_property = pytest.mark.property


@pytest.mark.property
def test_property_idempotent_and_invariants():
    # NB: plain ``importorskip("hypothesis")`` is not enough on this box — a
    # broken/partial namespace-package shell for ``hypothesis`` can resolve while
    # ``given``/``assume``/``settings`` are absent. Guard the concrete imports and
    # skip (ImportError also covers ModuleNotFoundError) so the suite never reddens.
    try:
        from hypothesis import assume, given, settings
        from hypothesis import strategies as st
    except ImportError:
        pytest.skip("hypothesis not available")

    _alnum = "abcdefghijklmnopqrstuvwxyz0123456789"
    _label = st.text(alphabet=_alnum, min_size=1, max_size=6)
    _seg = st.text(alphabet=_alnum, min_size=1, max_size=6)
    _kv = st.tuples(
        st.text(alphabet=_alnum, min_size=1, max_size=5),
        st.text(alphabet=_alnum, min_size=0, max_size=5),
    )

    @st.composite
    def raw_urls(draw):
        from urllib.parse import urlencode

        scheme = draw(st.sampled_from(["http", "https"]))
        host = ".".join(draw(st.lists(_label, min_size=1, max_size=3)))
        segs = draw(st.lists(_seg, min_size=0, max_size=3))
        path = "/" + "/".join(segs)
        params = draw(st.lists(_kv, min_size=0, max_size=3))
        url = scheme + "://" + host + path
        q = urlencode(params)
        if q:
            url += "?" + q
        return url

    @given(raw_urls())
    @settings(max_examples=200, deadline=None)
    def _check(url):
        once = c.canonicalize(url)
        assert once is not None
        # idempotence
        assert c.canonicalize(once) == once

        parts = urlsplit(url)
        netloc = parts.netloc
        assume(not netloc.startswith("www."))

        # prepending "www." does not change the canonical form
        www_url = urlunsplit(parts._replace(netloc="www." + netloc))
        assert c.canonicalize(www_url) == once

        # adding a utm_* tracker param does not change the canonical form
        sep = "&" if parts.query else "?"
        assert c.canonicalize(url + sep + "utm_source=zzz") == once

    _check()
