"""Tests for signalpipe.llm.adapter — the single LLM entry point.

The adapter resolves tier -> (backend, model), gates the usage-limit hold and
the spend cap, dispatches to the local / subscription / api backends, records
cost (including failed-call estimates), and exposes complete / complete_with_cost
/ probe_auth.

Every I/O leaf is faked. The adapter holds its OWN module references
(``from . import backend_api, backend_cli, backend_local, quota, spend``), so
the tests monkeypatch the names bound ON the adapter's imported modules — never
a real network / subprocess / Ollama call is made.
"""

from __future__ import annotations

import types
from typing import Any, Optional

import pytest

from signalpipe.llm import (LLMError, SpendCapExceeded, UsageLimitExhausted,
                            adapter)

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class RunRecorder:
    """A callable stand-in for a backend ``run`` / ``consensus`` / ``status``.

    Records every ``(args, kwargs)`` on ``.calls``. Returns ``result`` (or the
    result of calling it, when callable) unless ``exc`` is set, in which case it
    raises. Accepts any signature so it can double for the 5-arg and 6-arg
    backend runs and the 0-arg quota status.
    """

    def __init__(self, result: Any = None, exc: Optional[BaseException] = None):
        self.result = result
        self.exc = exc
        self.calls = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self.exc is not None:
            raise self.exc
        if callable(self.result):
            return self.result(*args, **kwargs)
        return self.result

    @property
    def called(self) -> bool:
        return bool(self.calls)


@pytest.fixture
def leaves(monkeypatch):
    """Patch every leaf the adapter dispatches to, with benign defaults.

    Each backend returns a distinct sentinel dict so a test can tell which one
    produced the returned object. Individual tests override ``.result`` / ``.exc``.
    """
    ns = types.SimpleNamespace()
    ns.local_run = RunRecorder(result=({"src": "local"}, 0.0))
    ns.consensus = RunRecorder(result={"src": "consensus"})
    ns.cli_run = RunRecorder(result=({"src": "cli"}, 0.0))
    ns.api_run = RunRecorder(result=({"src": "api"}, 0.0))
    ns.status = RunRecorder(result=(False, ""))
    ns.assert_cap = RunRecorder(result=None)
    ns.record = RunRecorder(result=None)

    monkeypatch.setattr(adapter.backend_local, "run", ns.local_run)
    monkeypatch.setattr(adapter.backend_local, "consensus", ns.consensus)
    monkeypatch.setattr(adapter.backend_cli, "run", ns.cli_run)
    monkeypatch.setattr(adapter.backend_api, "run", ns.api_run)
    monkeypatch.setattr(adapter.quota, "status", ns.status)
    monkeypatch.setattr(adapter.spend, "assert_under_cap", ns.assert_cap)
    monkeypatch.setattr(adapter.spend, "record", ns.record)
    return ns


@pytest.fixture
def fake_conn():
    """An opaque connection sentinel — spend is patched, so it is never used
    as a real DB, only checked for identity in ``record`` assertions."""
    return object()


def _route_local(cfg, tier="triage"):
    """Force ``backend_for(tier)`` to resolve to 'local' via a tier override."""
    cfg.data["backend"]["tier_overrides"] = {tier: "local"}


def _route_api(cfg, tier="triage"):
    cfg.data["backend"]["tier_overrides"] = {tier: "api"}


