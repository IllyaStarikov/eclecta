"""Tests for :mod:`signalpipe.curate` — the LLM curation funnel.

The funnel over deterministic finalists is: a cheap triage keep/skip gate (only
for clusters inside ``funnel.triage_band``) -> a single judge call -> a Claude
write for survivors. Every cluster gets exactly one ``curations`` row and re-runs
skip anything already pending/done/failed. Spend-cap / usage-limit / ollama-down
all defer cleanly without marking items failed.

Every I/O leaf is faked. The sole LLM seam is ``adapter.complete`` — curate does
``from .llm import ... adapter`` so the tests patch ``curate.adapter.complete``
(the name curate actually resolves at call time). The downtime/quota gates are
patched on ``curate.downtime`` / ``curate.quota``. ``run`` executes against a real
on-disk WAL DB (the ``conn``/``seed``/``cfg`` fixtures) with the clock frozen so
every ``curated_at`` is deterministic. No network, no subprocess, no real LLM.
"""

from __future__ import annotations

import datetime
import itertools
import json
from types import SimpleNamespace

import pytest

import signalpipe.curate as curate
from signalpipe.llm import LLMError, SpendCapExceeded, UsageLimitExhausted

FROZEN = "2026-07-04T12:00:00+00:00"

# Unique canonical_url generator (clusters.canonical_url is UNIQUE).
_CANON = itertools.count(1)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class FakeAdapter:
    """Stand-in for ``adapter.complete``.

    Dispatches on the tier (first positional arg) via a user-supplied
    ``handler(tier, prompt)`` that returns a schema-shaped dict or raises. Every
    call is recorded on ``.calls`` so a test can assert which tiers ran (and in
    what order) and inspect the exact prompt each tier received.
    """

    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    def __call__(self, tier, system, prompt, schema, *, cfg=None, conn=None,
                 effort=None, cap_kind="daily", model_override=None):
        self.calls.append(SimpleNamespace(tier=tier, system=system,
                                          prompt=prompt, schema=schema))
        return self._handler(tier, prompt)

    @property
    def tiers(self):
        return [c.tier for c in self.calls]


def _finalist(seed, score, title="Example story about AI models", **over):
    """Seed a scored cluster eligible for curation (unique canonical_url)."""
    over.setdefault("canonical_url", "https://ex.com/story/%d" % next(_CANON))
    return seed.cluster(score=score, title=title, **over)


def _curation(conn, cid):
    return conn.execute(
        "SELECT * FROM curations WHERE cluster_id=?", (cid,)).fetchone()


def _count(conn, table, where="1", args=()):
    return conn.execute(
        "SELECT COUNT(*) FROM %s WHERE %s" % (table, where), args).fetchone()[0]


def _last_run(cfg):
    return json.loads(cfg.path.read_text())["last_run"]


@pytest.fixture
def patched(monkeypatch, freeze_now_iso):
    """Freeze curate's clock and pin the downtime/quota gates open (never touch
    a real Ollama/urllib probe or the quota hold file). Individual tests override
    the adapter and, where needed, the gates."""
    frozen = freeze_now_iso(curate)
    monkeypatch.setattr(curate.downtime, "ollama_up", lambda cfg: True)
    monkeypatch.setattr(curate.quota, "status", lambda: (False, ""))
    return frozen


def _patch_adapter(monkeypatch, handler):
    fake = FakeAdapter(handler)
    monkeypatch.setattr(curate.adapter, "complete", fake)
    return fake


# ═══════════════════════════════════════════════════════════════════════════
# _now_iso
# ═══════════════════════════════════════════════════════════════════════════
def test_now_iso_is_tz_aware_utc_isoformat():
    s = curate._now_iso()
    dt = datetime.datetime.fromisoformat(s)
    assert dt.tzinfo is not None
    assert dt.utcoffset() == datetime.timedelta(0)


# ═══════════════════════════════════════════════════════════════════════════
# _write_prompt  (pure string assembly)
# ═══════════════════════════════════════════════════════════════════════════
def test_write_prompt_appends_judgment_block_after_article():
    judged = {
        "relevance_score": 7,
        "channels": ["ai", "devtools"],
        "novelty": "first open-weight 400B",
        "facts": ["trained on 15T tokens", "MoE with 8 experts"],
    }
    out = curate._write_prompt("ARTICLE BODY", judged)
    assert out.startswith("ARTICLE BODY\n\n")
    assert "EDITOR'S JUDGMENT" in out
    assert "relevance: 7/10" in out
    assert "channels: ai, devtools" in out
    assert "novelty: first open-weight 400B" in out
    assert "extracted facts:" in out
    assert "- trained on 15T tokens" in out
    assert "- MoE with 8 experts" in out


