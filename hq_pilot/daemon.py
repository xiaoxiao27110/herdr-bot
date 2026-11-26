"""0-LLM deterministic daemon event loop for herdr-bot (100% interactive terminal sessions)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import json
import os
import re
import signal
import subprocess
import time

from hq_pilot.agents.base import CodingAgentAdapter
from hq_pilot.agents.registry import get_agent_adapter
from hq_pilot.archiver import ArchiveReport, archive_eligible_sessions
from hq_pilot.channels.base import InboundMessage, InlineButton, MessagingChannel
from hq_pilot.channels.registry import get_channel
from hq_pilot.config import HQConfig, load_config, load_secrets_env
from hq_pilot.cursors import CursorStore
from hq_pilot.db import EventRow, HQDatabase
from hq_pilot.gatekeeper import (
    detect_manual_touch,
    parse_blocked_prompt,
    scan_distill_candidates,
    scan_pane_blocked,
    scan_repo_commits,
)
from hq_pilot.prompt_builder import build_prompt
from hq_pilot.syncer import sync_hq_repo
from hq_pilot.terminals.base import AgentBlockedError, PaneInfo, TerminalDriver, git_worktree_add
from hq_pilot.terminals.registry import get_terminal_driver


GENERAL_PANE_NAME = "hq-general"
GENERAL_TRACKS = {"GENERAL", "B"}
TOPIC_TRACKS = {"TOPIC", "C"}
GENERAL_ROTATABLE_STATUSES = {"idle", "done", "unknown", "running", ""}
OUTBOX_BACKOFF_SCHEDULE = [10, 30, 120, 300, 600]
DIGEST_CATEGORY_LABELS: Dict[str, str] = {
    "dead_event": "失败事件",
    "dream_error": "夜间整理异常",
    "outbox_dead": "外发投递失败",
    "dirty_worktree": "脏工作区提醒",
    "dirty_stale": "滞留脏工作区",
    "quiet_hours_downgrade": "夜间免打扰汇总",
    "agent_digest": "进展汇总",
}


def _format_digest_category(category: str) -> str:
    raw = (category or "").strip()
    if raw in DIGEST_CATEGORY_LABELS:
        return DIGEST_CATEGORY_LABELS[raw]
    if raw and any(ord(ch) > 127 for ch in raw):
        return raw
    return "系统提醒"


_NOOP_DISTILL_PREFIX_RE = re.compile(
    r"^(?:[-*]\s*)?(?:无新增|暂无新增|没有新增|无可记录|no\s+new\b)",
    re.IGNORECASE,
)
_DISTILL_ITEM_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*`?\[|`?\[(?:DECISION|CONTRACT_CHANGE|WIP)\b)",
    re.IGNORECASE,
)
_DISTILL_COMMENT_LINE_RE = re.compile(r"^\s*<!--\s*distill:[^>]*-->\s*$")
_TODAY_WAL_HEADER_RE = re.compile(r"^\s*#\s*Today\s+WAL\b", re.IGNORECASE)
_ACTIVE_WIP_HEADER_RE = re.compile(r"^\s*#\s*Active\s+Cross-Repo\s+WIP\b", re.IGNORECASE)
_DIGEST_HEADING_RE = re.compile(r"^\s*#{1,4}\s*.*晨报")
_FENCE_BLOCK_RE = re.compile(
    r"^\s*```(?:markdown|md)?\s*\n(.*?)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)


def _is_noop_distill_reply(reply: str) -> bool:
    """Return True when a distill reply carries no new structured entries (e.g. '无新增 ...')."""
    text = (reply or "").strip()
    if not text:
        return True
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return True
    if any(_DISTILL_ITEM_LINE_RE.match(ln) for ln in lines):
        return False
    return bool(_NOOP_DISTILL_PREFIX_RE.match(lines[0]))


def _clean_wal_text(raw: str) -> str:
    """Strip default WAL header, distill HTML comments, and noop distill noise lines."""
    if not raw:
        return ""
    kept: List[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if _DISTILL_COMMENT_LINE_RE.match(stripped):
            continue
        if _TODAY_WAL_HEADER_RE.match(stripped):
            continue
        if not _DISTILL_ITEM_LINE_RE.match(stripped) and _NOOP_DISTILL_PREFIX_RE.match(stripped):
            continue
        kept.append(line.rstrip())
    return "\n".join(kept).strip()


def _clean_active_wip_text(raw: str) -> str:
    """Return stripped active-wip content, or empty string if only the default heading is present."""
    if not raw:
        return ""
    substantive = [
        ln for ln in raw.splitlines()
        if ln.strip() and not _ACTIVE_WIP_HEADER_RE.match(ln.strip())
    ]
    if not substantive:
        return ""
    return raw.strip()


def _strip_outer_markdown_fence(text: str) -> str:
    cleaned = (text or "").strip()
    match = _FENCE_BLOCK_RE.match(cleaned)
    if match:
        return match.group(1).strip()
    return cleaned


def _run_readonly_git(repo_path: Path, args: List[str]) -> Optional[str]:
    try:
        res = subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout


def _format_git_log_lines(raw_log: str) -> List[str]:
    lines: List[str] = []
    for raw in (raw_log or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if " | " in stripped:
            refs, subject = stripped.split(" | ", 1)
            refs = refs.strip()
            subject = subject.strip()
            if refs and subject:
                lines.append(f"{refs} | {subject}")
            elif subject:
                lines.append(subject)
            elif refs:
                lines.append(refs)
        else:
            lines.append(stripped)
    return lines


def _has_substantive_digest_material(ctx: Dict[str, Any]) -> bool:
    if (ctx.get("yesterday_archive") or "").strip():
        return True
    if (ctx.get("today_wal") or "").strip():
        return True
    for rs in ctx.get("repo_states") or []:
        if (
            rs.get("recent_commits")
            or rs.get("unpushed_branches")
            or int(rs.get("dirty_files_count") or 0) > 0
        ):
            return True
    return False


def _truncate_digest_block(text: str, max_chars: int = 12000) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + "\n...(已截断)"


_parse_blocked_prompt = parse_blocked_prompt


def _build_busy_queue_card_text(busy_kind: str, hint: str, count: int) -> str:
    """Format button-less Feishu queuing card text for a busy `#General` batch."""
    _ = hint
    n = max(1, int(count))
    if busy_kind == "blocked":
        if n <= 1:
            return "⏸️ 正在等待审批，请先点击审批卡片确认；消息已排队"
        return f"⏸️ 正在等待审批，请先点击审批卡片确认；消息已排队（共 {n} 条）"
    if n <= 1:
        return "⏳ 收到，正在排队处理..."
    return f"⏳ 收到，正在排队处理（共 {n} 条）..."


def _format_merged_channel_batch(events: List[EventRow]) -> str:
    """Merge chronological queued Feishu messages into one turn (no prefix if single)."""
    if not events:
        return ""
    if len(events) == 1:
        return str(events[0].payload.get("text") or "")
    lines: List[str] = [f"[排队期间你陆续发来以下 {len(events)} 条消息，请一并回复]"]
    for idx, item in enumerate(events, start=1):
        ts_val = int(item.payload.get("received_at") or item.created_at or 0)
        hhmm = datetime.fromtimestamp(ts_val).strftime("%H:%M") if ts_val > 0 else "--:--"
        msg_text = str(item.payload.get("text") or "").strip()
        lines.append(f"{idx}. ({hhmm}) {msg_text}")
    return "\n".join(lines)


def _upsert_task_progress_file(
    hq_dir: Path,
    slug: str,
    repo_name: str,
    branch_name: str,
    session_key: str,
    prompt: str,
    status: str = "active",
) -> Path:
    """Create or update per-task progress file `tasks/<slug>.md`."""
    tasks_dir = Path(hq_dir) / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    task_path = tasks_dir / f"{slug}.md"
    if task_path.exists():
        lines = task_path.read_text(encoding="utf-8").splitlines()
        updated = False
        new_lines: List[str] = []
        for line in lines:
            if line.strip().startswith("- status:"):
                new_lines.append(f"- status: {status}")
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f"- status: {status}")
        task_path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
        return task_path

    compact_prompt = " ".join((prompt or "").split())
    content = (
        f"# Task: {slug}\n\n"
        f"- repo: {repo_name}\n"
        f"- branch: {branch_name}\n"
        f"- session_key: {session_key}\n"
        f"- status: {status}\n"
        f"- prompt: {compact_prompt}\n\n"
        "## Done\n"
        "-\n\n"
        "## Left\n"
        "-\n"
    )
    task_path.write_text(content, encoding="utf-8")
    return task_path


def _upsert_active_wip_index(
    hq_dir: Path,
    slug: str,
    repo_name: str,
    branch_name: str,
    status: str = "active",
) -> Path:
    """Maintain single-line per-task index entry in `tasks/active-wip.md`."""
    wip_path = Path(hq_dir) / "tasks" / "active-wip.md"
    wip_path.parent.mkdir(parents=True, exist_ok=True)
    entry = f"- [{slug}](tasks/{slug}.md) | repo={repo_name} | branch={branch_name} | status={status}"
    prefix = f"- [{slug}](tasks/{slug}.md)"
    if wip_path.exists():
        lines = wip_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = ["# Active Cross-Repo WIP", ""]
    replaced = False
    new_lines: List[str] = []
    for line in lines:
        if line.strip().startswith(prefix):
            new_lines.append(entry)
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(entry)
    wip_path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
    return wip_path

CmdRunner = Callable[[List[str], Path], subprocess.CompletedProcess[str]]


def default_cmd_runner(cmd: List[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


class _EventParked(Exception):
    """Internal control-flow signal when an event is parked in `waiting_general`."""


@dataclass
class TickSummary:
    events_processed: int = 0
    events_failed: int = 0
    events_parked: int = 0
    distill_enqueued: int = 0
    commits_enqueued: int = 0
    timers_fired: int = 0
    nightly_ran: bool = False
    general_rotated: bool = False
    digest_sent: bool = False
    outbox_sent: int = 0


class HQDaemon:
    def __init__(
        self,
        hq_dir: str | Path,
        agent_adapter: Optional[CodingAgentAdapter] = None,
        terminal_driver: Optional[TerminalDriver] = None,
        channel: Optional[MessagingChannel] = None,
        cmd_runner: Optional[CmdRunner] = None,
    ) -> None:
        self.hq_dir = Path(hq_dir).expanduser().resolve()
        self.config: HQConfig = load_config(self.hq_dir)
        self.db = HQDatabase(self.hq_dir / ".hq" / "runtime.sqlite")
        self.cursors = CursorStore(self.hq_dir / ".hq" / "cursors.json")
        self.secrets = load_secrets_env(self.hq_dir)

        self.agent_adapter: CodingAgentAdapter = agent_adapter or get_agent_adapter(
            self.config.agent.backend,
            bin_path=self.config.local.bin_overrides.get(self.config.agent.backend),
        )
        self.terminal_driver: TerminalDriver = terminal_driver or get_terminal_driver(
            self.config.terminal.backend,
            bin_path=self.config.local.bin_overrides.get(self.config.terminal.backend),
        )
        self.channel: Optional[MessagingChannel] = (
            channel
            if channel is not None
            else get_channel(
                self.config.channel.backend,
                secrets=self.secrets,
                allowed_chat_id=self.config.channel.allowed_chat_id,
                db=self.db,
            )
        )
        # Retained only for constructor compatibility; daemon never invokes headless CLI runs.
        self.cmd_runner: CmdRunner = cmd_runner or default_cmd_runner
        self._stop_requested = False
        self._ephemeral_panes_used: Set[str] = set()
        self._seen_live_manual_panes: Set[str] = set()
        self.last_digest_text: str = ""

    def close(self) -> None:
        self.db.close()

    def _hook_bin_path(self) -> str:
        return self.config.local.bin_overrides.get(
            "hq-agent-hook",
            str(Path(__file__).resolve().parent.parent / "bin" / "hq-agent-hook"),
        )

    # ------------------------------------------------------------------
    # Channel Inbound Handling
    # ------------------------------------------------------------------
    def _handle_inbound(self, msg: InboundMessage, now: int) -> None:
        if msg.is_callback and msg.callback_data:
            self._handle_callback(msg, now)
            return

        text = msg.text.strip()
        if not text:
            return

        if msg.reply_to_msg_id:
            action = self.db.get_deferred_action(msg.reply_to_msg_id)
            if action and action.get("resolved_at") is None:
                self.db.resolve_deferred_action(msg.reply_to_msg_id, now=now)
                self.db.enqueue_event(
                    source="deferred_resume",
                    dedupe_key=f"defer_reply:{msg.reply_to_msg_id}:{msg.update_id}",
                    priority=10,
                    available_at=now,
                    payload={
                        "session_key": action["session_key"],
                        "choice_id": "reply",
                        "choice_label": text,
                        "thread_id": msg.thread_id,
                    },
                    now=now,
                )
                return

        if text.startswith("/task "):
            parts = text.split(None, 3)
            if len(parts) >= 4:
                _, repo_name, slug, task_prompt = parts
                sess = self.spawn_task(repo_name, slug, task_prompt, now=now)
                if self.channel:
                    self.channel.send_message(
                        f"已启动任务 `{sess['session_key']}`（面板 `{sess['pane_name']}`，分支 `{sess['branch_name']}`），"
                        "可在 herdr 查看。"
                    )
                return

        # Single-window Feishu routing: ignore `msg.thread_id`, always route to `#General`
        msg_uid = msg.message_id or f"{now}:{msg.update_id}"
        self.db.enqueue_event(
            source="channel",
            dedupe_key=f"channel:{msg.chat_id}:{msg_uid}",
            priority=10,
            available_at=now,
            payload={
                "update_id": msg.update_id,
                "message_id": msg.message_id,
                "chat_id": msg.chat_id,
                "thread_id": None,
                "sender_id": msg.sender_id,
                "text": text,
                "received_at": now,
            },
            now=now,
        )

    def _handle_callback(self, msg: InboundMessage, now: int) -> None:
        data = msg.callback_data or ""
        if self.channel and msg.callback_id:
            self.channel.answer_callback(msg.callback_id, "已收到")

        if data.startswith("defer:"):
            rest = data[len("defer:"):]
            if ":" in rest:
                skey, choice_idx = rest.rsplit(":", 1)
            else:
                skey, choice_idx = rest, ""
            choice_label = choice_idx
            existing_action = (
                self.db.get_deferred_action(msg.message_id) if msg.message_id else None
            )
            if msg.message_id and existing_action is None:
                sess_check = self.db.get_session(skey) if skey else None
                if not sess_check or not sess_check.get("suspended_msg_id"):
                    if self.channel:
                        self.channel.send_message(
                            "该操作卡片已过期或已处理，请直接回复最新消息。",
                            thread_id=msg.thread_id
                            or (sess_check.get("channel_thread_id") if sess_check else None),
                        )
                    return
            if existing_action is not None and (
                existing_action.get("resolved_at") is not None
                or int(existing_action.get("expires_at") or 0) <= now
            ):
                orig = self.db.get_session(str(existing_action["session_key"]))
                if self.channel:
                    self.channel.send_message(
                        "该操作卡片已过期或已处理，请直接回复最新消息。",
                        thread_id=msg.thread_id
                        or (orig.get("channel_thread_id") if orig else None),
                    )
                return

            action = (
                self.db.resolve_deferred_action(msg.message_id, now=now)
                if msg.message_id
                else None
            )
            if action:
                skey = str(action["session_key"])
                for opt in action.get("options") or []:
                    if str(opt.get("id")) == str(choice_idx):
                        choice_label = str(opt.get("label") or choice_idx)
                        break
            elif skey:
                self.db.update_session(skey, suspended_msg_id=None)

            orig = self.db.get_session(skey) if skey else None
            reply_thread_id = msg.thread_id or (
                orig.get("channel_thread_id") if orig else None
            )
            if self.channel:
                self.channel.send_message(
                    f"已收到你的选择：{choice_label}",
                    thread_id=reply_thread_id,
                )

            if skey:
                self.db.enqueue_event(
                    source="deferred_resume",
                    dedupe_key=f"defer_cb:{skey}:{msg.update_id}",
                    priority=10,
                    available_at=now,
                    payload={
                        "session_key": skey,
                        "choice_id": choice_idx,
                        "choice_label": choice_label,
                        "thread_id": reply_thread_id,
                    },
                    now=now,
                )
            return

        if data.startswith("approve_key:"):
            rest = data[len("approve_key:"):]
            if ":" in rest:
                skey, action_id = rest.rsplit(":", 1)
            else:
                skey, action_id = rest, ""

            def _reply_approval_status(target_card_id: Optional[str], status_text: str) -> None:
                if self.channel is None:
                    return
                if target_card_id and self.channel.finish_working_indicator(
                    str(target_card_id), status_text, thread_id=None
                ):
                    return
                self.channel.send_message(status_text, thread_id=None)

            raw_meta = self.db.get_meta(f"blocked_approval_card:{skey}") if skey else None
            card_meta: Optional[Dict[str, Any]] = None
            if raw_meta:
                try:
                    parsed_meta = json.loads(raw_meta)
                    if isinstance(parsed_meta, dict):
                        card_meta = parsed_meta
                except json.JSONDecodeError:
                    card_meta = None

            actions_map = card_meta.get("actions") if isinstance(card_meta, dict) else None
            action_info = (
                actions_map.get(action_id)
                if isinstance(actions_map, dict) and action_id
                else None
            )
            saved_card_id = (
                str(card_meta.get("card_id") or "")
                if isinstance(card_meta, dict)
                else ""
            )
            if (
                card_meta is None
                or not isinstance(action_info, dict)
                or (
                    msg.message_id
                    and saved_card_id
                    and str(msg.message_id) != saved_card_id
                )
            ):
                _reply_approval_status(msg.message_id, "该审批已处理或已失效。")
                return

            pane_name = str(card_meta.get("pane_name") or "").strip()
            screen_hash = str(card_meta.get("screen_hash") or "")
            is_still_blocked = False
            for p in self.terminal_driver.list_panes():
                if (
                    p.name == pane_name
                    and self._is_pane_live(p)
                    and str(p.status or "").strip().lower() == "blocked"
                ):
                    is_still_blocked = True
                    break

            if not is_still_blocked:
                self.db.delete_meta(f"blocked_approval_card:{skey}")
                _reply_approval_status(
                    saved_card_id or msg.message_id, "该审批已处理或已失效。"
                )
                return

            keys = [str(k) for k in (action_info.get("keys") or []) if str(k).strip()]
            label = str(action_info.get("label") or action_id)
            is_negative = bool(action_info.get("negative")) or action_id in {"deny", "cancel"}
            self.terminal_driver.send_keys(pane_name, keys)
            self.db.delete_meta(f"blocked_approval_card:{skey}")
            self.db.set_meta(
                f"blocked_last_resolved:{skey}",
                json.dumps({"screen_hash": screen_hash, "resolved_at": now}, ensure_ascii=False),
                now=now,
            )
            prev_epoch = int(self.db.get_meta(f"blocked_epoch:{skey}") or "0")
            self.db.set_meta(f"blocked_epoch:{skey}", str(prev_epoch + 1), now=now)

            if is_negative:
                done_text = f"❌ 已执行操作（{label}）"
            else:
                done_text = f"✅ 已执行审批（{label}），继续执行中..."
            _reply_approval_status(saved_card_id or msg.message_id, done_text)
            return

        if data.startswith("pane_continue:") or data.startswith("pane_seal:"):
            is_seal = data.startswith("pane_seal:")
            skey = data.split(":", 1)[1]
            label = "封口结束" if is_seal else "继续执行"
            existing_action = (
                self.db.get_deferred_action(msg.message_id) if msg.message_id else None
            )
            if existing_action is not None and (
                existing_action.get("resolved_at") is not None
                or int(existing_action.get("expires_at") or 0) <= now
            ):
                orig = self.db.get_session(str(existing_action["session_key"]))
                if self.channel:
                    self.channel.send_message(
                        "该操作卡片已过期或已处理，请直接回复最新消息。",
                        thread_id=msg.thread_id
                        or (orig.get("channel_thread_id") if orig else None),
                    )
                return
            if msg.message_id and existing_action is not None:
                self.db.resolve_deferred_action(msg.message_id, now=now)
            elif skey:
                self.db.update_session(skey, suspended_msg_id=None)
            orig = self.db.get_session(skey) if skey else None
            reply_thread_id = msg.thread_id or (
                orig.get("channel_thread_id") if orig else None
            )
            if self.channel:
                self.channel.send_message(
                    f"已收到你的选择：{label}",
                    thread_id=reply_thread_id,
                )
            if is_seal and orig:
                self.seal_session(str(orig["session_key"]), now=now)
            return

        if data.startswith("busy_wait:") or data.startswith("busy_fork:"):
            if self.channel:
                self.channel.send_message(
                    "该按钮已失效，消息已在排队",
                    thread_id=None,
                )
            return

        if data.startswith("seal_new:") or data.startswith("seal_pin:"):
            if self.channel:
                self.channel.send_message(
                    "该按钮已失效，请直接在主窗口发送消息。",
                    thread_id=None,
                )
            return

        if msg.message_id:
            self.db.resolve_deferred_action(msg.message_id, now=now)

    def spawn_task(
        self,
        repo_name: str,
        slug: str,
        prompt: str,
        now: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Spawn a #Topic interactive worktree + TerminalDriver agent pane."""
        ts = int(now if now is not None else time.time())
        repo_path = self.config.get_repo_path(repo_name)
        base_branch = (
            self.config.repos[repo_name].default_branch
            if repo_name in self.config.repos
            else "main"
        )
        branch_name = f"hq/{slug}"
        wt_path = self.hq_dir / ".hq" / "worktrees" / f"{repo_name}-{slug}"
        git_worktree_add(repo_path, wt_path, branch_name, base_branch=base_branch)

        pane_name = f"hq-{slug}"
        skey = f"task:{repo_name}:{slug}:{ts}"
        sess = self.db.create_session(
            session_key=skey,
            track="TOPIC",
            agent_backend=self.agent_adapter.name,
            status="active",
            channel_thread_id=None,
            topic_slug=slug,
            repo_name=repo_name,
            branch_name=branch_name,
            worktree_path=str(wt_path),
            pane_name=pane_name,
            now=ts,
        )

        delivery_prompt = (
            self._materialize_long_prompt(prompt, tag=f"task-{slug}", now=ts)
            if prompt
            else ""
        )
        self.agent_adapter.ensure_hooks_installed(wt_path, self._hook_bin_path())
        self.db.record_dispatch(skey, None, delivery_prompt, now=ts)
        self.db.set_meta(f"last_input_origin:{skey}", "daemon", now=ts)
        _upsert_task_progress_file(
            self.hq_dir, slug, repo_name, branch_name, skey, prompt, status="active"
        )
        _upsert_active_wip_index(
            self.hq_dir, slug, repo_name, branch_name, status="active"
        )

        interactive_cmd = self.agent_adapter.build_interactive_cmd(
            initial_prompt=None,
            model=self.config.agent.model,
            effort=self.config.agent.default_effort,
        )
        trust_args = self.agent_adapter.extra_trust_args(
            [wt_path, self.hq_dir],
            permission_mode=self.config.agent.permission_mode,
        )
        self.terminal_driver.start_agent(
            name=pane_name,
            cwd=wt_path,
            kind=self.agent_adapter.name,
            agent_args=[*interactive_cmd[1:], *trust_args],
            env={
                "HQ_DIR": str(self.hq_dir),
                "HQ_SESSION_KEY": skey,
            },
        )
        if delivery_prompt:
            self.terminal_driver.prompt_agent(pane_name, delivery_prompt, wait=False)
        return sess

    # ------------------------------------------------------------------
    # Two-Tier Interactive Session Management (#General + #Topic)
    # ------------------------------------------------------------------
    def _create_next_general_session(self, now: int) -> Dict[str, Any]:
        seq = int(self.db.get_meta("general_seq", "0") or "0") + 1
        while self.db.get_session(f"general:{seq}") is not None:
            seq += 1
        self.db.set_meta("general_seq", str(seq), now=now)
        return self.db.create_session(
            session_key=f"general:{seq}",
            track="GENERAL",
            agent_backend=self.agent_adapter.name,
            status="active",
            pane_name=GENERAL_PANE_NAME,
            agent_session_id=None,
            transcript_path=None,
            now=now,
        )

    def _ensure_general_session(self, now: int) -> Dict[str, Any]:
        for s in self.db.list_sessions(statuses=["active", "pinned"]):
            if s["track"] in GENERAL_TRACKS:
                if s.get("pane_name") != GENERAL_PANE_NAME:
                    self.db.update_session(
                        str(s["session_key"]), pane_name=GENERAL_PANE_NAME
                    )
                    s["pane_name"] = GENERAL_PANE_NAME
                return s
        return self._create_next_general_session(now)

    def _get_valid_transcript_mtime(self, session: Dict[str, Any], now: int) -> int:
        skey = str(session["session_key"])
        override = self.db.get_meta(f"transcript_mtime:{skey}")
        if override is not None:
            try:
                return int(override)
            except ValueError:
                pass
        tpath = session.get("transcript_path")
        if tpath and Path(str(tpath)).exists():
            try:
                st_mtime = int(Path(str(tpath)).stat().st_mtime)
                if st_mtime <= now + 5:
                    return st_mtime
            except OSError:
                pass
        return 0

    def _get_transcript_size_bytes(self, session: Dict[str, Any]) -> int:
        skey = str(session["session_key"])
        override = self.db.get_meta(f"transcript_size_bytes:{skey}")
        if override is not None:
            try:
                return max(0, int(override))
            except ValueError:
                pass
        tpath = session.get("transcript_path")
        if tpath and Path(str(tpath)).exists():
            try:
                return int(Path(str(tpath)).stat().st_size)
            except OSError:
                pass
        return 0

    def _get_current_work_hint(self, session: Dict[str, Any]) -> str:
        skey = str(session["session_key"])
        hint = (self.db.get_meta(f"current_work_hint:{skey}") or "").strip()
        if hint:
            return f"「{hint}」"
        return "当前任务"

    @staticmethod
    def _is_pane_live(pane: PaneInfo) -> bool:
        return (
            pane.agent_live is not False
            and str(pane.status or "").strip().lower() != "exited"
        )

    def _find_live_pane(self, pane_name: str) -> Optional[PaneInfo]:
        for p in self.terminal_driver.list_panes():
            if p.name == pane_name and self._is_pane_live(p):
                return p
        return None

    def _inspect_session_busy(
        self, session: Dict[str, Any], now: int
    ) -> Tuple[bool, str, str]:
        """
        Determine whether `session`'s interactive pane is busy:
          1. `daemon_waiting` flag set on session or pane
          2. Herdr / terminal pane status is `working` or `blocked`
          3. Transcript had post-Stop writes within `transcript_busy_window_sec`
             (covers background subagents running while terminal status shows `idle`).
        """
        skey = str(session["session_key"])
        pane_name = self._resolve_pane_name(session)
        hint = self._get_current_work_hint(session)

        if (
            self.db.get_meta(f"daemon_waiting:{skey}") == "1"
            or self.db.get_meta(f"daemon_waiting_pane:{pane_name}") == "1"
        ):
            return True, "daemon_waiting", hint

        pane_info = self._find_live_pane(pane_name)
        if pane_info is not None:
            pstatus = str(pane_info.status or "").strip().lower()
            if pstatus == "blocked":
                return True, "blocked", hint
            if pstatus == "working":
                return True, "working", hint

        win = int(self.config.tuning.transcript_busy_window_sec)
        tpath = session.get("transcript_path")
        if win > 0 and tpath and Path(str(tpath)).exists():
            mtime = self._get_valid_transcript_mtime(session, now)
            if mtime > 0 and 0 <= (now - mtime) < win:
                total_lines = self.agent_adapter.count_transcript_lines(str(tpath))
                reply_offset = int(session.get("reply_line_offset") or 0)
                last_stop_mtime = int(self.db.get_meta(f"last_stop_mtime:{skey}") or "0")
                if total_lines > reply_offset or (
                    last_stop_mtime > 0 and mtime > last_stop_mtime
                ):
                    return True, "transcript_active", hint

        return False, "", ""

    def _build_general_handoff_summary(
        self, current: Dict[str, Any], now: int
    ) -> str:
        """Build a concise handoff summary before rotating `#General`."""
        _ = now
        lines: List[str] = []
        tpath = current.get("transcript_path")
        if tpath and Path(str(tpath)).exists():
            turns, _ = self.agent_adapter.read_transcript_slice(str(tpath), from_line=0)
            recent_turns = turns[-4:]
            if recent_turns:
                lines.append("### 上一轮总管会话最近上下文")
                for t in recent_turns:
                    role_label = "用户" if t.role == "user" else "总管"
                    clean = " ".join(t.text.split())
                    if len(clean) > 180:
                        clean = clean[:177] + "..."
                    lines.append(f"- {role_label}：{clean}")

        if not lines:
            recent_out = self._get_recent_outbound()[-3:]
            if recent_out:
                lines.append("### 最近已向用户汇报的结论")
                for m in recent_out:
                    clean = " ".join(str(m.get("text") or "").split())
                    if clean:
                        if len(clean) > 180:
                            clean = clean[:177] + "..."
                        lines.append(f"- {clean}")

        active_topics = [
            s
            for s in self.db.list_sessions(statuses=["active", "pinned"])
            if s.get("track") in TOPIC_TRACKS
        ]
        if active_topics:
            lines.append("### 当前在途独立 Topic 任务")
            for tsess in active_topics[:6]:
                lines.append(
                    f"- `{tsess['session_key']}` | 面板 `{tsess.get('pane_name') or '-'}` "
                    f"| 分支 `{tsess.get('branch_name') or '-'}` | 状态 `{tsess['status']}`"
                )

        active_timers = self.db.list_timers(statuses=["active"])
        if active_timers:
            lines.append("### 当前待触发定时器")
            for tm in active_timers[:6]:
                lines.append(
                    f"- timer#{tm['id']} ({tm['kind']}, next={tm['next_fire_at']}): {tm['reason']}"
                )

        if not lines:
            lines.append("- 上一轮会话已正常收口，详见 `notes/` 与 `tasks/active-wip.md`。")
        return "\n".join(lines)

    def rotate_general_session(
        self, now: int, force: bool = False, reason: str = "night"
    ) -> bool:
        """Rotate `#General` in-place via `/new`, migrating timers and saving handoff summary."""
        current: Optional[Dict[str, Any]] = None
        for s in self.db.list_sessions(statuses=["active", "pinned"]):
            if s["track"] in GENERAL_TRACKS:
                current = s
                break
        if current is None:
            return False

        skey = str(current["session_key"])
        pane_name = str(current.get("pane_name") or GENERAL_PANE_NAME)
        pane_info = self._find_live_pane(pane_name)

        pane_status = (
            str(pane_info.status or "").strip().lower() if pane_info is not None else ""
        )
        pane_is_live = pane_info is not None

        if not force:
            if pane_is_live and pane_status not in GENERAL_ROTATABLE_STATUSES:
                return False
            is_busy, _, _ = self._inspect_session_busy(current, now)
            if is_busy:
                return False
            if self.db.has_queued_channel_events():
                return False

        handoff_summary = self._build_general_handoff_summary(current, now)

        if pane_is_live:
            try:
                self.terminal_driver.send_text(pane_name, "/new", enter=True)
            except Exception as exc:
                print(
                    f"[hq-daemon] warn: failed to send /new to {pane_name}: {exc}",
                    flush=True,
                )
                return False

        if int(current.get("pinned") or 0) != 0 or current.get("status") == "pinned":
            self.db.unpin_session(skey, target_status="sealed", now=now)
            self.db.update_session(skey, sealed_at=now, last_active_at=now)
        else:
            self.db.seal_session(skey, now=now)

        self.db.delete_meta(f"last_reply:{skey}")
        self.db.delete_meta(f"last_reply_pane:{pane_name}")
        self.db.delete_meta(f"last_input_origin:{skey}")
        self.db.delete_meta(f"pending_approval_reply:{skey}")
        self.db.delete_meta(f"blocked_approval_card:{skey}")
        self.db.delete_meta(f"blocked_last_resolved:{skey}")
        self.db.delete_meta(f"current_work_hint:{skey}")

        new_sess = self._create_next_general_session(now)
        new_key = str(new_sess["session_key"])
        self.db.migrate_session_timers(skey, new_key)
        self.db.delete_meta(f"last_reply:{new_key}")
        self.db.delete_meta(f"last_input_origin:{new_key}")

        norm_reason = "size" if reason == "size" else "night"
        self.db.set_meta("general_handoff_summary", handoff_summary, now=now)
        self.db.set_meta("general_handoff_reason", norm_reason, now=now)
        self.db.set_meta("general_handoff_pending", "1", now=now)
        self.db.set_meta("general_rotation_notice_pending", norm_reason, now=now)
        self.db.delete_meta("general_rotation_due")
        return True

    def _maybe_run_due_general_rotation(self, now: int) -> bool:
        """Execute due `#General` rotation (night or daytime size/turns) when idle, no queued Feishu msgs, and quiet >= threshold."""
        due = (self.db.get_meta("general_rotation_due") or "").strip()
        if due not in {"1", "night", "size"}:
            return False
        current: Optional[Dict[str, Any]] = None
        for s in self.db.list_sessions(statuses=["active", "pinned"]):
            if s["track"] in GENERAL_TRACKS:
                current = s
                break
        if current is None:
            self.db.delete_meta("general_rotation_due")
            return False

        if self.db.has_queued_channel_events():
            return False

        is_busy, _, _ = self._inspect_session_busy(current, now)
        if is_busy:
            return False

        if due == "size":
            quiet_min = int(self.config.tuning.general_rotate_quiet_minutes)
            rot_reason = "size"
        else:
            quiet_min = int(self.config.tuning.general_rotation_quiet_minutes)
            rot_reason = "night"

        quiet_sec = max(0, quiet_min) * 60
        last_act = max(
            int(current.get("last_active_at") or 0),
            self._get_valid_transcript_mtime(current, now),
        )
        if quiet_sec > 0 and (now - last_act) < quiet_sec:
            return False

        return self.rotate_general_session(now, force=False, reason=rot_reason)

    def _seal_idle_general_sessions(self, now: int) -> None:
        idle_limit = self.config.tuning.general_idle_seal_minutes * 60
        max_turns = self.config.tuning.general_max_turns
        max_mb = self.config.tuning.general_rotate_transcript_mb
        max_bytes = max_mb * 1024 * 1024 if max_mb > 0 else 0

        for s in self.db.list_sessions(statuses=["active"]):
            if s["track"] not in GENERAL_TRACKS:
                continue
            last_act = max(
                int(s["last_active_at"]),
                self._get_valid_transcript_mtime(s, now),
            )
            idle_sec = now - last_act
            idle_exceeded = idle_limit > 0 and idle_sec >= idle_limit
            turns_exceeded = max_turns > 0 and int(s["turn_count"]) >= max_turns
            size_exceeded = max_bytes > 0 and self._get_transcript_size_bytes(s) >= max_bytes

            if turns_exceeded or size_exceeded:
                existing_due = (self.db.get_meta("general_rotation_due") or "").strip()
                if existing_due not in {"1", "night", "size"}:
                    self.db.set_meta("general_rotation_due", "size", now=now)

            if idle_exceeded and not self.db.has_queued_channel_events():
                self.rotate_general_session(now, force=False, reason="night")
                break

    def seal_session(
        self,
        session_key: str,
        reason: str = "",
        now: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Silently seal a session (preserving its worktree until nightly safe archive)."""
        _ = reason
        ts = int(now if now is not None else time.time())
        sess = self.db.find_session(session_key)
        if not sess:
            raise KeyError(f"Session not found: {session_key}")
        skey = str(sess["session_key"])
        if sess["status"] == "archived":
            return sess

        was_sealed = sess["status"] == "sealed"
        if int(sess.get("pinned") or 0) != 0:
            self.db.unpin_session(skey, target_status="sealed", now=ts)
            self.db.update_session(skey, sealed_at=ts, last_active_at=ts)
        elif not was_sealed:
            self.db.seal_session(skey, now=ts)

        updated = self.db.get_session(skey) or sess
        if updated.get("topic_slug"):
            t_slug = str(updated["topic_slug"])
            t_repo = str(updated.get("repo_name") or "")
            t_branch = str(updated.get("branch_name") or "")
            _upsert_task_progress_file(
                self.hq_dir, t_slug, t_repo, t_branch, skey, "", status="sealed"
            )
            _upsert_active_wip_index(
                self.hq_dir, t_slug, t_repo, t_branch, status="sealed"
            )
        return updated

    def _resolve_pane_name(self, session: Dict[str, Any]) -> str:
        existing = session.get("pane_name")
        if existing:
            return str(existing)
        if session.get("track") in GENERAL_TRACKS:
            pane_name = GENERAL_PANE_NAME
        elif session.get("topic_slug"):
            pane_name = f"hq-{session['topic_slug']}"
        elif session.get("channel_thread_id"):
            pane_name = f"hq-{session['channel_thread_id']}"
        else:
            pane_name = GENERAL_PANE_NAME
        self.db.update_session(str(session["session_key"]), pane_name=pane_name)
        session["pane_name"] = pane_name
        return pane_name

    def _ensure_session_pane(
        self,
        session: Dict[str, Any],
        cwd: Path,
        effort: str,
    ) -> str:
        fresh = self.db.get_session(str(session["session_key"])) or session
        pane_name = self._resolve_pane_name(fresh)
        session["pane_name"] = pane_name
        if self._find_live_pane(pane_name) is None:
            if (
                not cwd.exists()
                and fresh.get("track") in TOPIC_TRACKS
                and fresh.get("repo_name")
                and fresh.get("branch_name")
            ):
                rname = str(fresh["repo_name"])
                rpath = self.config.get_repo_path(rname)
                if (rpath / ".git").exists():
                    base_br = (
                        self.config.repos[rname].default_branch
                        if rname in self.config.repos
                        else "main"
                    )
                    git_worktree_add(rpath, cwd, str(fresh["branch_name"]), base_branch=base_br)
            cwd.mkdir(parents=True, exist_ok=True)
            self.agent_adapter.ensure_hooks_installed(cwd, self._hook_bin_path())
            add_dirs = (
                [
                    self.config.get_repo_path(r)
                    for r in sorted(self.config.repos.keys())
                    if self.config.get_repo_path(r).exists()
                ]
                if cwd.resolve() == self.hq_dir.resolve()
                else None
            )
            interactive_cmd = self.agent_adapter.build_interactive_cmd(
                initial_prompt=None,
                session_id=fresh.get("agent_session_id"),
                model=self.config.agent.model,
                effort=effort,
                add_dirs=add_dirs,
            )
            trust_args = self.agent_adapter.extra_trust_args(
                [cwd, *(add_dirs or [])],
                permission_mode=self.config.agent.permission_mode,
            )
            self.terminal_driver.start_agent(
                name=pane_name,
                cwd=cwd,
                kind=self.agent_adapter.name,
                agent_args=[*interactive_cmd[1:], *trust_args],
                env={
                    "HQ_DIR": str(self.hq_dir),
                    "HQ_SESSION_KEY": str(fresh["session_key"]),
                },
            )
        return pane_name

    def _ensure_general_pane(self, now: Optional[int] = None) -> Dict[str, Any]:
        """Ensure `#General` (`hq-general`) interactive agent pane is running and ready."""
        ts = int(now if now is not None else time.time())
        sess = self._ensure_general_session(ts)
        self._ensure_session_pane(
            sess,
            cwd=self.hq_dir,
            effort=self.config.agent.default_effort,
        )
        return sess

    def _restore_manual_herdr_sessions(self, now: Optional[int] = None) -> List[str]:
        """Restore manual Herdr agent sessions in-place when their panes recover as bare shells."""
        ts = int(now if now is not None else time.time())
        bindings = self.db.list_meta_prefix("herdr_manual_pane:")
        if not bindings:
            return []

        ensure_fn = getattr(self.terminal_driver, "ensure_server", None)
        if callable(ensure_fn):
            ensure_fn()

        all_panes = self.terminal_driver.list_panes()
        general_pane_names: Set[str] = {GENERAL_PANE_NAME}
        for s in self.db.list_sessions(statuses=["active", "pinned"]):
            if s.get("track") in GENERAL_TRACKS and s.get("pane_name"):
                general_pane_names.add(str(s["pane_name"]).strip())
        general_pane_ids: Set[str] = {
            p.pane_id for p in all_panes if p.pane_id and p.name in general_pane_names
        }

        unique_panes: List[PaneInfo] = []
        panes_by_id: Dict[str, PaneInfo] = {}
        for p in all_panes:
            if p.name in general_pane_names:
                continue
            if p.pane_id and p.pane_id in general_pane_ids:
                continue
            if p.pane_id:
                if p.pane_id in panes_by_id:
                    continue
                panes_by_id[p.pane_id] = p
            unique_panes.append(p)

        restored: List[str] = []
        for meta_key, raw_val in bindings.items():
            pane_id = meta_key[len("herdr_manual_pane:"):]
            try:
                binding = json.loads(raw_val)
                if not isinstance(binding, dict):
                    raise ValueError("binding must be dict")
            except (json.JSONDecodeError, ValueError):
                self.db.delete_meta(meta_key)
                self._seen_live_manual_panes.discard(pane_id)
                continue

            conversation_id = str(binding.get("conversation_id") or "").strip()
            if not conversation_id:
                self.db.delete_meta(meta_key)
                self._seen_live_manual_panes.discard(pane_id)
                continue

            workspace_label = str(binding.get("workspace_label") or "").strip()
            matched = panes_by_id.get(pane_id) if pane_id else None
            if matched is None and workspace_label:
                label_matches = [p for p in unique_panes if p.name == workspace_label]
                if len(label_matches) == 1:
                    matched = label_matches[0]

            if matched is None:
                self.db.delete_meta(meta_key)
                self._seen_live_manual_panes.discard(pane_id)
                continue

            eff_pane_id = (matched.pane_id or pane_id).strip()
            eff_meta_key = f"herdr_manual_pane:{eff_pane_id}"

            if self._is_pane_live(matched):
                self._seen_live_manual_panes.add(eff_pane_id)
                changed = eff_meta_key != meta_key
                if eff_pane_id and binding.get("pane_id") != eff_pane_id:
                    binding["pane_id"] = eff_pane_id
                    changed = True
                if matched.workspace_id and binding.get("workspace_id") != matched.workspace_id:
                    binding["workspace_id"] = matched.workspace_id
                    changed = True
                if matched.name and binding.get("workspace_label") != matched.name:
                    binding["workspace_label"] = matched.name
                    changed = True
                if matched.cwd and binding.get("cwd") != matched.cwd:
                    binding["cwd"] = matched.cwd
                    changed = True
                if changed:
                    binding["updated_at"] = ts
                    if eff_meta_key != meta_key:
                        self.db.delete_meta(meta_key)
                        self._seen_live_manual_panes.discard(pane_id)
                    self.db.set_meta(
                        eff_meta_key,
                        json.dumps(binding, ensure_ascii=False),
                        now=ts,
                    )
                continue

            cwd_raw = (matched.cwd or str(binding.get("cwd") or "") or str(self.hq_dir)).strip()
            cwd_path = Path(cwd_raw).expanduser().resolve()
            add_dirs = (
                [
                    self.config.get_repo_path(r)
                    for r in sorted(self.config.repos.keys())
                    if self.config.get_repo_path(r).exists()
                ]
                if cwd_path == self.hq_dir.resolve()
                else None
            )
            interactive_cmd = self.agent_adapter.build_interactive_cmd(
                initial_prompt=None,
                session_id=conversation_id,
                model=self.config.agent.model,
                effort=self.config.agent.default_effort,
                add_dirs=add_dirs,
            )
            trust_args = self.agent_adapter.extra_trust_args(
                [cwd_path, *(add_dirs or [])],
                permission_mode=self.config.agent.permission_mode,
            )
            target_name = (matched.name or workspace_label or eff_pane_id).strip()
            try:
                self.terminal_driver.start_agent_in_pane(
                    name=target_name,
                    pane_id=eff_pane_id,
                    kind=self.agent_adapter.name,
                    agent_args=[*interactive_cmd[1:], *trust_args],
                )
            except Exception as exc:
                print(
                    f"[hq-daemon] warn: failed to restore manual session "
                    f"pane={eff_pane_id} name={target_name} conversation={conversation_id}: {exc}",
                    flush=True,
                )
                continue

            self._seen_live_manual_panes.add(eff_pane_id)
            binding["pane_id"] = eff_pane_id
            if matched.workspace_id:
                binding["workspace_id"] = matched.workspace_id
            if target_name:
                binding["workspace_label"] = target_name
            binding["cwd"] = str(cwd_path)
            binding["updated_at"] = ts
            if eff_meta_key != meta_key:
                self.db.delete_meta(meta_key)
                self._seen_live_manual_panes.discard(pane_id)
            self.db.set_meta(
                eff_meta_key,
                json.dumps(binding, ensure_ascii=False),
                now=ts,
            )
            restored.append(eff_pane_id)
            print(
                f"[hq-daemon] restored manual session pane={eff_pane_id} "
                f"name={target_name} conversation={conversation_id}",
                flush=True,
            )
        return restored

    def _reconcile_manual_herdr_bindings(self, now: int) -> None:
        """Keep manual Herdr pane bindings in sync: supplement labels and unbind on exit/close."""
        bindings = self.db.list_meta_prefix("herdr_manual_pane:")
        if not bindings:
            self._seen_live_manual_panes.clear()
            return

        server_running_fn = getattr(self.terminal_driver, "_server_running", None)
        if callable(server_running_fn) and not server_running_fn():
            return

        panes_by_id: Dict[str, PaneInfo] = {}
        for p in self.terminal_driver.list_panes():
            if p.pane_id and p.pane_id not in panes_by_id:
                panes_by_id[p.pane_id] = p

        for meta_key, raw_val in bindings.items():
            pane_id = meta_key[len("herdr_manual_pane:"):]
            try:
                binding = json.loads(raw_val)
                if not isinstance(binding, dict):
                    raise ValueError("binding must be dict")
            except (json.JSONDecodeError, ValueError):
                self.db.delete_meta(meta_key)
                self._seen_live_manual_panes.discard(pane_id)
                continue

            p = panes_by_id.get(pane_id)
            if p is None:
                self.db.delete_meta(meta_key)
                self._seen_live_manual_panes.discard(pane_id)
                continue

            if self._is_pane_live(p):
                self._seen_live_manual_panes.add(pane_id)
                changed = False
                if p.name and binding.get("workspace_label") != p.name:
                    binding["workspace_label"] = p.name
                    changed = True
                if p.cwd and binding.get("cwd") != p.cwd:
                    binding["cwd"] = p.cwd
                    changed = True
                if p.workspace_id and binding.get("workspace_id") != p.workspace_id:
                    binding["workspace_id"] = p.workspace_id
                    changed = True
                if changed:
                    binding["updated_at"] = now
                    self.db.set_meta(
                        meta_key,
                        json.dumps(binding, ensure_ascii=False),
                        now=now,
                    )
            elif pane_id in self._seen_live_manual_panes:
                self._seen_live_manual_panes.discard(pane_id)
                self.db.delete_meta(meta_key)

    def _materialize_long_prompt(self, prompt: str, tag: str, now: int) -> str:
        """Persist long prompts (>2KB) or distill/dream/digest prompts to a file and return a short instruction."""
        if len(prompt.encode("utf-8")) <= 2048 and not tag.startswith(("distill", "dream", "digest")):
            return prompt
        prompts_dir = self.hq_dir / ".hq" / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        safe_tag = (
            "".join(c if (c.isalnum() or c in "-_") else "-" for c in (tag or "prompt")).strip("-")
            or "prompt"
        )
        prompt_file = prompts_dir / f"{safe_tag}-{now}.md"
        if prompt_file.exists():
            seq = 1
            while (prompts_dir / f"{safe_tag}-{now}-{seq}.md").exists():
                seq += 1
            prompt_file = prompts_dir / f"{safe_tag}-{now}-{seq}.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        if tag.startswith("digest"):
            return f"请读取并严格执行指令文件 `{prompt_file}` 中的全部要求，直接输出最终晨报正文。"
        return f"请读取并执行指令文件 `{prompt_file}` 中的全部要求，完成后直接给出简洁结论。"

    def _allocate_ephemeral_pane_name(self, tag: str, now: int) -> str:
        clean_tag = (
            "".join(c if (c.isalnum() or c in "-_") else "-" for c in (tag or "bg").lower()).strip("-")
            or "bg"
        )
        base = f"hq-bg-{clean_tag}-{int(now) % 100000}"
        live_panes = {p.name for p in self.terminal_driver.list_panes()}
        if len(self._ephemeral_panes_used) > 128:
            self._ephemeral_panes_used.clear()
        candidate = base
        seq = 1
        while candidate in live_panes or candidate in self._ephemeral_panes_used:
            candidate = f"{base}-{seq}"
            seq += 1
        self._ephemeral_panes_used.add(candidate)
        return candidate

    def _run_ephemeral_background_task(
        self,
        prompt: str,
        *,
        effort: str,
        now: int,
        tag: str = "bg",
    ) -> str:
        """Run a background task in a disposable `hq-bg-*` pane and close it immediately."""
        pane_name = self._allocate_ephemeral_pane_name(tag=tag, now=now)
        reply_key = f"ephemeral_reply:{pane_name}"
        delivery_prompt = self._materialize_long_prompt(prompt, tag=tag, now=now)

        self.hq_dir.mkdir(parents=True, exist_ok=True)
        self.agent_adapter.ensure_hooks_installed(self.hq_dir, self._hook_bin_path())
        repo_paths = [
            self.config.get_repo_path(r)
            for r in sorted(self.config.repos.keys())
            if self.config.get_repo_path(r).exists()
        ]
        interactive_cmd = self.agent_adapter.build_interactive_cmd(
            initial_prompt=None,
            model=self.config.agent.model,
            effort=effort,
            add_dirs=repo_paths or None,
        )
        trust_args = self.agent_adapter.extra_trust_args(
            [self.hq_dir, *repo_paths],
            permission_mode=self.config.agent.permission_mode,
        )

        self.db.delete_meta(reply_key)
        try:
            self.terminal_driver.start_agent(
                name=pane_name,
                cwd=self.hq_dir,
                kind=self.agent_adapter.name,
                agent_args=[*interactive_cmd[1:], *trust_args],
                env={
                    "HQ_DIR": str(self.hq_dir),
                    "HQ_EPHEMERAL": "1",
                    "HQ_EPHEMERAL_REPLY_KEY": reply_key,
                },
            )
            fallback_reply = self.terminal_driver.prompt_agent(
                pane_name, delivery_prompt, wait=True
            )
            hook_reply = self.db.get_meta(reply_key)
            if hook_reply and hook_reply.strip():
                return hook_reply.strip()
            return (fallback_reply or "").strip()
        finally:
            self.db.delete_meta(reply_key)
            try:
                self.terminal_driver.kill_pane(pane_name)
            except Exception as exc:
                print(
                    f"[hq-daemon] warn: failed to close ephemeral pane {pane_name}: {exc}",
                    flush=True,
                )

    def _ensure_pane_approval_card(
        self,
        sess: Dict[str, Any],
        pane_name: str,
        now: int,
    ) -> Optional[str]:
        """Capture visible blocked prompt and push a deduplicated Feishu interactive approval card."""
        if self.channel is None:
            return None
        skey = str(sess.get("session_key") or pane_name)
        try:
            raw_screen = self.terminal_driver.read_visible_pane(pane_name, lines=30)
        except Exception:
            raw_screen = ""
        parsed = _parse_blocked_prompt(raw_screen)
        summary = str(parsed["summary"] or "")
        screen_hash = str(parsed["screen_hash"] or "")
        options = list(parsed["options"] or [])

        existing_raw = self.db.get_meta(f"blocked_approval_card:{skey}")
        if existing_raw:
            try:
                existing = json.loads(existing_raw)
                if isinstance(existing, dict) and existing.get("screen_hash") == screen_hash:
                    return str(existing.get("card_id") or "") or None
            except json.JSONDecodeError:
                pass

        resolved_raw = self.db.get_meta(f"blocked_last_resolved:{skey}")
        if resolved_raw:
            try:
                resolved = json.loads(resolved_raw)
                if (
                    isinstance(resolved, dict)
                    and resolved.get("screen_hash") == screen_hash
                    and (now - int(resolved.get("resolved_at") or 0)) < 10
                ):
                    return None
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        if summary:
            safe_summary = summary.replace("```", "` ` `")
            card_text = f"⏸️ 面板 `{pane_name}` 等待操作审批：\n```text\n{safe_summary}\n```"
        else:
            card_text = f"⏸️ 面板 `{pane_name}` 等待操作审批，请点击下方按钮确认："

        actions: Dict[str, Dict[str, Any]] = {}
        flat_buttons: List[InlineButton] = []
        for opt in options:
            aid = str(opt["id"])
            label = str(opt["label"])
            actions[aid] = {
                "label": label,
                "keys": list(opt["keys"]),
                "negative": bool(opt.get("negative", False)),
            }
            flat_buttons.append(
                InlineButton(text=label, callback_data=f"approve_key:{skey}:{aid}")
            )

        if len(flat_buttons) <= 3:
            button_rows = [flat_buttons]
        elif len(flat_buttons) == 4:
            button_rows = [flat_buttons[:2], flat_buttons[2:]]
        else:
            button_rows = [flat_buttons[:3], flat_buttons[3:]]

        card_id = self.channel.send_message(
            card_text,
            thread_id=None,
            buttons=button_rows,
        )
        record = {
            "card_id": card_id,
            "screen_hash": screen_hash,
            "pane_name": pane_name,
            "session_key": skey,
            "actions": actions,
            "created_at": now,
        }
        self.db.set_meta(
            f"blocked_approval_card:{skey}",
            json.dumps(record, ensure_ascii=False),
            now=now,
        )
        return card_id

    def _dispatch_interactive(
        self,
        session: Dict[str, Any],
        prompt: str,
        effort: str,
        now: int,
        cwd: Optional[Path] = None,
        wait: bool = True,
        prompt_tag: str = "prompt",
    ) -> str:
        skey = str(session["session_key"])
        target_cwd = cwd or (
            Path(session["worktree_path"]) if session.get("worktree_path") else self.hq_dir
        )
        pane_name = self._ensure_session_pane(session, cwd=target_cwd, effort=effort)

        if (
            self.terminal_driver.name == "herdr"
            or prompt_tag.startswith("distill")
            or len(prompt.encode("utf-8")) > 8192
        ):
            delivery_prompt = self._materialize_long_prompt(prompt, tag=prompt_tag, now=now)
        else:
            delivery_prompt = prompt
        agent_sid = session.get("agent_session_id")
        prev_turns = int(session.get("turn_count") or 0)
        tpath_before = session.get("transcript_path")
        offset_before = (
            self.agent_adapter.count_transcript_lines(str(tpath_before))
            if tpath_before
            else 0
        )
        self.db.record_dispatch(skey, agent_sid, delivery_prompt, now=now)
        self.db.set_meta(f"last_input_origin:{skey}", "daemon", now=now)
        self.db.delete_meta(f"pending_approval_reply:{skey}")
        self.db.delete_meta(f"last_reply:{skey}")
        self.db.delete_meta(f"last_reply_pane:{pane_name}")
        self.db.set_meta(f"daemon_waiting:{skey}", "1", now=now)
        self.db.set_meta(f"daemon_waiting_pane:{pane_name}", "1", now=now)
        blocked = False
        try:
            fallback_reply = self.terminal_driver.prompt_agent(
                pane_name, delivery_prompt, wait=wait
            )
        except AgentBlockedError:
            blocked = True
            self.db.set_meta(f"pending_approval_reply:{skey}", "1", now=now)
            self.db.set_meta(f"last_input_origin:{skey}", "daemon", now=now)
            self._ensure_pane_approval_card(session, pane_name, now=now)
            return "⏸️ 需要审批，请点击下方审批卡片确认"
        finally:
            self.db.delete_meta(f"daemon_waiting:{skey}")
            self.db.delete_meta(f"daemon_waiting_pane:{pane_name}")
            self.db.delete_meta(f"current_work_hint:{skey}")
            if not blocked and wait:
                self.db.delete_meta(f"last_input_origin:{skey}")

        hook_reply = self.db.get_meta(f"last_reply:{skey}")
        if hook_reply is not None:
            self.db.delete_meta(f"last_reply:{skey}")
        pane_reply = self.db.get_meta(f"last_reply_pane:{pane_name}")
        if pane_reply is not None:
            self.db.delete_meta(f"last_reply_pane:{pane_name}")

        fresh = self.db.get_session(skey) or session
        if hook_reply and hook_reply.strip():
            reply = hook_reply
        elif pane_reply and pane_reply.strip():
            reply = pane_reply
        else:
            transcript_reply = ""
            tpath_after = fresh.get("transcript_path") or tpath_before
            if tpath_after and Path(str(tpath_after)).exists():
                turns, _ = self.agent_adapter.read_transcript_slice(
                    str(tpath_after), from_line=offset_before
                )
                assistant_msgs = [
                    t.text for t in turns if t.role == "assistant" and t.text.strip()
                ]
                if assistant_msgs:
                    transcript_reply = assistant_msgs[-1]
            if transcript_reply:
                reply = transcript_reply
            elif self.terminal_driver.name != "herdr":
                reply = fallback_reply or ""
            elif wait:
                reply = (
                    "⏳ 请求已送达交互式会话，但本轮尚未产出最终文本回复（可能仍在执行后台任务），"
                    "可打开 herdr 查看实时进度。"
                )
            else:
                reply = ""

        cur_turns = int(fresh.get("turn_count") or 0)
        new_turns = cur_turns if cur_turns > prev_turns else prev_turns + 1
        self.db.update_session(
            skey,
            pane_name=pane_name,
            turn_count=new_turns,
            last_active_at=now,
        )
        return reply

    def _get_recent_outbound(self) -> List[Dict[str, Any]]:
        limit = int(self.config.tuning.past_messages_limit)
        if limit <= 0:
            return []
        return self.db.list_recent_local_outbound(limit)

    def _consume_general_handoff_prefix(self, session: Dict[str, Any]) -> str:
        if session.get("track") not in GENERAL_TRACKS:
            return ""
        if self.db.get_meta("general_handoff_pending") != "1":
            return ""
        self.db.delete_meta("general_handoff_pending")
        summary = (self.db.get_meta("general_handoff_summary") or "").strip()
        if not summary:
            return ""
        reason = (self.db.get_meta("general_handoff_reason") or "night").strip()
        header = (
            "## 昨日会话交接摘要" if reason in {"night", "1", ""} else "## 上一轮会话交接摘要"
        )
        return f"{header}\n{summary}\n\n"

    def _wake_waiting_events(self, now: int) -> int:
        """Promote at most 1 FIFO `waiting_general` event (or batch leader) per idle target session."""
        waiting = self.db.list_waiting_events()
        if not waiting:
            return 0
        woken_sessions: Set[str] = set()
        promoted = 0
        for ev in waiting:
            wait_skey = str(ev.payload.get("wait_session_key") or "")
            sess = self.db.get_session(wait_skey) if wait_skey else None
            if sess is None or sess.get("status") not in {"active", "pinned"}:
                sess = self._ensure_general_session(now)
            eff_key = str(sess["session_key"])
            if eff_key in woken_sessions:
                continue
            is_busy, _, _ = self._inspect_session_busy(sess, now)
            if not is_busy:
                if self.db.promote_waiting_event(ev.id):
                    woken_sessions.add(eff_key)
                    promoted += 1
        return promoted

    def _park_channel_event_in_batch(
        self,
        ev: EventRow,
        sess: Dict[str, Any],
        busy_kind: str,
        hint: str,
    ) -> None:
        """Park a Feishu channel event into the open busy batch (1 button-less card per batch)."""
        payload = ev.payload
        skey = str(sess["session_key"])

        # Already assigned to a batch (e.g. promoted leader re-parked because General became busy again)
        if payload.get("busy_batch_id") and payload.get("busy_card_msg_id"):
            self.db.park_event_waiting(
                ev.id,
                payload_updates={"wait_session_key": skey},
            )
            return

        open_batch = [e for e in self.db.list_open_busy_batch_events() if e.id != ev.id]
        if open_batch:
            batch_id = str(open_batch[0].payload.get("busy_batch_id") or f"batch:{open_batch[0].id}")
            card_id = open_batch[0].payload.get("busy_card_msg_id")
            new_count = len(open_batch) + 1
            card_text = _build_busy_queue_card_text(busy_kind, hint, new_count)
            if not card_id and self.channel is not None:
                card_id = self.channel.send_message(card_text, thread_id=None)
            elif card_id and self.channel is not None:
                self.channel.finish_working_indicator(str(card_id), card_text, thread_id=None)
            self.db.park_event_waiting(
                ev.id,
                payload_updates={
                    "busy_batch_id": batch_id,
                    "busy_card_msg_id": card_id,
                    "wait_session_key": skey,
                },
            )
            return

        batch_id = f"batch:{ev.id}"
        card_text = _build_busy_queue_card_text(busy_kind, hint, 1)
        card_id = (
            self.channel.send_message(card_text, thread_id=None)
            if self.channel is not None
            else None
        )
        self.db.park_event_waiting(
            ev.id,
            payload_updates={
                "busy_batch_id": batch_id,
                "busy_card_msg_id": card_id,
                "wait_session_key": skey,
            },
        )

    # ------------------------------------------------------------------
    # Event Execution
    # ------------------------------------------------------------------
    def _process_event(self, ev: EventRow, now: int) -> int:
        if ev.source == "channel":
            payload = dict(ev.payload)
            # Single-window Feishu routing: always target `#General`, always reply in main window
            sess = self._ensure_general_session(now)
            pane_name = self._resolve_pane_name(sess)
            is_busy, busy_kind, hint = self._inspect_session_busy(sess, now)
            if is_busy:
                self._park_channel_event_in_batch(ev, sess, busy_kind, hint)
                if busy_kind == "blocked":
                    self._ensure_pane_approval_card(sess, pane_name, now=now)
                raise _EventParked()

            batch_events: List[EventRow] = [ev]
            follower_ids: List[int] = []
            if payload.get("busy_batch_id") or payload.get("batch_closed"):
                closed = self.db.close_busy_batch_for_dispatch(ev.id)
                if closed:
                    batch_events = closed
                    follower_ids = [b.id for b in batch_events if b.id != ev.id]
                    refreshed_leader = self.db.get_event(ev.id)
                    if refreshed_leader:
                        payload = json.loads(refreshed_leader["payload_json"])
                        ev.payload = payload

            merged_user_text = _format_merged_channel_batch(batch_events)
            first_msg_text = str(batch_events[0].payload.get("text") or "")
            compact_hint = " ".join(first_msg_text.split())[:24]
            if compact_hint:
                self.db.set_meta(f"current_work_hint:{sess['session_key']}", compact_hint, now=now)

            is_live_pane = self._find_live_pane(pane_name) is not None
            has_prior_turns = int(sess.get("turn_count") or 0) > 0
            use_raw_prompt = is_live_pane and has_prior_turns
            recent_outbound = self._get_recent_outbound()
            handoff_prefix = self._consume_general_handoff_prefix(sess)
            prompt_payload = dict(payload)
            prompt_payload["text"] = merged_user_text
            prompt_payload["thread_id"] = None
            base_prompt = (
                merged_user_text
                if use_raw_prompt
                else build_prompt(
                    self.hq_dir,
                    self.config,
                    prompt_payload,
                    recent_outbound=recent_outbound,
                    topic_slug=None,
                )
            )
            prompt = (handoff_prefix + base_prompt) if handoff_prefix else base_prompt

            existing_busy_card = payload.get("busy_card_msg_id")
            if existing_busy_card:
                # Keep queuing card untouched until final reply arrives (no "轮到你了" flicker)
                indicator_id: Optional[str] = str(existing_busy_card)
            else:
                indicator_id = (
                    self.channel.send_working_indicator("⏳ 收到，正在处理中...", thread_id=None)
                    if self.channel
                    else None
                )
            try:
                reply = self._dispatch_interactive(
                    sess,
                    prompt,
                    effort=self.config.agent.default_effort,
                    now=now,
                    cwd=self.hq_dir,
                    wait=True,
                )
            except Exception as exc:
                if self.channel and indicator_id:
                    self.channel.finish_working_indicator(
                        indicator_id, f"❌ 处理失败：{exc}", thread_id=None
                    )
                raise

            notice_pending = self.db.get_meta("general_rotation_notice_pending")
            if (
                sess.get("track") in GENERAL_TRACKS
                and reply
                and notice_pending in {"1", "night", "size"}
            ):
                self.db.delete_meta("general_rotation_notice_pending")
                if notice_pending == "size":
                    notice_line = (
                        "\n\n---\n_🧹 当前对话上下文较长，已自动整理交接摘要并开启新对话，"
                        "旧对话可在 `herdr` 中输入 `/resume` 查看。_"
                    )
                else:
                    notice_line = (
                        "\n\n---\n_🧹 昨晚已整理记忆并开启新对话，旧对话可在 `herdr` 中输入 `/resume` 查看。_"
                    )
                reply = reply.rstrip() + notice_line

            if self.channel:
                if reply:
                    if indicator_id and self.channel.finish_working_indicator(
                        indicator_id, reply, thread_id=None
                    ):
                        pass
                    else:
                        self.channel.send_message(reply, thread_id=None)
                elif indicator_id:
                    self.channel.finish_working_indicator(
                        indicator_id, "✅ 已完成（无额外文本回复）", thread_id=None
                    )
            if follower_ids:
                self.db.complete_events(follower_ids)
            return len(batch_events)

        if ev.source == "git_commit":
            repo_name = str(ev.payload.get("repo", ""))
            recent_outbound = self._get_recent_outbound()
            prompt = build_prompt(
                self.hq_dir,
                self.config,
                ev.payload,
                matched_repos=[repo_name] if repo_name else None,
                recent_outbound=recent_outbound,
            )
            self._run_ephemeral_background_task(
                prompt,
                effort=self.config.agent.light_effort,
                now=now,
                tag="commit",
            )
            return

        if ev.source == "session_distill":
            skey = str(ev.payload.get("session_key", ""))
            agent_sid = str(ev.payload.get("agent_session_id", ""))
            tpath = str(ev.payload.get("transcript_path", ""))
            to_line = int(ev.payload.get("to_line", 0))

            recent_outbound = self._get_recent_outbound()
            prompt = build_prompt(
                self.hq_dir,
                self.config,
                ev.payload,
                recent_outbound=recent_outbound,
            )
            reply = self._run_ephemeral_background_task(
                prompt,
                effort=self.config.agent.light_effort,
                now=now,
                tag="distill",
            )

            if reply.strip() and not _is_noop_distill_reply(reply):
                today_path = self.hq_dir / "notes" / "today.md"
                today_path.parent.mkdir(parents=True, exist_ok=True)
                existing = today_path.read_text(encoding="utf-8") if today_path.exists() else ""
                entry = f"\n<!-- distill:{skey}:{to_line} -->\n{reply.strip()}\n"
                today_path.write_text(existing.rstrip() + entry, encoding="utf-8")

            if agent_sid:
                self.cursors.advance_transcript_cursor(
                    agent_sid,
                    new_line=to_line,
                    transcript_path=tpath,
                    session_key=skey,
                )
            prev_count = int(self.db.get_meta("sessions_since_dream", "0") or "0")
            self.db.set_meta("sessions_since_dream", str(prev_count + 1), now=now)
            return

        if ev.source == "deferred_resume":
            skey = str(ev.payload.get("session_key", ""))
            orig_sess = self.db.get_session(skey)
            if not orig_sess:
                return
            general_sess = self._ensure_general_session(now)
            is_busy, _, _ = self._inspect_session_busy(general_sess, now)
            if is_busy:
                self.db.park_event_waiting(
                    ev.id, payload_updates={"wait_session_key": str(general_sess["session_key"])}
                )
                raise _EventParked()
            recent_outbound = self._get_recent_outbound()
            handoff_prefix = self._consume_general_handoff_prefix(general_sess)
            prompt = handoff_prefix + build_prompt(
                self.hq_dir,
                self.config,
                ev.payload,
                recent_outbound=recent_outbound,
            )
            reply = self._dispatch_interactive(
                general_sess,
                prompt,
                effort=self.config.agent.default_effort,
                now=now,
                cwd=self.hq_dir,
                wait=True,
            )
            if orig_sess.get("track") == "A":
                self.db.seal_session(skey, now=now)
            thread_id = ev.payload.get("thread_id") or orig_sess.get("channel_thread_id")
            if self.channel and reply:
                self.channel.send_message(reply, thread_id=thread_id)
            return

        if ev.source == "timer_wake":
            payload = dict(ev.payload)
            target_key = payload.get("session_key")
            target_sess = self.db.get_session(str(target_key)) if target_key else None
            if target_sess and target_sess.get("status") in {"active", "pinned"}:
                sess = target_sess
            else:
                sess = self._ensure_general_session(now)
                if target_key:
                    payload["migrated_from_session"] = str(target_key)

            is_busy, _, _ = self._inspect_session_busy(sess, now)
            if is_busy:
                self.db.park_event_waiting(
                    ev.id, payload_updates={"wait_session_key": str(sess["session_key"])}
                )
                raise _EventParked()

            recent_outbound = self._get_recent_outbound()
            topic_slug = (
                str(sess["topic_slug"])
                if (sess.get("track") in TOPIC_TRACKS and sess.get("topic_slug"))
                else None
            )
            handoff_prefix = self._consume_general_handoff_prefix(sess)
            prompt = handoff_prefix + build_prompt(
                self.hq_dir,
                self.config,
                payload,
                recent_outbound=recent_outbound,
                topic_slug=topic_slug,
            )
            target_cwd = (
                Path(sess["worktree_path"])
                if sess.get("worktree_path")
                else self.hq_dir
            )
            reply = self._dispatch_interactive(
                sess,
                prompt,
                effort=self.config.agent.default_effort,
                now=now,
                cwd=target_cwd,
                wait=True,
            )
            thread_id = sess.get("channel_thread_id")
            if self.channel and reply and reply.strip():
                self.channel.send_message(reply, thread_id=thread_id)
            return

        if ev.source == "pane_blocked":
            skey = str(ev.payload.get("session_key", ""))
            sess = self.db.get_session(skey) if skey else None
            pane_name = str(
                ev.payload.get("pane_name")
                or (sess.get("pane_name") if sess else "")
                or GENERAL_PANE_NAME
            )
            if sess is None:
                sess = {"session_key": skey or pane_name, "pane_name": pane_name}
            self._ensure_pane_approval_card(sess, pane_name, now=now)
            return

    # ------------------------------------------------------------------
    # Nightly 3-Stage Pipeline & Morning Digest
    # ------------------------------------------------------------------
    def run_nightly_pipeline(self, now: Optional[int] = None) -> ArchiveReport:
        """
        Strict serial 3-stage nightly pipeline:
          Step 0: Force-distill all active/pinned/sealed sessions with unsummarized lines.
          Step 1: Dreaming (Orient/Gather/Consolidate/Prune) & archive/clear notes/today.md.
          Step 2: Archive eligible sealed sessions (never pinned or dirty).
        """
        ts = int(now if now is not None else time.time())

        # Step 0: Distill all unsummarized lines
        scan_distill_candidates(
            self.db, self.cursors, self.agent_adapter, self.config, now=ts, force_all=True
        )
        for ev in self.db.claim_next_events(
            limit=50, now=ts, lease_ttl_sec=self.config.tuning.inbox_lease_ttl_sec
        ):
            if ev.source == "session_distill":
                try:
                    self._process_event(ev, ts)
                    self.db.complete_event(ev.id)
                except _EventParked:
                    pass
                except Exception as exc:
                    self.db.fail_event(
                        ev.id, max_attempts=self.config.tuning.inbox_max_attempts, now=ts, error_msg=str(exc)
                    )
            else:
                # Return non-distill event to pending
                self.db.conn.execute(
                    "UPDATE events SET status='pending', lease_until=0, attempts=attempts-1 WHERE id=?",
                    (ev.id,),
                )

        # Step 1: Dreaming (isolated ephemeral background pane)
        last_dream_at = int(self.db.get_meta("last_dream_at", "0") or "0")
        sessions_since = int(self.db.get_meta("sessions_since_dream", "0") or "0")
        interval_ok = (ts - last_dream_at) >= self.config.tuning.dream_min_interval_hours * 3600
        sessions_ok = sessions_since >= self.config.tuning.dream_min_sessions

        if self.config.tuning.dream_force_nightly or (interval_ok and sessions_ok):
            prompt = build_prompt(
                self.hq_dir,
                self.config,
                {"task": "dream-consolidate", "timestamp": ts},
            )
            try:
                self._run_ephemeral_background_task(
                    prompt,
                    effort=self.config.agent.default_effort,
                    now=ts,
                    tag="dream",
                )
            except Exception as exc:
                self.db.add_digest_item(
                    level="alert",
                    category="dream_error",
                    message=f"夜间记忆整理失败：{exc}",
                    now=ts,
                )

            # Archive today.md -> notes/archive/YYYY-MM-DD.md and reset today.md
            today_path = self.hq_dir / "notes" / "today.md"
            if today_path.exists():
                content = today_path.read_text(encoding="utf-8").strip()
                if content:
                    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                    arch_dir = self.hq_dir / "notes" / "archive"
                    arch_dir.mkdir(parents=True, exist_ok=True)
                    arch_file = arch_dir / f"{date_str}.md"
                    prev = arch_file.read_text(encoding="utf-8") if arch_file.exists() else ""
                    arch_file.write_text(
                        (prev + "\n\n" + content).strip() + "\n", encoding="utf-8"
                    )
                today_path.write_text(
                    "# Today WAL [UNVERIFIED / TODAY ONLY]\n\n", encoding="utf-8"
                )
            self.db.set_meta("last_dream_at", str(ts), now=ts)
            self.db.set_meta("sessions_since_dream", "0", now=ts)

        # Step 2: Safe Archiver
        report = archive_eligible_sessions(
            self.config,
            self.db,
            terminal_driver=self.terminal_driver,
            channel=self.channel,
            now=ts,
        )
        if self.config.tuning.general_nightly_reset:
            self.db.set_meta("general_rotation_due", "night", now=ts)
            self._maybe_run_due_general_rotation(ts)
        sync_hq_repo(self.hq_dir, commit_message="herdr-bot: nightly memory closeout", push=False)
        return report

    def _collect_morning_digest_context(self, now: int) -> Dict[str, Any]:
        """Deterministically collect read-only facts from HQ memory and registered repos."""
        dt = datetime.fromtimestamp(now)
        today_date = dt.strftime("%Y-%m-%d")
        yesterday_date = (dt - timedelta(days=1)).strftime("%Y-%m-%d")

        yesterday_path = self.hq_dir / "notes" / "archive" / f"{yesterday_date}.md"
        yesterday_archive = (
            _clean_wal_text(yesterday_path.read_text(encoding="utf-8", errors="replace"))
            if yesterday_path.is_file()
            else ""
        )

        today_path = self.hq_dir / "notes" / "today.md"
        today_wal = (
            _clean_wal_text(today_path.read_text(encoding="utf-8", errors="replace"))
            if today_path.is_file()
            else ""
        )

        wip_path = self.hq_dir / "tasks" / "active-wip.md"
        active_wip = (
            _clean_active_wip_text(wip_path.read_text(encoding="utf-8", errors="replace"))
            if wip_path.is_file()
            else ""
        )

        repo_states: List[Dict[str, Any]] = []
        for repo_name in sorted(self.config.repos.keys()):
            repo_path = self.config.get_repo_path(repo_name)
            if not (repo_path / ".git").exists():
                continue
            branch = (
                _run_readonly_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"]) or ""
            ).strip()
            porcelain = _run_readonly_git(repo_path, ["status", "--porcelain"]) or ""
            dirty_files_count = sum(1 for ln in porcelain.splitlines() if ln.strip())

            raw_recent = (
                _run_readonly_git(
                    repo_path,
                    ["log", "--all", "--since=36 hours ago", "--format=%D | %s", "-n", "25"],
                )
                or ""
            )
            recent_commits = _format_git_log_lines(raw_recent)

            remotes_out = _run_readonly_git(repo_path, ["remote"]) or ""
            has_remotes = bool(remotes_out.strip())
            unpushed_branches: List[str] = []
            for_each = (
                _run_readonly_git(
                    repo_path,
                    [
                        "for-each-ref",
                        "--format=%(refname:short)\t%(upstream:short)\t%(upstream:track)",
                        "refs/heads/",
                    ],
                )
                or ""
            )
            for line in for_each.splitlines():
                parts = line.split("\t")
                br = parts[0].strip() if len(parts) > 0 else ""
                upstream = parts[1].strip() if len(parts) > 1 else ""
                track = parts[2].strip() if len(parts) > 2 else ""
                if not br:
                    continue
                if upstream and "ahead" in track:
                    unpushed_branches.append(f"{br} ({track.strip('[]')})")
                elif not upstream and has_remotes:
                    cnt_raw = (
                        _run_readonly_git(
                            repo_path, ["rev-list", "--count", br, "--not", "--remotes"]
                        )
                        or ""
                    ).strip()
                    try:
                        cnt = int(cnt_raw)
                    except ValueError:
                        cnt = 0
                    if cnt > 0:
                        unpushed_branches.append(f"{br} (未关联远端，+{cnt} 个未推提交)")

            unpushed_commits: List[str] = []
            if has_remotes and unpushed_branches:
                raw_unpushed = (
                    _run_readonly_git(
                        repo_path,
                        ["log", "--branches", "--not", "--remotes", "--format=%D | %s", "-n", "15"],
                    )
                    or ""
                )
                unpushed_commits = _format_git_log_lines(raw_unpushed)

            repo_states.append(
                {
                    "repo": repo_name,
                    "branch": branch or "-",
                    "dirty_files_count": dirty_files_count,
                    "recent_commits": recent_commits,
                    "unpushed_branches": unpushed_branches,
                    "unpushed_commits": unpushed_commits,
                }
            )

        raw_items = self.db.list_pending_digest_items()
        alerts: List[Dict[str, Any]] = [
            {
                "id": int(it["id"]),
                "category": str(it.get("category") or ""),
                "label": _format_digest_category(str(it.get("category") or "")),
                "message": str(it.get("message") or ""),
            }
            for it in raw_items
        ]

        warn_hours = self.config.tuning.dirty_worktree_warn_hours
        warn_sec = warn_hours * 3600
        dirty_worktrees: List[Dict[str, Any]] = []
        for sess in self.db.list_sessions(statuses=["sealed", "pinned"]):
            if sess.get("archive_blocked_reason") and (now - int(sess["last_active_at"])) >= warn_sec:
                dirty_worktrees.append(
                    {
                        "session_key": str(sess["session_key"]),
                        "worktree_path": str(sess.get("worktree_path") or ""),
                        "warn_hours": warn_hours,
                        "reason": str(sess.get("archive_blocked_reason") or ""),
                    }
                )

        active_timers: List[Dict[str, Any]] = []
        for tm in self.db.list_timers(statuses=["active"]):
            next_ts = int(tm.get("next_fire_at") or 0)
            active_timers.append(
                {
                    "id": int(tm["id"]),
                    "name": str(tm.get("name") or ""),
                    "kind": str(tm.get("kind") or "once"),
                    "next_fire_at": next_ts,
                    "next_fire_str": (
                        datetime.fromtimestamp(next_ts).strftime("%Y-%m-%d %H:%M")
                        if next_ts > 0
                        else "-"
                    ),
                    "reason": str(tm.get("reason") or ""),
                    "session_key": str(tm.get("session_key") or "general"),
                }
            )

        return {
            "date": today_date,
            "yesterday_date": yesterday_date,
            "yesterday_archive": yesterday_archive,
            "today_wal": today_wal,
            "active_wip": active_wip,
            "repo_states": repo_states,
            "alerts": alerts,
            "dirty_worktrees": dirty_worktrees,
            "active_timers": active_timers,
        }

    def _format_fallback_morning_digest(self, ctx: Dict[str, Any]) -> str:
        lines: List[str] = ["### 🌅 每日晨报"]
        for alert in ctx.get("alerts") or []:
            lines.append(f"- 【{alert['label']}】{alert['message']}")
        for wt in ctx.get("dirty_worktrees") or []:
            lines.append(
                f"- 【滞留脏工作区】会话 `{wt['session_key']}` 已有超过 "
                f"{wt['warn_hours']} 小时未清理（路径：`{wt['worktree_path']}`）"
            )
        if len(lines) == 1:
            lines.append("- 一切正常，暂无待处理告警或滞留工作区。")
        return "\n".join(lines)

    def _build_morning_digest_prompt(self, ctx: Dict[str, Any], now: int) -> str:
        _ = now
        project_name = (self.config.project_name or "").strip() or "current-project"
        today_date = str(ctx.get("date") or "")
        yesterday_date = str(ctx.get("yesterday_date") or "")

        repo_lines: List[str] = []
        for rs in ctx.get("repo_states") or []:
            unpushed_str = (
                "、".join(rs.get("unpushed_branches") or [])
                if rs.get("unpushed_branches")
                else "无"
            )
            repo_lines.append(
                f"- **{rs['repo']}**：当前分支 `{rs['branch']}`，"
                f"未提交文件数 `{rs['dirty_files_count']}`，未推送分支：{unpushed_str}"
            )
            recent = rs.get("recent_commits") or []
            if recent:
                repo_lines.append("  - 近 36 小时提交（仅作提炼素材，切勿逐条照搬）：")
                for c in recent[:25]:
                    repo_lines.append(f"    - {c}")
            else:
                repo_lines.append("  - 近 36 小时提交：无")
            unpushed_commits = rs.get("unpushed_commits") or []
            if unpushed_commits:
                repo_lines.append("  - 未推送提交参考（仅作提炼素材，切勿逐条照搬）：")
                for uc in unpushed_commits[:15]:
                    repo_lines.append(f"    - {uc}")

        sys_lines: List[str] = []
        alerts = ctx.get("alerts") or []
        if alerts:
            sys_lines.append("- 待处理系统告警：")
            for a in alerts:
                sys_lines.append(f"  - 【{a['label']}】{a['message']}")
        else:
            sys_lines.append("- 待处理系统告警：无")

        dirty_wts = ctx.get("dirty_worktrees") or []
        if dirty_wts:
            sys_lines.append("- 滞留脏工作区：")
            for wt in dirty_wts:
                sys_lines.append(
                    f"  - 会话 `{wt['session_key']}` 超过 {wt['warn_hours']} 小时未清理"
                    f"（路径：`{wt['worktree_path']}`）"
                )
        else:
            sys_lines.append("- 滞留脏工作区：无")

        timers = ctx.get("active_timers") or []
        if timers:
            sys_lines.append("- 活跃定时器：")
            for tm in timers[:12]:
                name_part = f" [{tm['name']}]" if tm.get("name") else ""
                sys_lines.append(
                    f"  - timer#{tm['id']}{name_part} ({tm['kind']}, 下次触发 {tm['next_fire_str']}, "
                    f"session={tm['session_key']}): {tm['reason']}"
                )
        else:
            sys_lines.append("- 活跃定时器：无")

        yesterday_block = _truncate_digest_block(str(ctx.get("yesterday_archive") or "")) or "（无）"
        today_block = _truncate_digest_block(str(ctx.get("today_wal") or "")) or "（无）"
        wip_block = _truncate_digest_block(str(ctx.get("active_wip") or ""), max_chars=6000) or "（无）"
        repos_block = "\n".join(repo_lines) if repo_lines else "（无已注册仓库）"
        sys_block = "\n".join(sys_lines)

        return "\n".join(
            [
                f"# 每日研发晨报生成任务（项目：`{project_name}`，日期：{today_date}）",
                "",
                "请严格基于下方采集到的只读事实素材，撰写一份**简洁、高抽象层次的中文研发晨报**。",
                "",
                "## 输出要求（必须严格遵守）",
                "1. 按业务主题与阶段里程碑高度归纳，**不要照搬逐条原始 commit 文案、Git SHA 或冗长文件路径列表**。",
                "2. **严禁虚构**素材中不存在的事实、数据、结论或未注册仓库；不确定处按素材如实标注。",
                "3. **只读总结，严禁修改任何文件、严禁执行写操作或修改仓库状态**。",
                "4. **直接输出最终可发给用户的 Markdown 晨报正文**，不要写“已完成晨报撰写”“以下为晨报内容”等元话术，也不要用最外层代码块包裹。",
                "5. 严格按以下 4 个一级模块组织（保留标题与 emoji）：",
                f"   - 顶部标题：`### 🌅 每日研发晨报（{today_date}）`",
                "   - `### 🚀 昨日推进与决策`：昨天整体推进了哪些核心任务、取得了什么阶段性成果、拍板了什么关键方案。",
                "   - `### 🧩 遇到的问题与待确认`：踩到的坑、未闭环问题、需要用户确认/拍板/补充现场的事项；若无则写“暂无阻塞或待拍板事项。”",
                "   - `### 📦 当前代码状态`：按已注册仓库/主题概括当前所在分支、改动是否已提交、哪些已推/未推或待合并打包、工作区是否干净（不要逐条列出 commit 原文）。",
                "   - `### ⚠️ 系统与提醒`：汇总系统告警、滞留脏工作区及活跃定时器；若全部正常则一行写“系统运行正常，暂无告警或滞留工作区。”",
                "",
                f"## 素材 1：昨日归档（`notes/archive/{yesterday_date}.md`）",
                yesterday_block,
                "",
                "## 素材 2：近期工作日志（`notes/today.md`，已过滤空噪点）",
                today_block,
                "",
                "## 素材 3：进行中任务台账（`tasks/active-wip.md`）",
                wip_block,
                "",
                "## 素材 4：已注册仓库只读 Git 状态",
                repos_block,
                "",
                "## 素材 5：系统告警、滞留工作区与活跃定时器",
                sys_block,
                "",
            ]
        )

    def run_morning_digest(self, now: Optional[int] = None, use_llm: bool = True) -> bool:
        ts = int(now if now is not None else time.time())
        ctx = self._collect_morning_digest_context(ts)
        fallback_text = self._format_fallback_morning_digest(ctx)
        digest_text = fallback_text

        if use_llm and _has_substantive_digest_material(ctx):
            prompt = self._build_morning_digest_prompt(ctx, ts)
            try:
                llm_reply = self._run_ephemeral_background_task(
                    prompt,
                    effort=self.config.agent.default_effort,
                    now=ts,
                    tag="digest",
                )
                cleaned = _strip_outer_markdown_fence(llm_reply)
                if cleaned:
                    if not _DIGEST_HEADING_RE.match(cleaned):
                        digest_text = f"### 🌅 每日研发晨报（{ctx['date']}）\n\n{cleaned}"
                    else:
                        digest_text = cleaned
                else:
                    print(
                        "[hq-daemon] warn: morning digest LLM returned empty reply; falling back to plain digest",
                        flush=True,
                    )
            except Exception as exc:
                print(
                    f"[hq-daemon] warn: morning digest LLM failed ({exc}); falling back to plain digest",
                    flush=True,
                )

        self.last_digest_text = digest_text
        if self.channel:
            self.channel.send_message(digest_text, thread_id=None)
        item_ids = [int(a["id"]) for a in ctx.get("alerts") or []]
        self.db.mark_digest_items_sent(item_ids, now=ts)
        return True

    def run_once(self, now: Optional[int] = None) -> TickSummary:
        ts = int(now if now is not None else time.time())
        summary = TickSummary()

        self.config.reload_if_changed()
        self.db.expire_deferred_actions(now=ts)

        if self.channel is not None:
            for msg in self.channel.poll_updates(timeout=1):
                self._handle_inbound(msg, ts)

        summary.commits_enqueued = len(
            scan_repo_commits(
                self.config, self.cursors, self.db, terminal_driver=self.terminal_driver, now=ts
            )
        )
        summary.distill_enqueued = len(
            scan_distill_candidates(
                self.db, self.cursors, self.agent_adapter, self.config, now=ts
            )
        )
        scan_pane_blocked(self.db, self.terminal_driver, self.config, now=ts)
        self._reconcile_manual_herdr_bindings(ts)
        for sess in self.db.list_sessions(statuses=["active", "sealed"]):
            if int(sess.get("pinned") or 0) == 0 and sess.get("pane_name"):
                detect_manual_touch(
                    self.db, sess, terminal_driver=self.terminal_driver, now=ts
                )
        self._seal_idle_general_sessions(ts)
        summary.timers_fired = self._tick_custom_timers(ts)
        self._wake_waiting_events(ts)

        events = self.db.claim_next_events(
            limit=10,
            now=ts,
            lease_ttl_sec=self.config.tuning.inbox_lease_ttl_sec,
        )
        for ev in events:
            try:
                processed_count = self._process_event(ev, ts)
                self.db.complete_event(ev.id)
                summary.events_processed += (
                    processed_count if isinstance(processed_count, int) and processed_count > 0 else 1
                )
                print(
                    f"[hq-daemon] done event={ev.id} source={ev.source}",
                    flush=True,
                )
            except _EventParked:
                summary.events_parked += 1
            except Exception as exc:
                print(
                    f"[hq-daemon] error event={ev.id} source={ev.source}: {exc}",
                    flush=True,
                )
                fail_state = self.db.fail_event(
                    ev.id,
                    max_attempts=self.config.tuning.inbox_max_attempts,
                    now=ts,
                    error_msg=str(exc),
                )
                if fail_state == "dead":
                    member_ids = ev.payload.get("batch_member_ids") or []
                    follower_ids = [int(i) for i in member_ids if int(i) != ev.id]
                    if follower_ids:
                        self.db.mark_events_dead(follower_ids)
                summary.events_failed += 1

        dt = datetime.fromtimestamp(ts)
        hhmm = dt.strftime("%H:%M")
        today_str = dt.strftime("%Y-%m-%d")

        if hhmm >= self.config.tuning.dream_cron and self.db.get_meta("last_dream_date") != today_str:
            self.run_nightly_pipeline(now=ts)
            self.db.set_meta("last_dream_date", today_str, now=ts)
            summary.nightly_ran = True

        if self._maybe_run_due_general_rotation(ts):
            summary.general_rotated = True

        if hhmm >= self.config.tuning.digest_cron and self.db.get_meta("last_digest_date") != today_str:
            self.run_morning_digest(now=ts)
            self.db.set_meta("last_digest_date", today_str, now=ts)
            summary.digest_sent = True

        summary.outbox_sent = self._drain_outbox(ts)

        return summary

    def _tick_custom_timers(self, now: int) -> int:
        """Fire due self-scheduled timers and enqueue `timer_wake` events into Inbox."""
        due = self.db.fire_due_timers(now=now)
        enqueued = 0
        for t in due:
            fired_at = int(t.get("fired_at") or t.get("last_fired_at") or now)
            ev_id = self.db.enqueue_event(
                source="timer_wake",
                dedupe_key=f"timer:{t['id']}:{fired_at}",
                priority=20,
                available_at=now,
                payload={
                    "timer_id": int(t["id"]),
                    "name": t.get("name"),
                    "kind": str(t["kind"]),
                    "session_key": t.get("session_key"),
                    "reason": str(t["reason"]),
                    "fired_at": fired_at,
                    "fired_count": int(t.get("fired_count") or 1),
                    "every_sec": int(t.get("every_sec") or 0),
                    "next_fire_at": int(t["next_fire_at"]),
                },
                now=now,
            )
            if ev_id is not None:
                enqueued += 1
        return enqueued

    def _drain_outbox(self, now: int) -> int:
        """Retry due durable outbox messages with backoff and per-minute rate protection."""
        if self.channel is None:
            return 0
        max_per_min = int(self.config.tuning.outbox_max_per_minute)
        max_retries = int(self.config.tuning.outbox_max_retries)
        sent_in_window = (
            self.db.count_recent_outbox_sent(now - 60) if max_per_min > 0 else 0
        )
        fetch_limit = max(10, max_per_min) if max_per_min > 0 else 20
        due_rows = self.db.list_due_outbox(now=now, limit=fetch_limit)
        sent_count = 0

        for row in due_rows:
            lvl = str(row.get("level") or "").lower().strip()
            if (
                max_per_min > 0
                and sent_in_window >= max_per_min
                and lvl not in {"question", "review"}
            ):
                continue

            buttons: Optional[List[List[InlineButton]]] = None
            opt_objs: List[Dict[str, str]] = []
            raw_buttons_json = row.get("buttons_json")
            if raw_buttons_json:
                try:
                    parsed = json.loads(raw_buttons_json)
                    if isinstance(parsed, list):
                        rebuilt: List[List[InlineButton]] = []
                        opt_idx = 0
                        for btn_row in parsed:
                            if not isinstance(btn_row, list):
                                continue
                            cur_row: List[InlineButton] = []
                            for b in btn_row:
                                if not isinstance(b, dict):
                                    continue
                                b_text = str(b.get("text", ""))
                                b_cb = str(b.get("callback_data", ""))
                                cur_row.append(InlineButton(text=b_text, callback_data=b_cb))
                                opt_idx += 1
                                oid = (
                                    b_cb.rsplit(":", 1)[-1]
                                    if b_cb.startswith("defer:") and ":" in b_cb
                                    else str(opt_idx)
                                )
                                opt_objs.append({"id": oid, "label": b_text})
                            if cur_row:
                                rebuilt.append(cur_row)
                        if rebuilt:
                            buttons = rebuilt
                except Exception:
                    buttons = None

            try:
                msg_id = self.channel.send_message(
                    str(row["text"]),
                    thread_id=row.get("thread_id"),
                    buttons=buttons,
                )
                if not msg_id:
                    raise RuntimeError("channel.send_message returned None")
            except Exception as exc:
                attempts = int(row.get("attempts") or 0)
                backoff_idx = min(attempts, len(OUTBOX_BACKOFF_SCHEDULE) - 1)
                backoff_sec = OUTBOX_BACKOFF_SCHEDULE[backoff_idx]
                self.db.mark_outbox_retry(
                    outbox_id=int(row["id"]),
                    error=str(exc) or exc.__class__.__name__,
                    next_attempt_at=now + backoff_sec,
                    max_retries=max_retries,
                    now=now,
                )
                continue

            self.db.mark_outbox_sent(int(row["id"]), now=now)
            sent_count += 1
            sent_in_window += 1

            skey = row.get("session_key")
            if lvl in {"question", "review"} and skey and opt_objs:
                self.db.create_deferred_action(
                    channel_msg_id=str(msg_id),
                    session_key=str(skey),
                    level=lvl,
                    options=opt_objs,
                    ttl_hours=self.config.tuning.deferred_approval_ttl_hours,
                    now=now,
                )

        return sent_count

    def run_forever(self, poll_interval: float = 2.0) -> None:
        pid_file = self.hq_dir / ".hq" / "daemon.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        chan_name = self.channel.name if self.channel else "none (offline)"
        print(
            f"[hq-daemon] running pid={os.getpid()} hq_dir={self.hq_dir} "
            f"agent={self.agent_adapter.name} terminal={self.terminal_driver.name} "
            f"channel={chan_name}",
            flush=True,
        )

        def _handle_sig(_signum: int, _frame: Any) -> None:
            self._stop_requested = True

        signal.signal(signal.SIGTERM, _handle_sig)
        signal.signal(signal.SIGINT, _handle_sig)

        try:
            self._ensure_general_pane()
        except Exception as exc:
            print(
                f"[hq-daemon] warn: failed to pre-start {GENERAL_PANE_NAME}: {exc}",
                flush=True,
            )

        try:
            self._restore_manual_herdr_sessions()
        except Exception as exc:
            print(
                f"[hq-daemon] warn: failed to restore manual Herdr sessions: {exc}",
                flush=True,
            )

        try:
            while not self._stop_requested:
                self.run_once()
                time.sleep(poll_interval)
        finally:
            if pid_file.exists():
                pid_file.unlink()
            self.close()
