"""Unit + integration tests for ``signalpipe.ingest.bulk_import``.

Bulk source expansion harvests candidate feeds from OPML / markdown / inline
lists, probe-verifies them in waves through the registry harness, applies
freshness/quality gates with auto-tiering (tier 1 is never auto-assigned), and
merges survivors into the registry + DB with per-entry resume checkpoints.

Hermeticity strategy (NO real network / clock is ever touched):

* Pure helpers (`_is_transient_reason`, `_host`, `_chunks`, the candidate
  harvesters) are exercised directly with literal inputs.
* Time-dependent helpers (`_fresh_enough`, `auto_tier`, `_apply_quality_gates`)
  run against a frozen ``bulk_import._now`` so freshness is deterministic.
* Checkpoints write under ``cfg.db_path.parent`` — the ``cfg`` fixture repoints
  ``db_path`` at pytest ``tmp_path`` (outside iCloud), so all FS writes are tmp.
* ``run()`` orchestration is driven with an *inline* manifest (which skips the
  list-fetch ``PoliteClient`` entirely) and scripted fakes for
  ``registry.probe_candidates`` / ``merge_into_registry`` / ``seed``. One test
  exercises the markdown list-fetch path with a fake ``PoliteClient``. One
  end-to-end test patches ONLY ``probe_candidates`` and lets merge+seed hit a
  tmp sources.json + tmp DB.
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Callable, Dict, List, Optional

import pytest

import signalpipe.db as db_mod
from signalpipe.ingest import bulk_import
from signalpipe.ingest import registry as registry_mod
from signalpipe.ingest.fetch_http import FetchResult
from signalpipe.models import SourceSpec

# --------------------------------------------------------------------------- #
# Frozen clock + fixed ISO instants (relative to FROZEN)
# --------------------------------------------------------------------------- #
FROZEN = datetime.datetime(2026, 7, 4, 12, 0, 0, tzinfo=datetime.timezone.utc)

FRESH_1D = "2026-07-03T12:00:00+00:00"      # 1 day old
FRESH_20D = "2026-06-14T12:00:00+00:00"     # 20 days old
FRESH_30D = "2026-06-04T12:00:00+00:00"     # exactly 30 days old
AGE_31D = "2026-06-03T12:00:00+00:00"       # 31 days old
AGE_60D = "2026-05-05T12:00:00+00:00"       # 60 days old (< STALE_DAYS)
DORMANT = "2024-01-01T12:00:00+00:00"       # > STALE_DAYS old


@pytest.fixture
def frozen_now(monkeypatch):
    """Freeze ``bulk_import._now`` so freshness math is deterministic."""
    monkeypatch.setattr(bulk_import, "_now", lambda: FROZEN)
    return FROZEN


# --------------------------------------------------------------------------- #
# Local helpers
# --------------------------------------------------------------------------- #
def _verified_feed(host: str, name: str = "Feed", category: str = "tech_news",
                   entries: int = 15, latest: str = FRESH_1D, tier: int = 2,
                   **over: Any) -> Dict[str, Any]:
    """A verified-candidate dict shaped like ``registry.probe_candidates``
    output (the shape ``_apply_quality_gates`` + ``merge_into_registry`` read)."""
    d: Dict[str, Any] = {
        "name": name,
        "slug": registry_mod.slugify(name),
        "type": "rss",
        "url": "https://%s/feed.xml" % host,
        "homepage": "https://%s" % host,
        "category": category,
        "topics": ["ai"],
        "tier": tier,
        "reputation": 1.0,
        "cadence_min": 240,
        "paywalled": False,
        "why": "test",
        "entries": entries,
        "latest_entry": latest,
    }
    d.update(over)
    return d


def _inline_cand(host: str, name: Optional[str] = None,
                 feed: bool = True, **over: Any) -> Dict[str, Any]:
    """An inline manifest candidate keyed on ``host`` (feed_url or homepage)."""
    c: Dict[str, Any] = {"name": name or host}
    if feed:
        c["feed_url"] = "https://%s/feed.xml" % host
    else:
        c["homepage"] = "https://%s/" % host
    c.update(over)
    return c


def _make_probe(mapping: Dict[str, tuple], calls: Optional[List] = None) -> Callable:
    """Fake ``registry.probe_candidates``. ``mapping`` maps a candidate host to
    either ``("v", verified_dict)`` or ``("r", reason)``. Unknown hosts reject
    definitively ('no valid feed'). Records each wave into ``calls`` if given."""

    def _probe(cfg, wave, max_workers: int = 8):
        if calls is not None:
            calls.append(list(wave))
        verified: List[Dict] = []
        rejected: List[Dict] = []
        for c in wave:
            url = c.get("feed_url") or c.get("homepage") or ""
            h = bulk_import._host(url)
            action = mapping.get(h, ("r", "no valid feed"))
            if action[0] == "v":
                verified.append(dict(action[1]))
            else:
                rejected.append({"name": c.get("name"), "url": url,
                                 "reason": action[1]})
        return verified, rejected

    return _probe


def _install_run_fakes(monkeypatch, probe: Callable,
                       merge_records: Optional[List] = None,
                       seed_records: Optional[List] = None) -> None:
    def _merge(cfg, kept):
        if merge_records is not None:
            merge_records.append(list(kept))
        return len(kept)

    def _seed(cfg):
        if seed_records is not None:
            seed_records.append(cfg)
        return 0

    monkeypatch.setattr(bulk_import.registry, "probe_candidates", probe)
    monkeypatch.setattr(bulk_import.registry, "merge_into_registry", _merge)
    monkeypatch.setattr(bulk_import.registry, "seed", _seed)


def _point_sources_at_tmp(cfg, tmp_path) -> None:
    """Repoint the registry files at a fresh tmp dir so ``registry.load_specs``
    (used by ``_existing_hosts``) never reads the real committed sources.json,
    and ``merge_into_registry`` (end-to-end) writes only under tmp."""
    reg = tmp_path / "reg"
    cfg.data["sources"] = {
        "registry": str(reg / "sources.json"),
        "opml": str(reg / "sources.opml"),
    }


def _write_manifest(tmp_path, data: Dict[str, Any], name: str = "manifest.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


# =========================================================================== #
# Constants — sanity checks (guard against silent product-side drift)
# =========================================================================== #
def test_module_constants():
    assert bulk_import.STALE_DAYS == 365
    assert bulk_import.TIER2_FRESH_DAYS == 30
    assert bulk_import.TIER2_MIN_ENTRIES == 10
    assert bulk_import.TIER_CADENCE == {1: 120, 2: 240, 3: 720}
    assert bulk_import.TIER_REPUTATION == {1: 1.2, 2: 1.0, 3: 0.8}
    assert "tech_news" in bulk_import.TIER2_CATEGORIES
    assert "physics" in bulk_import.TIER2_CATEGORIES
    assert "feeds.bbci.co.uk" in bulk_import.SHARED_HOSTS
    assert "feeds.feedburner.com" in bulk_import.SHARED_HOSTS


@pytest.mark.parametrize("url", [
    "https://github.com/foo/bar",
    "https://img.shields.io/badge/x",
    "https://example.com/badge.svg",
    "https://cdn.example.com/logo.png",
    "https://cdn.example.com/pic.jpeg",
    "https://cdn.example.com/pic.jpg",
    "https://cdn.example.com/anim.gif",
    "https://twitter.com/someone",
    "https://x.com/someone",
    "https://t.me/channel",
    "https://discord.gg/abc",
    "https://youtube.com/watch?v=abc",
])
def test_skip_url_re_matches_junk(url):
    assert bulk_import.SKIP_URL_RE.search(url) is not None


@pytest.mark.parametrize("url", [
    "https://example.com/feed.xml",
    "https://blog.example.org/index.rss",
    "https://news.example.net/",
])
def test_skip_url_re_allows_real_feeds(url):
    assert bulk_import.SKIP_URL_RE.search(url) is None


# =========================================================================== #
# _is_transient_reason — definitive vs transient truth table (pure)
# =========================================================================== #
@pytest.mark.parametrize("reason,expected", [
    (None, True),
    ("", True),
    ("   ", True),
    # Definitive content verdicts -> NOT transient.
    ("no valid feed found", False),
    ("No Valid Feed", False),                       # case-insensitive
    ("duplicate/dead", False),
    ("domain already registered (example.com)", False),
    ("no url", False),
    # Retryable HTTP statuses -> transient.
    ("http 408", True),
    ("http 429", True),
    ("http 500", True),
    ("http 503", True),
    ("http 504", True),
    ("HTTP 500", True),                             # uppercased then lowered
    # Definitive HTTP statuses -> NOT transient.
    ("http 404", False),
    ("http 403", False),
    ("http 410", False),
    ("http 400", False),                            # <500, not 408/429
    # Transport/exception text -> transient.
    ("ConnectTimeout: host unreachable", True),
    ("fetch failed", True),
    ("some other error", True),
    ("http500", True),                              # no space -> not an http verdict
])
def test_is_transient_reason(reason, expected):
    assert bulk_import._is_transient_reason(reason) is expected


# =========================================================================== #
# _host — hostname extraction (pure)
# =========================================================================== #
@pytest.mark.parametrize("url,expected", [
    ("https://Example.COM/feed.xml", "example.com"),
    ("http://sub.host.example.org:8080/x", "sub.host.example.org"),
    ("https://feeds.bbci.co.uk/news/rss.xml", "feeds.bbci.co.uk"),
    ("example.com/feed", ""),                        # scheme-less -> no hostname
    ("not a url at all", ""),
    ("", ""),
    ("http://[::1", ""),                             # urlsplit raises ValueError
])
def test_host(url, expected):
    assert bulk_import._host(url) == expected


# =========================================================================== #
# _fresh_enough — freshness (frozen clock)
# =========================================================================== #
@pytest.mark.parametrize("latest,max_age,expected", [
    (FRESH_1D, 30, True),
    (FRESH_20D, 30, True),
    (FRESH_30D, 30, True),                           # boundary: exactly 30 days
    (AGE_31D, 30, False),                            # just over the window
    (AGE_60D, 30, False),
    (AGE_60D, 365, True),                            # within a year
    (DORMANT, 365, False),                           # dormant
    (None, 30, False),
    ("", 30, False),
    ("2026-07-03T12:00:00Z", 30, True),              # 'Z' suffix accepted
    ("2026-07-03T12:00:00", 30, True),               # naive -> assume UTC
    ("garbage", 30, False),                          # unparseable
    ("not-a-date", 365, False),
])
def test_fresh_enough(frozen_now, latest, max_age, expected):
    assert bulk_import._fresh_enough(latest, max_age) is expected


def test_fresh_enough_future_date_is_fresh(frozen_now):
    # A timestamp after _now yields a negative delta whose .days <= max_age.
    assert bulk_import._fresh_enough("2026-08-01T12:00:00+00:00", 30) is True


def test_now_returns_aware_utc():
    # The unfrozen clock: a tz-aware UTC datetime that tracks real wall-clock
    # 'now' (not a frozen/wrong-offset stub).
    before = datetime.datetime.now(datetime.timezone.utc)
    got = bulk_import._now()
    after = datetime.datetime.now(datetime.timezone.utc)
    assert got.tzinfo is datetime.timezone.utc
    assert isinstance(got, datetime.datetime)
    assert before <= got <= after


# =========================================================================== #
# auto_tier — tier from evidence (frozen clock); never tier 1
# =========================================================================== #
def test_auto_tier_eligible_returns_2(frozen_now):
    v = {"category": "tech_news", "entries": 10, "latest_entry": FRESH_1D}
    assert bulk_import.auto_tier(v) == 2


@pytest.mark.parametrize("category", sorted(bulk_import.TIER2_CATEGORIES))
def test_auto_tier_every_tier2_category(frozen_now, category):
    v = {"category": category, "entries": 12, "latest_entry": FRESH_20D}
    assert bulk_import.auto_tier(v) == 2


@pytest.mark.parametrize("v", [
    {"category": "uncategorized", "entries": 50, "latest_entry": FRESH_1D},  # category
    {"category": "tech_news", "entries": 9, "latest_entry": FRESH_1D},        # entries
    {"category": "tech_news", "entries": 50, "latest_entry": AGE_60D},        # stale
    {"category": "tech_news", "entries": 50, "latest_entry": None},           # no date
    {"category": "tech_news", "latest_entry": FRESH_1D},                      # entries missing
])
def test_auto_tier_falls_back_to_3(frozen_now, v):
    assert bulk_import.auto_tier(v) == 3


def test_auto_tier_never_returns_1(frozen_now):
    combos = [
        {"category": "tech_news", "entries": 100, "latest_entry": FRESH_1D},
        {"category": "uncategorized", "entries": 0, "latest_entry": None},
        {"category": "physics", "entries": 10, "latest_entry": FRESH_30D},
    ]
    assert all(bulk_import.auto_tier(v) in (2, 3) for v in combos)


# =========================================================================== #
# _apply_quality_gates — drop dormant, auto-tier, demote-only clamp
# =========================================================================== #
def test_apply_quality_gates_drops_dormant(frozen_now):
    kept, dropped = bulk_import._apply_quality_gates(
        [_verified_feed("dormant.example.com", latest=DORMANT)]
    )
    assert kept == []
    assert dropped == 1


def test_apply_quality_gates_keeps_eligible_tier2(frozen_now):
    kept, dropped = bulk_import._apply_quality_gates(
        [_verified_feed("t2.example.com", category="tech_news", entries=20,
                        latest=FRESH_1D, tier=2)]
    )
    assert dropped == 0
    (v,) = kept
    assert v["tier"] == 2
    assert v["cadence_min"] == bulk_import.TIER_CADENCE[2] == 240
    assert v["reputation"] == bulk_import.TIER_REPUTATION[2] == 1.0


def test_apply_quality_gates_declared_tier1_clamped_to_2(frozen_now):
    # Evidence is NOT tier-2-eligible (auto=3); declared tier 1 -> min(3,1)=1
    # -> clamp up to 2. Evidence can never promote a feed to tier 1.
    kept, _ = bulk_import._apply_quality_gates(
        [_verified_feed("t1.example.com", category="uncategorized", entries=3,
                        latest=FRESH_1D, tier=1)]
    )
    (v,) = kept
    assert v["tier"] == 2
    assert v["cadence_min"] == 240


def test_apply_quality_gates_non_eligible_declared3_stays_3(frozen_now):
    kept, dropped = bulk_import._apply_quality_gates(
        [_verified_feed("t3.example.com", category="uncategorized", entries=4,
                        latest=AGE_60D, tier=3)]
    )
    assert dropped == 0
    (v,) = kept
    assert v["tier"] == 3
    assert v["cadence_min"] == bulk_import.TIER_CADENCE[3] == 720
    assert v["reputation"] == bulk_import.TIER_REPUTATION[3] == 0.8


def test_apply_quality_gates_eligible_declared3_promotes_to_2(frozen_now):
    # auto=2, declared=3 -> min(2,3)=2 (evidence demotes the declared 3 up to 2).
    kept, _ = bulk_import._apply_quality_gates(
        [_verified_feed("p.example.com", category="research", entries=40,
                        latest=FRESH_1D, tier=3)]
    )
    assert kept[0]["tier"] == 2


def test_apply_quality_gates_output_tier_never_1_and_counts(frozen_now):
    feeds = [
        _verified_feed("a.example.com", category="tech_news", entries=20,
                       latest=FRESH_1D, tier=2),
        _verified_feed("b.example.com", category="uncategorized", entries=2,
                       latest=AGE_60D, tier=3),
        _verified_feed("c.example.com", latest=DORMANT),           # dropped
        _verified_feed("d.example.com", category="science", entries=1,
                       latest=FRESH_1D, tier=1),                    # clamp to 2
    ]
    kept, dropped = bulk_import._apply_quality_gates(feeds)
    assert dropped == 1
    assert len(kept) == 3
    for v in kept:
        assert v["tier"] in (2, 3)
        assert v["cadence_min"] == bulk_import.TIER_CADENCE[v["tier"]]
        assert v["reputation"] == bulk_import.TIER_REPUTATION[v["tier"]]


def test_apply_quality_gates_missing_tier_defaults_to_3(frozen_now):
    v = _verified_feed("m.example.com", category="uncategorized", entries=1,
                       latest=FRESH_1D)
    del v["tier"]
    kept, _ = bulk_import._apply_quality_gates([v])
    assert kept[0]["tier"] == 3
    assert kept[0]["cadence_min"] == bulk_import.TIER_CADENCE[3] == 720
    assert kept[0]["reputation"] == bulk_import.TIER_REPUTATION[3] == 0.8


# =========================================================================== #
# _chunks — fixed-size windowing (pure)
# =========================================================================== #
@pytest.mark.parametrize("seq,n,expected", [
    ([1, 2, 3, 4], 2, [[1, 2], [3, 4]]),
    ([1, 2, 3, 4, 5], 2, [[1, 2], [3, 4], [5]]),
    ([1, 2, 3], 5, [[1, 2, 3]]),
    ([], 3, []),
    ([1], 1, [[1]]),
])
def test_chunks(seq, n, expected):
    assert list(bulk_import._chunks(seq, n)) == expected


# =========================================================================== #
# _candidates_from_markdown — classification + skip filtering (pure)
# =========================================================================== #
_MARKDOWN = b"""# Awesome feeds

