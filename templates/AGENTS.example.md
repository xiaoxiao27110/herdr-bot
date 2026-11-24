# Project Memory Index (AGENTS.md)

> Keep this index concise (`<= 200` lines, `<= 25600` bytes). Use `<repo>:<relpath>` logical paths.

## Repositories
- `gateway-svc`: Edge HTTP/gRPC gateway (`gateway-svc:api/router.py`)
- `worker-svc`: Background job processor (`worker-svc:jobs/runner.py`)

## Key Contracts & Decisions
- Cross-repo contracts live in `memory/contracts/`.
- Architecture decisions & invariants live in `memory/decisions.md`.
- Active cross-repo work is tracked in `tasks/active-wip.md`.

## #General & #Topic Auto-Orchestration
- **轻量问答直回**：打招呼、轻量问答、状态查询、临时汇报直接在 `#General` 回复，不开 Topic。
- **独立开发自动开 Topic**：遇到需要修改代码、跑测试或长链路排查的独立开发需求时，`#General` 自主执行 `herdr-bot task spawn <repo> <slug> "<需求>" --hq-dir "$HQ_DIR"` 拉起独立 worktree + Herdr 面板 + 话题卡片。
- **验收后自动封口**：`#Topic` 完成并验收通过后，Agent 可调用 `herdr-bot seal "$HQ_SESSION_KEY" --hq-dir "$HQ_DIR"` 自主封口（否则空闲超时也会自动封口）。
