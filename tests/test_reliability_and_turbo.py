"""Reliability fixes and agy Turbo permission_mode unit tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hq_pilot.agents.agy import AgyAdapter
from hq_pilot.archiver import archive_eligible_sessions
from hq_pilot.channels.base import InboundMessage
from hq_pilot.cli import main as hq_main
from hq_pilot.config import load_config
from hq_pilot.daemon import GENERAL_PANE_NAME, HQDaemon
from hq_pilot.hook_cli import handle_hook_event
from hq_pilot.terminals.base import AgentBlockedError, PaneInfo
from hq_pilot.terminals.herdr import HerdrDriver

try:
    from tests.test_adapters_and_channels import FakeChannel
    from tests.test_herdr_interactive_mode import IndicatorChannel, NoHeadlessAgyAdapter
    from tests.test_review_fixes import RecordingTerminal
except ModuleNotFoundError:
    from test_adapters_and_channels import FakeChannel
    from test_herdr_interactive_mode import IndicatorChannel, NoHeadlessAgyAdapter
    from test_review_fixes import RecordingTerminal


class FakeHerdrTerminal(RecordingTerminal):
    """RecordingTerminal impersonating real `herdr` driver name for strict fallback tests."""

    name = "herdr"

    def __init__(self, reply_fn=None, raise_blocked: bool = False):
        super().__init__(reply_fn=reply_fn)
        self.raise_blocked = raise_blocked
        self.killed_panes = []

    def prompt_agent(self, name, text, wait=True, timeout_ms=300000):
        self.prompt_calls.append({"name": name, "text": text, "wait": wait, "timeout_ms": timeout_ms})
        if self.raise_blocked:
            raise AgentBlockedError(pane_name=name, detail="awaiting tool approval")
        if self.reply_fn:
            return self.reply_fn(name, text)
        return ""

    def kill_pane(self, name: str) -> None:
        self.killed_panes.append(name)
        super().kill_pane(name)


class TestReliabilityAndTurbo(unittest.TestCase):
    def test_herdr_driver_prompt_agent_raises_blocked_without_reading_pane(self) -> None:
        drv = HerdrDriver(bin_path="herdr", session="s-test")

        # Case 1: error.code == "agent_blocked"
        calls_err = []

        def fake_run_err(self_drv, args, check=True):
            calls_err.append(list(args))
            return subprocess.CompletedProcess(
                args,
                1,
                stdout="",
                stderr=json.dumps({"error": {"code": "agent_blocked", "message": "waiting for approval"}}),
            )

        with patch.object(HerdrDriver, "_run", fake_run_err):
            with self.assertRaises(AgentBlockedError) as ctx:
                drv.prompt_agent("hq-general", "run cmd", wait=True)
        self.assertEqual(ctx.exception.pane_name, "hq-general")
        self.assertIn("approval", ctx.exception.detail)
        self.assertEqual(len(calls_err), 1)

        # Case 2: rc=0 but .result.agent.agent_status == "blocked"
        calls_status = []

        def fake_run_status(self_drv, args, check=True):
            calls_status.append(list(args))
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({"result": {"agent": {"name": "hq-general", "agent_status": "blocked"}}}),
                stderr="",
            )

        with patch.object(HerdrDriver, "_run", fake_run_status):
            with self.assertRaises(AgentBlockedError) as ctx2:
                drv.prompt_agent("hq-general", "run cmd", wait=True)
        self.assertEqual(ctx2.exception.pane_name, "hq-general")
        self.assertEqual(len(calls_status), 1)

    def test_daemon_handles_agent_blocked_gracefully_without_crashing_or_screen_dump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            ch = IndicatorChannel()
            term = FakeHerdrTerminal(raise_blocked=True)
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
                        text="deploy check",
                        message_id="m1",
                    )
                )
                summary = daemon.run_once(now=1000)
                self.assertEqual(summary.events_processed, 1)
                self.assertEqual(summary.events_failed, 0)
                self.assertEqual(len(ch.finished_indicators), 1)
                msg = ch.finished_indicators[0]["text"]
                self.assertIn("blocked", msg)
                self.assertIn("agent.permission_mode: turbo", msg)
                self.assertIsNone(daemon.db.get_meta("daemon_waiting:general:1"))
            finally:
                daemon.close()

    def test_herdr_mode_reads_transcript_incrementally_and_never_leaks_terminal_screen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            tfile = Path(tmp) / "transcript_full.jsonl"
            tfile.write_text(
                json.dumps({"type": "USER_INPUT", "content": "old q"}, ensure_ascii=False) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "content": "old reply"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            ch = IndicatorChannel()

            def append_new_turn(name, text):
                with tfile.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"type": "USER_INPUT", "content": text}, ensure_ascii=False) + "\n")
                    fh.write(
                        json.dumps({"type": "PLANNER_RESPONSE", "content": "全新结构化回复"}, ensure_ascii=False)
                        + "\n"
                    )
                return "RAW NOISY TERMINAL SCREEN MUST BE IGNORED"

            term = FakeHerdrTerminal(reply_fn=append_new_turn)
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                daemon.db.create_session(
                    "general:1",
                    "GENERAL",
                    "agy",
                    status="active",
                    pane_name=GENERAL_PANE_NAME,
                    transcript_path=str(tfile),
                    now=900,
                )
                # Turn 1: Stop hook did not write last_reply -> reads new PLANNER_RESPONSE from transcript slice
                ch.updates.append(
                    InboundMessage(
                        update_id=1,
                        chat_id="c1",
                        thread_id=None,
                        sender_id="u1",
                        text="new question",
                        message_id="m1",
                    )
                )
                daemon.run_once(now=1000)
                self.assertEqual(ch.finished_indicators[-1]["text"], "全新结构化回复")

                # Turn 2: No new assistant line in transcript -> NEVER leaks raw terminal screen
                term.reply_fn = lambda name, text: "RAW TERMINAL SCROLLBACK LEAK"
                ch.updates.append(
                    InboundMessage(
                        update_id=2,
                        chat_id="c1",
                        thread_id=None,
                        sender_id="u1",
                        text="silent background task",
                        message_id="m2",
                    )
                )
                daemon.run_once(now=1010)
                reply2 = ch.finished_indicators[-1]["text"]
                self.assertNotIn("RAW TERMINAL SCROLLBACK LEAK", reply2)
                self.assertIn("尚未产出最终文本回复", reply2)
            finally:
                daemon.close()

    def test_hook_cli_ignores_subagent_session_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            main_tfile = Path(tmp) / "main.jsonl"
            main_tfile.write_text(
                json.dumps({"role": "user", "content": "main q"}) + "\n"
                + json.dumps({"role": "assistant", "content": "main a"}) + "\n",
                encoding="utf-8",
            )
            sub_tfile = Path(tmp) / "subagent.jsonl"
            sub_tfile.write_text(
                json.dumps({"role": "user", "content": "sub task"}) + "\n"
                + json.dumps({"role": "assistant", "content": "INTERNAL SUBAGENT REPORT"}) + "\n",
                encoding="utf-8",
            )

            ch = FakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=RecordingTerminal(),
                channel=ch,
            )
            try:
                daemon.db.create_session(
                    "general:1",
                    "GENERAL",
                    "agy",
                    status="active",
                    agent_session_id="conv-main-001",
                    pane_name=GENERAL_PANE_NAME,
                    transcript_path=str(main_tfile),
                    now=1000,
                )
                daemon.db.update_session("general:1", reply_line_offset=2, turn_count=1)
            finally:
                daemon.close()

            # Subagent inherits HQ_SESSION_KEY=general:1 but has a different conversationId
            with patch.dict(os.environ, {"HQ_SESSION_KEY": "general:1"}):
                res_stop = handle_hook_event(
                    "agy",
                    {
                        "hookEventName": "Stop",
                        "conversationId": "conv-subagent-999",
                        "transcriptPath": str(sub_tfile),
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=1020,
                )
                res_pre = handle_hook_event(
                    "agy",
                    {
                        "hookEventName": "PreInvocation",
                        "conversationId": "conv-subagent-999",
                        "lastUserInput": "subagent internal prompt",
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=1021,
                )

            self.assertEqual(
                res_stop,
                {"status": "ignored", "reason": "subagent_session_mismatch", "event": "Stop"},
            )
            self.assertEqual(
                res_pre,
                {"status": "ignored", "reason": "subagent_session_mismatch", "event": "PreInvocation"},
            )
            self.assertEqual(ch.sent, [])

            daemon2 = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=RecordingTerminal(),
                channel=ch,
            )
            try:
                sess = daemon2.db.get_session("general:1")
                self.assertEqual(sess["agent_session_id"], "conv-main-001")
                self.assertEqual(sess["reply_line_offset"], 2)
                self.assertEqual(sess["turn_count"], 1)
                self.assertEqual(sess["status"], "active")
                self.assertIsNone(daemon2.db.get_meta("last_reply:general:1"))
                self.assertIsNone(daemon2.db.get_meta(f"last_reply_pane:{GENERAL_PANE_NAME}"))
            finally:
                daemon2.close()

    def test_long_prompt_and_distill_materialize_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            term = FakeHerdrTerminal()
            ch = IndicatorChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                sess = daemon._ensure_general_session(1000)
                # Short non-distill prompt stays inline
                short_prompt = "短消息测试"
                self.assertEqual(
                    daemon._materialize_long_prompt(short_prompt, tag="channel", now=999),
                    short_prompt,
                )

                # >2KB prompt materializes to .hq/prompts/*.md
                huge_prompt = "请分析以下详细日志：\n" + ("A" * 3000)
                daemon._dispatch_interactive(
                    sess,
                    huge_prompt,
                    effort="high",
                    now=1000,
                    cwd=hq_dir,
                    wait=True,
                    prompt_tag="channel",
                )
                delivered = term.prompt_calls[-1]["text"]
                self.assertLess(len(delivered.encode("utf-8")), 512)
                self.assertIn("请读取并执行指令文件", delivered)
                expected_file = hq_dir / ".hq" / "prompts" / "channel-1000.md"
                self.assertTrue(expected_file.exists())
                self.assertEqual(expected_file.read_text(encoding="utf-8"), huge_prompt)

                # Short distill prompt still materializes because tag starts with "distill"
                distill_prompt = "Distill this short turn."
                daemon._dispatch_interactive(
                    sess,
                    distill_prompt,
                    effort="low",
                    now=1005,
                    cwd=hq_dir,
                    wait=True,
                    prompt_tag="distill",
                )
                delivered_distill = term.prompt_calls[-1]["text"]
                self.assertIn("请读取并执行指令文件", delivered_distill)
                distill_file = hq_dir / ".hq" / "prompts" / "distill-1005.md"
                self.assertTrue(distill_file.exists())
                self.assertEqual(distill_file.read_text(encoding="utf-8"), distill_prompt)
            finally:
                daemon.close()

    def test_archiver_does_not_kill_pane_shared_with_active_or_pinned_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            term = FakeHerdrTerminal()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=FakeChannel(),
            )
            try:
                # general:1 is pinned on hq-general; general:2 is sealed on the SAME hq-general pane
                daemon.db.create_session(
                    "general:1",
                    "GENERAL",
                    "agy",
                    status="pinned",
                    pinned=True,
                    pinned_reason="manual",
                    pane_name=GENERAL_PANE_NAME,
                    now=900,
                )
                daemon.db.create_session(
                    "general:2",
                    "GENERAL",
                    "agy",
                    status="sealed",
                    pane_name=GENERAL_PANE_NAME,
                    now=950,
                )
                # Dedicated topic session sealed on its own pane -> SHOULD be killed
                daemon.db.create_session(
                    "task:repo:old-topic:1",
                    "TOPIC",
                    "agy",
                    status="sealed",
                    pane_name="hq-old-topic",
                    now=960,
                )

                report = archive_eligible_sessions(
                    daemon.config,
                    daemon.db,
                    terminal_driver=term,
                    channel=None,
                    now=1000,
                )
                self.assertIn("general:2", report.archived)
                self.assertIn("task:repo:old-topic:1", report.archived)
                self.assertNotIn(GENERAL_PANE_NAME, term.killed_panes)
                self.assertIn("hq-old-topic", term.killed_panes)
            finally:
                daemon.close()

    def test_agy_permission_mode_turbo_and_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            adapter = AgyAdapter()

            args_turbo = adapter.extra_trust_args([hq_dir], permission_mode="turbo")
            self.assertEqual(args_turbo[0], "--settings")
            parsed_turbo = json.loads(args_turbo[1])
            self.assertEqual(parsed_turbo["trustedWorkspaces"], [str(hq_dir.resolve())])
            self.assertEqual(parsed_turbo["toolPermission"], "always-proceed")

            args_always = adapter.extra_trust_args([hq_dir], permission_mode="always-proceed")
            self.assertEqual(json.loads(args_always[1])["toolPermission"], "always-proceed")

            args_edits = adapter.extra_trust_args([hq_dir], permission_mode="accept-edits")
            self.assertEqual(json.loads(args_edits[1])["agentMode"], "accept-edits")

            args_skip = adapter.extra_trust_args([hq_dir], permission_mode="skip-all")
            self.assertIn("--dangerously-skip-permissions", args_skip)
            self.assertNotIn("toolPermission", json.loads(args_skip[1]))

            args_default = adapter.extra_trust_args([hq_dir], permission_mode="default")
            self.assertEqual(json.loads(args_default[1]), {"trustedWorkspaces": [str(hq_dir.resolve())]})

            # Verify config loading & daemon forwarding into start_agent
            hq_yaml = hq_dir / "hq.yaml"
            hq_yaml.write_text(
                hq_yaml.read_text(encoding="utf-8").replace(
                    'permission_mode: "default"', 'permission_mode: "turbo"'
                ),
                encoding="utf-8",
            )
            cfg = load_config(hq_dir)
            self.assertEqual(cfg.agent.permission_mode, "turbo")

            term = RecordingTerminal()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=FakeChannel(),
            )
            try:
                daemon._ensure_general_pane(now=1000)
                agent_args = term.start_agent_calls[0]["agent_args"]
                self.assertIn("--settings", agent_args)
                settings_idx = agent_args.index("--settings")
                settings_obj = json.loads(agent_args[settings_idx + 1])
                self.assertEqual(settings_obj["toolPermission"], "always-proceed")
            finally:
                daemon.close()

    def test_herdr_local_direct_chat_in_general_does_not_push_to_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            tfile = Path(tmp) / "general_local.jsonl"
            tfile.write_text(
                json.dumps(
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>\n帮我本地看下当前目录结构\n</USER_REQUEST>",
                    },
                    ensure_ascii=False,
                )
                + "\n"
                + json.dumps(
                    {"type": "PLANNER_RESPONSE", "content": "这是本地终端直接对话的回复"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            ch = FakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=RecordingTerminal(),
                channel=ch,
            )
            try:
                daemon.db.create_session(
                    "general:1",
                    "GENERAL",
                    "agy",
                    status="active",
                    agent_session_id="conv-gen-local",
                    pane_name=GENERAL_PANE_NAME,
                    transcript_path=str(tfile),
                    now=1000,
                )
                # Even if an earlier turn came from daemon, this new USER_INPUT is NOT in daemon_dispatches
                daemon.db.set_meta("last_input_origin:general:1", "daemon", now=1000)
            finally:
                daemon.close()

            with patch.dict(os.environ, {"HQ_SESSION_KEY": "general:1"}):
                res = handle_hook_event(
                    "agy",
                    {
                        "hookEventName": "Stop",
                        "conversationId": "conv-gen-local",
                        "transcriptPath": str(tfile),
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=1020,
                )

            self.assertEqual(res["status"], "ok")
            self.assertFalse(res["reply_pushed"])
            self.assertEqual(ch.sent, [])

            daemon2 = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=RecordingTerminal(),
                channel=ch,
            )
            try:
                sess = daemon2.db.get_session("general:1")
                self.assertEqual(sess["reply_line_offset"], 2)
                self.assertEqual(sess["turn_count"], 1)
                self.assertEqual(
                    daemon2.db.get_meta("last_reply:general:1"),
                    "这是本地终端直接对话的回复",
                )
                self.assertEqual(
                    daemon2.db.get_meta("last_input_origin:general:1"),
                    "local_tty",
                )
            finally:
                daemon2.close()

    def test_preinvocation_manual_input_suppresses_subsequent_stop_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            tfile = Path(tmp) / "topic_local.jsonl"
            tfile.write_text(
                json.dumps(
                    {
                        "type": "USER_INPUT",
                        "content": "<USER_REQUEST>直接在终端追问一个细节</USER_REQUEST>",
                    },
                    ensure_ascii=False,
                )
                + "\n"
                + json.dumps(
                    {"type": "PLANNER_RESPONSE", "content": "终端追问的本地回复"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            ch = FakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=RecordingTerminal(),
                channel=ch,
            )
            try:
                daemon.db.create_session(
                    "task:repo:demo:1000",
                    "TOPIC",
                    "agy",
                    status="active",
                    channel_thread_id="om_thread_demo",
                    agent_session_id="conv-topic-1",
                    pane_name="hq-demo",
                    transcript_path=str(tfile),
                    now=1000,
                )
                daemon.db.set_meta("last_input_origin:task:repo:demo:1000", "daemon", now=1000)
            finally:
                daemon.close()

            with patch.dict(os.environ, {"HQ_SESSION_KEY": "task:repo:demo:1000"}):
                pre_res = handle_hook_event(
                    "agy",
                    {
                        "hookEventName": "PreInvocation",
                        "conversationId": "conv-topic-1",
                        "lastUserInput": "<USER_REQUEST>直接在终端追问一个细节</USER_REQUEST>",
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=1010,
                )
                stop_res = handle_hook_event(
                    "agy",
                    {
                        "hookEventName": "Stop",
                        "conversationId": "conv-topic-1",
                        "transcriptPath": str(tfile),
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=1020,
                )

            self.assertEqual(pre_res["pinned_reason"], "manual_input")
            self.assertEqual(stop_res["status"], "ok")
            self.assertFalse(stop_res["reply_pushed"])
            self.assertEqual(ch.sent, [])

            daemon2 = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=RecordingTerminal(),
                channel=ch,
            )
            try:
                sess = daemon2.db.get_session("task:repo:demo:1000")
                self.assertEqual(sess["reply_line_offset"], 2)
                self.assertEqual(sess["turn_count"], 1)
                self.assertEqual(
                    daemon2.db.get_meta("last_input_origin:task:repo:demo:1000"),
                    "local_tty",
                )
                self.assertEqual(
                    daemon2.db.get_meta("last_reply:task:repo:demo:1000"),
                    "终端追问的本地回复",
                )
            finally:
                daemon2.close()


if __name__ == "__main__":
    unittest.main()
