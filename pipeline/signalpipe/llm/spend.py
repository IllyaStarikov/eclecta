"""Daily spend ledger + hard-cap circuit breaker.

`claude -p --output-format json` reports total_cost_usd per call (official,
includes cache/thinking) — that is the cli_usd column. API costs are computed
from usage × pricing table in backend_api. The cap check runs BEFORE every
call: when the cap is hit, curation stops with a health warning instead of
silently draining the Agent SDK credit.
"""

from __future__ import annotations

import datetime
import sqlite3

from . import SpendCapExceeded


def _today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def today_total(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT cli_usd + api_usd AS total FROM spend WHERE day=?", (_today(),)
    ).fetchone()
    return float(row["total"]) if row and row["total"] else 0.0


def today_digest(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT digest_usd FROM spend WHERE day=?", (_today(),)
    ).fetchone()
    return float(row["digest_usd"]) if row and row["digest_usd"] else 0.0


def assert_under_cap(conn: sqlite3.Connection, cfg, kind: str = "daily") -> None:
    # Decoupled caps so a heavy curation day can't starve the daily editorial
    # product: the digest is gated ONLY by its own sub-cap (it is bounded —
    # a handful of Opus calls/day), while curation and everything else are
    # gated by the global daily cap. Checking the whole day's total against
    # daily_cap for the digest would let a busy curation morning silently
    # block every digest.
    if kind == "digest":
        dcap = float(cfg.spend.get("digest_cap_usd", 5.0))
        dspent = today_digest(conn)
        if dspent >= dcap:
            raise SpendCapExceeded(
                "digest spend cap hit: $%.4f >= $%.2f (%s)"
                % (dspent, dcap, _today())
            )
        return
    cap = float(cfg.spend.get("daily_cap_usd", 5.0))
    spent = today_total(conn)
    if spent >= cap:
        raise SpendCapExceeded(
            "daily spend cap hit: $%.4f >= $%.2f (%s)" % (spent, cap, _today())
        )


def record(conn: sqlite3.Connection, backend: str, cost_usd: float,
           kind: str = "daily") -> None:
    col = "cli_usd" if backend == "subscription" else "api_usd"
    digest_add = float(cost_usd or 0.0) if kind == "digest" else 0.0
    # No commit() here: connections run isolation_level=None (autocommit), and
    # an explicit commit would prematurely end a caller's write_tx if one were
    # ever open on this connection. The upsert is durable on its own.
    conn.execute(
        "INSERT INTO spend(day, %s, digest_usd, calls) VALUES(?, ?, ?, 1) "
        "ON CONFLICT(day) DO UPDATE SET %s = %s + excluded.%s, "
        "digest_usd = digest_usd + excluded.digest_usd, "
        "calls = calls + 1" % (col, col, col, col),
        (_today(), float(cost_usd or 0.0), digest_add),
    )
