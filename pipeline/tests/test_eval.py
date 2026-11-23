"""Tests for signalpipe.eval — pure metrics, gold I/O, candidate build, replay."""

from __future__ import annotations

import json

from signalpipe import eval as ev


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def _gold(id, featured, relevance, category="ai"):
    return {"id": id, "human": {"featured": featured, "relevance": relevance,
                                "category": category}}


def _pred(id, relevance, skip=False, category="ai"):
    return {"id": id, "relevance": relevance, "skip": skip, "category": category}


def test_metrics_perfect_agreement():
    golds = [_gold("a", True, 9), _gold("b", False, 2)]
    preds = [_pred("a", 9), _pred("b", 2)]
    m = ev.score_predictions(preds, golds)
    assert m["n"] == 2
    assert m["agreement_featured"] == 1.0
    assert m["featured_precision"] == 1.0
    assert m["featured_recall"] == 1.0
    assert m["relevance_mae"] == 0.0
    assert m["category_accuracy"] == 1.0


def test_metrics_known_confusion():
    # gold featured: a,b ; gold not-featured: c,d
    golds = [_gold("a", True, 8), _gold("b", True, 7),
             _gold("c", False, 3), _gold("d", False, 4)]
    # pred featured (rel>=6, not skip): a (tp), c (fp) ; b predicted skip -> fn
    preds = [_pred("a", 8), _pred("b", 8, skip=True),
             _pred("c", 7), _pred("d", 2)]
    m = ev.score_predictions(preds, golds)
    # tp=1 (a), fp=1 (c), fn=1 (b) -> precision 1/2, recall 1/2
    assert m["featured_precision"] == 0.5
    assert m["featured_recall"] == 0.5
    # agreement: a correct, b wrong, c wrong, d correct -> 2/4
    assert m["agreement_featured"] == 0.5


def test_metrics_relevance_mae():
    golds = [_gold("a", True, 8), _gold("b", False, 2)]
    preds = [_pred("a", 6), _pred("b", 4)]  # errors 2 and 2
    m = ev.score_predictions(preds, golds)
    assert m["relevance_mae"] == 2.0


def test_metrics_empty_is_zero_not_error():
    m = ev.score_predictions([], [])
    assert m["n"] == 0
    assert m["featured_precision"] == 0.0
    assert m["relevance_mae"] == 0.0


# --------------------------------------------------------------------------- #
# Gold I/O + grow + label
# --------------------------------------------------------------------------- #
def test_gold_roundtrip(tmp_path):
    rows = [{"id": "x", "human": {"featured": True, "relevance": 9,
                                  "category": "ai"}}]
    ev.save_gold(tmp_path, rows)
    assert ev.gold_path(tmp_path).exists()
    assert ev.load_gold(tmp_path) == rows


def test_grow_dedups_and_respects_k():
    gold = [{"id": "a"}]
    cands = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]
    out = ev.grow(gold, cands, k=2)
    ids = [g["id"] for g in out]
    assert ids == ["a", "b", "c"]  # a kept, b+c added, d dropped by k


def test_label_upserts_and_confirms():
    gold = [{"id": "a", "human": {"featured": False, "relevance": 2},
             "confidence": "provisional", "labeled_by": "seed"}]
    out = ev.label(gold, "a", {"featured": True, "relevance": 9, "category": "ai"})
    assert out[0]["human"]["featured"] is True
    assert out[0]["confidence"] == "confirmed"
    assert out[0]["labeled_by"] == "illya"
    # unknown id appends a new confirmed row
    out2 = ev.label(gold, "z", {"featured": True, "relevance": 8})
    assert any(g["id"] == "z" and g["confidence"] == "confirmed" for g in out2)


# --------------------------------------------------------------------------- #
# Candidate build (DB read-only)
# --------------------------------------------------------------------------- #
def _story_id(conn, cid):
    return conn.execute(
        "SELECT story_id FROM clusters WHERE id=?", (cid,)
    ).fetchone()[0]


def test_build_candidates_mixes_provenance(conn, seed):
    # positive: done curation, featured in a daily edition
    pos = seed.cluster(title="Anthropic ships a new model",
                       canonical_url="https://a.example/1")
    seed.article(pos, excerpt="A deep look at the release.")
    seed.curation(pos, status="done", skip=0, relevance_score=9,
                  channels=json.dumps(["ai"]))
    seed.ledger(_story_id(conn, pos), "daily", edition_key="2026-07-01")

    # negative: a skipped curation
    neg = seed.cluster(title="Yet another minor point release",
                       canonical_url="https://a.example/2")
    seed.curation(neg, status="skipped", relevance_score=2,
                  channels=json.dumps(["devtools"]))

    # hard-negative: high deterministic score, never curated
    hard = seed.cluster(title="A loud but shallow security story",
                        canonical_url="https://a.example/3", score=8.0)

    cands = ev.build_candidates(conn)
    by_prov = {c["provenance"]: c for c in cands}
    assert by_prov["edition"]["human"]["featured"] is True
    assert by_prov["skipped"]["human"]["featured"] is False
    assert by_prov["top-uncurated"]["human"]["featured"] is False
    # excerpt falls back to title when no article
    assert by_prov["top-uncurated"]["excerpt"] == "A loud but shallow security story"
    _ = hard


# --------------------------------------------------------------------------- #
# Judge replay (stub backend — no metered call)
# --------------------------------------------------------------------------- #
def test_run_with_stub_judge():
    gold = [_gold("a", True, 9), _gold("b", False, 2)]
    calls = []

    def stub(ex, prompt):
        calls.append(ex["id"])
        # judge says: feature 'a' (rel 9), skip 'b'
        if ex["id"] == "a":
            return {"relevance_score": 9, "skip": False, "channels": ["ai"]}
        return {"relevance_score": 2, "skip": True, "channels": ["ai"]}

    res = ev.run(gold, backend="local", date="2026-07-18", judge_fn=stub)
    assert set(calls) == {"a", "b"}
    m = res["metrics"]
    assert m["backend"] == "local"
    assert m["cost_usd"] == 0.0
    assert m["agreement_featured"] == 1.0
    assert len(res["predictions"]) == 2


def test_write_and_latest_result(tmp_path):
    res = {"metrics": {"n": 1}, "predictions": []}
    ev.write_result(tmp_path, "2026-07-18", res)
    assert ev.latest_result(tmp_path)["metrics"]["n"] == 1
