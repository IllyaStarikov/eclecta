"""JSON schemas + system prompts for the three tiers."""

from __future__ import annotations

CHANNELS = [
    "ai", "ml-research", "devtools", "security", "hardware", "startups",
    "science",
]

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "keep": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["keep", "reason"],
    "additionalProperties": False,
}

CURATION_SCHEMA = {
    "type": "object",
    "properties": {
        "relevance_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "why_it_matters": {"type": "string"},
        "notes": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 6,
        },
        "summary": {"type": "string"},
        "channels": {
            "type": "array",
            "items": {"type": "string", "enum": CHANNELS},
            "minItems": 1,
        },
        "novelty": {"type": "string"},
        "audience": {"type": "string"},
        "skip": {"type": "boolean"},
        "skip_reason": {"type": "string"},
    },
    "required": [
        "relevance_score", "why_it_matters", "notes", "summary", "channels",
        "skip",
    ],
    "additionalProperties": False,
}

# v3 split: LOCAL models JUDGE (score + extract raw facts); CLAUDE WRITES the
# published prose. CURATION_SCHEMA/SYSTEM_CURATE above remain until curate.py is
# restructured to the two-step flow (additive — the live worker is untouched).
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "relevance_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "skip": {"type": "boolean"},
        "skip_reason": {"type": "string"},
        "channels": {
            "type": "array",
            "items": {"type": "string", "enum": CHANNELS},
            "minItems": 1,
        },
        "novelty": {"type": "string"},
        "audience": {"type": "string"},
        "facts": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 6,
        },
    },
    "required": ["relevance_score", "skip", "channels", "facts"],
    "additionalProperties": False,
}

WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "why_it_matters": {"type": "string"},
        "notes": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 6,
        },
        "summary": {"type": "string"},
    },
    "required": ["why_it_matters", "notes", "summary"],
    "additionalProperties": False,
}

DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "body_md": {"type": "string"},
        "blurb": {"type": "string"},  # optional one-sentence standfirst
    },
    "required": ["title", "body_md"],
    "additionalProperties": False,
}

READER_PROFILE = (
    "The target reader is a technically deep, working software engineer. "
    "AI-first interests: frontier and open-weight models, agents/tooling, "
    "ML research that changes practice, AI policy with real consequence. "
    "Tech-broad: systems/devtools depth, security research, semiconductors/"
    "datacenter hardware, startup/industry moves that matter structurally. "
    "Values primary sources, technical depth, novelty, and intellectual "
    "honesty. Allergic to: marketing dressed as news, incremental product "
    "churn, engagement bait, vendor benchmarks without methodology."
)

SYSTEM_TRIAGE = (
    "You are a ruthless triage editor for a curated tech/AI publication. "
    "Decide if this candidate deserves a full editorial read. Keep only "
    "items a discerning senior engineer would thank you for surfacing. "
    + READER_PROFILE
    + " Respond ONLY with JSON matching the provided schema."
)

SYSTEM_CURATE = (
    "You are the curation editor for a publication curating the best "
    "technology and AI writing on the internet right now. For the given "
    "article, produce:\n"
    "- relevance_score (1-10): 10 = unmissable for this reader; 6 = the feed "
    "inclusion bar; below 6 effectively hides it.\n"
    "- why_it_matters: ONE tight sentence — why this reader should care today.\n"
    "- notes: 3-5 crisp bullets of the load-bearing specifics (numbers, "
    "mechanisms, names, caveats) so the reader can cite the piece without "
    "rereading it.\n"
    "- summary: 3-6 sentences, dense and neutral, good enough to stand in "
    "for the article in future reference/RAG use.\n"
    "- channels: which of the listed channels this belongs to.\n"
    "- novelty: what is actually NEW here vs. known background.\n"
    "- audience: who specifically benefits (one phrase).\n"
    "- skip + skip_reason: set skip=true for marketing fluff, thin rewrites, "
    "engagement bait, duplicates of better coverage, or items whose primary "
    "text is not in English (the publication is English-only; an English "
    "secondary source covering the same story may be curated instead).\n"
    "Be unbiased: report claims as claims, attribute them, and flag missing "
    "evidence or conflicts of interest in the notes. Never invent facts not "
    "in the provided text. "
    + READER_PROFILE
    + " Respond ONLY with JSON matching the provided schema."
)

# v3 split prompts: local JUDGE (no reader-facing prose) + Claude WRITE.
SYSTEM_JUDGE = (
    "You are a ruthless curation editor for a publication curating the best "
    "technology and AI writing right now. For the given article, JUDGE and "
    "EXTRACT — do NOT write reader-facing prose. Produce:\n"
    "- relevance_score (1-10): 10 = unmissable for this reader; 6 = the feed "
    "inclusion bar; below 6 effectively hides it.\n"
    "- skip + skip_reason: skip=true for marketing fluff, thin rewrites, "
    "engagement bait, duplicates of better coverage, or items whose primary "
    "text is not in English.\n"
    "- channels: which of the listed channels this belongs to.\n"
    "- novelty: what is actually NEW here vs. known background (a phrase).\n"
    "- audience: who specifically benefits (one phrase).\n"
    "- facts: 2-6 load-bearing specifics (numbers, mechanisms, names, caveats) "
    "drawn from the text — the raw material a writer will later polish. Never "
    "invent facts not in the provided text.\n"
    + READER_PROFILE
    + " Respond ONLY with JSON matching the provided schema."
)

