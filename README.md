# hq-pilot

A lightweight, general-purpose, 24/7 multi-repository R&D orchestrator engine for native coding agents.

`hq-pilot` acts as a deterministic **0-LLM control plane** (`hq-daemon`) that coordinates your repositories, memory files, persistent terminal panes, and external chat channels without introducing a second agent framework.

## Key Features

- **Single Brain, Zero Nested Agents**: Delegates all reasoning and coding to your native coding agent CLI via `CodingAgentAdapter` (V1 ships `AgyAdapter`).
- **4-State Session Lifecycle**: `active` → `sealed` (logical closeout, workspace preserved until night) → `archived` (safe nightly cleanup), with automatic `pinned` promotion whenever a human touches the session locally or via terminal attach.
- **In-Place Resurrection (`hq resume <id>`)**: Rebuilds archived Git worktrees on their preserved branch and resumes the original agent conversation.
- **Decoupled Cursor Distillation & 3-Stage Nightly Closeout**: Incremental transcript slicing (`[last_summarized_line .. EOF]`) across `active`, `pinned`, and `sealed` sessions, followed by `23:30` 4-phase Dreaming (`Orient → Gather → Consolidate → Prune`) and safe archiving.
- **Pluggable Extension Axes**:
  - `hq_pilot/agents/`: `CodingAgentAdapter` (`agy` included)
  - `hq_pilot/terminals/`: `TerminalDriver` (`herdr` preferred, `tmux` automatic fallback)
  - `hq_pilot/channels/`: `MessagingChannel` (`telegram` Forum Topics + Inline Keyboard, `feishu` WebSocket + Interactive Cards + `reply_in_thread`, and `local` terminal chat)
- **Centralized Tuning & Guards**: 20+ tunable parameters in `hq.yaml` (`hq config list` / `hq config set`) with automatic hot-reload, `hq guard` for memory/path budgets, and `hq oss-check` for open-source hygiene.

## Quick Start

```bash
# 1. Initialize an HQ memory repository for your project
./bin/hq init ~/my-project-hq
cd ~/my-project-hq

# 2. Configure repos in hq.yaml and repo-boundaries.yaml, then clone missing repos
/path/to/hq-pilot/bin/hq bootstrap --hq-dir .

# 3. Check host dependencies
/path/to/hq-pilot/bin/hq doctor --hq-dir .

# 4. Inspect or adjust tuning parameters
/path/to/hq-pilot/bin/hq config list --hq-dir .
/path/to/hq-pilot/bin/hq config set commit_debounce_sec 180 --hq-dir .

# 5. Validate memory budgets and logical paths (<repo>:<relpath>)
/path/to/hq-pilot/bin/hq guard --hq-dir .

# 6. Start the 24/7 daemon
/path/to/hq-pilot/bin/hq up --hq-dir .
```

## Architecture Overview

```text
Inputs (Telegram Topics / Local TTY / Git Commits / Timers)
   │
   ▼
0-LLM Daemon Kernel (gatekeeper + SQLite inbox + cursors.json)
   │
   ├─► Track A: Background headless short sessions (event-driven -> sealed)
   ├─► Track B: #General coordinator session (idle/turn-bounded -> sealed)
   └─► Track C: #Topic-N interactive worktree panes (human touch -> pinned)
```

## Feishu / Lark Channel (WebSocket Long-Connection)

`hq-pilot` supports Feishu/Lark via outbound Open API (`open.feishu.cn`) and inbound WebSocket long-connection (`lark-oapi`, no public webhook URL required):

```bash
# Install optional Feishu dependency
pip install -e ".[feishu]"   # or: pip install "lark-oapi>=1.4.0"

# Configure .hq/secrets.env
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_CHAT_ID=oc_xxx

# Validate credentials, list joined group chats, or capture first DM chat_id
hq feishu detect-chat --hq-dir /path/to/my-project-hq --timeout 30
```

Set `channel.backend: feishu` in `hq.yaml` to enable dual-write with `LocalChannel` (`CompositeChannel`).

## License

MIT License — see [LICENSE](LICENSE).
