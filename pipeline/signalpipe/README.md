# Signal — local tech+AI feed-curation pipeline

A continuously-running local pipeline that ingests 1,000+ sources, dedups
stories into clusters, scores them deterministically (free), has Claude read
and curate the ~40 daily finalists, serves a **parameterized RSS feed** + a
review dashboard on `127.0.0.1:8765`, and writes a weekly Opus digest that can
be promoted (on explicit approval) to the blog's separated `/signal/`
collection — never the main blog feed or newsletter.

Provenance/design reference: `doc/signal_research.md`. Control surface:
`/signal` skill. Source expansion: `/source-hunt` (loop with
`/loop /source-hunt` until ≥1,000 verified).

## Quick start

```bash
cd <repo root>
python3 -m pip install -r signalpipe/requirements.txt

python3 -m signalpipe sources seed      # registry -> DB (+ generates sources.opml)
python3 -m signalpipe sources expand    # Techmeme lb.opml + arXiv + Reddit subs
python3 -m signalpipe ingest            # poll everything due
python3 -m signalpipe score             # rank clusters (no LLM, free)
python3 -m signalpipe serve             # http://127.0.0.1:8765/
```

LLM stages (spend — gated by `config/signal.json -> spend` caps):

```bash
python3 -m signalpipe fetch             # article extraction + paywall chain (free)
python3 -m signalpipe curate --dry-run  # what would be read
python3 -m signalpipe curate            # triage (Haiku) -> deep (Sonnet)
python3 -m signalpipe digest            # weekly Opus digest
python3 -m signalpipe promote --target local --apply   # preview on local Ghost
```

Daemons (TCC-safe runtime copy under `~/.local/state/signal/app/`):

```bash
python3 -m signalpipe install           # runtime copy + 2 launchd agents
python3 -m signalpipe sync --restart    # after editing signalpipe/** or config
```

## Layout

- `config/signal.json` — all knobs: cadences, funnel sizes, tier→model map,
  `backend.selector` (`subscription` = `claude -p` | `api` = metered SDK),
  spend caps, channels, score weights, paywall policy, dedup thresholds.
- `signalpipe/sources.json` — THE source registry (all access types).
  `sources.opml` is GENERATED from it (review + `/opml` + reader import).
- DB: `~/.local/state/signal/signal.db` (WAL; one writer = worker/CLI,
  read-only server). **Never** inside iCloud; never file-copy live
  (`VACUUM INTO` for backups).
- Feed: `GET /feed.xml?channel=ai&min_score=6&min_relevance=7&since=7d&limit=25&sources=hacker-news,lobsters`
  + `/feed/<channel>.xml`, `/opml`, `/healthz`.

## Separation guarantees (vs. the main blog)

- Digests publish under tag `Signal` → routes.yaml's `/signal/` collection
  (declared ABOVE `/blog/`, so they never enter `/blog/` or `/blog/rss`).
- Theme queries that aren't featured-gated exclude `tag:-signal`
  (archive, essay count, error page, recent-blocks).
- Publishers are shelled with `--no-feature` and never pass a newsletter
  param — Ghost emails nothing.
- archive.today links: config-off by default, internal-only column,
  digest + promote both refuse them in any publishable body.

## Cost model

Deterministic funnel (consensus/engagement/reputation/recency/topic) ranks
thousands; only finalists get LLM reads. Subscription backend = the separate
Agent SDK monthly credit ($100 Max 5x / $200 Max 20x, hard-stop) at API
rates; `total_cost_usd` from every `claude -p` envelope lands in the `spend`
table and the pre-flight cap gate stops curation before the credit drains.
Flip `backend.selector` to `api` for Batches/Haiku economics on a metered key.