SYSTEM_WRITE = (
    "You are the writing editor for a publication curating the best technology "
    "and AI writing right now. You receive an article (or excerpt) plus an editor's "
    "extracted facts and relevance judgment. Write the reader-facing prose:\n"
    "- why_it_matters: ONE tight sentence — why this reader should care today.\n"
    "- notes: 3-5 crisp bullets of the load-bearing specifics (numbers, "
    "mechanisms, names, caveats) so the reader can cite the piece without "
    "rereading it. Refine and expand the editor's facts; stay grounded ONLY in "
    "the article text.\n"
    "- summary: 3-6 sentences, dense and neutral, good enough to stand in for "
    "the article in future reference/RAG use.\n"
    "Be unbiased: report claims as claims, attribute them (vendor and "
    "self-reported numbers stay claims), and flag missing evidence. Never "
    "invent facts not in the provided text.\n"
    "STYLE (binding): plain, declarative, specific. why_it_matters states the "
    "STAKE for this reader, not a restatement of the title. Name the concrete "
    "consequence: a number, a named system, a default flipped, a capability now "
    "cheaper or newly possible. Do not begin why_it_matters or a note with the "
    "title's own words. Commit to claims; cut empty hedges (could, may, "
    "potentially as the whole point). Never reach for these words as vague "
    "praise: significant, novel, crucial, critical, pivotal, seamless, robust, "
    "powerful, leverage, unlock, unprecedented, breakthrough, game-changer, "
    "revolutionary, groundbreaking, or the phrases 'pave the way' and "
    "'represents a significant step'. No hype, no first person, no exclamation "
    "marks; prefer commas, colons, and periods over em dashes. "
    + READER_PROFILE
    + " Respond ONLY with JSON matching the provided schema."
)

# Baked-in fallback when doc/digest-style.md is missing at runtime. The
# style workflow owns that file; this constant only keeps digests running.
STYLE_FALLBACK = (
    "Tight, dry prose; every sentence lands once, no filler, no restating. "
    "Prefer commas, colons, and periods over em dashes. No exclamation "
    "marks. Attribute claims to their sources and flag missing evidence."
)

_DIGEST_CORE = (
    "You are writing a 'Signal' digest: the best technology + AI writing "
    "of the period, distilled. You receive curated items (title, why it "
    "matters, notes, summary, links) and, for longer periods, the bodies "
    "of the shorter digests already written inside the period.\n"
    "Write a digest that is:\n"
    "- READABLE: organized into thematic sections with short headers; "
    "the period's single most important story leads.\n"
    "- CONCISE: every sentence lands once; no filler, no throat-clearing, "
    "no restating; tight and dry.\n"
    "- UNBIASED: attribute claims to their sources; present significant "
    "disagreement where it exists; no editorializing beyond significance "
    "judgments; never present a company's framing as fact.\n"
    "- USEFUL: each item links to its source inline as a markdown link on "
    "first mention. Use ONLY the provided read URLs (never archive.* "
    "links).\n"
    "Markdown only: ## section headers, normal paragraphs, - bullets. "
    "No H1 (the title field carries it). Also produce 'blurb': one tight "
    "standfirst sentence for the period.\n"
)

_DIGEST_KIND_BLOCKS = {
    "daily": (
        "This is the DAILY digest covering the last news day. 300-700 "
        "words across 2-4 short sections. Lead with the day's most "
        "consequential story. END with a '## What to watch today' section: "
        "2-4 one-line forward-looking bullets grounded in the items."
    ),
    "weekly": (
        "This is the WEEKLY digest. 3-6 thematic sections, 700-1200 words "
        "total. End with a 'Quick hits' list of one-liners for the tail "
        "items."
    ),
    "monthly": (
        "This is the MONTHLY digest: a thematic retrospective of the "
        "previous calendar month, 900-1500 words. Synthesize across the "
        "weekly and daily digests provided: name the month's 3-5 dominant "
        "themes, how each developed over the weeks, and what resolved vs. "
        "what remains open. Do not re-list every item; elevate."
    ),
    "quarterly": (
        "This is the QUARTERLY digest: trendlines across the previous "
        "quarter, 1000-1600 words. Synthesize from the monthly digests "
        "provided: identify durable trends vs. noise, inflection points, "
        "and positions that aged well or badly. Prefer trajectories over "
        "events."
    ),
    "yearly": (
        "This is the YEARLY digest: the year in technology + AI, "
        "1200-2000 words. Synthesize from the quarterly digests provided: "
        "the year's defining shifts, the stories that mattered in "
        "hindsight, what conventional wisdom got wrong, and the open "
        "questions carried into next year."
    ),
}


