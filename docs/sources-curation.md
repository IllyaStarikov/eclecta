# Sources curation runbook

How `src/data/sources.json` — the curated source roll behind the `/sources` page — is
kept world-class. This file is **site-owned and hand/agent-curated** (no `signal:` pipeline
commit writes it), so edits here are safe and durable. It is *not* the live ingestion list
(that lives in the separate `signalpipe` repo); adding the `feed` field makes this file an
OPML-exportable manifest `signalpipe` can import later.

## Relationship to the live ingestion registry (reconciliation)

This roll and the pipeline's `signalpipe/sources.json` are **intentionally divergent**, not
mirror copies — do not assume one is a strict subset of the other:

- **Different purpose.** This file is a hand-curated, feed-expressible *showcase* of subscribable
  sources. The pipeline registry (~3,400 entries) is the actual *fetch list*, and it includes
  API/scrape surfaces that have no simple RSS URL — arXiv, Reddit JSON, HN Algolia, GDELT,
  Mastodon/Bluesky, GitHub Trending — plus primary-feed additions (e.g. the FTC regulator feeds)
  that may not be mirrored onto this roll.
- **Neither contains the other.** Counts differ (this roll ~3,649 vs registry ~3,400) and each
  holds entries the other lacks.
- **Deliberate roll-only exceptions.** Some sources appear here for completeness but are *not*
  ingested — e.g. **EurekAlert!** (a press-release wire that collides with the "no marketing
  dressed as news" bar) and **Reuters** (paywalled / ToS-restrictive). That is by design.

**Reconciliation rule:** the `/sources` page must never imply the pipeline ingests a source it
does not. When curating, treat this roll as a superset-showcase, and periodically eyeball the
delta against `signalpipe/sources.json` (by `homepage`/`feed`) so roll-only exceptions stay
intentional rather than accidental drift. A strict equality test is deliberately *not* enforced —
it would encode a false invariant.

## Schema (6 fields)

Each entry, validated by `src/lib/sources.ts` (zod) and `tests/unit/sources.test.ts`:

| field | type | notes |
|---|---|---|
| `name` | string | canonical publication/author name |
| `homepage` | http(s) URL | main site/blog, not a single article |
| `category` | enum(12) | the `/sources` grouping (see below) |
| `tier` | 1 \| 2 \| 3 | 1 flagship · 2 core · 3 secondary |
| `paywalled` | boolean | true only for a hard paywall |
| `feed` | http(s) URL or null | RSS/Atom, verified to resolve |

**12 categories:** `aggregators, ai_companies, devtools, expert_blogs, hardware_science,
news, newsletters, physics, research, science, security, tech_news`. (These are distinct
from the 6-category *pick* taxonomy in `src/lib/taxonomy.ts` — do not conflate.)

## Tiering rubric

- **Tier 1 — flagship.** The source a domain expert names unprompted; canonical, near-universally
  cited. Keep small: **≤ 12% of the set** (enforced by the test), ≤ ~12 per large category.
- **Tier 2 — core.** Reliably excellent; what a well-read practitioner subscribes to.
- **Tier 3 — secondary.** Good but narrower / more intermittent / niche.

## Category decision rule (first match wins)

AI lab/model org → `ai_companies` · security/threat-intel → `security` · dev-tool vendor/project
eng blog → `devtools` · papers-first journal/preprint/lab → `research` · physics institution/topic
→ `physics`, broad science desk → `science`, silicon/datacenter/robotics → `hardware_science` ·
email-first publication → `newsletters` · link/trending aggregator → `aggregators` · pro trade
press → `tech_news` · general newsdesk → `news` · individual/small-team blog → `expert_blogs`.

## Quality bar for additions

Currently active (a post within ~12 months) · original reporting/analysis/research (no SEO farms,
no aggregators-of-aggregators) · primary sources / renowned experts / canonical publications /
excellent under-the-radar finds · English-publishing.

## Dedup & normalization

- **Canonical URL key** (`canonicalUrl` in `src/lib/sources.ts`): lowercase host, drop scheme,
  leading `www.`, trailing slash, fragment, tracking params; **keep the path**. No two entries may
  share a canonical URL.
- **Normalized name key** (`normalizeName`): no two entries may share one. Resolve collisions by
  keeping the better entry (lowest tier → has feed → more-specific path → shorter name), or rename
  to disambiguate genuinely different sources.
- Precedence when merging duplicates: **lowest tier wins → has feed → more-specific homepage path →
  cleaner name**; `paywalled = OR` of the group.

## Tooling

- `scripts/dedup-sources.mjs` — read-only duplicate reporter (exits non-zero on any dup). Run before
  committing.
- `scripts/feed-health.mjs <in.json> [out.json] [--freshness] [--concurrency=N]` — fetches each
  homepage, **verifies/discovers the RSS/Atom feed**, flags hard-dead hosts (DNS/refused/404/410/5xx;
  403/401/429 are treated as alive-but-bot-blocked → kept), and with `--freshness` records the newest
  feed date. Network-bound; never in CI.
- Tests: `npm run test:unit` (schema + no-dups + tier cap + soft stats check), `npm run build`.

## How to run a curation pass

A "pass" finds the best sources **not already present**, verifies them, and merges them in.

1. **Snapshot the current set.** Treat the committed `src/data/sources.json` as the existing list.
2. **Scavenge (multi-agent).** Use a Workflow fan-out — one agent per category/subdomain plus a
   cross-cutting gap-hunter — to find best-in-class sources beyond what's covered, then a completeness
   critic that names gaps, then a gap-fill round. Each agent returns `{name, homepage, category, tier,
   paywalled, feed, reason}` and must avoid sources already in the set. (See the original workflow
   under `.claude` workflow scripts for the shape.)
3. **Pre-filter** candidates: drop any whose canonical URL or normalized name is already in the set.
4. **Verify + enrich:** `node scripts/feed-health.mjs candidates.json candidates-enriched.json
   --freshness`. Drop `alive:false`. Keep verified `feed`s (null is OK).
5. **Merge:** `node scripts/merge-sources.mjs candidates-enriched.json` — adds survivors, resolves any
   URL/name collisions by precedence, demotes newly-added tier-1 beyond the 12% cap, cleans to the 6
   fields, and sorts by `category → tier → name` (deterministic, idempotent).
6. **Prune (balanced):** remove only reliably hard-dead entries (feed-health `alive:false`). Do NOT
   prune on freshness alone — the freshness signal has false positives (bot-blocked feeds, odd dates),
   and dormant-but-live landmark sources are harmless (the pipeline ranks by recency).
7. **Verify:** `node scripts/dedup-sources.mjs` (0 groups) → `npm run test:unit` → `npm run build`.
8. **Commit + push to `main`** with a message summarizing adds/removes/feed coverage.

Passes are incremental: because each pass commits the merged set, the next pass automatically treats
those as "existing" and looks beyond them. Run several passes per night for breadth, varying the
search angles (geography, language-of-origin, subfields, awesome-lists/blogrolls/OPML).
