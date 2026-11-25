"""Tests for v3 interaction design: Feishu single-window routing to #General,
button-less busy batch queuing & merging, Topic seal result reflow,
daytime + nightly quiet rotation with handoff summary, and pane recovery."""

from __future__ import annotations

from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
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
from hq_pilot.terminals.base import AgentBlockedError
from hq_pilot.terminals.herdr import HerdrDriver
from hq_pilot.terminals.tmux import TmuxDriver

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
        """When agy is blocked awaiting approval, queuing card has no buttons and approval card is pushed once."""
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
                queue_cards = [m for m in ch.sent if m["buttons"] is None]
                approval_cards = [m for m in ch.sent if m["buttons"] is not None]
                self.assertEqual(len(queue_cards), 1)
                self.assertEqual(len(approval_cards), 1)
                self.assertIsNone(queue_cards[0]["thread_id"])
                self.assertEqual(
                    queue_cards[0]["text"],
                    "⏸️ 正在等待审批，请先点击审批卡片确认；消息已排队",
                )
                self.assertIsNone(approval_cards[0]["thread_id"])
                self.assertIn("面板 `hq-general` 等待操作审批", approval_cards[0]["text"])
                card_id = queue_cards[0]["id"]

                # Second message while still blocked updates queue card count in-place & does NOT duplicate approval card
                ch.inbound.append(InboundMessage(2, "c1", None, "u1", "顺便看下日志", message_id="m-blk-2"))
                daemon.run_once(now=1020)
                self.assertEqual(len(ch.sent), 2)
                self.assertEqual(ch.card_updates[-1]["message_id"], card_id)
                self.assertEqual(
                    ch.card_updates[-1]["text"],
                    "⏸️ 正在等待审批，请先点击审批卡片确认；消息已排队（共 2 条）",
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

    def test_herdr_and_tmux_driver_send_keys_and_read_visible_pane(self) -> None:
        """HerdrDriver send_keys (agent + pane fallback) & read_visible_pane, plus TmuxDriver key mapping."""
        drv = HerdrDriver(bin_path="herdr", session="s-test")

        # 1. HerdrDriver.send_keys succeeds via `agent send-keys`
        calls_ok = []

        def fake_run_ok(self_drv, args, check=True):
            calls_ok.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch.object(HerdrDriver, "_run", fake_run_ok):
            drv.send_keys("hq-general", ["down", "enter"])
        self.assertEqual(calls_ok, [["agent", "send-keys", "hq-general", "down", "enter"]])

        # 2. HerdrDriver.send_keys falls back to `pane send-keys` when `agent send-keys` returns non-0
        calls_fb = []

        def fake_run_fb(self_drv, args, check=True):
            calls_fb.append(list(args))
            if args[:2] == ["agent", "send-keys"]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="agent not found")
            if args[:2] == ["pane", "send-keys"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

        with patch.object(HerdrDriver, "_run", fake_run_fb), patch.object(
            HerdrDriver, "_resolve_pane_id", return_value="w5:p1"
        ):
            drv.send_keys("hq-general", ["esc"])
        self.assertEqual(
            calls_fb,
            [
                ["agent", "send-keys", "hq-general", "esc"],
                ["pane", "send-keys", "w5:p1", "esc"],
            ],
        )

        # 3. HerdrDriver.send_keys raises RuntimeError if fallback also fails
        def fake_run_fail(self_drv, args, check=True):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="pane missing")

        with patch.object(HerdrDriver, "_run", fake_run_fail), patch.object(
            HerdrDriver, "_resolve_pane_id", return_value="w5:p1"
        ):
            with self.assertRaises(RuntimeError):
                drv.send_keys("hq-general", ["enter"])

        # 4. HerdrDriver.read_visible_pane calls `agent read --source visible --lines N --format text`
        calls_read = []

        def fake_run_read(self_drv, args, check=True):
            calls_read.append(list(args))
            return subprocess.CompletedProcess(
                args, 0, stdout="Allow command?\n❯ 1. Yes\n  2. No\n", stderr=""
            )

        with patch.object(HerdrDriver, "_run", fake_run_read):
            txt = drv.read_visible_pane("hq-general", lines=25)
        self.assertIn("❯ 1. Yes", txt)
        self.assertEqual(
            calls_read,
            [
                [
                    "agent",
                    "read",
                    "hq-general",
                    "--source",
                    "visible",
                    "--lines",
                    "25",
                    "--format",
                    "text",
                ]
            ],
        )

        # 5. HerdrDriver.read_visible_pane falls back to read_pane on non-zero rc
        def fake_run_read_fail(self_drv, args, check=True):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="failed")

        with patch.object(HerdrDriver, "_run", fake_run_read_fail), patch.object(
            HerdrDriver, "read_pane", return_value="fallback pane output"
        ) as mock_rp:
            txt_fb = drv.read_visible_pane("hq-general", lines=20)
        self.assertEqual(txt_fb, "fallback pane output")
        mock_rp.assert_called_once_with("hq-general", lines=20)

        # 6. TmuxDriver.send_keys maps canonical key names
        tmux_drv = TmuxDriver(bin_path="tmux")
        tmux_calls = []

        def fake_tmux_run(self_drv, args, check=True):
            tmux_calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch.object(TmuxDriver, "_run", fake_tmux_run):
            tmux_drv.send_keys("hq-general", ["up", "down", "tab", "space", "esc", "escape", "enter"])
        self.assertEqual(
            tmux_calls,
            [["send-keys", "-t", "hq-general", "Up", "Down", "Tab", "Space", "Escape", "Escape", "Enter"]],
        )

    def test_remote_approval_default_buttons_dedupe_and_safety_guard(self) -> None:
        """Immediate blocked approval card, same-screen dedupe, click sends ['enter'], safety blocks stale clicks."""

        class ApprovalTerminal(StatusAwareTerminal):
            def __init__(self, reply_fn=None):
                super().__init__(reply_fn=reply_fn)
                self.visible_screens = {}
                self.sent_keys = []

            def read_visible_pane(self, name: str, lines: int = 30) -> str:
                return self.visible_screens.get(name, "")

            def send_keys(self, name: str, keys) -> None:
                self.sent_keys.append((name, list(keys)))

        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            term = ApprovalTerminal()
            ch = IndicatorFakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                sess = daemon._ensure_general_session(1000)
                skey = sess["session_key"]
                self.assertEqual(daemon.config.tuning.pane_blocked_notify_sec, 0)

                term.set_pane(GENERAL_PANE_NAME, status="blocked")
                term.visible_screens[GENERAL_PANE_NAME] = (
                    "\x1b[33mRun shell command: git push origin main\x1b[0m\n"
                    "────────────────────────────────────\n"
                    "Confirm? (Enter to allow, Esc to deny)\n"
                )

                # Tick 1 (same timestamp as last_active=1000): pushes approval card IMMEDIATELY
                daemon.run_once(now=1000)
                self.assertEqual(len(ch.sent), 1)
                card1 = ch.sent[0]
                self.assertIsNone(card1["thread_id"])
                self.assertIn("git push origin main", card1["text"])
                self.assertNotIn("\x1b[33m", card1["text"])
                flat_btns = [b for row in card1["buttons"] for b in row]
                self.assertEqual([b.text for b in flat_btns], ["✅ 允许", "❌ 拒绝"])
                allow_cb = flat_btns[0].callback_data
                self.assertEqual(allow_cb, f"approve_key:{skey}:allow")

                # Tick 2 (still blocked, identical screen): MUST NOT send duplicate card
                daemon.run_once(now=1002)
                self.assertEqual(len(ch.sent), 1)

                # User clicks [✅ 允许] while pane is blocked -> sends ['enter'] and refreshes card in-place
                ch.inbound.append(
                    InboundMessage(
                        update_id=10,
                        chat_id="c1",
                        thread_id=None,
                        sender_id="u1",
                        text="",
                        is_callback=True,
                        callback_id="cb-allow-1",
                        callback_data=allow_cb,
                        message_id=card1["id"],
                    )
                )
                daemon.run_once(now=1005)
                self.assertEqual(term.sent_keys, [(GENERAL_PANE_NAME, ["enter"])])
                self.assertEqual(ch.card_updates[-1]["message_id"], card1["id"])
                self.assertEqual(
                    ch.card_updates[-1]["text"],
                    "✅ 已执行审批（✅ 允许），继续执行中...",
                )
                # Same-tick scan_pane_blocked MUST NOT re-send the old card while terminal clears popup
                self.assertEqual(len(ch.sent), 1)

                # Clicking the same card a second time -> NO extra keys sent, replies expired/processed
                ch.inbound.append(
                    InboundMessage(
                        update_id=11,
                        chat_id="c1",
                        thread_id=None,
                        sender_id="u1",
                        text="",
                        is_callback=True,
                        callback_id="cb-allow-2",
                        callback_data=allow_cb,
                        message_id=card1["id"],
                    )
                )
                daemon.run_once(now=1006)
                self.assertEqual(len(term.sent_keys), 1)
                self.assertEqual(ch.card_updates[-1]["text"], "该审批已处理或已失效。")

                # Safety guard: new approval card pushed, then pane leaves blocked before click -> ZERO keys sent!
                term.set_pane(GENERAL_PANE_NAME, status="working")
                daemon.run_once(now=1008)
                term.set_pane(GENERAL_PANE_NAME, status="blocked")
                term.visible_screens[GENERAL_PANE_NAME] = "Delete temp files?"
                daemon.run_once(now=1010)
                self.assertEqual(len(ch.sent), 2)
                card2 = ch.sent[1]
                allow_cb2 = card2["buttons"][0][0].callback_data

                # Pane resolved externally (status becomes idle) before user clicks button
                term.set_pane(GENERAL_PANE_NAME, status="idle")
                ch.inbound.append(
                    InboundMessage(
                        update_id=12,
                        chat_id="c1",
                        thread_id=None,
                        sender_id="u1",
                        text="",
                        is_callback=True,
                        callback_id="cb-stale",
                        callback_data=allow_cb2,
                        message_id=card2["id"],
                    )
                )
                daemon.run_once(now=1012)
                self.assertEqual(len(term.sent_keys), 1)  # Still only the 1 valid keypress!
                self.assertEqual(ch.card_updates[-1]["text"], "该审批已处理或已失效。")

                # Explicit pane_blocked_notify_sec > 0 still enforces delay threshold
                update_tuning_value(hq_dir, "pane_blocked_notify_sec", "60")
                daemon.db.update_session(skey, last_active_at=2000)
                term.set_pane(GENERAL_PANE_NAME, status="blocked")
                term.visible_screens[GENERAL_PANE_NAME] = "Delayed prompt?"
                daemon.run_once(now=2030)
                self.assertEqual(len(ch.sent), 2)  # Not yet >= 60s
                daemon.run_once(now=2060)
                self.assertEqual(len(ch.sent), 3)  # Now >= 60s -> pushed!
            finally:
                daemon.close()

    def test_remote_approval_numbered_menu_with_cursor_key_sequences(self) -> None:
        """Numbered menus (cursor at item 1 or item 2) generate option buttons + cancel with exact key sequences."""

        class MenuTerminal(StatusAwareTerminal):
            def __init__(self):
                super().__init__()
                self.visible_screens = {}
                self.sent_keys = []

            def read_visible_pane(self, name: str, lines: int = 30) -> str:
                return self.visible_screens.get(name, "")

            def send_keys(self, name: str, keys) -> None:
                self.sent_keys.append((name, list(keys)))

        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            term = MenuTerminal()
            ch = IndicatorFakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                sess = daemon._ensure_general_session(1000)
                skey = sess["session_key"]
                term.set_pane(GENERAL_PANE_NAME, status="blocked")

                # Case A: Cursor `❯` on item 1 -> pick item 2 (down+enter), then cancel (esc)
                term.visible_screens[GENERAL_PANE_NAME] = (
                    "Allow running `rm -rf dist/`?\n"
                    "❯ 1. Yes\n"
                    "  2. Yes, always for this session\n"
                    "  3. No, tell me what to do\n"
                    "? for shortcuts   auto · high\n"
                )
                daemon.run_once(now=1000)
                self.assertEqual(len(ch.sent), 1)
                card_a = ch.sent[0]
                btns_a = [b for row in card_a["buttons"] for b in row]
                self.assertEqual(len(btns_a), 4)
                self.assertEqual(btns_a[0].text, "1. Yes")
                self.assertTrue(btns_a[1].text.startswith("2. Yes, always"))
                self.assertTrue(btns_a[2].text.startswith("3. No"))
                self.assertEqual(btns_a[3].text, "❌ 取消")

                # Click option 2 -> sends ["down", "enter"]
                ch.inbound.append(
                    InboundMessage(
                        1, "c1", None, "u1", "",
                        is_callback=True,
                        callback_id="cb-opt2",
                        callback_data=btns_a[1].callback_data,
                        message_id=card_a["id"],
                    )
                )
                daemon.run_once(now=1005)
                self.assertEqual(term.sent_keys[-1], (GENERAL_PANE_NAME, ["down", "enter"]))
                self.assertTrue(ch.card_updates[-1]["text"].startswith("✅ 已执行审批"))

                # Case B: Cursor `❯` on item 2 -> pick item 1 (up+enter)
                term.visible_screens[GENERAL_PANE_NAME] = (
                    "Choose permission scope:\n"
                    "  1. Allow once\n"
                    "❯ 2. Allow for this session\n"
                    "  3. Deny\n"
                )
                daemon.run_once(now=1010)
                self.assertEqual(len(ch.sent), 2)
                card_b = ch.sent[1]
                btns_b = [b for row in card_b["buttons"] for b in row]
                self.assertEqual(btns_b[0].text, "1. Allow once")

                ch.inbound.append(
                    InboundMessage(
                        2, "c1", None, "u1", "",
                        is_callback=True,
                        callback_id="cb-opt1",
                        callback_data=btns_b[0].callback_data,
                        message_id=card_b["id"],
                    )
                )
                daemon.run_once(now=1012)
                self.assertEqual(term.sent_keys[-1], (GENERAL_PANE_NAME, ["up", "enter"]))

                # Case C: Click ❌ 取消 -> sends ["esc"] and updates card with ❌ 已执行操作
                term.visible_screens[GENERAL_PANE_NAME] = (
                    "Pick deployment target:\n"
                    "  1. Staging\n"
                    "❯ 2. Production\n"
                )
                daemon.run_once(now=1015)
                self.assertEqual(len(ch.sent), 3)
                card_c = ch.sent[2]
                btns_c = [b for row in card_c["buttons"] for b in row]
                self.assertEqual(btns_c[-1].text, "❌ 取消")
                ch.inbound.append(
                    InboundMessage(
                        3, "c1", None, "u1", "",
                        is_callback=True,
                        callback_id="cb-cancel",
                        callback_data=btns_c[-1].callback_data,
                        message_id=card_c["id"],
                    )
                )
                daemon.run_once(now=1016)
                self.assertEqual(term.sent_keys[-1], (GENERAL_PANE_NAME, ["esc"]))
                self.assertEqual(ch.card_updates[-1]["text"], "❌ 已执行操作（❌ 取消）")
            finally:
                daemon.close()

    def test_remote_approval_consecutive_two_prompts_in_same_turn(self) -> None:
        """Two consecutive approval popups in the same turn (unchanged last_active_at) both push cards."""

        class TwoStepTerminal(StatusAwareTerminal):
            def __init__(self):
                super().__init__()
                self.screen = "Step 1: Run `git fetch`?"
                self.sent_keys = []

            def read_visible_pane(self, name: str, lines: int = 30) -> str:
                return self.screen

            def send_keys(self, name: str, keys) -> None:
                self.sent_keys.append((name, list(keys)))
                # Immediately advance to step 2 prompt within the same turn (pane stays blocked)
                if "Step 1" in self.screen:
                    self.screen = "Step 2: Run `git rebase origin/main`?"

        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            term = TwoStepTerminal()
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

                # Prompt 1 pushed
                daemon.run_once(now=1000)
                self.assertEqual(len(ch.sent), 1)
                self.assertIn("Step 1", ch.sent[0]["text"])
                cb1 = ch.sent[0]["buttons"][0][0].callback_data

                # User approves Prompt 1 -> terminal immediately shows Prompt 2 -> Card 2 pushed!
                ch.inbound.append(
                    InboundMessage(
                        1, "c1", None, "u1", "",
                        is_callback=True,
                        callback_id="cb-step1",
                        callback_data=cb1,
                        message_id=ch.sent[0]["id"],
                    )
                )
                daemon.run_once(now=1005)
                self.assertEqual(len(term.sent_keys), 1)
                self.assertEqual(len(ch.sent), 2)
                self.assertIn("Step 2", ch.sent[1]["text"])
                cb2 = ch.sent[1]["buttons"][0][0].callback_data

                # User approves Prompt 2
                ch.inbound.append(
                    InboundMessage(
                        2, "c1", None, "u1", "",
                        is_callback=True,
                        callback_id="cb-step2",
                        callback_data=cb2,
                        message_id=ch.sent[1]["id"],
                    )
                )
                daemon.run_once(now=1010)
                self.assertEqual(len(term.sent_keys), 2)
                self.assertEqual(term.sent_keys, [(GENERAL_PANE_NAME, ["enter"]), (GENERAL_PANE_NAME, ["enter"])])
            finally:
                daemon.close()

    def test_agent_blocked_approval_stop_reflow_and_system_message_no_leak(self) -> None:
        """
        Regression covering two coupled flows:
          1. Completed Feishu sync turn clears last_input_origin; subsequent SYSTEM_MESSAGE wake
             (PreInvocation with stale lastUserInput + Stop with no new USER_INPUT) MUST NOT push to Feishu.
          2. AgentBlockedError pushes approval card & sets pending_approval_reply; clicking approve sends key;
             subsequent Stop pushes final reply to main window (thread_id=None) once, consumes flag,
             queued messages drain automatically, and later SYSTEM_MESSAGE Stop MUST NOT leak to Feishu.
        """

        class FlowTerminal(StatusAwareTerminal):
            name = "herdr"

            def __init__(self, tfile: Path):
                super().__init__()
                self.tfile = tfile
                self.should_block = False
                self.visible_screen = "Run deploy.sh?\n❯ 1. Allow\n  2. Deny\n"
                self.sent_keys = []

            def read_visible_pane(self, name: str, lines: int = 30) -> str:
                return self.visible_screen

            def send_keys(self, name: str, keys) -> None:
                self.sent_keys.append((name, list(keys)))

            def prompt_agent(self, name, text, wait=True, timeout_ms=300000):
                self.prompt_calls.append({"name": name, "text": text, "wait": wait, "timeout_ms": timeout_ms})
                if self.should_block:
                    self.set_pane(name, status="blocked")
                    raise AgentBlockedError(pane_name=name, detail="awaiting approval")
                with self.tfile.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"type": "USER_INPUT", "content": text}, ensure_ascii=False) + "\n")
                    fh.write(json.dumps({"type": "PLANNER_RESPONSE", "content": f"同步回复：{text[:12]}"}, ensure_ascii=False) + "\n")
                return ""

        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            tfile = Path(tmp) / "gen_flow.jsonl"
            tfile.write_text("", encoding="utf-8")

            term = FlowTerminal(tfile)
            ch = IndicatorFakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                sess = daemon._ensure_general_session(1000)
                skey = sess["session_key"]
                daemon.db.update_session(
                    skey,
                    agent_session_id="conv-gen-flow",
                    transcript_path=str(tfile),
                    turn_count=1,
                )
                term.set_pane(GENERAL_PANE_NAME, status="idle")

                # --- Part 1: Normal sync Feishu turn, then background SYSTEM_MESSAGE wake ---
                ch.inbound.append(InboundMessage(1, "c1", None, "u1", "现在在干啥", message_id="m-q1"))
                s1 = daemon.run_once(now=1010)
                self.assertEqual(s1.events_processed, 1)
                self.assertEqual(ch.card_updates[-1]["text"], "同步回复：现在在干啥")
                self.assertIsNone(daemon.db.get_meta(f"last_input_origin:{skey}"))
                self.assertIsNone(daemon.db.get_meta(f"pending_approval_reply:{skey}"))
                # Advance reply_line_offset as if Stop fired during sync wait
                daemon.db.update_session(skey, reply_line_offset=2)

                sent_before_bg = len(ch.sent)
                # Background subagent / timer wakes via SYSTEM_MESSAGE:
                # PreInvocation still carries stale lastUserInput="现在在干啥" (in daemon_dispatches!),
                # transcript appends ONLY PLANNER_RESPONSE (no new USER_INPUT).
                with tfile.open("a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {"type": "PLANNER_RESPONSE", "content": "后台子 Agent 定时进度汇报（不应推飞书）"},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                with patch.dict(os.environ, {"HQ_SESSION_KEY": skey}):
                    handle_hook_event(
                        "agy",
                        {
                            "hookEventName": "PreInvocation",
                            "conversationId": "conv-gen-flow",
                            "lastUserInput": "<USER_REQUEST>现在在干啥</USER_REQUEST>",
                        },
                        hq_dir=hq_dir,
                        channel=ch,
                        now=1100,
                    )
                    bg_stop = handle_hook_event(
                        "agy",
                        {
                            "hookEventName": "Stop",
                            "conversationId": "conv-gen-flow",
                            "transcriptPath": str(tfile),
                        },
                        hq_dir=hq_dir,
                        channel=ch,
                        now=1105,
                    )
                self.assertFalse(bg_stop["reply_pushed"])
                self.assertEqual(len(ch.sent), sent_before_bg)

                # --- Part 2: AgentBlockedError -> Feishu approve -> Stop pushes final reply once -> queue drains ---
                term.should_block = True
                ch.inbound.append(InboundMessage(2, "c1", None, "u1", "执行部署脚本", message_id="m-q2"))
                s2 = daemon.run_once(now=1200)
                self.assertEqual(s2.events_processed, 1)
                self.assertEqual(ch.card_updates[-1]["text"], "⏸️ 需要审批，请点击下方审批卡片确认")
                self.assertIsNone(daemon.db.get_meta(f"daemon_waiting:{skey}"))
                self.assertEqual(daemon.db.get_meta(f"pending_approval_reply:{skey}"), "1")
                self.assertEqual(daemon.db.get_meta(f"last_input_origin:{skey}"), "daemon")

                approval_card = ch.sent[-1]
                self.assertIsNone(approval_card["thread_id"])
                self.assertIn("Run deploy.sh?", approval_card["text"])
                allow_cb = approval_card["buttons"][0][0].callback_data

                # Another Feishu message arrives while blocked -> parked in queue
                ch.inbound.append(InboundMessage(3, "c1", None, "u1", "顺便查下版本号", message_id="m-q3"))
                s_park = daemon.run_once(now=1205)
                self.assertEqual(s_park.events_parked, 1)

                # User clicks Allow on Feishu approval card
                ch.inbound.append(
                    InboundMessage(
                        4, "c1", None, "u1", "",
                        is_callback=True,
                        callback_id="cb-deploy-allow",
                        callback_data=allow_cb,
                        message_id=approval_card["id"],
                    )
                )
                daemon.run_once(now=1210)
                self.assertEqual(term.sent_keys, [(GENERAL_PANE_NAME, ["enter"])])

                # Agent finishes approved tool and triggers Stop (no new USER_INPUT, only PLANNER_RESPONSE)
                with tfile.open("a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {"type": "PLANNER_RESPONSE", "content": "部署脚本已执行完毕，全部成功！"},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                with patch.dict(os.environ, {"HQ_SESSION_KEY": skey}):
                    approved_stop = handle_hook_event(
                        "agy",
                        {
                            "hookEventName": "Stop",
                            "conversationId": "conv-gen-flow",
                            "transcriptPath": str(tfile),
                        },
                        hq_dir=hq_dir,
                        channel=ch,
                        now=1220,
                    )
                self.assertTrue(approved_stop["reply_pushed"])
                self.assertEqual(ch.sent[-1]["text"], "部署脚本已执行完毕，全部成功！")
                self.assertIsNone(ch.sent[-1]["thread_id"])
                # One-shot flags consumed!
                self.assertIsNone(daemon.db.get_meta(f"pending_approval_reply:{skey}"))
                self.assertIsNone(daemon.db.get_meta(f"last_input_origin:{skey}"))

                # Subsequent background SYSTEM_MESSAGE Stop MUST NOT leak again!
                sent_after_approved = len(ch.sent)
                with tfile.open("a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {"type": "PLANNER_RESPONSE", "content": "审批后的后续后台定时回报（不应推飞书）"},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                with patch.dict(os.environ, {"HQ_SESSION_KEY": skey}):
                    handle_hook_event(
                        "agy",
                        {
                            "hookEventName": "PreInvocation",
                            "conversationId": "conv-gen-flow",
                            "lastUserInput": "<USER_REQUEST>执行部署脚本</USER_REQUEST>",
                        },
                        hq_dir=hq_dir,
                        channel=ch,
                        now=1250,
                    )
                    bg_stop2 = handle_hook_event(
                        "agy",
                        {
                            "hookEventName": "Stop",
                            "conversationId": "conv-gen-flow",
                            "transcriptPath": str(tfile),
                        },
                        hq_dir=hq_dir,
                        channel=ch,
                        now=1255,
                    )
                self.assertFalse(bg_stop2["reply_pushed"])
                self.assertEqual(len(ch.sent), sent_after_approved)

                # Once pane returns idle & transcript quiet, queued message "顺便查下版本号" drains automatically!
                term.should_block = False
                term.set_pane(GENERAL_PANE_NAME, status="idle")
                daemon.db.set_meta(f"transcript_mtime:{skey}", "1255", now=1255)
                s_drain = daemon.run_once(now=1400)
                self.assertEqual(s_drain.events_processed, 1)
                self.assertEqual(term.prompt_calls[-1]["text"], "顺便查下版本号")
                self.assertEqual(ch.card_updates[-1]["text"], "同步回复：顺便查下版本号")
            finally:
                daemon.close()


if __name__ == "__main__":
    unittest.main()
