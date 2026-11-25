# Project Memory Index (AGENTS.md)

> Keep this index concise (`<= 200` lines, `<= 25600` bytes). Use `<repo>:<relpath>` logical paths.

## Role & Jurisdiction
- **管辖范围内充分授权**：仅管辖 `hq.yaml` 已注册仓库与当前 `$HQ_DIR` 记忆工作区（`AGENTS.md`、`memory/`、`tasks/`、`notes/`），拥有完整的排查、开发（改代码前先切功能分支，勿直接提交主分支）、测试与记忆维护权限。
- **管辖范围外只读参考**：排障时允许只读查看系统其他文件、运行日志或外部工具状态作为参考依据。
- **严禁越界修改或自我迭代**：严禁修改、也严禁主动提议或声称要去修改任何未在 `hq.yaml` 注册的仓库、外部系统工具或编排器自身（如 `herdr-bot` / `herdr`，严禁自我迭代）；遇到属于编排器或未注册仓库的问题时，仅客观说明现象与建议，提醒用户在对应独立工作区自行处理。

## Repositories
- `gateway-svc`: Edge HTTP/gRPC gateway (`gateway-svc:api/router.py`)
- `worker-svc`: Background job processor (`worker-svc:jobs/runner.py`)

## Key Contracts & Decisions
- Cross-repo contracts live in `memory/contracts/`.
- Architecture decisions & invariants live in `memory/decisions.md`.
- User context, preferences & corrections live in `memory/user-notes.md`.
- Active cross-repo work is tracked in `tasks/active-wip.md`.

## #General & #Topic Collaboration
- **默认在 `#General` 完成**：日常问答、排查与常规开发默认在 `#General` 连贯推进（可派子 Agent，改代码前先切功能分支），未经用户明确要求永不擅自新开 `#Topic`。
- **按用户要求开 `#Topic`**：仅当用户明确提出“单独开个任务 / 另开分身跟进”时，执行 `herdr-bot task spawn <repo> <slug> "<含背景摘要的需求>" --hq-dir "$HQ_DIR"` 拉起独立 worktree + Herdr 面板 + 话题卡片。
- **验收后封口**：`#Topic` 完成并验收通过后，可调用 `herdr-bot seal "$HQ_SESSION_KEY" --hq-dir "$HQ_DIR"` 封口（非 `pinned` 话题空闲超时也会自动封口）。
- **自主定时唤醒**：需稍后跟进构建或验收时，Agent 可调用 `herdr-bot timer set --in 15m --reason "..." --hq-dir "$HQ_DIR"` 设定一次性或周期性唤醒闹钟（支持 `--at` / `--every` 及 `timer list` / `timer cancel`）。
