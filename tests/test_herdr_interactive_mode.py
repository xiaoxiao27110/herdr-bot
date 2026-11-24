"""Tests for pure interactive Herdr/Terminal two-tier architecture and zero headless `-p` calls."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hq_pilot.agents.agy import AgyAdapter
from hq_pilot.channels.base import InboundMessage
from hq_pilot.cli import main as hq_main
from hq_pilot.daemon import GENERAL_PANE_NAME, HQDaemon
from hq_pilot.db import HQDatabase
from hq_pilot.hook_cli import handle_hook_event
from hq_pilot.terminals.base import PaneInfo, TerminalDriver
from hq_pilot.terminals.herdr import (
    HerdrDriver,
    clean_agent_terminal_output,
    sanitize_herdr_agent_name,
)
from hq_pilot.terminals.tmux import TmuxDriver

try:
    from tests.test_adapters_and_channels import FakeChannel
    from tests.test_archiver_resume_cursors import _init_git_repo
    from tests.test_review_fixes import RecordingTerminal
except ModuleNotFoundError:
    from test_adapters_and_channels import FakeChannel
    from test_archiver_resume_cursors import _init_git_repo
    from test_review_fixes import RecordingTerminal


class NoHeadlessAgyAdapter(AgyAdapter):
    """AgyAdapter subclass that fails loudly if any headless `-p` method is invoked."""

    def build_headless_cmd(self, *args, **kwargs):
        raise AssertionError("build_headless_cmd / -p must NEVER be called by HQDaemon")

    def parse_headless_output(self, *args, **kwargs):
        raise AssertionError("parse_headless_output must NEVER be called by HQDaemon")


class IndicatorChannel(FakeChannel):
    """FakeChannel with working-indicator card refresh tracking."""

    def __init__(self, updates=None):
        super().__init__(updates=updates)
        self.indicators = []
        self.finished_indicators = []

    def send_working_indicator(self, text="⏳ 收到，正在处理中...", thread_id=None):
        mid = f"ind-{len(self.indicators) + 1}"
        self.indicators.append({"id": mid, "text": text, "thread_id": thread_id})
        return mid

    def finish_working_indicator(self, message_id, text, thread_id=None):
        self.finished_indicators.append({"id": message_id, "text": text, "thread_id": thread_id})
        return True


class TestHerdrInteractiveMode(unittest.TestCase):
    def test_sanitize_herdr_agent_name_rules(self) -> None:
        pat = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
        samples = [
            "hq-general",
            "hq-fix-cors",
            "HQ_Upper/Slash!Name",
            "123-leading-digit",
            "",
            "hq-" + "very-long-topic-slug-name-" * 4,
        ]
        for raw in samples:
            norm = sanitize_herdr_agent_name(raw)
            self.assertRegex(norm, pat, msg=f"Invalid normalized name for {raw!r}: {norm!r}")
        self.assertEqual(sanitize_herdr_agent_name("hq-general"), "hq-general")
        self.assertEqual(sanitize_herdr_agent_name("hq-fix-cors"), "hq-fix-cors")
        # Distinct long names avoid collision via hash suffix
        n1 = sanitize_herdr_agent_name("hq-super-long-feature-branch-alpha-001")
        n2 = sanitize_herdr_agent_name("hq-super-long-feature-branch-alpha-002")
        self.assertNotEqual(n1, n2)

    def test_herdr_driver_start_agent_prompt_agent_and_list_panes(self) -> None:
        calls = []

        def fake_run(self_drv, args, check=True):
            calls.append(list(args))
            if args == ["status", "--json"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout=json.dumps({"server": {"running": True}}), stderr=""
                )
            if args == ["agent", "list"]:
                if any(c[:2] == ["agent", "start"] for c in calls):
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        stdout=json.dumps(
                            {
                                "result": {
                                    "agents": [
                                        {
                                            "name": "hq-general",
                                            "agent": "agy",
                                            "agent_status": "idle",
                                            "pane_id": "w1:p1",
                                            "workspace_id": "w1",
                                            "cwd": "/tmp/hq",
                                        }
                                    ]
                                }
                            }
                        ),
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    args, 0, stdout=json.dumps({"result": {"agents": []}}), stderr=""
                )
            if args[:2] == ["workspace", "create"]:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=json.dumps({"result": {"root_pane": {"pane_id": "w1:p1"}}}),
                    stderr="",
                )
            if args[:2] == ["agent", "start"]:
                return subprocess.CompletedProcess(args, 0, stdout='{"result":{}}', stderr="")
            if args[:2] == ["agent", "prompt"]:
                return subprocess.CompletedProcess(args, 0, stdout='{"result":{}}', stderr="")
            if args[:2] == ["agent", "read"]:
                return subprocess.CompletedProcess(args, 0, stdout="Agent screen reply\n", stderr="")
            if args == ["workspace", "list"]:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=json.dumps(
                        {"result": {"workspaces": [{"workspace_id": "w1", "label": "hq-general"}]}}
                    ),
                    stderr="",
                )
            if args[:2] == ["pane", "list"]:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=json.dumps(
                        {
                            "result": {
                                "panes": [
                                    {
                                        "workspace_id": "w1",
                                        "pane_id": "w1:p1",
                                        "agent_status": "idle",
                                        "cwd": "/tmp/hq",
                                    }
                                ]
                            }
                        }
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        drv = HerdrDriver(bin_path="herdr", session="test-sess")
        with patch.object(HerdrDriver, "_run", fake_run), patch("hq_pilot.terminals.herdr.time.sleep"):
            drv.start_agent(
                name="hq-general",
                cwd="/tmp/hq",
                kind="agy",
                agent_args=["--effort", "high"],
                env={"HQ_DIR": "/tmp/hq", "HQ_SESSION_KEY": "general:1"},
                timeout_ms=60000,
            )
            # Calling start_agent again when live is idempotent
            drv.start_agent(
                name="hq-general",
                cwd="/tmp/hq",
                kind="agy",
                agent_args=["--effort", "high"],
            )
            out_wait = drv.prompt_agent("hq-general", "hello world", wait=True, timeout_ms=120000)
            out_nowait = drv.prompt_agent("hq-general", "fire and forget", wait=False)
            panes = drv.list_panes()

        ws_creates = [c for c in calls if c[:2] == ["workspace", "create"]]
        self.assertEqual(len(ws_creates), 1)
        self.assertIn("--no-focus", ws_creates[0])
        self.assertIn("HQ_DIR=/tmp/hq", ws_creates[0])
        self.assertIn("HQ_SESSION_KEY=general:1", ws_creates[0])

        agent_starts = [c for c in calls if c[:2] == ["agent", "start"]]
        self.assertEqual(len(agent_starts), 1)
        self.assertEqual(
            agent_starts[0],
            ["agent", "start", "hq-general", "--kind", "agy", "--pane", "w1:p1", "--timeout", "60000", "--", "--effort", "high"],
        )

        prompt_calls = [c for c in calls if c[:2] == ["agent", "prompt"]]
        self.assertEqual(
            prompt_calls[0],
            ["agent", "prompt", "hq-general", "hello world", "--wait", "--timeout", "120000"],
        )
        self.assertEqual(prompt_calls[1], ["agent", "prompt", "hq-general", "fire and forget"])

        read_calls = [c for c in calls if c[:2] == ["agent", "read"]]
        self.assertEqual(len(read_calls), 1)
        self.assertEqual(
            read_calls[0],
            ["agent", "read", "hq-general", "--source", "recent-unwrapped", "--lines", "120"],
        )
        self.assertNotIn("--raw", read_calls[0])
        self.assertEqual(out_wait, "Agent screen reply")
        self.assertEqual(out_nowait, "")
        self.assertEqual(len(panes), 1)
        self.assertEqual(panes[0].name, "hq-general")
        self.assertEqual(panes[0].status, "idle")

    def test_tmux_driver_start_agent_and_prompt_agent_fallback(self) -> None:
        tmux = TmuxDriver(bin_path="tmux")
        seen = []

        def fake_run(self_drv, args, check=True):
            seen.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch.object(TmuxDriver, "_run", fake_run):
            tmux.start_agent(
                name="hq-general",
                cwd="/tmp/hq",
                kind="agy",
                agent_args=["--effort", "high"],
                env={"HQ_DIR": "/tmp/hq", "HQ_SESSION_KEY": "general:1"},
            )
            ret = tmux.prompt_agent("hq-general", "hi there", wait=True)

        self.assertEqual(ret, "")
        self.assertEqual(seen[0][:6], ["new-session", "-d", "-s", "hq-general", "-c", "/tmp/hq"])
        self.assertIn("HQ_DIR=/tmp/hq", seen[0][6])
        self.assertIn("HQ_SESSION_KEY=general:1", seen[0][6])
        self.assertIn("agy --effort high", seen[0][6])
        self.assertEqual(seen[1], ["send-keys", "-t", "hq-general", "-l", "hi there"])
        self.assertEqual(seen[2], ["send-keys", "-t", "hq-general", "Enter"])

    def test_all_daemon_event_paths_zero_headless_and_two_tier_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            repo_dir = Path(tmp) / "gateway-svc"
            _init_git_repo(repo_dir)
            (hq_dir / ".hq" / "local.yaml").write_text(
                f"repo_paths:\n  gateway-svc: {repo_dir}\n", encoding="utf-8"
            )

            tfile = Path(tmp) / "session.jsonl"
            tfile.write_text(
                json.dumps({"role": "user", "content": "q1"}) + "\n"
                + json.dumps({"role": "assistant", "content": "a1"}) + "\n",
                encoding="utf-8",
            )

            def forbid_runner(cmd, cwd):
                raise AssertionError(f"cmd_runner must NEVER be called: {cmd}")

            ch = IndicatorChannel()
            term = RecordingTerminal(reply_fn=lambda name, text: f"interactive-reply@{name}")
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
                cmd_runner=forbid_runner,
            )
            try:
                # 1. channel (#General)
                ch.updates.append(
                    InboundMessage(
                        update_id=1,
                        chat_id="c1",
                        thread_id=None,
                        sender_id="u1",
                        text="Hello General",
                        message_id="m1",
                    )
                )
                # 2. git_commit -> #General
                daemon.db.enqueue_event(
                    source="git_commit",
                    dedupe_key="gc:1",
                    priority=20,
                    available_at=1000,
                    payload={"repo": "gateway-svc", "matched_files": ["api/router.py"]},
                    now=1000,
                )
                # 3. session_distill -> #General
                daemon.db.enqueue_event(
                    source="session_distill",
                    dedupe_key="sd:1",
                    priority=30,
                    available_at=1000,
                    payload={
                        "session_key": "general:1",
                        "agent_session_id": "conv-distill-1",
                        "transcript_path": str(tfile),
                        "from_line": 0,
                        "to_line": 2,
                        "turns": [{"role": "user", "text": "q1"}],
                    },
                    now=1000,
                )
                # 4. deferred_resume -> #General
                daemon.db.create_session(
                    "s-def", "TOPIC", "agy", status="active", channel_thread_id="th-def", now=999
                )
                daemon.db.enqueue_event(
                    source="deferred_resume",
                    dedupe_key="dr:1",
                    priority=10,
                    available_at=1000,
                    payload={
                        "session_key": "s-def",
                        "choice_id": "1",
                        "choice_label": "Proceed",
                        "thread_id": "th-def",
                    },
                    now=1000,
                )

                summary = daemon.run_once(now=1000)
                self.assertEqual(summary.events_processed, 4)
                self.assertEqual(summary.events_failed, 0)

                # All 4 routed to hq-general, started once, NEVER with -p
                self.assertEqual(len(term.start_agent_calls), 1)
                self.assertEqual(term.start_agent_calls[0]["name"], GENERAL_PANE_NAME)
                self.assertEqual(term.start_agent_calls[0]["kind"], "agy")
                self.assertNotIn("-p", term.start_agent_calls[0]["agent_args"])
                self.assertNotIn("--print", term.start_agent_calls[0]["agent_args"])
                self.assertEqual(term.start_agent_calls[0]["env"]["HQ_DIR"], str(hq_dir.resolve()))
                self.assertEqual(term.start_agent_calls[0]["env"]["HQ_SESSION_KEY"], "general:1")
                self.assertTrue((hq_dir / ".agents" / "hooks.json").exists())

                self.assertEqual(len(term.prompt_calls), 4)
                self.assertTrue(all(p["name"] == GENERAL_PANE_NAME for p in term.prompt_calls))

                # General session track is GENERAL
                gen = daemon.db.get_session("general:1")
                self.assertEqual(gen["track"], "GENERAL")
                self.assertEqual(gen["pane_name"], GENERAL_PANE_NAME)

                # Working indicator created & refreshed in-place for channel message
                self.assertEqual(len(ch.indicators), 1)
                self.assertEqual(ch.finished_indicators[0]["id"], ch.indicators[0]["id"])
                self.assertEqual(ch.finished_indicators[0]["text"], "interactive-reply@hq-general")

                # Distill wrote to notes/today.md and advanced cursor
                self.assertIn("interactive-reply@hq-general", (hq_dir / "notes" / "today.md").read_text(encoding="utf-8"))
                self.assertEqual(daemon.cursors.get_transcript_cursor("conv-distill-1")["last_summarized_line"], 2)

                # 5. Nightly dream_consolidate also routes to hq-general with zero -p
                daemon.run_nightly_pipeline(now=2000)
                self.assertEqual(term.prompt_calls[-1]["name"], GENERAL_PANE_NAME)
                self.assertIn("dream-consolidate", term.prompt_calls[-1]["text"])
            finally:
                daemon.close()

    def test_stop_hook_last_reply_and_daemon_waiting_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            tfile = Path(tmp) / "gen_transcript.jsonl"
            tfile.write_text(
                json.dumps({"role": "user", "content": "ping"}) + "\n"
                + json.dumps({"role": "assistant", "content": "Clean hook reply!"}) + "\n",
                encoding="utf-8",
            )

            ch = IndicatorChannel()

            class HookSimTerminal(RecordingTerminal):
                def __init__(self, hq_root: Path, transcript: Path, channel_obj):
                    super().__init__(reply_fn=lambda name, text: "Raw noisy terminal fallback")
                    self.hq_root = hq_root
                    self.transcript = transcript
                    self.channel_obj = channel_obj
                    self.hook_result = None

                def prompt_agent(self, name, text, wait=True, timeout_ms=300000):
                    # While daemon is synchronously waiting, Stop hook fires inside agy
                    with patch.dict(os.environ, {"HQ_SESSION_KEY": "general:1"}):
                        self.hook_result = handle_hook_event(
                            "agy",
                            {
                                "hookEventName": "Stop",
                                "conversationId": "conv-gen-1",
                                "transcriptPath": str(self.transcript),
                            },
                            hq_dir=self.hq_root,
                            channel=self.channel_obj,
                            now=1001,
                        )
                    return super().prompt_agent(name, text, wait=wait, timeout_ms=timeout_ms)

            term = HookSimTerminal(hq_dir, tfile, ch)
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                ch.updates.append(
                    InboundMessage(
                        update_id=1,
                        chat_id="c1",
                        thread_id=None,
                        sender_id="u1",
                        text="ping",
                        message_id="m-ping",
                    )
                )
                summary = daemon.run_once(now=1000)
                self.assertEqual(summary.events_processed, 1)

                # Stop hook saw daemon_waiting=1 so it did NOT duplicate send_message
                self.assertIsNotNone(term.hook_result)
                self.assertFalse(term.hook_result["reply_pushed"])
                self.assertEqual(ch.sent, [])

                # Daemon preferred clean Stop-hook last_reply over noisy terminal fallback
                self.assertEqual(len(ch.finished_indicators), 1)
                self.assertEqual(ch.finished_indicators[0]["text"], "Clean hook reply!")

                # Both daemon_waiting and last_reply meta keys are cleaned up
                self.assertIsNone(daemon.db.get_meta("daemon_waiting:general:1"))
                self.assertIsNone(daemon.db.get_meta("last_reply:general:1"))

                # Now simulate manual user prompt in Herdr (daemon_waiting NOT set) -> Stop hook pushes to channel
                with tfile.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"role": "user", "content": "manual input"}) + "\n")
                    fh.write(json.dumps({"role": "assistant", "content": "Manual reply from Herdr"}) + "\n")
                with patch.dict(os.environ, {"HQ_SESSION_KEY": "general:1"}):
                    res_manual = handle_hook_event(
                        "agy",
                        {
                            "hookEventName": "Stop",
                            "conversationId": "conv-gen-1",
                            "transcriptPath": str(tfile),
                        },
                        hq_dir=hq_dir,
                        channel=ch,
                        now=1050,
                    )
                self.assertTrue(res_manual["reply_pushed"])
                self.assertEqual(ch.sent[-1]["text"], "Manual reply from Herdr")
                self.assertEqual(daemon.db.get_meta("last_reply:general:1"), "Manual reply from Herdr")
            finally:
                daemon.close()

    def test_ensure_general_pane_prestart_and_legacy_track_b_compat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            term = RecordingTerminal()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=FakeChannel(),
            )
            try:
                # Seed a legacy track="B" general session from an older DB
                daemon.db.create_session("general:legacy", "B", "agy", status="active", now=100)

                # run_once with no events does NOT eagerly spawn hq-general
                daemon.run_once(now=105)
                self.assertEqual(term.start_agent_calls, [])

                # _ensure_general_pane (called by run_forever) reuses legacy B session and starts hq-general
                sess = daemon._ensure_general_pane(now=110)
                self.assertEqual(sess["session_key"], "general:legacy")
                self.assertEqual(sess["pane_name"], GENERAL_PANE_NAME)
                self.assertEqual(len(term.start_agent_calls), 1)
                self.assertEqual(term.start_agent_calls[0]["name"], GENERAL_PANE_NAME)
            finally:
                daemon.close()

    def test_agy_transcript_real_format_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tfile = Path(tmp) / "transcript_full.jsonl"
            records = [
                {"type": "SYSTEM_MESSAGE", "content": "system bootstrap"},
                {"type": "USER_INPUT", "content": "帮我查下状态", "timestamp": "2026-01-01T00:00:01Z"},
                {
                    "type": "PLANNER_RESPONSE",
                    "thinking": "checking files first",
                    "tool_calls": [{"name": "read_file", "args": {"path": "a.py"}}],
                },
                {"type": "PLANNER_RESPONSE", "content": "   ", "thinking": "still thinking"},
                {
                    "type": "PLANNER_RESPONSE",
                    "content": "状态正常，全部服务已就绪。",
                    "thinking": "done",
                    "timestamp": "2026-01-01T00:00:05Z",
                },
                # Backward compatibility with legacy role/message formats
                {"role": "user", "content": "legacy question"},
                {"type": "model", "message": {"role": "assistant", "content": "legacy answer"}},
            ]
            tfile.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                encoding="utf-8",
            )

            adapter = AgyAdapter()
            turns, total = adapter.read_transcript_slice(tfile, from_line=0)
            self.assertEqual(total, len(records))
            self.assertEqual(
                [(t.role, t.text) for t in turns],
                [
                    ("user", "帮我查下状态"),
                    ("assistant", "状态正常，全部服务已就绪。"),
                    ("user", "legacy question"),
                    ("assistant", "legacy answer"),
                ],
            )

    def test_stop_hook_last_reply_pane_cross_session_key_and_raw_followup_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            tfile = Path(tmp) / "gen_full.jsonl"
            tfile.write_text(
                json.dumps({"type": "USER_INPUT", "content": "第一轮消息"}, ensure_ascii=False) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "thinking": "tool step"}, ensure_ascii=False) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "content": "第一轮干净回复"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            ch = IndicatorChannel()

            class CrossKeyHookTerminal(RecordingTerminal):
                def __init__(self, hq_root: Path, transcript: Path, channel_obj):
                    super().__init__(reply_fn=lambda name, text: "Noisy fallback screen")
                    self.hq_root = hq_root
                    self.transcript = transcript
                    self.channel_obj = channel_obj
                    self.hook_results = []
                    self.turn_idx = 0

                def prompt_agent(self, name, text, wait=True, timeout_ms=300000):
                    self.turn_idx += 1
                    if self.turn_idx == 2:
                        with self.transcript.open("a", encoding="utf-8") as fh:
                            fh.write(
                                json.dumps({"type": "USER_INPUT", "content": "第二轮消息"}, ensure_ascii=False)
                                + "\n"
                                + json.dumps({"type": "PLANNER_RESPONSE", "content": "第二轮干净回复"}, ensure_ascii=False)
                                + "\n"
                            )
                    # Simulate stale HQ_SESSION_KEY pointing to general:1 while daemon dispatches general:2
                    with patch.dict(os.environ, {"HQ_SESSION_KEY": "general:1"}):
                        res = handle_hook_event(
                            "agy",
                            {
                                "hookEventName": "Stop",
                                "conversationId": "conv-stale-1",
                                "transcriptPath": str(self.transcript),
                            },
                            hq_dir=self.hq_root,
                            channel=self.channel_obj,
                            now=1000 + self.turn_idx,
                        )
                        self.hook_results.append(res)
                    return super().prompt_agent(name, text, wait=wait, timeout_ms=timeout_ms)

            term = CrossKeyHookTerminal(hq_dir, tfile, ch)
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                # Seed an old sealed general:1 (stale env) and an active general:2 on the same hq-general pane
                daemon.db.create_session(
                    "general:1", "GENERAL", "agy", status="sealed", pane_name=GENERAL_PANE_NAME, now=900
                )
                daemon.db.create_session(
                    "general:2", "GENERAL", "agy", status="active", pane_name=GENERAL_PANE_NAME, now=950
                )

                # Turn 1 (turn_count == 0): sends full build_prompt; Stop hook writes under general:1 + pane hq-general
                ch.updates.append(
                    InboundMessage(
                        update_id=1,
                        chat_id="c1",
                        thread_id=None,
                        sender_id="u1",
                        text="第一轮消息",
                        message_id="m-1",
                    )
                )
                summary1 = daemon.run_once(now=1000)
                self.assertEqual(summary1.events_processed, 1)
                self.assertFalse(term.hook_results[0]["reply_pushed"])
                self.assertEqual(ch.sent, [])
                self.assertEqual(ch.finished_indicators[0]["text"], "第一轮干净回复")
                self.assertIn("## 1. Repo Boundaries", term.prompt_calls[0]["text"])
                self.assertIn("## 5. Event Payload", term.prompt_calls[0]["text"])
                self.assertIsNone(daemon.db.get_meta(f"daemon_waiting_pane:{GENERAL_PANE_NAME}"))
                self.assertIsNone(daemon.db.get_meta(f"last_reply_pane:{GENERAL_PANE_NAME}"))

                # Turn 2 (pane live and turn_count > 0): sends raw user text without re-injecting AGENTS.md
                ch.updates.append(
                    InboundMessage(
                        update_id=2,
                        chat_id="c1",
                        thread_id=None,
                        sender_id="u1",
                        text="第二轮消息",
                        message_id="m-2",
                    )
                )
                summary2 = daemon.run_once(now=1010)
                self.assertEqual(summary2.events_processed, 1)
                self.assertFalse(term.hook_results[1]["reply_pushed"])
                self.assertEqual(ch.sent, [])
                self.assertEqual(term.prompt_calls[1]["text"], "第二轮消息")
                self.assertEqual(ch.finished_indicators[1]["text"], "第二轮干净回复")
            finally:
                daemon.close()

    def test_clean_agent_terminal_output_strips_tui_footer(self) -> None:
        raw = (
            "已完成检查：\n"
            "> 引用正文内容应保留\n"
            "全部测试通过。\n"
            "\n"
            "────────────────────────────────────────────────────────\n"
            "> \n"
            "────────────────────────────────────────────────────────\n"
            "? for shortcuts · /help              auto · Gemini 4 Neon · high\n"
        )
        cleaned = clean_agent_terminal_output(raw)
        self.assertEqual(
            cleaned,
            "已完成检查：\n> 引用正文内容应保留\n全部测试通过。",
        )

        raw_plan = (
            "Plan summary ready.\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "plan · Model X · medium\n"
        )
        self.assertEqual(clean_agent_terminal_output(raw_plan), "Plan summary ready.")


if __name__ == "__main__":
    unittest.main()
