"""Lightweight row models for Signal. No ORM — sqlite3.Row does most work;
these dataclasses exist where construction/validation helps (registry import,
probe results)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

VALID_ACCESS_TYPES = ("rss", "atom", "json", "api", "scrape")
VALID_CHANNELS = (
    "ai",
    "ml-research",
    "devtools",
    "security",
    "hardware",
    "startups",
    "science",
    "news",
)


@dataclass
class SourceSpec:
    """A source as declared in sources.json / sources.opml before DB import."""

    slug: str
    name: str
    type: str  # rss|atom|json|api|scrape
    url: str
    homepage: Optional[str] = None
    category: str = "uncategorized"
    topics: List[str] = field(default_factory=list)
    reputation: float = 1.0
    tier: int = 2
    cadence_min: int = 60
    paywalled: bool = False
    enabled: bool = True
    mode: Optional[str] = None  # per-source fetcher mode (e.g. reddit auth mode)
    why: Optional[str] = None
    api_notes: Optional[str] = None

    def validate(self) -> Optional[str]:
        """Return an error string, or None if valid."""
        if not self.slug or not self.name or not self.url:
            return "missing slug/name/url"
        if self.type not in VALID_ACCESS_TYPES:
            return "bad type %r" % self.type
        if self.tier not in (1, 2, 3):
            return "bad tier %r" % self.tier
        bad = [t for t in self.topics if t not in VALID_CHANNELS]
        if bad:
            return "unknown topics %r" % bad
        return None


@dataclass
class ProbeResult:
    """Outcome of probing a candidate homepage/feed URL."""

    candidate_url: str
    feed_url: Optional[str] = None
    ok: bool = False
    kind: Optional[str] = None  # rss|atom
    title: Optional[str] = None
    latest_entry: Optional[str] = None  # ISO date of newest entry, if any
    entries: int = 0
    error: Optional[str] = None
