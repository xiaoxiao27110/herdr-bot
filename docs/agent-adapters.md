# Coding Agent 适配器开发与接入指南

`herdr-bot` 通过 `hq_pilot.agents` 适配层将编排逻辑（记忆栈、事件循环、Herdr 终端调度、生命周期 Hook 处理、定时与夜间 Dreaming）与具体 Coding Agent CLI 解耦。无需修改核心代码，即可接入新的 Coding Agent（如 Claude Code、OpenCode 等）。

---

## 1. 内置适配器

| `agent.backend` | 实现类 | 默认命令 | 索引文件 | 项目 Hook 文件 |
| --- | --- | --- | --- | --- |
| `agy`（默认） | `hq_pilot.agents.agy.AgyAdapter` | `agy` | `AGENTS.md` | `<ws>/.agents/hooks.json` |
| `codex` | `hq_pilot.agents.codex.CodexAdapter` | `codex` | `AGENTS.md` | `<ws>/.codex/hooks.json` |

### 使用 Codex CLI 后端

1. 初始化工作区时指定后端（或在现有 `hq.yaml` 中将 `agent.backend` 改为 `codex`）：

```bash
herdr-bot setup --agent-backend codex --hq-dir ~/herdr-hq
```

2. **Codex 0.163+ 项目与 Hook 双层信任说明**：
   - Codex 0.163+ 发现并执行项目级 `<ws>/.codex/hooks.json` 依赖两层信任门禁：
     1. **项目层信任（默认自动写入）**：Codex 在读取 CLI `-c` 覆盖参数前就已基于 `$CODEX_HOME/config.toml`（未设 `CODEX_HOME` 时为 `~/.codex/config.toml`）决定是否加载项目 `.codex/` 层，且普通子目录与 `.hq/worktrees/*` 独立 Git Worktree 不会继承 `$HQ_DIR` 的信任状态。
        - 默认 `agent.auto_trust_project: true` 时，`herdr-bot setup` 以及 daemon 在安装 hooks / 创建 worktree / 拉起会话前，会自动调用 `ensure_project_trusted(...)` 幂等地为 `$HQ_DIR`、托管仓库与实际启动 `cwd` / worktree 根写入：
          ```toml
          [projects."/path/to/herdr-hq"]
          trust_level = "trusted"
          ```
        - 写入守则：已为 `trusted` 则零改动；仅在实际变更已有文件前创建 `config.toml.bak-herdr-<时间戳>` 备份；采用文本级追加/局部更新，绝不重排或丢失其他内容和注释；原子写（临时文件 + `os.replace`）并保留原权限；写后通过 TOML 解析器校验合法性，失败则自动回滚备份并报错。
        - 如需关闭自动写入，可在 `hq.yaml` 中设置 `agent.auto_trust_project: false`（或执行 `herdr-bot setup --no-auto-trust-project`），此时 `herdr-bot` 不会修改 `config.toml`，仅打印需要手动添加的 TOML 片段。
     2. **Hook 执行信任与 Linked Worktree 同步**：Codex 默认要求对每个 `hooks.json` 持久化 `trusted_hash` 或传入 `--dangerously-bypass-hook-trust`。`CodexAdapter.extra_trust_args()` 会自动附带 `--dangerously-bypass-hook-trust` 以及 `-c 'projects."<abs>".trust_level="trusted"'`，以确保由 `herdr-bot` 自动写入的项目级钩子在无人值守模式下正常触发。
        - 在 linked Git Worktree 场景下，Codex 0.163 需要 `<wt>/.codex/` 存在且实际从主仓 `<main_repo>/.codex/hooks.json` 读取钩子。`CodexAdapter.ensure_hooks_installed()` 会自动通过 `git rev-parse --git-common-dir` 识别 linked worktree，对 worktree 与主仓执行同样的合并式写入（保留用户已有钩子），并把未跟踪的 `/.codex/hooks.json` 幂等追加到 `<git-common-dir>/info/exclude` 保持 `git status` 干净（若文件已被 Git 跟踪则向 stderr 打印 warning，绝不静默）。用户在主仓直接手动运行 `codex`（无 `HQ_SESSION_KEY` / 非受管 pane）时，`hq-agent-hook` 会安全忽略或隔离，不误绑定 `#General`/`#Topic` 也不误推消息。
   - **权限模式映射**（`hq.yaml` 中 `agent.permission_mode`）：
     - `default`：沿用用户 `~/.codex/config.toml` 默认策略
     - `accept-edits`：`-s workspace-write -a on-request`
     - `turbo` / `always-proceed`：`-s workspace-write -a never`
     - `skip-all`：`--dangerously-bypass-approvals-and-sandbox`

---

## 2. `CodingAgentAdapter` 接口契约

核心基类位于 `hq_pilot/agents/base.py`，子类需定义类属性 `name`、`index_filename`（建议同时声明 `default_bin`）并实现以下方法：

