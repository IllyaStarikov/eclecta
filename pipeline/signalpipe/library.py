"""The Library: a growing world-knowledge wiki, built from what got covered.

Deterministic and non-LLM in v1. Each tracked entity (a company, model,
technology, standard, event, or project — never a person, to avoid biography
risk) gets a dated timeline assembled from the curations that mention it. The
registry grows a few entities per run; only a few pages are rebuilt per run.

Builders only: every function RETURNS (relpath, content) pairs; ``publish.py`` is
the sole writer of the site repo. No archive.* links (reused scrub). The public
site copy is derived by ``publish.publish_library``; the canonical notes live in
``kb/library/``.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
from typing import Any, Dict, List, Tuple

from .publish import _ARCHIVE_RE, no_archive

ALLOWED_TYPES = ("company", "model", "technology", "standard", "event", "project")
REGISTRY_REL = "kb/library/registry.json"
INDEX_REL = "kb/library/index.json"

STORIES_SINCE_DAYS = 365   # how far back a timeline reaches
ACTIVE_SINCE_DAYS = 60     # "fresh activity" = a match this recent
TIMELINE_MAX = 24          # bullets per page

# A conservative starter set of well-known, non-person entities. The registry
# grows from here as these (and later, proposed) entities get coverage.
ENTITY_SEEDS: List[Dict[str, Any]] = [
    {"slug": "anthropic", "name": "Anthropic", "type": "company",
     "aliases": ["anthropic", "claude"]},
    {"slug": "openai", "name": "OpenAI", "type": "company",
     "aliases": ["openai", "chatgpt"]},
    {"slug": "google-deepmind", "name": "Google DeepMind", "type": "company",
     "aliases": ["deepmind", "gemini"]},
    {"slug": "meta-ai", "name": "Meta AI", "type": "company",
     "aliases": ["meta ai", "llama"]},
    {"slug": "nvidia", "name": "Nvidia", "type": "company",
     "aliases": ["nvidia", "cuda"]},
    {"slug": "mistral", "name": "Mistral AI", "type": "company",
     "aliases": ["mistral"]},
    {"slug": "eu-ai-act", "name": "EU AI Act", "type": "standard",
     "aliases": ["eu ai act", "ai act"]},
    {"slug": "rust", "name": "Rust", "type": "technology",
     "aliases": ["rust lang", "rustlang", "rust language"]},
    {"slug": "linux-kernel", "name": "Linux kernel", "type": "project",
     "aliases": ["linux kernel"]},
    {"slug": "webassembly", "name": "WebAssembly", "type": "technology",
     "aliases": ["webassembly", "wasm"]},
    {"slug": "postgresql", "name": "PostgreSQL", "type": "technology",
     "aliases": ["postgresql", "postgres"]},
    {"slug": "kubernetes", "name": "Kubernetes", "type": "technology",
     "aliases": ["kubernetes", "k8s"]},
]


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "entity"


# --------------------------------------------------------------------------- #
# Registry I/O
# --------------------------------------------------------------------------- #
def _registry_path(repo_root) -> pathlib.Path:
    return pathlib.Path(repo_root) / REGISTRY_REL


def load_registry(repo_root) -> List[Dict[str, Any]]:
    p = _registry_path(repo_root)
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def save_registry(repo_root, rows: List[Dict[str, Any]]) -> None:
    p = _registry_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- #
# Matching + story gathering
# --------------------------------------------------------------------------- #
def _matches(entity: Dict[str, Any], title: str) -> bool:
    t = " %s " % (title or "").lower()
    return any(a.lower() in t for a in entity.get("aliases", []))


def _fetch_stories(conn, since_iso: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT c.title, c.canonical_url, cu.why_it_matters, cu.curated_at, "
        "a.source_url, a.read_url "
        "FROM curations cu JOIN clusters c ON c.id = cu.cluster_id "
        "LEFT JOIN articles a ON a.cluster_id = cu.cluster_id "
        "WHERE cu.status='done' AND cu.skip=0 AND cu.curated_at >= ? "
        "ORDER BY cu.curated_at DESC",
        (since_iso,),
    ).fetchall()
    return [dict(r) for r in rows]


def _entity_timeline(entity: Dict[str, Any], stories: List[Dict[str, Any]]):
    hits = [s for s in stories if _matches(entity, s["title"])]
    tl = []
    for s in hits:
        link = no_archive(s.get("source_url")) or no_archive(s.get("read_url")) \
            or no_archive(s.get("canonical_url"))
        tl.append({
            "date": (s["curated_at"] or "")[:10],
            "title": s["title"],
            "link": link,
            "why": (s.get("why_it_matters") or "").strip(),
        })
    return tl


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _render_body(entity: Dict[str, Any], timeline: List[Dict[str, Any]]) -> str:
    lines = ["# %s" % entity["name"], ""]
    lines.append("%s tracked by Eclecta — %d stor%s in coverage."
                 % (entity["type"].capitalize(), len(timeline),
                    "y" if len(timeline) == 1 else "ies"))
    lines.append("")
    lines.append("## Timeline")
    lines.append("")
    if not timeline:
        lines.append("No coverage yet.")
    for e in timeline[:TIMELINE_MAX]:
        title = "[%s](%s)" % (e["title"], e["link"]) if e["link"] else e["title"]
        bullet = "- **%s** — %s" % (e["date"], title)
        if e["why"]:
            bullet += " — %s" % e["why"]
        lines.append(bullet)
    lines.append("")
    return "\n".join(lines)


def _entity_writes(entity, timeline) -> Tuple[str, str]:
    """(kb relpath, content) for one entity's canonical note."""
    body = _render_body(entity, timeline)
    return "kb/library/%s.md" % entity["slug"], body + "\n"


