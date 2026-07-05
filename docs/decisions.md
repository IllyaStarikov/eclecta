# Decisions

Short records of the choices behind the current shape of Eclecta, newest first.
Each is "context → decision → why", so a future change knows what it's undoing.
Visual-system decisions belong here too — `design-language.md` states the
rules; this file records their rationale.

## 2026-07-04 — "The dateline is a claim": masthead assignment by archetype

**Context.** Header choice had drifted page-by-page: dated digests got the
*compact* header while undated about/contact got the full masthead with the
"as of today" dateline band — exactly backwards, and there was no written rule
to appeal to.
**Decision.** The dateline asserts "this page reflects the wire as of today."
Pages that are views of the current wire (front, categories, subcategories,
archive, feeds, sources, coverage, stats, preferences) carry the full masthead
and dateline. Pages that carry their own date (digests — compact head +
edition stamp) or no date (about, contact, 404) get the compact head. Every
page belongs to exactly one archetype (design-language.md §5).
**Why.** A dateline over a June 8 digest is a false statement. Making the
dateline a *claim* gives a principled, testable axis instead of a per-page
taste call, and it survives new pages: ask "does this page assert today's
wire?" and the header follows.

## 2026-07-04 — Design system v2: tokens made true, primitives consolidated

**Context.** The written language claimed "three mono sizes, two trackings"
while the CSS had five-plus of each; dark mode was hand-duplicated in two
blocks with a "keep in sync" comment; five near-identical arrow-link classes,
two chip classes, and eight eyebrow variants had accreted; six pages inlined
the same `padding-top`; print hid a class that no longer existed, so the rail
printed as a stray column.
**Decision.** Single-source dark via `light-dark()`; collapse the near-
duplicates into `.arrow-link`, `.chip`, and the `.label` ladder; make the mono
ladder literally true; canonicalize breakpoints to {30, 40, 52, 60}rem; move
every repeated inline pattern into the system; enforce all of it with
`design-tokens.test.ts` (doc ↔ `:root` sync) and `design-lint.test.ts`
(no off-system styling in pages), with the capture harness + `shotdiff` as the
visual review loop.
**Why.** A design language you can refine over time needs the doc, the CSS,
and the pages to agree *by construction*, not by discipline. Consolidation is
what makes future changes one-line: a hover tweak edits one primitive, not
five forks of it. The tests turn drift from a slow leak into a failing build.

## 2026-06-13 — Front page is a composed "today's edition", not the daily prose

**Context.** The front page was a flat wall of ~18 identical picks plus a digest
hero. The owner wanted it to *be* a daily edition, refreshed ~2×/day.
**Decision.** Compose the front page from the current confident picks: a lead
story, a daily-brief panel that links the prose digest, then picks grouped into
category sections (busiest first), an editions index, and subscribe.
**Why.** Picks refresh every ~4h; the daily prose digest regenerates only ~5×/
week. Composing from picks keeps the front page genuinely fresh and dated, and
leaves the prose digests to their own pages. The masthead dateline supplies the
"as of" so the page reads as an edition.

## 2026-06-13 — Category→subcategory taxonomy, derived on the front end (for now)

**Context.** The taxonomy was a flat list of 8 "channels"; `news` was even dead
(referenced by the site but never tagged). The owner wanted real categories with
subcategories and "the best data API — no backwards compatibility."
**Decision.** Six top categories (AI, Research, Software, Security, Hardware,
Industry) with 25 subcategories, in `src/lib/taxonomy.ts`. Each pick gets one
primary category + cross-cutting subcategories. For now the front end *derives*
them from the pick's title + legacy `channels[]`; the pipeline will emit them
natively next (same lexicon, in `topics.py`).
**Why.** Deriving on the front end let the whole redesign ship and go live
reading the *existing* pipeline output — no risky data-contract cutover needed
on an unattended night. The derive function is written to be a fallback the
moment the pipeline emits `category`/`subcategories`, so there's no throwaway.
One *primary* category (vs. multi-tag) is what makes "which section leads this
story" well-defined for the edition layout.

## 2026-06-13 — Remove the per-pick +/- votes

**Context.** Every pick carried thumbs that wrote a device-local vote; the owner
didn't believe they did anything.
**Decision.** Remove them entirely (markup, CSS, the `prefs.js` handlers, the
e2e). Replace the one useful affordance (hide-downvoted) with a far more useful
device-local **mute-categories** preference.
**Why.** The thumbs added visual noise to every row for a feature with no
server side and little reader value. Muting whole sections is the control people
actually want from a sectioned edition.