| 方法 | 职责与契约 |
| --- | --- |
| `build_headless_cmd(prompt, session_id=None, model=None, effort=None, add_dirs=None) -> list[str]` | 构造单轮无头执行命令（支持新建与 `session_id` 续聊），首元素为 `self.bin_path`。 |
| `build_interactive_cmd(initial_prompt=None, session_id=None, model=None, effort=None, add_dirs=None) -> list[str]` | 构造交互式会话启动/续聊命令。注意 `daemon.py` 会把 `interactive_cmd[1:]` 与 `extra_trust_args(...)` 拼接后通过 `herdr agent start --kind <name> -- <args>` 传给终端驱动。 |
| `extra_trust_args(trusted_dirs, permission_mode="default") -> list[str]` | 可选。返回按会话注入的工作区信任与权限模式参数（应去重路径并解析为绝对路径）。 |
| `ensure_project_trusted(project_dirs, *, auto_trust=True) -> ProjectTrustResult` | 可选。在需要持久化项目信任的后端（如 Codex `$CODEX_HOME/config.toml`）中幂等写入信任条目；`auto_trust=False` 时只返回需手动添加的片段而不修改文件。 |
| `parse_headless_output(stdout) -> HeadlessResult` | 解析无头命令 stdout，返回 `reply_markdown` 与 `agent_session_id`；非结构化输出时应降级为原文，空输出返回 `("", None)`。 |
| `count_transcript_lines(transcript_path) -> int` | 返回 transcript 文件的物理总行数；文件不存在时返回 `0`。 |
| `read_transcript_slice(transcript_path, from_line=0) -> tuple[list[Turn], int]` | 按 **0-based 物理行号** 从 `from_line` 增量读取并规范化为 `Turn(role, text, timestamp, line_no)`（`role` 规范为 `"user"` / `"assistant"`），同时返回当前物理总行数作为游标。应过滤系统提示、环境上下文、思维链（reasoning）与工具调用噪音。 |
| `ensure_hooks_installed(workspace_dir, hook_bin) -> Path` | 在工作区安装/合并生命周期 Hook，命令统一指向 `<hook_bin> --agent <name>`。必须**保留用户已有配置**且**多次调用幂等**。 |
| `parse_hook_payload(payload) -> HookEvent` | 将该 Agent 的原始 Hook JSON 规范化为统一的 `HookEvent`。 |
| `render_hook_response(event, inject_messages=None) -> dict` | 将规范化事件与待注入文本渲染为该 Agent CLI Hook 协议要求的 stdout JSON 字典。 |
| `detect_bin(explicit="", existing_override="") -> str` | 默认按 `explicit -> existing_override -> PATH -> ~/.local/bin/<default_bin>` 探测可执行文件，供 `setup` / `doctor` 使用。 |
| `describe() -> dict` | 返回 `name`、`default_bin`、`bin_path`、`index_filename` 元数据，供 `setup` / `service` 使用。 |

---

## 3. `HookEvent` 规范化协议

`hq_pilot/hook_cli.py` 只依赖 `HookEvent` 字段，不耦合任何具体 Agent 的字段名：

```python
@dataclass
class HookEvent:
    kind: Literal["session_start", "user_prompt", "stop", "other"]
    raw_event_name: str = ""
    agent_session_id: str = ""
    transcript_path: str = ""
    user_prompt: Optional[str] = None
    cwd: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)
```

### 内置后端映射对照

| `HookEvent.kind` | `agy` 事件与输出 | `codex` 事件与输出 |
| --- | --- | --- |
| `session_start` | `SessionStart` → `{"injectSteps": [...]}` | `SessionStart` → `{}` 或 `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}` |
| `user_prompt` | `PreInvocation` → `{"injectSteps": [...]}` | `UserPromptSubmit` → `{}` 或 `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "..."}}` |
| `stop` | `Stop` → `{"decision": "allow_stop"}` | `Stop` → `{}` |
| `other` | 其他事件 → `{"injectSteps": []}` | 其他事件 → `{}` |

> 说明：`hq-agent-hook` 未显式传 `--agent` 时，会按 `hq.yaml` 中的 `agent.backend` 自动推断适配器（找不到 `hq.yaml` 时回退 `agy`）。

---

## 4. 接入新适配器（无需改核心代码）

### 方式一：`module.path:ClassName` 动态加载（推荐外部插件）

1. 在可被当前 Python 环境 import 的模块中继承 `CodingAgentAdapter`：

```python
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from hq_pilot.agents.base import CodingAgentAdapter, HeadlessResult, HookEvent, Turn


class MyAgentAdapter(CodingAgentAdapter):
    name = "my-agent"
    default_bin = "my-agent"
    index_filename = "AGENTS.md"

    def __init__(self, bin_path: str = "my-agent") -> None:
        self.bin_path = bin_path

    # 实现上述抽象方法与 parse_hook_payload / render_hook_response ...
```

2. 在 `hq.yaml` 中直接指向该类（或通过 `herdr-bot setup --agent-backend my_pkg.adapter:MyAgentAdapter` 配置）：

```yaml
agent:
  backend: "my_pkg.adapter:MyAgentAdapter"
```

### 方式二：使用 `@register_agent_adapter` 装饰器注册

若将适配器作为内置或启动期加载模块，可使用装饰器注册短名：

```python
from hq_pilot.agents.registry import register_agent_adapter

@register_agent_adapter("claude")
class ClaudeCodeAdapter(CodingAgentAdapter):
    ...
```

注册后即可在 `hq.yaml` 中直接配置 `agent.backend: claude`，并自动出现在 `list_agent_backends()` 中。

---

## 5. 新适配器测试要求

提交新适配器时，建议补充以下测试（可参考 `tests/test_codex_adapter.py` 与 `tests/test_agent_registry_and_contracts.py`）：

1. **命令构造测试**：覆盖 headless/interactive 的新建、resume、model、effort、add-dir 与全部 `permission_mode`。
2. **输出与 Transcript 解析测试**：使用脱敏真实样本验证 `parse_headless_output`（含非 JSON 降级）与 `read_transcript_slice`（物理行号游标、噪声过滤）。
3. **Hook 幂等与端到端测试**：验证 `ensure_hooks_installed` 合并保留用户钩子且幂等；通过 `handle_hook_event` 与 `hook_cli.main` 跑通 `session_start -> user_prompt -> stop`（含会话绑定、回复回推、交接摘要注入与 `HQ_EPHEMERAL=1` 后台任务）。
4. **参数化契约测试**：内置注册后端会自动纳入 `tests/test_agent_registry_and_contracts.py::test_builtin_adapter_contract`。
