"""Tests for :mod:`signalpipe.render` — Jinja2 rendering of the review dashboard
page and the plain semantic-HTML per-item block embedded in the feed's
``content:encoded``.

Fully hermetic: the only external boundary is the filesystem (the real repo
templates under ``signalpipe/templates``), which are deterministic assets. No
clock, network, or DB. autoescape is on for html/xml templates, so assertions
account for ``&lt;`` / ``&amp;`` escaping.
"""

from __future__ import annotations

import pathlib

import pytest

from signalpipe import render


# --------------------------------------------------------------------------- #
# Builders — minimal but complete contexts derived from the real templates
# --------------------------------------------------------------------------- #
def _curated_item(**over):
    item = dict(
        id=1,
        curated=True,
        why_it_matters="It matters because reasons.",
        notes_list=["point one", "point two"],
        summary="A concise summary.",
        novelty="a genuinely new benchmark",
        channel_list=["ai", "security"],
        source_url="https://example.com/story",
        surfaces=[
            {
                "url": "https://news.ycombinator.com/item?id=1",
                "name": "Hacker News",
                "points": 100,
                "comments": 42,
            }
        ],
    )
    item.update(over)
    return item


def _uncurated_item(**over):
    item = dict(
        id=2,
        curated=False,
        score=7.5,
        excerpt="A deterministic excerpt of the article body.",
    )
    item.update(over)
    return item


def _dashboard_ctx(**over):
    """A minimal-but-complete ctx matching every key dashboard.html reads."""
    ctx = dict(
        auto_refresh=60,
        active_channel="",
        channels=["everything", "ai", "security"],
        version="1.2.3",
        health=dict(
            sources_verified=8,
            sources_enabled=10,
            clusters=123,
            curated=45,
            spend_today=1.2,
            last_ingest="2026-07-04T12:34:56+00:00",
            last_curate="2026-07-04T11:00:00+00:00",
            failing_sources=[],
            recent=[],
        ),
        items=[
            dict(
                id=1,
                curated=True,
                relevance_score=8,
                score=7.5,
                channel_list=["ai"],
                paywalled=0,
                surface_count=1,
                link="https://example.com/story",
                title="Example story about AI models",
                why_it_matters="It matters.",
                notes_list=["note a"],
                summary="Summary here.",
                surfaces=[
                    {"url": "https://news.ycombinator.com/item?id=1", "name": "HN", "points": 100}
                ],
                source_url="https://example.com/orig",
            )
        ],
    )
    ctx.update(over)
    return ctx


# --------------------------------------------------------------------------- #
# _fmt_ts (the 'ts' filter)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-07-04T12:34:56+00:00", "2026-07-04 12:34"),
        ("2026-07-04T12:34", "2026-07-04 12:34"),
        ("2026-07-04", "2026-07-04"),
        ("", ""),
        (None, ""),
        (0, ""),
        (False, ""),
    ],
)
def test_fmt_ts(value, expected):
    assert render._fmt_ts(value) == expected


def test_fmt_ts_truncates_to_16_chars():
    # str(iso)[:16] then T->space; a long ISO string keeps only date + HH:MM.
    assert render._fmt_ts("2026-12-31T23:59:59.123456+00:00") == "2026-12-31 23:59"


def test_fmt_ts_registered_as_filter():
    assert render._env.filters["ts"] is render._fmt_ts


def test_templates_dir_points_at_real_assets():
    assert isinstance(render.TEMPLATES_DIR, pathlib.Path)
    assert render.TEMPLATES_DIR.is_dir()
    assert (render.TEMPLATES_DIR / "dashboard.html").is_file()
    assert (render.TEMPLATES_DIR / "feed_item.html").is_file()


# --------------------------------------------------------------------------- #
# render_feed_item_html — curated block
# --------------------------------------------------------------------------- #
def test_feed_curated_renders_all_sections():
    html = render.render_feed_item_html(_curated_item())
    assert "<strong>Why it matters:</strong> It matters because reasons." in html
    assert "<strong>Notes</strong>" in html
    assert "<li>point one</li>" in html
    assert "<li>point two</li>" in html
    assert "<strong>Summary:</strong> A concise summary." in html
    # "What's new" is static template text (not a variable), so its apostrophe
    # is a literal, not autoescaped to &#39;.
    assert "<strong>What's new:</strong> a genuinely new benchmark" in html
    # Uncurated fallback text must not appear for a curated item.
    assert "deterministic signals" not in html


def test_feed_curated_omits_missing_optional_sections():
    item = _curated_item(why_it_matters="", notes_list=[], summary="", novelty="")
    html = render.render_feed_item_html(item)
    assert "Why it matters" not in html
    assert "<strong>Notes</strong>" not in html
    assert "Summary:" not in html
    assert "What&#39;s new" not in html


