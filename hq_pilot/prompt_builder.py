"""Prefix-Cache-friendly deterministic prompt builder."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import json

from hq_pilot.config import HQConfig


def _render_jurisdiction_guide(config: HQConfig) -> str:
    project_name = (config.project_name or "").strip() or "current-project"
    managed_repos = sorted(config.repos.keys())
    if managed_repos:
        repos_desc = "、".join(f"`{name}`" for name in managed_repos)
        scope_line = (
            f"- **管辖范围内充分授权**：你对 `hq.yaml` 已注册仓库（{repos_desc}）"
            "以及当前 `$HQ_DIR` 记忆工作区（`AGENTS.md`、`memory/`、`tasks/`、`notes/`）"
            "拥有完整的排查、开发（修改业务仓代码前先切功能分支，勿直接提交主分支）、测试与记忆维护权限。"
        )
    else:
        scope_line = (
            "- **管辖范围内充分授权**：当前尚未在 `hq.yaml` 注册业务仓，你当前仅管辖本 `$HQ_DIR` 记忆工作区"
            "（`AGENTS.md`、`memory/`、`tasks/`、`notes/`），拥有完整的记忆维护与工作区排查权限。"
        )
    return "\n".join(
        [
            "## 0. Role & Jurisdiction",
            f"- **身份定位**：你是项目 `{project_name}` 的专属研发总控 Agent（`#General` 与 `#Topic` 会话均遵守本边界）。",
            scope_line,
            "- **管辖范围外只读参考**：排障时允许**只读**查看系统其他文件、运行日志或外部工具状态作为参考依据。",
            "- **严禁越界修改或自我迭代**：**严禁修改、也严禁主动提议或声称要去修改**任何未在 `hq.yaml` 注册的仓库、外部系统工具或编排器自身（如 `herdr-bot` / `herdr`，严禁自我迭代）。遇到属于编排器或未注册仓库的问题时，仅客观说明现象与建议，提醒用户在对应独立工作区自行处理。",
        ]
    )


GENERAL_ORCHESTRATION_GUIDE = """## 0.5 #General & #Topic Collaboration Guide
- **飞书单窗口只对 `#General`**：用户在飞书主窗口或任意话题内发送的消息一律路由给当前 `#General`，回复统一发回飞书主窗口。
- **默认在 `#General` 连贯完成（永不擅自开 Topic）**：日常问答、状态查询、环境排查、方案讨论及常规代码修改默认均在当前 `#General` 会话连贯完成（可自行派子 Agent 协助；修改业务仓代码前请先在业务仓切换或创建功能分支，勿直接提交主分支）。未经用户明确要求，`#General` 严禁擅自创建新 `#Topic`。
- **仅在用户明确要求时开 `#Topic`**：仅当用户明确提出“单独开个任务 / 新开面板跟进某条线”时，才执行：
  `herdr-bot task spawn <repo> <slug> "<含当前背景与约束的需求>" --hq-dir "$HQ_DIR"`
  拉起独立 Git worktree 与 Herdr 面板（实际多面板并行交互在 Herdr 终端中进行）。
- **手动封口与夜间归档**：`#Topic` 完成开发并验收通过后，可手动调用：
  `herdr-bot seal "$HQ_SESSION_KEY" --hq-dir "$HQ_DIR"`
  静默封口（更新任务状态为 `sealed`，worktree 保留至夜间安全归档）。
