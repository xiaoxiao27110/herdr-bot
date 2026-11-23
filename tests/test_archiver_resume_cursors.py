"""Tests for nightly safe archiver, hq resume worktree resurrection, and decoupled cursors."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from hq_pilot.agents.agy import AgyAdapter
from hq_pilot.archiver import archive_eligible_sessions
from hq_pilot.cli import main as hq_main
from hq_pilot.config import load_config
from hq_pilot.cursors import CursorStore
from hq_pilot.daemon import HQDaemon
from hq_pilot.db import HQDatabase
from hq_pilot.gatekeeper import scan_distill_candidates
from hq_pilot.terminals.base import git_worktree_add
from test_adapters_and_channels import DummyTerminal, FakeChannel


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_dir), check=True)
    (repo_dir / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)


class TestArchiverResumeCursors(unittest.TestCase):
    def test_dirty_worktree_and_unpushed_commits_blocked_while_clean_sealed_archived_and_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            repo_dir = Path(tmp) / "gateway-svc"
            _init_git_repo(repo_dir)
            (hq_dir / ".hq" / "local.yaml").write_text(
                f"repo_paths:\n  gateway-svc: {repo_dir}\n", encoding="utf-8"
            )

            wt_dirty = hq_dir / ".hq" / "worktrees" / "wt-dirty"
            wt_unpushed = hq_dir / ".hq" / "worktrees" / "wt-unpushed"
            wt_clean = hq_dir / ".hq" / "worktrees" / "wt-clean"
            wt_pinned = hq_dir / ".hq" / "worktrees" / "wt-pinned"

            git_worktree_add(repo_dir, wt_dirty, "feat/dirty", "main")
            git_worktree_add(repo_dir, wt_unpushed, "feat/unpushed", "main")
            git_worktree_add(repo_dir, wt_clean, "feat/clean", "main")
            git_worktree_add(repo_dir, wt_pinned, "feat/pinned", "main")

            # Make wt_dirty dirty
            (wt_dirty / "untracked.txt").write_text("wip\n", encoding="utf-8")
            # Make wt_unpushed have a local unpushed commit
            (wt_unpushed / "local.txt").write_text("commit\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=str(wt_unpushed), check=True)
            subprocess.run(["git", "commit", "-m", "local"], cwd=str(wt_unpushed), check=True, capture_output=True)

            cfg = load_config(hq_dir)
            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            ch = FakeChannel()
            try:
                db.create_session(
                    "s-dirty", "C", "agy", status="sealed",
                    repo_name="gateway-svc", branch_name="feat/dirty",
                    worktree_path=str(wt_dirty), now=100,
                )
                db.create_session(
                    "s-unpushed", "C", "agy", status="sealed",
                    repo_name="gateway-svc", branch_name="feat/unpushed",
                    worktree_path=str(wt_unpushed), now=100,
                )
                db.create_session(
                    "s-clean", "C", "agy", status="sealed",
                    channel_thread_id="901",
                    repo_name="gateway-svc", branch_name="feat/clean",
                    worktree_path=str(wt_clean), agent_session_id="conv-clean", now=100,
                )
                db.create_session(
                    "s-pinned", "C", "agy", status="pinned", pinned=True,
                    pinned_reason="manual_input",
                    repo_name="gateway-svc", branch_name="feat/pinned",
                    worktree_path=str(wt_pinned), now=100,
                )

                report = archive_eligible_sessions(
                    cfg, db, terminal_driver=DummyTerminal(), channel=ch, now=200
                )
                self.assertEqual(report.archived, ["s-clean"])
                self.assertEqual(set(report.blocked_dirty), {"s-dirty", "s-unpushed"})

                # Dirty & unpushed worktrees MUST still exist
                self.assertTrue(wt_dirty.exists())
                self.assertTrue(wt_unpushed.exists())
                self.assertEqual(db.get_session("s-dirty")["archive_blocked_reason"], "dirty_or_unpushed")
                self.assertEqual(db.get_session("s-unpushed")["archive_blocked_reason"], "dirty_or_unpushed")

                # Digest buffer has red alerts for dirty & unpushed
                alerts = db.list_pending_digest_items()
                self.assertEqual(len(alerts), 2)

                # Pinned session untouched
                self.assertTrue(wt_pinned.exists())
                self.assertEqual(db.get_session("s-pinned")["status"], "pinned")

                # Clean session archived, worktree removed, receipt sent, thread closed
                self.assertFalse(wt_clean.exists())
                self.assertEqual(db.get_session("s-clean")["status"], "archived")
                self.assertIn("hq resume s-clean", ch.sent[-1]["text"])
                self.assertIn("901", ch.closed_threads)
            finally:
                db.close()

            # Now test `hq resume s-clean --dry-run` rebuilds worktree and promotes to pinned
            rc = hq_main(["resume", "s-clean", "--hq-dir", str(hq_dir), "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertTrue(wt_clean.exists())
            db2 = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                resumed = db2.get_session("s-clean")
                self.assertEqual(resumed["status"], "pinned")
                self.assertEqual(resumed["pinned_reason"], "resumed")
            finally:
                db2.close()

    def test_decoupled_cursors_incremental_distill_across_all_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            t_active = Path(tmp) / "t_active.jsonl"
            t_pinned = Path(tmp) / "t_pinned.jsonl"
            t_sealed = Path(tmp) / "t_sealed.jsonl"

            for tf in (t_active, t_pinned, t_sealed):
                tf.write_text(
                    "\n".join([
                        json.dumps({"role": "user", "content": "turn 1"}),
                        json.dumps({"role": "assistant", "content": "ans 1"}),
                        json.dumps({"role": "user", "content": "turn 2"}),
                        json.dumps({"role": "assistant", "content": "ans 2"}),
                    ]) + "\n",
                    encoding="utf-8",
                )

            def fake_runner(cmd, cwd):
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps({"reply": "[DECISION] distilled slice", "sid": "d-1"}), stderr=""
                )

            daemon = HQDaemon(
                hq_dir,
                agent_adapter=AgyAdapter(),
                terminal_driver=DummyTerminal(),
                channel=FakeChannel(),
                cmd_runner=fake_runner,
            )
            try:
                daemon.db.create_session(
                    "s-act", "C", "agy", status="active",
                    agent_session_id="conv-act", transcript_path=str(t_active), now=100,
                )
                daemon.db.create_session(
                    "s-pin", "C", "agy", status="pinned", pinned=True,
                    agent_session_id="conv-pin", transcript_path=str(t_pinned), now=100,
                )
                daemon.db.create_session(
                    "s-sea", "C", "agy", status="sealed",
                    agent_session_id="conv-sea", transcript_path=str(t_sealed), now=100,
                )

                # Run after 25 minutes idle -> all 3 states distilled once
                now_later = 100 + 25 * 60
                summary = daemon.run_once(now=now_later)
                self.assertEqual(summary.distill_enqueued, 3)
                self.assertEqual(summary.events_processed, 3)
                self.assertEqual(daemon.cursors.get_transcript_cursor("conv-act")["last_summarized_line"], 4)
                self.assertEqual(daemon.cursors.get_transcript_cursor("conv-pin")["last_summarized_line"], 4)
                self.assertEqual(daemon.cursors.get_transcript_cursor("conv-sea")["last_summarized_line"], 4)

                # Re-running with no new lines -> 0 distilled (no duplicate reading)
                enq2 = scan_distill_candidates(
                    daemon.db, daemon.cursors, daemon.agent_adapter, daemon.config, now=now_later + 60
                )
                self.assertEqual(enq2, [])

                # Append 1 new turn to pinned session -> nightly Step 0 force_all catches it
                with t_pinned.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"role": "user", "content": "late night fix"}) + "\n")
                daemon.run_nightly_pipeline(now=now_later + 120)
                self.assertEqual(daemon.cursors.get_transcript_cursor("conv-pin")["last_summarized_line"], 5)
                # Pinned session remains pinned after nightly pipeline
                self.assertEqual(daemon.db.get_session("s-pin")["status"], "pinned")
            finally:
                daemon.close()


if __name__ == "__main__":
    unittest.main()
