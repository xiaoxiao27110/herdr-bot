---
name: boundary-triage
description: Analyze cross-repo sensitive commits against downstream contracts and emit appropriate L0-L3 notifications.
---

# Boundary Triage Skill

Triggered when `gatekeeper` detects a commit touching `watch_paths` (and not `ignore_paths`) in `repo-boundaries.yaml`.

## Procedure
1. Inspect `from_sha..to_sha` diff in the upstream repo for the matched files.
2. Read the declared `downstream` contract files in `memory/contracts/`.
3. Determine compatibility:
   - **Compatible / Additive**: Record a concise `[CONTRACT_CHANGE]` note (`<repo>@<sha>`) in `notes/today.md` and stay silent (0 interruption).
   - **Breaking / Ambiguous**: Call `hq-notify --level review --message "..." --option "Accept & Update Downstream" --option "Revert / Hold"` so the human decides before downstream breakage spreads.
