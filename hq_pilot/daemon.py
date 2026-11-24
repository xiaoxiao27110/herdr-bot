"""0-LLM deterministic daemon event loop for hq-pilot (100% interactive terminal sessions)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
import json
import os
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
    scan_distill_candidates,
    scan_pane_blocked,
    scan_repo_commits,
)
from hq_pilot.prompt_builder import build_prompt
from hq_pilot.syncer import sync_hq_repo
from hq_pilot.terminals.base import TerminalDriver, git_worktree_add
from hq_pilot.terminals.registry import get_terminal_driver


GENERAL_PANE_NAME = "hq-general"
GENERAL_TRACKS = {"GENERAL", "B"}
TOPIC_TRACKS = {"TOPIC", "C"}
OUTBOX_BACKOFF_SCHEDULE = [10, 30, 120, 300, 600]


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


@dataclass
class TickSummary:
    events_processed: int = 0
    events_failed: int = 0
    distill_enqueued: int = 0
    commits_enqueued: int = 0
    timers_fired: int = 0
    nightly_ran: bool = False
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

        if not msg.thread_id and text.startswith("/task "):
            parts = text.split(None, 3)
            if len(parts) >= 4:
                _, repo_name, slug, task_prompt = parts
                sess = self.spawn_task(repo_name, slug, task_prompt, now=now)
                if self.channel:
                    self.channel.send_message(
                        f"Spawned #Topic task `{sess['session_key']}` in pane `{sess['pane_name']}` "
                        f"(branch `{sess['branch_name']}`, thread `{sess.get('channel_thread_id') or '-'}`)."
                    )
                return

        if msg.thread_id:
            sess = self.db.get_session_by_thread(msg.thread_id)
            if text == "/done" and sess:
                self.seal_session(str(sess["session_key"]), now=now)
                return

            if sess and sess["status"] == "sealed":
                # Sealed topic interception: refuse direct injection, offer 2 options
                self.db.set_meta(f"pending_sealed_msg:{msg.thread_id}", text, now=now)
                if self.channel:
                    buttons = [
                        [
                            InlineButton(
                                text="1. 基于当前分支开新短会话",
                                callback_data=f"seal_new:{sess['session_key']}",
                            ),
                            InlineButton(
                                text="2. 解锁转 Pinned 续聊",
                                callback_data=f"seal_pin:{sess['session_key']}",
                            ),
                        ]
                    ]
                    self.channel.send_message(
                        f"Session `{sess['session_key']}` is sealed. Choose how to proceed:",
                        thread_id=msg.thread_id,
                        buttons=buttons,
                    )
                return

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
                "thread_id": msg.thread_id,
                "sender_id": msg.sender_id,
                "text": text,
            },
            now=now,
        )

    def _handle_callback(self, msg: InboundMessage, now: int) -> None:
        data = msg.callback_data or ""
        if self.channel and msg.callback_id:
            self.channel.answer_callback(msg.callback_id, "Received")

        if data.startswith("seal_new:"):
            old_key = data.split(":", 1)[1]
            old_sess = self.db.get_session(old_key)
            thread_id = msg.thread_id or (old_sess.get("channel_thread_id") if old_sess else None)
            pending_text = (
                self.db.get_meta(f"pending_sealed_msg:{thread_id}")
                if thread_id
                else None
            ) or "Continue work on current branch."
            new_key = f"thread:{thread_id or 'main'}:{now}"
            topic_slug = old_sess.get("topic_slug") if old_sess else None
            pane_name = (
                f"hq-{topic_slug}"
                if topic_slug
                else (f"hq-{thread_id}" if thread_id else GENERAL_PANE_NAME)
            )
            self.db.create_session(
                session_key=new_key,
                track="TOPIC",
                agent_backend=self.agent_adapter.name,
                status="active",
                channel_thread_id=thread_id,
                topic_slug=topic_slug,
                repo_name=old_sess.get("repo_name") if old_sess else None,
                branch_name=old_sess.get("branch_name") if old_sess else None,
                worktree_path=old_sess.get("worktree_path") if old_sess else None,
                pane_name=pane_name,
                now=now,
            )
            self.db.enqueue_event(
                source="channel",
                dedupe_key=f"seal_new_exec:{new_key}",
                priority=10,
                available_at=now,
                payload={
                    "session_key": new_key,
                    "thread_id": thread_id,
                    "text": pending_text,
                },
                now=now,
            )
            return

        if data.startswith("seal_pin:"):
            old_key = data.split(":", 1)[1]
            self.db.pin_session(old_key, reason="channel_unlock", now=now)
            old_sess = self.db.get_session(old_key)
            thread_id = msg.thread_id or (old_sess.get("channel_thread_id") if old_sess else None)
            pending_text = (
                self.db.get_meta(f"pending_sealed_msg:{thread_id}")
                if thread_id
                else None
            )
            if pending_text:
                self.db.enqueue_event(
                    source="channel",
                    dedupe_key=f"seal_pin_exec:{old_key}:{now}",
                    priority=10,
                    available_at=now,
                    payload={
                        "session_key": old_key,
                        "thread_id": thread_id,
                        "text": pending_text,
                    },
                    now=now,
                )
            return

        if data.startswith("defer:"):
            rest = data[len("defer:"):]
            if ":" in rest:
                skey, choice_idx = rest.rsplit(":", 1)
            else:
                skey, choice_idx = rest, ""
            choice_label = choice_idx
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
                        "thread_id": msg.thread_id,
                    },
                    now=now,
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
        """Spawn a #Topic interactive worktree + thread + TerminalDriver agent pane."""
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

        thread_id = None
        if self.channel is not None:
            thread_id = self.channel.create_thread(f"[{repo_name}] {slug}")

        pane_name = f"hq-{slug}"
        skey = f"task:{repo_name}:{slug}:{ts}"
        sess = self.db.create_session(
            session_key=skey,
            track="TOPIC",
            agent_backend=self.agent_adapter.name,
            status="active",
            channel_thread_id=thread_id,
            topic_slug=slug,
            repo_name=repo_name,
            branch_name=branch_name,
            worktree_path=str(wt_path),
            pane_name=pane_name,
            now=ts,
        )

        self.agent_adapter.ensure_hooks_installed(wt_path, self._hook_bin_path())
        self.db.record_dispatch(skey, None, prompt, now=ts)
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
        trust_args = self.agent_adapter.extra_trust_args([wt_path, self.hq_dir])
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
        if prompt:
            self.terminal_driver.prompt_agent(pane_name, prompt, wait=False)
        return sess

    # ------------------------------------------------------------------
    # Two-Tier Interactive Session Management (#General + #Topic)
    # ------------------------------------------------------------------
    def _ensure_general_session(self, now: int) -> Dict[str, Any]:
        for s in self.db.list_sessions(statuses=["active", "pinned"]):
            if s["track"] in GENERAL_TRACKS:
                if s.get("pane_name") != GENERAL_PANE_NAME:
                    self.db.update_session(
                        str(s["session_key"]), pane_name=GENERAL_PANE_NAME
                    )
                    s["pane_name"] = GENERAL_PANE_NAME
                return s
        seq = int(self.db.get_meta("general_seq", "0") or "0") + 1
        self.db.set_meta("general_seq", str(seq), now=now)
        return self.db.create_session(
            session_key=f"general:{seq}",
            track="GENERAL",
            agent_backend=self.agent_adapter.name,
            status="active",
            pane_name=GENERAL_PANE_NAME,
            now=now,
        )

    def _seal_idle_general_sessions(self, now: int) -> None:
        idle_limit = self.config.tuning.general_idle_seal_minutes * 60
        max_turns = self.config.tuning.general_max_turns
        for s in self.db.list_sessions(statuses=["active"]):
            if s["track"] not in GENERAL_TRACKS:
                continue
            idle_sec = now - int(s["last_active_at"])
            if idle_sec >= idle_limit or int(s["turn_count"]) >= max_turns:
                self.db.seal_session(str(s["session_key"]), now=now)

    def _seal_idle_topic_sessions(self, now: int) -> None:
        idle_limit = self.config.tuning.topic_idle_seal_minutes * 60
        if idle_limit <= 0:
            return
        for s in self.db.list_sessions(statuses=["active"]):
            if s["track"] not in TOPIC_TRACKS:
                continue
            if int(s.get("pinned") or 0) != 0:
                continue
            idle_sec = now - int(s["last_active_at"])
            if idle_sec >= idle_limit:
                skey = str(s["session_key"])
                if self.db.seal_session(skey, now=now) and s.get("topic_slug"):
                    t_slug = str(s["topic_slug"])
                    t_repo = str(s.get("repo_name") or "")
                    t_branch = str(s.get("branch_name") or "")
                    _upsert_task_progress_file(
                        self.hq_dir, t_slug, t_repo, t_branch, skey, "", status="sealed"
                    )
                    _upsert_active_wip_index(
                        self.hq_dir, t_slug, t_repo, t_branch, status="sealed"
                    )

    def seal_session(
        self,
        session_key: str,
        reason: str = "",
        now: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Explicitly seal a session (preserving its worktree until nightly safe archive)."""
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
        thread_id = updated.get("channel_thread_id")
        if not was_sealed and thread_id and self.channel is not None:
            reason_note = f" ({reason.strip()})" if reason and reason.strip() else ""
            try:
                self.channel.send_message(
                    f"Session `{skey}` sealed{reason_note}. Worktree preserved until nightly archive.",
                    thread_id=str(thread_id),
                )
            except Exception:
                pass
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
        pane_name = self._resolve_pane_name(session)
        live_panes = {p.name for p in self.terminal_driver.list_panes()}
        if pane_name not in live_panes:
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
                session_id=session.get("agent_session_id"),
                model=self.config.agent.model,
                effort=effort,
                add_dirs=add_dirs,
            )
            trust_args = self.agent_adapter.extra_trust_args([cwd, *(add_dirs or [])])
            self.terminal_driver.start_agent(
                name=pane_name,
                cwd=cwd,
                kind=self.agent_adapter.name,
                agent_args=[*interactive_cmd[1:], *trust_args],
                env={
                    "HQ_DIR": str(self.hq_dir),
                    "HQ_SESSION_KEY": str(session["session_key"]),
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

    def _dispatch_interactive(
        self,
        session: Dict[str, Any],
        prompt: str,
        effort: str,
        now: int,
        cwd: Optional[Path] = None,
        wait: bool = True,
    ) -> str:
        skey = str(session["session_key"])
        target_cwd = cwd or (
            Path(session["worktree_path"]) if session.get("worktree_path") else self.hq_dir
        )
        pane_name = self._ensure_session_pane(session, cwd=target_cwd, effort=effort)

        agent_sid = session.get("agent_session_id")
        prev_turns = int(session.get("turn_count") or 0)
        self.db.record_dispatch(skey, agent_sid, prompt, now=now)
        self.db.delete_meta(f"last_reply:{skey}")
        self.db.set_meta(f"daemon_waiting:{skey}", "1", now=now)
        try:
            fallback_reply = self.terminal_driver.prompt_agent(
                pane_name, prompt, wait=wait
            )
        finally:
            self.db.delete_meta(f"daemon_waiting:{skey}")

        hook_reply = self.db.get_meta(f"last_reply:{skey}")
        if hook_reply is not None:
            self.db.delete_meta(f"last_reply:{skey}")

        reply = hook_reply if (hook_reply and hook_reply.strip()) else (fallback_reply or "")
        fresh = self.db.get_session(skey) or session
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

    # ------------------------------------------------------------------
    # Event Execution
    # ------------------------------------------------------------------
    def _process_event(self, ev: EventRow, now: int) -> None:
        if ev.source == "channel":
            payload = ev.payload
            thread_id = payload.get("thread_id")
            explicit_key = payload.get("session_key")
            if explicit_key:
                sess = self.db.get_session(str(explicit_key)) or self._ensure_general_session(now)
            elif thread_id:
                sess = self.db.get_session_by_thread(str(thread_id))
                if not sess:
                    sess = self.db.create_session(
                        session_key=f"thread:{thread_id}:{now}",
                        track="TOPIC",
                        agent_backend=self.agent_adapter.name,
                        status="active",
                        channel_thread_id=str(thread_id),
                        pane_name=f"hq-{thread_id}",
                        now=now,
                    )
            else:
                sess = self._ensure_general_session(now)

            pane_name = self._resolve_pane_name(sess)
            live_panes = {p.name for p in self.terminal_driver.list_panes()}
            is_live_topic = (sess.get("track") in TOPIC_TRACKS) and (pane_name in live_panes)
            recent_outbound = self._get_recent_outbound()
            topic_slug = (
                str(sess["topic_slug"])
                if (sess.get("track") in TOPIC_TRACKS and sess.get("topic_slug"))
                else None
            )
            prompt = (
                str(payload.get("text") or "")
                if is_live_topic
                else build_prompt(
                    self.hq_dir,
                    self.config,
                    payload,
                    recent_outbound=recent_outbound,
                    topic_slug=topic_slug,
                )
            )

            indicator_id = (
                self.channel.send_working_indicator("⏳ 收到，正在处理中...", thread_id=thread_id)
                if self.channel
                else None
            )
            try:
                reply = self._dispatch_interactive(
                    sess,
                    prompt,
                    effort=self.config.agent.default_effort,
                    now=now,
                    cwd=Path(sess["worktree_path"]) if sess.get("worktree_path") else self.hq_dir,
                    wait=True,
                )
            except Exception as exc:
                if self.channel and indicator_id:
                    self.channel.finish_working_indicator(
                        indicator_id, f"❌ 处理失败：{exc}", thread_id=thread_id
                    )
                raise
            if self.channel:
                if reply:
                    if indicator_id and self.channel.finish_working_indicator(
                        indicator_id, reply, thread_id=thread_id
                    ):
                        pass
                    else:
                        self.channel.send_message(reply, thread_id=thread_id)
                elif indicator_id:
                    self.channel.finish_working_indicator(
                        indicator_id, "✅ 已完成（无额外文本回复）", thread_id=thread_id
                    )
            return

        if ev.source == "git_commit":
            repo_name = str(ev.payload.get("repo", ""))
            sess = self._ensure_general_session(now)
            recent_outbound = self._get_recent_outbound()
            prompt = build_prompt(
                self.hq_dir,
                self.config,
                ev.payload,
                matched_repos=[repo_name] if repo_name else None,
                recent_outbound=recent_outbound,
            )
            self._dispatch_interactive(
                sess,
                prompt,
                effort=self.config.agent.light_effort,
                now=now,
                cwd=self.hq_dir,
                wait=True,
            )
            return

        if ev.source == "session_distill":
            skey = str(ev.payload.get("session_key", ""))
            agent_sid = str(ev.payload.get("agent_session_id", ""))
            tpath = str(ev.payload.get("transcript_path", ""))
            to_line = int(ev.payload.get("to_line", 0))

            sess = self._ensure_general_session(now)
            recent_outbound = self._get_recent_outbound()
            prompt = build_prompt(
                self.hq_dir,
                self.config,
                ev.payload,
                recent_outbound=recent_outbound,
            )
            reply = self._dispatch_interactive(
                sess,
                prompt,
                effort=self.config.agent.light_effort,
                now=now,
                cwd=self.hq_dir,
                wait=True,
            )

            if reply.strip():
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
            recent_outbound = self._get_recent_outbound()
            prompt = build_prompt(
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
            payload = ev.payload
            target_key = payload.get("session_key")
            target_sess = self.db.get_session(str(target_key)) if target_key else None
            if target_sess and target_sess.get("status") in {"active", "pinned"}:
                sess = target_sess
            else:
                sess = self._ensure_general_session(now)

            recent_outbound = self._get_recent_outbound()
            topic_slug = (
                str(sess["topic_slug"])
                if (sess.get("track") in TOPIC_TRACKS and sess.get("topic_slug"))
                else None
            )
            prompt = build_prompt(
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
            thread_id = ev.payload.get("channel_thread_id")
            if self.channel:
                msg_id = self.channel.send_message(
                    f"[Review] Managed pane for session `{skey}` appears blocked. Choose action:",
                    thread_id=thread_id,
                    buttons=[
                        [
                            InlineButton(text="Continue", callback_data=f"pane_continue:{skey}"),
                            InlineButton(text="Seal", callback_data=f"pane_seal:{skey}"),
                        ]
                    ],
                )
                if msg_id:
                    self.db.create_deferred_action(
                        channel_msg_id=msg_id,
                        session_key=skey,
                        level="review",
                        options=[
                            {"id": "continue", "label": "Continue"},
                            {"id": "seal", "label": "Seal"},
                        ],
                        ttl_hours=self.config.tuning.deferred_approval_ttl_hours,
                        now=now,
                    )
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

        # Step 1: Dreaming (routed to #General interactive pane)
        last_dream_at = int(self.db.get_meta("last_dream_at", "0") or "0")
        sessions_since = int(self.db.get_meta("sessions_since_dream", "0") or "0")
        interval_ok = (ts - last_dream_at) >= self.config.tuning.dream_min_interval_hours * 3600
        sessions_ok = sessions_since >= self.config.tuning.dream_min_sessions

        if self.config.tuning.dream_force_nightly or (interval_ok and sessions_ok):
            dream_sess = self._ensure_general_session(ts)
            prompt = build_prompt(
                self.hq_dir,
                self.config,
                {"task": "dream-consolidate", "timestamp": ts},
            )
            try:
                self._dispatch_interactive(
                    dream_sess,
                    prompt,
                    effort=self.config.agent.default_effort,
                    now=ts,
                    cwd=self.hq_dir,
                    wait=True,
                )
            except Exception as exc:
                self.db.add_digest_item(
                    level="alert",
                    category="dream_error",
                    message=f"Nightly dreaming failed: {exc}",
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
        sync_hq_repo(self.hq_dir, commit_message="hq-pilot: nightly memory closeout", push=False)
        return report

    def run_morning_digest(self, now: Optional[int] = None) -> bool:
        ts = int(now if now is not None else time.time())
        items = self.db.list_pending_digest_items()
        warn_sec = self.config.tuning.dirty_worktree_warn_hours * 3600
        lines: List[str] = ["[L0 Morning Digest]"]
        for it in items:
            lines.append(f"- [{it['category']}] {it['message']}")
        for sess in self.db.list_sessions(statuses=["sealed", "pinned"]):
            if sess.get("archive_blocked_reason") and (ts - int(sess["last_active_at"])) >= warn_sec:
                lines.append(
                    f"- [dirty_stale] `{sess['session_key']}` blocked > {self.config.tuning.dirty_worktree_warn_hours}h at `{sess.get('worktree_path')}`"
                )
        if len(lines) == 1:
            lines.append("- All clear. No pending alerts or blocked worktrees.")
        if self.channel:
            self.channel.send_message("\n".join(lines))
        self.db.mark_digest_items_sent([int(i["id"]) for i in items], now=ts)
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
        for sess in self.db.list_sessions(statuses=["active", "sealed"]):
            if int(sess.get("pinned") or 0) == 0 and sess.get("pane_name"):
                detect_manual_touch(
                    self.db, sess, terminal_driver=self.terminal_driver, now=ts
                )
        self._seal_idle_general_sessions(ts)
        self._seal_idle_topic_sessions(ts)
        summary.timers_fired = self._tick_custom_timers(ts)

        events = self.db.claim_next_events(
            limit=10,
            now=ts,
            lease_ttl_sec=self.config.tuning.inbox_lease_ttl_sec,
        )
        for ev in events:
            try:
                self._process_event(ev, ts)
                self.db.complete_event(ev.id)
                summary.events_processed += 1
                print(
                    f"[hq-daemon] done event={ev.id} source={ev.source}",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"[hq-daemon] error event={ev.id} source={ev.source}: {exc}",
                    flush=True,
                )
                self.db.fail_event(
                    ev.id,
                    max_attempts=self.config.tuning.inbox_max_attempts,
                    now=ts,
                    error_msg=str(exc),
                )
                summary.events_failed += 1

        dt = datetime.fromtimestamp(ts)
        hhmm = dt.strftime("%H:%M")
        today_str = dt.strftime("%Y-%m-%d")

        if hhmm >= self.config.tuning.dream_cron and self.db.get_meta("last_dream_date") != today_str:
            self.run_nightly_pipeline(now=ts)
            self.db.set_meta("last_dream_date", today_str, now=ts)
            summary.nightly_ran = True

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
            while not self._stop_requested:
                self.run_once()
                time.sleep(poll_interval)
        finally:
            if pid_file.exists():
                pid_file.unlink()
            self.close()
