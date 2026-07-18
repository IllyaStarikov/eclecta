"""The single LLM entry point. Stages ask for a tier; the adapter resolves
tier -> (backend, model), enforces the spend cap, dispatches, and records
the cost. Callers never know which backend ran."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional, Tuple

from . import (LLMError, UsageLimitExhausted, backend_api, backend_cli,
               backend_local, quota, spend)


def complete_with_cost(
    tier: str,
    system: str,
    prompt: str,
    schema: Dict[str, Any],
    *,
    cfg,
    conn: sqlite3.Connection,
    effort: Optional[str] = None,
    cap_kind: str = "daily",
    model_override: Optional[str] = None,
) -> Tuple[Dict[str, Any], float]:
    """(schema-valid dict, cost_usd) or raises SpendCapExceeded / LLMError.

    model_override forces the subscription cloud path on a specific model id
    (used by the one-time all-Opus backfill); it bypasses the local arena."""
    backend = cfg.backend_for(tier)

    # LOCAL (Ollama): free, never gated by the spend cap. Runs the tier's local
    # model(s) — a single model for per-item callers (triage); the judge ARENA
    # is batched model-outer by curate.py, not here. On Ollama failure, degrade
    # to the configured cloud fallback so the worker keeps running.
    if backend == "local" and model_override is None:
        models = cfg.local_models_for(tier)
        try:
            outs = [
                backend_local.run(m, system, prompt, schema, cfg)[0]
                for m in models
            ]
            obj = outs[0] if len(outs) == 1 else backend_local.consensus(outs, schema)
            return obj, 0.0
        except LLMError:
            fb = (cfg.backend.get("local") or {}).get("fallback")
            if not fb or fb == "local":
                raise
            backend = fb  # degrade to a cloud backend below

    # Usage-limit hold: while armed, fail fast without spawning the CLI so a
    # curate/digest run stops on its first call instead of burning a spawn per
    # item. backend_cli arms the hold on a limit hit and clears it on success;
    # the worker's probe job re-checks once retry_at passes.
    if backend == "subscription":
        held, why = quota.status()
        if held:
            raise UsageLimitExhausted(why, cost_usd=0.0)

    spend.assert_under_cap(conn, cfg, kind=cap_kind)
    if model_override is not None:
        backend = "subscription"
        model = model_override
    else:
        model = cfg.model_for(tier, backend)

    try:
        if backend == "subscription":
            obj, cost = backend_cli.run(model, system, prompt, schema, cfg, effort)
        else:
            obj, cost = backend_api.run(
                model, system, prompt, schema, cfg, effort)
    except LLMError as e:
        # Failed calls still cost money — record them, or the cap
        # under-counts exactly when calls are being wasted. cost_usd=None
        # means "unknown, a call plausibly happened": charge a conservative
        # estimate. An explicit 0.0 (e.g. the CLI binary missing) is free.
        failed_cost = getattr(e, "cost_usd", None)
        if failed_cost is None:
            failed_cost = float(
                cfg.spend.get("failed_call_estimate_usd", 0.02))
        if failed_cost > 0:
            spend.record(conn, backend, failed_cost, kind=cap_kind)
        raise

    spend.record(conn, backend, cost, kind=cap_kind)
    return obj, cost


def complete(
    tier: str,
    system: str,
    prompt: str,
    schema: Dict[str, Any],
    *,
    cfg,
    conn: sqlite3.Connection,
    effort: Optional[str] = None,
    cap_kind: str = "daily",
    model_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Schema-valid dict or raises SpendCapExceeded / LLMError."""
    obj, _ = complete_with_cost(
        tier, system, prompt, schema,
        cfg=cfg, conn=conn, effort=effort, cap_kind=cap_kind,
        model_override=model_override,
    )
    return obj


def probe_auth(cfg) -> Tuple[bool, str]:
    """Cheap preflight used by `status` and the dashboard health panel:
    confirms the active backend can authenticate at all. Costs ~nothing
    (a one-word prompt) but DOES spend; callers invoke it sparingly."""
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    try:
        if cfg.backend["selector"] == "subscription":
            obj, _ = backend_cli.run(
                cfg.model_for("triage"),
                "Reply with JSON only.",
                'Return {"ok": true}',
                schema,
                cfg,
            )
        else:
            obj, _ = backend_api.run(
                cfg.model_for("triage"),
                "Reply with JSON only.",
                'Return {"ok": true}',
                schema,
                cfg,
            )
        return bool(obj.get("ok")), "auth ok"
    except LLMError as e:
        return False, str(e)
