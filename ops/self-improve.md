# Eclecta — nightly self-improvement runbook

This is the procedure the scheduled 4am pass follows (queued via the `runat`
skill as `eclecta-nightly`). It exists so eclecta.co keeps getting better on its
own: each pass verifies health, learns something, records it, and lands a few
safe improvements — leaving anything bigger or riskier for a human to approve.

## Golden rules
1. **Repo/staging edits ONLY.** Never start, stop, or reload the launchd
   services (`io.starikov.signal.{server,worker,watchdog}`), and never edit the
   deployed pipeline copy at `~/.local/state/signal/app`. Pipeline changes go to
   the repo copy under `pipeline/`; going live is a separate, human-approved sync
   + `launchctl kickstart`. (See the memory note "Ask before touching services".)
2. **Green or revert.** Anything you land keeps `npm run build`, `npm test`, and
   (if `pipeline/` changed) `pytest` green. If you can't get green, revert and
   log it as a blocked item — don't ship red.
3. **Safe-only auto-apply.** Auto-apply only obviously-correct changes: typos,
   dead-config removal, doc fixes, test additions, small polish, and the
   owner-requested items in `IMPROVEMENTS.md` §A that don't need a decision.
   Items marked `[?]` (model routing, caps, brand wording, going live) are for
   Illya — propose, never decide.
4. **Bounded.** ~20–30 min of work per pass. Depth over breadth: land 1–3 solid
   items rather than half-doing ten.

## The pass, in order
1. **Sync + clean tree.** `git status`; if dirty with stray iCloud `"name 2.ext"`
   duplicates, they're safe to delete (originals == HEAD). Start from a clean,
   up-to-date `main`.
2. **Health check.**
   - `npm run build` succeeds.
   - `curl -sI https://eclecta.co` → 200; the newest edition
     (`src/content/digests/`) isn't stale for its cadence (a daily on a weekday
     morning, etc.).
   - Skim `~/Library/Logs/signal/` for fresh errors (read-only) — note, don't act
     on the live pipeline.
3. **Quality review.** Read the newest 1–2 published editions and
   `src/data/picks.json` against `docs/editorial-policy.md` +
   `docs/digest-style.md`. Note concrete issues (banned words that slipped
   through, a lead that shouldn't lead, an unattributed claim, a missing free
   link, an over-long `novelty`).
4. **Measure + learn (the self-learning instruments).** All repo-side, all
   green-or-revert, all cheap:
   - **Eval.** `python3 -m signalpipe eval run` (local backend, $0) → writes
     `eval/results/<date>.json`. Compare `agreement_featured` / `featured_precision`
     / `relevance_mae` to the previous result. A drop with the gold set unchanged
     is a **judge regression** — flag it in the journal and, if you can see the
     cause (a prompt edit, a model swap), propose a fix `[?]`. Then
     `python3 -m signalpipe eval grow -k 5` to add a few fresh provisional
     candidates. Correct any obviously-mislabeled gold with `eval label`.
   - **Momentum.** Read `kb/momentum.json`. Note the `rising` and `emerging`
     categories in the journal — that is "what matters now / next." A category
     surging for several passes may justify enabling the momentum multiplier or a
     topic nudge (`[?]`, propose — don't flip it yourself).
   - **Library.** `python3 -m signalpipe library refresh -k 3` to grow the
     registry + rebuild a few entity pages from fresh coverage. Skim one page for
     accuracy before committing; entities are non-person by policy.
   - **Adaptive bar (only if enabled).** Check the effective bar in the latest
     `runs.stats` (or `signal runs`). If it's pinned at the floor every pass, the
     bar is too high for current supply — flag it `[?]`.
5. **Pull work.** Open `ops/IMPROVEMENTS.md`. Pick the highest-value
   non-`[?]` item(s) you can finish and verify this pass. Prefer §A
   (owner-requested) first, then High, then Medium.
6. **Do it, test-first where it's behavior.** Add/adjust tests, make the change,
   run the relevant suite(s), confirm green. Re-capture pages if it's visual
   (`node scripts/capture.mjs`, or the focused helper).
7. **Record.**
   - Append a dated entry to `ops/journal/YYYY-MM-DD.md` (what you checked, what
     you changed, what you learned, what's still open).
   - Move finished items to `[x]` in `IMPROVEMENTS.md`; add any NEW ideas you
     found (this file grows over time — that's the point).
   - Add durable, reusable insights to `ops/LEARNINGS.md` (not one-off task
     notes — things a future pass should know).
8. **Commit.** One focused commit per landed item (or one per pass for small
   polish). Clear message. Data-only or ops-only commits are fine and skip the
   pytest deploy gate. Do NOT push if unsure; committing to local `main` is
   enough for a human to review. (Illya has authorized direct-to-main for this
   repo previously; still, keep commits reviewable.)
9. **Re-arm.** The `runat` tick handles re-scheduling tomorrow's 4am pass; if the
   task instruction includes the re-enqueue step, make sure it ran.

## What "great" looks like
The whole value of Eclecta is subtraction and trust: the right handful of stories
with a clear account of why, the original source credited and a free way in, no
tracking, everything inspectable. Every improvement should push toward that —
sharper cuts, better provenance, cleaner chrome, lower cost at equal or better
quality — never more volume for its own sake.
