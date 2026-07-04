# Signal pipeline v2 — staged (not yet activated)

Built and validated on a **copy** of the live DB on 2026-06-13. The live DB and
the launchd worker were deliberately left untouched — activation is a single
supervised step (below). The Lede site already shows the v2 taxonomy because the
front end derives it from the existing v1 export; this change makes the pipeline
emit it natively and adds cross-edition de-duplication.

## What changed (uncommitted in this repo — review `git diff signalpipe/`)

- **`topics.py`** — `TAXONOMY` (6 categories × subcategories) + `match_taxonomy()`,
  mirroring `~/dev/lede/src/lib/taxonomy.ts` so derived and native categories
  agree. `BASE_LEXICON`/`match_channels` kept (score.py unchanged).
- **`dedup.py`** — `story_id()` (stable content id: canonical domain+path, else
  title-key) stamped on every new cluster.
- **`db.py`** — schema **v5**: `clusters.story_id`, `curations.category` +
  `curations.subcategories`, and a `published_ledger` table. `_migrate_taxonomy_v5`
  is additive + idempotent and backfills `story_id` for all existing clusters.
- **`retag.py`** (new) + CLI `retag` — deterministic backfill of
  `category`/`subcategories` onto every historical curation, plus seeding the
  ledger from already-published digests. `--dry-run` writes nothing.
- **`digest.py`** — `_gather` excludes any story already run in a *prior edition
  of the same cadence* (the ledger); `run` records the edition's stories. A daily
  never repeats a previous daily; weeklies+ may still synthesize their dailies.
- **`publish.py`** — `export_picks` additionally emits `story_id`, `category`,
  `subcategories`, `state="confident"`, and `published_at` (additive; `channels`
  retained). Un-retagged rows fall back to `match_taxonomy`, so it's never null.

The Lede `src/lib/schema.ts` already accepts these as optional fields, so a v2
export validates against the site contract with no further change.

## Validation (on `/tmp` copies, never the live DB)

- v4→v5 migration: all columns + ledger created; **53,673/53,673** clusters got a
  `story_id` (53,072 distinct); idempotent on re-run.
- `retag`: 1,707 curations tagged (industry 680, ai 475, research 181, security
  155, software 125, hardware 91); ledger seeded with 19 daily + 12 weekly rows.
- `export_picks`: 60 picks, every one with category/subcategories/story_id/state/
  channels/published_at; 0 missing `story_id`.
- dedup: a new daily would skip the 19 stories already in the daily edition.

## To activate (supervised — migrates the live DB + restarts the worker)

```bash
cd <this repo>
python3 -m signalpipe backup                 # snapshot first (VACUUM INTO)
python3 -m signalpipe sync --restart         # copy repo→runtime, restart worker
                                             #   (worker start runs the v5 migration)
python3 -m signalpipe retag --dry-run        # eyeball the category distribution
python3 -m signalpipe retag                  # tag old curations + seed the ledger
python3 -m signalpipe publish --what picks --no-push   # dry-run the v2 export
#   diff src/data/picks.json in ~/dev/lede, confirm category/story_id present,
#   then let the next scheduled publish push, or: publish --what all
```

Safe to run later; nothing here expires. A v4 worker tolerates a v5 DB (it
ignores the new columns), so even a partial activation can't corrupt anything.
