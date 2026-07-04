"""Topic lexicons for channel matching + personal-interest weighting.

Two layers:
  1. A curated base lexicon per channel (deterministic, code-owned).
  2. Personal-interest terms extracted from doc/ask-me-scope.md — hash-gated:
     re-extracted only when that file's content hash changes; cached in
     config/signal.topics.json. Extraction is plain frequency analysis (no
     LLM): the per-item scoring path must stay free and deterministic.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set

from .config import Config, sha256_file

# Channel lexicons: lowercase substrings/tokens matched against titles.
BASE_LEXICON: Dict[str, List[str]] = {
    "ai": [
        "ai", "llm", "gpt", "claude", "gemini", "openai", "anthropic",
        "deepmind", "mistral", "llama", "agent", "agentic", "chatbot",
        "copilot", "prompt", "rag", "inference", "alignment", "frontier model",
        "foundation model", "multimodal", "genai", "generative",
    ],
    "ml-research": [
        "arxiv", "paper", "transformer", "attention", "diffusion",
        "reinforcement learning", "rlhf", "fine-tun", "pretrain", "benchmark",
        "eval", "dataset", "neural", "gradient", "embedding", "tokenizer",
        "distillation", "quantization", "interpretability", "scaling law",
        "mixture of experts", "moe",
    ],
    "devtools": [
        "rust", "python", "golang", "typescript", "javascript", "compiler",
        "kubernetes", "docker", "database", "postgres", "sqlite", "api",
        "framework", "open source", "github", "cli", "sdk", "devops", "ci/cd",
        "observability", "linux", "kernel", "git", "vscode", "neovim",
        "programming", "refactor", "debugger",
    ],
    "security": [
        "vulnerability", "cve", "exploit", "ransomware", "malware", "breach",
        "zero-day", "0-day", "phishing", "botnet", "encryption", "infosec",
        "appsec", "supply chain attack", "backdoor", "patch", "security",
    ],
    "hardware": [
        "chip", "semiconductor", "gpu", "tpu", "nvidia", "amd", "intel",
        "tsmc", "datacenter", "silicon", "fab", "wafer", "cpu", "soc", "ram",
        "asic", "risc-v", "arm", "apple silicon", "hbm", "interconnect",
    ],
    "startups": [
        "startup", "funding", "series a", "series b", "seed round", "vc",
        "venture", "acquisition", "acquire", "ipo", "valuation", "yc",
        "y combinator", "founder", "layoff", "antitrust", "ftc", "lawsuit",
    ],
    "science": [
        "quantum", "physics", "biology", "genome", "crispr", "fusion",
        "telescope", "spacex", "nasa", "climate", "battery", "materials",
        "neuroscience", "mathematics", "theorem", "protein",
    ],
}

_STOP = {
    "the", "and", "for", "that", "this", "with", "from", "have", "what",
    "when", "where", "which", "while", "about", "into", "their", "there",
    "would", "could", "should", "been", "being", "more", "most", "some",
    "such", "than", "then", "them", "they", "your", "just", "like", "want",
    "will", "make", "made", "good", "best", "really", "thing", "things",
    "post", "posts", "blog", "write", "writing", "page", "site", "https",
    "http", "com", "org",
}

_WORD_RE = re.compile(r"[a-z][a-z0-9\-]{3,}")


def _extract_interests(text: str, top_n: int = 120) -> Dict[str, float]:
    """Frequency-based interest terms from the scope doc (deterministic)."""
    counts: Dict[str, int] = {}
    base_terms = {t for terms in BASE_LEXICON.values() for t in terms}
    for w in _WORD_RE.findall(text.lower()):
        if w in _STOP or w in base_terms:
            continue
        counts[w] = counts.get(w, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    out: Dict[str, float] = {}
    for w, n in ranked[:top_n]:
        if n < 3:
            break
        out[w] = min(1.0, 0.3 + 0.1 * n)  # 3 hits -> 0.6, caps at 1.0
    return out


def build_or_load(cfg: Config) -> Dict[str, Any]:
    """Return {"channels": BASE_LEXICON, "interests": {...}} — rebuilt only
    when doc/ask-me-scope.md's hash changes."""
    topics_path = cfg.repo_root / "config" / "signal.topics.json"
    scope_path = cfg.repo_path("doc/ask-me-scope.md")
    scope_hash = sha256_file(scope_path)

    if topics_path.exists():
        try:
            cached = json.loads(topics_path.read_text())
            if cached.get("scope_hash") == scope_hash:
                return cached
        except ValueError:
            pass

    interests: Dict[str, float] = {}
    if scope_path.exists():
        interests = _extract_interests(scope_path.read_text(errors="ignore"))
    data = {
        "scope_hash": scope_hash,
        "channels": BASE_LEXICON,
        "interests": interests,
    }
    topics_path.write_text(json.dumps(data, indent=1) + "\n")
    return data