def test_write_prompt_empty_facts_omits_facts_header():
    judged = {"relevance_score": 5, "channels": [], "novelty": None, "facts": []}
    out = curate._write_prompt("A", judged)
    assert "extracted facts:" not in out
    # empty channels/novelty still render their labels
    assert "channels: " in out
    assert "novelty: " in out


def test_write_prompt_missing_keys_use_none_and_empties():
    # relevance_score absent -> 'None/10'; channels/novelty/facts absent.
    out = curate._write_prompt("BODY", {})
    assert "relevance: None/10" in out
    assert "channels: " in out          # ", ".join([]) -> ""
    assert "novelty: " in out           # (None or "") -> ""
    assert "extracted facts:" not in out


def test_write_prompt_facts_order_preserved():
    judged = {"facts": ["one", "two", "three"]}
    out = curate._write_prompt("X", judged)
    i1, i2, i3 = out.index("- one"), out.index("- two"), out.index("- three")
    assert i1 < i2 < i3


# ═══════════════════════════════════════════════════════════════════════════
# _model_label
# ═══════════════════════════════════════════════════════════════════════════
def test_model_label_local_returns_first_local_model(cfg):
    # signal.min.json: tiers.judge.local == "qwen2.5:14b"
    assert curate._model_label(cfg, "judge", "local") == "qwen2.5:14b"


def test_model_label_local_empty_list_falls_back_to_literal_local():
    fake = SimpleNamespace(
        local_models_for=lambda tier: [],
        model_for=lambda tier, backend: "should-not-be-used",
    )
    assert curate._model_label(fake, "judge", "local") == "local"


def test_model_label_non_local_delegates_to_model_for(cfg):
    assert curate._model_label(cfg, "triage", "subscription") == "claude-haiku-4-5"
    assert curate._model_label(cfg, "write", "api") == "claude-sonnet-4-6"


def test_model_label_non_local_uses_model_for_seam():
    calls = []

    def model_for(tier, backend):
        calls.append((tier, backend))
        return "cloud-x"

    fake = SimpleNamespace(local_models_for=lambda tier: ["unused"],
                           model_for=model_for)
    assert curate._model_label(fake, "write", "api") == "cloud-x"
    assert calls == [("write", "api")]


# ═══════════════════════════════════════════════════════════════════════════
# _build_prompt  (integration — real DB rows)
# ═══════════════════════════════════════════════════════════════════════════
def _cluster_row(conn, cid):
    return conn.execute("SELECT * FROM clusters WHERE id=?", (cid,)).fetchone()


@pytest.mark.integration
def test_build_prompt_header_full_text_and_surfaces(cfg, conn, seed):
    cid = _finalist(seed, 7.5, surface_count=3,
                    first_seen="2026-07-04T06:30:00+00:00",
                    canonical_url="https://ex.com/story-full")
    src1 = seed.source(slug="hn", name="Hacker News")
    src2 = seed.source(slug="lob", name="Lobsters")
    src3 = seed.source(slug="rd", name="Reddit")
    seed.surface(cid, src1, points=200, comments=10)
    seed.surface(cid, src2, points=50, comments=None)
    seed.surface(cid, src3, points=None, comments=None)
    seed.article(cid, text="HEAD-BODY-CONTENT", excerpt="ignored", paywalled=0)

    out = curate._build_prompt(conn, _cluster_row(conn, cid))

    assert "TITLE: Example story about AI models" in out
    assert "URL: https://ex.com/story-full" in out
    assert "FIRST SEEN: 2026-07-04" in out
    assert "DETERMINISTIC SCORE: 7.5/10" in out
    assert "SURFACES: 3" in out
    assert "ARTICLE TEXT:\nHEAD-BODY-CONTENT" in out
    assert "WHERE IT SURFACED:" in out
    # ordering: points DESC with NULLs last
    assert out.index("Hacker News") < out.index("Lobsters") < out.index("Reddit")
    assert "- Hacker News, 200 points, 10 comments" in out
    assert "- Lobsters, 50 points" in out          # comments None -> omitted
    assert "- Reddit" in out                        # no points/comments
    # the no-metrics surface has no trailing ", N points/comments"
    assert "- Reddit," not in out


