# Project Memory Index (AGENTS.md)

> Keep this index concise (`<= 200` lines, `<= 25600` bytes). Use `<repo>:<relpath>` logical paths.

## Repositories
- `gateway-svc`: Edge HTTP/gRPC gateway (`gateway-svc:api/router.py`)
- `worker-svc`: Background job processor (`worker-svc:jobs/runner.py`)

## Key Contracts & Decisions
- Cross-repo contracts live in `memory/contracts/`.
- Architecture decisions & invariants live in `memory/decisions.md`.
- Active cross-repo work is tracked in `tasks/active-wip.md`.
