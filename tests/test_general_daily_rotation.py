"""Tests for #General non-pinning manual touch, in-place /new daily rotation, and hook rebinding."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hq_pilot.cli import main as hq_main
from hq_pilot.config import update_tuning_value
from hq_pilot.daemon import GENERAL_PANE_NAME, HQDaemon
from hq_pilot.db import HQDatabase
from hq_pilot.gatekeeper import detect_manual_touch
from hq_pilot.hook_cli import handle_hook_event
from hq_pilot.terminals.base import PaneInfo

try:
    from tests.test_adapters_and_channels import FakeChannel
    from tests.test_herdr_interactive_mode import NoHeadlessAgyAdapter
    from tests.test_review_fixes import RecordingTerminal
except ModuleNotFoundError:
    from test_adapters_and_channels import FakeChannel
    from test_herdr_interactive_mode import NoHeadlessAgyAdapter
    from test_review_fixes import RecordingTerminal


class StatusAwareTerminal(RecordingTerminal):
    """RecordingTerminal with controllable per-pane status and kill tracking."""

    def __init__(self, reply_fn=None):
        super().__init__(reply_fn=reply_fn)
        self.killed_panes = []

    def set_pane(self, name: str, status: str = "idle", attached: bool = False) -> None:
        self.panes[name] = PaneInfo(name=name, status=status, attached=attached, cwd="")

    def kill_pane(self, name: str) -> None:
        self.killed_panes.append(name)
        super().kill_pane(name)


class TestGeneralDailyRotation(unittest.TestCase):
    def test_general_manual_input_suppresses_push_without_pinning_while_topic_pins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            gen_tfile = Path(tmp) / "gen_local.jsonl"
            gen_tfile.write_text(
                json.dumps(
                    {"type": "USER_INPUT", "content": "<USER_REQUEST>本地直聊一下</USER_REQUEST>"},
                    ensure_ascii=False,
                )
                + "\n"
                + json.dumps(
                    {"type": "PLANNER_RESPONSE", "content": "本地回复，不推飞书"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                db.create_session(
                    "general:1",
                    "GENERAL",
                    "agy",
                    status="active",
                    pane_name=GENERAL_PANE_NAME,
                    agent_session_id="conv-gen-1",
                    transcript_path=str(gen_tfile),
                    now=100,
                )
                db.create_session(
                    "task:repo:demo:100",
                    "TOPIC",
                    "agy",
                    status="active",
                    channel_thread_id="thread-demo",
                    pane_name="hq-demo",
                    agent_session_id="conv-topic-1",
                    now=100,
                )
            finally:
                db.close()

            ch = FakeChannel()
            # 1. #General manual input via PreInvocation -> returns manual_input, sets local_tty,
            #    suppresses Stop push, and NEVER promotes #General to pinned.
            with patch.dict(os.environ, {"HQ_SESSION_KEY": "general:1"}):
                pre_gen = handle_hook_event(
                    "agy",
                    {
                        "hookEventName": "PreInvocation",
                        "conversationId": "conv-gen-1",
                        "lastUserInput": "<USER_REQUEST>本地直聊一下</USER_REQUEST>",
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=110,
                )
                stop_gen = handle_hook_event(
                    "agy",
                    {
                        "hookEventName": "Stop",
                        "conversationId": "conv-gen-1",
                        "transcriptPath": str(gen_tfile),
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=115,
                )

            self.assertEqual(pre_gen["pinned_reason"], "manual_input")
            self.assertFalse(stop_gen["reply_pushed"])
            self.assertEqual(ch.sent, [])

            # 2. #Topic manual input -> STILL promotes #Topic to pinned.
            with patch.dict(os.environ, {"HQ_SESSION_KEY": "task:repo:demo:100"}):
                pre_topic = handle_hook_event(
                    "agy",
                    {
                        "hookEventName": "PreInvocation",
                        "conversationId": "conv-topic-1",
                        "lastUserInput": "Direct human input in topic pane",
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=120,
                )
            self.assertEqual(pre_topic["pinned_reason"], "manual_input")

            # 3. Terminal attach on #General also does NOT pin #General, while #Topic pins.
            term = StatusAwareTerminal()
            term.set_pane(GENERAL_PANE_NAME, status="idle", attached=True)
            term.set_pane("hq-topic-attach", status="idle", attached=True)

            db2 = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                gen_after = db2.get_session("general:1")
                self.assertEqual(gen_after["status"], "active")
                self.assertEqual(gen_after["pinned"], 0)
                self.assertEqual(db2.get_meta("last_input_origin:general:1"), "local_tty")

                topic_after = db2.get_session("task:repo:demo:100")
                self.assertEqual(topic_after["status"], "pinned")
                self.assertEqual(topic_after["pinned"], 1)
                self.assertEqual(topic_after["pinned_reason"], "manual_input")

                attach_gen = detect_manual_touch(db2, gen_after, terminal_driver=term, now=130)
                self.assertEqual(attach_gen, "terminal_attach")
                self.assertEqual(db2.get_session("general:1")["status"], "active")
                self.assertEqual(db2.get_session("general:1")["pinned"], 0)

                s_topic_2 = db2.create_session(
                    "task:repo:attach:100",
                    "TOPIC",
                    "agy",
                    status="active",
                    pane_name="hq-topic-attach",
                    now=100,
                )
                attach_topic = detect_manual_touch(db2, s_topic_2, terminal_driver=term, now=135)
                self.assertEqual(attach_topic, "terminal_attach")
                self.assertEqual(db2.get_session("task:repo:attach:100")["status"], "pinned")
            finally:
                db2.close()

    def test_rotate_general_session_sends_slash_new_in_place_and_seals_legacy_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            term = StatusAwareTerminal()
            term.set_pane(GENERAL_PANE_NAME, status="idle")
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=FakeChannel(),
            )
            try:
                # No active general session -> returns False
                self.assertFalse(daemon.rotate_general_session(now=900))

                # Seed a legacy pinned general:1 with cached meta
                daemon.db.create_session(
                    "general:1",
                    "GENERAL",
                    "agy",
                    status="pinned",
                    pinned=True,
                    pinned_reason="manual_input",
                    pane_name=GENERAL_PANE_NAME,
                    agent_session_id="conv-old-1",
                    transcript_path="/tmp/old.jsonl",
                    now=1000,
                )
                daemon.db.update_session("general:1", turn_count=12)
                daemon.db.set_meta("last_reply:general:1", "old reply", now=1000)
                daemon.db.set_meta(f"last_reply_pane:{GENERAL_PANE_NAME}", "old reply", now=1000)
                daemon.db.set_meta("last_input_origin:general:1", "local_tty", now=1000)

                rotated = daemon.rotate_general_session(now=2000)
                self.assertTrue(rotated)

                # Sent `/new` via send_text (never prompt_agent --wait, never kill_pane)
                self.assertEqual(
                    term.sent_texts,
                    [{"name": GENERAL_PANE_NAME, "text": "/new", "enter": True}],
                )
                self.assertEqual(term.prompt_calls, [])
                self.assertEqual(term.killed_panes, [])

                # Old general:1 unpinned and sealed; caches cleared
                s1 = daemon.db.get_session("general:1")
                self.assertEqual(s1["status"], "sealed")
                self.assertEqual(s1["pinned"], 0)
                self.assertEqual(s1["sealed_at"], 2000)
                self.assertIsNone(daemon.db.get_meta("last_reply:general:1"))
                self.assertIsNone(daemon.db.get_meta(f"last_reply_pane:{GENERAL_PANE_NAME}"))
                self.assertIsNone(daemon.db.get_meta("last_input_origin:general:1"))

                # New general:2 created immediately with fresh state
                s2 = daemon.db.get_session("general:2")
                self.assertIsNotNone(s2)
                self.assertEqual(s2["track"], "GENERAL")
                self.assertEqual(s2["status"], "active")
                self.assertEqual(s2["pane_name"], GENERAL_PANE_NAME)
                self.assertIsNone(s2["agent_session_id"])
                self.assertIsNone(s2["transcript_path"])
                self.assertEqual(s2["turn_count"], 0)
            finally:
                daemon.close()

    def test_rotate_general_skips_when_working_or_blocked_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            term = StatusAwareTerminal()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=FakeChannel(),
            )
            try:
                daemon._ensure_general_session(1000)

                for busy_status in ("working", "blocked"):
                    term.set_pane(GENERAL_PANE_NAME, status=busy_status)
                    self.assertFalse(daemon.rotate_general_session(now=1500, force=False))
                    self.assertEqual(term.sent_texts, [])
                    self.assertEqual(daemon.db.get_session("general:1")["status"], "active")
                    self.assertIsNone(daemon.db.get_session("general:2"))

                # Even when working, force=True performs the rotation
                term.set_pane(GENERAL_PANE_NAME, status="working")
                self.assertTrue(daemon.rotate_general_session(now=1600, force=True))
                self.assertEqual(
                    term.sent_texts,
                    [{"name": GENERAL_PANE_NAME, "text": "/new", "enter": True}],
                )
                self.assertEqual(daemon.db.get_session("general:1")["status"], "sealed")
                self.assertEqual(daemon.db.get_session("general:2")["status"], "active")
            finally:
                daemon.close()

    def test_hook_rebinds_new_conversation_after_slash_new_and_still_filters_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            old_tfile = Path(tmp) / "old.jsonl"
            old_tfile.write_text(
                json.dumps({"type": "USER_INPUT", "content": "yesterday"}) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "content": "yesterday reply"}) + "\n",
                encoding="utf-8",
            )
            new_tfile = Path(tmp) / "new.jsonl"
            new_tfile.write_text(
                json.dumps({"type": "USER_INPUT", "content": "today prompt"}) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "content": "today reply"}) + "\n",
                encoding="utf-8",
            )
            sub_tfile = Path(tmp) / "sub.jsonl"
            sub_tfile.write_text(
                json.dumps({"type": "USER_INPUT", "content": "sub work"}) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "content": "SUBAGENT SECRET"}) + "\n",
                encoding="utf-8",
            )

            term = StatusAwareTerminal()
            term.set_pane(GENERAL_PANE_NAME, status="idle")
            ch = FakeChannel()
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
                    agent_session_id="conv-old-1",
                    transcript_path=str(old_tfile),
                    now=1000,
                )
                self.assertTrue(daemon.rotate_general_session(now=2000))
            finally:
                daemon.close()

            # Stale HQ_SESSION_KEY=general:1 remains in the long-lived agy process env!
            with patch.dict(os.environ, {"HQ_SESSION_KEY": "general:1"}):
                # Edge case: trailing event from conv-old-1 must NOT crash UNIQUE constraint or pollute general:2
                res_old = handle_hook_event(
                    "agy",
                    {
                        "hookEventName": "Stop",
                        "conversationId": "conv-old-1",
                        "transcriptPath": str(old_tfile),
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=2001,
                )
                self.assertEqual(res_old["session_key"], "general:1")
                self.assertFalse(res_old["reply_pushed"])

                # Brand-new conversationId after `/new` fires SessionStart -> rebinds to general:2
                res_start = handle_hook_event(
                    "agy",
                    {
                        "hookEventName": "SessionStart",
                        "conversationId": "conv-new-2",
                        "transcriptPath": str(new_tfile),
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=2005,
                )
                self.assertEqual(res_start["status"], "ok")
                self.assertEqual(res_start["session_key"], "general:2")

                # Stop for conv-new-2 records turn and reply on general:2
                res_stop = handle_hook_event(
                    "agy",
                    {
                        "hookEventName": "Stop",
                        "conversationId": "conv-new-2",
                        "transcriptPath": str(new_tfile),
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=2010,
                )
                self.assertEqual(res_stop["status"], "ok")
                self.assertEqual(res_stop["session_key"], "general:2")

                # Subagent spawned by general:2 inherits HQ_SESSION_KEY=general:1 with different conversationId -> filtered!
                res_sub_stop = handle_hook_event(
                    "agy",
                    {
                        "hookEventName": "Stop",
                        "conversationId": "conv-subagent-888",
                        "transcriptPath": str(sub_tfile),
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=2020,
                )
                res_sub_pre = handle_hook_event(
                    "agy",
                    {
                        "hookEventName": "PreInvocation",
                        "conversationId": "conv-subagent-888",
                        "lastUserInput": "subagent internal task",
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=2021,
                )

            self.assertEqual(
                res_sub_stop,
                {"status": "ignored", "reason": "subagent_session_mismatch", "event": "Stop"},
            )
            self.assertEqual(
                res_sub_pre,
                {"status": "ignored", "reason": "subagent_session_mismatch", "event": "PreInvocation"},
            )
            self.assertEqual(ch.sent, [])

            db_check = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                s1 = db_check.get_session("general:1")
                s2 = db_check.get_session("general:2")
                self.assertEqual(s1["agent_session_id"], "conv-old-1")
                self.assertEqual(s2["agent_session_id"], "conv-new-2")
                self.assertEqual(s2["transcript_path"], str(new_tfile))
                self.assertEqual(s2["turn_count"], 1)
                self.assertEqual(db_check.get_meta("last_reply:general:2"), "today reply")
            finally:
                db_check.close()

    def test_nightly_pipeline_rotates_general_when_enabled_and_respects_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            term = StatusAwareTerminal(reply_fn=lambda name, text: "dream done")
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=FakeChannel(),
            )
            try:
                daemon._ensure_general_session(1000)
                term.set_pane(GENERAL_PANE_NAME, status="idle")

                # Default general_nightly_reset=True -> nightly pipeline sends /new & rotates general:1 -> general:2
                self.assertTrue(daemon.config.tuning.general_nightly_reset)
                daemon.run_nightly_pipeline(now=2000)

                slash_new_calls = [
                    c for c in term.sent_texts if c["name"] == GENERAL_PANE_NAME and c["text"] == "/new"
                ]
                self.assertEqual(len(slash_new_calls), 1)
                self.assertEqual(term.killed_panes, [])
                self.assertEqual(daemon.db.get_session("general:1")["status"], "sealed")
                self.assertEqual(daemon.db.get_session("general:2")["status"], "active")

                # Disable general_nightly_reset=False -> nightly pipeline does NOT rotate general:2
                update_tuning_value(hq_dir, "general_nightly_reset", "false")
                daemon.config.reload_if_changed()
                self.assertFalse(daemon.config.tuning.general_nightly_reset)

                daemon.run_nightly_pipeline(now=3000)
                slash_new_calls_after = [
                    c for c in term.sent_texts if c["name"] == GENERAL_PANE_NAME and c["text"] == "/new"
                ]
                self.assertEqual(len(slash_new_calls_after), 1)
                self.assertEqual(daemon.db.get_session("general:2")["status"], "active")
                self.assertIsNone(daemon.db.get_session("general:3"))
            finally:
                daemon.close()

    def test_seal_idle_general_sessions_rotates_in_place_when_idle_and_skips_when_working(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            term = StatusAwareTerminal()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=FakeChannel(),
            )
            try:
                daemon._ensure_general_session(1000)
                # Exceed 24h idle limit while pane is working -> skipped, not sealed!
                term.set_pane(GENERAL_PANE_NAME, status="working")
                daemon._seal_idle_general_sessions(now=1000 + 1440 * 60 + 10)
                self.assertEqual(daemon.db.get_session("general:1")["status"], "active")
                self.assertEqual(term.sent_texts, [])

                # Pane becomes idle -> rotates in-place with /new without killing pane!
                term.set_pane(GENERAL_PANE_NAME, status="idle")
                daemon._seal_idle_general_sessions(now=1000 + 1440 * 60 + 20)
                self.assertEqual(
                    term.sent_texts,
                    [{"name": GENERAL_PANE_NAME, "text": "/new", "enter": True}],
                )
                self.assertEqual(term.killed_panes, [])
                self.assertEqual(daemon.db.get_session("general:1")["status"], "sealed")
                self.assertEqual(daemon.db.get_session("general:2")["status"], "active")
            finally:
                daemon.close()


if __name__ == "__main__":
    unittest.main()
