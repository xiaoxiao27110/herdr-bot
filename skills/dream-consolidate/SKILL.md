---
name: dream-consolidate
description: Nightly 4-phase memory consolidation (Orient -> Gather -> Consolidate -> Prune) reconciling notes/today.md against real repository code and git history.
---

# Dream Consolidate Skill

Run during the nightly `23:30` closeout pipeline after Step 0 (`session-distill` catch-up) and before Step 2 (`archiver`).

## 4-Phase Workflow

### Phase 1: Orient
- Read `<index_file>` (e.g. `AGENTS.md`), `repo-boundaries.yaml`, `memory/contracts/*.md`, `memory/decisions.md`, and `tasks/active-wip.md`.
- Inspect `notes/today.md` (`[UNVERIFIED / TODAY ONLY]` WAL hot buffer).

### Phase 2: Gather
- For every `[DECISION]`, `[CONTRACT_CHANGE]` (`<repo>@<sha>`), and `[WIP]` entry in `notes/today.md`:
  - Verify against actual repository files and `git log` / `git show <sha>` in the target repo.
  - Discard stale, superseded, or unverified claims that contradict real code.

### Phase 3: Consolidate
- Promote verified contract changes to `memory/contracts/<pair>.md` using `<repo>:<relpath>` logical paths.
- Promote verified architectural rules/pitfalls to `memory/decisions.md` using the `(Rule, Verification, Why Not)` triad.
- Update `tasks/active-wip.md` (cross off completed items, keep active cross-repo work).
- Keep `<index_file>` concise (`<= index_max_lines`, `<= index_max_bytes`).

### Phase 4: Prune
- Archive `notes/today.md` into `notes/archive/YYYY-MM-DD.md`.
- Reset `notes/today.md` to an empty WAL header (`<= today_wal_max_lines`).
- Run `hq guard` to verify line/byte budgets and that all `<repo>:<relpath>` references exist.