def test_feed_archive_url_never_rendered():
    item = _curated_item(archive_url="https://archive.example/should-not-appear")
    html = render.render_feed_item_html(item)
    assert "archive.example" not in html
    assert "should-not-appear" not in html


# --------------------------------------------------------------------------- #
# render_feed_item_html — uncurated block
# --------------------------------------------------------------------------- #
def test_feed_uncurated_shows_score_and_excerpt():
    html = render.render_feed_item_html(_uncurated_item())
    assert "Scored 7.5/10 by deterministic signals;" in html
    assert "not yet LLM-curated." in html
    assert "A deterministic excerpt of the article body." in html
    # No curated sections.
    assert "Why it matters" not in html
    assert "<strong>Notes</strong>" not in html


def test_feed_uncurated_missing_score_defaults_to_zero():
    item = {"id": 9, "curated": False}
    html = render.render_feed_item_html(item)
    assert "Scored 0.0/10 by deterministic signals;" in html


def test_feed_uncurated_without_excerpt_omits_paragraph():
    item = {"id": 9, "curated": False, "score": 3.0}
    html = render.render_feed_item_html(item)
    assert "Scored 3.0/10" in html
    # Only the <em> paragraph, no trailing excerpt paragraph.
    assert html.count("<p>") == 0 or "excerpt" not in html


# --------------------------------------------------------------------------- #
# surfaces rendering
# --------------------------------------------------------------------------- #
def test_feed_surfaces_points_and_comments():
    item = _curated_item(
        surfaces=[
            {"url": "https://hn.example/1", "name": "Hacker News", "points": 100, "comments": 42},
            {"url": "https://lob.example/2", "name": "Lobsters", "points": 0, "comments": 0},
        ]
    )
    html = render.render_feed_item_html(item)
    assert "<strong>Where it surfaced</strong>" in html
    assert '<a href="https://hn.example/1">Hacker News</a>' in html
    assert "100 points" in html
    assert "42 comments" in html
    # The second surface has zero points/comments -> those clauses are suppressed,
    # leaving a bare list item with no " points"/" comments" trailer.
    assert '<li><a href="https://lob.example/2">Lobsters</a></li>' in html


def test_feed_no_surfaces_section_when_empty():
    item = _curated_item(surfaces=[])
    html = render.render_feed_item_html(item)
    assert "Where it surfaced" not in html


# --------------------------------------------------------------------------- #
# source_url + paywalled/read_kind badge
# --------------------------------------------------------------------------- #
def test_feed_paywalled_primary_shows_free_read_note():
    item = _curated_item(source_url="https://paywall.example/x", paywalled=1, read_kind="primary")
    html = render.render_feed_item_html(item)
    assert "<strong>Original:</strong>" in html
    assert '<a href="https://paywall.example/x">https://paywall.example/x</a>' in html
    assert "(paywalled; free read linked above via primary)" in html


def test_feed_paywalled_canonical_fallback_is_plain():
    item = _curated_item(
        source_url="https://paywall.example/x", paywalled=1, read_kind="canonical-fallback"
    )
    html = render.render_feed_item_html(item)
    assert "(paywalled)" in html
    assert "free read" not in html


def test_feed_paywalled_without_read_kind_is_plain():
    item = _curated_item(source_url="https://paywall.example/x", paywalled=1, read_kind="")
    html = render.render_feed_item_html(item)
    assert "(paywalled)" in html
    assert "free read" not in html


def test_feed_not_paywalled_has_no_badge():
    item = _curated_item(source_url="https://free.example/x", paywalled=0)
    html = render.render_feed_item_html(item)
    assert "<strong>Original:</strong>" in html
    assert "(paywalled" not in html


def test_feed_no_source_url_omits_original():
    item = _curated_item(source_url="")
    html = render.render_feed_item_html(item)
    assert "Original:" not in html


# --------------------------------------------------------------------------- #
# channels join
# --------------------------------------------------------------------------- #
def test_feed_channels_joined():
    item = _curated_item(channel_list=["ai", "security", "web"])
    html = render.render_feed_item_html(item)
    assert "<small>Channels: ai, security, web</small>" in html


def test_feed_no_channels_omits_line():
    item = _curated_item(channel_list=[])
    html = render.render_feed_item_html(item)
    assert "Channels:" not in html


def test_feed_minimal_item_renders_uncurated_default():
    # An essentially empty item still renders (curated falsy -> uncurated branch).
    html = render.render_feed_item_html({})
    assert "Scored 0.0/10 by deterministic signals;" in html
    assert "Original:" not in html
    assert "Channels:" not in html


