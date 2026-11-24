"""Tests for durable outbox retries, quiet-hours auto-downgrade, and rate limiting."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from typing import List, Optional, Tuple

from hq_pilot.agents.base import CodingAgentAdapter, HeadlessResult, Turn
from hq_pilot.channels.base import InboundMessage, InlineButton, MessagingChannel
from hq_pilot.cli import main as hq_main
from hq_pilot.config import TUNING_DESCRIPTIONS, TuningConfig, load_config, update_tuning_value
from hq_pilot.daemon import HQDaemon, OUTBOX_BACKOFF_SCHEDULE
from hq_pilot.db import HQDatabase
from hq_pilot.notify_cli import deliver_notification, is_in_quiet_hours
from hq_pilot.terminals.base import PaneInfo, TerminalDriver


def _local_ts(hour: int, minute: int, day: int = 15) -> int:
    return int(datetime(2026, 6, day, hour, minute, 0).timestamp())


class _StubAgent(CodingAgentAdapter):
    name = "stub"
    index_filename = "AGENTS.md"

    def build_headless_cmd(
        self, prompt, session_id=None, model=None, effort=None, add_dirs=None
    ):
        return ["stub"]

    def build_interactive_cmd(
        self, initial_prompt=None, session_id=None, model=None, effort=None, add_dirs=None
    ):
        return ["stub"]

    def parse_headless_output(self, stdout: str) -> HeadlessResult:
        return HeadlessResult(reply_markdown=stdout)

    def read_transcript_slice(self, transcript_path, from_line: int = 0):
        return [], from_line

    def count_transcript_lines(self, transcript_path) -> int:
        return 0

    def ensure_hooks_installed(self, workspace_dir, hook_bin) -> Path:
        return Path(workspace_dir)


class _StubTerminal(TerminalDriver):
    name = "stub"

    def run_bg(self, name, cwd, cmd) -> None:
        return None

    def list_panes(self) -> List[PaneInfo]:
        return []

    def send_text(self, name, text, enter: bool = True) -> None:
        return None

    def read_pane(self, name, lines: int = 80) -> str:
        return ""

    def attach_cmd(self, name) -> List[str]:
        return ["stub", name]

    def kill_pane(self, name) -> None:
        return None

    def is_client_attached(self, name) -> bool:
        return False


class _FlakyChannel(MessagingChannel):
    name = "flaky"

    def __init__(self, fail_times: int = 0, return_none: bool = False) -> None:
        self.fail_times = fail_times
        self.return_none = return_none
        self.calls: List[Tuple[str, Optional[str], Optional[List[List[InlineButton]]]]] = []
        self.sent: List[Tuple[str, Optional[str], Optional[List[List[InlineButton]]]]] = []
        self._seq = 0

    def poll_updates(self, timeout: int = 10) -> List[InboundMessage]:
        return []

    def send_message(
        self,
        text: str,
        thread_id: Optional[str] = None,
        buttons: Optional[List[List[InlineButton]]] = None,
    ) -> Optional[str]:
        self.calls.append((text, thread_id, buttons))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("transient network error 502")
        if self.return_none:
            return None
        self._seq += 1
        self.sent.append((text, thread_id, buttons))
        return f"msg-{self._seq}"

    def create_thread(self, title: str) -> Optional[str]:
        return "thread-1"

    def close_thread(self, thread_id: str) -> bool:
        return True

    def answer_callback(self, callback_id: str, text: str = "") -> bool:
        return True


class TestOutboxResilience(unittest.TestCase):
    def test_tuning_config_and_descriptions(self) -> None:
        t = TuningConfig()
        self.assertEqual(t.quiet_hours, "")
        self.assertEqual(t.outbox_max_retries, 5)
        self.assertEqual(t.outbox_max_per_minute, 12)
        self.assertIn("quiet_hours", TUNING_DESCRIPTIONS)
        self.assertIn("outbox_max_retries", TUNING_DESCRIPTIONS)
        self.assertIn("outbox_max_per_minute", TUNING_DESCRIPTIONS)

        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            self.assertEqual(hq_main(["init", str(hq_dir)]), 0)
            cfg = load_config(hq_dir)
            self.assertEqual(cfg.tuning.quiet_hours, "")
            self.assertEqual(cfg.tuning.outbox_max_retries, 5)
            self.assertEqual(cfg.tuning.outbox_max_per_minute, 12)

    def test_is_in_quiet_hours_same_day_overnight_and_edges(self) -> None:
        # Disabled / invalid inputs
        self.assertFalse(is_in_quiet_hours("", _local_ts(23, 30)))
        self.assertFalse(is_in_quiet_hours("   ", _local_ts(23, 30)))
        self.assertFalse(is_in_quiet_hours("invalid", _local_ts(23, 30)))
        self.assertFalse(is_in_quiet_hours("25:00-08:00", _local_ts(2, 0)))
        self.assertFalse(is_in_quiet_hours("12:00-12:00", _local_ts(12, 0)))
        self.assertFalse(is_in_quiet_hours("12:00-14:00-16:00", _local_ts(13, 0)))

        # Same-day window: 12:00-14:00
        self.assertFalse(is_in_quiet_hours("12:00-14:00", _local_ts(11, 59)))
        self.assertTrue(is_in_quiet_hours("12:00-14:00", _local_ts(12, 0)))
        self.assertTrue(is_in_quiet_hours("12:00-14:00", _local_ts(13, 30)))
        self.assertFalse(is_in_quiet_hours("12:00-14:00", _local_ts(14, 0)))
        self.assertFalse(is_in_quiet_hours("12:00-14:00", _local_ts(14, 1)))

        # Overnight window: 23:00-08:00
        self.assertFalse(is_in_quiet_hours("23:00-08:00", _local_ts(22, 59)))
        self.assertTrue(is_in_quiet_hours("23:00-08:00", _local_ts(23, 0)))
        self.assertTrue(is_in_quiet_hours("23:00-08:00", _local_ts(0, 0)))
        self.assertTrue(is_in_quiet_hours("23:00-08:00", _local_ts(7, 59)))
        self.assertFalse(is_in_quiet_hours("23:00-08:00", _local_ts(8, 0)))
        self.assertFalse(is_in_quiet_hours("23:00-08:00", _local_ts(15, 0)))

    def test_quiet_hours_downgrades_notify_but_not_question_or_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            self.assertEqual(hq_main(["init", str(hq_dir)]), 0)
            update_tuning_value(hq_dir, "quiet_hours", "23:00-08:00")

            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            db.create_session(
                session_key="sess-q",
                track="TOPIC",
                agent_backend="stub",
                channel_thread_id="thread-q",
                now=_local_ts(23, 0),
            )
            db.close()

            ch = _FlakyChannel()
            night_ts = _local_ts(2, 30)

            # notify during quiet hours -> downgraded to digest_buffer
            res_notify = deliver_notification(
                hq_dir=hq_dir,
                level="notify",
                message="nightly build finished",
                channel=ch,
                now=night_ts,
            )
            self.assertIsNone(res_notify)
            self.assertEqual(len(ch.sent), 0)

            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            digest_items = db.list_pending_digest_items()
            self.assertEqual(len(digest_items), 1)
            self.assertEqual(digest_items[0]["category"], "quiet_hours_downgrade")
            self.assertEqual(digest_items[0]["message"], "nightly build finished")
            db.close()

            # question & review during quiet hours -> delivered immediately
            res_q = deliver_notification(
                hq_dir=hq_dir,
                level="question",
                message="Need decision on migration?",
                session_key="sess-q",
                options=["Proceed", "Hold"],
                channel=ch,
                now=night_ts,
            )
            res_r = deliver_notification(
                hq_dir=hq_dir,
                level="review",
                message="Approve release PR?",
                session_key="sess-q",
                options=["Approve", "Reject"],
                channel=ch,
                now=night_ts,
            )
            self.assertEqual(res_q, "msg-1")
            self.assertEqual(res_r, "msg-2")
            self.assertEqual(len(ch.sent), 2)

            # notify outside quiet hours -> delivered immediately
            day_ts = _local_ts(14, 0)
            res_day = deliver_notification(
                hq_dir=hq_dir,
                level="notify",
                message="afternoon sync complete",
                channel=ch,
                now=day_ts,
            )
            self.assertEqual(res_day, "msg-3")
            self.assertEqual(len(ch.sent), 3)

    def test_failed_delivery_enqueues_outbox_and_daemon_retries_to_sent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            self.assertEqual(hq_main(["init", str(hq_dir)]), 0)
            base_ts = _local_ts(8, 0)

            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            db.create_session(
                session_key="sess-rev",
                track="TOPIC",
                agent_backend="stub",
                channel_thread_id="thread-rev",
                now=base_ts,
            )
            db.close()

            # Fail initial delivery in notify_cli + 1 daemon retry, then succeed on 2nd daemon retry
            ch = _FlakyChannel(fail_times=2)
            res = deliver_notification(
                hq_dir=hq_dir,
                level="review",
                message="Please review schema diff",
                session_key="sess-rev",
                options=["Approve", "Cancel"],
                channel=ch,
                now=base_ts,
            )
            self.assertIsNone(res)

            daemon = HQDaemon(
                hq_dir,
                agent_adapter=_StubAgent(),
                terminal_driver=_StubTerminal(),
                channel=ch,
            )
            try:
                due = daemon.db.list_due_outbox(now=base_ts)
                self.assertEqual(len(due), 1)
                outbox_id = int(due[0]["id"])
                self.assertEqual(due[0]["status"], "pending")
                self.assertEqual(due[0]["attempts"], 0)
                self.assertIn("transient network error 502", due[0]["last_error"])

                # First daemon drain at base_ts fails -> backoff +10s
                sent1 = daemon._drain_outbox(base_ts)
                self.assertEqual(sent1, 0)
                row1 = daemon.db.get_outbox_message(outbox_id)
                assert row1 is not None
                self.assertEqual(row1["status"], "pending")
                self.assertEqual(row1["attempts"], 1)
                self.assertEqual(row1["next_attempt_at"], base_ts + OUTBOX_BACKOFF_SCHEDULE[0])

                # Too early (base_ts + 5) -> not due yet
                self.assertEqual(daemon._drain_outbox(base_ts + 5), 0)

                # At base_ts + 10 -> succeeds and creates deferred_action
                summary = daemon.run_once(now=base_ts + 10)
                self.assertEqual(summary.outbox_sent, 1)
                row2 = daemon.db.get_outbox_message(outbox_id)
                assert row2 is not None
                self.assertEqual(row2["status"], "sent")
                self.assertEqual(row2["sent_at"], base_ts + 10)
                self.assertEqual(len(ch.sent), 1)

                # Verify deferred_action was created and session suspended_msg_id set
                action = daemon.db.get_deferred_action("msg-1")
                self.assertIsNotNone(action)
                assert action is not None
                self.assertEqual(action["session_key"], "sess-rev")
                self.assertEqual(action["level"], "review")
                self.assertEqual(len(action["options"]), 2)
                sess = daemon.db.get_session("sess-rev")
                assert sess is not None
                self.assertEqual(sess["suspended_msg_id"], "msg-1")
            finally:
                daemon.close()

    def test_outbox_exceeding_max_retries_moves_to_dead_and_alerts_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            self.assertEqual(hq_main(["init", str(hq_dir)]), 0)
            update_tuning_value(hq_dir, "outbox_max_retries", "3")

            base_ts = _local_ts(11, 0)
            # Channel always returns None (failed delivery)
            ch = _FlakyChannel(return_none=True)
            res = deliver_notification(
                hq_dir=hq_dir,
                level="notify",
                message="persistent failure test",
                channel=ch,
                now=base_ts,
            )
            self.assertIsNone(res)

            daemon = HQDaemon(
                hq_dir,
                agent_adapter=_StubAgent(),
                terminal_driver=_StubTerminal(),
                channel=ch,
            )
            try:
                due = daemon.db.list_due_outbox(now=base_ts)
                self.assertEqual(len(due), 1)
                outbox_id = int(due[0]["id"])

                # Attempt 1 -> pending, next +10
                t = base_ts
                self.assertEqual(daemon._drain_outbox(t), 0)
                row = daemon.db.get_outbox_message(outbox_id)
                assert row is not None
                self.assertEqual(row["status"], "pending")
                self.assertEqual(row["attempts"], 1)

                # Attempt 2 -> pending, next +30
                t = int(row["next_attempt_at"])
                self.assertEqual(daemon._drain_outbox(t), 0)
                row = daemon.db.get_outbox_message(outbox_id)
                assert row is not None
                self.assertEqual(row["status"], "pending")
                self.assertEqual(row["attempts"], 2)
                self.assertEqual(row["next_attempt_at"], t + OUTBOX_BACKOFF_SCHEDULE[1])

                # Attempt 3 -> hits max_retries=3 -> dead + digest alert
                t = int(row["next_attempt_at"])
                self.assertEqual(daemon._drain_outbox(t), 0)
                row = daemon.db.get_outbox_message(outbox_id)
                assert row is not None
                self.assertEqual(row["status"], "dead")
                self.assertEqual(row["attempts"], 3)

                alerts = [
                    it
                    for it in daemon.db.list_pending_digest_items()
                    if it["category"] == "outbox_dead"
                ]
                self.assertEqual(len(alerts), 1)
                self.assertEqual(alerts[0]["level"], "alert")
                self.assertIn(f"Outbox #{outbox_id}", alerts[0]["message"])
            finally:
                daemon.close()

    def test_outbox_max_per_minute_rate_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            self.assertEqual(hq_main(["init", str(hq_dir)]), 0)
            update_tuning_value(hq_dir, "outbox_max_per_minute", "2")

            base_ts = _local_ts(15, 0)
            ch = _FlakyChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=_StubAgent(),
                terminal_driver=_StubTerminal(),
                channel=ch,
            )
            try:
                ids = [
                    daemon.db.enqueue_outbox(
                        level="notify",
                        text=f"[NOTIFY] msg {i}",
                        next_attempt_at=base_ts,
                        now=base_ts,
                    )
                    for i in range(4)
                ]

                # First tick sends only 2 due to outbox_max_per_minute=2
                sent_first = daemon._drain_outbox(base_ts)
                self.assertEqual(sent_first, 2)
                self.assertEqual(daemon.db.count_recent_outbox_sent(base_ts - 60), 2)

                # Remaining 2 stay pending with 0 attempts (not treated as errors)
                for oid in ids[2:]:
                    row = daemon.db.get_outbox_message(oid)
                    assert row is not None
                    self.assertEqual(row["status"], "pending")
                    self.assertEqual(row["attempts"], 0)

                # Within the same 60s window, no more non-urgent notifies are sent
                self.assertEqual(daemon._drain_outbox(base_ts + 30), 0)

                # Urgent review still delivers even when non-urgent budget is full
                urgent_id = daemon.db.enqueue_outbox(
                    level="review",
                    text="[REVIEW] urgent check",
                    next_attempt_at=base_ts + 35,
                    now=base_ts + 35,
                )
                self.assertEqual(daemon._drain_outbox(base_ts + 35), 1)
                urgent_row = daemon.db.get_outbox_message(urgent_id)
                assert urgent_row is not None
                self.assertEqual(urgent_row["status"], "sent")

                # After the 60s window passes, the remaining 2 notifies are sent
                sent_next = daemon._drain_outbox(base_ts + 100)
                self.assertEqual(sent_next, 2)
                for oid in ids:
                    row = daemon.db.get_outbox_message(oid)
                    assert row is not None
                    self.assertEqual(row["status"], "sent")
            finally:
                daemon.close()


if __name__ == "__main__":
    unittest.main()
