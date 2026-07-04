"""URL canonicalization for dedup + display.

Strategy (per doc/signal_research.md Part C): strip ONLY a known-tracker
allowlist of query params, never unknown ones; drop fragments (keep `#!`);
lowercase scheme+host; force https; normalize slashes; sort surviving params.
AMP URLs are reduced to their canonical form where derivable offline.

All functions here are pure (no network). Redirect resolution lives in
ingest.fetch_http.
"""

from __future__ import annotations

import re
from typing import Optional, Set
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Exact-match tracker params (case-insensitive).
TRACKER_PARAMS: Set[str] = {
    # campaign
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_source_platform", "utm_creative_format", "utm_marketing_tactic",
    # ad click ids
    "gclid", "gbraid", "wbraid", "dclid", "fbclid", "msclkid", "twclid",
    "ttclid", "li_fat_id", "yclid", "igshid",
    # analytics / email
    "_ga", "_gl", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "_openstat",
    "mkt_tok", "ref", "ref_src", "source", "srsltid",
    # misc common
    "cmpid", "camp", "smid", "sref",
}
TRACKER_PREFIXES = ("utm_", "pk_")

# Host-specific strippable params (avoid breaking content keys elsewhere).
HOST_SPECIFIC = {
    "youtube.com": {"si", "feature"},
    "youtu.be": {"si"},
    "open.spotify.com": {"si"},
    "twitter.com": {"s", "t"},
    "x.com": {"s", "t"},
}

# Aggregator/discussion hosts: links here are commentary, not articles.
AGGREGATOR_HOSTS = {
    "news.ycombinator.com",
    "lobste.rs",
    "reddit.com",
    "old.reddit.com",
    "news.google.com",
    "techmeme.com",
}

_AMP_CDN_RE = re.compile(r"^https?://[^/]*cdn\.ampproject\.org/[cv]/(?:s/)?(.+)$", re.I)


def _host_base(host: str) -> str:
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def registered_domain(url_or_host: str) -> str:
    """Cheap registered-domain heuristic (last two labels). Good enough for
    same-domain gating in dedup; co.uk-style suffixes degrade gracefully to
    stricter (cross-domain) thresholds."""
    host = url_or_host
    if "://" in url_or_host:
        host = urlsplit(url_or_host).hostname or ""
    host = _host_base(host or "")
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    return ".".join(parts[-2:])


def is_aggregator(url: str) -> bool:
    try:
        host = _host_base(urlsplit(url).hostname or "")
    except ValueError:
        return False
    return host in AGGREGATOR_HOSTS or host.endswith(".reddit.com")


def _strip_amp(url: str) -> str:
    m = _AMP_CDN_RE.match(url)
    if m:
        rest = m.group(1)
        if not rest.startswith(("http://", "https://")):
            rest = "https://" + rest
        url = rest
    return url


def _filter_params(host: str, query: str) -> str:
    if not query:
        return ""
    host_extra = HOST_SPECIFIC.get(_host_base(host), set())
    kept = []
    for k, v in parse_qsl(query, keep_blank_values=True):
        kl = k.lower()
        if kl in TRACKER_PARAMS or kl in host_extra:
            continue
        if any(kl.startswith(p) for p in TRACKER_PREFIXES):
            continue
        if kl in ("amp", "outputtype") and v.lower() in ("1", "true", "amp"):
            continue
        kept.append((k, v))
    kept.sort()
    return urlencode(kept)


def canonicalize(url: Optional[str]) -> Optional[str]:
    """Normalize a URL into its dedup/display canonical form.

    Returns None for non-http(s) or unparseable input.
    """
    if not url:
        return None
    url = url.strip()
    url = _strip_amp(url)
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host
    if port and port not in (80, 443):
        netloc = "%s:%d" % (host, port)

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    # AMP path suffixes
    if path.endswith("/amp/"):
        path = path[:-4]
    elif path.endswith("/amp"):
        path = path[:-3]
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if not path:
        path = "/"

    query = _filter_params(host, parts.query)

    fragment = ""
    if parts.fragment.startswith("!"):
        fragment = parts.fragment

    return urlunsplit(("https", netloc, path, query, fragment))


def dedup_key(url: Optional[str]) -> Optional[str]:
    """Identity key for clustering: the canonical URL."""
    return canonicalize(url)
