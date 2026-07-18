"""Tests for signalpipe.library — deterministic entity wiki builder."""

from __future__ import annotations

import datetime

from signalpipe import library as lib

NOW = datetime.datetime(2026, 7, 4, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - datetime.timedelta(days=days_ago)).isoformat()


def _story(seed, i, title, days_ago=2, source_url=None, read_url=None):
    cid = seed.cluster(title=title, canonical_url="https://ex.example/%d" % i)
    seed.article(cid, source_url=source_url or "https://ex.example/%d" % i,
                 read_url=read_url or "https://ex.example/%d" % i)
    seed.curation(cid, status="done", skip=0, relevance_score=9,
                  why_it_matters="Matters.", curated_at=_iso(days_ago))
    return cid


def test_refresh_builds_pages_and_index(conn, seed, tmp_path):
    _story(seed, 1, "Anthropic ships Claude 5")
    _story(seed, 2, "OpenAI previews a new model")
    _story(seed, 3, "Nvidia announces a GPU")

    out = lib.refresh(conn, tmp_path, k=2, now=NOW)

    # <= k entity pages rebuilt this run
    pages = [p for p in out["kb_writes"] if p.endswith(".md")]
    assert len(pages) == 2
    assert "kb/library/anthropic.md" in out["kb_writes"]
    # anthropic's page carries a timeline entry
    assert "## Timeline" in out["kb_writes"]["kb/library/anthropic.md"]
    assert "Claude 5" in out["kb_writes"]["kb/library/anthropic.md"]
    # index covers the entities registered so far (grows a few per run)
    idx_slugs = {e["slug"] for e in out["index"]}
    assert idx_slugs == {"anthropic", "openai"}
    # registry grew and persisted
    reg = lib.load_registry(tmp_path)
    assert {"anthropic", "openai"} <= {e["slug"] for e in reg}


def test_no_person_types_proposed(conn, seed):
    _story(seed, 1, "Anthropic and OpenAI and Nvidia and Rust lang news")
    proposed = lib.propose_entities(conn, [], k=99, now=NOW)
    assert all(p["type"] in lib.ALLOWED_TYPES for p in proposed)


def test_archive_links_scrubbed():
    entity = {"slug": "eu-ai-act", "name": "EU AI Act", "type": "standard",
              "aliases": ["ai act"]}
    stories = [{
        "title": "AI Act enforcement begins", "curated_at": _iso(1),
        "canonical_url": "https://archive.today/abcd",
        "source_url": "https://archive.today/abcd", "read_url": None,
        "why_it_matters": "Live now.",
    }]
    tl = lib._entity_timeline(entity, stories)
    body = lib._render_body(entity, tl)
    assert not lib.has_archive_link(body)
    # with no clean link, the title renders without a hyperlink
    assert "AI Act enforcement begins" in body


def test_k_respected_and_registry_dedups(conn, seed, tmp_path):
    for i, t in enumerate(["Anthropic news", "OpenAI news", "Nvidia news"]):
        _story(seed, i, t)
    lib.refresh(conn, tmp_path, k=1, now=NOW)
    reg1 = lib.load_registry(tmp_path)
    assert len(reg1) == 1  # grew by k
    lib.refresh(conn, tmp_path, k=1, now=NOW)
    reg2 = lib.load_registry(tmp_path)
    assert len(reg2) == 2  # +1, no dup of the first
    assert len({e["slug"] for e in reg2}) == 2


def test_empty_db_yields_nothing(conn, tmp_path):
    out = lib.refresh(conn, tmp_path, k=3, now=NOW)
    assert out["kb_writes"] == {}
    assert out["entities"] == []
    assert out["index"] == []
