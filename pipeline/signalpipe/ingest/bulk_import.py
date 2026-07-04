"""Bulk source expansion: harvest candidate feeds from public curated lists
(OPML files, markdown link lists, inline candidate arrays), probe-verify them
in waves through the existing registry harness, auto-tier the survivors, and
merge them into sources.json + the DB.

Manifest lives at config/bulk_sources.json:

  {
    "lists": [
      {"name": "engineering-blogs", "url": "https://...opml",
       "format": "opml" | "markdown",
       "category": "expert_blogs", "topics": ["devtools"],
       "tier": 3, "cadence_min": 720, "paywalled": false,
       "max_candidates": 800},
      ...
    ],
    "inline": [
      {"name": "...", "homepage": "https://...", "feed_url": "https://...",
       "category": "physics", "topics": ["science"], "tier": 2,
       "paywalled": false, "why": "..."},
      ...
    ]
  }

Quality gates (the user's call: quality over quantity):
  - probe must find a live, parseable feed (registry.probe_candidates)
  - dormant feeds dropped (newest entry older than STALE_DAYS)
  - feeds with zero parseable entry dates dropped
  - per-host dedupe against the registry and within the run
  - tier 1 is NEVER auto-assigned

Resume: per-entry checkpoints in <state>/bulk/<entry>.json record processed
hosts and counters, so an interrupted run continues where it left off.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
import sys
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .. import db as db_mod
from . import registry
from .fetch_http import PoliteClient

STALE_DAYS = 365          # newest entry older than this -> drop (dormant)
TIER2_FRESH_DAYS = 30     # tier-2 requires activity within this window
TIER2_MIN_ENTRIES = 10
TIER2_CATEGORIES = {
    "tech_news", "research", "physics", "science", "ai_companies", "news",
}
TIER_CADENCE = {1: 120, 2: 240, 3: 720}
TIER_REPUTATION = {1: 1.2, 2: 1.0, 3: 0.8}

# Hosts that legitimately serve MANY distinct feeds — never host-deduped.
SHARED_HOSTS = {
    "feeds.bbci.co.uk", "www.theguardian.com", "rss.nytimes.com",
    "feeds.npr.org", "www.nature.com", "www.sciencedaily.com",
    "www.eurekalert.org", "rss.arxiv.org", "export.arxiv.org",
    "phys.org", "feeds.aps.org", "feeds.feedburner.com",
    "news.google.com", "www.reddit.com", "medium.com",
    "feeds.bloomberg.com", "rss.sciam.com", "journals.plos.org",
}

# Markdown-list links we never probe (repo/self links, badges, images).
SKIP_URL_RE = re.compile(
    r"(github\.com/|shields\.io|badge|\.png$|\.svg$|\.jpe?g$|\.gif$"
    r"|twitter\.com|x\.com/|t\.me/|discord\.|youtube\.com/watch)", re.I
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# Probe-reject reasons that are definitive content verdicts: safe to
# checkpoint (never retried). Everything else — transport errors (timeout,
# DNS, dropped Wi-Fi: "ConnectTimeout: ...", "fetch failed") and retryable
# HTTP statuses — stays OUT of hosts_done so the next run retries it.
_DEFINITIVE_PREFIXES = (
    "no valid feed", "duplicate", "domain already registered", "no url",
)


def _is_transient_reason(reason) -> bool:
    """True when a probe rejection was a transient transport/server failure
    rather than a definitive verdict about the candidate."""
    r = (reason or "").strip().lower()
    if not r:
        return True
    if r.startswith(_DEFINITIVE_PREFIXES):
        return False
    m = re.match(r"http (\d{3})", r)
    if m:
        code = int(m.group(1))
        return code in (408, 429) or code >= 500  # 404/403/410 are definitive
    return True  # httpx exception text / "fetch failed" — transport error


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _bulk_dir(cfg) -> pathlib.Path:
    d = cfg.db_path.parent / "bulk"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _checkpoint_path(cfg, entry_name: str) -> pathlib.Path:
    safe = re.sub(r"[^a-z0-9-]+", "-", entry_name.lower()).strip("-")
    return _bulk_dir(cfg) / ("%s.json" % safe)


def _load_checkpoint(cfg, entry_name: str) -> Dict:
    p = _checkpoint_path(cfg, entry_name)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (ValueError, OSError):
            pass
    return {"hosts_done": [], "verified": 0, "rejected": 0, "imported": 0,
            "complete": False}


def _save_checkpoint(cfg, entry_name: str, state: Dict) -> None:
    _checkpoint_path(cfg, entry_name).write_text(json.dumps(state, indent=1))


def _existing_hosts(cfg) -> set:
    """Hostnames already in the registry (feed URL + homepage)."""
    hosts = set()
    for s in registry.load_specs(cfg):
        for u in (s.url, s.homepage):
            h = _host(u or "")
            if h and h not in SHARED_HOSTS:
                hosts.add(h)
    return hosts


# ---------------------------------------------------------------------------
# candidate harvesting
# ---------------------------------------------------------------------------

def _candidates_from_opml(content: bytes, entry: Dict) -> List[Dict]:
    out = []
    try:
        outlines = registry.parse_opml(content)
    except Exception as e:  # noqa: BLE001 — malformed remote OPML
        print("  opml parse error (%s): %s" % (entry.get("name"), e),
              file=sys.stderr)
        return out
    for title, xml_url, html_url in outlines:
        out.append({
            "name": title,
            "feed_url": xml_url,
            "homepage": html_url,
            "category": entry.get("category", "uncategorized"),
            "topics": entry.get("topics", []),
            "tier": int(entry.get("tier", 3)),
            "cadence_min": int(entry.get("cadence_min", 720)),
            "paywalled": bool(entry.get("paywalled", False)),
            "why": "bulk: %s" % entry.get("name", "list"),
        })
    return out


def _candidates_from_markdown(content: bytes, entry: Dict) -> List[Dict]:
    text = content.decode("utf-8", "ignore")
    out = []
    for name, url in registry.parse_markdown_list(text):
        if SKIP_URL_RE.search(url):
            continue
        cand = {
            "name": name,
            "category": entry.get("category", "uncategorized"),
            "topics": entry.get("topics", []),
            "tier": int(entry.get("tier", 3)),
            "cadence_min": int(entry.get("cadence_min", 720)),
            "paywalled": bool(entry.get("paywalled", False)),
            "why": "bulk: %s" % entry.get("name", "list"),
        }
        # Heuristic: URLs that already look like feeds go in as feed_url so
        # the prober validates directly; everything else is a homepage.
        if re.search(r"(feed|rss|atom|\.xml)(/|$)", url, re.I):
            cand["feed_url"] = url
        else:
            cand["homepage"] = url
        out.append(cand)
    return out


# ---------------------------------------------------------------------------
# quality gates
# ---------------------------------------------------------------------------

def _fresh_enough(latest_iso: Optional[str], max_age_days: int) -> bool:
    if not latest_iso:
        return False
    try:
        latest = datetime.datetime.fromisoformat(latest_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=datetime.timezone.utc)
    return (_now() - latest).days <= max_age_days


def auto_tier(verified: Dict) -> int:
    """Tier from probe evidence. Tier 1 is never auto-assigned."""
    if (
        verified.get("category") in TIER2_CATEGORIES
        and int(verified.get("entries") or 0) >= TIER2_MIN_ENTRIES
        and _fresh_enough(verified.get("latest_entry"), TIER2_FRESH_DAYS)
    ):
        return 2
    return 3


def _apply_quality_gates(verified: List[Dict]) -> Tuple[List[Dict], int]:
    """Drop dormant/dateless feeds; auto-tier the rest. -> (kept, dropped)."""
    kept = []
    dropped = 0
    for v in verified:
        if not _fresh_enough(v.get("latest_entry"), STALE_DAYS):
            dropped += 1
            continue
        tier = min(auto_tier(v), int(v.get("tier", 3)) if v.get("tier") else 3)
        # A list may declare tier 2 for its members; evidence can only demote
        # to 3, never promote to 1.
        tier = max(tier, 2) if tier < 2 else tier
        v["tier"] = tier
        v["cadence_min"] = TIER_CADENCE[tier]
        v["reputation"] = TIER_REPUTATION[tier]
        kept.append(v)
    return kept, dropped


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------

def _chunks(seq: List, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def run(cfg, manifest_path: Optional[pathlib.Path] = None,
        only_entry: Optional[str] = None, wave_size: int = 200,
        max_workers: int = 16, limit: Optional[int] = None,
        no_resume: bool = False) -> int:
    manifest_path = manifest_path or (cfg.path.parent / "bulk_sources.json")
    if not manifest_path.exists():
        print("manifest not found: %s" % manifest_path, file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text())

    entries = list(manifest.get("lists", []))
    inline = list(manifest.get("inline", []))
    if inline:
        entries.append({"name": "inline", "format": "inline",
                        "candidates": inline})
    if only_entry:
        entries = [e for e in entries if e.get("name") == only_entry]
        if not entries:
            print("no manifest entry named %r" % only_entry, file=sys.stderr)
            return 2

    totals = {"candidates": 0, "verified": 0, "imported": 0, "rejected": 0,
              "dropped_quality": 0, "skipped_dupe": 0}

    for entry in entries:
        name = entry.get("name", "?")
        state = ({"hosts_done": [], "verified": 0, "rejected": 0,
                  "imported": 0, "complete": False}
                 if no_resume else _load_checkpoint(cfg, name))
        if state.get("complete"):
            print("[%s] checkpoint says complete — skipping (use --no-resume "
                  "to redo)" % name)
            continue
        hosts_done = set(state.get("hosts_done", []))

        # -- harvest ---------------------------------------------------------
        if entry.get("format") == "inline":
            cands = list(entry.get("candidates", []))
        else:
            with PoliteClient(cfg) as client:
                res = client.fetch(entry["url"], conditional=False)
            if res.status != 200 or not res.content:
                print("[%s] list fetch failed (%s) — skipping this entry"
                      % (name, res.error or res.status), file=sys.stderr)
                continue
            if entry.get("format") == "markdown":
                cands = _candidates_from_markdown(res.content, entry)
            else:
                cands = _candidates_from_opml(res.content, entry)

        # -- dedupe / resume filtering ----------------------------------------
        existing_hosts = _existing_hosts(cfg)
        seen_run_hosts = set()
        filtered = []
        for c in cands:
            url = c.get("feed_url") or c.get("homepage") or ""
            h = _host(url)
            if not h:
                continue
            if h in hosts_done:
                continue
            if h in SHARED_HOSTS:
                filtered.append(c)
                continue
            if h in existing_hosts or h in seen_run_hosts:
                totals["skipped_dupe"] += 1
                continue
            seen_run_hosts.add(h)
            filtered.append(c)
        max_c = entry.get("max_candidates")
        if max_c:
            filtered = filtered[: int(max_c)]
        if limit:
            filtered = filtered[: int(limit)]
        totals["candidates"] += len(filtered)
        print("[%s] %d candidates after dedupe/resume (raw %d)"
              % (name, len(filtered), len(cands)))

        # -- probe in waves ----------------------------------------------------
        entry_transient = 0
        for wave_i, wave in enumerate(_chunks(filtered, wave_size), 1):
            verified, rejected = registry.probe_candidates(
                cfg, wave, max_workers=max_workers
            )
            kept, dropped = _apply_quality_gates(verified)
            added = registry.merge_into_registry(cfg, kept) if kept else 0
            if added:
                registry.seed(cfg)
            # Checkpoint only definitive outcomes (verified, no-feed, 404,
            # duplicate). Transport-failed candidates (timeout, DNS, sleep
            # mid-wave) stay out of hosts_done so the next run retries them
            # instead of consuming them forever.
            transient_hosts = set()
            for r in rejected:
                if _is_transient_reason(r.get("reason")):
                    h = _host(r.get("url") or "")
                    if h:
                        transient_hosts.add(h)
            wave_transient = 0
            for c in wave:
                h = _host(c.get("feed_url") or c.get("homepage") or "")
                hh = _host(c.get("homepage") or "")
                if not h:
                    continue
                if h in transient_hosts or (hh and hh in transient_hosts):
                    wave_transient += 1
                    continue
                hosts_done.add(h)
            entry_transient += wave_transient
            state.update({
                "hosts_done": sorted(hosts_done),
                "verified": state.get("verified", 0) + len(kept),
                "rejected": state.get("rejected", 0) + len(rejected) + dropped,
                "imported": state.get("imported", 0) + added,
            })
            _save_checkpoint(cfg, name, state)
            totals["verified"] += len(kept)
            totals["imported"] += added
            totals["rejected"] += len(rejected)
            totals["dropped_quality"] += dropped
            print("[%s] wave %d: %d probed -> %d verified (%d quality-dropped)"
                  " -> %d imported%s"
                  % (name, wave_i, len(wave), len(kept), dropped, added,
                     ", %d transient-failed (will retry)" % wave_transient
                     if wave_transient else ""))

        # An entry with transient failures is NOT complete: leaving the flag
        # off lets the next run retry just the failed hosts via the resume
        # filter, instead of forcing a full --no-resume redo.
        if entry_transient == 0:
            state["complete"] = True
        else:
            print("[%s] %d candidate(s) failed transiently — entry left "
                  "incomplete for retry" % (name, entry_transient))
        _save_checkpoint(cfg, name, state)

    # -- summary + health -------------------------------------------------------
    print("bulk import: %(candidates)d candidates, %(verified)d verified, "
          "%(imported)d imported, %(rejected)d rejected, "
          "%(dropped_quality)d dropped (stale/dateless), "
          "%(skipped_dupe)d duplicate hosts skipped" % totals)
    if cfg.db_path.exists():
        conn = db_mod.connect_rw(cfg.db_path)
        try:
            db_mod.log_health(
                conn, "sources", "info",
                "bulk import: +%d imported (%d verified, %d rejected)"
                % (totals["imported"], totals["verified"], totals["rejected"]),
                stats=json.dumps(totals),
            )
        finally:
            conn.close()
    return 0
