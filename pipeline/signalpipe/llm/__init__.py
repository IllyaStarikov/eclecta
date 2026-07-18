"""Pluggable LLM layer.

adapter.complete(tier, system, prompt, schema, cfg=...) is the ONLY entry
point pipeline stages use. tier -> model resolves via config; the backend
selector swaps between:

  subscription — headless `claude -p --json-schema` (separate Agent SDK
                 credit; no API key involved; keychain OAuth)
  api          — anthropic SDK with output_config json_schema (+ prompt cache)

Both return the same schema-validated dict. Every call is pre-gated by the
daily spend cap and recorded in the spend ledger.
"""

class LLMError(Exception):
    """Unrecoverable LLM failure (after retry).

    cost_usd carries what the failed call(s) cost so the adapter records it
    in the spend ledger before re-raising:
      None  — unknown (a call plausibly happened): the adapter charges the
              configured failed_call_estimate_usd
      0.0   — definitively free (e.g. the backend binary never ran)
      > 0   — actual reported cost of the failed attempt(s)
    """

    def __init__(self, message, cost_usd=None):
        super().__init__(message)
        self.cost_usd = cost_usd


class SpendCapExceeded(LLMError):
    """Daily/digest spend cap reached; no call was made."""

    def __init__(self, message):
        super().__init__(message, cost_usd=0.0)


class UsageLimitExhausted(LLMError):
    """Subscription usage limit hit (Max plan 5-hour/weekly quota).

    Not a permanent failure: quota.py arms a hold and the worker re-checks on
    an interval, so stages defer instead of marking items failed. retry_at is
    the epoch when the hold expires — the CLI's reset hint when it gave one,
    else now + backend.quota_recheck_min.
    """

    def __init__(self, message, retry_at=None, cost_usd=0.0):
        super().__init__(message, cost_usd=cost_usd)
        self.retry_at = retry_at
