"""Tests for agent self-scheduled follow-up timers (wake_me_at / cancel_wake)."""

from __future__ import annotations

from datetime import datetime
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hq_pilot.cli import main as hq_main, parse_duration_seconds, parse_fire_at
from hq_pilot.config import load_config
from hq_pilot.daemon import GENERAL_PANE_NAME, HQDaemon
from hq_pilot.db import HQDatabase
from hq_pilot.prompt_builder import build_prompt

try:
    from tests.test_adapters_and_channels import FakeChannel
    from tests.test_herdr_interactive_mode import NoHeadlessAgyAdapter
    from tests.test_review_fixes import RecordingTerminal
except ModuleNotFoundError:
    from test_adapters_and_channels import FakeChannel
    from test_herdr_interactive_mode import NoHeadlessAgyAdapter
    from test_review_fixes import RecordingTerminal


def _init_hq_workspace(root: Path) -> Path:
    hq_dir = root / "hq"
    rc = hq_main(["init", str(hq_dir)])
    assert rc == 0
    return hq_dir


class TestTimersSelfWake(unittest.TestCase):
    def test_duration_and_time_parsers(self) -> None:
        self.assertEqual(parse_duration_seconds("30s"), 30)
        self.assertEqual(parse_duration_seconds("15m"), 900)
        self.assertEqual(parse_duration_seconds("2h"), 7200)
        self.assertEqual(parse_duration_seconds("1d"), 86400)
        self.assertEqual(parse_duration_seconds("1h30m"), 5400)

        with self.assertRaises(ValueError):
            parse_duration_seconds("0m")
        with self.assertRaises(ValueError):
            parse_duration_seconds("abc")

        self.assertEqual(parse_fire_at("1700000000", now=1699999000), 1700000000)
        dt_ts = parse_fire_at("2026-05-10 14:30")
        self.assertEqual(
            datetime.fromtimestamp(dt_ts).strftime("%Y-%m-%d %H:%M"),
            "2026-05-10 14:30",
        )

        base_ts = int(datetime(2026, 5, 10, 10, 0, 0).timestamp())
        same_day = parse_fire_at("10:30", now=base_ts)
        self.assertEqual(same_day - base_ts, 1800)
        next_day = parse_fire_at("09:30", now=base_ts)
        self.assertEqual(next_day - base_ts, 23 * 3600 + 1800)

    def test_once_timer_fires_atomically_wakes_topic_and_does_not_refire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hq_dir = _init_hq_workspace(root)
            wt_dir = root / "wt-ci"
            wt_dir.mkdir(parents=True, exist_ok=True)
            (hq_dir / "tasks" / "ci-fix.md").write_text(
                "# Task: ci-fix\n\n- status: active\n\n## Done\n- triggered build\n",
                encoding="utf-8",
            )

            term = RecordingTerminal(reply_fn=lambda _n, _t: "Build passed; ready to seal.")
            chan = FakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=chan,
            )
            try:
                base_ts = int(datetime(2026, 5, 10, 8, 0, 0).timestamp())
                sess = daemon.db.create_session(
                    session_key="task:demo:ci-fix:1",
                    track="TOPIC",
                    agent_backend="agy",
                    status="active",
                    channel_thread_id="topic-42",
                    topic_slug="ci-fix",
                    worktree_path=str(wt_dir),
                    pane_name="hq-ci-fix",
                    now=base_ts,
                )
                daemon.db.add_local_message(
                    direction="outbound",
                    text="Already notified user about build start.",
                    thread_id="topic-42",
                    now=base_ts,
                )

                timer = daemon.db.create_timer(
                    reason="Check CI build result",
                    fire_at=base_ts + 900,
                    kind="once",
                    session_key=sess["session_key"],
                    name="ci-followup",
                    now=base_ts,
                )
                self.assertEqual(timer["status"], "active")

                # Not due yet at +899s
                s_early = daemon.run_once(now=base_ts + 899)
                self.assertEqual(s_early.timers_fired, 0)
                self.assertEqual(s_early.events_processed, 0)
                self.assertEqual(len(term.prompt_calls), 0)

                # Due at +900s: fires atomically, wakes topic pane, transitions to fired
                s_due = daemon.run_once(now=base_ts + 900)
                self.assertEqual(s_due.timers_fired, 1)
                self.assertEqual(s_due.events_processed, 1)

                updated = daemon.db.get_timer(int(timer["id"]))
                self.assertIsNotNone(updated)
                self.assertEqual(updated["status"], "fired")
                self.assertEqual(updated["fired_count"], 1)
                self.assertEqual(updated["last_fired_at"], base_ts + 900)

                # Interactive dispatch targeted the Topic pane with topic progress & recent outbound
                self.assertEqual(len(term.prompt_calls), 1)
                call = term.prompt_calls[0]
                self.assertEqual(call["name"], "hq-ci-fix")
                self.assertTrue(call["wait"])
                sent_prompt = call["text"]
                self.assertIn("Check CI build result", sent_prompt)
                self.assertIn("## 3.5 Task Progress (ci-fix)", sent_prompt)
                self.assertIn("Already notified user about build start.", sent_prompt)

                # Reply pushed to the Topic thread
                self.assertEqual(len(chan.sent), 1)
                self.assertEqual(chan.sent[0]["thread_id"], "topic-42")
                self.assertIn("Build passed", chan.sent[0]["text"])

                # Repeating ticks must NOT fire again
                s_rep1 = daemon.run_once(now=base_ts + 900)
                s_rep2 = daemon.run_once(now=base_ts + 960)
                self.assertEqual(s_rep1.timers_fired, 0)
                self.assertEqual(s_rep2.timers_fired, 0)
                self.assertEqual(len(term.prompt_calls), 1)
            finally:
                daemon.close()

    def test_timer_wake_falls_back_to_general_when_session_sealed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = _init_hq_workspace(Path(tmp))
            term = RecordingTerminal(reply_fn=lambda _n, _t: "General handled follow-up.")
            chan = FakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=chan,
            )
            try:
                base_ts = int(datetime(2026, 5, 10, 8, 0, 0).timestamp())
                daemon.db.create_session(
                    session_key="task:demo:old:1",
                    track="TOPIC",
                    agent_backend="agy",
                    status="sealed",
                    channel_thread_id="topic-99",
                    topic_slug="old",
                    pane_name="hq-old",
                    now=base_ts,
                )
                daemon.db.create_timer(
                    reason="Follow up sealed topic",
                    fire_at=base_ts + 60,
                    kind="once",
                    session_key="task:demo:old:1",
                    now=base_ts,
                )
                summary = daemon.run_once(now=base_ts + 60)
                self.assertEqual(summary.timers_fired, 1)
                self.assertEqual(summary.events_processed, 1)
                self.assertEqual(len(term.prompt_calls), 1)
                self.assertEqual(term.prompt_calls[0]["name"], GENERAL_PANE_NAME)
                self.assertEqual(len(chan.sent), 1)
                self.assertIsNone(chan.sent[0]["thread_id"])
            finally:
                daemon.close()

    def test_recurring_timer_drift_free_grid_skips_downtime_without_avalanche(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = HQDatabase(Path(tmp) / "runtime.sqlite")
            try:
                anchor = 1_750_020_000
                step = 600  # 10m
                timer = db.create_timer(
                    reason="Periodic health check",
                    fire_at=anchor,
                    kind="recurring",
                    every_sec=step,
                    now=anchor - 60,
                )
                tid = int(timer["id"])

                # Fire 5 seconds late -> next_fire_at stays on anchor grid (anchor + 600, NOT + 605)
                fired1 = db.fire_due_timers(now=anchor + 5)
                self.assertEqual(len(fired1), 1)
                self.assertEqual(fired1[0]["fired_count"], 1)
                self.assertEqual(fired1[0]["next_fire_at"], anchor + step)
                self.assertEqual(db.get_timer(tid)["next_fire_at"], anchor + step)

                # Simulate downtime skipping 3 intervals (anchor+600, +1200, +1800)
                # At anchor+1900, fires ONCE (no avalanche) and jumps to anchor+2400
                fired2 = db.fire_due_timers(now=anchor + 1900)
                self.assertEqual(len(fired2), 1)
                self.assertEqual(fired2[0]["fired_count"], 2)
                self.assertEqual(fired2[0]["next_fire_at"], anchor + 4 * step)

                # Calling again right after does not double fire
                self.assertEqual(db.fire_due_timers(now=anchor + 1900), [])
                self.assertEqual(db.fire_due_timers(now=anchor + 4 * step - 1), [])

                # Fires cleanly on next grid slot
                fired3 = db.fire_due_timers(now=anchor + 4 * step)
                self.assertEqual(len(fired3), 1)
                self.assertEqual(fired3[0]["fired_count"], 3)
                self.assertEqual(fired3[0]["next_fire_at"], anchor + 5 * step)
            finally:
                db.close()

    def test_cli_timer_set_list_cancel_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = _init_hq_workspace(Path(tmp))

            # Set one-shot timer picking up HQ_SESSION_KEY from environment
            buf_set = io.StringIO()
            with patch.dict(os.environ, {"HQ_SESSION_KEY": "task:demo:deploy:7"}), patch(
                "sys.stdout", buf_set
            ):
                rc1 = hq_main(
                    [
                        "timer",
                        "set",
                        "--in",
                        "15m",
                        "--reason",
                        "Verify deployment rollout",
                        "--name",
                        "rollout-check",
                        "--hq-dir",
                        str(hq_dir),
                    ]
                )
            self.assertEqual(rc1, 0)
            self.assertIn("Scheduled timer #1", buf_set.getvalue())
            self.assertIn("session=task:demo:deploy:7", buf_set.getvalue())

            # Set recurring timer
            buf_rec = io.StringIO()
            with patch("sys.stdout", buf_rec):
                rc2 = hq_main(
                    [
                        "timer",
                        "set",
                        "--every",
                        "10m",
                        "--reason",
                        "Poll queue depth",
                        "--hq-dir",
                        str(hq_dir),
                    ]
                )
            self.assertEqual(rc2, 0)
            self.assertIn("kind=recurring", buf_rec.getvalue())
            self.assertIn("every=600s", buf_rec.getvalue())

            # Reject mutually exclusive --in and --at
            buf_err = io.StringIO()
            with patch("sys.stderr", buf_err):
                rc_bad = hq_main(
                    [
                        "timer",
                        "set",
                        "--in",
                        "5m",
                        "--at",
                        "12:00",
                        "--reason",
                        "bad",
                        "--hq-dir",
                        str(hq_dir),
                    ]
                )
            self.assertEqual(rc_bad, 2)

            # List active timers
            buf_list = io.StringIO()
            with patch("sys.stdout", buf_list):
                rc_list = hq_main(["timer", "list", "--hq-dir", str(hq_dir)])
            self.assertEqual(rc_list, 0)
            self.assertIn("#1", buf_list.getvalue())
            self.assertIn("#2", buf_list.getvalue())

            # Cancel timer #1
            buf_cancel = io.StringIO()
            with patch("sys.stdout", buf_cancel):
                rc_cancel = hq_main(["timer", "cancel", "1", "--hq-dir", str(hq_dir)])
            self.assertEqual(rc_cancel, 0)
            self.assertIn("Cancelled timer #1", buf_cancel.getvalue())

            # Active list excludes #1; --all includes #1 as cancelled
            buf_active = io.StringIO()
            with patch("sys.stdout", buf_active):
                hq_main(["timer", "list", "--hq-dir", str(hq_dir)])
            self.assertNotIn("#1", buf_active.getvalue())
            self.assertIn("#2", buf_active.getvalue())

            buf_all = io.StringIO()
            with patch("sys.stdout", buf_all):
                hq_main(["timer", "list", "--all", "--hq-dir", str(hq_dir)])
            self.assertIn("#1 | status=cancelled", buf_all.getvalue())

            # Cancelled timer #1 must never fire even far in the future
            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                t1 = db.get_timer(1)
                fired = db.fire_due_timers(now=int(t1["next_fire_at"]) + 10_000)
                fired_ids = [int(f["id"]) for f in fired]
                self.assertNotIn(1, fired_ids)
                self.assertIn(2, fired_ids)
            finally:
                db.close()

    def test_timer_wake_uses_interactive_dispatch_zero_headless_and_prompt_guide(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = _init_hq_workspace(Path(tmp))
            cfg = load_config(hq_dir)
            prompt = build_prompt(hq_dir, cfg, {"text": "hi"})
            self.assertIn("herdr-bot timer set --in 15m --reason", prompt)
            agents_md = (hq_dir / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("herdr-bot timer set --in 15m --reason", agents_md)

            def forbid_cmd_runner(*args, **kwargs):
                raise AssertionError("cmd_runner / headless -p must never be called for timer_wake")

            term = RecordingTerminal(reply_fn=lambda _n, _t: "Interactive wake reply")
            chan = FakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=chan,
                cmd_runner=forbid_cmd_runner,
            )
            try:
                now = int(datetime(2026, 5, 10, 8, 0, 0).timestamp())
                daemon.db.create_timer(
                    reason="Pure interactive wake check",
                    fire_at=now,
                    kind="once",
                    now=now - 10,
                )
                summary = daemon.run_once(now=now)
                self.assertEqual(summary.timers_fired, 1)
                self.assertEqual(summary.events_processed, 1)
                self.assertEqual(len(term.start_agent_calls), 1)
                self.assertEqual(len(term.prompt_calls), 1)
                self.assertEqual(len(chan.sent), 1)
                self.assertEqual(chan.sent[0]["text"], "Interactive wake reply")
            finally:
                daemon.close()


if __name__ == "__main__":
    unittest.main()