@pytest.mark.integration
def test_build_prompt_zero_points_and_comments_are_omitted(cfg, conn, seed):
    # 0 is falsy, so a surface with 0 points / 0 comments renders name-only.
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/story-zero")
    src = seed.source(slug="z", name="ZeroSource")
    seed.surface(cid, src, points=0, comments=0)
    out = curate._build_prompt(conn, _cluster_row(conn, cid))
    assert "- ZeroSource" in out
    assert "points" not in out.split("WHERE IT SURFACED:")[1]
    assert "comments" not in out.split("WHERE IT SURFACED:")[1]


@pytest.mark.integration
def test_build_prompt_truncates_full_text_at_max_chars(cfg, conn, seed):
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/story-trunc")
    seed.article(cid, text="HEAD" + "x" * 100 + "TAIL")
    out = curate._build_prompt(conn, _cluster_row(conn, cid), max_chars=8)
    assert "ARTICLE TEXT:\nHEADxxxx" in out
    assert "TAIL" not in out


@pytest.mark.integration
def test_build_prompt_excerpt_only_branch(cfg, conn, seed):
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/story-exc")
    seed.article(cid, text="", excerpt="Just the lede sentence.")
    out = curate._build_prompt(conn, _cluster_row(conn, cid))
    assert "EXCERPT ONLY:\nJust the lede sentence." in out
    assert "ARTICLE TEXT:" not in out
    assert "NO ARTICLE TEXT AVAILABLE" not in out


@pytest.mark.integration
def test_build_prompt_no_text_conservative_note_with_empty_article(cfg, conn, seed):
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/story-empty")
    seed.article(cid, text="", excerpt="")
    out = curate._build_prompt(conn, _cluster_row(conn, cid))
    assert "NO ARTICLE TEXT AVAILABLE" in out
    assert "be conservative with relevance_score" in out


@pytest.mark.integration
def test_build_prompt_paywalled_note(cfg, conn, seed):
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/story-pw")
    seed.article(cid, text="Partial paywalled text.", paywalled=1)
    out = curate._build_prompt(conn, _cluster_row(conn, cid))
    assert "NOTE: original is paywalled; text below may be partial." in out
    assert "ARTICLE TEXT:\nPartial paywalled text." in out


@pytest.mark.integration
def test_build_prompt_no_article_row_and_discussion_only_url(conn):
    # A synthetic cluster with no article/surface rows and canonical_url NULL.
    cluster = {
        "id": 987654,
        "title": "Discussion thread only",
        "canonical_url": None,
        "first_seen": "2026-07-04T09:00:00+00:00",
        "score": None,
        "surface_count": 0,
    }
    out = curate._build_prompt(conn, cluster)
    assert "URL: (discussion-only)" in out
    assert "DETERMINISTIC SCORE: 0.0/10" in out     # score None -> 0
    assert "SURFACES: 0" in out
    assert "NO ARTICLE TEXT AVAILABLE" in out
    assert "WHERE IT SURFACED:" not in out          # no surfaces


# ═══════════════════════════════════════════════════════════════════════════
# persistence helpers  (direct, precise column assertions)
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.integration
def test_mark_triaged_out_sets_skip_columns(cfg, conn, seed, patched):
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/to")
    seed.curation(cid, status="pending")
    curate._mark_triaged_out(conn, {"id": cid}, {"reason": "engagement bait"}, cfg)

    row = _curation(conn, cid)
    assert row["status"] == "skipped"
    assert row["skip"] == 1
    assert row["skip_reason"] == "engagement bait"
    assert row["tier_used"] == "triage"
    assert row["backend_used"] == "subscription"
    assert row["model_used"] == "claude-haiku-4-5"
    assert row["curated_at"] == FROZEN


@pytest.mark.integration
def test_mark_triaged_out_reason_truncated_and_default(cfg, conn, seed, patched):
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/to2")
    seed.curation(cid, status="pending")
    curate._mark_triaged_out(conn, {"id": cid}, {"reason": "x" * 400}, cfg)
    assert len(_curation(conn, cid)["skip_reason"]) == 300

    cid2 = _finalist(seed, 5.0, canonical_url="https://ex.com/to3")
    seed.curation(cid2, status="pending")
    curate._mark_triaged_out(conn, {"id": cid2}, {}, cfg)  # no 'reason' key
    assert _curation(conn, cid2)["skip_reason"] == ""


