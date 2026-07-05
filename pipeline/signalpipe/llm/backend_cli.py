"""Subscription backend: headless `claude -p`.

Contract (per doc/signal_research.md Part E):
- Prompt via STDIN (never argv — article text contains shell metacharacters).
- `--output-format json` returns an envelope: is_error, result (string),
  structured_output (when --json-schema is passed), total_cost_usd, usage.
- `--json-schema` is the primary structured-output path; defensive extraction
  from .result is the fallback; ONE repair retry; then LLMError.
- NO --bare (it skips keychain OAuth); ANTHROPIC_API_KEY is removed from the
  child env so billing can never silently switch to metered.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Dict, Optional, Tuple

from . import LLMError

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Pull a JSON object out of model text: fences first, then brace-match."""
    if not text:
        return None
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def _validate(obj: Dict[str, Any], schema: Dict[str, Any]) -> Optional[str]:
    """Minimal structural validation: required keys + coarse types."""
    if not isinstance(obj, dict):
        return "not an object"
    for key in schema.get("required", []):
        if key not in obj:
            return "missing required key %r" % key
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    for key, sub in (schema.get("properties") or {}).items():
        if key not in obj or obj[key] is None:
            continue
        expected = type_map.get(sub.get("type"))
        if expected and not isinstance(obj[key], expected):
            return "key %r has wrong type" % key
    return None


def run(
    model: str,
    system: str,
    prompt: str,
    schema: Dict[str, Any],
    cfg,
    effort: Optional[str] = None,
) -> Tuple[Dict[str, Any], float]:
    """Returns (validated dict, cost_usd). Raises LLMError on failure.

    effort (low|medium|high|xhigh|max) maps to claude's --effort: it sets
    thinking depth and overall token spend. Per-item judgment runs 'low'
    (cheap, the task is classification/extraction); digests run 'max'."""
    cli = cfg.backend.get("cli_bin", "claude")
    timeout = int(cfg.backend.get("cli_timeout_sec", 240))
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # subscription billing only, explicitly

    # max-turns >= 2: with --json-schema the model emits a text turn THEN the
    # structured-output turn, so --max-turns 1 always errors with "Reached
    # maximum number of turns (1)". Tools are all disallowed + dontAsk, so the
    # extra turns carry no runaway risk.
    max_turns = str(int(cfg.backend.get("cli_max_turns", 4)))
    argv = [
        cli,
        "-p",
        "--model", model,
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--max-turns", max_turns,
        "--permission-mode", "dontAsk",
        "--disallowedTools", "Bash Edit Write WebFetch WebSearch Task",
        "--system-prompt", system,
    ]
    if effort:
        argv += ["--effort", effort]
    # Optional context-trimming for batch headless runs (the one-time backfill
    # sets these in its config): --strict-mcp-config + --setting-sources project
    # drop MCP tool schemas and user-global settings/memory, and an empty cwd
    # drops project CLAUDE.md — together cutting ~13K tokens of cache-created
    # harness context off every call (~$0.50 -> ~$0.06). Absent from the live
    # config, so the worker's path is unchanged.
    argv += [str(a) for a in cfg.backend.get("cli_extra_args", [])]
    cli_cwd = cfg.backend.get("cli_cwd") or None

    last_err = "unknown"
    attempt_prompt = prompt
    total_cost = 0.0
    for attempt in (1, 2):
        try:
            proc = subprocess.run(
                argv,
                input=attempt_prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=cli_cwd,
            )
        except subprocess.TimeoutExpired:
            # cost unknown — the call ran for up to `timeout` seconds and
            # may well have been billed; the adapter charges an estimate.
            raise LLMError(
                "claude -p timed out after %ds (model %s)" % (timeout, model),
                cost_usd=total_cost or None)
        except OSError as e:
            # Never executed: definitively free.
            raise LLMError("cannot exec %s: %s" % (cli, e),
                           cost_usd=total_cost or 0.0)

        if proc.returncode != 0 and not proc.stdout.strip():
            last_err = (proc.stderr or "exit %d" % proc.returncode)[:300]
            raise LLMError("claude -p failed: %s" % last_err,
                           cost_usd=total_cost or None)

        try:
            envelope = json.loads(proc.stdout)
        except ValueError:
            raise LLMError("claude -p non-JSON envelope: %s" % proc.stdout[:200],
                           cost_usd=total_cost or None)

        total_cost += float(envelope.get("total_cost_usd") or 0.0)

        # Infrastructure errors (credit, turns, rate limit, auth) won't be
        # fixed by a prompt-repair retry — surface them immediately with the
        # real detail instead of masking as a validation failure. The
        # envelope still carries a real total_cost_usd for rate-limit/
        # turn-limit failures — pass it along so the ledger sees it.
        if envelope.get("is_error"):
            detail = envelope.get("errors") or envelope.get("result")
            raise LLMError("claude -p api error (%s): %s" % (
                envelope.get("api_error_status"), str(detail)[:300]),
                cost_usd=total_cost or None)

        obj = envelope.get("structured_output")
        if not isinstance(obj, dict):
            obj = _extract_json(str(envelope.get("result") or ""))
        if isinstance(obj, dict):
            err = _validate(obj, schema)
            if err is None:
                return obj, total_cost
            last_err = "schema validation: %s" % err
        else:
            last_err = "no parseable JSON in structured_output/result"

        if attempt == 1:
            attempt_prompt = (
                prompt
                + "\n\nYour previous output failed validation (%s). "
                "Return ONLY a JSON object matching the schema — no prose, "
                "no markdown fences." % last_err
            )

    raise LLMError("claude -p output invalid after retry: %s" % last_err,
                   cost_usd=total_cost or None)