- [Alpha Blog](https://alpha.example.com/feed/) great blog
- [Beta News](https://beta.example.com/index.xml)
- [Gamma Home](https://gamma.example.com/) homepage only
- [Repo](https://github.com/foo/bar) skip repo link
- [Badge](https://img.shields.io/badge/build.svg) skip badge
- [Bird](https://twitter.com/someone) skip social
- [Logo](https://cdn.example.com/logo.png) skip image
Not a list line [Ignored](https://ignored.example.com/feed/)
"""


def test_candidates_from_markdown_classifies_and_skips():
    entry = {"name": "awesome-list", "category": "science",
             "topics": ["science"], "tier": 2, "cadence_min": 240,
             "paywalled": True}
    cands = bulk_import._candidates_from_markdown(_MARKDOWN, entry)

    by_name = {c["name"]: c for c in cands}
    # github / shields / twitter / png links are skipped; the non-list line is
    # never parsed by registry.parse_markdown_list.
    assert set(by_name) == {"Alpha Blog", "Beta News", "Gamma Home"}

    assert by_name["Alpha Blog"]["feed_url"] == "https://alpha.example.com/feed/"
    assert "homepage" not in by_name["Alpha Blog"]
    assert by_name["Beta News"]["feed_url"] == "https://beta.example.com/index.xml"
    assert by_name["Gamma Home"]["homepage"] == "https://gamma.example.com/"
    assert "feed_url" not in by_name["Gamma Home"]

    # Entry metadata + 'why' propagate onto every candidate.
    for c in cands:
        assert c["category"] == "science"
        assert c["topics"] == ["science"]
        assert c["tier"] == 2
        assert c["cadence_min"] == 240
        assert c["paywalled"] is True
        assert c["why"] == "bulk: awesome-list"


def test_candidates_from_markdown_defaults_when_entry_sparse():
    cands = bulk_import._candidates_from_markdown(
        b"- [X](https://x.example.com/rss)", {}
    )
    (c,) = cands
    assert c["category"] == "uncategorized"
    assert c["topics"] == []
    assert c["tier"] == 3
    assert c["cadence_min"] == 720
    assert c["paywalled"] is False
    assert c["why"] == "bulk: list"
    assert c["feed_url"] == "https://x.example.com/rss"


def test_candidates_from_markdown_empty():
    assert bulk_import._candidates_from_markdown(b"no links here", {}) == []


# =========================================================================== #
# _candidates_from_opml — parse OPML bytes -> candidate dicts
# =========================================================================== #
_OPML = b"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>list</title></head>
  <body>
    <outline text="Cat">
      <outline type="rss" text="Feed One"
               xmlUrl="https://one.example.com/feed.xml"
               htmlUrl="https://one.example.com/"/>
      <outline type="rss" title="Feed Two"
               xmlUrl="https://two.example.com/atom.xml"/>
    </outline>
  </body>
</opml>
"""


def test_candidates_from_opml_maps_outlines():
    entry = {"name": "eng-blogs", "category": "expert_blogs",
             "topics": ["devtools"], "tier": 3, "cadence_min": 720,
             "paywalled": False}
    cands = bulk_import._candidates_from_opml(_OPML, entry)

    assert len(cands) == 2
    one, two = cands
    assert one["name"] == "Feed One"
    assert one["feed_url"] == "https://one.example.com/feed.xml"
    assert one["homepage"] == "https://one.example.com/"
    assert two["name"] == "Feed Two"
    assert two["feed_url"] == "https://two.example.com/atom.xml"
    assert two["homepage"] is None
    for c in cands:
        assert c["category"] == "expert_blogs"
        assert c["topics"] == ["devtools"]
        assert c["tier"] == 3
        assert c["cadence_min"] == 720
        assert c["paywalled"] is False
        assert c["why"] == "bulk: eng-blogs"


def test_candidates_from_opml_parse_error_returns_empty(monkeypatch, capsys):
    def _boom(content):
        raise ValueError("broken opml")

    monkeypatch.setattr(bulk_import.registry, "parse_opml", _boom)
    out = bulk_import._candidates_from_opml(b"<opml/>", {"name": "bad-list"})
    assert out == []
    err = capsys.readouterr().err
    assert "opml parse error" in err
    assert "bad-list" in err


# =========================================================================== #
# Checkpoints — round-trip, corruption, path sanitization (tmp FS)
# =========================================================================== #
@pytest.mark.integration
def test_checkpoint_path_sanitizes_entry_name(cfg):
    p = bulk_import._checkpoint_path(cfg, "My Entry/Two Names!")
    assert p.name == "my-entry-two-names.json"
    assert p.parent == cfg.db_path.parent / "bulk"


@pytest.mark.integration
def test_bulk_dir_created_under_db_parent(cfg):
    d = bulk_import._bulk_dir(cfg)
    assert d == cfg.db_path.parent / "bulk"
    assert d.is_dir()


@pytest.mark.integration
def test_checkpoint_round_trip(cfg):
    state = {"hosts_done": ["a.example.com", "b.example.com"],
             "verified": 3, "rejected": 1, "imported": 2, "complete": True}
    bulk_import._save_checkpoint(cfg, "round trip", state)
    assert bulk_import._load_checkpoint(cfg, "round trip") == state


@pytest.mark.integration
def test_load_checkpoint_default_when_absent(cfg):
    state = bulk_import._load_checkpoint(cfg, "never-written")
    assert state == {"hosts_done": [], "verified": 0, "rejected": 0,
                     "imported": 0, "complete": False}


@pytest.mark.integration
def test_load_checkpoint_corrupt_json_returns_default(cfg):
    path = bulk_import._checkpoint_path(cfg, "corrupt")
    path.write_text("{ this is not json ]")
    state = bulk_import._load_checkpoint(cfg, "corrupt")
    # Corruption falls back to the full pristine default, not a partial dict.
    assert state == {"hosts_done": [], "verified": 0, "rejected": 0,
                     "imported": 0, "complete": False}


# =========================================================================== #
# _existing_hosts — registry hosts, SHARED_HOSTS excluded
# =========================================================================== #
def test_existing_hosts_excludes_shared_hosts(monkeypatch, cfg):
    specs = [
        SourceSpec(slug="a", name="A", type="rss",
                   url="https://a.example.com/feed.xml",
                   homepage="https://a.example.com"),
        # feed host is SHARED (excluded) but the homepage host is kept.
        SourceSpec(slug="bbc", name="BBC", type="rss",
                   url="https://feeds.bbci.co.uk/news/rss.xml",
                   homepage="https://www.bbc.co.uk"),
        SourceSpec(slug="empty", name="Empty", type="rss", url="",
                   homepage=None),
    ]
    monkeypatch.setattr(bulk_import.registry, "load_specs", lambda c: specs)
    hosts = bulk_import._existing_hosts(cfg)
    assert hosts == {"a.example.com", "www.bbc.co.uk"}
    assert "feeds.bbci.co.uk" not in hosts


def test_existing_hosts_empty_registry(monkeypatch, cfg):
    monkeypatch.setattr(bulk_import.registry, "load_specs", lambda c: [])
    assert bulk_import._existing_hosts(cfg) == set()


# =========================================================================== #
# run() — control-flow returns
# =========================================================================== #
@pytest.mark.integration
def test_run_missing_manifest_returns_2(cfg, tmp_path, capsys):
    missing = tmp_path / "does-not-exist.json"
    assert bulk_import.run(cfg, manifest_path=missing) == 2
    assert "manifest not found" in capsys.readouterr().err


@pytest.mark.integration
def test_run_only_entry_names_nothing_returns_2(cfg, tmp_path, capsys):
    mp = _write_manifest(tmp_path, {"inline": [_inline_cand("x.example.com")]})
    assert bulk_import.run(cfg, manifest_path=mp, only_entry="nope") == 2
    assert "no manifest entry named 'nope'" in capsys.readouterr().err


# =========================================================================== #
# run() — inline manifest, fully faked deps
# =========================================================================== #
@pytest.mark.integration
def test_run_inline_basic_import(cfg, tmp_path, monkeypatch, frozen_now, capsys):
    _point_sources_at_tmp(cfg, tmp_path)
    mp = _write_manifest(tmp_path, {"inline": [_inline_cand("keep.example.com")]})

    seed_records: List = []
    probe = _make_probe({"keep.example.com": ("v", _verified_feed("keep.example.com"))})
    _install_run_fakes(monkeypatch, probe, seed_records=seed_records)

    rc = bulk_import.run(cfg, manifest_path=mp)
    assert rc == 0

    out = capsys.readouterr().out
    assert "1 candidates, 1 verified, 1 imported" in out

    # merge added -> seed invoked exactly once.
    assert len(seed_records) == 1

    # Checkpoint records the outcome and completes (no transient failures).
    state = bulk_import._load_checkpoint(cfg, "inline")
    assert state["complete"] is True
    assert state["verified"] == 1
    assert state["imported"] == 1
    assert state["rejected"] == 0
    assert state["hosts_done"] == ["keep.example.com"]


@pytest.mark.integration
def test_run_inline_within_run_dedupe(cfg, tmp_path, monkeypatch, frozen_now, capsys):
    _point_sources_at_tmp(cfg, tmp_path)
    # Two candidates share a host -> the second is a same-run duplicate.
    inline = [
        {"name": "dup-a", "feed_url": "https://dup.example.com/a.xml"},
        {"name": "dup-b", "feed_url": "https://dup.example.com/b.xml"},
    ]
    mp = _write_manifest(tmp_path, {"inline": inline})

    calls: List = []
    probe = _make_probe({"dup.example.com": ("v", _verified_feed("dup.example.com"))},
                        calls=calls)
    _install_run_fakes(monkeypatch, probe)

    assert bulk_import.run(cfg, manifest_path=mp) == 0
    out = capsys.readouterr().out
    assert "1 duplicate hosts skipped" in out
    # Only one candidate survived dedupe into the probe wave.
    assert len(calls) == 1 and len(calls[0]) == 1


@pytest.mark.integration
def test_run_transient_leaves_entry_incomplete(cfg, tmp_path, monkeypatch,
                                               frozen_now, capsys):
    _point_sources_at_tmp(cfg, tmp_path)
    inline = [_inline_cand("trans.example.com", name="T"),
              _inline_cand("defin.example.com", name="D")]
    mp = _write_manifest(tmp_path, {"inline": inline})

    probe = _make_probe({
        "trans.example.com": ("r", "http 503"),   # transient -> retry
        "defin.example.com": ("r", "no valid feed"),  # definitive -> done
    })
    _install_run_fakes(monkeypatch, probe)

    assert bulk_import.run(cfg, manifest_path=mp) == 0
    out = capsys.readouterr().out
    assert "transient-failed (will retry)" in out
    assert "entry left incomplete for retry" in out

    state = bulk_import._load_checkpoint(cfg, "inline")
    assert state["complete"] is False
    # The definitive host is checkpointed; the transient host is NOT.
    assert state["hosts_done"] == ["defin.example.com"]
    assert state["verified"] == 0
    assert state["rejected"] == 2
    assert state["imported"] == 0


@pytest.mark.integration
def test_run_transient_rejection_without_url_still_completes(
        cfg, tmp_path, monkeypatch, frozen_now):
    _point_sources_at_tmp(cfg, tmp_path)
    mp = _write_manifest(tmp_path, {"inline": [_inline_cand("keep.example.com")]})

    def _probe(cfg, wave, max_workers=8):
        # Transient reason but a URL with no parseable host: no host lands in
        # transient_hosts, so the candidate is still checkpointed as done.
        return [], [{"name": "x", "url": "", "reason": "http 500"}]

    monkeypatch.setattr(bulk_import.registry, "probe_candidates", _probe)
    monkeypatch.setattr(bulk_import.registry, "merge_into_registry",
                        lambda c, k: 0)
    monkeypatch.setattr(bulk_import.registry, "seed", lambda c: 0)

    assert bulk_import.run(cfg, manifest_path=mp) == 0
    state = bulk_import._load_checkpoint(cfg, "inline")
    assert state["complete"] is True
    assert state["hosts_done"] == ["keep.example.com"]


@pytest.mark.integration
def test_run_resume_skips_completed_entry(cfg, tmp_path, monkeypatch,
                                          frozen_now, capsys):
    _point_sources_at_tmp(cfg, tmp_path)
    mp = _write_manifest(tmp_path, {"inline": [_inline_cand("keep.example.com")]})
    # Pre-existing checkpoint marks the (auto-named) 'inline' entry complete.
    bulk_import._save_checkpoint(cfg, "inline", {
        "hosts_done": [], "verified": 5, "rejected": 0, "imported": 5,
        "complete": True})

    calls: List = []
    probe = _make_probe({}, calls=calls)
    _install_run_fakes(monkeypatch, probe)

    assert bulk_import.run(cfg, manifest_path=mp) == 0
    out = capsys.readouterr().out
    assert "checkpoint says complete" in out
    assert calls == []                       # probe never ran
    assert "0 candidates, 0 verified" in out


@pytest.mark.integration
def test_run_resume_filters_hosts_done(cfg, tmp_path, monkeypatch, frozen_now):
    _point_sources_at_tmp(cfg, tmp_path)
    inline = [_inline_cand("done.example.com", name="already"),
              _inline_cand("new.example.com", name="fresh")]
    mp = _write_manifest(tmp_path, {"inline": inline})
    bulk_import._save_checkpoint(cfg, "inline", {
        "hosts_done": ["done.example.com"], "verified": 1, "rejected": 0,
        "imported": 1, "complete": False})

    calls: List = []
    probe = _make_probe({"new.example.com": ("v", _verified_feed("new.example.com"))},
                        calls=calls)
    _install_run_fakes(monkeypatch, probe)

    assert bulk_import.run(cfg, manifest_path=mp) == 0
    # Only the not-yet-done host reaches the prober.
    assert len(calls) == 1
    (wave,) = calls
    assert [c["name"] for c in wave] == ["fresh"]

    # Resume accumulates onto the checkpoint (1 pre-existing + 1 new), and the
    # newly-probed host joins the already-done host.
    state = bulk_import._load_checkpoint(cfg, "inline")
    assert state["verified"] == 2
    assert state["imported"] == 2
    assert state["rejected"] == 0
    assert state["hosts_done"] == ["done.example.com", "new.example.com"]
    assert state["complete"] is True


@pytest.mark.integration
def test_run_no_resume_ignores_checkpoint(cfg, tmp_path, monkeypatch, frozen_now):
    _point_sources_at_tmp(cfg, tmp_path)
    mp = _write_manifest(tmp_path, {"inline": [_inline_cand("keep.example.com")]})
    bulk_import._save_checkpoint(cfg, "inline", {
        "hosts_done": ["keep.example.com"], "verified": 9, "rejected": 0,
        "imported": 9, "complete": True})

    calls: List = []
    probe = _make_probe({"keep.example.com": ("v", _verified_feed("keep.example.com"))},
                        calls=calls)
    _install_run_fakes(monkeypatch, probe)

    assert bulk_import.run(cfg, manifest_path=mp, no_resume=True) == 0
    # The complete checkpoint is ignored: the candidate is processed afresh.
    assert len(calls) == 1 and len(calls[0]) == 1
    state = bulk_import._load_checkpoint(cfg, "inline")
    assert state["complete"] is True
    assert state["verified"] == 1        # reset counters, not the stale 9
    assert state["imported"] == 1        # imported also reset (was 9)
    assert state["hosts_done"] == ["keep.example.com"]


@pytest.mark.integration
def test_run_only_entry_selects_one_list(cfg, tmp_path, monkeypatch, frozen_now):
    _point_sources_at_tmp(cfg, tmp_path)
    # Two list entries in inline-harvest form; only 'beta' is selected.
    manifest = {"lists": [
        {"name": "alpha", "format": "inline",
         "candidates": [_inline_cand("alpha.example.com")]},
        {"name": "beta", "format": "inline",
         "candidates": [_inline_cand("beta.example.com")]},
    ]}
    mp = _write_manifest(tmp_path, manifest)

    probe = _make_probe({"beta.example.com": ("v", _verified_feed("beta.example.com"))})
    _install_run_fakes(monkeypatch, probe)

    assert bulk_import.run(cfg, manifest_path=mp, only_entry="beta") == 0
    # Only 'beta' produced a checkpoint; 'alpha' was filtered out entirely.
    assert bulk_import._checkpoint_path(cfg, "beta").exists()
    assert not bulk_import._checkpoint_path(cfg, "alpha").exists()


@pytest.mark.integration
def test_run_limit_truncates_candidates(cfg, tmp_path, monkeypatch, frozen_now, capsys):
    _point_sources_at_tmp(cfg, tmp_path)
    inline = [_inline_cand("h%d.example.com" % i) for i in range(3)]
    mp = _write_manifest(tmp_path, {"inline": inline})

    calls: List = []
    probe = _make_probe({"h%d.example.com" % i: ("v", _verified_feed("h%d.example.com" % i))
                         for i in range(3)}, calls=calls)
    _install_run_fakes(monkeypatch, probe)

    assert bulk_import.run(cfg, manifest_path=mp, limit=2) == 0
    assert "2 candidates" in capsys.readouterr().out
    assert len(calls) == 1 and len(calls[0]) == 2


@pytest.mark.integration
def test_run_max_candidates_truncates_per_entry(cfg, tmp_path, monkeypatch,
                                                frozen_now, capsys):
    _point_sources_at_tmp(cfg, tmp_path)
    # max_candidates is read off the entry, not the candidate, so nest the
    # candidates under a list entry that carries the cap.
    manifest = {"lists": [{
        "name": "capped", "format": "inline", "max_candidates": 1,
        "candidates": [_inline_cand("h%d.example.com" % i) for i in range(3)],
    }]}
    mp = _write_manifest(tmp_path, manifest)

    calls: List = []
    probe = _make_probe({"h%d.example.com" % i: ("v", _verified_feed("h%d.example.com" % i))
                         for i in range(3)}, calls=calls)
    _install_run_fakes(monkeypatch, probe)

    assert bulk_import.run(cfg, manifest_path=mp) == 0
    assert len(calls) == 1 and len(calls[0]) == 1


@pytest.mark.integration
def test_run_quality_drop_counts(cfg, tmp_path, monkeypatch, frozen_now, capsys):
    _point_sources_at_tmp(cfg, tmp_path)
    mp = _write_manifest(tmp_path, {"inline": [_inline_cand("stale.example.com")]})

    # Probe verifies it, but the feed is dormant -> quality gate drops it.
    probe = _make_probe({"stale.example.com":
                         ("v", _verified_feed("stale.example.com", latest=DORMANT))})
    merge_records: List = []
    _install_run_fakes(monkeypatch, probe, merge_records=merge_records)

    assert bulk_import.run(cfg, manifest_path=mp) == 0
    out = capsys.readouterr().out
    assert "0 verified" in out
    assert "1 dropped (stale/dateless)" in out
    # Nothing kept -> merge never called with a non-empty list.
    assert merge_records == []
    state = bulk_import._load_checkpoint(cfg, "inline")
    assert state["verified"] == 0
    assert state["rejected"] == 1        # the quality-drop counts as rejected
    assert state["complete"] is True


@pytest.mark.integration
def test_run_waves_split_by_wave_size(cfg, tmp_path, monkeypatch, frozen_now, capsys):
    _point_sources_at_tmp(cfg, tmp_path)
    inline = [_inline_cand("w%d.example.com" % i) for i in range(5)]
    mp = _write_manifest(tmp_path, {"inline": inline})

    calls: List = []
    probe = _make_probe({"w%d.example.com" % i: ("v", _verified_feed("w%d.example.com" % i))
                         for i in range(5)}, calls=calls)
    _install_run_fakes(monkeypatch, probe)

    assert bulk_import.run(cfg, manifest_path=mp, wave_size=2) == 0
    # 5 candidates, wave_size 2 -> waves of 2, 2, 1.
    assert [len(w) for w in calls] == [2, 2, 1]
    out = capsys.readouterr().out
    assert "wave 3:" in out


@pytest.mark.integration
def test_run_health_logged_when_db_exists(cfg, tmp_path, monkeypatch, frozen_now):
    _point_sources_at_tmp(cfg, tmp_path)
    # Create the DB so the final health-log branch runs (seed is faked no-op).
    db_mod.connect_rw(cfg.db_path).close()
    mp = _write_manifest(tmp_path, {"inline": [_inline_cand("keep.example.com")]})

    probe = _make_probe({"keep.example.com": ("v", _verified_feed("keep.example.com"))})
    _install_run_fakes(monkeypatch, probe)

    assert bulk_import.run(cfg, manifest_path=mp) == 0
    conn = db_mod.connect_ro(cfg.db_path)
    try:
        row = conn.execute(
            "SELECT message, stats FROM health WHERE job='sources' "
            "AND message LIKE 'bulk import:%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert "+1 imported" in row["message"]
    assert json.loads(row["stats"])["imported"] == 1


@pytest.mark.integration
def test_run_skips_health_when_db_absent(cfg, tmp_path, monkeypatch, frozen_now):
    _point_sources_at_tmp(cfg, tmp_path)
    mp = _write_manifest(tmp_path, {"inline": [_inline_cand("keep.example.com")]})
    probe = _make_probe({"keep.example.com": ("v", _verified_feed("keep.example.com"))})
    _install_run_fakes(monkeypatch, probe)

    assert not cfg.db_path.exists()
    assert bulk_import.run(cfg, manifest_path=mp) == 0
    # seed is faked (no DB creation); nothing created the DB.
    assert not cfg.db_path.exists()


# =========================================================================== #
# run() — markdown list-fetch path (fake PoliteClient)
# =========================================================================== #
def _fake_polite_factory(result: FetchResult, calls: List):
    class _FakeClient:
        def __init__(self, cfg):
            calls.append(("ctor", cfg))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def fetch(self, url, conditional=True):
            calls.append(("fetch", url, conditional))
            return result

    return _FakeClient


@pytest.mark.integration
def test_run_markdown_list_harvest(cfg, tmp_path, monkeypatch, frozen_now):
    _point_sources_at_tmp(cfg, tmp_path)
    manifest = {"lists": [{
        "name": "md", "format": "markdown",
        "url": "https://awesome.example.com/list.md",
        "category": "science", "topics": ["science"], "tier": 3,
    }]}
    mp = _write_manifest(tmp_path, manifest)

    http_calls: List = []
    result = FetchResult(status=200, content=_MARKDOWN)
    monkeypatch.setattr(bulk_import, "PoliteClient",
                        _fake_polite_factory(result, http_calls))

    probe_calls: List = []
    # Verify all three harvested candidates.
    probe = _make_probe({
        "alpha.example.com": ("v", _verified_feed("alpha.example.com")),
        "beta.example.com": ("v", _verified_feed("beta.example.com")),
        "gamma.example.com": ("v", _verified_feed("gamma.example.com")),
    }, calls=probe_calls)
    _install_run_fakes(monkeypatch, probe)

    assert bulk_import.run(cfg, manifest_path=mp) == 0

    # The list URL was fetched exactly once (conditional=False).
    assert ("fetch", "https://awesome.example.com/list.md", False) in http_calls
    # Three markdown candidates flowed into the prober.
    (wave,) = probe_calls
    names = {c["name"] for c in wave}
    assert names == {"Alpha Blog", "Beta News", "Gamma Home"}


@pytest.mark.integration
def test_run_opml_list_harvest(cfg, tmp_path, monkeypatch, frozen_now):
    _point_sources_at_tmp(cfg, tmp_path)
    manifest = {"lists": [{
        "name": "opml", "format": "opml",
        "url": "https://feeds.example.com/list.opml",
        "category": "research", "topics": ["ml-research"], "tier": 3,
    }]}
    mp = _write_manifest(tmp_path, manifest)

    result = FetchResult(status=200, content=_OPML)
    monkeypatch.setattr(bulk_import, "PoliteClient",
                        _fake_polite_factory(result, []))

    probe_calls: List = []
    probe = _make_probe({
        "one.example.com": ("v", _verified_feed("one.example.com")),
        "two.example.com": ("v", _verified_feed("two.example.com")),
    }, calls=probe_calls)
    _install_run_fakes(monkeypatch, probe)

    assert bulk_import.run(cfg, manifest_path=mp) == 0
    (wave,) = probe_calls
    assert {c["name"] for c in wave} == {"Feed One", "Feed Two"}


@pytest.mark.integration
def test_run_shared_host_bypasses_dedupe_and_no_host_dropped(
        cfg, tmp_path, monkeypatch, frozen_now):
    _point_sources_at_tmp(cfg, tmp_path)
    inline = [
        {"name": "no-host", "feed_url": "not-a-real-url"},       # host '' -> dropped
        {"name": "shared", "feed_url": "https://medium.com/@a/feed"},  # SHARED host
        _inline_cand("normal.example.com", name="normal"),
    ]
    mp = _write_manifest(tmp_path, {"inline": inline})

    calls: List = []
    probe = _make_probe({
        "medium.com": ("v", _verified_feed("medium.com", name="shared")),
        "normal.example.com": ("v", _verified_feed("normal.example.com")),
    }, calls=calls)
    _install_run_fakes(monkeypatch, probe)

    assert bulk_import.run(cfg, manifest_path=mp) == 0
    (wave,) = calls
    # The no-host candidate is filtered out; the SHARED host and normal survive.
    names = {c["name"] for c in wave}
    assert names == {"shared", "normal"}


@pytest.mark.integration
def test_run_markdown_list_fetch_failure_skips_entry(cfg, tmp_path, monkeypatch,
                                                     frozen_now, capsys):
    _point_sources_at_tmp(cfg, tmp_path)
    manifest = {"lists": [{
        "name": "md", "format": "markdown",
        "url": "https://awesome.example.com/list.md",
    }]}
    mp = _write_manifest(tmp_path, manifest)

    http_calls: List = []
    result = FetchResult(status=500, content=None, error="server error")
    monkeypatch.setattr(bulk_import, "PoliteClient",
                        _fake_polite_factory(result, http_calls))

    probe_calls: List = []
    _install_run_fakes(monkeypatch, _make_probe({}, calls=probe_calls))

    assert bulk_import.run(cfg, manifest_path=mp) == 0
    assert "list fetch failed" in capsys.readouterr().err
    assert probe_calls == []              # entry skipped before probing


# =========================================================================== #
# run() — end-to-end registry growth (only probe_candidates faked)
# =========================================================================== #
@pytest.mark.integration
def test_run_end_to_end_registry_growth(cfg, tmp_path, monkeypatch, frozen_now):
    _point_sources_at_tmp(cfg, tmp_path)
    mp = _write_manifest(tmp_path, {"inline": [_inline_cand("e2e.example.com")]})

    verified = _verified_feed("e2e.example.com", name="E2E Feed",
                              category="tech_news", entries=20, latest=FRESH_1D,
                              tier=2)
    verified["slug"] = "e2e-feed"
    # Patch ONLY the network-bound prober; merge_into_registry + seed run for real.
    monkeypatch.setattr(bulk_import.registry, "probe_candidates",
                        _make_probe({"e2e.example.com": ("v", verified)}))

    assert bulk_import.run(cfg, manifest_path=mp) == 0

    # sources.json grew by one real registry row.
    reg_data = json.loads(cfg.sources_json.read_text())
    slugs = [s["slug"] for s in reg_data["sources"]]
    assert "e2e-feed" in slugs

    # The DB was seeded from the registry and carries the new source.
    conn = db_mod.connect_ro(cfg.db_path)
    try:
        src = conn.execute(
            "SELECT slug, tier, cadence_min FROM sources WHERE slug='e2e-feed'"
        ).fetchone()
        health = conn.execute(
            "SELECT message FROM health WHERE job='sources' "
            "AND message LIKE 'bulk import:%'"
        ).fetchone()
    finally:
        conn.close()
    assert src is not None
    assert src["tier"] == 2
    assert src["cadence_min"] == bulk_import.TIER_CADENCE[2]
    assert health is not None
    assert "+1 imported" in health["message"]
