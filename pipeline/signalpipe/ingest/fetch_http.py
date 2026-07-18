"""Polite HTTP for Signal.

- Shared httpx client with a descriptive User-Agent and bounded redirects.
- Per-host minimum-interval rate limiting (arXiv 1req/3s+, Reddit ~10 QPM
  unauthenticated, gentle default elsewhere).
- Conditional GET via the fetch_cache table (ETag / Last-Modified) plus a
  body-hash short-circuit so unchanged feeds aren't reparsed (repo convention:
  hash, not mtime).
"""

from __future__ import annotations

import datetime
import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

# Body-size cap + per-request wall-clock deadline: fetch() pulls arbitrary
# URLs harvested from the wild (a "canonical link" can be a multi-GB video),
# and the httpx timeout is per-socket-op — a slow-drip server can keep one
# GET alive indefinitely without these.
MAX_BODY_BYTES = 5 * 1024 * 1024
FETCH_DEADLINE_SEC = 60.0

# Per-host minimum seconds between requests. Anything not listed uses default.
HOST_MIN_INTERVAL = {
    "rss.arxiv.org": 3.5,
    "export.arxiv.org": 3.5,
    "arxiv.org": 3.5,
    "www.reddit.com": 7.0,
    "oauth.reddit.com": 1.0,
    "hn.algolia.com": 0.5,
    "lobste.rs": 2.0,
    "huggingface.co": 1.0,
    "github.com": 2.0,
}


