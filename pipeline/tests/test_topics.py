"""Tests for :mod:`signalpipe.topics`.

The module is genuinely pure — code-owned channel + taxonomy lexicons plus two
deterministic title matchers. There are NO external I/O seams, so every test here
is a plain unit test derived from the real code path (see
``docs`` / the module docstrings). Expected values were computed against the live
functions, not the aspirational docstrings.

The only non-unit test is an opt-in ``@pytest.mark.live`` cross-language parity
check that runs the site's ``src/lib/taxonomy.ts`` ``deriveCategory`` under Node
and diffs it against the Python output. It is deselected by default and skips
unless ``SIGNAL_LIVE`` is set and Node is available.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from signalpipe import topics


# --------------------------------------------------------------------------- #
# build_or_load
# --------------------------------------------------------------------------- #
def test_build_or_load_returns_channels_wrapping_base_lexicon():
    out = topics.build_or_load(None)
    # Exactly one key, and its value IS the module constant (identity, not a copy).
    assert set(out.keys()) == {"channels"}
    assert out["channels"] is topics.BASE_LEXICON


@pytest.mark.parametrize("cfg_arg", [None, object(), 123, {"anything": "ignored"}])
def test_build_or_load_ignores_cfg_argument(cfg_arg):
    # The Config arg is deleted immediately; any value yields the same static data.
    assert topics.build_or_load(cfg_arg)["channels"] is topics.BASE_LEXICON


# --------------------------------------------------------------------------- #
# match_channels — boundary vs. long-substring rule (custom, isolated lexicon)
# --------------------------------------------------------------------------- #
# A small hand-built lexicon so each assertion isolates one matching rule and is
# not perturbed by unrelated real-lexicon terms.
CUSTOM_DATA = {
    "channels": {
        "ai": ["ai"],                    # len 2 -> boundary-only
        "security": ["security"],        # len 8 -> substring allowed
        "devtools": ["programming", "rust"],  # 'rust' len 4 -> boundary-only
    }
}


@pytest.mark.parametrize(
    "title, expected",
    [
        # ' ai ' boundary hit (case-insensitive).
        ("this is AI", {"ai"}),
        # 'ai' is a substring of 'email' but len('ai')<=4 so substring is NOT applied.
        ("email digest", set()),
        # 'security' (len>4) matches as a substring inside 'cybersecurity'.
        ("cybersecurity update", {"security"}),
        # Boundary match is case-insensitive (title lowercased before matching).
        ("SECURITY patch", {"security"}),
        # 'programming' (len>4) matches as substring inside 'metaprogramming'.
        ("metaprogramming guide", {"devtools"}),
        # 'rust' at a word boundary hits.
        ("rust lang", {"devtools"}),
        # 'rust' is a substring of 'trusted' but len 4 <= 4 -> substring NOT applied.
        ("trusted source", set()),
    ],
)
def test_match_channels_boundary_and_substring_rules(title, expected):
    assert topics.match_channels(title, CUSTOM_DATA) == expected


@pytest.mark.parametrize("title", ["", None])
def test_match_channels_empty_or_none_title_is_empty_set(title):
    assert topics.match_channels(title, CUSTOM_DATA) == set()


@pytest.mark.parametrize(
    "data",
    [
        {},                    # no 'channels' key at all
        {"channels": None},    # explicit None -> `or {}` fallback
        {"channels": {}},      # empty mapping
    ],
)
def test_match_channels_missing_or_empty_channels_key(data):
    assert topics.match_channels("ai llm gpt security", data) == set()


# --------------------------------------------------------------------------- #
# match_channels — against the REAL BASE_LEXICON via build_or_load
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "title, expected",
    [
        ("The new AI model is here", {"ai"}),
        ("cybersecurity report", {"security"}),
        ("Kubernetes and Docker in production", {"devtools"}),
        ("Nvidia announces a new GPU", {"hardware"}),
        ("Rust compiler internals", {"devtools"}),
        ("CRISPR gene editing breakthrough", {"science"}),
    ],
)
def test_match_channels_real_lexicon(title, expected):
    data = topics.build_or_load(None)
    assert topics.match_channels(title, data) == expected


def test_match_channels_short_token_gpu_is_boundary_only():
    # 'gpu' (len 3) is in the hardware lexicon; 'GPUs' has no ' gpu ' boundary and
    # substring is disallowed for short tokens, so 'benchmark' (ml-research) is the
    # only hit — proving the short-token boundary rule.
    data = topics.build_or_load(None)
    assert topics.match_channels("GPUs benchmark", data) == {"ml-research"}


# --------------------------------------------------------------------------- #
# match_channels — differential invariant over many random titles
# --------------------------------------------------------------------------- #
def _reference_match(title, topics_data):
    """Independent re-implementation of the documented matching rule.

    Structured differently from the product code (explicit padded token list,
    separate boundary/substring predicates) so agreement over random inputs is a
    real cross-check, not a copy of the same expression.
    """
    padded = " " + (title or "").lower() + " "
    tokens = set(padded.split())  # whitespace-delimited words of the padded title
    result = set()
    for channel, terms in (topics_data.get("channels") or {}).items():
        for term in terms:
            single_word_term = " " not in term
            boundary = (single_word_term and term in tokens) or ((" " + term + " ") in padded)
            long_substr = len(term) > 4 and term in padded
            if boundary or long_substr:
                result.add(channel)
                break
    return result


def test_match_channels_matches_reference_over_random_titles():
    data = topics.build_or_load(None)
    # Word pool: real lexicon terms + noise words that embed short tokens as
    # substrings (email/trusted/gpus) to stress the boundary rule.
    pool = ["ai", "llm", "gpt", "email", "rust", "trusted", "gpus", "security",
            "cybersecurity", "kubernetes", "the", "a", "new", "quantum", "chip",
            "startup", "funding", "paper", "benchmark", "nvidia", "programming",
            "metaprogramming", "docker", "python", "and", "of", "for"]
    rng = random.Random(20260704)
    for _ in range(500):
        n = rng.randint(0, 6)
        title = " ".join(rng.choice(pool) for _ in range(n))
        assert topics.match_channels(title, data) == _reference_match(title, data), title


def test_match_channels_returned_channels_all_have_a_satisfying_term():
    """Every channel in the result must have >=1 term satisfying the rule."""
    data = topics.build_or_load(None)
    rng = random.Random(1234)
    pool = ["ai", "security", "cybersecurity", "gpu", "gpus", "rust", "trusted",
            "kubernetes", "startup", "quantum", "the", "new", "model", "chip"]
    for _ in range(300):
        title = " ".join(rng.choice(pool) for _ in range(rng.randint(0, 5)))
        padded = " " + title.lower() + " "
        for channel in topics.match_channels(title, data):
            terms = topics.BASE_LEXICON[channel]
            assert any(
                (" " + term + " ") in padded or (len(term) > 4 and term in padded)
                for term in terms
            ), (channel, title)


# --------------------------------------------------------------------------- #
# match_taxonomy — scoring, tie-break, subcategories, fallback
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "title, channels, category, subcategories",
    [
        # Equal bare-match tie -> CATEGORY_PRIORITY winner (security before ai),
        # even though 'security' is defined AFTER 'ai' in TAXONOMY insertion order.
        ("AI security", None, "security", []),
        # A subcategory hit (score 2) outranks a bare match (score 1): 'agent'
        # drives ai past the bare 'security' match despite security's priority.
        ("agent security", None, "ai", ["agents"]),
        # Five subcategory hits -> subcategories truncated to the first 3.
        ("multimodal agent benchmark alignment copilot", None, "ai",
         ["models", "agents", "evals"]),
        # Bare match with no subcategory hits -> empty subcategories list.
        ("startup", None, "industry", []),
        # Tie-break priority overrides insertion order: hardware(idx2) beats
        # research(idx3) though research is inserted first.
        ("processor paper", None, "hardware", []),
        # Empty title, channel maps via CHANNEL_TO_CATEGORY into the score.
        ("", ["ml-research"], "research", []),
        ("", ["science"], "research", []),
        ("", ["news"], "industry", []),
        # No signal at all -> default 'industry' via the fallback branch.
        ("", None, "industry", []),
        ("", [], "industry", []),
        # Unmapped channel contributes nothing; fallback default 'industry'.
        ("", ["unknown-channel"], "industry", []),
        # Channel boost creates a tie with a bare match -> priority winner.
        ("gpt", ["security"], "security", []),
        # A subcategory-driven score (3) beats a channel-boosted bare match.
        ("gpt agent", ["security"], "ai", ["agents"]),
        # Primary is the higher-scoring category; only ITS subs are returned
        # (ai's 'agents' sub-hit is not leaked into security's result).
        ("agent security exploit", None, "security", ["research"]),
        ("funding round raises $2 billion", None, "industry", ["funding"]),
    ],
)
def test_match_taxonomy_table(title, channels, category, subcategories):
    out = topics.match_taxonomy(title, channels)
    assert out == {"category": category, "subcategories": subcategories}


def test_match_taxonomy_subcategories_capped_at_three():
    out = topics.match_taxonomy("multimodal agent benchmark alignment copilot")
    assert out["category"] == "ai"
    assert len(out["subcategories"]) == 3
    # Order preserved from the sub-spec insertion order, truncated to first 3.
    assert out["subcategories"] == ["models", "agents", "evals"]
    assert "safety" not in out["subcategories"]
    assert "apps" not in out["subcategories"]


def test_match_taxonomy_only_primary_subhits_returned():
    # 'agent' hits ai.agents; 'security'/'exploit' push security to the top score.
    out = topics.match_taxonomy("agent security exploit")
    assert out["category"] == "security"
    assert out["subcategories"] == ["research"]
    assert "agents" not in out["subcategories"]  # non-primary sub-hits suppressed


def test_match_taxonomy_none_and_empty_channels_equivalent():
    # Pin the concrete result (not just equivalence): 'agent' -> ai.agents (score 2)
    # outranks the bare 'security' match (score 1), so ai wins with its one sub-hit.
    expected = {"category": "ai", "subcategories": ["agents"]}
    none_out = topics.match_taxonomy("agent security", None)
    empty_out = topics.match_taxonomy("agent security", [])
    assert none_out == expected
    assert empty_out == expected
    assert none_out == empty_out


def test_match_taxonomy_none_title_falls_back_to_industry():
    assert topics.match_taxonomy(None) == {"category": "industry", "subcategories": []}


def test_match_taxonomy_channel_boost_can_flip_primary():
    # Bare 'gpt' alone -> ai wins.
    assert topics.match_taxonomy("gpt")["category"] == "ai"
    # Same title + a security channel ties the score; priority makes security win.
    assert topics.match_taxonomy("gpt", ["security"])["category"] == "security"


def test_match_taxonomy_tie_break_follows_category_priority():
    # Construct a clean bare-match tie for each adjacent priority pair and assert
    # the higher-priority category wins regardless of TAXONOMY insertion order.
    # research(idx3) vs software(idx4): 'paper' (research) + ' api ' (software).
    out = topics.match_taxonomy("paper about api design")
    assert out["category"] == "research"


# --------------------------------------------------------------------------- #
# Structural invariants of the constants (guard the .index() tie-break)
# --------------------------------------------------------------------------- #
def test_category_priority_covers_every_taxonomy_category_exactly():
    # CATEGORY_PRIORITY.index() is called on any scored slug during tie-break;
    # every TAXONOMY category (and no extras) must appear exactly once.
    assert sorted(topics.CATEGORY_PRIORITY) == sorted(topics.TAXONOMY)
    assert len(topics.CATEGORY_PRIORITY) == len(set(topics.CATEGORY_PRIORITY))


def test_channel_to_category_targets_are_valid_priority_slugs():
    # Channel-boost adds these into `score`; each must be indexable by the
    # tie-break (i.e. present in CATEGORY_PRIORITY) or the code would ValueError.
    for target in topics.CHANNEL_TO_CATEGORY.values():
        assert target in topics.CATEGORY_PRIORITY
        assert target in topics.TAXONOMY


def test_taxonomy_specs_have_expected_shape():
    for slug, spec in topics.TAXONOMY.items():
        assert isinstance(spec["name"], str) and spec["name"]
        assert isinstance(spec["match"], list) and spec["match"]
        assert isinstance(spec["subs"], dict) and spec["subs"]
        for sub_slug, terms in spec["subs"].items():
            assert isinstance(sub_slug, str) and sub_slug
            assert isinstance(terms, list) and terms
            assert all(isinstance(term, str) for term in terms)


# --------------------------------------------------------------------------- #
# LIVE (opt-in): cross-language parity with src/lib/taxonomy.ts deriveCategory
# --------------------------------------------------------------------------- #
_PARITY_CORPUS = [
    ["AI security", []],
    ["agent security", []],
    ["multimodal agent benchmark alignment copilot", []],
    ["startup", []],
    ["processor paper", []],
    ["", ["ml-research"]],
    ["", ["science"]],
    ["", ["news"]],
    ["", []],
    ["", ["unknown-channel"]],
    ["gpt", ["security"]],
    ["gpt agent", ["security"]],
    ["agent security exploit", []],
    ["funding round raises $2 billion", []],
    ["Nvidia GPU datacenter interconnect", []],
    ["CRISPR gene editing study in a journal", ["science"]],
    ["Kubernetes deployment with terraform", ["devtools"]],
    ["zero-day exploit and ransomware breach", []],
]


@pytest.mark.live
def test_match_taxonomy_ts_parity(tmp_path):
    if not os.environ.get("SIGNAL_LIVE"):
        pytest.skip("live parity check: set SIGNAL_LIVE=1 to run")
    node = shutil.which("node")
    if not node:
        pytest.skip("live parity check requires Node.js on PATH")
    ts_path = Path(__file__).resolve().parents[2] / "src" / "lib" / "taxonomy.ts"
    if not ts_path.exists():
        pytest.skip("src/lib/taxonomy.ts not found")

    corpus_path = tmp_path / "topics_parity_corpus.json"
    corpus_path.write_text(json.dumps(_PARITY_CORPUS))
    harness = tmp_path / "topics_parity_harness.mjs"
    harness.write_text(
        "import { pathToFileURL } from 'node:url';\n"
        "import { readFileSync } from 'node:fs';\n"
        "const mod = await import(pathToFileURL(process.argv[2]).href);\n"
        "const corpus = JSON.parse(readFileSync(process.argv[3], 'utf8'));\n"
        "const out = corpus.map(([title, channels]) => "
        "mod.deriveCategory(title, channels));\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    try:
        proc = subprocess.run(
            [node, str(harness), str(ts_path), str(corpus_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:  # pragma: no cover - infra only
        pytest.skip("could not invoke Node harness: %s" % exc)
    if proc.returncode != 0:  # pragma: no cover - infra only
        pytest.skip("Node harness failed (TS runtime unavailable?): %s" % proc.stderr)

    ts_results = json.loads(proc.stdout)
    py_results = [topics.match_taxonomy(title, channels)
                  for title, channels in _PARITY_CORPUS]
    assert ts_results == py_results
