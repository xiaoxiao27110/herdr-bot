---
name: session-distill
description: Incrementally distill transcript slices [last_summarized_line .. EOF] into high-signal WAL tags without re-reading prior turns.
---

# Session Distill Skill

Triggered during idle windows (`distill_idle_minutes` & `distill_min_new_turns`) across `active`, `pinned`, and `sealed` sessions, and forced for all unsummarized lines at `23:30` Step 0.

## Output Rules
- Summarize ONLY the provided `turns` slice (`[from_line .. to_line]`).
- Emit at most 3 high-signal tag types:
  - `[DECISION]` — architectural or implementation decision with rationale
  - `[CONTRACT_CHANGE]` — cross-repo interface change referencing `<repo>@<sha>` or `<repo>:<relpath>`
  - `[WIP]` — unfinished cross-repo task or blocker
- Overwrite same-topic entries in `notes/today.md` rather than appending duplicates.
- If the slice contains only routine chatter/status with no new `[DECISION]`/`[CONTRACT_CHANGE]`/`[WIP]`, output a single line `NOOP` with no extra explanation.
