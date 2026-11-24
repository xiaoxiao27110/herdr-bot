"""Additional tests for CLI commands, hq-notify, #General sealing, memory limits, and drivers."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from hq_pilot.agents.agy import AgyAdapter
from hq_pilot.channels.base import InboundMessage
from hq_pilot.channels.telegram import TelegramChannel
from hq_pilot.cli import main as hq_main
from hq_pilot.config import load_config, update_tuning_value
from hq_pilot.daemon import HQDaemon
from hq_pilot.db import HQDatabase
from hq_pilot.gatekeeper import glob_match
from hq_pilot.guards import run_memory_guard
from hq_pilot.hook_cli import handle_hook_event
from hq_pilot.notify_cli import deliver_notification
from hq_pilot.terminals.herdr import HerdrDriver, install_herdr_guide
from hq_pilot.terminals.registry import get_terminal_driver
from hq_pilot.terminals.tmux import TmuxDriver
try:
    from tests.test_adapters_and_channels import DummyTerminal, FakeChannel
    from tests.test_archiver_resume_cursors import _init_git_repo
except ModuleNotFoundError:
    from test_adapters_and_channels import DummyTerminal, FakeChannel
    from test_archiver_resume_cursors import _init_git_repo


class TestCLIAndEdgeCases(unittest.TestCase):
    def test_glob_match_double_star(self) -> None:
        self.assertTrue(glob_match("api/v1/users.py", "api/**"))
        self.assertTrue(glob_match("docs/readme.md", "**/*.md"))
        self.assertTrue(glob_match("readme.md", "**/*.md"))
        self.assertFalse(glob_match("api/v1/users.py", "jobs/**"))

    def test_notify_four_levels_and_deferred_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            ch = FakeChannel()

            # Digest -> buffered, not sent immediately
            m_dig = deliver_notification(hq_dir, "digest", "overnight summary", channel=ch, now=100)
            self.assertIsNone(m_dig)
            self.assertEqual(len(ch.sent), 0)

            # Notify -> sent immediately
            m_not = deliver_notification(hq_dir, "notify", "build green", channel=ch, now=105)
            self.assertIsNotNone(m_not)
            self.assertEqual(len(ch.sent), 1)

            # Question -> creates deferred_action & suspends session
            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            db.create_session("s-q", "C", "agy", channel_thread_id="50", now=100)
            db.close()

            m_q = deliver_notification(
                hq_dir, "question", "Proceed?", session_key="s-q",
                options=["Yes", "No"], channel=ch, now=110,
            )
            self.assertIsNotNone(m_q)
            db2 = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                self.assertEqual(db2.get_session("s-q")["suspended_msg_id"], m_q)
                expired = db2.expire_deferred_actions(now=110 + 25 * 3600)
                self.assertEqual(expired, 1)
                self.assertIsNone(db2.get_session("s-q")["suspended_msg_id"])
            finally:
                db2.close()

    def test_hook_stop_pushes_clean_reply_to_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            tfile = Path(tmp) / "t.jsonl"
            tfile.write_text(
                "\n".join([
                    json.dumps({"role": "user", "content": "check status"}),
                    json.dumps({"role": "assistant", "content": "All tests pass."}),
                ]) + "\n",
                encoding="utf-8",
            )
            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            db.create_session(
                "thread:88:100", "C", "agy", status="active",
                channel_thread_id="88", agent_session_id="conv-88",
                transcript_path=str(tfile), now=100,
            )
            db.close()

            ch = FakeChannel()
            res = handle_hook_event(
                "agy",
                {"hook_event_name": "Stop", "conversation_id": "conv-88", "transcript_path": str(tfile)},
                hq_dir=hq_dir,
                channel=ch,
                now=110,
            )
            self.assertTrue(res["reply_pushed"])
            self.assertEqual(ch.sent[-1]["text"], "All tests pass.")
            self.assertEqual(ch.sent[-1]["thread_id"], "88")

    def test_general_session_idle_and_max_turns_seal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            update_tuning_value(hq_dir, "general_max_turns", "2")

            def fake_runner(cmd, cwd):
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps({"reply": "ok", "sid": "gen-sid-1"}), stderr=""
                )

            ch = FakeChannel(
                updates=[
                    InboundMessage(1, "c1", None, "u1", "msg 1"),
                    InboundMessage(2, "c1", None, "u1", "msg 2"),
                ]
            )
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=AgyAdapter(),
                terminal_driver=DummyTerminal(),
                channel=ch,
                cmd_runner=fake_runner,
            )
            try:
                daemon.run_once(now=1000)
                # After 2 turns, next tick seals general:1
                daemon.run_once(now=1010)
                s1 = daemon.db.get_session("general:1")
                self.assertEqual(s1["status"], "sealed")
            finally:
                daemon.close()

    def test_memory_guard_line_and_byte_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            update_tuning_value(hq_dir, "index_max_lines", "5")
            update_tuning_value(hq_dir, "today_wal_max_lines", "3")
            (hq_dir / "AGENTS.md").write_text("\n".join(f"line {i}" for i in range(10)), encoding="utf-8")
            (hq_dir / "notes" / "today.md").write_text("1\n2\n3\n4\n", encoding="utf-8")
            res = run_memory_guard(hq_dir)
            self.assertFalse(res.ok)
            self.assertTrue(any("AGENTS.md exceeds max lines" in v for v in res.violations))
            self.assertTrue(any("notes/today.md exceeds max lines" in v for v in res.violations))

    def test_cli_pin_unpin_status_doctor_sync_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            subprocess.run(["git", "init", "-b", "main"], cwd=str(hq_dir), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(hq_dir), check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(hq_dir), check=True)

            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            db.create_session("s-cli", "C", "agy", topic_slug="fix-auth", now=100)
            db.close()

            self.assertEqual(hq_main(["pin", "fix-auth", "--hq-dir", str(hq_dir)]), 0)
            self.assertEqual(hq_main(["unpin", "s-cli", "--hq-dir", str(hq_dir)]), 0)
            self.assertEqual(hq_main(["status", "--hq-dir", str(hq_dir)]), 0)
            self.assertEqual(hq_main(["doctor", "--hq-dir", str(hq_dir)]), 0)
            self.assertEqual(hq_main(["sync", "--hq-dir", str(hq_dir), "--no-push"]), 0)

            # Test bootstrap cloning from a local bare git repo
            upstream = Path(tmp) / "upstream-gw"
            _init_git_repo(upstream)
            (hq_dir / "hq.yaml").write_text(
                f"version: 1\nproject_name: test\nrepos:\n  gateway-svc:\n    git_url: {upstream}\n    default_branch: main\n",
                encoding="utf-8",
            )
            self.assertEqual(hq_main(["bootstrap", "--hq-dir", str(hq_dir)]), 0)
            self.assertTrue((hq_dir / ".hq" / "repos" / "gateway-svc" / "README.md").exists())

    def test_terminal_drivers_and_telegram_callback(self) -> None:
        self.assertIn("herdr", install_herdr_guide())
        self.assertEqual(HerdrDriver().attach_cmd("p1"), ["herdr"])
        self.assertEqual(
            HerdrDriver(session="hq").attach_cmd("p1"), ["herdr", "session", "attach", "hq"]
        )
        self.assertEqual(TmuxDriver().attach_cmd("p1"), ["tmux", "attach-session", "-t", "p1"])
        drv = get_terminal_driver("tmux")
        self.assertEqual(drv.name, "tmux")

        answered = []

        def mock_req(url, body, timeout):
            method = url.rsplit("/", 1)[-1]
            if method == "getUpdates":
                return {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 20,
                            "callback_query": {
                                "id": "cb-99",
                                "from": {"id": 5},
                                "data": "seal_pin:s1",
                                "message": {
                                    "message_id": 300,
                                    "message_thread_id": 44,
                                    "chat": {"id": -1001},
                                },
                            },
                        }
                    ],
                }
            if method == "answerCallbackQuery":
                answered.append(body)
                return {"ok": True, "result": True}
            return {"ok": True, "result": True}

        ch = TelegramChannel("tok", "-1001", request_fn=mock_req)
        msgs = ch.poll_updates()
        self.assertEqual(len(msgs), 1)
        self.assertTrue(msgs[0].is_callback)
        self.assertEqual(msgs[0].callback_data, "seal_pin:s1")
        self.assertTrue(ch.answer_callback("cb-99", "OK"))
        self.assertEqual(len(answered), 1)

    def test_morning_digest_clears_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            ch = FakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=AgyAdapter(),
                terminal_driver=DummyTerminal(),
                channel=ch,
            )
            try:
                daemon.db.add_digest_item("alert", "dirty_worktree", "wt-1 dirty", now=100)
                daemon.run_morning_digest(now=200)
                self.assertEqual(len(ch.sent), 1)
                self.assertIn("wt-1 dirty", ch.sent[0]["text"])
                self.assertEqual(daemon.db.list_pending_digest_items(), [])
            finally:
                daemon.close()


if __name__ == "__main__":
    unittest.main()
