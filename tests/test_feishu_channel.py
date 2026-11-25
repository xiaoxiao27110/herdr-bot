"""Unit tests for FeishuChannel, registry integration, and `hq feishu detect-chat`."""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hq_pilot.channels.base import InlineButton
from hq_pilot.channels.feishu import FeishuChannel, detect_feishu_chats
from hq_pilot.channels.local import LocalChannel
from hq_pilot.channels.registry import CompositeChannel, get_channel
from hq_pilot.cli import main as hq_main
from hq_pilot.db import HQDatabase


class TestFeishuChannel(unittest.TestCase):
    def test_token_cache_send_text_and_reply_in_thread(self) -> None:
        calls = []

        def mock_req(method, url, body, headers, timeout):
            calls.append((method, url, body, headers))
            if url.endswith("/open-apis/auth/v3/tenant_access_token/internal"):
                return {
                    "code": 0,
                    "tenant_access_token": "t-cached-001",
                    "expire": 7200,
                }
            if url.endswith("/open-apis/im/v1/messages?receive_id_type=chat_id"):
                return {"code": 0, "data": {"message_id": "om_root_1"}}
            if url.endswith("/open-apis/im/v1/messages/om_root_1/reply"):
                return {"code": 0, "data": {"message_id": "om_reply_2"}}
            return {"code": 0, "data": {}}

        ch = FeishuChannel(
            app_id="cli_abc",
            app_secret="sec_xyz",
            allowed_chat_id="oc_target_1",
            request_fn=mock_req,
        )

        # Create thread -> sends anchor card to chat_id and returns message_id
        tid = ch.create_thread("Task Alpha")
        self.assertEqual(tid, "om_root_1")

        # Send plain text in thread -> uses /reply with reply_in_thread=True
        mid = ch.send_message("Progress update", thread_id=tid)
        self.assertEqual(mid, "om_reply_2")

        # Close thread -> replies "已归档" in thread
        self.assertTrue(ch.close_thread("om_root_1"))

        # tenant_access_token should have been fetched only once across all 3 API calls
        token_calls = [c for c in calls if "tenant_access_token" in c[1]]
        self.assertEqual(len(token_calls), 1)

        reply_call = [c for c in calls if c[1].endswith("/om_root_1/reply")][0]
        self.assertEqual(reply_call[0], "POST")
        self.assertEqual(reply_call[3]["Authorization"], "Bearer t-cached-001")
        self.assertTrue(reply_call[2]["reply_in_thread"])
        self.assertEqual(reply_call[2]["msg_type"], "interactive")
        reply_card = json.loads(reply_call[2]["content"])
        self.assertEqual(reply_card["schema"], "2.0")
        self.assertEqual(
            reply_card["body"]["elements"],
            [{"tag": "markdown", "content": "Progress update"}],
        )

    def test_send_message_without_buttons_uses_schema2_markdown_card(self) -> None:
        captured = []

        def mock_req(method, url, body, headers, timeout):
            captured.append((method, url, body))
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "t-md", "expire": 7200}
            return {"code": 0, "data": {"message_id": "om_md_1"}}

        ch = FeishuChannel(
            app_id="cli_abc",
            app_secret="sec_xyz",
            allowed_chat_id="oc_target_1",
            request_fn=mock_req,
        )
        md_text = (
            "### 发布检查报告\n\n"
            "- 状态：**全部通过**\n\n"
            "| 检查项 | 结果 |\n"
            "| --- | --- |\n"
            "| 单元测试 | PASS |\n"
        )
        mid = ch.send_message(md_text)
        self.assertEqual(mid, "om_md_1")

        send_call = [c for c in captured if "im/v1/messages" in c[1]][0]
        self.assertEqual(send_call[2]["msg_type"], "interactive")
        card = json.loads(send_call[2]["content"])
        self.assertEqual(card["schema"], "2.0")
        self.assertTrue(card["config"]["wide_screen_mode"])
        self.assertEqual(
            card["body"]["elements"],
            [{"tag": "markdown", "content": md_text}],
        )

    def test_send_message_with_buttons_builds_interactive_card(self) -> None:
        captured = []

        def mock_req(method, url, body, headers, timeout):
            captured.append((method, url, body))
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            return {"code": 0, "data": {"message_id": "om_card_9"}}

        ch = FeishuChannel(
            app_id="cli_abc",
            app_secret="sec_xyz",
            allowed_chat_id="oc_target_1",
            request_fn=mock_req,
        )
        mid = ch.send_message(
            "Approve deployment?",
            buttons=[
                [
                    InlineButton("Approve", "defer:s1:1"),
                    InlineButton("Reject", "defer:s1:2"),
                ]
            ],
        )
        self.assertEqual(mid, "om_card_9")

        send_call = [c for c in captured if "im/v1/messages" in c[1]][0]
        self.assertEqual(send_call[2]["msg_type"], "interactive")
        card = json.loads(send_call[2]["content"])
        self.assertEqual(card["schema"], "2.0")
        elements = card["body"]["elements"]
        self.assertEqual(elements[0], {"tag": "markdown", "content": "Approve deployment?"})
        col_set = elements[1]
        self.assertEqual(col_set["tag"], "column_set")
        btn0 = col_set["columns"][0]["elements"][0]
        btn1 = col_set["columns"][1]["elements"][0]
        self.assertEqual(btn0["tag"], "button")
        self.assertEqual(btn0["text"]["content"], "Approve")
        self.assertEqual(btn0["value"], {"callback_data": "defer:s1:1"})
        self.assertEqual(btn1["value"], {"callback_data": "defer:s1:2"})

    def test_inbound_message_and_card_action_with_fail_closed_whitelist(self) -> None:
        from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

        ch = FeishuChannel(
            app_id="cli_abc",
            app_secret="sec_xyz",
            allowed_chat_id="oc_allowed",
            auto_start_ws=False,
        )

        # 1. Unauthorized chat -> fail-closed (not queued, but recorded in seen_chats)
        unauth_evt = P2ImMessageReceiveV1(
            {
                "event": {
                    "sender": {
                        "sender_id": {"open_id": "ou_intruder"},
                        "sender_type": "user",
                    },
                    "message": {
                        "message_id": "om_bad",
                        "chat_id": "oc_intruder",
                        "chat_type": "p2p",
                        "message_type": "text",
                        "content": json.dumps({"text": "hack"}),
                    },
                }
            }
        )
        self.assertIsNone(ch.handle_message_event(unauth_evt))
        self.assertEqual(ch.poll_updates(timeout=0), [])
        self.assertTrue(any(c["chat_id"] == "oc_intruder" for c in ch.list_seen_chats()))

        # 2. Bot's own message -> filtered out
        bot_evt = P2ImMessageReceiveV1(
            {
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_bot"}, "sender_type": "app"},
                    "message": {
                        "message_id": "om_bot",
                        "chat_id": "oc_allowed",
                        "chat_type": "group",
                        "message_type": "text",
                        "content": json.dumps({"text": "echo"}),
                    },
                }
            }
        )
        self.assertIsNone(ch.handle_message_event(bot_evt))

        # 3. Authorized group thread message -> root_id/parent_id mapped to thread_id/reply_to_msg_id
        ok_evt = P2ImMessageReceiveV1(
            {
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_alice"}, "sender_type": "user"},
                    "message": {
                        "message_id": "om_msg_10",
                        "root_id": "om_root_1",
                        "parent_id": "om_parent_9",
                        "chat_id": "oc_allowed",
                        "chat_type": "group",
                        "message_type": "text",
                        "content": json.dumps({"text": "ship it"}),
                    },
                }
            }
        )
        parsed = ch.handle_message_event(ok_evt)
        self.assertIsNotNone(parsed)

        # 4. Card action trigger callback
        card_evt = P2CardActionTrigger(
            {
                "event": {
                    "operator": {"open_id": "ou_alice"},
                    "token": "cb_tok_1",
                    "action": {"tag": "button", "value": {"callback_data": "defer:s1:1"}},
                    "context": {
                        "open_message_id": "om_card_9",
                        "open_chat_id": "oc_allowed",
                    },
                }
            }
        )
        resp = ch.handle_card_action(card_evt)
        self.assertIsNotNone(resp)

        msgs = ch.poll_updates(timeout=0)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].text, "ship it")
        self.assertEqual(msgs[0].thread_id, "om_root_1")
        self.assertEqual(msgs[0].reply_to_msg_id, "om_parent_9")
        self.assertEqual(msgs[0].sender_id, "ou_alice")
        self.assertFalse(msgs[0].is_callback)

        self.assertTrue(msgs[1].is_callback)
        self.assertEqual(msgs[1].callback_data, "defer:s1:1")
        self.assertEqual(msgs[1].message_id, "om_card_9")
        self.assertEqual(msgs[1].callback_id, "cb_tok_1")

        # Empty allowed_chat_id -> fail-closed
        closed = FeishuChannel("cli_abc", "sec_xyz", allowed_chat_id="", auto_start_ws=False)
        self.assertEqual(closed.poll_updates(timeout=0), [])
        with self.assertRaises(RuntimeError):
            closed.send_message("blocked")

    def test_registry_composite_fallback_and_detect_chat_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = HQDatabase(Path(tmp) / "runtime.sqlite")
            try:
                # Missing credentials -> graceful fallback to LocalChannel
                ch_fallback = get_channel("feishu", secrets={}, db=db)
                self.assertIsInstance(ch_fallback, LocalChannel)

                # Missing credentials + db=None -> None
                self.assertIsNone(get_channel("feishu", secrets={"FEISHU_APP_ID": "a"}, db=None))

                # Full credentials + db -> CompositeChannel([FeishuChannel, LocalChannel])
                ch_comp = get_channel(
                    "feishu",
                    secrets={
                        "FEISHU_APP_ID": "cli_abc",
                        "FEISHU_APP_SECRET": "sec_xyz",
                        "FEISHU_CHAT_ID": "oc_123",
                    },
                    db=db,
                )
                self.assertIsInstance(ch_comp, CompositeChannel)
                self.assertIsInstance(ch_comp.channels[0], FeishuChannel)
                self.assertIsInstance(ch_comp.channels[1], LocalChannel)

                # Full credentials + db=None -> FeishuChannel
                ch_only = get_channel(
                    "feishu",
                    secrets={
                        "FEISHU_APP_ID": "cli_abc",
                        "FEISHU_APP_SECRET": "sec_xyz",
                        "FEISHU_CHAT_ID": "oc_123",
                    },
                    db=None,
                )
                self.assertIsInstance(ch_only, FeishuChannel)
            finally:
                db.close()

        # detect_feishu_chats + CLI `hq feishu detect-chat`
        def mock_detect_req(method, url, body, headers, timeout):
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "t-det", "expire": 7200}
            if "/open-apis/im/v1/chats" in url:
                return {
                    "code": 0,
                    "data": {
                        "items": [
                            {"chat_id": "oc_grp_1", "name": "Dev Ops", "chat_mode": "group"}
                        ]
                    },
                }
            return {"code": 0, "data": {}}

        det = detect_feishu_chats("cli_abc", "sec_xyz", request_fn=mock_detect_req)
        self.assertEqual(len(det["group_chats"]), 1)
        self.assertEqual(det["group_chats"][0]["chat_id"], "oc_grp_1")

        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            (hq_dir / ".hq" / "secrets.env").write_text(
                "FEISHU_APP_ID=cli_abc\nFEISHU_APP_SECRET=sec_xyz\n",
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch("hq_pilot.cli.detect_feishu_chats", return_value=det), patch(
                "sys.stdout", buf
            ):
                rc = hq_main(["feishu", "detect-chat", "--hq-dir", str(hq_dir), "--timeout", "0"])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("Feishu tenant_access_token: OK", out)
            self.assertIn("oc_grp_1", out)
            self.assertIn("Dev Ops", out)

    def test_working_indicator_and_composite_mirroring(self) -> None:
        calls = []

        def mock_req(method, url, body, headers, timeout):
            calls.append((method, url, body))
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "t-ind", "expire": 7200}
            if method == "POST" and "/open-apis/im/v1/messages" in url:
                return {"code": 0, "data": {"message_id": "om_placeholder_1"}}
            if method == "PATCH" and "/open-apis/im/v1/messages/om_placeholder_1" in url:
                return {"code": 0, "data": {}}
            return {"code": 0, "data": {}}

        with tempfile.TemporaryDirectory() as tmp:
            db = HQDatabase(Path(tmp) / "runtime.sqlite")
            try:
                feishu = FeishuChannel(
                    "cli_abc",
                    "sec_xyz",
                    allowed_chat_id="oc_123",
                    request_fn=mock_req,
                    auto_start_ws=False,
                )
                local = LocalChannel(db)
                comp = CompositeChannel([feishu, local])

                mid = comp.send_working_indicator("⏳ 收到，正在处理中...")
                self.assertEqual(mid, "om_placeholder_1")

                ok = comp.finish_working_indicator(mid, "最终回复内容")
                self.assertTrue(ok)

                # Verify PATCH was called on Feishu
                patch_calls = [c for c in calls if c[0] == "PATCH"]
                self.assertEqual(len(patch_calls), 1)
                self.assertIn("/open-apis/im/v1/messages/om_placeholder_1", patch_calls[0][1])
                self.assertIn("最终回复内容", patch_calls[0][2]["content"])

                # Verify final reply was mirrored to LocalChannel
                msgs = db.list_recent_local_outbound(10)
                self.assertEqual(len(msgs), 1)
                self.assertEqual(msgs[0]["text"], "最终回复内容")
            finally:
                db.close()

    def test_daemon_dedupe_uses_message_id_across_restarts(self) -> None:
        from hq_pilot.channels.base import InboundMessage
        from hq_pilot.daemon import HQDaemon

        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            daemon = HQDaemon(hq_dir)
            try:
                # Two distinct messages that both have update_id=1 (e.g. after daemon restart)
                msg1 = InboundMessage(
                    update_id=1,
                    chat_id="oc_123",
                    thread_id=None,
                    sender_id="ou_1",
                    text="你好",
                    message_id="om_first_msg",
                )
                msg2 = InboundMessage(
                    update_id=1,
                    chat_id="oc_123",
                    thread_id=None,
                    sender_id="ou_1",
                    text="第二条消息",
                    message_id="om_second_msg",
                )
                daemon._handle_inbound(msg1, now=1000)
                daemon._handle_inbound(msg2, now=2000)
                # Duplicate of msg2 should still be deduplicated
                daemon._handle_inbound(msg2, now=2001)

                rows = daemon.db.conn.execute(
                    "SELECT dedupe_key FROM events WHERE source='channel' ORDER BY id ASC"
                ).fetchall()
                keys = [r["dedupe_key"] for r in rows]
                self.assertEqual(keys, ["channel:oc_123:om_first_msg", "channel:oc_123:om_second_msg"])
            finally:
                daemon.close()


if __name__ == "__main__":
    unittest.main()
