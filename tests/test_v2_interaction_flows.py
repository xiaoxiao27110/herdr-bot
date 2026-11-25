"""Tests for v2 interaction design: General-first UX, busy queue & fork card,
60-minute quiet daily rotation + timer migration + handoff summary, and pane recovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from hq_pilot.channels.base import InboundMessage
from hq_pilot.cli import main as hq_main
from hq_pilot.config import load_config
from hq_pilot.daemon import GENERAL_PANE_NAME, HQDaemon
from hq_pilot.hook_cli import handle_hook_event
from hq_pilot.prompt_builder import build_prompt

try:
    from tests.test_adapters_and_channels import FakeChannel
    from tests.test_archiver_resume_cursors import _init_git_repo
    from tests.test_general_daily_rotation import StatusAwareTerminal
    from tests.test_herdr_interactive_mode import NoHeadlessAgyAdapter
except ModuleNotFoundError:
    from test_adapters_and_channels import FakeChannel
    from test_archiver_resume_cursors import _init_git_repo
    from test_general_daily_rotation import StatusAwareTerminal
    from test_herdr_interactive_mode import NoHeadlessAgyAdapter


class IndicatorFakeChannel(FakeChannel):
    """FakeChannel that also records working indicator creations and in-place updates."""

    def __init__(self) -> None:
        super().__init__()
        self.indicators = {}
        self.card_updates = []
        self._ind_seq = 0

    @property
    def inbound(self):
        return self.updates

    def send_working_indicator(self, text: str = "⏳ 收到，正在处理中...", thread_id=None):
        self._ind_seq += 1
        mid = f"ind-{self._ind_seq}"
        self.indicators[mid] = {"text": text, "thread_id": thread_id}
        return mid

    def finish_working_indicator(self, message_id: str, final_text: str, thread_id=None) -> bool:
        self.card_updates.append({"message_id": message_id, "text": final_text, "thread_id": thread_id})
        self.indicators[message_id] = {"text": final_text, "thread_id": thread_id}
        return True


class TestV2InteractionFlows(unittest.TestCase):
    def test_guides_forbid_unsolicited_auto_topic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            cfg = load_config(hq_dir)
            prompt = build_prompt(hq_dir, cfg, {"text": "hello"})
            self.assertIn("未经用户明确要求", prompt)
            self.assertIn("严禁擅自创建新 `#Topic`", prompt)

            agents_md = (hq_dir / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("未经用户明确要求永不擅自新开 `#Topic`", agents_md)

    def test_busy_general_parks_fifo_and_updates_card_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            term = StatusAwareTerminal(reply_fn=lambda name, text: f"DONE:{text[:24]}")
            ch = IndicatorFakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                sess = daemon._ensure_general_session(1000)
                daemon.db.set_meta(f"current_work_hint:{sess['session_key']}", "v38 镜像注册", now=1000)
                term.set_pane(GENERAL_PANE_NAME, status="working")

                # Two messages arrive while General is working
                ch.inbound.append(InboundMessage(1, "c1", None, "u1", "第一条排队消息", message_id="m1"))
                ch.inbound.append(InboundMessage(2, "c1", None, "u1", "第二条排队消息", message_id="m2"))
                s1 = daemon.run_once(now=1010)
                self.assertEqual(s1.events_parked, 2)
                self.assertEqual(len(term.prompt_calls), 0)

                waiting = daemon.db.list_waiting_events()
                self.assertEqual(len(waiting), 2)
                self.assertEqual(len(ch.sent), 2)
                self.assertIn("v38 镜像注册", ch.sent[0]["text"])
                btn_labels = [b.text for b in ch.sent[0]["buttons"][0]]
                self.assertEqual(btn_labels, ["等它忙完", "另开分身并行"])

                # User clicks "等它忙完" on message 1 -> updates card in-place, stays parked while working
                ev1_id = waiting[0].id
                card1_id = waiting[0].payload["busy_card_msg_id"]
                ch.inbound.append(
                    InboundMessage(
                        3,
                        "c1",
                        None,
                        "u1",
                        "",
                        is_callback=True,
                        callback_data=f"busy_wait:{ev1_id}",
                        message_id=card1_id,
                    )
                )
                daemon.run_once(now=1020)
                self.assertTrue(any("已确认排队" in u["text"] for u in ch.card_updates))
                self.assertEqual(len(daemon.db.list_waiting_events()), 2)

                # General becomes idle -> strictly 1 FIFO message promoted & processed per tick!
                term.set_pane(GENERAL_PANE_NAME, status="idle")
                s2 = daemon.run_once(now=1030)
                self.assertEqual(s2.events_processed, 1)
                self.assertEqual(len(daemon.db.list_waiting_events()), 1)
                # Card 1 updated in-place first with "轮到你了" then with final reply
                card1_updates = [u["text"] for u in ch.card_updates if u["message_id"] == card1_id]
                self.assertTrue(any("轮到你了" in t for t in card1_updates))
                self.assertTrue(card1_updates[-1].startswith("DONE:"))

                # Next tick processes the 2nd queued message
                s3 = daemon.run_once(now=1040)
                self.assertEqual(s3.events_processed, 1)
                self.assertEqual(len(daemon.db.list_waiting_events()), 0)
            finally:
                daemon.close()

    def test_background_subagent_transcript_writes_detected_as_busy_even_when_herdr_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            tfile = Path(tmp) / "general_bg.jsonl"
            tfile.write_text(
                json.dumps({"type": "USER_INPUT", "content": "patrol"}) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "content": "spawned subagent"}) + "\n"
                + json.dumps({"type": "GENERIC", "content": "subagent still running..."}) + "\n",
                encoding="utf-8",
            )
            term = StatusAwareTerminal(reply_fn=lambda name, text: "ok")
            term.set_pane(GENERAL_PANE_NAME, status="idle")
            ch = IndicatorFakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                sess = daemon._ensure_general_session(2000)
                # reply_line_offset=2 while file now has 3 lines written at t=2050 (<120s window)
                daemon.db.update_session(
                    sess["session_key"],
                    agent_session_id="conv-bg",
                    transcript_path=str(tfile),
                    reply_line_offset=2,
                    turn_count=1,
                )
                daemon.db.set_meta(f"transcript_mtime:{sess['session_key']}", "2050", now=2050)
                daemon.db.set_meta(f"last_stop_mtime:{sess['session_key']}", "2010", now=2010)

                # At t=2100 (50s after background write < 120s), General is treated as BUSY!
                ch.inbound.append(InboundMessage(1, "c1", None, "u1", "顺便查下另一个问题", message_id="m-bg"))
                s1 = daemon.run_once(now=2100)
                self.assertEqual(s1.events_parked, 1)
                self.assertEqual(len(term.prompt_calls), 0)

                # At t=2200 (150s after last write >= 120s), transcript is quiet -> promoted & executed!
                s2 = daemon.run_once(now=2200)
                self.assertEqual(s2.events_processed, 1)
                self.assertEqual(len(term.prompt_calls), 1)
            finally:
                daemon.close()

    def test_busy_fork_callback_spawns_parallel_topic_with_general_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            repo_dir = Path(tmp) / "gateway-svc"
            _init_git_repo(repo_dir)
            (hq_dir / "hq.yaml").write_text(
                "version: 1\nproject_name: demo\nrepos:\n  gateway-svc:\n    git_url: ''\n    default_branch: main\n",
                encoding="utf-8",
            )
            (hq_dir / ".hq" / "local.yaml").write_text(
                f"python_bin: {sys.executable}\nrepo_paths:\n  gateway-svc: {repo_dir}\n",
                encoding="utf-8",
            )
            (hq_dir / "memory" / "user-notes.md").write_text(
                "# User Notes\n- 只允许单向升级，禁止降级\n", encoding="utf-8"
            )
            gen_tfile = Path(tmp) / "gen_ctx.jsonl"
            gen_tfile.write_text(
                json.dumps({"type": "USER_INPUT", "content": "容器无状态幂等"}) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "content": "已确认容器丢失自动重建方案"}) + "\n",
                encoding="utf-8",
            )

            term = StatusAwareTerminal()
            term.set_pane(GENERAL_PANE_NAME, status="working")
            ch = IndicatorFakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                gen = daemon._ensure_general_session(3000)
                daemon.db.update_session(
                    gen["session_key"],
                    agent_session_id="conv-g1",
                    transcript_path=str(gen_tfile),
                )

                ch.inbound.append(
                    InboundMessage(
                        1,
                        "c1",
                        None,
                        "u1",
                        "帮我把 gateway-svc 的健康检查接口补上单测",
                        message_id="m-fork",
                    )
                )
                daemon.run_once(now=3010)
                waiting = daemon.db.list_waiting_events()
                self.assertEqual(len(waiting), 1)
                ev_id = waiting[0].id
                card_id = waiting[0].payload["busy_card_msg_id"]

                # User clicks "另开分身并行"
                ch.inbound.append(
                    InboundMessage(
                        2,
                        "c1",
                        None,
                        "u1",
                        "",
                        is_callback=True,
                        callback_data=f"busy_fork:{ev_id}",
                        message_id=card_id,
                    )
                )
                daemon.run_once(now=3020)

                # Original waiting event marked done, new TOPIC spawned with context + constraints
                self.assertEqual(daemon.db.get_event(ev_id)["status"], "done")
                topics = [
                    s for s in daemon.db.list_sessions() if s["track"] == "TOPIC"
                ]
                self.assertEqual(len(topics), 1)
                self.assertEqual(topics[0]["repo_name"], "gateway-svc")
                self.assertTrue(Path(topics[0]["worktree_path"]).exists())
                # Verify fork prompt carried General's recent context & user-notes constraint
                fork_prompt_sent = term.prompt_calls[-1]["text"]
                if "请读取并执行指令文件" in fork_prompt_sent:
                    # Read materialized file
                    pfiles = list((hq_dir / ".hq" / "prompts").glob("*.md"))
                    self.assertTrue(len(pfiles) >= 1)
                    fork_prompt_sent = pfiles[-1].read_text(encoding="utf-8")
                self.assertIn("已确认容器丢失自动重建方案", fork_prompt_sent)
                self.assertIn("只允许单向升级，禁止降级", fork_prompt_sent)
                self.assertIn("健康检查接口补上单测", fork_prompt_sent)
                self.assertTrue(any("已另开并行分身" in u["text"] for u in ch.card_updates))
            finally:
                daemon.close()

    def test_daily_cleanup_60m_quiet_timer_migration_handoff_and_local_hook_inject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            gen_tfile = Path(tmp) / "gen_day1.jsonl"
            gen_tfile.write_text(
                json.dumps({"type": "USER_INPUT", "content": "推进容器容错改造"}) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "content": "已完成代码修改，等待明日验收"}) + "\n",
                encoding="utf-8",
            )

            term = StatusAwareTerminal(reply_fn=lambda name, text: f"REPLY:{text[:30]}")
            term.set_pane(GENERAL_PANE_NAME, status="idle")
            ch = IndicatorFakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                gen1 = daemon._ensure_general_session(1000)
                daemon.db.update_session(
                    gen1["session_key"],
                    agent_session_id="conv-day1",
                    transcript_path=str(gen_tfile),
                    reply_line_offset=2,
                    turn_count=4,
                    last_active_at=1000,
                )
                # Bind an active timer to general:1
                tm = daemon.db.create_timer(
                    reason="检查 147 验收容器状态",
                    fire_at=9000,
                    kind="once",
                    session_key="general:1",
                    now=1000,
                )

                # 23:30 nightly closeout runs at t=2000 -> sets general_rotation_due=1
                daemon.run_nightly_pipeline(now=2000)
                self.assertEqual(daemon.db.get_meta("general_rotation_due"), "1")
                self.assertEqual(daemon.db.get_session("general:1")["status"], "active")

                # Transcript write at t=3000 delays quiet window until 3000 + 3600 = 6600
                daemon.db.set_meta("transcript_mtime:general:1", "3000", now=3000)
                daemon.db.set_meta("last_stop_mtime:general:1", "3000", now=3000)
                daemon.run_once(now=5800)  # >3600s since 2000, but only 2800s since transcript write!
                self.assertEqual(daemon.db.get_session("general:1")["status"], "active")

                # At t=6605 (60m+ quiet since transcript write) -> rotates general:1 -> general:2!
                s_rot = daemon.run_once(now=6605)
                self.assertTrue(s_rot.general_rotated)
                self.assertEqual(daemon.db.get_session("general:1")["status"], "sealed")
                self.assertEqual(daemon.db.get_session("general:2")["status"], "active")

                # Timer migrated from general:1 -> general:2!
                tm_after = daemon.db.get_timer(tm["id"])
                self.assertEqual(tm_after["session_key"], "general:2")

                # First Feishu message after rotation carries handoff summary AND 1-time subtle notice
                ch.inbound.append(InboundMessage(1, "c1", None, "u1", "昨天那个容错做完了吗", message_id="m-day2"))
                daemon.run_once(now=7000)
                first_prompt = term.prompt_calls[-1]["text"]
                self.assertIn("昨日会话交接摘要", first_prompt)
                self.assertIn("已完成代码修改，等待明日验收", first_prompt)
                first_reply = ch.card_updates[-1]["text"]
                self.assertIn("🧹 昨晚已整理记忆并开启新对话", first_reply)

                # Second Feishu message does NOT repeat handoff summary or subtle notice
                ch.inbound.append(InboundMessage(2, "c1", None, "u1", "好的收到", message_id="m-day2-b"))
                daemon.run_once(now=7010)
                second_prompt = term.prompt_calls[-1]["text"]
                self.assertNotIn("昨日会话交接摘要", second_prompt)
                second_reply = ch.card_updates[-1]["text"]
                self.assertNotIn("🧹", second_reply)

                # Also verify local Herdr direct input right after another rotation injects via hook!
                self.assertTrue(daemon.rotate_general_session(now=8000, force=True))
                self.assertEqual(daemon.db.get_meta("general_handoff_pending"), "1")
            finally:
                daemon.close()

            with patch.dict(os.environ, {"HQ_SESSION_KEY": "general:3"}):
                hook_res = handle_hook_event(
                    "agy",
                    {
                        "hookEventName": "PreInvocation",
                        "conversationId": "conv-day3",
                        "lastUserInput": "<USER_REQUEST>早安，在终端里直接问一句</USER_REQUEST>",
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=8010,
                )
            self.assertEqual(len(hook_res["inject_steps"]), 1)
            self.assertIn(
                "昨日会话交接摘要",
                hook_res["inject_steps"][0]["systemMessage"]["systemMessage"],
            )

    def test_pane_loss_restores_conversation_id_and_archived_thread_prompts_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            repo_dir = Path(tmp) / "gw-repo"
            _init_git_repo(repo_dir)
            (hq_dir / "hq.yaml").write_text(
                "version: 1\nproject_name: demo\nrepos:\n  gw-repo:\n    git_url: ''\n    default_branch: main\n",
                encoding="utf-8",
            )
            (hq_dir / ".hq" / "local.yaml").write_text(
                f"python_bin: {sys.executable}\nrepo_paths:\n  gw-repo: {repo_dir}\n",
                encoding="utf-8",
            )
            term = StatusAwareTerminal(reply_fn=lambda name, text: "restored ok")
            ch = IndicatorFakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                # 1. General session has agent_session_id="conv-keep-99", but pane was closed
                gen = daemon._ensure_general_session(1000)
                daemon.db.update_session(gen["session_key"], agent_session_id="conv-keep-99")
                self.assertEqual(term.list_panes(), [])

                ch.inbound.append(InboundMessage(1, "c1", None, "u1", "还在吗", message_id="m-rec"))
                daemon.run_once(now=1010)
                self.assertEqual(len(term.start_agent_calls), 1)
                self.assertIn("--conversation", term.start_agent_calls[0]["agent_args"])
                self.assertIn("conv-keep-99", term.start_agent_calls[0]["agent_args"])

                # 2. Topic worktree + pane lost while active -> recreates worktree on branch & restores conversation
                missing_wt = hq_dir / ".hq" / "worktrees" / "wt-lost"
                daemon.db.create_session(
                    "task:gw-repo:lost-wt:700",
                    "TOPIC",
                    "agy",
                    repo_name="gw-repo",
                    worktree_path=str(missing_wt),
                    branch_name="hq/lost-wt",
                    status="active",
                    agent_session_id="conv-topic-77",
                    channel_thread_id="thread-active-77",
                    topic_slug="lost-wt",
                    now=700,
                )
                ch.inbound.append(
                    InboundMessage(2, "c1", "thread-active-77", "u1", "继续修复", message_id="m-wt-rec")
                )
                daemon.run_once(now=1015)
                self.assertTrue(missing_wt.exists())
                self.assertIn("conv-topic-77", term.start_agent_calls[-1]["agent_args"])

                # 3. Archived Topic thread reply prompts `herdr-bot resume` instead of creating orphan topic
                daemon.db.create_session(
                    "task:gw:old-feat:500",
                    "TOPIC",
                    "agy",
                    status="sealed",
                    channel_thread_id="thread-arch-1",
                    topic_slug="old-feat",
                    branch_name="hq/old-feat",
                    now=500,
                )
                daemon.db.archive_session("task:gw:old-feat:500", now=600)

                ch.inbound.append(
                    InboundMessage(3, "c1", "thread-arch-1", "u1", "这个旧任务还能接着改吗", message_id="m-arch")
                )
                daemon.run_once(now=1020)
                self.assertIn("已归档", ch.sent[-1]["text"])
                self.assertIn("herdr-bot resume task:gw:old-feat:500", ch.sent[-1]["text"])
            finally:
                daemon.close()


if __name__ == "__main__":
    unittest.main()
