"""Tests for LocalChannel, CompositeChannel, `hq chat`, and `hq telegram detect-chat`."""

import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hq_pilot.channels.base import InlineButton
from hq_pilot.channels.local import LocalChannel
from hq_pilot.channels.registry import CompositeChannel, get_channel
from hq_pilot.channels.telegram import TelegramChannel, detect_telegram_chats
from hq_pilot.chat_cli import ChatSession, format_outbound_message
from hq_pilot.cli import main as hq_main
from hq_pilot.daemon import HQDaemon
from hq_pilot.db import HQDatabase
from hq_pilot.notify_cli import deliver_notification

try:
    from tests.test_adapters_and_channels import FakeAgentAdapter
    from tests.test_review_fixes import RecordingTerminal
except ModuleNotFoundError:
    from test_adapters_and_channels import FakeAgentAdapter
    from test_review_fixes import RecordingTerminal


class TestLocalChat(unittest.TestCase):
    def test_local_channel_roundtrip_threads_and_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = HQDatabase(Path(tmp) / "runtime.sqlite")
            try:
                ch = LocalChannel(db)
                tid = ch.create_thread("[svc] auth-fix")
                self.assertEqual(tid, "topic-1")
                threads = db.list_local_threads()
                self.assertEqual(len(threads), 1)
                self.assertEqual(threads[0]["closed"], 0)

                mid = ch.send_message(
                    "Need review",
                    thread_id=tid,
                    buttons=[[InlineButton("Accept", "defer:s1:1"), InlineButton("Reject", "defer:s1:2")]],
                )
                self.assertEqual(mid, "local:1")
                recent = db.list_recent_local_outbound(15)
                self.assertEqual(len(recent), 1)
                formatted = format_outbound_message(recent[0])
                self.assertIn("[msg#1][#topic-1] Need review", formatted)
                self.assertIn("[1] Accept", formatted)
                self.assertIn("[2] Reject", formatted)

                # Inbound message & callback polling
                db.add_local_message(direction="inbound", thread_id=tid, text="hello bot")
                db.add_local_message(
                    direction="inbound",
                    thread_id=tid,
                    text="",
                    is_callback=True,
                    callback_data="defer:s1:1",
                    reply_to_msg_id="local:1",
                )
                msgs = ch.poll_updates()
                self.assertEqual(len(msgs), 2)
                self.assertEqual(msgs[0].text, "hello bot")
                self.assertEqual(msgs[0].thread_id, tid)
                self.assertTrue(msgs[1].is_callback)
                self.assertEqual(msgs[1].callback_data, "defer:s1:1")
                self.assertEqual(msgs[1].message_id, "local:1")
                # Second poll returns empty (consumed=1)
                self.assertEqual(ch.poll_updates(), [])

                self.assertTrue(ch.answer_callback("local-cb:3", "Received"))
                self.assertTrue(ch.close_thread(tid))
                self.assertEqual(db.list_local_threads()[0]["closed"], 1)
            finally:
                db.close()

    def test_composite_channel_and_fallback_to_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = HQDatabase(Path(tmp) / "runtime.sqlite")
            try:
                # Missing Telegram credentials -> automatic fallback to LocalChannel
                ch_fallback = get_channel("telegram", secrets={}, allowed_chat_id=None, db=db)
                self.assertIsInstance(ch_fallback, LocalChannel)

                # Explicit none -> None
                self.assertIsNone(get_channel("none", secrets={}, db=db))

                # With Telegram credentials + db -> CompositeChannel
                ch_comp = get_channel(
                    "telegram",
                    secrets={"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "-100123"},
                    db=db,
                )
                self.assertIsInstance(ch_comp, CompositeChannel)

                # Mock TelegramChannel._call_api for dual-write verification
                tg_calls = []

                def fake_api(url, body, timeout):
                    tg_calls.append((url, body))
                    if url.endswith("/sendMessage"):
                        return {"ok": True, "result": {"message_id": 777}}
                    if url.endswith("/createForumTopic"):
                        return {"ok": True, "result": {"message_thread_id": 88}}
                    if url.endswith("/getUpdates"):
                        return {"ok": True, "result": []}
                    return {"ok": True, "result": True}

                tg = TelegramChannel("tok", "-100123", request_fn=fake_api)
                comp = CompositeChannel([tg, LocalChannel(db)])
                tid = comp.create_thread("Dual Topic")
                self.assertEqual(tid, "88")
                self.assertEqual(db.list_local_threads()[0]["thread_id"], "88")

                mid = comp.send_message("Hello both", thread_id=tid)
                self.assertEqual(mid, "777")
                self.assertEqual(len(db.list_local_outbound()), 1)
                self.assertEqual(db.list_local_outbound()[0]["text"], "Hello both")
                self.assertTrue(any(u.endswith("/sendMessage") for u, _ in tg_calls))
            finally:
                db.close()

    def test_chat_session_commands_and_daemon_end_to_end_with_deferred_pick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])

            agent = FakeAgentAdapter()
            term = RecordingTerminal(reply_fn=lambda name, text: f"FakeReply: {text}")

            def forbid_runner(cmd, cwd):
                raise AssertionError(f"Headless cmd_runner must never be called: {cmd}")

            daemon = HQDaemon(hq_dir, agent_adapter=agent, terminal_driver=term, cmd_runner=forbid_runner)
            try:
                self.assertIsInstance(daemon.channel, LocalChannel)
                chat = ChatSession(daemon.db)

                # 1. Plain #General message -> LocalChannel -> HQDaemon -> FakeAgent -> outbound
                cont, out = chat.handle_line("Summarize today's status", now=1000)
                self.assertTrue(cont)
                self.assertEqual(out, [])

                daemon.run_once(now=1001)
                outbound = daemon.db.list_local_outbound()
                self.assertTrue(any("FakeReply:" in r["text"] for r in outbound))

                # 2. Topic switch & /topics listing
                daemon.channel.create_thread("api-refactor")
                cont, out = chat.handle_line("/topics")
                self.assertTrue(cont)
                self.assertTrue(any("#topic-1" in line for line in out))

                cont, out = chat.handle_line("/topic topic-1")
                self.assertEqual(chat.current_thread_id, "topic-1")
                self.assertEqual(chat.prompt_label(), "[#topic-1] > ")
                cont, out = chat.handle_line("/general")
                self.assertIsNone(chat.current_thread_id)

                # 3. Background hq-notify --level review -> /pick -> deferred_resume
                daemon.db.create_session(
                    session_key="s-rev-1",
                    track="C",
                    agent_backend="agy",
                    status="active",
                    channel_thread_id="topic-1",
                    now=1005,
                )
                msg_id = deliver_notification(
                    hq_dir=hq_dir,
                    level="review",
                    message="Approve schema migration?",
                    session_key="s-rev-1",
                    options=["Accept", "Reject"],
                    now=1010,
                )
                self.assertIsNotNone(msg_id)
                numeric_id = msg_id.split(":")[1]

                cont, out = chat.handle_line(f"/pick {numeric_id} 1", now=1015)
                self.assertTrue(cont)
                self.assertTrue(any("Picked [1] Accept" in line for line in out))

                daemon.run_once(now=1020)
                # Deferred action should be resolved and #General interactive agent prompted with chosen option
                act = daemon.db.get_deferred_action(msg_id)
                self.assertIsNotNone(act["resolved_at"])
                self.assertTrue(any("Accept" in c["text"] for c in term.prompt_calls))
            finally:
                daemon.close()

    def test_telegram_detect_chat_and_cli(self) -> None:
        def fake_api(url, body, timeout):
            return {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {
                                "id": -100998877,
                                "type": "supergroup",
                                "is_forum": True,
                                "title": "HQ Command Center",
                            }
                        },
                    },
                    {
                        "update_id": 2,
                        "message": {
                            "chat": {
                                "id": -100998877,
                                "type": "supergroup",
                                "is_forum": True,
                                "title": "HQ Command Center",
                            }
                        },
                    },
                ],
            }

        chats = detect_telegram_chats("tok-123", request_fn=fake_api)
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["chat_id"], "-100998877")
        self.assertTrue(chats[0]["is_forum"])
        self.assertEqual(chats[0]["title"], "HQ Command Center")

        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            (hq_dir / ".hq" / "secrets.env").write_text(
                "TELEGRAM_BOT_TOKEN=tok-123\n", encoding="utf-8"
            )
            buf = io.StringIO()
            with patch("hq_pilot.cli.detect_telegram_chats", return_value=chats), patch(
                "sys.stdout", buf
            ):
                rc = hq_main(["telegram", "detect-chat", "--hq-dir", str(hq_dir)])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("-100998877", out)
            self.assertIn("is_forum=True", out)
            self.assertIn("HQ Command Center", out)


if __name__ == "__main__":
    unittest.main()
