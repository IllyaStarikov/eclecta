"""Dead-man's-switch pings. No-ops unless the env carries a ping URL, so the
default install stays local-only; paste healthchecks.io URLs into
~/.config/signal/worker.env (HEALTHCHECKS_WORKER_URL / HEALTHCHECKS_PUBLISH_URL)
to get external alerting with zero code change. Monitoring must never break
the pipeline: every failure here is swallowed."""

from __future__ import annotations

import os


def ping(name: str) -> None:
    """name in {"worker", "publish"} — fire-and-forget liveness ping."""
    url = os.environ.get("HEALTHCHECKS_%s_URL" % name.upper())
    if not url:
        return
    try:
        import httpx

        httpx.get(url, timeout=5)
    except Exception:  # noqa: BLE001 — monitoring never breaks the pipeline
        pass
