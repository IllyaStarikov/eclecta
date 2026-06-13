# Lede

A digital broadsheet for the frontier — the best of technology, AI, and the
sciences, curated continuously and distilled on five cadences: a daily brief
on weekday mornings, a weekly digest on Fridays, and monthly / quarterly /
yearly reviews. Static site (Astro 5), deployed to GitHub Pages.

**We read the firehose, so you read the lede.**

## How it works

The public face of a local curation pipeline (`signalpipe`, in a separate
repo) that:

1. ingests thousands of verified sources (RSS + HN/Lobsters/Reddit/arXiv/
   Mastodon trends/Google News/Wikipedia current events/GDELT and more),
2. canonicalizes and clusters them — one story, many surfaces,
3. scores deterministically (consensus × engagement × reputation × recency ×
   topic fit),
4. has Claude (Sonnet) read the day's finalists closely and explain what
   matters, and Claude (Opus, high effort) write the digests under an
   Economist-school style guide,
5. writes its output INTO this repo and pushes — GitHub Actions builds and
   deploys. The site never depends on the pipeline being online.

## What the pipeline writes

- `src/content/digests/<kind>/<period>.md` — digests (daily | weekly |
  monthly | quarterly | yearly), frontmatter: title/kind/period/date/blurb
- `src/data/picks.json` — current curated picks (why / notes / summary /
  channels / primary `source_url` + free `read_url` / surfaces)
- `src/data/stats.json` — pipeline numbers for /stats/
- `kb/` — **unpublished** knowledge base: `kb/days/YYYY-MM-DD.md` ledgers and
  `kb/trends.md`. Text and links only; excluded from the build.

`src/data/channels.json` is site-owned (editorial blurbs).

## Reader features

- Front page is a dated **today's edition**: a lead story, a daily-brief panel,
  picks grouped into category sections, an editions index.
- Reading-first picks: headline → why → notes → summary. Primary source
  linked first, always; a free read linked when the original is paywalled.
- Six-section taxonomy (AI, Research, Software, Security, Hardware, Industry)
  with subcategories — pages at `/<category>/` and `/<category>/<sub>/`.
- `/coverage/` — transparency dashboard (sources by category/tier, loudest
  surfaces). `/sources/` — the full feed roll.
- `/preferences/` — theme (auto/light/dark), type size, density, the
  curation-signals panel, inline scores, and muting whole sections. All
  device-local (localStorage); nothing leaves the browser.
- Feeds for everything: `/rss.xml`, `/digests/rss.xml`, per-kind
  `/digests/<kind>/rss.xml`, per-category `/<category>/rss.xml`. Directory at
  `/feeds/`.

## Develop

```bash
npm install
npm run dev        # http://localhost:4321/lede/
npm run build      # -> dist/
npm run check      # astro check
npm run test:unit  # vitest (data contracts, feed lib)
npm run test:e2e   # Playwright (pages, prefs, feeds)
npm run capture    # screenshot every page × light/dark × 3 viewports
```

## Deploy

GitHub Actions (`.github/workflows/deploy.yml`): test → build → Pages.
Site/base are env-driven (`LEDE_SITE` / `LEDE_BASE`): project pages
(`illyastarikov.github.io/lede`) today; flipping to a custom subdomain is a
one-variable change plus `public/CNAME`.

## Renaming the publication

The name lives in `src/site.ts` (masthead, feeds, meta) and the pipeline's
`config/signal.json` (`site.name`). Change those two, the repo name, and
`public/CNAME` when the domain lands.

## Structure

```
src/
  site.ts              identity + base-aware href helpers (THE config spot)
  content/digests/     <kind>/<period>.md   (pipeline-written)
  data/                picks.json, stats.json (pipeline-written); channels.json
  layouts/Base.astro   masthead/dateline/nav/footer, pre-paint prefs stamp, meta
  components/Pick.astro reading-first pick + hidden signals panel
  scripts/prefs.js     device-local preferences runtime
  lib/                 taxonomy.ts (categories + derive), feeds.ts (registry +
                       item HTML), schema.ts (zod)
  pages/               index (today's edition), digests/[...slug],
                       [category]/ + [category]/[sub]/, coverage, sources,
                       archive, about, feeds, preferences, stats, contact, 404
  styles/global.css    the editorial stylesheet (light+dark, print, a11y)
docs/                  design-language.md, decisions.md
kb/                    unpublished knowledge base (pipeline-written)
scripts/capture.mjs    screenshot harness
tests/                 unit (vitest) + e2e (Playwright)
```

## Design

A wire service for the frontier: **Schibsted Grotesk** (headlines), **Source
Serif 4** (reading), **IBM Plex Mono** (datelines, source tags, furniture).
Cool oat-grey ground, one signal-orange accent, hairline rules, sharp corners,
light + dark + print. Type and rules do all the work — no images. The full
system is written down in [`docs/design-language.md`](docs/design-language.md).