# --------------------------------------------------------------------------- #
# LOCAL tier — free, bypasses cap + ledger
# --------------------------------------------------------------------------- #
def test_local_single_model_returns_zero_cost_and_skips_cap(cfg, fake_conn, leaves):
    _route_local(cfg)
    leaves.local_run.result = ({"verdict": "keep"}, 0.0)

    obj, cost = adapter.complete_with_cost(
        "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn)

    assert obj == {"verdict": "keep"}
    assert cost == 0.0
    # single local model -> one run, no consensus fusion
    assert len(leaves.local_run.calls) == 1
    assert leaves.local_run.calls[0][0][0] == "qwen2.5:14b"  # tiers.triage.local
    assert not leaves.consensus.called
    # local NEVER touches the cap or the ledger
    assert not leaves.assert_cap.called
    assert not leaves.record.called


def test_local_multiple_models_fuse_via_consensus(cfg, fake_conn, leaves):
    _route_local(cfg)
    cfg.data["tiers"]["triage"]["local"] = ["m1", "m2"]
    leaves.local_run.result = ({"n": 1}, 0.0)
    leaves.consensus.result = {"fused": True}

    obj, cost = adapter.complete_with_cost(
        "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn)

    assert obj == {"fused": True}
    assert cost == 0.0
    # both arena models were run, in order
    assert [c[0][0] for c in leaves.local_run.calls] == ["m1", "m2"]
    # consensus fed the two single-model outputs + the schema
    assert leaves.consensus.calls[0][0][0] == [{"n": 1}, {"n": 1}]
    assert leaves.consensus.calls[0][0][1] is SCHEMA
    assert not leaves.assert_cap.called
    assert not leaves.record.called


def test_local_failure_degrades_to_subscription_fallback(cfg, fake_conn, leaves):
    _route_local(cfg)
    # fixture fallback is 'subscription'
    leaves.local_run.exc = LLMError("ollama down", cost_usd=0.0)
    leaves.cli_run.result = ({"src": "cli"}, 0.11)

    obj, cost = adapter.complete_with_cost(
        "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn)

    assert obj == {"src": "cli"}
    assert cost == 0.11
    # degraded through the subscription cloud path
    assert leaves.cli_run.called
    assert not leaves.api_run.called
    # fallback is cap-gated + recorded as subscription
    assert leaves.assert_cap.called
    assert leaves.record.calls[0][0][1] == "subscription"
    assert leaves.record.calls[0][0][2] == 0.11


def test_local_failure_degrades_to_api_fallback(cfg, fake_conn, leaves):
    _route_local(cfg)
    cfg.data["backend"]["local"]["fallback"] = "api"
    leaves.local_run.exc = LLMError("ollama down", cost_usd=0.0)
    leaves.api_run.result = ({"src": "api"}, 0.3)

    obj, cost = adapter.complete_with_cost(
        "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn)

    assert obj == {"src": "api"}
    assert cost == 0.3
    assert leaves.api_run.called
    assert not leaves.cli_run.called
    # api fallback skips the quota hold (that gate is subscription-only)
    assert not leaves.status.called
    assert leaves.record.calls[0][0][1] == "api"


@pytest.mark.parametrize("fallback", ["local", None])
def test_local_failure_reraises_when_no_cloud_fallback(cfg, fake_conn, leaves, fallback):
    _route_local(cfg)
    if fallback is None:
        del cfg.data["backend"]["local"]["fallback"]
    else:
        cfg.data["backend"]["local"]["fallback"] = fallback
    leaves.local_run.exc = LLMError("ollama down", cost_usd=0.0)

    with pytest.raises(LLMError, match="ollama down"):
        adapter.complete_with_cost(
            "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn)

    # no cloud dispatch, no ledger write on a local-only failure
    assert not leaves.cli_run.called
    assert not leaves.api_run.called
    assert not leaves.record.called


# --------------------------------------------------------------------------- #
# SUBSCRIPTION tier — usage-limit hold + spend cap
# --------------------------------------------------------------------------- #
def test_usage_limit_hold_fast_fails_without_spawn(cfg, fake_conn, leaves):
    # default selector is 'subscription'
    leaves.status.result = (True, "usage limit hit until 15:00")

    with pytest.raises(UsageLimitExhausted) as ei:
        adapter.complete_with_cost(
            "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn)

    assert ei.value.cost_usd == 0.0
    assert "usage limit hit until 15:00" in str(ei.value)
    # fast-fail: never even reach the cap check or the CLI spawn
    assert not leaves.assert_cap.called
    assert not leaves.cli_run.called
    assert not leaves.record.called


def test_spend_cap_blocks_dispatch(cfg, fake_conn, leaves):
    leaves.assert_cap.exc = SpendCapExceeded("daily spend cap hit")

    with pytest.raises(SpendCapExceeded, match="daily spend cap hit"):
        adapter.complete_with_cost(
            "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn)

    # cap gate is BEFORE dispatch: no call, no record
    assert not leaves.cli_run.called
    assert not leaves.record.called


def test_success_subscription_records_backend_and_cost(cfg, fake_conn, leaves):
    leaves.cli_run.result = ({"ok": True}, 0.42)

    obj, cost = adapter.complete_with_cost(
        "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn, effort="high")

    assert (obj, cost) == ({"ok": True}, 0.42)
    # resolved the subscription model id + passed effort through
    cli_args = leaves.cli_run.calls[0][0]
    assert cli_args[0] == "claude-haiku-4-5"  # tiers.triage.subscription
    assert cli_args[5] == "high"
    # recorded exactly once against the resolved backend
    assert len(leaves.record.calls) == 1
    rec_args, rec_kwargs = leaves.record.calls[0]
    assert rec_args[0] is fake_conn
    assert rec_args[1] == "subscription"
    assert rec_args[2] == 0.42
    assert rec_kwargs["kind"] == "daily"
    assert not leaves.api_run.called


def test_success_api_records_backend_and_cost(cfg, fake_conn, leaves):
    _route_api(cfg)
    leaves.api_run.result = ({"ok": True}, 0.7)

    obj, cost = adapter.complete_with_cost(
        "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn)

    assert (obj, cost) == ({"ok": True}, 0.7)
    # api backend resolves the api model id and skips the subscription-only hold
    assert leaves.api_run.calls[0][0][0] == "claude-haiku-4-5"  # tiers.triage.api
    assert not leaves.status.called
    assert not leaves.cli_run.called
    assert leaves.record.calls[0][0][1] == "api"
    assert leaves.record.calls[0][0][2] == 0.7


def test_cap_kind_propagates_to_gate_and_record(cfg, fake_conn, leaves):
    leaves.cli_run.result = ({"ok": True}, 0.05)

    adapter.complete_with_cost(
        "digest", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn, cap_kind="digest")

    assert leaves.assert_cap.calls[0][1]["kind"] == "digest"
    assert leaves.record.calls[0][1]["kind"] == "digest"


# --------------------------------------------------------------------------- #
# Failed-call cost accounting
# --------------------------------------------------------------------------- #
def test_failed_call_unknown_cost_records_estimate(cfg, fake_conn, leaves):
    # cost_usd defaults to None -> "a call plausibly happened"
    leaves.cli_run.exc = LLMError("boom")

    with pytest.raises(LLMError, match="boom"):
        adapter.complete_with_cost(
            "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn)

    assert len(leaves.record.calls) == 1
    rec_args = leaves.record.calls[0][0]
    assert rec_args[1] == "subscription"
    assert rec_args[2] == 0.02  # cfg.spend.failed_call_estimate_usd


def test_failed_call_zero_cost_records_nothing(cfg, fake_conn, leaves):
    # explicit 0.0 -> definitively free (e.g. the CLI binary never ran)
    leaves.cli_run.exc = LLMError("binary missing", cost_usd=0.0)

    with pytest.raises(LLMError, match="binary missing"):
        adapter.complete_with_cost(
            "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn)

    assert not leaves.record.called


def test_failed_call_reported_cost_is_recorded(cfg, fake_conn, leaves):
    leaves.cli_run.exc = LLMError("half a call", cost_usd=0.5)

    with pytest.raises(LLMError, match="half a call"):
        adapter.complete_with_cost(
            "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn)

    assert len(leaves.record.calls) == 1
    assert leaves.record.calls[0][0][2] == 0.5


def test_failed_api_call_records_against_api_backend(cfg, fake_conn, leaves):
    _route_api(cfg)
    leaves.api_run.exc = LLMError("api boom", cost_usd=0.25)

    with pytest.raises(LLMError, match="api boom"):
        adapter.complete_with_cost(
            "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn)

    assert leaves.record.calls[0][0][1] == "api"
    assert leaves.record.calls[0][0][2] == 0.25


# --------------------------------------------------------------------------- #
# model_override — forces the subscription cloud path on a specific model id
# --------------------------------------------------------------------------- #
def test_model_override_forces_subscription_even_for_local_tier(cfg, fake_conn, leaves):
    _route_local(cfg)  # would normally use the local arena
    leaves.cli_run.result = ({"ok": True}, 0.9)

    obj, cost = adapter.complete_with_cost(
        "triage", "sys", "prompt", SCHEMA,
        cfg=cfg, conn=fake_conn, model_override="claude-opus-4-8")

    assert (obj, cost) == ({"ok": True}, 0.9)
    # local arena is bypassed entirely
    assert not leaves.local_run.called
    # routed to the subscription CLI on the override model
    assert leaves.cli_run.calls[0][0][0] == "claude-opus-4-8"
    # still cap-gated + recorded as subscription
    assert leaves.assert_cap.called
    assert leaves.record.calls[0][0][1] == "subscription"


def test_model_override_is_still_cap_gated(cfg, fake_conn, leaves):
    leaves.assert_cap.exc = SpendCapExceeded("cap hit")

    with pytest.raises(SpendCapExceeded):
        adapter.complete_with_cost(
            "triage", "sys", "prompt", SCHEMA,
            cfg=cfg, conn=fake_conn, model_override="claude-opus-4-8")

    assert not leaves.cli_run.called
    assert not leaves.record.called


# --------------------------------------------------------------------------- #
# complete() — thin wrapper returning just the dict
# --------------------------------------------------------------------------- #
def test_complete_returns_dict_only(cfg, fake_conn, leaves):
    leaves.cli_run.result = ({"payload": 1}, 0.3)

    result = adapter.complete(
        "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn)

    assert result == {"payload": 1}  # dict, not the (dict, cost) tuple
    assert leaves.record.calls[0][0][2] == 0.3


def test_complete_propagates_errors(cfg, fake_conn, leaves):
    leaves.assert_cap.exc = SpendCapExceeded("nope")

    with pytest.raises(SpendCapExceeded, match="nope"):
        adapter.complete(
            "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=fake_conn)


# --------------------------------------------------------------------------- #
# probe_auth — cheap preflight, routed by the active selector
# --------------------------------------------------------------------------- #
def test_probe_auth_subscription_ok(cfg, leaves):
    leaves.cli_run.result = ({"ok": True}, 0.0)

    ok, msg = adapter.probe_auth(cfg)

    assert ok is True
    assert msg == "auth ok"
    # subscription selector -> CLI backend, on the triage model
    assert leaves.cli_run.calls[0][0][0] == "claude-haiku-4-5"
    assert not leaves.api_run.called


def test_probe_auth_subscription_false_flag(cfg, leaves):
    leaves.cli_run.result = ({"ok": False}, 0.0)

    ok, msg = adapter.probe_auth(cfg)

    assert ok is False
    assert msg == "auth ok"


def test_probe_auth_missing_ok_key_is_false(cfg, leaves):
    leaves.cli_run.result = ({}, 0.0)

    ok, msg = adapter.probe_auth(cfg)

    assert ok is False
    assert msg == "auth ok"


def test_probe_auth_maps_llmerror_to_false_reason(cfg, leaves):
    leaves.cli_run.exc = LLMError("not authenticated")

    ok, msg = adapter.probe_auth(cfg)

    assert ok is False
    assert msg == "not authenticated"


def test_probe_auth_api_selector_uses_api_backend(cfg, leaves):
    cfg.data["backend"]["selector"] = "api"
    leaves.api_run.result = ({"ok": True}, 0.0)

    ok, msg = adapter.probe_auth(cfg)

    assert (ok, msg) == (True, "auth ok")
    assert leaves.api_run.called
    assert not leaves.cli_run.called


# --------------------------------------------------------------------------- #
# Integration: real spend module + real DB, only the leaf backend faked
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_end_to_end_cap_enforcement(cfg, conn, monkeypatch):
    """A successful subscription call bumps cli_usd through the REAL spend
    ledger; a second call once the cap is reached raises SpendCapExceeded."""
    # Freeze the ledger's day so the seeded/recorded rows line up deterministically.
    monkeypatch.setattr(adapter.spend, "_today", lambda: "2026-07-04")
    # No usage-limit hold armed.
    monkeypatch.setattr(adapter.quota, "status", lambda: (False, ""))

    cfg.data["spend"]["daily_cap_usd"] = 1.0
    cli = RunRecorder(result=({"ok": True}, 1.0))
    monkeypatch.setattr(adapter.backend_cli, "run", cli)

    # First call: under cap -> dispatches and records.
    obj, cost = adapter.complete_with_cost(
        "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=conn)
    assert (obj, cost) == ({"ok": True}, 1.0)
    assert adapter.spend.today_total(conn) == 1.0
    assert len(cli.calls) == 1

    # Second call: cap now reached (spent 1.0 >= cap 1.0) -> blocked before dispatch.
    with pytest.raises(SpendCapExceeded):
        adapter.complete_with_cost(
            "triage", "sys", "prompt", SCHEMA, cfg=cfg, conn=conn)

    assert len(cli.calls) == 1  # no second spawn
    row = conn.execute(
        "SELECT cli_usd, calls FROM spend WHERE day='2026-07-04'").fetchone()
    assert row["cli_usd"] == 1.0
    assert row["calls"] == 1
