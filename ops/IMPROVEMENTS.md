# Eclecta — Improvements backlog

The running, prioritized list of improvements to make to eclecta.co (the static
site) and signalpipe (the pipeline). Append to it over time; check items off as
they ship. This is the work queue the nightly self-improvement pass
(`ops/self-improve.md`, scheduled 4am) pulls from.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[?]` needs Illya's decision
(do NOT auto-apply). Every item names the file(s) and the concrete change.

Ground rules (see `ops/self-improve.md`):
- Repo/staging edits ONLY. Never start/reload the launchd services
  (`io.starikov.signal.*`) or edit the deployed pipeline copy at
  `~/.local/state/signal/app` without Illya's explicit go.
- Pipeline edits here change the **repo** copy; going live is a separate,
  human-approved sync + worker kickstart.
- Keep the site build green (`npm run build`), the JS suites green
  (`npm test`), and the pipeline suite green (`pytest`) for anything you land.

---

## SHIPPED 2026-07-18 (this session, all suites green)

Landed on `main` after the owner returned with fresh usage. Site: 246 unit +
110 e2e green. Pipeline: 2650 pytest green. Verified visually at desktop + mobile.

- **A1 masthead** — `align-items: center`; kicker + tagline now balanced against
  the wordmark (was floating low). ✓
- **A2 edition labels** — Title Case (Daily Brief … The Year); tests updated. ✓
- **A3 dateline** — the edition slot now links the CURRENT edition
  (`No. N · Daily Brief`) instead of the static repo. ✓
- **A4 mobile footer** — `.foot` padding-block only, `.wrap` gutter restored; no
  more flush-left clip. Also fixed ALL horizontal overflow at 360–390px
  (`/feeds/` grid `min()`, coverage count wrap, global `overflow-wrap`); new
  `tests/e2e/layout.spec.ts` pins it. ✓
- **A5 header unified** — about/contact/404/digest now use the full masthead;
  compact-header code + CSS removed; digest-header e2e assertion added. ✓
- **A6 novelty** — CSS 2-line clamp + pipeline persistence clamp `[:160]` +
  tightened judge/curate prompts; `test_persist_clamps_runaway_novelty` pins it. ✓
- **A8 provenance** — editorial-policy §3 + the digest writer prompt now demand
  crediting whoever broke the story and always leaving a free read. ✓
- **A9 de-personalization** — editorial-policy §1 anonymized (Google/Garmin
  removed from the public repo). ✓
- **Review High #1** — `MAX_JUDGE_CHARS` wired: triage/judge run the 6K excerpt,
  only the writer sees the full article (~75% off the two hottest LLM calls). ✓
- **Review High #2** — `pr.yml` now runs the pipeline pytest suite (paths-filtered). ✓
- **Review #3 (partial)** — `effort="low"` on triage + judge; the write call left
  at default (Opus prose — quality tradeoff, see `[?]`). ✓

Second batch (owner directive + remaining review findings):
- **Model routing** — per Illya: **Sonnet triage, Sonnet curation (judge), Opus
  writing**. Example config: `tier_overrides={}` (all on subscription),
  triage/judge/deep → `claude-sonnet-5`, write/digest → `claude-opus-4-8`;
  `claude-sonnet-5` + `claude-fable-5` added to `backend_api.PRICING`. ⚠️ This is
  the REPO/example only — his LIVE config still routes triage/judge to local
  qwen; moving them to subscription Sonnet raises Max-quota burn a lot (see note). 
- **Digest effort** — `high` for daily/weekly, `max` only for monthly+. ✓
- **Dead knobs** — removed `api_use_batches`, `escalate_spread`,
  `stop_curate_on_cap` from the example; `tiers.digest.local` 72b→14b (OOM trap);
  daily cap 10→5; runbook fixed to say `digests.*.cron` is documentation-only. ✓
- **Coverage** — new sitemap-output + default-OG-card e2e tests. ✓

Still open (owner decisions in §C): whether to apply the Sonnet/Opus routing to
the LIVE pipeline config (quota tradeoff), and cadence interval tuning.

---

## A. Owner-requested (2026-07-18 screenshot + message) — SHIP THESE FIRST

### A1. Masthead vertical alignment / centering  [ ]  (site)
The wordmark "ECLECTA" and the kicker "THE FRONTIER, DISTILLED" + tagline read
off-balance: `.masthead { align-items: baseline }` bottom-anchors the two-line
kicker to the wordmark's baseline, leaving dead space above it.
- `src/styles/global.css` `.masthead` (~line 374): change `align-items: baseline`
  → `align-items: center` so the kicker block centers against the wordmark.
  Re-capture front + a digest at desktop/mobile and confirm it reads balanced;
  nudge the kicker `line-height`/padding if the optical center is off.
- Confirm the mobile override (`.masthead__kicker { text-align: left }`, ~line
  1239) still looks right once centered.

### A2. Title-case edition labels  [ ]  (site)
- `src/site.ts` `KIND_LABEL`: `Daily Brief`, `Weekly Digest`, `Monthly Review`,
  `Quarterly Report`, `The Year`.
- Prose sites lowercasing them still read fine (`about.astro:59` uses
  `.toLowerCase()`; `feeds.ts:157` too; og card `.toUpperCase()`).
- Update tests that pin the old casing: `tests/unit/site.test.ts:73-84`,
  `tests/unit/feeds.more.test.ts:156,163,177`, `tests/e2e/broadsheet.spec.ts:58`.

### A3. Dateline "Open source" → link to the CURRENT edition  [ ]  (site)
Today the dateline's right slot links the repo (static, redundant with the nav
`Source` + footer). Make it useful: link to the latest published edition.
- `src/layouts/Base.astro`: sort `getCollection('digests')` by date desc, take
  `latest`; render `No. {editionNo} · <a href={href('/digests/'+latest.id+'/')}>{KIND_LABEL[latest.kind]}</a>`
  (falls back to plain `No. {editionNo}` when no digests). CSS uppercases it.
- Update `tests/e2e/chrome.spec.ts:27-34` (it asserts text `Open source` +
  a github href) to assert the link now points at `/digests/…` and the newest
  edition label.

### A4. Mobile footer clipping (left edge)  [ ]  (site)  ROOT-CAUSED
`.foot` sets `padding: var(--space-6) 0 var(--space-8)` (global.css ~line 1161),
whose `0` horizontal value overrides `.wrap { padding: 0 var(--gutter) }` (later
in the cascade, same specificity). On mobile the centered max-width no longer
adds side space, so footer content sits flush against the screen's left edge.
- Change `.foot` to `padding-top: var(--space-6); padding-bottom: var(--space-8);`
  (drop the shorthand) so the `.wrap` gutter provides left/right padding.
- Remove the dead `.foot { flex-direction: column }` in the `max-width:40rem`
  block (~line 1241 — `.foot` is `display:grid`, so it's a no-op).
- Repoint the dead `.foot__links a { padding:0.4rem 0 }` (~line 1243) →
  `.foot__col a` (the real footer link class) for proper mobile tap targets.

### A5. Unify the header across all pages  [ ]  (site)
About/contact/404/digest pages use the compact header; the owner wants the same
full masthead everywhere.
- Remove `compactHeader` (and the `edition` prop) from `about.astro:45`,
  `contact.astro:5`, `404.astro:5`, `digests/[...slug].astro:43`.
- Once unused, delete the `compactHeader`/`edition` branch + props in
  `Base.astro` and the `.compact-head*` rules in global.css (dead-code cleanup).
  Digest edition info still shows in the article header (`.article__kind`).
- Before the rewrite, ADD an e2e assertion for the digest header (finding B/#8),
  then update it in the same commit so the new contract is pinned.

### A6. Lead "novelty" wonky standfirst  [ ]  (site + pipeline)
Root cause (confirmed): `novelty` is unconstrained end-to-end; long LLM output
renders as a paragraph-length lead standfirst and also pollutes write + digest
prompts. Fix in layers:
- Pipeline prompt: `llm/schemas.py` SYSTEM_JUDGE — "novelty: ONE phrase, ≤12
  words, no full sentence, no trailing period." (also tighten SYSTEM_CURATE).
- Pipeline persistence clamp: in `curate.py` `_persist_done` (~line 169) and
  `_mark_judge_skip` (~line 147), store `(judged.get("novelty") or "")[:160]`
  (mirrors the existing `skip_reason [:300]` convention). Choke point covers all
  backends + downstream consumers.
- Site tripwire: `src/lib/schema.ts` novelty `z.string().max(200).nullable()`
  (or `.refine`) + a `tests/unit/schema.more.test.ts` case that an over-long
  novelty fails `parsePicks`.
- Site defensive clamp: `.pick__novelty` gets a 2-line `-webkit-line-clamp` so a
  stray long value can never dominate the lead.
- Pin with `pipeline/tests/test_publish.py` (novelty ≤ budget on export).

### A7. Brand — title + subtitle options  [?]  NEEDS ILLYA
Owner isn't sure about the kicker ("The frontier, distilled") and the tagline
("We read the firehose, so you read what matters.") and wants OPTIONS to pick a
proper brand. Keep the wordmark "Eclecta" (renaming touches the domain, repo,
CNAME, and pipeline config — out of scope unless requested).
Do NOT change `src/site.ts` name/kicker/tagline unilaterally. Present options and
let Illya choose. Candidate directions to offer (kicker / tagline):
- Kicker: "The frontier, distilled" · "The signal, not the noise" ·
  "Everything worth reading, nothing else" · "The wire for the frontier".
- Tagline: "We read the firehose, so you read what matters." · "Thousands of
  sources in. The few that matter out." · "Machines read everything; you read
  the part that counts." · "The day's technology and science, cut to what's
  load-bearing."

### A8. Emphasize the ORIGINAL source + always a FREE version  [ ]  (pipeline + docs + site)
Owner: whatever breaks a story deserves credit, but there should always be a free
version. The policy exists but should be stronger and consistent.
- `docs/editorial-policy.md`: add a dedicated "Provenance and access" section —
  find and link the ORIGINAL source (who broke it / the primary document) first;
  credit the outlet that broke it ("first reported by …"); ALWAYS provide a free
  read alongside any paywalled primary (the `free_source_chain` already exists in
  config). Strengthen §3's "never the aggregator's rewrite when the source is
  free."
- `docs/digest-style.md` §2 (24/25) and `docs/cadence-templates.md` (Links):
  reinforce primary-source-first + free-read.
- `llm/schemas.py` SYSTEM_WRITE / SYSTEM_JUDGE / `system_digest`: instruct to
  prefer/attribute the original breaking source and surface a free link.
- `src/pages/about.astro`: the "primary source is linked first, always … a free
  read is linked beside it" line is good; make the provenance/free-read promise a
  touch more prominent.

### A9. De-personalize public docs/prompts  [ ]  (docs) — PRIVACY
This is a PUBLIC repo. `docs/editorial-policy.md` §1 still names a specific
employer: "A senior software engineer (Google; before that Garmin)". The live
`READER_PROFILE` in `llm/schemas.py` is already anonymized ("a technically deep,
working software engineer"). Rewrite §1 to match (anonymous senior engineer).
Grep the repo for other leaks: `grep -rniE 'garmin|google;|illya|starikov' docs pipeline src` and scrub anything that shouldn't be public (author credit on the
about page / footer is intentional and fine).

---

## B. Production review — 19 adversarially-verified findings (2026-07-18)

Full detail in the workflow output (`tasks/we1xrqjcj.output`, run
`wf_e75e6ea7-c5a`). Severity is the post-verification value.

### High
- [ ] **MAX_JUDGE_CHARS never wired** — `curate.py:41` defines a 6000-char judge
  excerpt but `_build_prompt` (line 249) always uses 24000, reused for triage +
  judge + write. Build `short_prompt = _build_prompt(conn, c, MAX_JUDGE_CHARS)`
  for the triage/judge calls; keep the full prompt only for the writer. ~75% off
  the two highest-frequency LLM calls, quality-neutral.
- [ ] **pr.yml never runs pipeline pytest** — the ~2,900-test suite runs only in
  `deploy.yml` on push to main, so pipeline PRs merge untested and break the
  deploy. Copy deploy.yml's paths-filtered `pytest` job into `.github/workflows/pr.yml`.

### Medium
- [ ] **No `effort` on triage/judge/write** — `curate.py` (253/281/290) omits
  `effort=`, so the CLI runs default (high) effort for keep/skip + extraction.
  Pass `effort="low"` on triage+judge. The write call produces published prose —
  `effort="medium"` is a quality tradeoff → treat as `[?]` / discuss.
- [ ] **novelty unconstrained** — see A6 (same root cause).
- [ ] **`digests.<kind>.cron` dead** — scheduling runs on a gated 30-min interval
  (`worker.py`), not these crons. Remove them from `signal.example.json` (keep
  `min_relevance`/`max_items`) and fix `docs/operating-runbook.md:39-40` which
  wrongly tells the operator the crons are live. Consider excluding `cron` from
  `config_fingerprint()`.
- [?] **Model routing drift** — config runs triage/judge on Sonnet 4.6 + write on
  Opus 4.8, a full tier above `curate.py`'s documented Haiku/Haiku/Sonnet design.
  Consider triage/judge → `claude-haiku-4-5`, deep/write → `claude-sonnet-5`.
  MUST-DO if migrating to Sonnet 5: add `"claude-sonnet-5": (3.0, 15.0)` to
  `backend_api.PRICING` (else the ledger overcharges ~2.5x via the fallback and
  trips the daily cap early). Illya's call — it changes cost/quality.
- [?] **Phase-1 triage redundant when triage==judge model** — when both resolve to
  the same (backend, model), the gate runs the same model twice on the same
  prompt. Either make triage genuinely cheaper (Haiku) or skip phase 1 when
  tiers collapse. Pairs with the Haiku routing decision above.
- [ ] **Digest-header e2e coverage gap** — add the assertion (see A5) before the
  unification rewrite.
- [ ] **No horizontal-overflow test** — add `tests/e2e/layout.spec.ts`: at 390 and
  320 px, assert `scrollWidth - clientWidth <= 0` on `/`, latest daily digest,
  `/coverage/`, `/preferences/`, `/feeds/`, and that `footer.foot` fits with all
  three `.foot__col` visible. Pins the A4 bug class.
- [ ] **novelty length contract untested** — pin the A6 clamp in
  `test_publish.py` + `schema.more.test.ts`.
- [ ] **Sitemap output untested** — add to `seo.spec.ts`: GET
  `/sitemap-index.xml` → 200 + valid XML, contains root, `/ai/`, latest daily
  digest URL, and the digest `<lastmod>`.

### Low
- [ ] **`spend.stop_curate_on_cap` dead** — behavior is unconditional; delete the
  key from `signal.example.json` + `tests/fixtures/signal.min.json`.
- [ ] **`backend.local.escalate_spread` dead** — remove from example config.
- [ ] **`backend.api_use_batches` inert** — reserved flag, no Batches impl; remove
  from example config (or implement for backfill mode only).
- [ ] **`site.picks_min_relevance` alive but undocumented** — no code change; add a
  doc note (careful: a literal value decouples it from `min_relevance_for_feed`).
- [?] **Digest `effort="max"` for all kinds** — `digest.py:224`; consider
  `effort=("max" if kind in ("monthly","quarterly","yearly") else "high")`.
- [?] **Cadence/cap sanity** — `curate_min:15` is aggressive and `daily_cap_usd:10`
  exceeds the code default (5) and the stated free/low-cost goal. Consider
  `curate_min` 30-60 (or `curate_batch` 2) and `daily_cap_usd:5`. Align
  `worker.py`'s fallback default with the example so a missing key can't shift the
  cadence 8x. Illya's call.
- [ ] **Default OG card untested** — in `seo.spec.ts`, GET `/og/default.png` →
  200 + `image/png`.

### Nit
- [ ] **`tiers.digest.local = qwen2.5:72b`** — a 64GB-RAM OOM trap if ever
  activated (currently inert). Change to `qwen2.5:14b` or delete the digest local
  entry so it falls back to `backend.local.models`.

---

## C. Decisions parked for Illya  (do NOT auto-apply)
- A7 brand kicker/tagline (present options, let him pick).
- Model routing / Sonnet 5 migration + cadence & cap tuning (B medium/low `[?]`).
- Whether to drop write/digest effort (quality tradeoff on published prose).
- Any pipeline change GOING LIVE (repo edits are safe; deploy = sync +
  `launchctl kickstart` is his explicit go).

## D. Already done (2026-07-18, pre-limit)
- Scheduled the nightly self-improvement pass (`runat`, 04:00, `eclecta-nightly`).
- Ran the 6-dimension adversarial production review (19 confirmed findings above).
- Captured every page type at desktop + mobile (before-state) and root-caused
  the masthead, footer, and about-header issues firsthand.
- Built this ops system.