@pytest.mark.integration
def test_mark_judge_skip_triaged_and_untriaged_tiers(cfg, conn, seed, patched):
    judged = {"relevance_score": 7, "channels": ["ai", "devtools"],
              "novelty": "nv", "audience": "au", "skip_reason": "duplicate"}

    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/js1")
    seed.curation(cid, status="pending")
    curate._mark_judge_skip(conn, {"id": cid}, judged, cfg, triaged=True)
    row = _curation(conn, cid)
    assert row["status"] == "skipped"
    assert row["tier_used"] == "triage+judge"
    assert row["backend_used"] == "subscription"
    assert row["model_used"] == "claude-haiku-4-5"
    assert row["relevance_score"] == 7
    assert json.loads(row["channels"]) == ["ai", "devtools"]
    assert row["novelty"] == "nv"
    assert row["audience"] == "au"
    assert row["skip"] == 1
    assert row["skip_reason"] == "duplicate"
    assert row["curated_at"] == FROZEN

    cid2 = _finalist(seed, 5.0, canonical_url="https://ex.com/js2")
    seed.curation(cid2, status="pending")
    curate._mark_judge_skip(conn, {"id": cid2}, judged, cfg, triaged=False)
    assert _curation(conn, cid2)["tier_used"] == "judge"


@pytest.mark.integration
def test_mark_judge_skip_none_relevance_coerced_to_zero(cfg, conn, seed, patched):
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/js3")
    seed.curation(cid, status="pending")
    curate._mark_judge_skip(
        conn, {"id": cid},
        {"relevance_score": None, "channels": None, "novelty": None,
         "audience": None, "skip_reason": None},
        cfg, triaged=False)
    row = _curation(conn, cid)
    assert row["relevance_score"] == 0
    assert json.loads(row["channels"]) == []   # None -> []
    assert row["skip_reason"] == ""             # None -> ""


@pytest.mark.integration
def test_persist_done_sets_full_row(cfg, conn, seed, patched):
    judged = {"relevance_score": 9, "channels": ["ai"], "novelty": "nv",
              "audience": "au"}
    written = {"why_it_matters": "it matters", "notes": ["a", "b"],
               "summary": "the summary"}

    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/pd1")
    seed.curation(cid, status="pending")
    curate._persist_done(conn, {"id": cid}, judged, written, cfg, triaged=True)
    row = _curation(conn, cid)
    assert row["status"] == "done"
    assert row["tier_used"] == "triage+judge+write"
    assert row["backend_used"] == "subscription"
    assert row["model_used"] == "claude-sonnet-4-6"
    assert row["relevance_score"] == 9
    assert row["why_it_matters"] == "it matters"
    assert json.loads(row["notes"]) == ["a", "b"]
    assert row["summary"] == "the summary"
    assert json.loads(row["channels"]) == ["ai"]
    assert row["novelty"] == "nv"
    assert row["audience"] == "au"
    assert row["skip"] == 0
    assert row["skip_reason"] is None
    assert row["curated_at"] == FROZEN

    cid2 = _finalist(seed, 5.0, canonical_url="https://ex.com/pd2")
    seed.curation(cid2, status="pending")
    curate._persist_done(conn, {"id": cid2}, judged, written, cfg, triaged=False)
    assert _curation(conn, cid2)["tier_used"] == "judge+write"


@pytest.mark.integration
def test_persist_done_notes_none_becomes_empty_json_array(cfg, conn, seed, patched):
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/pd3")
    seed.curation(cid, status="pending")
    curate._persist_done(
        conn, {"id": cid},
        {"relevance_score": None, "channels": None, "novelty": None,
         "audience": None},
        {"why_it_matters": None, "notes": None, "summary": None},
        cfg, triaged=True)
    row = _curation(conn, cid)
    assert row["relevance_score"] == 0
    assert json.loads(row["notes"]) == []
    assert json.loads(row["channels"]) == []


@pytest.mark.integration
def test_mark_failed_sets_failed_status_and_truncates(cfg, conn, seed, patched):
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/mf1")
    seed.curation(cid, status="pending")
    curate._mark_failed(conn, {"id": cid}, LLMError("boom happened"))
    row = _curation(conn, cid)
    assert row["status"] == "failed"
    assert row["skip_reason"] == "boom happened"
    assert row["curated_at"] == FROZEN

    cid2 = _finalist(seed, 5.0, canonical_url="https://ex.com/mf2")
    seed.curation(cid2, status="pending")
    curate._mark_failed(conn, {"id": cid2}, LLMError("y" * 500))
    assert len(_curation(conn, cid2)["skip_reason"]) == 300


