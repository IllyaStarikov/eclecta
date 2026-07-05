# Eclecta — design language

**Version 2.3.0** — see the [changelog](#changelog) at the bottom.

The one place the look is written down. If a change to `global.css`, a layout,
or a page contradicts something here, either the change is wrong or this doc is
out of date — fix one of them **in the same commit**. That contract is now
enforced: `tests/unit/design-tokens.test.ts` fails when the token tables below
drift from `:root`, and `tests/unit/design-lint.test.ts` fails on off-system
styling in pages. Rationale for visual decisions lives in
[`decisions.md`](decisions.md); this doc states the rules, that one records why.

Eclecta is a **wire service for the frontier**: an automated newswire that reads
thousands of sources across technology, AI, and the sciences and files a dated
edition. The design has to read as a *publication*, not a blog and not a
dashboard. It is deliberately unlike starikov.co (the author's warm-cream
personal site): Eclecta is cool, structural, and built from rules and type.

## 1. First principles

1. **Type and rules do the work — no images.** There is no photography, no
   illustration, no icon font. Hierarchy comes from size, weight, color, and
   hairlines. Charts are CSS/inline-SVG bars, never an image or a chart library.
2. **Sharp corners, everywhere.** `* { border-radius: 0 !important; }` is load
   bearing. The wordmark's period is a square. No pills, no rounded cards.
3. **No glyph separators in the design.** No middots or bullet glyphs as
   separators. Spacing, a register change, or the orange square does the
   separating. Commas in running text and og:titles. (The one allowed mark is
   the square motif and the accent square block.)
4. **Reading first.** The body is a serif at a 66ch measure and 1.6 leading.
   Furniture (kickers, datelines, source tags, counts) is mono and quiet.
   Nothing chrome-like competes with a headline.
5. **Self-hosted, free fonts only.** No CDN, no AI-slop families (no Inter,
   Geist, Fraunces, Instrument Serif, Space Grotesk). See Type.
6. **Light, dark, and print are all first-class.** Never pure black on pure
   white. Dark bumps body weight so serifs don't vanish.

## 2. Tokens

The tables below mirror `:root` in `src/styles/global.css` exactly —
`design-tokens.test.ts` asserts it. Add a token: add it in both places, same
commit, with a role, and bump this doc's minor version.

### 2.1 Color

All ten color tokens are single-sourced with `light-dark()`; the reader/OS
theme is selected via `color-scheme` on `:root` and `html[data-theme]`. There
is no second dark block to keep in sync.

| token | light | dark | role |
|-------|-------|------|------|
| `--ground` | `#f1f2f0` | `#101214` | paper |
| `--ground-2` | `#e9eae7` | `#16181b` | inset panels (signals, chart tracks, previews) |
| `--ink` | `#16181d` | `#ecece8` | body text |
| `--ink-soft` | `#5b5f66` | `#9ca0a6` | secondary text, decks, notes |
| `--ink-faint` | `#63676e` | `#888d96` | tertiary: dates, counts, ordinals — darkened/lifted to clear AA (4.5:1+) at the small furniture sizes it carries |
| `--hairline` | `#d5d7d2` | `#2a2d31` | row rules |
| `--hairline-bold` | `#bfc2bc` | `#3a3e43` | section rules, chip borders |
| `--link-rule` | `rgba(22,24,29,0.30)` | `rgba(236,236,232,0.28)` | resting underline |

**Accent inks** — the reader can re-ink the signal colour (Preferences,
`html[data-accent]`). Each pair is a `light-dark()`; `--accent` and
`--accent-ink` are derivations (`var(--acc-<choice>)` /
`var(--acc-<choice>-ink)`), defaulting to signal:

| pair | light | dark |
|------|-------|------|
| `--acc-signal` / `--acc-signal-ink` | `#e8451f` / `#c63a18` | `#ff6a3d` / `#ff8a63` |
| `--acc-cobalt` / `--acc-cobalt-ink` | `#2251d6` / `#1a41ad` | `#7aa2ff` / `#9db9ff` |
| `--acc-moss` / `--acc-moss-ink` | `#2e7d3f` / `#256633` | `#6fce85` / `#8edd9f` |
| `--acc-plum` / `--acc-plum-ink` | `#9333b8` / `#7b2a9b` | `#cf8af0` / `#dea8f5` |

`--accent` rules are unchanged: the square, bar fills, hover, focus ring;
accent as *text* is always `--accent-ink`.

**Orange is a spice, not a sauce** — the square motif, bar fills, link hover,
focus ring, and the lead accents. Never large fills. Accent that carries text
is always `--accent-ink`. Contrast: `--ink` and `--ink-soft` on both grounds
clear AA for body sizes; `--ink-faint` clears AA-large and is only ever set at
mono furniture sizes with generous tracking; `--accent-ink` clears AA on both
grounds — raw `--accent` never carries text.

Two weight tokens follow the theme in the one remaining dual block (weights are
not colors, so `light-dark()` can't carry them): `--body-wght` 420 light / 430
dark, `--bold-wght` 600 light / 640 dark — dark bumps weight so serif strokes
survive.

### 2.2 Type

| role | family | token | notes |
|------|--------|-------|-------|
| headlines / wordmark | **Schibsted Grotesk** (variable `wght`) | `--sans` | tight tracking, 600–800 |
| reading body | **Source Serif 4** (variable `opsz`) | `--serif` | the only serif; 1.6 leading |
| furniture | **IBM Plex Mono** | `--mono` | kickers, datelines, tags, counts, nav, CTAs |

All three are `@fontsource*`, self-hosted woff2. The reading face is
indirected through `--body-font` (default `var(--serif)`; the reader can swap
it via `html[data-bodyfont]`), and `--measure` derives from `--measure-prose`
so the reader's measure preference retunes the column.

**The scale** — six steps, all fluid `clamp()` with a `rem` term so browser
zoom still scales. One usage rule per step; nothing between steps:

| token | value | used for |
|-------|-------|----------|
| `--display` | `clamp(2.07rem, 1.67rem + 2vw, 3rem)` | the lead pick title — nothing else |
| `--h1` | `clamp(1.73rem, 1.45rem + 1.39vw, 2.4rem)` | page titles (`.section-head h1`, `.article__title`) |
| `--h2` | `clamp(1.44rem, 1.25rem + 0.96vw, 1.8rem)` | prose h2, section-head h2 |
| `--h3` | `clamp(1.2rem, 1.1rem + 0.5vw, 1.45rem)` | pick titles, prose h3 |
| `--body` | `clamp(1.06rem, 0.97rem + 0.45vw, 1.19rem)` | running serif text |
| `--small` | `0.92rem` | notes, summaries, secondary prose |

**Documented display constants** — deliberate one-offs that are *not* scale
members and may not spread: the masthead wordmark and lead-blurb clamps, the
footer wordmark (1.7rem), stat values (2rem), the compact-head wordmark
(1.5rem), spotlight titles (1.06rem), the prose drop-cap, and the 404 numeral (the single sanctioned
display stunt, see §5D).

### 2.3 The mono ladder

Three sizes, two trackings. This is now literally true in the CSS — the lint
test keeps it that way. Don't invent new mono sizes.

| token | value | used for |
|-------|-------|----------|
| `--mono-xs` | `0.68rem` | per-pick meta: ordinal, source, date, tags, score; masthead tag; source flags |
| `--mono-sm` | `0.72rem` | structural: nav, rails, labels, arrow-links, erail, footer, code |
| `--mono-lg` | `0.78rem` | emphasis: rail labels, stat table code |
| `--track-meta` | `0.08em` | lowercase meta — and uppercase set *inside* bordered chips/controls, where the tighter fit is deliberate |
| `--track-cap` | `0.14em` | standalone UPPERCASE structural labels (rails, nav, eyebrows, table heads) |

### 2.4 Spacing & measure

4px scale with named roles — pages reference tokens, never raw margins:

| token | value | role |
|-------|-------|------|
| `--space-1` `--space-2` `--space-3` `--space-4` | 0.25 / 0.5 / 0.75 / 1rem | intra-component gaps |
| `--space-5` | 1.5rem | component ↔ component inside a section |
| `--space-6` | 2rem | page-head offset (`.section-head--page`) |
| `--space-7` | 3rem | section ↔ section |
| `--space-8` | 4rem | footer clearance |
| `--pick-pad` | 1.5rem | briefing row padding (compact density overrides to 0.9rem) |

Measures: `--measure-prose` 66ch (canonical reading column), `--measure-lead`
54ch (leads, standfirsts, decks), `--measure` (66ch fluid), `--maxw` 78rem (the
page frame), `--maxw-read` 66rem (single reading column), `--gutter` fluid.
Leading: `--leading-read` 1.6 (running serif), `--leading-tight` 1.34 (display
blurbs). Center a measure column with `.measure--center`.

### 2.5 Breakpoints

Four, canonical, referenced by comment name at every media query (the lint
test rejects any other width):

| name | width | what changes |
|------|-------|--------------|
| `bp-phone` | 30rem | pref fields and mute grid stack |
| `bp-mobile` | 40rem | bars stack, archive rows stack, dateline slims, 44px touch targets |
| `bp-column` | 52rem | preferences layout stacks |
| `bp-rail` | 60rem | broadsheet collapses to one column; erail drops its index/nav and goes static |

## 3. Furniture & glyphs

**The square is the mark.** The orange square — the wordmark's period, the
favicon, the tombstone — is Eclecta's entire logo system; there is no other
logotype. It appears where structure begins (rail labels, erail heads) and
where a piece ends (the tombstone), and that restraint is the point: don't
add instances. Its canonical expressions, in order of ceremony: the wordmark
period `ECLECTA▪`, the favicon `E▪`, the **tombstone** closing every filed
piece (the Economist-school QED square), the **reading marker** (the one
kinetic square: it glides down the front page's section index as the reader
scrolls — the mark keeping their place), and the small structural squares on
band labels.

The complete glyph vocabulary. Anything not listed here is banned by
principle 3.

| mark | implementation | where it may appear |
|------|----------------|---------------------|
| ▪ square | **CSS block element only** (an inline-block with `background: var(--accent)`, or a 2px accent left border for panels) — never the text glyph | wordmark period, favicon, rail labels, erail heads, chip dots, prose list markers, pick source prefix, bar leaders |
| ▪ tombstone | `.prose > p:last-child::after` — fires only when a paragraph closes the piece | the end of every filed article (digests, about); exactly one per page |
| — dash | text glyph, `--accent` | *annotation* lists only (pick notes — terse editorial asides). **Never in chrome copy**: reader-facing strings pair with the `|` separator or recast with commas (lint-enforced); the em-dash register belongs to annotations alone |
| ▪ block marker | CSS block | *content* lists (prose body lists). The dash and the block are two intentional list registers: annotation vs content |
| → / ← | text glyph inside `.arrow-link` (aria-hidden span) | arrow-links only — never a bare arrow in running copy |
| + / – | `::before` content on `details > summary` | collapsible details toggles |
| ◐ | `::before` on the theme toggle | the theme toggle only |

## 4. Component catalog

The complete inventory. Reuse before inventing; a new component needs a
catalog entry, a `decisions.md` rationale, and a minor version bump here.

| component | purpose | variants | pages |
|-----------|---------|----------|-------|
| `.masthead` | nameplate band: wordmark, kicker, 3px rule | — | archetype A + B |
| `.dateline` | the "as of" wire band: date left; right, the standing claim `No. <editions published> · Open source →` (a real, incrementing issue number and the repo link) | — | archetype A + B only — **the dateline is a claim** (§5) |
| `.compact-head` | small nameplate for dated/undated articles | `+ edition stamp` | archetype C + D |
| `.channels` | section nav, aria-current wayfinding | — | A + B |
| `.toggle` | theme cycle button | — | all |
| `.rail` | in-column band header: `▪ LABEL ──── count/more` | with `.rail__count`, with `.arrow-link`; `--sub` (quiet, no ▪ — groupings inside a fold) | everywhere a band starts |
| `.label` | uppercase mono eyebrow | `--accent`, `--faint` | article kickers, group heads, form legends |
| `.count` | tabular count chip beside a head | — | section heads, prefs |
| `.arrow-link` | THE navigational link: mono caps + →/← , gap grows on hover | `--faint` | rails, erail, article backs, digest nav |
| `.chip` | bordered mono tag | `+ .chip__dot` (feed), `aria-current` | subcategory chips, feed chips |
| `.deck` | one-line page intro under a page title (roman, `--ink-soft`) | — | every B-archetype page |
| `.section-head` | page/section title row | `--page` (adds the page-head offset) | B pages, archive groups |
| `.pick` | the briefing row: meta, novelty kicker (lead), developing tag (marked case), headline, why, details, signals | `--lead`, `--brief`; `.also__item` is the bare tail | A pages, preferences preview |
| `.briefing` | a bordered stack of picks | — | A pages, archive |
| `.also` | compact headline+source tail of a section | — | A pages |
| `.spot` | the Spotlight traction wire: headline + spaced-mono traction line, curated entries add why + full-pick arrow-link | — | front page, when spotlight.json has items |
| `.arch-month` | native details fold for the archive's earlier editions: mono summary, +/- cue, count; each cadence shows a fixed recent window (7/4/3/4/5) in the open, the rest folds (dailies grouped by month inside) | — | archive |
| `.subchips` | chip row of subcategories with counts | — | category pages |
| `.crumb` | ← parent breadcrumb above a title | — | subcategory pages (their one back affordance) |
| `.empty-state` | "nothing here right now" panel — always a `<div>` of `<p>`s | — | category/sub pages |
| `.erail` | the broadsheet right rail: index (with the JS-built `.erail__marker` reading square), nav, the editions ledger (latest of every cadence), subscribe, back | per-block | A pages |
| `.bars` / `.bar` | CSS bar chart, one accent leader | `--lead` | coverage |
| `.stat-grid` / `.stat` | headline stat cards | — | coverage, stats (card sets intentionally disjoint: transparency vs ops) |
| `table.bare` | mono-headed data table | — | stats |
| `.article` | long-form column: head, kind/serial, title, standfirst, prose | `.article__foot` colophon | digests, about, contact |
| `.prose` | reading body: serif, drop-cap, block-marker lists | — | digests, about |
| `.digest-nav` | prev/next sibling nav (arrow-links) | — | digests |
| `.foot` | the colophon grid: identity column (wordmark, mission, stamp + issue no.) plus three single-axis link columns — Read / The wire / Made by (open source, model credit) | `.foot__id`, `.foot__col` | all |
| `.prefs` controls | `.seg` radio rows, `.pref-toggle`, `.mute-grid` | — | preferences |
| `.skip` / `.sr-only` | a11y: skip link, visually-hidden text | — | all |
| `.nf` | the 404 stunt numeral + line + links | — | 404 only (sanctioned bespoke, `design-lint-allow`) |

## 5. Page archetypes

Every page is exactly one of these. The axis for headers: **the dateline is a
claim** — "this page reflects the wire as of today, the `No.` counts every
edition ever published, and the source is open." Pages that are views of
the current wire may make it; pages carrying their own date (a digest) or no
date (colophon pages) may not.

- **A. Broadsheet edition** — front, category, subcategory. Full masthead +
  dateline + nav; `wide`; main column + `.erail`. Back affordance: the erail
  (front has none; category → front; sub → its `.crumb`).
- **B. Wire index** — archive, feeds, sources, coverage, stats, preferences.
  Full masthead + dateline + nav; single `.column`; `.section-head--page` +
  `.deck`. No back link — these are top-level, the masthead nav is the way
  back.
- **C. Article** — digests (compact head **+ edition stamp**), about, contact
  (compact head, no stamp). Centered measure column (`.measure--center`).
  Exactly one `.arrow-link` back to the IA parent (digest → Archive; about /
  contact → Front page). Digests end with an `.article__foot` colophon: kind
  feed + all feeds (and, when the pipeline emits it, the model attribution).
- **D. Utility** — 404. Compact head; system primitives; the numeral is the
  one sanctioned display stunt.

## 6. Interaction states

Written down so they stop drifting. Timing budget: 0.15–0.25s, `ease`.

- **Arrow-links**: resting `--ink-soft` (`--faint` variant `--ink-faint`),
  gap `0.4rem` → hover `0.7rem` + `--accent` text.
- **Chips**: border + text flip to `--accent` on hover; `aria-current` chips
  carry `--accent-ink` text + `--accent` border at rest.
- **Titles**: resting underline `--link-rule` 1px → hover `--accent` 2px +
  `--accent-ink` text. Leads and the wordmark instead sweep an accent
  underline left-to-right (background-size transition).
- **Details summaries**: `+` → `–`, hover `--accent`.
- **Focus**: always `:focus-visible { outline: 2px solid var(--accent);
  outline-offset: 2px }` — never removed, never restyled per-component.
- **Target**: `:target` picks get a 2px accent left rule (anchor arrivals).
- **Reveal**: one staggered fade on load, 55ms steps, capped at 8 steps,
  killed under `prefers-reduced-motion: reduce`.
- **Reading marker**: `transform` glide 0.22s `cubic-bezier(0.2,0.7,0.2,1)`
  down the section index; `transition: none` under reduced motion (it jumps);
  without JS it never renders. Active index entries carry
  `aria-current="location"` + `--ink` at weight 600.

## 7. Contracts

- **Dark** — token-level only: `light-dark()` + the weight dual block. No
  component may hardcode a theme color; if a component needs a dark exception,
  that's a new token with a role, not a local override.
- **Print** — hides chrome (`.channels`, `.toggle`, `.erail`, `.foot`,
  `.dateline`, signals, details, nav); broadsheet collapses to one column;
  serif on white with `(href)` printed after content links; `@page` margin
  1.6cm; `color-scheme: light` forced.
- **Responsive** — the four breakpoints in §2.5 and nothing else. Touch
  targets ≥ 44px at `bp-mobile`. The erail is furniture: it drops before the
  reading column ever compresses.
- **Preferences** — reader prefs are `data-*` attributes on `<html>`, stamped
  pre-paint; every preference state is part of the capture matrix and must
  look intentional (compact, muted, signals, xl, forced-dark).

## 8. Governance

1. **A visual change is CSS + this doc in the same commit**, with a version
   bump: **patch** = doc clarification; **minor** = new component/variant or
   token; **major** = token removal, archetype change, or principle change.
2. Anything with a "why" (a trade-off, a taste call) also gets a
   [`decisions.md`](decisions.md) entry — Context → Decision → Why.
3. **The review loop is the capture harness**: `npm run capture` before and
   after, `npm run shotdiff -- <before> <after>`, and the changed-file list
   goes in the commit body. The newest post-merge run on main is the standing
   golden.
4. **The gates**: `design-tokens.test.ts` (doc ↔ `:root` sync) and
   `design-lint.test.ts` (no inline styles in pages, no off-token type/color
   in page styles, canonical breakpoints only, no em-dash in chrome copy)
   run in CI with the unit suite.

## 9. Taxonomy

Six top categories — **AI, Research, Software, Security, Hardware, Industry** —
each with subcategories (`src/lib/taxonomy.ts`). A pick has one *primary*
category (drives sectioning and routing) plus cross-cutting subcategories.
`resolveCategory()` trusts the pipeline's emitted category and falls back to
the title lexicon. The lexicon mirrors `signalpipe/topics.py`.

## 10. When you add something

- New page: pick its archetype in §5 first; that decides header, deck, back.
- New band: `.rail` introduces it; new link: `.arrow-link`; new tag: `.chip`;
  new eyebrow: `.label`. If none fit, it's a new component → catalog entry +
  decision + minor bump.
- New furniture: mono, one of the three sizes, both trackings per the rule.
- Re-capture and shotdiff light + dark + mobile before committing. Run
  `npm run check && npm test`.

## Changelog

| version | date | change |
|---------|------|--------|
| 2.3.0 | 2026-07-05 | The open-wire revision: the front page opens with the lead (standfirst retired — the identity lives in the chrome); the dateline claims a real issue number and links the source; the footer becomes the colophon grid (identity + Read / The wire / Made by); the rail merges the brief into one editions ledger (latest of every cadence) and its section index gains the reading marker (the kinetic ▪, scrollspy); the archive gives every cadence a fixed recent window with earlier editions folded; feed titles adopt the `|` separator and em-dashes are banned from chrome copy (lint-enforced). |
| 2.2.0 | 2026-07-05 | Absorbed the reader personalization tokens into the single-source scheme: `--acc-*` accent pairs converted to `light-dark()` (one dark block stays dead), `--accent`/`--accent-ink` documented as derivations, `--body-font` + retunable measure documented. Preferences/about rewrites reconciled onto system classes. |
| 2.1.0 | 2026-07-05 | Spotlight (`.spot`) + archive month folds (`.arch-month`) + editorial registers on picks (novelty kicker, developing tag, audience in signals) + digest colophon feeds/provenance; `--ink-faint` darkened to #63676e and accent-as-text moved to `--accent-ink` (nav current, pay tag) after the new axe gate flagged them; share cards generate at build (link-preview furniture, not site imagery); per-page feed autodiscovery. |
| 2.0.0 | 2026-07-04 | Systematization: single-sourced dark (`light-dark()`), mono ladder made true, primitives consolidated (`.arrow-link`, `.chip`, `.label` ladder, `.briefing`), pages de-inlined, print contract fixed (erail), canonical breakpoints, page archetypes + "the dateline is a claim" masthead rule, component catalog, interaction-state spec, governance + lint/sync tests. |
| 1.x | 2026-06 | Original wire-service language: principles, fonts, colors, page compositions. |