def match_channels(title: str, topics_data: Dict[str, Any]) -> Set[str]:
    """Channels whose lexicon matches the title (substring, lowercase)."""
    t = " %s " % (title or "").lower()
    hit = set()
    for channel, terms in (topics_data.get("channels") or {}).items():
        for term in terms:
            if (" %s " % term) in t or (len(term) > 4 and term in t):
                hit.add(channel)
                break
    return hit


def interest_score(title: str, topics_data: Dict[str, Any]) -> float:
    """0..1: how strongly the title matches personal-interest terms."""
    t = (title or "").lower()
    best = 0.0
    for term, w in (topics_data.get("interests") or {}).items():
        if term in t:
            best = max(best, float(w))
    return best


# ── Public 2-level taxonomy ─────────────────────────────────────────────────
# Six top categories, each with subcategories. A pick gets ONE primary category
# (sectioning/routing) plus cross-cutting subcategories. The lexicon MUST mirror
# the site's src/lib/taxonomy.ts so deterministic categorization agrees on both
# sides. Lowercase substrings matched against a padded " title ".
TAXONOMY: Dict[str, Dict[str, Any]] = {
    "ai": {
        "name": "AI",
        "match": ["artificial intelligence", " ai ", "a.i.", "llm",
                  "machine learning", "neural net", "openai", "anthropic",
                  "deepmind", "gpt", "claude", "gemini", "llama", "mistral",
                  "chatbot"],
        "subs": {
            "models": ["frontier model", "open-weight", "open weight",
                       "foundation model", "multimodal", "model release",
                       "context window", "parameters", "gpt-", "llama ",
                       "mixture of experts"],
            "agents": ["agent", "agentic", "tool use", "tool-use", "mcp",
                       "autonomy", "autonomous", "orchestrat", "agent loop"],
            "evals": ["benchmark", "eval", "leaderboard", "mmlu", "arena",
                      "state-of-the-art", "sota", "pass@"],
            "safety": ["alignment", "interpretab", "rlhf", "jailbreak",
                       "red team", "red-team", "ai safety", "ai policy",
                       "guardrail", "model welfare", "refus"],
            "apps": ["copilot", "assistant", "rag", "retrieval-augmented",
                     "inference", "genai", "generative ai", "prompt"],
        },
    },
    "research": {
        "name": "Research",
        "match": ["paper", "arxiv", "study", "researchers", "preprint",
                  "journal", "findings", "experiment"],
        "subs": {
            "ml": ["transformer", "diffusion", "reinforcement learning",
                   "gradient", "fine-tun", "embedding", "neural architecture",
                   "self-supervised", "dataset"],
            "systems": ["algorithm", "complexity", "distributed system",
                        "consensus", "theory", "random graph", "data structure",
                        "formal verification"],
            "science": ["physics", "quantum", "biology", "genom", "chemistry",
                        "astronom", "climate", "neuroscience",
                        "materials science", "particle", "protein", "fusion"],
            "math": ["mathematic", "theorem", "conjecture", "number theory",
                     "topology", "combinatoric", "prime"],
        },
    },
    "software": {
        "name": "Software",
        "match": ["programming", "open source", "open-source", "library",
                  "framework", "developer", " api ", "codebase"],
        "subs": {
            "languages": ["rust", "python", "golang", "typescript",
                          "javascript", "c++", "compiler", "language",
                          "runtime", "wasm", "webassembly", "zig"],
            "data": ["database", "sql", "postgres", "sqlite", "data pipeline",
                     "warehouse", "duckdb", "kafka", "query engine"],
            "infra": ["kubernetes", "docker", "cloud", "serverless", "devops",
                      "observability", "infrastructure", "deployment",
                      "terraform"],
            "web": ["browser", " css", "html", "frontend", "react",
                    "web platform", "dom ", "http"],
            "practice": ["testing", "refactor", "code review", "technical debt",
                         "architecture", "postmortem", "best practice",
                         "maintainab"],
        },
    },
    "security": {
        "name": "Security",
        "match": ["security", "vulnerab", "exploit", "malware", "breach",
                  "hacked", "cve", "ransomware", "phishing", "cyber"],
        "subs": {
            "vulns": ["cve", "vulnerab", "zero-day", "zero day", "0day", "rce",
                      "privilege escalation", "buffer overflow",
                      "patch tuesday"],
            "research": ["exploit", "reverse engineer", "fuzzing", "red team",
                         "threat actor", "attack surface", "side-channel",
                         "side channel"],
            "supplychain": ["supply chain", "supply-chain", "malware",
                            "npm package", "malicious package", "backdoor",
                            "typosquat", "compromised"],
            "privacy": ["privacy", "encryption", "cryptograph", "surveillance",
                        "tracking", "anonym", "end-to-end",
                        "certificate authority"],
        },
    },
    "hardware": {
        "name": "Hardware",
        "match": ["chip", "silicon", "gpu", "processor", "semiconductor",
                  "hardware", "datacenter", "data center", "wafer"],
        "subs": {
            "silicon": ["chip", " gpu", " cpu", "semiconductor", "tsmc",
                        "nvidia", " arm ", "risc-v", "transistor", "nanometer",
                        "wafer", "fab "],
            "datacenter": ["datacenter", "data center", "power grid", "cooling",
                           "megawatt", "gigawatt", "hyperscale", "interconnect"],
            "devices": ["robot", "wearable", "sensor", "drone",
                        "autonomous vehicle", "edge device", "humanoid"],
        },
    },
    "industry": {
        "name": "Industry",
        "match": ["funding", "startup", "acquisition", " ipo", "antitrust",
                  "regulat", "lawsuit", "billion", "revenue", "layoff"],
        "subs": {
            "funding": ["funding", "raises", "series a", "series b", "series c",
                        "valuation", "acqui", "merger", " ipo", "venture",
                        "seed round", "billion"],
            "policy": ["antitrust", "regulat", "lawsuit", " court", " ftc",
                       "export control", "sanction", "legislation", " ban ",
                       "ruling", "directive"],
            "labor": ["layoff", "hiring", "union", "workforce", "remote work",
                      "job cuts", "talent"],
            "business": ["partnership", "earnings", "revenue", "expansion",
                         "shutdown", "rebrand", "ceo"],
        },
    },
}

