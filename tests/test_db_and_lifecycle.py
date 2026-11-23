"""Tests for SQLite event leases, 4-state session lifecycle, and manual-touch pinning."""

from pathlib import Path
import tempfile
import unittest

from hq_pilot.cli import main as hq_main
from hq_pilot.db import HQDatabase
from hq_pilot.gatekeeper import detect_manual_touch
from hq_pilot.hook_cli import handle_hook_event
from hq_pilot.terminals.base import PaneInfo, TerminalDriver


class FakeTerminalDriver(TerminalDriver):
    name = "fake-term"

    def __init__(self, attached_panes=None):
        self.attached_panes = set(attached_panes or [])
        self.killed = []

    def run_bg(self, name, cwd, cmd):
        pass

    def list_panes(self):
        return [PaneInfo(name=p, attached=(p in self.attached_panes)) for p in self.attached_panes]

    def send_text(self, name, text, enter=True):
        pass

    def read_pane(self, name, lines=80):
        return ""

    def attach_cmd(self, name):
        return ["fake-attach", name]

    def kill_pane(self, name):
        self.killed.append(name)

    def is_client_attached(self, name):
        return name in self.attached_panes


class TestDBAndLifecycle(unittest.TestCase):
    def test_event_lease_reclaim_and_dead_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = HQDatabase(Path(tmp) / "runtime.sqlite")
            try:
                eid = db.enqueue_event("git_commit", "k1", 20, {"repo": "gateway-svc"}, now=1000)
                self.assertIsNotNone(eid)
                # Duplicate dedupe_key ignored
                self.assertIsNone(db.enqueue_event("git_commit", "k1", 20, {"repo": "gateway-svc"}, now=1001))

                claimed = db.claim_next_events(limit=5, now=1000, lease_ttl_sec=900)
                self.assertEqual(len(claimed), 1)
                self.assertEqual(claimed[0].attempts, 1)

                # Not yet expired at 1500
                self.assertEqual(db.claim_next_events(limit=5, now=1500, lease_ttl_sec=900), [])

                # Expired at 1900 -> reclaimed
                reclaimed = db.claim_next_events(limit=5, now=1900, lease_ttl_sec=900)
                self.assertEqual(len(reclaimed), 1)
                self.assertEqual(reclaimed[0].attempts, 2)

                # Fail attempt 2 -> backoff pending
                st1 = db.fail_event(eid, max_attempts=3, now=1900)
                self.assertEqual(st1, "pending")

                # Claim attempt 3 after backoff, then fail -> dead + digest alert
                row = db.get_event(eid)
                claimed3 = db.claim_next_events(limit=5, now=int(row["available_at"]), lease_ttl_sec=900)
                self.assertEqual(len(claimed3), 1)
                st2 = db.fail_event(eid, max_attempts=3, now=int(row["available_at"]))
                self.assertEqual(st2, "dead")

                alerts = db.list_pending_digest_items()
                self.assertEqual(len(alerts), 1)
                self.assertEqual(alerts[0]["category"], "dead_event")
            finally:
                db.close()

    def test_manual_input_fingerprint_and_terminal_attach_pin_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                sess = db.create_session(
                    session_key="thread:42:100",
                    track="C",
                    agent_backend="agy",
                    status="active",
                    pane_name="pane-42",
                    agent_session_id="conv-42",
                    now=100,
                )
                db.record_dispatch("thread:42:100", "conv-42", "Daemon prompt text", now=100)

                # Daemon input -> NOT pinned
                res_daemon = handle_hook_event(
                    "agy",
                    {
                        "hook_event_name": "PreInvocation",
                        "conversation_id": "conv-42",
                        "prompt": "Daemon   prompt   text",
                    },
                    hq_dir=hq_dir,
                    now=105,
                )
                self.assertIsNone(res_daemon["pinned_reason"])
                self.assertEqual(db.get_session("thread:42:100")["status"], "active")

                # Non-daemon manual input -> auto-pinned!
                res_manual = handle_hook_event(
                    "agy",
                    {
                        "hook_event_name": "PreInvocation",
                        "conversation_id": "conv-42",
                        "prompt": "Human typed this directly in terminal",
                    },
                    hq_dir=hq_dir,
                    now=110,
                )
                self.assertEqual(res_manual["pinned_reason"], "manual_input")
                s_after = db.get_session("thread:42:100")
                self.assertEqual(s_after["status"], "pinned")
                self.assertEqual(s_after["pinned"], 1)

                # Local direct session -> defaults to pinned
                res_local = handle_hook_event(
                    "agy",
                    {
                        "hook_event_name": "SessionStart",
                        "conversation_id": "conv-local-99",
                    },
                    hq_dir=hq_dir,
                    now=120,
                )
                self.assertEqual(res_local["session_key"], "local:conv-local-99")
                local_sess = db.get_session("local:conv-local-99")
                self.assertEqual(local_sess["status"], "pinned")
                self.assertEqual(local_sess["pinned_reason"], "local_direct")

                # Terminal attach -> auto-pinned
                s2 = db.create_session(
                    session_key="thread:55:100",
                    track="C",
                    agent_backend="agy",
                    status="active",
                    pane_name="pane-55",
                    now=100,
                )
                term = FakeTerminalDriver(attached_panes={"pane-55"})
                reason = detect_manual_touch(db, s2, terminal_driver=term, now=130)
                self.assertEqual(reason, "terminal_attach")
                self.assertEqual(db.get_session("thread:55:100")["status"], "pinned")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