# --------------------------------------------------------------------------- #
# autoescape / XSS
# --------------------------------------------------------------------------- #
def test_feed_autoescapes_html_and_amp():
    payload = '<script>alert("x")</script> & <b>bold</b>'
    item = _curated_item(why_it_matters=payload)
    html = render.render_feed_item_html(item)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html
    # The escaped bold tag must not survive as a real element.
    assert "<b>bold</b>" not in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html


def test_feed_autoescapes_notes_and_summary():
    item = _curated_item(
        notes_list=["<img src=x onerror=1>", "plain"],
        summary="a & b < c",
    )
    html = render.render_feed_item_html(item)
    assert "<img" not in html
    assert "&lt;img src=x onerror=1&gt;" in html
    assert "a &amp; b &lt; c" in html


@pytest.mark.property
def test_feed_property_field_always_escaped():
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st
    from markupsafe import escape

    text_strategy = st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=1,
        max_size=120,
    )

    @given(payload=text_strategy)
    @settings(max_examples=75, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def _check(payload):
        item = _curated_item(why_it_matters=payload)
        html = render.render_feed_item_html(item)
        # Jinja autoescape produces exactly markupsafe.escape() of the input,
        # so the escaped form is always a verbatim substring of the output.
        assert str(escape(payload)) in html

    _check()


# --------------------------------------------------------------------------- #
# render_feed_items — the {id: html} map
# --------------------------------------------------------------------------- #
def test_render_feed_items_maps_by_id():
    a = _curated_item(id=11)
    b = _uncurated_item(id=22)
    result = render.render_feed_items([a, b])
    assert set(result.keys()) == {11, 22}
    assert result[11] == render.render_feed_item_html(a)
    assert result[22] == render.render_feed_item_html(b)
    assert isinstance(result[11], str)
    assert "It matters because reasons." in result[11]
    assert "deterministic signals" in result[22]


def test_render_feed_items_empty_list():
    assert render.render_feed_items([]) == {}


def test_render_feed_items_last_id_wins_on_collision():
    a = _curated_item(id=5, why_it_matters="first")
    b = _curated_item(id=5, why_it_matters="second")
    result = render.render_feed_items([a, b])
    assert set(result.keys()) == {5}
    assert "second" in result[5]
    assert "first" not in result[5]


# --------------------------------------------------------------------------- #
# render_dashboard — integration smoke against the real template
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_dashboard_smoke_structure():
    html = render.render_dashboard(_dashboard_ctx())
    assert "<title>Signal" in html
    assert '<meta http-equiv="refresh" content="60">' in html
    assert "signal 1.2.3" in html


@pytest.mark.integration
def test_dashboard_channel_nav():
    html = render.render_dashboard(_dashboard_ctx(active_channel="ai"))
    # 'everything' is filtered out of the loop; explicit link stays.
    assert '<a href="/?channel=ai"' in html
    assert '<a href="/?channel=security"' in html
    # The active channel gets the active class.
    assert '<a href="/?channel=ai" class="active">ai</a>' in html
    # Feed link carries the active channel query.
    assert '<a href="/feed.xml?channel=ai">feed.xml</a>' in html


@pytest.mark.integration
def test_dashboard_everything_active_when_no_channel():
    html = render.render_dashboard(_dashboard_ctx(active_channel=""))
    assert '<a href="/" class="active">everything</a>' in html
    assert '<a href="/feed.xml">feed.xml</a>' in html


@pytest.mark.integration
def test_dashboard_health_stats_formatted():
    html = render.render_dashboard(_dashboard_ctx())
    assert "8/10" in html  # sources_verified/sources_enabled
    assert ">123<" in html  # clusters
    assert ">45<" in html  # curated
    assert "$1.20" in html  # spend_today formatted %.2f
    # ts filter applied to last_ingest / last_curate.
    assert "2026-07-04 12:34" in html
    assert "2026-07-04 11:00" in html


@pytest.mark.integration
def test_dashboard_never_when_no_timestamps():
    ctx = _dashboard_ctx()
    ctx["health"]["last_ingest"] = ""
    ctx["health"]["last_curate"] = None
    html = render.render_dashboard(ctx)
    assert html.count("never") >= 2


@pytest.mark.integration
def test_dashboard_failing_sources_details():
    ctx = _dashboard_ctx()
    ctx["health"]["failing_sources"] = [
        {"slug": "brokenfeed", "error_count": 3, "last_error": "timeout"}
    ]
    html = render.render_dashboard(ctx)
    assert "1 failing source(s)" in html
    assert "<code>brokenfeed</code>" in html
    assert "×3" in html
    assert "timeout" in html


@pytest.mark.integration
def test_dashboard_recent_runs():
    ctx = _dashboard_ctx()
    ctx["health"]["recent"] = [
        {
            "ts": "2026-07-04T09:15:00+00:00",
            "level": "INFO",
            "job": "ingest",
            "message": "ok",
        }
    ]
    html = render.render_dashboard(ctx)
    assert "recent runs" in html
    assert "2026-07-04 09:15" in html
    assert "[INFO]" in html
    assert "ingest: ok" in html


@pytest.mark.integration
def test_dashboard_curated_heading_and_badges():
    html = render.render_dashboard(_dashboard_ctx())
    assert "Curated" in html
    assert '<span class="badge rel">8/10</span>' in html
    assert '<span class="badge score">7.5</span>' in html
    assert '<span class="badge ch">ai</span>' in html
    assert '<span class="badge surf">1 surface</span>' in html  # singular


@pytest.mark.integration
def test_dashboard_surface_plural_and_paywall_badge():
    ctx = _dashboard_ctx()
    ctx["items"][0]["surface_count"] = 3
    ctx["items"][0]["paywalled"] = 1
    html = render.render_dashboard(ctx)
    assert '<span class="badge surf">3 surfaces</span>' in html  # plural
    assert '<span class="badge paywall">paywalled</span>' in html


@pytest.mark.integration
def test_dashboard_uncurated_heading_and_excerpt():
    ctx = _dashboard_ctx()
    ctx["items"] = [
        dict(
            id=2,
            curated=False,
            score=4.2,
            channel_list=[],
            paywalled=0,
            surface_count=2,
            link="https://example.com/x",
            title="Uncurated title",
            excerpt="X" * 400,  # will be truncated to 300 chars
            surfaces=[],
        )
    ]
    html = render.render_dashboard(ctx)
    assert "Scored (awaiting curation)" in html
    assert '<article class="card uncurated">' in html
    # excerpt[:300] -> exactly 300 X's, not 400.
    assert "X" * 300 in html
    assert "X" * 301 not in html


@pytest.mark.integration
def test_dashboard_empty_items():
    html = render.render_dashboard(_dashboard_ctx(items=[]))
    assert "Scored (awaiting curation)" in html
    assert '<span class="count">0</span>' in html
    assert 'class="empty"' in html
    assert "No items yet." in html


@pytest.mark.integration
def test_dashboard_original_link_when_source_differs_from_link():
    ctx = _dashboard_ctx()
    ctx["items"][0]["link"] = "https://reader.example/story"
    ctx["items"][0]["source_url"] = "https://origin.example/story"
    html = render.render_dashboard(ctx)
    assert '<a href="https://origin.example/story" class="orig">original</a>' in html


@pytest.mark.integration
def test_dashboard_no_original_link_when_source_equals_link():
    ctx = _dashboard_ctx()
    ctx["items"][0]["link"] = "https://same.example/story"
    ctx["items"][0]["source_url"] = "https://same.example/story"
    html = render.render_dashboard(ctx)
    assert 'class="orig"' not in html


@pytest.mark.integration
def test_dashboard_digest_draft_section():
    ctx = _dashboard_ctx()
    ctx["digest"] = dict(
        kind="weekly",
        period_key="2026-W27",
        promoted=0,
        body_html="<p>Digest <b>body</b>.</p>",
        staged_path="/tmp/digest.html",
    )
    html = render.render_dashboard(ctx)
    assert "Weekly digest — 2026-W27" in html
    assert '<span class="badge">draft</span>' in html  # not promoted
    assert "<p>Digest <b>body</b>.</p>" in html  # body_html|safe, not escaped
    assert "<code>/tmp/digest.html</code>" in html


@pytest.mark.integration
def test_dashboard_digest_promoted_no_draft_badge():
    ctx = _dashboard_ctx()
    ctx["digest"] = dict(
        kind="monthly",
        period_key="2026-07",
        promoted=1,
        body_html="<p>ok</p>",
        staged_path="/tmp/m.html",
    )
    html = render.render_dashboard(ctx)
    assert "Monthly digest — 2026-07" in html
    assert "draft" not in html


@pytest.mark.integration
def test_dashboard_no_digest_section_when_absent():
    html = render.render_dashboard(_dashboard_ctx())  # no 'digest' key
    assert 'class="digest"' not in html


@pytest.mark.integration
def test_dashboard_escapes_item_title():
    ctx = _dashboard_ctx()
    ctx["items"][0]["title"] = '<script>alert(1)</script> & more'
    html = render.render_dashboard(ctx)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&amp; more" in html