# Legacy pipeline channel slug -> primary category (fallback signal).
CHANNEL_TO_CATEGORY: Dict[str, str] = {
    "ai": "ai",
    "ml-research": "research",
    "devtools": "software",
    "security": "security",
    "hardware": "hardware",
    "startups": "industry",
    "science": "research",
    "news": "industry",
}

# Tie-break order when two categories score equally (most consequential first).
CATEGORY_PRIORITY = ["security", "ai", "hardware", "research", "software",
                     "industry"]


def match_taxonomy(title: str, channels: Optional[List[str]] = None) -> Dict[str, Any]:
    """Derive a primary category + subcategories from a title and the pipeline
    channel tags. Deterministic; mirrors src/lib/taxonomy.ts deriveCategory."""
    t = " %s " % (title or "").lower()
    sub_hits: Dict[str, List[str]] = {}
    score: Dict[str, int] = {}
    for cat, spec in TAXONOMY.items():
        subs = [s for s, terms in spec["subs"].items()
                if any(m in t for m in terms)]
        s = len(subs) * 2
        if any(m in t for m in spec["match"]):
            s += 1
        if subs:
            sub_hits[cat] = subs
        if s:
            score[cat] = s
    for ch in (channels or []):
        c = CHANNEL_TO_CATEGORY.get(ch)
        if c:
            score[c] = score.get(c, 0) + 1

    primary = ""
    best = -1
    for slug, s in score.items():
        if s > best or (
            s == best
            and CATEGORY_PRIORITY.index(slug) < CATEGORY_PRIORITY.index(primary)
        ):
            best = s
            primary = slug
    if not primary:
        primary = next(
            (CHANNEL_TO_CATEGORY[c] for c in (channels or [])
             if c in CHANNEL_TO_CATEGORY),
            "industry",
        )
    return {"category": primary, "subcategories": sub_hits.get(primary, [])[:3]}
