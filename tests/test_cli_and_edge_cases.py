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
            self.assertEqual(ch.sent[0]["text"], "【提醒】build green")
            self.assertNotIn("[NOTIFY]", ch.sent[0]["text"])

            # Question -> creates deferred_action & suspends session
            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            db.create_session("s-q", "C", "agy", channel_thread_id="50", now=100)
            db.close()

            m_q = deliver_notification(
                hq_dir, "question", "Proceed?", session_key="s-q",
                options=["Yes", "No"], channel=ch, now=110,
            )
            self.assertIsNotNone(m_q)
            self.assertEqual(ch.sent[1]["text"], "【待确认】Proceed?")
            self.assertNotIn("[QUESTION]", ch.sent[1]["text"])

            # Review -> uses 【待审批】 prefix
            m_rev = deliver_notification(
                hq_dir, "review", "Check release", channel=ch, now=115,
            )
            self.assertIsNotNone(m_rev)
            self.assertEqual(ch.sent[2]["text"], "【待审批】Check release")
            self.assertNotIn("[REVIEW]", ch.sent[2]["text"])
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
                # After 2 turns, next tick (+10s) marks due="size" without interrupting active chat
                daemon.run_once(now=1010)
                self.assertEqual(daemon.db.get_meta("general_rotation_due"), "size")
                self.assertEqual(daemon.db.get_session("general:1")["status"], "active")

                # After 30m continuous quiet -> rotates with handoff summary to general:2
                s_rot = daemon.run_once(now=1000 + 30 * 60)
                self.assertTrue(s_rot.general_rotated)
                self.assertEqual(daemon.db.get_session("general:1")["status"], "sealed")
                self.assertEqual(daemon.db.get_session("general:2")["status"], "active")
                self.assertEqual(daemon.db.get_meta("general_handoff_pending"), "1")
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

            # Task session_key `task:<repo>:<slug>:<ts>` must not false-positive as logical path,
            # while real broken `<repo>:<relpath>` references are still caught.
            (hq_dir / "AGENTS.md").write_text("# Index\n", encoding="utf-8")
            (hq_dir / "notes" / "today.md").write_text("# Today\n", encoding="utf-8")
            (hq_dir / "tasks" / "demo-slug.md").write_text(
                "# Task: demo-slug\n\n- session_key: task:gateway-svc:demo-slug:1700000000\n",
                encoding="utf-8",
            )
            res_ok = run_memory_guard(hq_dir)
            self.assertTrue(res_ok.ok, msg=res_ok.violations)

            (hq_dir / "tasks" / "demo-slug.md").write_text(
                "# Task: demo-slug\n\n- see `gateway-svc:missing/file.py`\n",
                encoding="utf-8",
            )
            res_broken = run_memory_guard(hq_dir)
            self.assertFalse(res_broken.ok)
            self.assertTrue(any("broken logical path 'gateway-svc:missing/file.py'" in v for v in res_broken.violations))

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
                daemon.db.add_digest_item("alert", "dead_event", "任务事件 #1（channel）连续重试 3 次失败，已转入死信：boom", now=101)
                daemon.db.add_digest_item("alert", "dream_error", "夜间记忆整理失败：timeout", now=102)
                daemon.db.add_digest_item("alert", "outbox_dead", "消息 #2（notify）连续重试 5 次投递失败：502", now=103)
                daemon.db.add_digest_item("digest", "quiet_hours_downgrade", "构建已完成", now=104)
                daemon.db.add_digest_item("digest", "agent_digest", "夜间巡检无异常", now=105)
                daemon.run_morning_digest(now=200)
                self.assertEqual(len(ch.sent), 1)
                digest_text = ch.sent[0]["text"]
                self.assertTrue(digest_text.startswith("### 🌅 每日晨报"))
                self.assertIn("【脏工作区提醒】wt-1 dirty", digest_text)
                self.assertIn("【失败事件】任务事件 #1", digest_text)
                self.assertIn("【夜间整理异常】夜间记忆整理失败：timeout", digest_text)
                self.assertIn("【外发投递失败】消息 #2", digest_text)
                self.assertIn("【夜间免打扰汇总】构建已完成", digest_text)
                self.assertIn("【进展汇总】夜间巡检无异常", digest_text)
                for forbidden in (
                    "[L0 Morning Digest]",
                    "[dead_event]",
                    "[dream_error]",
                    "[outbox_dead]",
                    "[dirty_worktree]",
                    "[quiet_hours_downgrade]",
                    "[agent_digest]",
                ):
                    self.assertNotIn(forbidden, digest_text)
                self.assertEqual(daemon.db.list_pending_digest_items(), [])

                # Empty digest outputs Chinese all-clear
                daemon.run_morning_digest(now=300)
                self.assertEqual(len(ch.sent), 2)
                empty_digest = ch.sent[1]["text"]
                self.assertTrue(empty_digest.startswith("### 🌅 每日晨报"))
                self.assertIn("一切正常，暂无待处理告警或滞留工作区。", empty_digest)
                self.assertNotIn("All clear", empty_digest)
            finally:
                daemon.close()

    def test_localized_system_notifications_and_cards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            repo_dir = Path(tmp) / "gateway-svc"
            _init_git_repo(repo_dir)
            (hq_dir / ".hq" / "local.yaml").write_text(
                f"repo_paths:\n  gateway-svc: {repo_dir}\n", encoding="utf-8"
            )
            ch = FakeChannel()

            class FailingDreamTerminal(DummyTerminal):
                def __init__(self) -> None:
                    super().__init__()
                    self.fail_prompt = False

                def prompt_agent(self, name, text, wait=True, timeout_ms=300000):
                    if self.fail_prompt:
                        raise RuntimeError("agent crashed")
                    return super().prompt_agent(name, text, wait=wait, timeout_ms=timeout_ms)

            term = FailingDreamTerminal()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=AgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                # 1. /task command receipt is Chinese
                daemon._handle_inbound(
                    InboundMessage(
                        update_id=1,
                        chat_id="c1",
                        thread_id=None,
                        sender_id="u1",
                        text="/task gateway-svc fix-zh 修复文案",
                    ),
                    now=1000,
                )
                self.assertIn("已启动任务 `task:gateway-svc:fix-zh:1000`", ch.sent[-1]["text"])
                self.assertIn("面板 `hq-fix-zh`", ch.sent[-1]["text"])
                self.assertIn("分支 `hq/fix-zh`", ch.sent[-1]["text"])
                self.assertNotIn("Spawned #Topic", ch.sent[-1]["text"])

                # 2. pane_blocked card & buttons are Chinese
                daemon.db.enqueue_event(
                    source="pane_blocked",
                    dedupe_key="pb:1",
                    priority=10,
                    available_at=1010,
                    payload={
                        "session_key": "task:gateway-svc:fix-zh:1000",
                        "channel_thread_id": "thread-101",
                    },
                    now=1010,
                )
                evs = daemon.db.claim_next_events(limit=5, now=1010)
                self.assertEqual(len(evs), 1)
                daemon._process_event(evs[0], now=1010)
                pb_msg = ch.sent[-1]
                self.assertEqual(
                    pb_msg["text"],
                    "⏸️ 任务 `task:gateway-svc:fix-zh:1000` 当前处于阻塞状态，等待你处理：",
                )
                self.assertNotIn("appears blocked", pb_msg["text"])
                btn_texts = [b.text for b in pb_msg["buttons"][0]]
                self.assertEqual(btn_texts, ["继续执行", "封口结束"])

                # 3. Clicking pane_continue sends confirmation receipt; clicking again reports expired/processed
                daemon._handle_callback(
                    InboundMessage(
                        update_id=2,
                        chat_id="c1",
                        thread_id="thread-101",
                        sender_id="u1",
                        text="",
                        is_callback=True,
                        callback_id="cb-pb-1",
                        callback_data="pane_continue:task:gateway-svc:fix-zh:1000",
                        message_id=pb_msg["id"],
                    ),
                    now=1015,
                )
                self.assertEqual(ch.sent[-1]["text"], "已收到你的选择：继续执行")

                daemon._handle_callback(
                    InboundMessage(
                        update_id=3,
                        chat_id="c1",
                        thread_id="thread-101",
                        sender_id="u1",
                        text="",
                        is_callback=True,
                        callback_id="cb-pb-2",
                        callback_data="pane_continue:task:gateway-svc:fix-zh:1000",
                        message_id=pb_msg["id"],
                    ),
                    now=1016,
                )
                self.assertEqual(
                    ch.sent[-1]["text"],
                    "该操作卡片已过期或已处理，请直接回复最新消息。",
                )

                # 4. Nightly dreaming failure writes Chinese dream_error digest item
                term.fail_prompt = True
                daemon.run_nightly_pipeline(now=2000)
                dream_items = [
                    it
                    for it in daemon.db.list_pending_digest_items()
                    if it["category"] == "dream_error"
                ]
                self.assertEqual(len(dream_items), 1)
                self.assertEqual(dream_items[0]["message"], "夜间记忆整理失败：agent crashed")
                self.assertNotIn("Nightly dreaming failed", dream_items[0]["message"])

                # 5. Stale blocked dirty worktree shows Chinese label in morning digest
                daemon.db.update_session(
                    "task:gateway-svc:fix-zh:1000",
                    status="sealed",
                    archive_blocked_reason="dirty_or_unpushed",
                    last_active_at=1000,
                )
                daemon.run_morning_digest(now=1000 + 50 * 3600)
                digest_text = ch.sent[-1]["text"]
                self.assertTrue(digest_text.startswith("### 🌅 每日晨报"))
                self.assertIn("【滞留脏工作区】", digest_text)
                self.assertNotIn("[dirty_stale]", digest_text)
                self.assertNotIn("[L0 Morning Digest]", digest_text)
            finally:
                daemon.close()


if __name__ == "__main__":
    unittest.main()
