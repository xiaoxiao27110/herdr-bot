"""Tests for v3 interaction design: Feishu single-window routing to #General,
button-less busy batch queuing & merging, Topic seal result reflow,
daytime + nightly quiet rotation with handoff summary, and pane recovery."""

from __future__ import annotations

from datetime import datetime
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from hq_pilot.channels.base import InboundMessage
from hq_pilot.cli import main as hq_main
from hq_pilot.config import load_config, update_tuning_value
from hq_pilot.daemon import GENERAL_PANE_NAME, HQDaemon
from hq_pilot.guards import run_memory_guard
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
    def test_guides_forbid_unsolicited_auto_topic_and_state_single_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            cfg = load_config(hq_dir)
            prompt = build_prompt(hq_dir, cfg, {"text": "hello"})
            self.assertIn("未经用户明确要求", prompt)
            self.assertIn("严禁擅自创建新 `#Topic`", prompt)
            self.assertIn("飞书单窗口只对 `#General`", prompt)
            self.assertIn("进度通知区", prompt)

            agents_md = (hq_dir / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("未经用户明确要求永不擅自新开 `#Topic`", agents_md)
            self.assertIn("飞书单窗口只对 `#General`", agents_md)
            self.assertIn("进度通知区", agents_md)

    def test_jurisdiction_guide_in_prompt_and_agents_scaffolds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # 1) Multi-repo config: dynamic project_name + sorted repos + strict jurisdiction rules
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            (hq_dir / "hq.yaml").write_text(
                "version: 1\n"
                "project_name: acme-platform\n"
                "repos:\n"
                "  zebra-svc:\n"
                "    git_url: ''\n"
                "    default_branch: main\n"
                "  alpha-api:\n"
                "    git_url: ''\n"
                "    default_branch: main\n",
                encoding="utf-8",
            )
            cfg = load_config(hq_dir)
            prompt = build_prompt(hq_dir, cfg, {"text": "check status"})

            self.assertIn("## 0. Role & Jurisdiction", prompt)
            self.assertIn("`acme-platform`", prompt)
            self.assertIn("`alpha-api`、`zebra-svc`", prompt)
            self.assertIn("管辖范围内充分授权", prompt)
            self.assertIn("管辖范围外只读参考", prompt)
            self.assertIn("严禁修改、也严禁主动提议或声称要去修改", prompt)
            self.assertIn("`herdr-bot` / `herdr`", prompt)
            self.assertIn("严禁自我迭代", prompt)
            self.assertLess(
                prompt.index("## 0. Role & Jurisdiction"),
                prompt.index("## 0.5 #General & #Topic Collaboration Guide"),
            )
            self.assertLess(
                prompt.index("## 0.5 #General & #Topic Collaboration Guide"),
                prompt.index("## 1. Repo Boundaries"),
            )

            # 2) Empty repos config: clarifies HQ memory workspace only
            (hq_dir / "hq.yaml").write_text(
                "version: 1\nproject_name: solo-memory\nrepos: {}\n",
                encoding="utf-8",
            )
            empty_cfg = load_config(hq_dir)
            empty_prompt = build_prompt(hq_dir, empty_cfg, {"text": "hello"})
            self.assertIn("`solo-memory`", empty_prompt)
            self.assertIn("尚未在 `hq.yaml` 注册业务仓", empty_prompt)
            self.assertIn("仅管辖本 `$HQ_DIR` 记忆工作区", empty_prompt)
            self.assertIn("严禁修改、也严禁主动提议或声称要去修改", empty_prompt)
            self.assertIn("`herdr-bot` / `herdr`", empty_prompt)

            # 3) `hq init` scaffold AGENTS.md includes Role & Jurisdiction
            init_agents = (hq_dir / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("## Role & Jurisdiction", init_agents)
            self.assertIn("管辖范围内充分授权", init_agents)
            self.assertIn("管辖范围外只读参考", init_agents)
            self.assertIn("严禁越界修改或自我迭代", init_agents)
            self.assertIn("`herdr-bot` / `herdr`", init_agents)

            # 4) `hq setup --non-interactive` scaffold AGENTS.md includes Role & Jurisdiction + single-window guide
            repo_a = Path(tmp) / "svc-a"
            _init_git_repo(repo_a)
            setup_hq = Path(tmp) / "setup-hq"
            with patch("sys.stdout", io.StringIO()):
                rc = hq_main(
                    [
                        "setup",
                        "--non-interactive",
                        "--hq-dir",
                        str(setup_hq),
                        "--project-name",
                        "beta-suite",
                        "--repo",
                        f"svc-a={repo_a}",
                        "--channel",
                        "local",
                    ]
                )
            self.assertEqual(rc, 0)
            setup_agents = (setup_hq / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("## Role & Jurisdiction", setup_agents)
            self.assertIn("`beta-suite`", setup_agents)
            self.assertIn("身份定位与充分授权", setup_agents)
            self.assertIn("管辖范围外只读参考", setup_agents)
            self.assertIn("严禁越界修改或自我迭代", setup_agents)
            self.assertIn("`herdr-bot` / `herdr`", setup_agents)
            self.assertIn("飞书单窗口只对 `#General`", setup_agents)

    def test_feishu_single_window_routes_all_topic_replies_to_general_main_window(self) -> None:
        """Replies inside active/sealed/archived/unknown Feishu threads all route to #General and reply in main window."""
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            term = StatusAwareTerminal(reply_fn=lambda name, text: f"GEN_OK:{name}")
            ch = IndicatorFakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                # Seed active, sealed, and archived Topic sessions with Feishu thread_ids
                daemon.db.create_session(
                    "task:gw:active-t:100",
                    "TOPIC",
                    "agy",
                    status="active",
                    channel_thread_id="th-active",
                    topic_slug="active-t",
                    pane_name="hq-active-t",
                    now=100,
                )
                daemon.db.create_session(
                    "task:gw:sealed-t:100",
                    "TOPIC",
                    "agy",
                    status="sealed",
                    channel_thread_id="th-sealed",
                    topic_slug="sealed-t",
                    pane_name="hq-sealed-t",
                    now=100,
                )
                daemon.db.create_session(
                    "task:gw:arch-t:100",
                    "TOPIC",
                    "agy",
                    status="sealed",
                    channel_thread_id="th-archived",
                    topic_slug="arch-t",
                    branch_name="hq/arch-t",
                    pane_name="hq-arch-t",
                    now=100,
                )
                daemon.db.archive_session("task:gw:arch-t:100", now=150)

                cases = [
                    (1, "th-active", "活跃话题里回一句"),
                    (2, "th-sealed", "已封口话题里回一句"),
                    (3, "th-archived", "已归档话题里回一句"),
                    (4, "th-unknown", "未知话题里回一句"),
                ]
                for uid, tid, text in cases:
                    ch.inbound.append(
                        InboundMessage(uid, "c1", tid, "u1", text, message_id=f"m-sw-{uid}")
                    )
                    s = daemon.run_once(now=1000 + uid * 10)
                    self.assertEqual(s.events_processed, 1)
                    self.assertEqual(term.prompt_calls[-1]["name"], GENERAL_PANE_NAME)
                    self.assertIn(text, term.prompt_calls[-1]["text"])
                    # Indicator and final card update both target main window (thread_id=None)
                    self.assertIsNone(ch.card_updates[-1]["thread_id"])
                    self.assertEqual(ch.card_updates[-1]["text"], f"GEN_OK:{GENERAL_PANE_NAME}")

                # Zero orphan thread:* sessions created, zero sealed/archived interception cards sent
                self.assertEqual(ch.sent, [])
                self.assertFalse(
                    any(s["session_key"].startswith("thread:") for s in daemon.db.list_sessions())
                )
                self.assertEqual(daemon.db.get_session("task:gw:sealed-t:100")["status"], "sealed")
                self.assertEqual(daemon.db.get_session("task:gw:arch-t:100")["status"], "archived")
            finally:
                daemon.close()

    def test_feishu_and_local_tty_concurrent_input_queues_feishu_without_pushing_tty(self) -> None:
        """When local Herdr TTY user is chatting with General, Feishu messages queue & merge; TTY Stop stays silent in Feishu."""
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            tfile = Path(tmp) / "gen_tty.jsonl"
            tfile.write_text(
                json.dumps({"type": "USER_INPUT", "content": "<USER_REQUEST>终端现场查一下日志</USER_REQUEST>"}, ensure_ascii=False)
                + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "content": "终端本地回复，不推飞书"}, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            term = StatusAwareTerminal(reply_fn=lambda name, text: "已合并处理你的飞书消息")
            ch = IndicatorFakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                sess = daemon._ensure_general_session(1000)
                daemon.db.update_session(
                    sess["session_key"],
                    agent_session_id="conv-tty-1",
                    transcript_path=str(tfile),
                    turn_count=1,
                )
                # Local TTY user starts a turn -> pane becomes working
                term.set_pane(GENERAL_PANE_NAME, status="working")
                with patch.dict(os.environ, {"HQ_SESSION_KEY": sess["session_key"]}):
                    pre = handle_hook_event(
                        "agy",
                        {
                            "hookEventName": "PreInvocation",
                            "conversationId": "conv-tty-1",
                            "lastUserInput": "<USER_REQUEST>终端现场查一下日志</USER_REQUEST>",
                        },
                        hq_dir=hq_dir,
                        channel=ch,
                        now=1005,
                    )
                self.assertEqual(pre["pinned_reason"], "manual_input")

                # Concurrent Feishu messages arrive while TTY turn is working -> queued in 1 batch card
                ch.inbound.append(InboundMessage(1, "c1", None, "u1", "飞书消息 A", message_id="m-tty-1"))
                ch.inbound.append(InboundMessage(2, "c1", None, "u1", "飞书消息 B", message_id="m-tty-2"))
                s1 = daemon.run_once(now=1010)
                self.assertEqual(s1.events_parked, 2)
                self.assertEqual(len(ch.sent), 1)
                self.assertIsNone(ch.sent[0]["buttons"])
                self.assertEqual(len(term.prompt_calls), 0)

                # Local TTY turn finishes -> Stop hook must NOT push TTY reply to Feishu
                with patch.dict(os.environ, {"HQ_SESSION_KEY": sess["session_key"]}):
                    stop = handle_hook_event(
                        "agy",
                        {
                            "hookEventName": "Stop",
                            "conversationId": "conv-tty-1",
                            "transcriptPath": str(tfile),
                        },
                        hq_dir=hq_dir,
                        channel=ch,
                        now=1020,
                    )
                self.assertFalse(stop["reply_pushed"])
                self.assertEqual(len(ch.sent), 1)  # Still only the 1 queuing card

                # Pane returns idle & transcript quiet -> Feishu batch merges and dispatches in 1 turn
                term.set_pane(GENERAL_PANE_NAME, status="idle")
                daemon.db.set_meta(f"transcript_mtime:{sess['session_key']}", "1020", now=1020)
                s2 = daemon.run_once(now=1200)
                self.assertEqual(s2.events_processed, 2)
                self.assertEqual(len(term.prompt_calls), 1)
                merged_prompt = term.prompt_calls[0]["text"]
                self.assertIn("[排队期间你陆续发来以下 2 条消息，请一并回复]", merged_prompt)
                self.assertIn("飞书消息 A", merged_prompt)
                self.assertIn("飞书消息 B", merged_prompt)
                self.assertEqual(ch.card_updates[-1]["message_id"], ch.sent[0]["id"])
                self.assertEqual(ch.card_updates[-1]["text"], "已合并处理你的飞书消息")
                self.assertIsNone(ch.card_updates[-1]["thread_id"])
            finally:
                daemon.close()

    def test_busy_general_batches_messages_in_single_card_and_merges_when_idle(self) -> None:
        """Busy General sends 1 button-less card, updates count in-place, persists across daemon restart,
        merges chronologically (no prefix if single), never shows '轮到你了', and keeps timers separate."""
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            term = StatusAwareTerminal(reply_fn=lambda name, text: f"DONE:{text[:40]}")
            ch = IndicatorFakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                sess = daemon._ensure_general_session(1000)
                daemon.db.update_session(sess["session_key"], turn_count=1)
                daemon.db.set_meta(f"current_work_hint:{sess['session_key']}", "v38 镜像注册", now=1000)
                term.set_pane(GENERAL_PANE_NAME, status="working")

                t1 = 1700000400  # deterministic timestamp
                t2 = t1 + 120
                t3 = t1 + 180

                # Msg 1 arrives -> 1 button-less card in main window
                ch.inbound.append(InboundMessage(1, "c1", None, "u1", "第一条排队消息", message_id="m1"))
                s1 = daemon.run_once(now=t1)
                self.assertEqual(s1.events_parked, 1)
                self.assertEqual(len(ch.sent), 1)
                self.assertIsNone(ch.sent[0]["buttons"])
                self.assertIsNone(ch.sent[0]["thread_id"])
                self.assertEqual(
                    ch.sent[0]["text"],
                    "⏳ 收到，正在排队处理...",
                )
                card1_id = ch.sent[0]["id"]

                # Msg 2 arrives -> same batch, updates card1 in-place with count=2
                ch.inbound.append(InboundMessage(2, "c1", "th-any", "u1", "第二条排队消息", message_id="m2"))
                s2 = daemon.run_once(now=t2)
                self.assertEqual(s2.events_parked, 1)
                self.assertEqual(len(ch.sent), 1)  # Still only 1 card!
                self.assertEqual(ch.card_updates[-1]["message_id"], card1_id)
                self.assertEqual(
                    ch.card_updates[-1]["text"],
                    "⏳ 收到，正在排队处理（共 2 条）...",
                )

                # Timer event also arrives while busy -> parked independently, NOT mixed into user batch
                daemon.db.create_timer(
                    reason="后台巡检定时任务",
                    fire_at=t2 + 10,
                    kind="once",
                    session_key=sess["session_key"],
                    now=t2,
                )
                daemon.run_once(now=t2 + 10)
                self.assertEqual(len(ch.sent), 1)  # Timer never sends user queuing card
            finally:
                daemon.close()

            # Simulate daemon restart -> Msg 3 joins persisted batch & refreshes SAME card1_id in-place!
            daemon2 = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                ch.inbound.append(InboundMessage(3, "c1", None, "u1", "第三条排队消息", message_id="m3"))
                s3 = daemon2.run_once(now=t3)
                self.assertEqual(s3.events_parked, 1)
                self.assertEqual(len(ch.sent), 1)
                self.assertEqual(ch.card_updates[-1]["message_id"], card1_id)
                self.assertEqual(
                    ch.card_updates[-1]["text"],
                    "⏳ 收到，正在排队处理（共 3 条）...",
                )

                # General becomes idle -> entire 3-msg batch merges into 1 turn & refreshes card1 in-place
                term.set_pane(GENERAL_PANE_NAME, status="idle")
                s4 = daemon2.run_once(now=t3 + 30)
                self.assertEqual(s4.events_processed, 3)
                self.assertEqual(len(term.prompt_calls), 1)
                expected_merged = "\n".join(
                    [
                        "[排队期间你陆续发来以下 3 条消息，请一并回复]",
                        f"1. ({datetime.fromtimestamp(t1).strftime('%H:%M')}) 第一条排队消息",
                        f"2. ({datetime.fromtimestamp(t2).strftime('%H:%M')}) 第二条排队消息",
                        f"3. ({datetime.fromtimestamp(t3).strftime('%H:%M')}) 第三条排队消息",
                    ]
                )
                self.assertEqual(term.prompt_calls[0]["text"], expected_merged)
                self.assertNotIn("后台巡检定时任务", term.prompt_calls[0]["text"])

                # Card1 refreshed directly to final reply, NEVER "轮到你了"
                card1_texts = [u["text"] for u in ch.card_updates if u["message_id"] == card1_id]
                self.assertFalse(any("轮到你了" in txt for txt in card1_texts))
                self.assertTrue(card1_texts[-1].startswith("DONE:"))

                # All 3 channel events are done; timer_wake is still waiting independently
                waiting_after = daemon2.db.list_waiting_events()
                self.assertEqual(len(waiting_after), 1)
                self.assertEqual(waiting_after[0].source, "timer_wake")

                # Next tick processes the independent timer_wake (sends timer reply to main window)
                s5 = daemon2.run_once(now=t3 + 60)
                self.assertEqual(s5.events_processed, 1)
                self.assertIn("后台巡检定时任务", term.prompt_calls[-1]["text"])
                self.assertEqual(daemon2.db.list_waiting_events(), [])
                self.assertEqual(len(ch.sent), 2)  # card1 + timer_wake reply

                # Single-message batch sends raw user text WITHOUT merge prefix
                term.set_pane(GENERAL_PANE_NAME, status="working")
                ch.inbound.append(InboundMessage(4, "c1", None, "u1", "单独一条排队消息", message_id="m4"))
                daemon2.run_once(now=t3 + 90)
                self.assertEqual(len(ch.sent), 3)
                card2_id = ch.sent[-1]["id"]
                self.assertIsNone(ch.sent[-1]["buttons"])
                term.set_pane(GENERAL_PANE_NAME, status="idle")
                daemon2.run_once(now=t3 + 100)
                self.assertEqual(term.prompt_calls[-1]["text"], "单独一条排队消息")
                self.assertNotIn("排队期间你陆续发来", term.prompt_calls[-1]["text"])
                self.assertEqual(ch.card_updates[-1]["message_id"], card2_id)
                self.assertTrue(ch.card_updates[-1]["text"].startswith("DONE:单独一条排队消息"))
            finally:
                daemon2.close()

    def test_new_message_during_batch_dispatch_opens_new_batch_and_card(self) -> None:
        """Once batch 1 starts dispatching, a new message arriving while General is still busy opens batch 2 + card 2."""
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            ch = IndicatorFakeChannel()
            daemon_ref = {}
            injected = {"done": False}

            class MidDispatchArrivalTerminal(StatusAwareTerminal):
                def prompt_agent(self, name, text, wait=True, timeout_ms=300000):
                    if not injected["done"] and "批次一消息A" in text:
                        injected["done"] = True
                        # While batch 1 is actively executing (daemon_waiting=1), msg 3 arrives!
                        d = daemon_ref["daemon"]
                        d._handle_inbound(
                            InboundMessage(3, "c1", None, "u1", "投递期间新消息", message_id="m-mid"),
                            now=1035,
                        )
                        evs = d.db.claim_next_events(limit=5, now=1035)
                        for ev in evs:
                            try:
                                d._process_event(ev, 1035)
                            except Exception:
                                pass
                    return super().prompt_agent(name, text, wait=wait, timeout_ms=timeout_ms)

            term = MidDispatchArrivalTerminal(reply_fn=lambda name, text: f"OK:{text[:24]}")
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            daemon_ref["daemon"] = daemon
            try:
                sess = daemon._ensure_general_session(1000)
                daemon.db.update_session(sess["session_key"], turn_count=1)
                term.set_pane(GENERAL_PANE_NAME, status="working")

                # Batch 1: 2 messages parked -> card 1
                ch.inbound.append(InboundMessage(1, "c1", None, "u1", "批次一消息A", message_id="m-b1-a"))
                ch.inbound.append(InboundMessage(2, "c1", None, "u1", "批次一消息B", message_id="m-b1-b"))
                daemon.run_once(now=1010)
                self.assertEqual(len(ch.sent), 1)
                card1_id = ch.sent[0]["id"]

                # Batch 1 dispatches; mid-dispatch msg 3 arrives -> opens NEW batch 2 & NEW card 2!
                term.set_pane(GENERAL_PANE_NAME, status="idle")
                s_dispatch = daemon.run_once(now=1030)
                self.assertEqual(s_dispatch.events_processed, 2)
                self.assertEqual(len(ch.sent), 2)
                card2_id = ch.sent[1]["id"]
                self.assertNotEqual(card2_id, card1_id)
                self.assertIsNone(ch.sent[1]["buttons"])

                # Batch 1 events are done; msg 3 is parked in a distinct waiting batch
                waiting = daemon.db.list_waiting_events()
                self.assertEqual(len(waiting), 1)
                self.assertEqual(waiting[0].payload["text"], "投递期间新消息")
                self.assertEqual(waiting[0].payload["busy_card_msg_id"], card2_id)

                # Next tick dispatches batch 2 and refreshes card 2 in-place
                s_next = daemon.run_once(now=1050)
                self.assertEqual(s_next.events_processed, 1)
                self.assertEqual(term.prompt_calls[-1]["text"], "投递期间新消息")
                self.assertEqual(ch.card_updates[-1]["message_id"], card2_id)
                self.assertEqual(ch.card_updates[-1]["text"], "OK:投递期间新消息")
            finally:
                daemon.close()

    def test_blocked_general_sends_approval_queue_card_without_buttons(self) -> None:
        """When agy is blocked awaiting approval, queuing card prompts opening herdr with no buttons."""
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            term = StatusAwareTerminal(reply_fn=lambda name, text: "已继续完成")
            ch = IndicatorFakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                daemon._ensure_general_session(1000)
                term.set_pane(GENERAL_PANE_NAME, status="blocked")

                ch.inbound.append(InboundMessage(1, "c1", None, "u1", "查一下状态", message_id="m-blk-1"))
                s1 = daemon.run_once(now=1010)
                self.assertEqual(s1.events_parked, 1)
                self.assertEqual(len(ch.sent), 1)
                self.assertIsNone(ch.sent[0]["buttons"])
                self.assertIsNone(ch.sent[0]["thread_id"])
                self.assertEqual(
                    ch.sent[0]["text"],
                    "⏸️ 正在等待审批，请打开 herdr 处理；消息已排队",
                )
                card_id = ch.sent[0]["id"]

                # Second message while still blocked updates card count in-place
                ch.inbound.append(InboundMessage(2, "c1", None, "u1", "顺便看下日志", message_id="m-blk-2"))
                daemon.run_once(now=1020)
                self.assertEqual(len(ch.sent), 1)
                self.assertEqual(ch.card_updates[-1]["message_id"], card_id)
                self.assertEqual(
                    ch.card_updates[-1]["text"],
                    "⏸️ 正在等待审批，请打开 herdr 处理；消息已排队（共 2 条）",
                )
            finally:
                daemon.close()

    def test_legacy_busy_wait_and_busy_fork_callbacks_reply_deprecated_without_fork(self) -> None:
        """Clicking legacy busy_wait/busy_fork buttons replies deprecated notice and NEVER spawns a fork."""
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
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
                daemon._ensure_general_session(3000)
                self.assertFalse(hasattr(daemon, "_spawn_fork_from_general"))

                ch.inbound.append(
                    InboundMessage(1, "c1", None, "u1", "帮我查一下接口", message_id="m-leg")
                )
                daemon.run_once(now=3010)
                waiting = daemon.db.list_waiting_events()
                self.assertEqual(len(waiting), 1)
                ev_id = waiting[0].id

                # Click legacy busy_wait and busy_fork callbacks
                ch.inbound.append(
                    InboundMessage(
                        2,
                        "c1",
                        "th-old",
                        "u1",
                        "",
                        is_callback=True,
                        callback_id="cb-w",
                        callback_data=f"busy_wait:{ev_id}",
                        message_id="old-card-1",
                    )
                )
                ch.inbound.append(
                    InboundMessage(
                        3,
                        "c1",
                        "th-old",
                        "u1",
                        "",
                        is_callback=True,
                        callback_id="cb-f",
                        callback_data=f"busy_fork:{ev_id}",
                        message_id="old-card-1",
                    )
                )
                daemon.run_once(now=3020)

                self.assertEqual(ch.sent[-2]["text"], "该按钮已失效，消息已在排队")
                self.assertIsNone(ch.sent[-2]["thread_id"])
                self.assertEqual(ch.sent[-1]["text"], "该按钮已失效，消息已在排队")
                self.assertIsNone(ch.sent[-1]["thread_id"])

                # Zero TOPIC sessions spawned; waiting event still intact in queue
                topics = [s for s in daemon.db.list_sessions() if s["track"] == "TOPIC"]
                self.assertEqual(topics, [])
                self.assertEqual(len(daemon.db.list_waiting_events()), 1)
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

    def test_daytime_rotation_by_turns_and_transcript_size_waits_30m_quiet(self) -> None:
        """Daytime turns>=60 or transcript>=8MB marks due='size', does not interrupt active chat,
        waits 30m quiet, rotates with handoff summary & timer migration, and supports config set hot-reload."""
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            tfile1 = Path(tmp) / "gen_turns.jsonl"
            tfile1.write_text(
                json.dumps({"type": "USER_INPUT", "content": "白天推进方案"}) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "content": "已完成第一阶段排查"}) + "\n",
                encoding="utf-8",
            )
            term = StatusAwareTerminal(reply_fn=lambda name, text: f"REPLY:{text[:24]}")
            term.set_pane(GENERAL_PANE_NAME, status="idle")
            ch = IndicatorFakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                # --- Part A: Turn count reaches default general_max_turns (60) ---
                self.assertEqual(daemon.config.tuning.general_max_turns, 60)
                self.assertEqual(daemon.config.tuning.general_rotate_transcript_mb, 8)
                self.assertEqual(daemon.config.tuning.general_rotate_quiet_minutes, 30)

                gen1 = daemon._ensure_general_session(1000)
                daemon.db.update_session(
                    gen1["session_key"],
                    agent_session_id="conv-day-turns",
                    transcript_path=str(tfile1),
                    reply_line_offset=2,
                    turn_count=60,
                    last_active_at=1000,
                )
                tm = daemon.db.create_timer(
                    reason="跟进构建进度",
                    fire_at=99999,
                    kind="once",
                    session_key="general:1",
                    now=1000,
                )

                # User keeps chatting at t=1000 + 10m -> marked due='size' but NOT rotated!
                daemon.run_once(now=1000 + 10 * 60)
                self.assertEqual(daemon.db.get_meta("general_rotation_due"), "size")
                self.assertEqual(daemon.db.get_session("general:1")["status"], "active")

                ch.inbound.append(InboundMessage(1, "c1", None, "u1", "我还在继续聊", message_id="m-chat"))
                daemon.run_once(now=1000 + 25 * 60)
                self.assertEqual(daemon.db.get_session("general:1")["status"], "active")

                # 29m after last chat (t=1000+25m+29m) -> still active!
                daemon.run_once(now=1000 + 54 * 60)
                self.assertEqual(daemon.db.get_session("general:1")["status"], "active")

                # 30m continuous quiet after last chat (t=1000+25m+30m) -> rotates!
                s_rot = daemon.run_once(now=1000 + 55 * 60)
                self.assertTrue(s_rot.general_rotated)
                self.assertEqual(daemon.db.get_session("general:1")["status"], "sealed")
                self.assertEqual(daemon.db.get_session("general:2")["status"], "active")
                self.assertEqual(daemon.db.get_timer(tm["id"])["session_key"], "general:2")
                self.assertIsNone(daemon.db.get_meta("general_rotation_due"))

                # First Feishu message after daytime rotation carries handoff summary + 1-time subtle notice
                ch.inbound.append(InboundMessage(2, "c1", None, "u1", "继续刚才的", message_id="m-after-day"))
                daemon.run_once(now=1000 + 56 * 60)
                self.assertIn("上一轮会话交接摘要", term.prompt_calls[-1]["text"])
                self.assertIn("已完成第一阶段排查", term.prompt_calls[-1]["text"])
                self.assertIn("当前对话上下文较长", ch.card_updates[-1]["text"])

                # --- Part B: Transcript file size >= 8MB triggers daytime rotation after hot-adjusted quiet window ---
                big_tfile = Path(tmp) / "gen_big.jsonl"
                with big_tfile.open("wb") as fh:
                    fh.truncate(8 * 1024 * 1024 + 64)
                os.utime(big_tfile, (1000 + 56 * 60, 1000 + 56 * 60))

                daemon.db.update_session(
                    "general:2",
                    agent_session_id="conv-day-size",
                    transcript_path=str(big_tfile),
                    turn_count=5,
                    last_active_at=1000 + 56 * 60,
                )
                # Hot-adjust general_rotate_quiet_minutes -> 15 via config set
                update_tuning_value(hq_dir, "general_rotate_quiet_minutes", "15")

                # At +10m -> marked due='size', still active
                daemon.run_once(now=1000 + 66 * 60)
                self.assertEqual(daemon.db.get_meta("general_rotation_due"), "size")
                self.assertEqual(daemon.db.get_session("general:2")["status"], "active")

                # At +15m -> rotates general:2 -> general:3!
                s_rot2 = daemon.run_once(now=1000 + 71 * 60)
                self.assertTrue(s_rot2.general_rotated)
                self.assertEqual(daemon.db.get_session("general:2")["status"], "sealed")
                self.assertEqual(daemon.db.get_session("general:3")["status"], "active")
            finally:
                daemon.close()

    def test_general_rotation_deferred_when_channel_messages_queued(self) -> None:
        """Rotation must be deferred whenever Feishu channel messages are queued/pending."""
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            term = StatusAwareTerminal(reply_fn=lambda name, text: "已先处理排队消息")
            term.set_pane(GENERAL_PANE_NAME, status="idle")
            ch = IndicatorFakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                gen = daemon._ensure_general_session(1000)
                daemon.db.update_session(gen["session_key"], turn_count=65, last_active_at=1000)
                daemon.db.set_meta("general_rotation_due", "size", now=1000)

                # Seed a queued channel event in waiting_general (simulating queued message before rotation)
                ev_id = daemon.db.enqueue_event(
                    source="channel",
                    dedupe_key="ch:queued:1",
                    priority=10,
                    available_at=1000,
                    payload={"text": "先处理我再换新", "thread_id": None, "received_at": 1000},
                    now=1000,
                )
                daemon.db.park_event_waiting(
                    ev_id,
                    payload_updates={
                        "busy_batch_id": f"batch:{ev_id}",
                        "busy_card_msg_id": "card-q-1",
                        "wait_session_key": gen["session_key"],
                    },
                )

                # Direct rotation check at +40m quiet MUST defer while channel message is queued!
                self.assertFalse(daemon._maybe_run_due_general_rotation(now=1000 + 40 * 60))
                self.assertFalse(daemon.rotate_general_session(now=1000 + 40 * 60, force=False))
                self.assertEqual(daemon.db.get_session("general:1")["status"], "active")

                # run_once processes the queued message first (updating last_active_at) and STILL defers rotation
                s1 = daemon.run_once(now=1000 + 40 * 60)
                self.assertEqual(s1.events_processed, 1)
                self.assertFalse(s1.general_rotated)
                self.assertEqual(daemon.db.get_session("general:1")["status"], "active")

                # 30m quiet after message processed -> now rotates cleanly!
                s2 = daemon.run_once(now=1000 + 70 * 60)
                self.assertTrue(s2.general_rotated)
                self.assertEqual(daemon.db.get_session("general:1")["status"], "sealed")
                self.assertEqual(daemon.db.get_session("general:2")["status"], "active")
            finally:
                daemon.close()

    def test_topic_seal_manual_and_idle_timeout_reflows_summary_to_main_and_today_wal(self) -> None:
        """Topic seal (manual or idle timeout) pushes 0-LLM Chinese summary (<=600 chars + branch) to main window
        and upserts `[WIP]` to `notes/today.md` without violating the 60-line memory guard."""
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            update_tuning_value(hq_dir, "topic_idle_seal_minutes", "30")

            # Pre-fill notes/today.md to exactly 60 lines to verify line cap preservation
            today_path = hq_dir / "notes" / "today.md"
            prefill = ["# Today WAL [UNVERIFIED / TODAY ONLY]", ""] + [
                f"- [WIP] earlier item {i}" for i in range(1, 59)
            ]
            self.assertEqual(len(prefill), 60)
            today_path.write_text("\n".join(prefill) + "\n", encoding="utf-8")

            ch = IndicatorFakeChannel()
            term = StatusAwareTerminal()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                # 1. Manual seal with >600-char last_reply -> truncated to 600 chars, main window push, today.md updated
                s1 = daemon.db.create_session(
                    "task:gw:fix-auth:100",
                    "TOPIC",
                    "agy",
                    status="active",
                    channel_thread_id="th-auth",
                    topic_slug="fix-auth",
                    repo_name="gateway-svc",
                    branch_name="hq/fix-auth",
                    pane_name="hq-fix-auth",
                    now=100,
                )
                long_reply = "已完成认证竞态修复并补齐回归单测。" + ("细节" * 400)
                daemon.db.set_meta(f"last_reply:{s1['session_key']}", long_reply, now=120)

                daemon.seal_session(s1["session_key"], now=200)
                self.assertEqual(len(ch.sent), 1)
                self.assertIsNone(ch.sent[0]["thread_id"])
                pushed1 = ch.sent[0]["text"]
                self.assertTrue(pushed1.startswith("✅ 任务 fix-auth 已完成："))
                self.assertIn("（分支 hq/fix-auth）", pushed1)
                self.assertIn("已完成认证竞态修复", pushed1)
                self.assertIn("...", pushed1)
                # Body inside prefix+suffix is capped at 600 chars
                body1 = pushed1.split("已完成：", 1)[1].rsplit("（分支 hq/fix-auth）", 1)[0]
                self.assertLessEqual(len(body1), 600)

                # Idempotent: sealing again does NOT send a duplicate summary
                daemon.seal_session(s1["session_key"], now=210)
                self.assertEqual(len(ch.sent), 1)

                # 2. Idle timeout auto-seal reads fallback reply from transcript and reflows to main window & today.md
                tfile = Path(tmp) / "topic_idle.jsonl"
                tfile.write_text(
                    json.dumps({"type": "USER_INPUT", "content": "修一下超时"}, ensure_ascii=False)
                    + "\n"
                    + json.dumps({"type": "PLANNER_RESPONSE", "content": "已调大连接池超时并通过全部单测"}, ensure_ascii=False)
                    + "\n",
                    encoding="utf-8",
                )
                daemon.db.create_session(
                    "task:gw:pool-timeout:300",
                    "TOPIC",
                    "agy",
                    status="active",
                    channel_thread_id="th-pool",
                    topic_slug="pool-timeout",
                    repo_name="gateway-svc",
                    branch_name="hq/pool-timeout",
                    pane_name="hq-pool-timeout",
                    transcript_path=str(tfile),
                    now=300,
                )
                daemon._seal_idle_topic_sessions(now=300 + 30 * 60)
                self.assertEqual(daemon.db.get_session("task:gw:pool-timeout:300")["status"], "sealed")
                self.assertEqual(len(ch.sent), 2)
                self.assertIsNone(ch.sent[1]["thread_id"])
                self.assertIn("✅ 任务 pool-timeout 已完成：", ch.sent[1]["text"])
                self.assertIn("已调大连接池超时并通过全部单测", ch.sent[1]["text"])
                self.assertIn("（分支 hq/pool-timeout）", ch.sent[1]["text"])

                # Verify notes/today.md contains both [WIP] entries AND stays <= 60 lines (memory_guard PASS)
                today_lines = today_path.read_text(encoding="utf-8").splitlines()
                self.assertLessEqual(len(today_lines), 60)
                today_text = "\n".join(today_lines)
                self.assertIn("- [WIP] Topic fix-auth 已封口：", today_text)
                self.assertIn("（分支 hq/fix-auth）", today_text)
                self.assertIn("- [WIP] Topic pool-timeout 已封口：", today_text)
                self.assertIn("（分支 hq/pool-timeout）", today_text)
                # Ensure no broken logical path or line violation in memory_guard
                (hq_dir / "AGENTS.md").write_text("# Index\n", encoding="utf-8")
                guard_res = run_memory_guard(hq_dir)
                self.assertTrue(guard_res.ok, msg=guard_res.violations)
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
                    reason="检查远端验收容器状态",
                    fire_at=9000,
                    kind="once",
                    session_key="general:1",
                    now=1000,
                )

                # 23:30 nightly closeout runs at t=2000 -> sets general_rotation_due=night
                daemon.run_nightly_pipeline(now=2000)
                self.assertEqual(daemon.db.get_meta("general_rotation_due"), "night")
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

    def test_pane_loss_restores_conversation_id_and_topic_worktree_recovery(self) -> None:
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

                # 2. Topic worktree + pane lost while active -> daemon timer_wake recreates worktree & restores conversation
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
                daemon.db.enqueue_event(
                    source="timer_wake",
                    dedupe_key="tw:wt-rec:1",
                    priority=20,
                    available_at=1015,
                    payload={
                        "timer_id": 1,
                        "kind": "once",
                        "session_key": "task:gw-repo:lost-wt:700",
                        "reason": "继续修复",
                    },
                    now=1015,
                )
                daemon.run_once(now=1015)
                self.assertTrue(missing_wt.exists())
                self.assertIn("conv-topic-77", term.start_agent_calls[-1]["agent_args"])

                # 3. Archived Topic thread reply in Feishu routes to General & replies in main window (not archived notice)
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
                self.assertEqual(term.prompt_calls[-1]["name"], GENERAL_PANE_NAME)
                self.assertEqual(ch.card_updates[-1]["text"], "restored ok")
                self.assertIsNone(ch.card_updates[-1]["thread_id"])
            finally:
                daemon.close()


if __name__ == "__main__":
    unittest.main()
