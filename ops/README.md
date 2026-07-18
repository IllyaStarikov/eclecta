# ops/ — Eclecta's operational journal & self-improvement system

A durable, version-controlled record of how eclecta.co is run and improved over
time. It lets each maintenance pass (human or the scheduled nightly agent) build
on the last instead of starting cold.

- **`self-improve.md`** — the runbook the nightly 4am pass follows.
- **`IMPROVEMENTS.md`** — the living, prioritized backlog of changes to make.
  Append over time; check items off as they ship. This is the work queue.
- **`LEARNINGS.md`** — durable, reusable insights about the site + pipeline
  (things worth knowing on the next pass, not one-off notes).
- **`journal/YYYY-MM-DD.md`** — one file per maintenance pass: what was checked,
  changed, and learned, and what's still open.

The nightly pass is scheduled in-session via the `runat` skill as
`eclecta-nightly` (04:00 local). It only runs while a Claude Code session stays
open on this Mac. List/cancel:

```
python3 ~/.claude/skills/runat/scripts/schedule.py list
python3 ~/.claude/skills/runat/scripts/schedule.py remove eclecta-nightly
```

Scope guardrail: this system edits the **repo** only. It never touches Illya's
live launchd services or the deployed pipeline copy — going live is always a
separate, explicitly-approved step.
