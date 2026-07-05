# Eclecta — design language

The one place the look is written down. If a change to `global.css`, a layout,
or a page contradicts something here, either the change is wrong or this doc is
out of date — fix one of them in the same commit.

Eclecta is a **wire service for the frontier**: an automated newswire that reads
thousands of sources across technology, AI, and the sciences and files a dated
edition. The design has to read as a *publication*, not a blog and not a
dashboard. It is deliberately unlike starikov.co (the author's warm-cream
personal site): Eclecta is cool, structural, and built from rules and type.

## First principles

1. **Type and rules do the work — no images.** There is no photography, no
   illustration, no icon font. Hierarchy comes from size, weight, color, and
   hairlines. Charts are CSS/inline-SVG bars, never an image or a chart library.
2. **Sharp corners, everywhere.** `* { border-radius: 0 !important; }` is load
   bearing. The wordmark's period is a square. No pills, no rounded cards.
3. **No glyph separators in the design.** No `·` middots or bullet glyphs as
   separators. Spacing, a register change, or the orange square does the
   separating. Commas in running text and og:titles. (The one allowed mark is
   the square `▪` motif and the accent square block.)
4. **Reading first.** The body is a serif at a 66ch measure and 1.6 leading.
   Furniture (kickers, datelines, source tags, counts) is mono and quiet.
   Nothing chrome-like competes with a headline.
5. **Self-hosted, free fonts only.** No CDN, no AI-slop families (no Inter,
   Geist, Fraunces, Instrument Serif, Space Grotesk). See Type.
6. **Light, dark, and print are all first-class.** Never pure black on pure
   white. Dark bumps body weight so serifs don't vanish.

## Type

| role | family | notes |
|------|--------|-------|
| headlines / wordmark | **Schibsted Grotesk** (variable `wght`) | tight tracking, 700–800 |
| reading body | **Source Serif 4** (variable `opsz`) | the only serif; 1.6 leading |
| furniture / mono | **IBM Plex Mono** | kickers, datelines, source tags, counts, nav, CTAs |

All three are `@fontsource*`, self-hosted woff2. The fluid scale lives in
`:root` (`--display`, `--h1`…`--body`, `--small`); every `clamp()` keeps a `rem`
term so browser zoom still scales. Mono furniture is intentionally small and
restrained: three sizes (`--mono-xs/-sm/-lg`) and two trackings (`--track-meta`
lowercase, `--track-cap` uppercase). Don't invent new mono sizes.

## Color

Cool oat-grey ground, near-black ink, one signal-orange accent. Tokens (light;
dark overrides mirror them under `@media (prefers-color-scheme: dark)` and
`html[data-theme="dark"]`):

| token | light | meaning |
|-------|-------|---------|
| `--ground` | `#f1f2f0` | paper |
| `--ground-2` | `#e9eae7` | inset panels (brief panel, chart tracks, signals) |
| `--ink` | `#16181d` | body |
| `--ink-soft` | `#5b5f66` | secondary |
| `--ink-faint` | `#757980` | dates, counts, tertiary — **darkened to clear AA** |
| `--hairline` / `--hairline-bold` | `#d5d7d2` / `#bfc2bc` | rules |
| `--accent` | `#e8451f` | the square, bar fills, hover, focus |
| `--accent-ink` | `#c63a18` | accent used as *text* (contrast-safe) |

Rule of thumb: **orange is a spice, not a sauce** — the square motif, bar fills,
link hover, focus ring, and the lead/serial accents. Never large fills of it.
Use `--accent-ink` whenever the accent carries text.

## Structural system

The page is a stack of labelled bands, each introduced by a **rail**:
`▪ LABEL ───────────── count`. The orange square prefixes every rail label and
is the recurring identity mark (it's also the wordmark period and the feed dot).

- **Masthead** (`Base.astro`): wordmark `ECLECTA▪`, a mono kicker + tagline, then a
  **dateline band** (`weekday, month day, year · Automated edition · N sources`)
  under a 3px nameplate rule, then the section nav. The dateline is what makes
  every view read as a *dated edition*.
- **Front page = today's edition** (`index.astro`): standfirst → **Today's lead**
  (one pick, `variant="lead"`, categorically larger) → **the brief panel**
  (ground-2 box linking the latest daily prose digest) → **category sections**
  (busiest first, ~4 picks each, "more in X →") → **Editions** index → Subscribe.
- **Picks** (`Pick.astro`): meta row (ordinal · source · date · category +
  subcategory tags · score) → headline link → why → collapsible details →
  signals (opt-in). `lead` is the big variant; `brief` is the row; `also` is a
  bare headline+source.
- **Category / subcategory pages**: section head, blurb, subcategory chips with
  counts, a feed chip, then lead + brief rows.
- **Coverage** (`/coverage/`): the transparency dashboard, a stack of
  rail-labelled bands rendered as build-time SVG/CSS (never a chart
  library): stat cards with 7-day deltas, the 90-day wire chart with its
  curated strip, the log-scale funnel, source bars and tier/echo strips,
  configured-vs-observed model provenance, the relevance histogram, the
  7x24 ingest heatmap, and the editions calendar. Chart primitives live in
  global.css (`.wire-chart`, `.funnel`, `.strip`, `.hist`, `.heatmap`,
  `.editions`); geometry in `src/lib/coverage.ts`. One accent lead per
  chart. `/stats/` redirects here. **Sources** (`/sources/`): the full
  feed roll, grouped.
- **Footer**: bookends the masthead — wordmark, one-line colophon, link row.

## Spacing & measure

4px scale (`--space-1`…`--space-8`). Reading column `--measure-prose: 66ch`;
leads/standfirsts `--measure-lead: 54ch`. Section gaps are `--space-7`. Don't
hardcode pixel margins; reference tokens.

## Motion & a11y

- One quiet staggered reveal on load (`.reveal`, 55ms steps), fully disabled
  under `prefers-reduced-motion: reduce`.
- Focus is always visible: `:focus-visible { outline: 2px solid var(--accent) }`.
- Reader preferences are data-attributes on `<html>` stamped pre-paint
  (theme / fontsize / density / showscores / showsignals / muted). They never
  cause layout shift and degrade to sensible defaults with JS off.
- Contrast: body and furniture clear WCAG AA in both themes; `--ink-faint` was
  darkened specifically because it carries functional text (dates, counts).

## Taxonomy

Six top categories — **AI, Research, Software, Security, Hardware, Industry** —
each with subcategories (`src/lib/taxonomy.ts`). A pick has one *primary*
category (drives sectioning and routing) plus cross-cutting subcategories. The
lexicon there is the source of truth and mirrors `signalpipe/topics.py`.

## When you add something

- New page: lead with a `section-head` or a `rail`; set the reading width with
  `.measure`/`--measure-prose`; reuse `.pick`, `.rail`, `.bars`, `.feed-chip`.
- New furniture: mono, one of the three sizes, `--ink-soft`/`--ink-faint`.
- Re-capture (`npm run capture`) and eyeball light + dark + mobile before
  committing. Run `npm run check && npm test`.
