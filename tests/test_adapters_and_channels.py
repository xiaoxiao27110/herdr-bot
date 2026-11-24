"""Tests for AgyAdapter, FakeAgentAdapter decoupling, and TelegramChannel mock."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from hq_pilot.agents.agy import AgyAdapter
from hq_pilot.agents.base import CodingAgentAdapter, HeadlessResult, Turn
from hq_pilot.channels.base import InboundMessage, InlineButton, MessagingChannel
from hq_pilot.channels.telegram import TelegramChannel
from hq_pilot.cli import main as hq_main
from hq_pilot.daemon import HQDaemon
from hq_pilot.terminals.base import PaneInfo, TerminalDriver


class FakeAgentAdapter(CodingAgentAdapter):
    name = "fake-agent"
    index_filename = "FAKE_INDEX.md"

    def build_headless_cmd(self, prompt, session_id=None, model=None, effort=None, add_dirs=None):
        return ["fake-agent", "--prompt", prompt]

    def build_interactive_cmd(self, initial_prompt=None, session_id=None, model=None, effort=None, add_dirs=None):
        return ["fake-agent", "--resume", session_id or "new"]

    def parse_headless_output(self, stdout):
        data = json.loads(stdout)
        return HeadlessResult(reply_markdown=data["reply"], agent_session_id=data["sid"])

    def read_transcript_slice(self, transcript_path, from_line=0):
        return [], 0

    def count_transcript_lines(self, transcript_path):
        return 0

    def ensure_hooks_installed(self, workspace_dir, hook_bin):
        p = Path(workspace_dir) / ".fake_hooks.json"
        p.write_text("{}", encoding="utf-8")
        return p


class FakeChannel(MessagingChannel):
    name = "fake-channel"

    def __init__(self, updates=None):
        self.updates = list(updates or [])
        self.sent = []
        self.closed_threads = []
        self.answered = []

    def poll_updates(self, timeout=10):
        batch = list(self.updates)
        self.updates.clear()
        return batch

    def send_message(self, text, thread_id=None, buttons=None):
        mid = f"msg-{len(self.sent) + 1}"
        self.sent.append({"id": mid, "text": text, "thread_id": thread_id, "buttons": buttons})
        return mid

    def create_thread(self, title):
        return "thread-101"

    def close_thread(self, thread_id):
        self.closed_threads.append(thread_id)
        return True

    def answer_callback(self, callback_id, text=""):
        self.answered.append((callback_id, text))
        return True


class DummyTerminal(TerminalDriver):
    name = "dummy"

    def run_bg(self, name, cwd, cmd):
        pass

    def list_panes(self):
        return []

    def send_text(self, name, text, enter=True):
        pass

    def read_pane(self, name, lines=80):
        return ""

    def attach_cmd(self, name):
        return ["dummy", name]

    def kill_pane(self, name):
        pass

    def is_client_attached(self, name):
        return False


class TestAdaptersAndChannels(unittest.TestCase):
    def test_agy_adapter_commands_transcript_and_hooks(self) -> None:
        adapter = AgyAdapter()
        self.assertEqual(adapter.index_filename, "AGENTS.md")
        cmd = adapter.build_headless_cmd(
            "hello", session_id="s-1", model="glm-test", effort="low", add_dirs=[Path("/tmp/r1")]
        )
        self.assertEqual(
            cmd,
            [
                "agy", "--output-format", "json",
                "--conversation", "s-1",
                "--model", "glm-test",
                "--effort", "low",
                "--add-dir", "/tmp/r1",
                "-p", "hello",
            ],
        )
        icmd = adapter.build_interactive_cmd(initial_prompt="start", session_id="s-2")
        self.assertEqual(icmd, ["agy", "--conversation", "s-2", "-i", "start"])

        parsed = adapter.parse_headless_output(
            'debug log line\n{"reply": "Done!", "conversationId": "conv-99"}\n'
        )
        self.assertEqual(parsed.reply_markdown, "Done!")
        self.assertEqual(parsed.agent_session_id, "conv-99")

        with tempfile.TemporaryDirectory() as tmp:
            tfile = Path(tmp) / "transcript.jsonl"
            tfile.write_text(
                "\n".join([
                    json.dumps({"role": "user", "content": "hi", "timestamp": "t1"}),
                    json.dumps({"type": "model", "message": {"parts": [{"text": "hello"}]}, "ts": "t2"}),
                    json.dumps({"role": "user", "content": "fix bug", "timestamp": "t3"}),
                ]) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(adapter.count_transcript_lines(tfile), 3)
            turns, total = adapter.read_transcript_slice(tfile, from_line=1)
            self.assertEqual(total, 3)
            self.assertEqual(len(turns), 2)
            self.assertEqual(turns[0].role, "assistant")
            self.assertEqual(turns[0].text, "hello")
            self.assertEqual(turns[1].role, "user")

            hook_path = adapter.ensure_hooks_installed(tmp, "/usr/local/bin/hq-agent-hook")
            self.assertTrue(hook_path.exists())
            hook_cfg = json.loads(hook_path.read_text(encoding="utf-8"))
            self.assertIn("SessionStart", hook_cfg["hq-pilot"])
            self.assertIn("PreInvocation", hook_cfg["hq-pilot"])
            self.assertIn("Stop", hook_cfg["hq-pilot"])

    def test_telegram_channel_mock_and_fail_closed_whitelist(self) -> None:
        calls = []

        def mock_req(url, body, timeout):
            calls.append((url, body))
            method = url.rsplit("/", 1)[-1]
            if method == "getUpdates":
                return {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 10,
                            "message": {
                                "message_id": 1,
                                "chat": {"id": 999999},  # Unauthorized chat!
                                "from": {"id": 1},
                                "text": "intruder",
                            },
                        },
                        {
                            "update_id": 11,
                            "message": {
                                "message_id": 2,
                                "message_thread_id": 77,
                                "chat": {"id": -10012345},
                                "from": {"id": 2},
                                "text": "authorized msg",
                            },
                        },
                    ],
                }
            if method == "createForumTopic":
                return {"ok": True, "result": {"message_thread_id": 88}}
            if method == "sendMessage":
                return {"ok": True, "result": {"message_id": 501}}
            return {"ok": True, "result": True}

        ch = TelegramChannel(
            bot_token="fake-token",
            allowed_chat_id="-10012345",
            request_fn=mock_req,
        )
        msgs = ch.poll_updates()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].text, "authorized msg")
        self.assertEqual(msgs[0].thread_id, "77")

        tid = ch.create_thread("Feature X")
        self.assertEqual(tid, "88")
        mid = ch.send_message(
            "Choose",
            thread_id=tid,
            buttons=[[InlineButton("OK", "ok_cb")]],
        )
        self.assertEqual(mid, "501")
        self.assertTrue(ch.close_thread("88"))

        # Fail-closed when allowed_chat_id is empty
        closed_ch = TelegramChannel(bot_token="fake-token", allowed_chat_id="", request_fn=mock_req)
        self.assertEqual(closed_ch.poll_updates(), [])
        with self.assertRaises(RuntimeError):
            closed_ch.send_message("should fail")

    def test_daemon_decoupled_with_fake_adapter_and_sealed_topic_interception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])

            ch = FakeChannel(
                updates=[
                    InboundMessage(
                        update_id=1,
                        chat_id="c1",
                        thread_id="77",
                        sender_id="u1",
                        text="build feature",
                    )
                ]
            )

            def fake_runner(cmd, cwd):
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps({"reply": "Built feature!", "sid": "fake-sid-1"}), stderr=""
                )

            daemon = HQDaemon(
                hq_dir,
                agent_adapter=FakeAgentAdapter(),
                terminal_driver=DummyTerminal(),
                channel=ch,
                cmd_runner=fake_runner,
            )
            try:
                summary = daemon.run_once(now=1000)
                self.assertEqual(summary.events_processed, 1)
                self.assertEqual(ch.sent[-1]["text"], "Built feature!")

                # Seal the topic via /done
                ch.updates.append(
                    InboundMessage(update_id=2, chat_id="c1", thread_id="77", sender_id="u1", text="/done")
                )
                daemon.run_once(now=1010)
                sess = daemon.db.get_session_by_thread("77")
                self.assertEqual(sess["status"], "sealed")

                # Posting to sealed topic triggers 2-option inline keyboard instead of running agent
                ch.updates.append(
                    InboundMessage(update_id=3, chat_id="c1", thread_id="77", sender_id="u1", text="one more tweak")
                )
                s2 = daemon.run_once(now=1020)
                self.assertEqual(s2.events_processed, 0)
                self.assertIsNotNone(ch.sent[-1]["buttons"])
                self.assertEqual(len(ch.sent[-1]["buttons"][0]), 2)

                # Clicking option 2 unlocks to pinned and executes the pending message
                ch.updates.append(
                    InboundMessage(
                        update_id=4,
                        chat_id="c1",
                        thread_id="77",
                        sender_id="u1",
                        text="",
                        is_callback=True,
                        callback_id="cb-1",
                        callback_data=f"seal_pin:{sess['session_key']}",
                    )
                )
                s3 = daemon.run_once(now=1030)
                self.assertEqual(s3.events_processed, 1)
                self.assertEqual(daemon.db.get_session(sess["session_key"])["status"], "pinned")
            finally:
                daemon.close()


if __name__ == "__main__":
    unittest.main()
