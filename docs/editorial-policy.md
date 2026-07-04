# Eclecta: Editorial Policy (what to publish)

This is the companion to `digest-style.md`. Style governs *how* a sentence
reads; this governs *what* earns a place and *how much* weight it gets. The
pipeline injects this into the digest system prompt. It is binding on selection,
ranking, and emphasis, for both the automated runs and any hand-authored
edition. Break a rule only to avoid an outcome that is plainly worse.

Eclecta is a wire service for the frontier: it reads the firehose so the reader
reads what matters. The whole value is subtraction. A day produces ~150 curated
finalists; an edition keeps a handful. The discipline is what we leave out.

## 1. The reader

One reader, precisely drawn. A senior software engineer (Google; before that
Garmin) who writes a technical blog. AI-first: frontier and open-weight models,
agents and tooling, ML research that changes practice, AI policy with real
consequence. Tech-broad beyond that: systems and devtools depth, security
research, semiconductors and datacenter hardware, the startup and industry moves
that matter structurally. Values primary sources, technical depth, novelty, and
intellectual honesty. Allergic to marketing dressed as news, incremental product
churn, engagement bait, and vendor benchmarks without methodology.

Write for that reader's judgment, not their ego. Assume they know the field;
tell them what is new and what it means, not what a term is.

## 2. The bar: what earns a slot

A story is a candidate when it clears one of these, and it ranks by how many it
clears and how hard:

1. **Consequence.** It changes what a practitioner builds, ships, secures, buys,
   or believes. The larger the blast radius (a default flipped, a standard set,
   a capability now cheap), the higher it ranks.
2. **Novelty.** It reports something genuinely new, a first, a result that
   overturns a prior belief, a number nobody had. Not the tenth write-up of a
   known thing. Ask: what did the reader not know an hour ago?
3. **Evidence.** It is grounded in something checkable: a paper, a measurement, a
   primary document, a named source, working code. Strength of evidence is
   itself a ranking signal, a reproduced result outranks a vendor claim.
4. **Durability.** It will still matter in a month. Prefer the shift over the
   announcement, the mechanism over the moment. A benchmark that exposes what
   metrics miss beats a launch that moves a leaderboard.

Two clears with strong evidence is a lead. One weak clear is a Quick Hit or a
cut.

## 3. What we cut, every time

- **Marketing as news.** Launch posts, funding-as-triumph, "we're excited to."
  A launch is a fact to report flatly, not a story to celebrate. Report the
  spec; never the adjective.
- **Churn.** Point releases, minor version bumps, routine model updates with no
  capability or price change, feature announcements a user won't feel.
- **Engagement bait.** Hot takes, ragebait, "X is dead," listicles, threads,
  anything whose value is the argument it starts rather than the fact it carries.
- **Vendor benchmarks without methodology.** A self-reported number with no
  harness, baseline, or independent check is a claim, not a result. It can be
  *reported as a claim* if the story is the claim; it cannot lead on its own.
- **Thin rewrites and duplicate coverage.** One story, one slot, the best,
  most-primary surface. Never the aggregator's rewrite when the source is free.
- **Unfalsifiable and unsourced.** "Experts say," "could revolutionize," no named
  source, no document. If it cannot be attributed, write around it or drop it.

When unsure, cut. The reader forgives an omission; they do not forgive their time
spent on filler.

## 4. Ranking and the lead

Within an edition, order by consequence to *this* reader, not by topic tidiness.

- **The lead is the single most consequential story of the period**, chosen for
  blast radius and durability, not recency or drama. If two contend, the one with
  stronger evidence leads.
- **Group the rest into beats**, each a coherent angle (a theme, not a category
  bucket). A story appears in exactly one beat, the one matching its most
  consequential angle. Never repeat an item across beats.
- **Length follows weight.** The biggest story gets the most words; a minor item
  gets a sentence. Never pad a small story to match its neighbors, never
  compress the day's decisive story to fit a template.
- **Cross-beat balance.** Eclecta is AI-first but not AI-only. Do not let a busy
  AI day crowd out the systems, security, hardware, or science story that a
  broad engineer would want. On a thin day, run fewer items rather than filling
  with weak AI churn.

## 5. Per-cadence emphasis

Each cadence answers a different question. Do not write a long daily; do not
write a weekly that is five dailies stapled together.

- **Daily**, *What matters today?* The day's decisive story leads; 2–4 beats;
  end with "What to watch today": concrete, dated, forward-looking hooks grounded
  in the day's items. Report events. 300–700 words.
- **Weekly**, *What were the week's through-lines?* Synthesize, don't recap:
  name 3–6 themes the week's stories share, and what each adds up to. A story
  that ran in a daily may reappear here only as part of a larger pattern. End
  with Quick Hits for the strong tail. 700–1200 words.
- **Monthly**, *What developed, and what resolved?* A thematic retrospective:
  the month's 3–5 dominant arcs, how each moved week to week, what closed versus
  what stays open. Elevate; do not re-list. 900–1500 words.
- **Quarterly**, *What is the trajectory?* Trendlines over events. Durable
  trends versus noise; inflection points; positions and predictions that aged
  well or badly. Synthesize the monthlies. Prefer the vector to the incident.
  1000–1600 words.
- **Yearly**, *What did the year decide, and what did conventional wisdom get
  wrong?* The defining shifts, the stories that mattered in hindsight, the open
  questions carried forward. 1200–2000 words.

The retrospectives earn their length only by adding a layer the reader could not
assemble from the dailies alone: the pattern, the through-line, the judgment
about what proved to matter.

## 6. Cross-edition discipline

- **Dedup across editions of a cadence.** A daily never repeats a prior daily's
  story. Retrospectives may revisit a story, but only to advance it, new
  development, or its place in a pattern, never to re-report it.
- **Track developing threads.** When a story moves (a recall reversed, a patch
  shipped, a claim rebutted), say what changed and when, and reference the prior
  state in one clause. Datelines are absolute; the reader may arrive late.
- **Callbacks earn their keep.** Reach back to an earlier edition only when the
  new fact reframes the old one. Not for continuity's sake.

## 7. Handling contested and provisional material

- **Claims stay claims.** Anything from an interested party, a vendor, a
  founder, a government without published basis, is attributed and framed as a
  claim until independently checked. Never launder a claim into fact.
- **Label review status.** Preprints are preprints; self-reported benchmarks are
  flagged as self-reported every time; demos are not shipping products.
- **Show disagreement.** Where credible sources conflict, present both with
  attribution. Do not resolve a live dispute for the reader.
- **Security items** carry the four facts that are the story: what is affected,
  what it allows, whether exploitation is observed, and whether a patch exists , 
  no panic verbs.
- **Separate fact from forecast.** What happened is indicative past tense; what
  might happen gets an explicit conditional and a named owner.

## 8. The hindsight test

For retrospectives and for any backfilled edition, apply the advantage the live
pipeline never had: knowing what proved to matter. Elevate the stories that
turned out to be load-bearing; give less room to what looked big and fizzled;
name the prediction or framing that aged badly. Hindsight is for *weighting and
judgment*, never for inventing a fact that was not in the record. Every claim
still traces to a curated source.

## 9. The test for any edition

Before it ships, the edition must pass all four:

1. Would this reader thank us for every item, and not resent one?
2. Does the lead deserve to lead over everything else in the period?
3. Is every claim attributed and every number given its baseline?
4. Could any item be cut with the reader losing nothing? If yes, cut it.
