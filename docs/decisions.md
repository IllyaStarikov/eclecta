# Decisions

Short records of the choices behind the current shape of Eclecta, newest first.
Each is "context → decision → why", so a future change knows what it's undoing.

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
