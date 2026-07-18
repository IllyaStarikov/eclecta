"""Metered API backend: anthropic SDK with guaranteed structured output.

- A single FORCED tool (tool_choice=tool) whose input_schema is the caller's
  JSON schema → the tool_use.input block is schema-valid JSON. This is the
  portable structured-output path on the Messages API (the installed anthropic
  SDK predates the newer output_config/json_schema response format).
- The rubric/system block carries cache_control so repeated curation calls
  hit the prompt cache (verify via usage.cache_read_input_tokens).
- Cost computed from usage × the pricing table below (cache reads ~0.1×,
  cache writes 1.25× input price).
- The opus/digest tier runs on the subscription claude -p backend (which
  carries adaptive thinking + effort), so this metered path stays simple.

Note: the Batches endpoint (−50%, async) is a planned optimization for the
deep tier; the single-call path below is correct and is what the adapter
uses today (config flag api_use_batches reserved).
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from . import LLMError

# $/MTok (input, output) — claude-api skill table, June 2026.
PRICING = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
}


def _cost(usage, model: str) -> float:
    pin, pout = PRICING.get(model, (5.0, 25.0))
    get = lambda k: float(getattr(usage, k, 0) or 0)  # noqa: E731
    return (
        get("input_tokens") * pin
        + get("output_tokens") * pout
        + get("cache_read_input_tokens") * pin * 0.1
        + get("cache_creation_input_tokens") * pin * 1.25
    ) / 1_000_000.0


def run(
    model: str,
    system: str,
    prompt: str,
    schema: Dict[str, Any],
    cfg,
    effort: Optional[str] = None,
) -> Tuple[Dict[str, Any], float]:
    import anthropic

    # ANTHROPIC_API_KEY from env — metered path. max_retries lets the SDK ride
    # out transient failures (429 rate-limit, 500/503, overloaded) with
    # exponential backoff before surfacing an LLMError, so one blip never fails
    # an item the worker would otherwise have to re-curate next cadence.
    client = anthropic.Anthropic(
        max_retries=int(cfg.backend.get("api_max_retries", 4)))

    system_param: Any = system
    if cfg.backend.get("api_use_prompt_cache", True):
        system_param = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    # Structured output via a single forced tool: the tool's input_schema is the
    # caller's JSON schema, so the model's tool_use.input IS the validated
    # object — no fragile text/JSON parsing.
    tool_name = "emit_result"
    kwargs: Dict[str, Any] = dict(
        model=model,
        max_tokens=16000,
        system=system_param,
        messages=[{"role": "user", "content": prompt}],
        tools=[{
            "name": tool_name,
            "description": "Emit the structured result for this item.",
            "input_schema": schema,
        }],
        tool_choice={"type": "tool", "name": tool_name},
    )
    try:
        resp = client.messages.create(**kwargs)
    except anthropic.APIError as e:
        # cost unknown (request may have been partially billed) — the
        # adapter charges the configured failed-call estimate.
        raise LLMError("anthropic API error: %s" % e, cost_usd=None)

    # Compute the cost BEFORE extracting, so a malformed response still carries
    # the real spend into the ledger.
    cost = _cost(resp.usage, model)
    for block in resp.content:
        if (getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == tool_name):
            data = getattr(block, "input", None)
            if isinstance(data, dict):
                return data, cost
            raise LLMError("tool_use.input was not an object", cost_usd=cost)
    raise LLMError("no tool_use block in API response", cost_usd=cost)