@dataclass
class FetchResult:
    status: int                      # HTTP status; 0 = transport error
    content: Optional[bytes] = None
    unchanged: bool = False          # 304 or identical body hash
    error: Optional[str] = None
    final_url: Optional[str] = None
    blocked_by_robots: bool = False  # skipped: a robots.txt rule disallowed it


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class PoliteClient:
    """One instance per job run. Not safe to share across processes; safe
    across threads within a run (lock-guarded rate limiter)."""

    def __init__(self, cfg, conn: Optional[sqlite3.Connection] = None):
        ing = cfg.ingest
        self.default_interval = float(ing.get("per_host_min_interval_sec", 2.0))
        self.host_intervals = dict(HOST_MIN_INTERVAL)
        self.host_intervals.update(ing.get("host_min_interval", {}))
        self.max_body_bytes = int(ing.get("max_body_bytes", MAX_BODY_BYTES))
        self.fetch_deadline = float(
            ing.get("fetch_deadline_sec", FETCH_DEADLINE_SEC))
        self.conn = conn
        # robots.txt compliance (RFC 9309). Off by default in code so the test
        # suite is undisturbed; the shipped config turns it on. Parsers are
        # cached per netloc with a TTL (RFC 9309 suggests a 24h caching bound).
        self.user_agent = cfg.user_agent
        self.respect_robots = bool(ing.get("respect_robots", False))
        self.robots_ttl = float(ing.get("robots_cache_ttl_sec", 86400.0))
        self._robots: Dict[str, Tuple[Optional[RobotFileParser], float]] = {}
        self._robots_lock = threading.Lock()
        self._last_hit: Dict[str, float] = {}
        self._lock = threading.Lock()
        self.client = httpx.Client(
            headers={"User-Agent": cfg.user_agent},
            timeout=float(ing.get("http_timeout_sec", 20)),
            follow_redirects=True,
            max_redirects=int(ing.get("max_redirects", 5)),
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    # ------------------------------------------------------------------

    def _respect_rate_limit(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        interval = float(self.host_intervals.get(host, self.default_interval))
        with self._lock:
            last = self._last_hit.get(host, 0.0)
            wait = last + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.monotonic()

    # ------------------------------------------------------------------

    def _load_robots(self, scheme: str, netloc: str) -> Optional[RobotFileParser]:
        """Fetch + parse a host's robots.txt through the shared (UA-bearing,
        rate-limited) client. Returns a parser, or None meaning "no applicable
        policy — allow all". Fail-open on 4xx/5xx/transport error: a missing or
        broken robots.txt must not silently halt ingestion (RFC 9309 leaves 5xx
        handling to the crawler; a feed reader errs toward continuing)."""
        robots_url = "%s://%s/robots.txt" % (scheme, netloc)
        try:
            self._respect_rate_limit(robots_url)
            resp = self.client.get(robots_url)
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None
        rp = RobotFileParser()
        try:
            text = resp.text
        except Exception:  # pragma: no cover - defensive decode guard
            return None
        rp.parse(text[: self.max_body_bytes].splitlines())
        return rp

    def _robots_allowed(self, url: str) -> bool:
        """True if robots.txt permits fetching ``url`` for our User-Agent.
        No-op (always True) unless ``respect_robots`` is enabled. Parsers are
        cached per netloc for ``robots_ttl`` seconds."""
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        netloc = (parts.netloc or "").lower()
        if not netloc:
            return True
        scheme = parts.scheme or "https"
        now = time.monotonic()
        with self._robots_lock:
            entry = self._robots.get(netloc)
            fresh = entry is not None and (now - entry[1]) < self.robots_ttl
            rp = entry[0] if entry else None
        if not fresh:
            rp = self._load_robots(scheme, netloc)
            with self._robots_lock:
                self._robots[netloc] = (rp, time.monotonic())
        if rp is None:
            return True
        try:
            return rp.can_fetch(self.user_agent, url)
        except Exception:  # pragma: no cover - robotparser edge cases
            return True

    # ------------------------------------------------------------------

    def _cache_row(self, url: str):
        if self.conn is None:
            return None
        return self.conn.execute(
            "SELECT etag, last_modified, body_sha256 FROM fetch_cache WHERE url=?",
            (url,),
        ).fetchone()

    def _cache_put(
        self,
        url: str,
        status: int,
        etag: Optional[str],
        last_modified: Optional[str],
        body_sha256: Optional[str],
    ) -> None:
        if self.conn is None:
            return
        # autocommit connection: no commit() — see db.py write_tx contract.
        self.conn.execute(
            "INSERT INTO fetch_cache(url, etag, last_modified, body_sha256, "
            "fetched_at, status) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET etag=excluded.etag, "
            "last_modified=excluded.last_modified, "
            "body_sha256=excluded.body_sha256, fetched_at=excluded.fetched_at, "
            "status=excluded.status",
            (url, etag, last_modified, body_sha256, _now_iso(), status),
        )

    # ------------------------------------------------------------------

    def fetch(self, url: str, conditional: bool = True) -> FetchResult:
        """GET with politeness + conditional headers + body-hash dedup.

        The body is STREAMED with a byte cap and a wall-clock deadline:
        oversized Content-Length is rejected before any body read; a body
        that grows past the cap or drips past the deadline is aborted with
        an error result instead of buffering unbounded data in the worker."""
        if not self._robots_allowed(url):
            return FetchResult(
                status=0,
                error="blocked by robots.txt",
                final_url=url,
                blocked_by_robots=True,
            )
        self._respect_rate_limit(url)
        headers = {}
        cached = self._cache_row(url) if conditional else None
        if cached:
            if cached["etag"]:
                headers["If-None-Match"] = cached["etag"]
            if cached["last_modified"]:
                headers["If-Modified-Since"] = cached["last_modified"]
        try:
            with self.client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 304:
                    self._cache_put(
                        url,
                        304,
                        cached["etag"] if cached else None,
                        cached["last_modified"] if cached else None,
                        cached["body_sha256"] if cached else None,
                    )
                    return FetchResult(status=304, unchanged=True,
                                       final_url=str(resp.url))

                if resp.status_code >= 400:
                    # Error body never read/buffered.
                    self._cache_put(
                        url,
                        resp.status_code,
                        resp.headers.get("ETag"),
                        resp.headers.get("Last-Modified"),
                        None,
                    )
                    return FetchResult(
                        status=resp.status_code,
                        error="HTTP %d" % resp.status_code,
                        final_url=str(resp.url),
                    )

                clen = resp.headers.get("Content-Length")
                if clen and clen.isdigit() and int(clen) > self.max_body_bytes:
                    return FetchResult(
                        status=resp.status_code,
                        error="body too large (Content-Length %s > %d)"
                              % (clen, self.max_body_bytes),
                        final_url=str(resp.url),
                    )

                chunks = []
                total = 0
                deadline = time.monotonic() + self.fetch_deadline
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > self.max_body_bytes:
                        return FetchResult(
                            status=resp.status_code,
                            error="body exceeded %d bytes — aborted"
                                  % self.max_body_bytes,
                            final_url=str(resp.url),
                        )
                    if time.monotonic() > deadline:
                        return FetchResult(
                            status=resp.status_code,
                            error="fetch deadline exceeded (%.0fs) — aborted"
                                  % self.fetch_deadline,
                            final_url=str(resp.url),
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
                status_code = resp.status_code
                etag = resp.headers.get("ETag")
                last_modified = resp.headers.get("Last-Modified")
                final_url = str(resp.url)
        except httpx.HTTPError as e:
            return FetchResult(status=0, error="%s: %s" % (type(e).__name__, e))

        sha = hashlib.sha256(body).hexdigest() if body else None
        unchanged = bool(cached and sha and cached["body_sha256"] == sha)
        self._cache_put(url, status_code, etag, last_modified, sha)
        return FetchResult(
            status=status_code,
            content=body,
            unchanged=unchanged,
            final_url=final_url,
        )

    def resolve(self, url: str) -> Optional[str]:
        """Resolve a redirect-wrapper URL to its final destination with one
        bounded request. Returns None on failure. The 405 GET fallback only
        needs the final URL, so the response is streamed and closed without
        ever reading the body (it can be an arbitrary-size download)."""
        self._respect_rate_limit(url)
        try:
            resp = self.client.head(url)
            if resp.status_code == 405:
                with self.client.stream("GET", url) as r2:
                    return str(r2.url)
            return str(resp.url)
        except httpx.HTTPError:
            return None
