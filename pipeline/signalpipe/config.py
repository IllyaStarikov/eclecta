"""Configuration for Signal.

Loads config/signal.json (repo convention: {tracking, last_run} blocks +
domain config), validates it, exposes typed accessors, and guards against
the SQLite DB living inside iCloud Drive.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
from typing import Any, Dict, List, Optional

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "signal.json"
STATE_DIR = pathlib.Path(os.path.expanduser("~/.local/state/signal"))

VALID_BACKENDS = ("subscription", "api")
# 'judge' + 'write' added for the local-judge / Claude-write split; 'deep' kept
# (harmless) for back-compat. 'local' is a routing override, NOT a per-tier
# backend, so it is deliberately absent from VALID_BACKENDS.
VALID_TIERS = ("triage", "deep", "judge", "write", "digest")

# Per-kind digest defaults; config "digests" entries merge over these.
DIGEST_DEFAULTS = {
    "daily": {"cron": "0 7 * * mon-fri", "min_relevance": 6, "max_items": 25},
    "weekly": {"cron": "30 7 * * fri", "min_relevance": 7, "max_items": 40},
    "monthly": {"cron": "0 8 1-3 * *", "min_relevance": 7, "max_items": 30},
    "quarterly": {
        "cron": "15 8 1-3 1,4,7,10 *", "min_relevance": 7, "max_items": 30,
    },
    "yearly": {"cron": "30 8 1-3 1 *", "min_relevance": 7, "max_items": 30},
}


class ConfigError(Exception):
    pass


def sha256_file(path: pathlib.Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


class Config:
    """Thin wrapper over the signal.json dict with validated accessors."""

    def __init__(self, data: Dict[str, Any], path: pathlib.Path):
        self.data = data
        self.path = path
        self._validate()

    # ----- core paths ------------------------------------------------------

    @property
    def db_path(self) -> pathlib.Path:
        raw = self.data.get("db_path", "~/.local/state/signal/signal.db")
        p = pathlib.Path(os.path.expanduser(raw))
        if "Mobile Documents" in str(p):
            raise ConfigError(
                "db_path %s is inside iCloud Drive; SQLite WAL there risks "
                "silent corruption. Use ~/.local/state/signal/signal.db" % p
            )
        return p

    @property
    def repo_root(self) -> pathlib.Path:
        return REPO_ROOT

    @property
    def blog_repo(self) -> pathlib.Path:
        """The REAL website repo (for staging copies + the publish scripts).
        When running from the TCC-safe runtime copy under ~/.local/state,
        repo_root points at the copy — blog_repo still points home."""
        raw = self.data.get("blog_repo")
        return pathlib.Path(os.path.expanduser(raw)) if raw else REPO_ROOT

    @property
    def staging_dir(self) -> pathlib.Path:
        """Where scheduler-run jobs stage artifacts. NEVER inside iCloud —
        launchd-spawned processes may be TCC-blocked from the repo tree."""
        p = STATE_DIR / "staging"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def repo_path(self, rel: str) -> pathlib.Path:
        """Resolve a repo-relative path from config (e.g. sources files)."""
        p = pathlib.Path(os.path.expanduser(rel))
        if p.is_absolute():
            return p
        return REPO_ROOT / p

    @property
    def sources_json(self) -> pathlib.Path:
        return self.repo_path(
            self.data.get("sources", {}).get("registry", "signalpipe/sources.json")
        )

    @property
    def sources_opml(self) -> pathlib.Path:
        return self.repo_path(
            self.data.get("sources", {}).get("opml", "signalpipe/sources.opml")
        )

    # ----- sections --------------------------------------------------------

    @property
    def server(self) -> Dict[str, Any]:
        return self.data["server"]

    @property
    def backend(self) -> Dict[str, Any]:
        return self.data["backend"]

    @property
    def tiers(self) -> Dict[str, Dict[str, str]]:
        return self.data["tiers"]

    @property
    def cadences(self) -> Dict[str, Any]:
        return self.data["cadences"]

    @property
    def funnel(self) -> Dict[str, Any]:
        return self.data["funnel"]

    @property
    def spend(self) -> Dict[str, Any]:
        return self.data["spend"]

    @property
    def channels(self) -> List[str]:
        return list(self.data["channels"])

    @property
    def score_weights(self) -> Dict[str, Any]:
        return self.data["score_weights"]

    @property
    def paywall(self) -> Dict[str, Any]:
        return self.data["paywall"]

    @property
    def dedup(self) -> Dict[str, Any]:
        return self.data["dedup"]

    @property
    def ingest(self) -> Dict[str, Any]:
        return self.data.get("ingest", {})

    @property
    def site(self) -> Dict[str, Any]:
        """The published-site block (repo, branch, push, picks knobs).
        Validated at publish time, not load time — the pipeline must keep
        running when the site repo is absent."""
        return self.data.get("site", {})

    @property
    def digests(self) -> Dict[str, Dict[str, Any]]:
        """Per-kind digest config, config values merged over DIGEST_DEFAULTS."""
        merged: Dict[str, Dict[str, Any]] = {}
        configured = self.data.get("digests", {})
        for kind, defaults in DIGEST_DEFAULTS.items():
            entry = dict(defaults)
            entry.update(configured.get(kind, {}))
            merged[kind] = entry
        return merged

    @property
    def user_agent(self) -> str:
        return self.data.get(
            "user_agent", "signalpipe/0.1 (+https://eclecta.co; feed curator)"
        )

    def model_for(self, tier: str, backend: Optional[str] = None) -> str:
        """Resolve tier -> cloud model id for a backend (default: the active
        selector). 'local' tiers carry no per-tier cloud id, so they resolve to
        the subscription model — used as the cloud fallback when Ollama is down."""
        if tier not in VALID_TIERS:
            raise ConfigError("unknown tier %r" % tier)
        b = backend or self.backend["selector"]
        if b == "local":
            b = "subscription"
        return self.tiers[tier][b]

    def backend_for(self, tier: str) -> str:
        """Resolve tier -> backend ('local' | 'subscription' | 'api'), honoring
        per-tier overrides (backend.tier_overrides), else the global selector."""
        if tier not in VALID_TIERS:
            raise ConfigError("unknown tier %r" % tier)
        overrides = self.backend.get("tier_overrides", {})
        return overrides.get(tier, self.backend["selector"])

    def local_models_for(self, tier: str) -> List[str]:
        """The arena model list for a local tier: a per-tier override
        (tiers[tier]['local']) if present, else backend.local.models."""
        per_tier = self.tiers.get(tier, {}).get("local")
        models = per_tier if per_tier else self.backend.get("local", {}).get("models", [])
        if isinstance(models, str):
            models = [models]
        return list(models)

    # ----- validation ------------------------------------------------------

    def _validate(self) -> None:
        required = (
            "server",
            "backend",
            "tiers",
            "cadences",
            "funnel",
            "spend",
            "channels",
            "score_weights",
            "paywall",
            "dedup",
        )
        missing = [k for k in required if k not in self.data]
        if missing:
            raise ConfigError("config missing keys: %s" % ", ".join(missing))
        sel = self.data["backend"].get("selector")
        if sel not in VALID_BACKENDS:
            raise ConfigError("backend.selector must be one of %s" % (VALID_BACKENDS,))
        for tier in VALID_TIERS:
            entry = self.data["tiers"].get(tier)
            if not entry or not all(b in entry for b in VALID_BACKENDS):
                raise ConfigError(
                    "tiers.%s must map both backends to a model id" % tier
                )
        if not self.data["channels"]:
            raise ConfigError("channels must be non-empty")
        # Trigger the iCloud guard early so `status` fails loudly, not the worker.
        _ = self.db_path

    # ----- tracking + last_run (repo config convention) ---------------------

    def tracked_changes(self) -> List[str]:
        """Tracked input files whose content hash changed since last record."""
        changed = []
        for rel, old in self.data.get("tracking", {}).items():
            new = sha256_file(self.repo_path(rel))
            if new != old:
                changed.append(rel)
        return changed

    def update_tracking(self, rels: Optional[List[str]] = None) -> None:
        tracking = self.data.setdefault("tracking", {})
        for rel in rels if rels is not None else list(tracking.keys()):
            tracking[rel] = sha256_file(self.repo_path(rel))
        self.save()

    def write_last_run(self, job: str, stats: Dict[str, Any]) -> None:
        self.data["last_run"] = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "job": job,
            "stats": stats,
        }
        self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2) + "\n")


def load(path: Optional[pathlib.Path] = None) -> Config:
    cfg_path = path or DEFAULT_CONFIG_PATH
    try:
        data = json.loads(cfg_path.read_text())
    except OSError as e:
        raise ConfigError("cannot read config %s: %s" % (cfg_path, e))
    except ValueError as e:
        raise ConfigError("config %s is not valid JSON: %s" % (cfg_path, e))
    return Config(data, cfg_path)