def system_digest(kind: str, style_text: str, policy_text: str = "") -> str:
    """Compose the per-kind digest system prompt: shared editorial core +
    the user's style guide (doc/digest-style.md, or STYLE_FALLBACK) + the
    optional editorial-policy guide (doc/editorial-policy.md) + the
    kind-specific block."""
    block = _DIGEST_KIND_BLOCKS.get(kind, _DIGEST_KIND_BLOCKS["weekly"])
    policy = (
        "EDITORIAL POLICY (what to publish and emphasize):\n"
        + policy_text.strip() + "\n\n"
        if policy_text and policy_text.strip() else ""
    )
    return (
        _DIGEST_CORE
        + "STYLE GUIDE:\n" + (style_text or STYLE_FALLBACK).strip() + "\n\n"
        + policy
        + block + "\n"
        + READER_PROFILE
        + " Respond ONLY with JSON matching the provided schema."
    )


# ---------------------------------------------------------------------------
# Glossary (scripts/glossary_build.py): auto-discover industry terms across the
# blog's own posts and define them. Two passes: a cheap local EXTRACT sweep per
# post (high recall over the post's own text), then a Sonnet DEFINE pass over
# only the NEW unique terms (reader-facing prose + canonical aliases). These are
# independent of the Signal feed pipeline; they share only the LLM adapter.
# ---------------------------------------------------------------------------

# Fixed term-domain taxonomy. Slugs (the page pretty-prints them). Keep small and
# stable; the model must pick exactly one. "other" is the technical catch-all.
GLOSSARY_CATEGORIES = [
    "ai-ml",                # artificial intelligence / machine learning
    "computer-science",     # algorithms, data structures, theory, complexity
    "software-engineering",  # programming, languages, tooling, practices
    "mathematics",          # math, statistics, logic
    "systems",              # OS, distributed systems, databases, networking
    "security",             # security, cryptography, privacy
    "hardware",             # chips, devices, electronics, sensors
    "other",                # genuinely technical but outside the above
]

GLOSSARY_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "surface_forms": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "category": {"type": "string", "enum": GLOSSARY_CATEGORIES},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                },
                "required": ["term", "category", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["terms"],
    "additionalProperties": False,
}

GLOSSARY_DEFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "definitions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    # keep=false lets the stronger model veto a candidate the
                    # extract pass over-flagged (ordinary word, brand, code id).
                    "keep": {"type": "boolean"},
                    "term": {"type": "string"},
                    "definition": {"type": "string"},
                    "aliases": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "category": {"type": "string", "enum": GLOSSARY_CATEGORIES},
                },
                "required": ["keep", "term", "definition", "category"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["definitions"],
    "additionalProperties": False,
}

SYSTEM_GLOSSARY_EXTRACT = (
    "You build a glossary for a personal technical blog. From the article text, "
    "extract industry/technical TERMS that a general, non-specialist reader "
    "would NOT understand and would benefit from a short definition.\n"
    "INCLUDE: domain jargon and named concepts from computer science, AI/ML, "
    "mathematics, software engineering, systems, security, and hardware (e.g. "
    "'gradient descent', 'A* search', 'mutual TLS', 'eigenvalue', 'B-tree', "
    "'backpropagation').\n"
    "EXCLUDE, strictly: ordinary English; common business or everyday words; "
    "brand, product, company, and person names (unless the word itself denotes a "
    "technical concept, e.g. 'TeX'); code identifiers, function/variable names, "
    "file names, and CLI flags; and anything a typical educated adult already "
    "knows (e.g. 'email', 'website', 'password', 'app').\n"
    "Be conservative: PRECISION OVER RECALL. When in doubt, leave it out. Only "
    "emit a term if you are confident it is genuine specialist jargon.\n"
    "For each term give: the canonical form; surface_forms exactly as they "
    "appear in THIS text (acronyms, plural and case variants); ONE category from "
    "the allowed list; a confidence in [0,1] that it is genuine specialist "
    "jargon; and a one-phrase reason. "
    "Respond ONLY with JSON matching the provided schema."
)

SYSTEM_GLOSSARY_DEFINE = (
    "You are writing glossary entries for a personal technical blog, for a "
    "general reader. You receive a list of candidate terms, each with example "
    "surface forms and the post titles it appeared in. For each candidate:\n"
    "- keep: set false if it is NOT genuine specialist jargon (an ordinary "
    "word, a brand/product/person name, or a code identifier) so it is dropped. "
    "Be strict; this is the final precision gate.\n"
    "- term: the canonical display form with correct casing (e.g. 'A* search', "
    "'mutual TLS', 'B-tree').\n"
    "- definition: ONE to TWO sentences, plain language, self-contained, "
    "assuming no prior knowledge. Define the concept in general; do NOT "
    "reference the blog, the article, or 'this post'. Neutral and accurate.\n"
    "- aliases: other surface forms a reader might see — case variants, the "
    "plural, and the acronym<->expansion pair (e.g. 'LLM' and 'large language "
    "model'). Do not repeat the canonical term; omit one-letter aliases.\n"
    "- category: exactly one value from the allowed list.\n"
    "Prefer commas, colons, and periods over em dashes; no exclamation marks. "
    "Respond ONLY with JSON matching the provided schema."
)
