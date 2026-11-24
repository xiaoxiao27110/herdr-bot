"""0-LLM deterministic daemon event loop for hq-pilot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
import json
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
    nightly_ran: bool = False
    digest_sent: bool = False


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
            )
        )
        self.cmd_runner: CmdRunner = cmd_runner or default_cmd_runner
        self._stop_requested = False

    def close(self) -> None:
        self.db.close()

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
                        f"Spawned Track C task `{sess['session_key']}` in pane `{sess['pane_name']}` "
                        f"(branch `{sess['branch_name']}`, thread `{sess.get('channel_thread_id') or '-'}`)."
                    )
                return

        if msg.thread_id:
            sess = self.db.get_session_by_thread(msg.thread_id)
            if text == "/done" and sess:
                self.db.seal_session(str(sess["session_key"]), now=now)
                if self.channel:
                    self.channel.send_message(
                        f"Session `{sess['session_key']}` sealed. Worktree preserved until nightly archive.",
                        thread_id=msg.thread_id,
                    )
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

        self.db.enqueue_event(
            source="channel",
            dedupe_key=f"channel:{msg.update_id}",
            priority=10,
            available_at=now,
            payload={
                "update_id": msg.update_id,
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
            self.db.create_session(
                session_key=new_key,
                track="C",
                agent_backend=self.agent_adapter.name,
                status="active",
                channel_thread_id=thread_id,
                repo_name=old_sess.get("repo_name") if old_sess else None,
                branch_name=old_sess.get("branch_name") if old_sess else None,
                worktree_path=old_sess.get("worktree_path") if old_sess else None,
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
            parts = data.split(":", 2)
            skey = parts[1] if len(parts) > 1 else ""
            choice_idx = parts[2] if len(parts) > 2 else ""
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
        """Spawn a Track C interactive worktree + Topic + TerminalDriver pane."""
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
            track="C",
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

        hook_bin = self.config.local.bin_overrides.get(
            "hq-agent-hook",
            str(Path(__file__).resolve().parent.parent / "bin" / "hq-agent-hook"),
        )
        self.agent_adapter.ensure_hooks_installed(wt_path, hook_bin)
        self.db.record_dispatch(skey, None, prompt, now=ts)

        interactive_cmd = self.agent_adapter.build_interactive_cmd(
            initial_prompt=prompt,
            model=self.config.agent.model,
            effort=self.config.agent.default_effort,
        )
        pane_cmd = [
            "env",
            f"HQ_DIR={self.hq_dir}",
            f"HQ_SESSION_KEY={skey}",
            *interactive_cmd,
        ]
        self.terminal_driver.run_bg(name=pane_name, cwd=wt_path, cmd=pane_cmd)
        return sess

    # ------------------------------------------------------------------
    # Event Execution
    # ------------------------------------------------------------------
    def _ensure_general_session(self, now: int) -> Dict[str, Any]:
        for s in self.db.list_sessions(statuses=["active", "pinned"]):
            if s["track"] == "B":
                return s
        seq = int(self.db.get_meta("general_seq", "0") or "0") + 1
        self.db.set_meta("general_seq", str(seq), now=now)
        return self.db.create_session(
            session_key=f"general:{seq}",
            track="B",
            agent_backend=self.agent_adapter.name,
            status="active",
            now=now,
        )

    def _seal_idle_general_sessions(self, now: int) -> None:
        idle_limit = self.config.tuning.general_idle_seal_minutes * 60
        max_turns = self.config.tuning.general_max_turns
        for s in self.db.list_sessions(statuses=["active"]):
            if s["track"] != "B":
                continue
            idle_sec = now - int(s["last_active_at"])
            if idle_sec >= idle_limit or int(s["turn_count"]) >= max_turns:
                self.db.seal_session(str(s["session_key"]), now=now)

    def _dispatch_headless(
        self,
        session: Dict[str, Any],
        prompt: str,
        effort: str,
        now: int,
        cwd: Optional[Path] = None,
    ) -> str:
        skey = str(session["session_key"])
        agent_sid = session.get("agent_session_id")
        self.db.record_dispatch(skey, agent_sid, prompt, now=now)

        add_dirs = [
            self.config.get_repo_path(r)
            for r in sorted(self.config.repos.keys())
            if self.config.get_repo_path(r).exists()
        ]
        cmd = self.agent_adapter.build_headless_cmd(
            prompt=prompt,
            session_id=agent_sid,
            model=self.config.agent.model,
            effort=effort,
            add_dirs=add_dirs,
        )
        res = self.cmd_runner(cmd, cwd or self.hq_dir)
        if res.returncode != 0:
            raise RuntimeError(
                f"Agent command failed ({res.returncode}): {res.stderr.strip()}"
            )
        parsed = self.agent_adapter.parse_headless_output(res.stdout)
        new_turns = int(session.get("turn_count") or 0) + 1
        updates: Dict[str, Any] = {
            "turn_count": new_turns,
            "last_active_at": now,
        }
        if parsed.agent_session_id and not agent_sid:
            updates["agent_session_id"] = parsed.agent_session_id
        self.db.update_session(skey, **updates)
        return parsed.reply_markdown

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
                        track="C",
                        agent_backend=self.agent_adapter.name,
                        status="active",
                        channel_thread_id=str(thread_id),
                        now=now,
                    )
            else:
                sess = self._ensure_general_session(now)

            pane_name = sess.get("pane_name")
            if sess.get("track") == "C" and pane_name:
                live_panes = {p.name for p in self.terminal_driver.list_panes()}
                if str(pane_name) in live_panes:
                    text = str(payload.get("text") or "")
                    self.db.record_dispatch(
                        str(sess["session_key"]),
                        sess.get("agent_session_id"),
                        text,
                        now=now,
                    )
                    self.terminal_driver.send_text(str(pane_name), text, enter=True)
                    self.db.update_session(str(sess["session_key"]), last_active_at=now)
                    return

            prompt = build_prompt(self.hq_dir, self.config, payload)
            reply = self._dispatch_headless(
                sess,
                prompt,
                effort=self.config.agent.default_effort,
                now=now,
                cwd=Path(sess["worktree_path"]) if sess.get("worktree_path") else self.hq_dir,
            )
            if self.channel and reply:
                self.channel.send_message(reply, thread_id=thread_id)
            return

        if ev.source == "git_commit":
            repo_name = str(ev.payload.get("repo", ""))
            sess = self.db.create_session(
                session_key=f"event:{ev.id}",
                track="A",
                agent_backend=self.agent_adapter.name,
                status="active",
                repo_name=repo_name,
                now=now,
            )
            prompt = build_prompt(
                self.hq_dir,
                self.config,
                ev.payload,
                matched_repos=[repo_name] if repo_name else None,
            )
            self._dispatch_headless(
                sess, prompt, effort=self.config.agent.light_effort, now=now
            )
            self.db.seal_session(sess["session_key"], now=now)
            return

        if ev.source == "session_distill":
            skey = str(ev.payload.get("session_key", ""))
            agent_sid = str(ev.payload.get("agent_session_id", ""))
            tpath = str(ev.payload.get("transcript_path", ""))
            to_line = int(ev.payload.get("to_line", 0))
            turns = ev.payload.get("turns") or []

            distill_sess = self.db.create_session(
                session_key=f"event:{ev.id}",
                track="A",
                agent_backend=self.agent_adapter.name,
                status="active",
                now=now,
            )
            prompt = build_prompt(self.hq_dir, self.config, ev.payload)
            reply = self._dispatch_headless(
                distill_sess, prompt, effort=self.config.agent.light_effort, now=now
            )
            self.db.seal_session(distill_sess["session_key"], now=now)

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
            sess = self.db.get_session(skey)
            if not sess:
                return
            prompt = build_prompt(self.hq_dir, self.config, ev.payload)
            reply = self._dispatch_headless(
                sess,
                prompt,
                effort=self.config.agent.default_effort,
                now=now,
                cwd=Path(sess["worktree_path"]) if sess.get("worktree_path") else self.hq_dir,
            )
            if sess.get("track") == "A":
                self.db.seal_session(skey, now=now)
            thread_id = ev.payload.get("thread_id") or sess.get("channel_thread_id")
            if self.channel and reply:
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

        # Step 1: Dreaming
        last_dream_at = int(self.db.get_meta("last_dream_at", "0") or "0")
        sessions_since = int(self.db.get_meta("sessions_since_dream", "0") or "0")
        interval_ok = (ts - last_dream_at) >= self.config.tuning.dream_min_interval_hours * 3600
        sessions_ok = sessions_since >= self.config.tuning.dream_min_sessions

        if self.config.tuning.dream_force_nightly or (interval_ok and sessions_ok):
            dream_sess = self.db.create_session(
                session_key=f"event:dream:{ts}",
                track="A",
                agent_backend=self.agent_adapter.name,
                status="active",
                now=ts,
            )
            prompt = build_prompt(
                self.hq_dir,
                self.config,
                {"task": "dream-consolidate", "timestamp": ts},
            )
            try:
                self._dispatch_headless(
                    dream_sess, prompt, effort=self.config.agent.default_effort, now=ts
                )
            except Exception as exc:
                self.db.add_digest_item(
                    level="alert",
                    category="dream_error",
                    message=f"Nightly dreaming failed: {exc}",
                    now=ts,
                )
            finally:
                self.db.seal_session(dream_sess["session_key"], now=ts)

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
            except Exception as exc:
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

        return summary

    def run_forever(self, poll_interval: float = 2.0) -> None:
        pid_file = self.hq_dir / ".hq" / "daemon.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        def _handle_sig(_signum: int, _frame: Any) -> None:
            self._stop_requested = True

        signal.signal(signal.SIGTERM, _handle_sig)
        signal.signal(signal.SIGINT, _handle_sig)

        try:
            while not self._stop_requested:
                self.run_once()
                time.sleep(poll_interval)
        finally:
            if pid_file.exists():
                pid_file.unlink()
            self.close()
