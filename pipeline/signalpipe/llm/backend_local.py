"""Local, subscription-free backend: Ollama with schema-constrained JSON.

Used for the JUDGMENT tiers (triage, judge) — never for writing. Each call goes
to a local model via Ollama's `format` (JSON-schema-constrained generation), so
the output is structurally valid by construction; we still validate + one repair
retry defensively. Cost is always 0.0.

Only ONE ~70B model fits in 64GB at a time, so the arena/ensemble is driven by
the caller (curate.py) MODEL-OUTER, ITEMS-INNER — run model A over the whole
batch (it stays resident via keep_alive), then model B, then `consensus()` per
item. This module therefore stays a plain per-item, single-model `run()` plus a
pure `consensus()` combiner; it never loops models itself.
"""

from __future__ import annotations

import json
import re
import statistics
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

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


def _validate(obj: Any, schema: Dict[str, Any]) -> Optional[str]:
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


def _ollama_chat(
    base_url: str, model: str, system: str, prompt: str,
    schema: Dict[str, Any], timeout: int, num_ctx: int, keep_alive: str,
) -> str:
    """One Ollama /api/chat call with schema-constrained output. Returns the
    raw message content string. Raises LLMError on transport/HTTP failure."""
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "format": schema,            # JSON-schema-constrained generation
        "stream": False,
        "keep_alive": keep_alive,    # hold the model resident across a batch
        "options": {"temperature": 0.2, "num_ctx": num_ctx},
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            env = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise LLMError("ollama unreachable (%s): %s" % (model, e), cost_usd=0.0)
    except (ValueError, OSError) as e:
        raise LLMError("ollama bad response (%s): %s" % (model, e), cost_usd=0.0)
    if env.get("error"):
        raise LLMError("ollama error (%s): %s" % (model, env["error"]), cost_usd=0.0)
    return env.get("message", {}).get("content", "") or ""


def run(
    model: str, system: str, prompt: str, schema: Dict[str, Any], cfg,
) -> Tuple[Dict[str, Any], float]:
    """One local judgment call (single model). Returns (validated dict, 0.0).
    Raises LLMError if the model is unreachable or returns invalid JSON after a
    single repair retry."""
    local = (cfg.backend or {}).get("local", {})
    base_url = local.get("base_url", "http://127.0.0.1:11434")
    timeout = int(local.get("timeout_sec", 600))
    num_ctx = int(local.get("num_ctx", 16384))
    keep_alive = str(local.get("keep_alive", "15m"))

    attempt_prompt = prompt
    last_err = "unknown"
    for attempt in (1, 2):
        content = _ollama_chat(
            base_url, model, system, attempt_prompt, schema,
            timeout, num_ctx, keep_alive,
        )
        obj = _extract_json(content)
        if isinstance(obj, dict):
            err = _validate(obj, schema)
            if err is None:
                return obj, 0.0
            last_err = "schema validation: %s" % err
        else:
            last_err = "no parseable JSON"
        if attempt == 1:
            attempt_prompt = (
                prompt + "\n\nYour previous output was invalid (%s). Return ONLY "
                "a JSON object matching the schema." % last_err
            )
    raise LLMError(
        "local model %s invalid after retry: %s" % (model, last_err),
        cost_usd=0.0,
    )


def consensus(
    objs: List[Dict[str, Any]], schema: Dict[str, Any],
    majority_arrays: Tuple[str, ...] = ("channels",),
) -> Dict[str, Any]:
    """Combine K single-model judgments into one (the arena verdict).

    booleans -> majority (ties broken by the PRIMARY model = objs[0]);
    integer/number -> median (rounded for integers);
    arrays in `majority_arrays` (e.g. channels) -> items a majority picked,
      falling back to the primary's pick (avoids over-tagging from a union);
      other arrays (e.g. facts) -> deduped union, capped to maxItems;
    strings/other -> the primary model's value (objs[0]).

    objs MUST be ordered with the primary/anchor model first. Recommend an ODD
    ensemble size to minimise ties.
    """
    objs = [o for o in objs if isinstance(o, dict)]
    if not objs:
        return {}
    if len(objs) == 1:
        return objs[0]
    primary = objs[0]
    props = schema.get("properties", {})
    out: Dict[str, Any] = {}
    for key, spec in props.items():
        vals = [o[key] for o in objs if key in o and o[key] is not None]
        if not vals:
            if key in primary:
                out[key] = primary[key]
            continue
        t = spec.get("type")
        if t == "boolean":
            trues = sum(1 for v in vals if v)
            if trues * 2 == len(vals):          # tie -> primary
                out[key] = bool(primary.get(key, vals[0]))
            else:
                out[key] = trues * 2 > len(vals)
        elif t in ("integer", "number"):
            med = statistics.median(v for v in vals if isinstance(v, (int, float)))
            out[key] = int(round(med)) if t == "integer" else float(med)
        elif t == "array":
            cap = spec.get("maxItems")
            if key in majority_arrays:
                counts: Dict[Any, int] = {}
                for v in vals:
                    if isinstance(v, list):
                        for item in set(v):
                            counts[item] = counts.get(item, 0) + 1
                thresh = len(vals) / 2.0
                maj = [it for it, n in counts.items() if n > thresh]
                if not maj:  # nobody in a majority -> the primary's pick
                    pv = primary.get(key)
                    maj = list(pv) if isinstance(pv, list) and pv else []
                out[key] = maj[:cap] if isinstance(cap, int) else maj
            else:
                seen: List[Any] = []
                for v in vals:
                    if not isinstance(v, list):
                        continue
                    for item in v:
                        if item not in seen:
                            seen.append(item)
                out[key] = seen[:cap] if isinstance(cap, int) else seen
        else:  # strings / objects -> the primary model's wording
            out[key] = primary.get(key, vals[0])
    # carry any required key the primary had that wasn't in properties
    for key in schema.get("required", []):
        if key not in out and key in primary:
            out[key] = primary[key]
    return out