- **自主定时唤醒**：需稍后跟进或定期巡检时，可设定一次性或周期性闹钟：
  `herdr-bot timer set --in 15m --reason "<跟进原因>" --hq-dir "$HQ_DIR"`
  （支持 `--at` / `--every`，以及 `timer list` / `timer cancel <id>`）。"""


def _read_optional(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _select_contracts(
    hq_dir: Path,
    config: HQConfig,
    matched_repos: Optional[List[str]] = None,
) -> List[Path]:
    contracts_dir = hq_dir / "memory" / "contracts"
    if not contracts_dir.exists():
        return []
    all_contracts = sorted(p for p in contracts_dir.glob("*.md") if p.is_file())
    if not matched_repos:
        return all_contracts

    repo_set: Set[str] = set(matched_repos)
    selected_names: Set[str] = set()
    for rname in list(repo_set):
        boundary = config.boundaries.repos.get(rname)
        if boundary:
            for dep in boundary.downstream:
                if dep.contract:
                    selected_names.add(Path(dep.contract).name)
                if dep.repo:
                    repo_set.add(dep.repo)

    filtered: List[Path] = []
    for p in all_contracts:
        if p.name in selected_names:
            filtered.append(p)
            continue
        stem_tokens = set(p.stem.lower().split("-"))
        if any(r.lower() in p.stem.lower() or r.lower() in stem_tokens for r in repo_set):
            filtered.append(p)
    return filtered


def build_prompt(
    hq_dir: str | Path,
    config: HQConfig,
    event_payload: Dict[str, Any],
    matched_repos: Optional[List[str]] = None,
    recent_outbound: Optional[List[Dict[str, Any]]] = None,
    topic_slug: Optional[str] = None,
) -> str:
    """
    Deterministically assemble the initial user prompt in strict Prefix-Cache order:
      0. Role & Jurisdiction + #General & #Topic Collaboration Guide
      1. repo-boundaries.yaml (+ optional memory/user-notes.md static block)
      2. Sorted matching memory/contracts/*.md
      3. tasks/active-wip.md (+ optional on-demand tasks/<topic_slug>.md)
      4. notes/today.md (+ optional recent_outbound anti-repeat block)
      5. event_payload (canonical JSON)
    """
    root = Path(hq_dir).resolve()
    sections: List[str] = [
        _render_jurisdiction_guide(config),
        GENERAL_ORCHESTRATION_GUIDE,
    ]

    boundaries_text = _read_optional(root / "repo-boundaries.yaml").rstrip()
    sections.append("## 1. Repo Boundaries\n```yaml\n" + boundaries_text + "\n```")

    user_notes_path = root / "memory" / "user-notes.md"
    has_user_notes_dir = (root / "memory" / "user-notes").is_dir()
    if user_notes_path.is_file() or has_user_notes_dir:
        user_notes_text = _read_optional(user_notes_path).rstrip() if user_notes_path.is_file() else ""
        user_notes_lines = user_notes_text.splitlines()
        if user_notes_path.is_file() and len(user_notes_lines) <= 40 and not has_user_notes_dir:
            sections.append("## 1.5 User Notes\n" + (user_notes_text or "(empty)"))
        else:
            sections.append(
                "## 1.5 User Notes\n"
                f"See `memory/user-notes.md` ({len(user_notes_lines)} lines) "
                "and `memory/user-notes/` for on-demand lookup."
            )

    contract_paths = _select_contracts(root, config, matched_repos=matched_repos)
    contract_blocks: List[str] = []
    for cp in contract_paths:
        rel = cp.relative_to(root).as_posix()
        body = _read_optional(cp).rstrip()
        contract_blocks.append(f"### {rel}\n{body}")
    sections.append(
        "## 2. Relevant Contracts\n"
        + ("\n\n".join(contract_blocks) if contract_blocks else "(none)")
    )

    wip_text = _read_optional(root / "tasks" / "active-wip.md").rstrip()
    sections.append("## 3. Active WIP\n" + (wip_text or "(empty)"))

    if topic_slug:
        task_path = root / "tasks" / f"{topic_slug}.md"
        if task_path.is_file():
            task_text = _read_optional(task_path).rstrip()
            sections.append(
                f"## 3.5 Task Progress ({topic_slug})\n" + (task_text or "(empty)")
            )

    today_text = _read_optional(root / "notes" / "today.md").rstrip()
    sections.append("## 4. Today WAL\n" + (today_text or "(empty)"))

    if recent_outbound:
        outbound_lines: List[str] = []
        for msg in recent_outbound:
            raw_text = " ".join(str(msg.get("text") or "").split())
            if not raw_text:
                continue
            if len(raw_text) > 240:
                raw_text = raw_text[:237] + "..."
            tid = msg.get("thread_id") or "general"
            outbound_lines.append(f"- [thread={tid}] {raw_text}")
        if outbound_lines:
            sections.append(
                "## 4.5 Recent Messages Sent to User (Do Not Repeat)\n"
                + "\n".join(outbound_lines)
            )

    payload_json = json.dumps(
        event_payload, sort_keys=True, ensure_ascii=False, indent=2
    )
    sections.append("## 5. Event Payload\n```json\n" + payload_json + "\n```")

    return "\n\n".join(sections) + "\n"
