"""Subscription usage-limit hold.

When `claude -p` reports the Max-plan usage limit, backend_cli arms a hold
file carrying retry_at (the CLI's reset hint when parseable, else now +
backend.quota_recheck_min). While the hold is active the adapter fast-fails
subscription calls without spawning the CLI, and the worker defers
curate/editions the way it defers for downtime. A worker probe job re-checks
after retry_at passes and pulls the stalled jobs forward the moment usage is
back; any successful subscription call also clears the hold.

Items are never marked 'failed' for a quota hold — the whole run defers, so
nothing pays the finalists retry penalty for what is just a waiting game.
"""

from __future__ import annotations

import datetime
import json
import time
from typing import Optional, Tuple

from ..config import STATE_DIR

HOLD_PATH = STATE_DIR / "quota_hold.json"


def set_hold(cfg, reason: str, retry_at: Optional[float] = None) -> float:
    """Arm (or re-arm) the hold; returns the effective retry_at epoch."""
    if retry_at is None or retry_at <= time.time():
        recheck_min = float(cfg.backend.get("quota_recheck_min", 30))
        retry_at = time.time() + recheck_min * 60
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        HOLD_PATH.write_text(json.dumps({
            "retry_at": retry_at,
            "reason": str(reason)[:300],
            "set_at": time.time(),
        }))
    except OSError:
        pass  # a hold-file hiccup must never mask the real LLM error
    return retry_at


def clear() -> None:
    try:
        HOLD_PATH.unlink()
    except OSError:
        pass


def exists() -> bool:
    return HOLD_PATH.exists()


def status() -> Tuple[bool, str]:
    """(hold_active, human why). A missing/corrupt/expired file is inactive.

    An expired file is deliberately left in place: it tells the worker's
    probe job 'we were limited — verify usage is back', and the next real
    call clears it on success or re-arms it on another limit hit.
    """
    try:
        data = json.loads(HOLD_PATH.read_text())
        retry_at = float(data["retry_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False, ""
    if time.time() >= retry_at:
        return False, ""
    until = datetime.datetime.fromtimestamp(retry_at).strftime("%H:%M")
    return True, "subscription usage limit — retrying after %s (%s)" % (
        until, data.get("reason", "")[:120])