## 2026-06-13 — Preferences rebuilt around legibility + a live preview

**Context.** The prefs page was three cramped fieldsets with near-invisible
segmented controls; the owner called it "hard to read and weird."
**Decision.** Grouped Appearance / Detail / Sections with generous rhythm,
square sliding toggles (ink fill = on, matching the segmented control), inline
hints, and a **live preview pick** that reacts to the settings as you change
them.
**Why.** The preview makes each setting's effect legible without leaving the
page, which was the core complaint.

## 2026-07-05 — The identity moves into the chrome; the front page opens with news

**Context.** The front page led with a four-line standfirst explaining what
Eclecta is, above the lead story. The owner: "i hate this always being on the
homepage." Meanwhile the dateline said "Automated edition" on every page — a
label that informs no one, since every edition is automated.
**Decision.** Retire the standfirst; the identity now lives in the site
furniture. The dateline carries the real claim: `No. <n> · Open source`, where
n counts every edition ever published (a true, incrementing issue number) and
the link goes to the repository. The footer becomes a colophon grid — identity
column plus Read / The wire / Made by — and "Made by" names the terms: open
source under MIT, written by Claude models, run by one person.
**Why.** A newspaper doesn't print its mission statement above the lead; its
identity is in the nameplate, the dateline, the colophon. "Automated edition"
was an apology. "No. 42 · Open source" is a boast, and both halves are
verifiable — the number increments with the archive and the link shows the code.

## 2026-07-05 — The mark becomes the reading marker (scrollspy)

**Context.** The rail's "In this edition" index was static; the owner wanted
the dot to "shoot down" the index as the reader scrolls. Separately, the rail
carried a "Latest brief" block *and* an "Editions" block — two blocks doing one
job — and the index's scaling was an open question.
**Decision.** One kinetic ▪: a JS-built `.erail__marker` glides down the index,
tracking the topmost section past the reading band (top 40% of the viewport),
with `aria-current="location"` on the active entry. The brief merges into a
single editions ledger — the latest of every cadence, daily first. Scaling is
bounded by design (the index can never exceed the six fixed categories +
Spotlight) and belt-and-braces by an overflow guard on the sticky rail.
**Why.** The square is Eclecta's whole logo system, and this is its one
allowed motion: it keeps the reader's place, which is furniture, not
decoration. Without JS the index is a plain anchor list — the enhancement
degrades to exactly the old behavior.

## 2026-07-05 — Archive: equal standing per cadence

**Context.** 31 dailies vs 8 weeklies vs 2 monthlies: the archive read as a
wall of daily briefs with the long views buried at the bottom.
**Decision.** Every cadence shows a fixed recent window in the open — 7
dailies, 4 weeklies, 3 monthlies, 4 quarterlies, 5 yearlies (each roughly the
cadence's natural span: a week of days, a month of weeks, a quarter of months,
a year of quarters) — and the remainder folds into one closed "Earlier" details
per kind, dailies grouped by month inside.
**Why.** The archive's job is "find the edition I remember," and recency
dominates that search. Fixed windows keep the five cadences visually equal
forever, however lopsided their counts grow.

## Planned — pipeline v2: native taxonomy, cross-edition dedup, RSS-on-confident

**Context.** Cross-edition de-duplication, retro-tagging old picks, a 2×/day
edition cadence, and "publish to RSS as soon as a story is confident" are all
pipeline concerns; `signalpipe` is a live launchd worker (sole DB writer).
**Decision (staged).** Add a durable `published_ledger` (db schema v5) keyed on
a stable `story_id`, a 2-level `TAXONOMY` + `match_taxonomy` in `topics.py`, a
`retag` backfill, a per-cadence dedup filter in `digest.py`, and v2
`export_picks/stats/edition/sources`. Build and validate on a DB **copy**, then
flip behind a `--no-push` dry-run when the worker can be synced under
supervision.
**Why.** The published output flips shape only once, atomically, with the worker
on the new code — never a half-migrated live site. The front end already derives
the taxonomy, so the site doesn't depend on this flip; it adds dedup, stable RSS
`published_at`, and LLM-assisted categories on top.