def _index_content(entries: List[Dict[str, Any]]) -> Tuple[str, str]:
    payload = sorted(entries, key=lambda e: e["updated"], reverse=True)
    return INDEX_REL, json.dumps(payload, ensure_ascii=False, indent=2,
                                 sort_keys=True) + "\n"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def propose_entities(conn, existing: List[Dict[str, Any]], k: int,
                     now: datetime.datetime) -> List[Dict[str, Any]]:
    """Up to k seed entities with recent coverage that aren't registered yet.
    Only ever proposes non-person types (the seed set has no people)."""
    have = {e["slug"] for e in existing}
    since = (now - datetime.timedelta(days=ACTIVE_SINCE_DAYS)).isoformat()
    stories = _fetch_stories(conn, since)
    out: List[Dict[str, Any]] = []
    for seed in ENTITY_SEEDS:
        if len(out) >= k:
            break
        if seed["slug"] in have:
            continue
        if seed["type"] not in ALLOWED_TYPES:
            continue
        if any(_matches(seed, s["title"]) for s in stories):
            out.append(dict(seed))
    return out


def refresh(conn, repo_root, k: int, now: datetime.datetime) -> Dict[str, Any]:
    """Grow the registry by ≤k, rebuild ≤k active entity pages, refresh the
    index. Returns {'kb_writes': {relpath: content}, 'entities': [meta...]}
    where each meta carries the reader-facing markdown for the site copy."""
    registry = load_registry(repo_root)
    new = propose_entities(conn, registry, k, now)
    if new:
        registry = registry + new
        save_registry(repo_root, registry)

    stories = _fetch_stories(
        conn, (now - datetime.timedelta(days=STORIES_SINCE_DAYS)).isoformat())
    active_cut = (now - datetime.timedelta(days=ACTIVE_SINCE_DAYS)).isoformat()

    # entity -> timeline, and whether it's freshly active
    covered: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
    for ent in registry:
        tl = _entity_timeline(ent, stories)
        if tl:
            covered.append((ent, tl))

    def _recent(tl):
        return tl[0]["date"] if tl else ""

    # rebuild priority: newly proposed first, then most-recently-active
    new_slugs = {e["slug"] for e in new}
    covered.sort(key=lambda ct: (ct[0]["slug"] in new_slugs, _recent(ct[1])),
                 reverse=True)
    to_build = [ct for ct in covered
                if ct[0]["slug"] in new_slugs or _recent(ct[1]) >= active_cut[:10]
                ][:k]

    kb_writes: Dict[str, str] = {}
    entities_meta: List[Dict[str, Any]] = []
    for ent, tl in to_build:
        rel, content = _entity_writes(ent, tl)
        kb_writes[rel] = content
        entities_meta.append({
            "slug": ent["slug"],
            "name": ent["name"],
            "type": ent["type"],
            "summary": "%d stor%s tracked; latest %s." % (
                len(tl), "y" if len(tl) == 1 else "ies", tl[0]["date"]),
            "updated": tl[0]["date"],
            "coverage": len(tl),
            "body_md": content,
        })

    # index over ALL covered entities (not just the rebuilt ones)
    index_entries = [{
        "slug": ent["slug"], "name": ent["name"], "type": ent["type"],
        "updated": tl[0]["date"], "coverage": len(tl),
    } for ent, tl in covered]
    if index_entries:
        rel, content = _index_content(index_entries)
        kb_writes[rel] = content

    return {"kb_writes": kb_writes, "entities": entities_meta,
            "index": index_entries}


def has_archive_link(content: str) -> bool:
    """Guard mirror of the publish scrub — for tests/asserts."""
    return bool(_ARCHIVE_RE.search(content))
