# Curation eval sets

A versioned gold corpus for measuring whether the **current** judge still makes
the calls we want — a regression alarm, and a way to watch the "featured" bar
tighten over time.

Nothing here touches the live pipeline. Candidates are built from the database
**read-only** (never the dashboard, which is itself read-only) using the
`published_ledger` as ground truth for whether a story was *featured*. The judge
is replayed with the same system prompt + schema the pipeline uses, on the free
**local** backend by default ($0).

## Layout

- `gold/curation.jsonl` — one labeled example per line (schema below).
- `results/<date>.json` — the metrics from one `eval run`.

## Example schema (one JSON object per line)

```json
{
  "id": "<story_id or url:...>",
  "title": "...", "source": "...", "url": "...",
  "excerpt": "...",
  "human": {"featured": true, "relevance": 8, "category": "ai"},
  "provenance": "edition | skipped | top-uncurated | manual",
  "labeled_by": "seed | nightly | illya",
  "confidence": "provisional | confirmed"
}
```

`provisional` labels come from past outcomes (what was featured / skipped);
`confirmed` labels are human-checked. Correct one with:

```
python3 -m signalpipe eval label --id <id> --featured --relevance 8 --category ai
```

## Commands

```
python3 -m signalpipe eval grow -k 5      # add a few provisional candidates from the DB
python3 -m signalpipe eval run            # replay the judge (local backend, $0) + record metrics
python3 -m signalpipe eval report         # print the latest metrics
```

## Metrics

`agreement_featured` (judge vs. gold on the yes/no feature call),
`featured_precision` / `featured_recall`, `relevance_mae` (0–10 scale), and
`category_accuracy`. A drop across a run — with the gold set unchanged — means
the judge (prompt or model) regressed. The nightly pass grows the set a few
examples at a time and records the trend in `ops/journal/`.
