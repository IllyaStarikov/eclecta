# pipeline: signalpipe

The local curation pipeline behind Eclecta. This directory is the **versioned
source of truth** for the pipeline; it is *not* built or run from here. The
launchd worker runs a TCC-safe copy under `~/.local/state/signal/app/`, kept in
sync with this tree (see [`../docs/operating-runbook.md`](../docs/operating-runbook.md)).

It reads ~1,500 sources, clusters stories, scores them deterministically, has
Claude read the day's finalists and write the editions on five cadences (daily,
weekly, monthly, quarterly, yearly), and pushes the output into this repo's
`src/content/digests/` and `src/data/`. The site never depends on the pipeline
being online.

```
signalpipe/     the pipeline package (ingest, score, curate, digest, publish)
ops/            run-worker.sh, signal-watchdog.sh (launchd job bodies)
config/         signal.example.json, a redacted reference of the runtime config
```

The editorial docs the pipeline reads at runtime live in [`../docs/`](../docs):
`digest-style.md` (voice), `editorial-policy.md` (what to publish),
`cadence-templates.md` (per-kind shape). The runtime config the worker actually
reads is `~/.local/state/signal/app/config/signal.json`.

## Running (on the host)

```
python3 -m pip install -r signalpipe/requirements.txt
python3 -m signalpipe ingest      # poll sources
python3 -m signalpipe score       # rank clusters (free)
python3 -m signalpipe curate      # triage + write (LLM)
python3 -m signalpipe digest --kind daily   # build an edition
python3 -m signalpipe publish --what all    # export to the site repo
```

See [`../docs/operating-runbook.md`](../docs/operating-runbook.md) for the
launchd setup, downtime gating, model routing, tuning, and go-live procedure.