# ═══════════════════════════════════════════════════════════════════════════
# run()  — deferral gates (nothing touched)
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.integration
def test_run_defers_when_ollama_down_for_local_judge(
    cfg, conn, seed, monkeypatch, capsys, freeze_now_iso
):
    freeze_now_iso(curate)
    cfg.data["backend"]["tier_overrides"] = {"judge": "local"}
    monkeypatch.setattr(curate.downtime, "ollama_up", lambda cfg: False)
    monkeypatch.setattr(curate.adapter, "complete", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no LLM call on deferral")))
    _finalist(seed, 8.0, canonical_url="https://ex.com/deferol")

    assert curate.run(cfg) == 0

    out = capsys.readouterr().out
    assert "ollama unreachable" in out
    assert "deferring" in out
    assert _count(conn, "curations") == 0
    assert _count(conn, "health",
                  "job='curate' AND level='warn'") >= 1


@pytest.mark.integration
def test_run_defers_when_quota_hold_active(
    cfg, conn, seed, monkeypatch, capsys, freeze_now_iso
):
    freeze_now_iso(curate)
    monkeypatch.setattr(curate.downtime, "ollama_up", lambda cfg: True)
    monkeypatch.setattr(curate.quota, "status", lambda: (True, "usage limit"))
    monkeypatch.setattr(curate.adapter, "complete", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no LLM call on deferral")))
    _finalist(seed, 8.0, canonical_url="https://ex.com/deferq")

    assert curate.run(cfg) == 0

    out = capsys.readouterr().out
    assert "deferring curate (usage limit)" in out
    assert _count(conn, "curations") == 0
    assert _count(conn, "health", "job='curate' AND level='warn'") >= 1


@pytest.mark.integration
def test_run_dry_run_skips_gates_and_writes_nothing(
    cfg, conn, seed, monkeypatch, capsys, freeze_now_iso
):
    freeze_now_iso(curate)
    # dry_run must bypass BOTH gates even when they would defer.
    cfg.data["backend"]["tier_overrides"] = {"judge": "local"}
    monkeypatch.setattr(curate.downtime, "ollama_up", lambda cfg: False)
    monkeypatch.setattr(curate.quota, "status", lambda: (True, "held"))
    monkeypatch.setattr(curate.adapter, "complete", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("dry-run must not call the LLM")))
    _finalist(seed, 8.0, title="Dry run candidate",
              canonical_url="https://ex.com/dry")

    assert curate.run(cfg, dry_run=True) == 0

    out = capsys.readouterr().out
    assert "would curate 1 clusters" in out
    assert "Dry run candidate" in out
    assert _count(conn, "curations") == 0            # nothing claimed
    assert _count(conn, "health", "job='curate'") == 0


# ═══════════════════════════════════════════════════════════════════════════
# run()  — orphan sweep + no-finalists
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.integration
def test_run_sweeps_orphaned_pending_claims(
    cfg, conn, seed, patched, monkeypatch, capsys
):
    # A leftover 'pending' claim from a crashed run, on a below-threshold cluster
    # (so it is NOT itself re-selected as a finalist).
    orphan = _finalist(seed, 2.0, canonical_url="https://ex.com/orphan")
    seed.curation(orphan, status="pending")
    # No finalists exist, so the adapter must never be reached.
    _patch_adapter(monkeypatch, lambda t, p: (_ for _ in ()).throw(
        AssertionError("no LLM call with zero finalists")))

    assert curate.run(cfg) == 0

    assert _curation(conn, orphan) is None           # swept
    out = capsys.readouterr().out
    assert "orphaned pending claim" in out
    assert "no uncurated finalists" in out
    assert _count(conn, "health",
                  "job='curate' AND level='warn' AND message LIKE '%orphaned%'") >= 1


@pytest.mark.integration
def test_run_no_finalists_returns_early(cfg, conn, seed, patched, capsys):
    _finalist(seed, 1.0, canonical_url="https://ex.com/lowscore")  # below 3.5
    assert curate.run(cfg) == 0
    out = capsys.readouterr().out
    assert "no uncurated finalists" in out
    assert _count(conn, "curations") == 0


# ═══════════════════════════════════════════════════════════════════════════
# run()  — happy paths
# ═══════════════════════════════════════════════════════════════════════════
def _keep_handler(tier, prompt):
    if tier == "triage":
        return {"keep": True, "reason": "worth a read"}
    if tier == "judge":
        return {"skip": False, "relevance_score": 8, "channels": ["ai"],
                "novelty": "genuinely new", "audience": "engineers",
                "facts": ["fact one", "fact two"]}
    if tier == "write":
        return {"why_it_matters": "it matters", "notes": ["n1", "n2"],
                "summary": "the summary"}
    raise AssertionError("unexpected tier %r" % tier)


@pytest.mark.integration
def test_run_happy_path_in_band_triage_judge_write(
    cfg, conn, seed, patched, monkeypatch, capsys
):
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/happy")  # in band
    seed.article(cid, text="Full article about a new model.")
    fake = _patch_adapter(monkeypatch, _keep_handler)

    assert curate.run(cfg) == 0

    row = _curation(conn, cid)
    assert row["status"] == "done"
    assert row["tier_used"] == "triage+judge+write"
    assert row["backend_used"] == "subscription"
    assert row["model_used"] == "claude-sonnet-4-6"
    assert row["relevance_score"] == 8
    assert row["why_it_matters"] == "it matters"
    assert json.loads(row["notes"]) == ["n1", "n2"]
    assert row["summary"] == "the summary"
    assert json.loads(row["channels"]) == ["ai"]
    assert row["novelty"] == "genuinely new"
    assert row["audience"] == "engineers"
    assert row["skip"] == 0
    assert row["skip_reason"] is None
    assert row["curated_at"] == FROZEN

    assert fake.tiers == ["triage", "judge", "write"]
    # the write tier saw the judgment block folded into its prompt
    write_prompt = fake.calls[-1].prompt
    assert "EDITOR'S JUDGMENT" in write_prompt
    assert "- fact one" in write_prompt

    out = capsys.readouterr().out
    assert "1 done" in out and "0 triaged out" in out
    assert _count(conn, "runs", "job='curate'") == 1
    assert _count(conn, "health", "job='curate' AND level='info'") >= 1
    lr = _last_run(cfg)
    assert lr["job"] == "curate"
    assert lr["stats"]["done"] == 1


@pytest.mark.integration
def test_run_above_band_skips_triage(cfg, conn, seed, patched, monkeypatch):
    cid = _finalist(seed, 8.0, canonical_url="https://ex.com/above")  # > band top
    fake = _patch_adapter(monkeypatch, _keep_handler)

    assert curate.run(cfg) == 0

    row = _curation(conn, cid)
    assert row["status"] == "done"
    assert row["tier_used"] == "judge+write"          # no triage prefix
    assert fake.tiers == ["judge", "write"]           # triage skipped


@pytest.mark.integration
def test_run_empty_triage_band_never_triages(cfg, conn, seed, patched, monkeypatch):
    # bool(band) is False -> in_band always False even for an in-range score.
    cfg.data["funnel"]["triage_band"] = []
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/noband")
    fake = _patch_adapter(monkeypatch, _keep_handler)

    assert curate.run(cfg) == 0
    assert _curation(conn, cid)["tier_used"] == "judge+write"
    assert "triage" not in fake.tiers


@pytest.mark.integration
def test_run_triaged_out_when_keep_false(cfg, conn, seed, patched, monkeypatch):
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/triout")  # in band

    def handler(tier, prompt):
        if tier == "triage":
            return {"keep": False, "reason": "not interesting"}
        raise AssertionError("judge/write must not run after a triage-out")

    fake = _patch_adapter(monkeypatch, handler)

    assert curate.run(cfg) == 0
    row = _curation(conn, cid)
    assert row["status"] == "skipped"
    assert row["skip"] == 1
    assert row["tier_used"] == "triage"
    assert row["skip_reason"] == "not interesting"
    assert fake.tiers == ["triage"]
    assert _last_run(cfg)["stats"]["triaged_out"] == 1


@pytest.mark.integration
def test_run_judge_skip_triaged(cfg, conn, seed, patched, monkeypatch):
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/jskip1")  # in band

    def handler(tier, prompt):
        if tier == "triage":
            return {"keep": True, "reason": "ok"}
        if tier == "judge":
            return {"skip": True, "skip_reason": "duplicate coverage",
                    "relevance_score": 4, "channels": ["ai"],
                    "novelty": "n", "audience": "a", "facts": ["x", "y"]}
        raise AssertionError("write must not run after a judge-skip")

    fake = _patch_adapter(monkeypatch, handler)

    assert curate.run(cfg) == 0
    row = _curation(conn, cid)
    assert row["status"] == "skipped"
    assert row["tier_used"] == "triage+judge"
    assert row["skip"] == 1
    assert row["skip_reason"] == "duplicate coverage"
    assert row["relevance_score"] == 4
    assert json.loads(row["channels"]) == ["ai"]
    assert fake.tiers == ["triage", "judge"]
    assert _last_run(cfg)["stats"]["skipped"] == 1


@pytest.mark.integration
def test_run_judge_skip_untriaged(cfg, conn, seed, patched, monkeypatch):
    cid = _finalist(seed, 8.0, canonical_url="https://ex.com/jskip2")  # above band

    def handler(tier, prompt):
        if tier == "judge":
            return {"skip": True, "skip_reason": "thin rewrite",
                    "relevance_score": 3, "channels": ["ai"],
                    "novelty": None, "audience": None, "facts": ["x", "y"]}
        raise AssertionError("only judge should run")

    fake = _patch_adapter(monkeypatch, handler)

    assert curate.run(cfg) == 0
    assert _curation(conn, cid)["tier_used"] == "judge"
    assert fake.tiers == ["judge"]


# ═══════════════════════════════════════════════════════════════════════════
# run()  — limits + idempotency
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.integration
def test_run_explicit_limit_curates_top_n_only(
    cfg, conn, seed, patched, monkeypatch
):
    hi = _finalist(seed, 9.0, canonical_url="https://ex.com/l9")
    mid = _finalist(seed, 8.0, canonical_url="https://ex.com/l8")
    lo = _finalist(seed, 7.0, canonical_url="https://ex.com/l7")
    _patch_adapter(monkeypatch, _keep_handler)

    assert curate.run(cfg, limit=2) == 0
    assert _curation(conn, hi)["status"] == "done"
    assert _curation(conn, mid)["status"] == "done"
    assert _curation(conn, lo) is None                # below the limit


@pytest.mark.integration
def test_run_default_limit_is_curate_batch(cfg, conn, seed, patched, monkeypatch):
    # signal.min.json funnel.curate_batch == 3; seed 5 finalists above the band.
    ids = [_finalist(seed, s, canonical_url="https://ex.com/b%d" % i)
           for i, s in enumerate([9.0, 8.5, 8.0, 7.5, 7.0])]
    _patch_adapter(monkeypatch, _keep_handler)

    assert curate.run(cfg) == 0
    done = _count(conn, "curations", "status='done'")
    assert done == 3
    assert _curation(conn, ids[3]) is None
    assert _curation(conn, ids[4]) is None


@pytest.mark.integration
def test_run_is_idempotent_for_already_done_cluster(
    cfg, conn, seed, patched, monkeypatch
):
    cid = _finalist(seed, 8.0, canonical_url="https://ex.com/idem")
    seed.curation(cid, status="done", curated_at="2000-01-01T00:00:00+00:00",
                  why_it_matters="original text")
    # Any adapter call would be a bug (a done cluster is not a finalist).
    _patch_adapter(monkeypatch, lambda t, p: (_ for _ in ()).throw(
        AssertionError("done cluster must not be re-curated")))

    assert curate.run(cfg) == 0
    row = _curation(conn, cid)
    assert row["status"] == "done"
    assert row["why_it_matters"] == "original text"   # untouched
    assert row["curated_at"] == "2000-01-01T00:00:00+00:00"


# ═══════════════════════════════════════════════════════════════════════════
# run()  — phase-1 (triage) error handling
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.integration
def test_run_phase1_usage_limit_stops_without_marking_failed(
    cfg, conn, seed, patched, monkeypatch, capsys
):
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/p1q")  # in band

    def handler(tier, prompt):
        if tier == "triage":
            raise UsageLimitExhausted("limit reached")
        raise AssertionError("no tier past triage")

    _patch_adapter(monkeypatch, handler)

    assert curate.run(cfg) == 0
    # claimed but neither failed nor deleted — swept by the next run.
    assert _curation(conn, cid)["status"] == "pending"
    out = capsys.readouterr().out
    assert "STOP: limit reached" in out
    assert "[stopped: subscription usage limit — will retry]" in out
    lr = _last_run(cfg)
    assert lr["stats"]["quota_stopped"] == 1
    assert lr["stats"]["done"] == 0


@pytest.mark.integration
def test_run_phase1_llm_error_marks_item_failed(
    cfg, conn, seed, patched, monkeypatch
):
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/p1e")  # in band

    def handler(tier, prompt):
        if tier == "triage":
            raise LLMError("triage exploded")
        raise AssertionError("no tier past triage")

    _patch_adapter(monkeypatch, handler)

    assert curate.run(cfg) == 0
    row = _curation(conn, cid)
    assert row["status"] == "failed"
    assert row["skip_reason"] == "triage exploded"
    assert _last_run(cfg)["stats"]["failed"] == 1


@pytest.mark.integration
def test_run_phase1_spend_cap_marks_item_failed_not_stopped(
    cfg, conn, seed, patched, monkeypatch
):
    # Phase 1 has no SpendCapExceeded handler, so it is caught as an LLMError
    # subclass and the item is marked failed (documented actual behavior).
    cid = _finalist(seed, 5.0, canonical_url="https://ex.com/p1c")  # in band
    _patch_adapter(monkeypatch, lambda t, p: (_ for _ in ()).throw(
        SpendCapExceeded("cap during triage")))

    assert curate.run(cfg) == 0
    row = _curation(conn, cid)
    assert row["status"] == "failed"
    assert row["skip_reason"] == "cap during triage"
    lr = _last_run(cfg)
    assert lr["stats"]["failed"] == 1
    assert lr["stats"]["cap_stopped"] == 0            # NOT a clean cap stop


# ═══════════════════════════════════════════════════════════════════════════
# run()  — phase-2 (judge/write) error handling
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.integration
def test_run_phase2_spend_cap_deletes_claim_and_stops(
    cfg, conn, seed, patched, monkeypatch, capsys
):
    hi = _finalist(seed, 9.0, canonical_url="https://ex.com/p2c1")   # above band
    lo = _finalist(seed, 7.0, canonical_url="https://ex.com/p2c2")

    def handler(tier, prompt):
        if tier == "judge":
            raise SpendCapExceeded("cap reached")
        raise AssertionError("write must not run")

    _patch_adapter(monkeypatch, handler)

    assert curate.run(cfg) == 0
    # first survivor's pending claim deleted; the rest left pending (untouched).
    assert _curation(conn, hi) is None
    assert _curation(conn, lo)["status"] == "pending"
    out = capsys.readouterr().out
    assert "STOP: cap reached" in out
    assert "[stopped at spend cap]" in out
    lr = _last_run(cfg)
    assert lr["stats"]["cap_stopped"] == 1
    assert lr["stats"]["done"] == 0
    assert _count(conn, "health",
                  "job='curate' AND level='warn' AND message LIKE '%cap reached%'") >= 1


@pytest.mark.integration
def test_run_phase2_usage_limit_deletes_claim_and_stops(
    cfg, conn, seed, patched, monkeypatch, capsys
):
    hi = _finalist(seed, 9.0, canonical_url="https://ex.com/p2q1")   # above band
    lo = _finalist(seed, 7.0, canonical_url="https://ex.com/p2q2")

    def handler(tier, prompt):
        if tier == "judge":
            raise UsageLimitExhausted("subscription exhausted")
        raise AssertionError("write must not run")

    _patch_adapter(monkeypatch, handler)

    assert curate.run(cfg) == 0
    assert _curation(conn, hi) is None
    assert _curation(conn, lo)["status"] == "pending"
    out = capsys.readouterr().out
    assert "STOP: subscription exhausted" in out
    assert "[stopped: subscription usage limit — will retry]" in out
    assert _last_run(cfg)["stats"]["quota_stopped"] == 1


@pytest.mark.integration
def test_run_phase2_llm_error_fails_one_and_continues(
    cfg, conn, seed, patched, monkeypatch
):
    c1 = _finalist(seed, 9.0, title="FailMe please break here",
                   canonical_url="https://ex.com/p2e1")
    c2 = _finalist(seed, 7.0, title="Second good story works",
                   canonical_url="https://ex.com/p2e2")

    def handler(tier, prompt):
        if tier == "judge":
            if "FailMe" in prompt:
                raise LLMError("judge boom")
            return {"skip": False, "relevance_score": 6, "channels": ["ai"],
                    "novelty": "n", "audience": "a", "facts": ["x", "y"]}
        if tier == "write":
            return {"why_it_matters": "w", "notes": ["a", "b"], "summary": "s"}
        raise AssertionError("unexpected tier")

    _patch_adapter(monkeypatch, handler)

    assert curate.run(cfg) == 0
    assert _curation(conn, c1)["status"] == "failed"      # no bleed
    assert _curation(conn, c1)["skip_reason"] == "judge boom"
    assert _curation(conn, c2)["status"] == "done"
    lr = _last_run(cfg)
    assert lr["stats"]["failed"] == 1
    assert lr["stats"]["done"] == 1
