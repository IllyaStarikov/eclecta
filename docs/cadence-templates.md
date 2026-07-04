# Eclecta — Cadence Templates & Craft

The shape of each edition, and how to write the parts that repeat. Structure
only; `digest-style.md` governs the sentences and `editorial-policy.md` governs
what goes in. Word counts are the pipeline's own targets (`digest.py`
kind-blocks); treat them as guides, not padding quotas.

## Frontmatter (written by `publish.py`, do not hand-write)

`write_digest_md` renders frontmatter from the DB row:
`title`, `kind`, `period` (quoted), `date` (bare ISO), `blurb`, `items`. When
authoring, you supply `title`, `blurb`, `body_md`, and the cluster ids that set
`items`. Titles and periods are derived — `digest_title()` produces
"Wednesday, June 24, 2026" / "Week of June 15, 2026" / "Q2 2026". Do not put an
H1 in the body; the title field carries it.

## The blurb (standfirst)

One sentence. It is the promise of the edition and the RSS/`<meta>` description.
Name the two or three most consequential facts of the period, comma-joined, no
hype, no "this edition covers." It should read like the deck under an Economist
headline.

- Good: "A new theory recasts prompt injection as role confusion the model can
  be tricked out of; the top open-weight model ships text-only; and a Codex
  logging default writes terabytes to local SSDs."
- Bad: "A look at today's biggest stories in AI and tech." (says nothing)

One em dash at most in the whole edition; the blurb rarely needs one. Use
semicolons to join the parallel clauses.

## Section headers (`##`)

A header names a beat, not a category. It is a short noun phrase or a compressed
claim, sentence case, no trailing punctuation, no glyphs. It should read as a
judgment about the period.

- Good: "Roles aren't trust boundaries", "The price of a zero-click",
  "What the benchmarks miss", "The substrate kept getting cheaper".
- Bad: "AI News", "Security", "Other Updates".

## The lede sentence

The first sentence of the first section carries the whole period. Who did what,
and the size of it, with the primary source linked on first mention. A reader who
stops after it is still correctly informed. Open with the news, never the scene.

---

## Daily — "What matters today?"

300–700 words. 2–4 sections. Lead section = the day's most consequential story.

```
## <the decisive story, as a claim>
<lede: who did what + size, source linked>. <mechanism / the load-bearing
specifics>. <the caveat or the counter-source>.

## <second beat>
<1–2 tight paragraphs>

## <third beat — optional, often "Systems and hardware" or a research cluster>
<shorter; minor items get a sentence each, linked>

## What to watch today
- <concrete, dated, forward hook grounded in today's items>
- <a decision point with an owner or a date>
- <a developing thread to track>
```

Notes: the "What to watch" bullets must be specific and forward-looking — "EU AI
Act GPAI obligations take effect August 2", not "keep an eye on the chip war."
Two to four bullets. A thin news day runs two sections, not padded filler.

## Weekly — "What were the week's through-lines?"

700–1200 words. 3–6 thematic sections. Not a recap of the dailies — a synthesis.
End with Quick Hits.

```
## <the week's dominant theme>
<synthesis: the several stories that share this through-line, woven, each
linked on first mention; the paragraph makes a point the individual stories
didn't>

## <second theme>
...

## <fourth/fifth theme>
...

## Quick hits
- [<title>](<url>) <one sentence: the finding + the caveat> (preprint).
- ...
```

Notes: a theme braids 2–4 related items into one argument. Quick Hits are the
strong tail — one-liners, each linked, each with the load-bearing number and its
review status. Label preprints.

## Monthly — "What developed, and what resolved?"

900–1500 words. Thematic retrospective. Synthesize the weeklies and dailies
(provided as lower-tier bodies). Name 3–5 arcs; trace each across the weeks; say
what closed and what stays open. End with a short "Carried forward" that hands
the open questions to next month.

```
## <the month's defining arc>
<how it started, how it moved week to week, where it stands>

## <second arc>
...

## Carried forward
<2–4 sentences: the unresolved threads and the questions the month hands on>
```

## Quarterly — "What is the trajectory?"

1000–1600 words. Trendlines over events. Synthesize the monthlies. Durable trends
vs. noise; inflection points; positions that aged well or badly. Prefer the
vector to the incident. Same shape as monthly, pitched one level up: sections are
trends, not months; close with what the quarter set in motion.

## Yearly — "What did the year decide?"

1200–2000 words. Synthesize the quarterlies. The defining shifts, the stories
that mattered in hindsight, what conventional wisdom got wrong, the open
questions carried into next year.

---

## Links

Every item links its source inline as a markdown link **on first mention only**.
Use the provided `read_url` (best free read) or `source_url`; never an `archive.*`
link (the pipeline refuses them at generation and publish time). Link the primary
source; a free read is linked when the original is paywalled.

## Length discipline

The word counts are ranges, not targets to hit. A quiet week is a short weekly.
Never inflate to reach a number, never truncate the period's real story to stay
under one. Weight follows consequence — always.
