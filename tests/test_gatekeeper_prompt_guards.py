"""Tests for gatekeeper commit scanning, prompt_builder determinism, memory_guard, and oss_guard."""

import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from hq_pilot.cli import main as hq_main
from hq_pilot.config import load_config
from hq_pilot.cursors import CursorStore
from hq_pilot.db import HQDatabase
from hq_pilot.gatekeeper import scan_repo_commits
from hq_pilot.guards import run_memory_guard, run_oss_guard
from hq_pilot.prompt_builder import build_prompt
from test_archiver_resume_cursors import _init_git_repo


class TestGatekeeperPromptGuards(unittest.TestCase):
    def test_gatekeeper_skips_unmatched_and_enqueues_matched_with_debounce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            repo_dir = Path(tmp) / "gateway-svc"
            _init_git_repo(repo_dir)
            (hq_dir / ".hq" / "local.yaml").write_text(
                f"repo_paths:\n  gateway-svc: {repo_dir}\n", encoding="utf-8"
            )

            cfg = load_config(hq_dir)
            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            cursors = CursorStore(hq_dir / ".hq" / "cursors.json")
            try:
                # First scan initializes git_heads cursor
                self.assertEqual(scan_repo_commits(cfg, cursors, db, now=1000), [])

                # Commit to ignored path (docs/notes.md) -> 0-token skip, cursor advances
                (repo_dir / "docs").mkdir()
                (repo_dir / "docs" / "notes.md").write_text("doc\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True)
                subprocess.run(["git", "commit", "-m", "docs"], cwd=str(repo_dir), check=True, capture_output=True)
                self.assertEqual(scan_repo_commits(cfg, cursors, db, now=1100), [])

                # Commit to watched path (api/router.py)
                (repo_dir / "api").mkdir()
                (repo_dir / "api" / "router.py").write_text("def route(): pass\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True)
                subprocess.run(["git", "commit", "-m", "api"], cwd=str(repo_dir), check=True, capture_output=True)

                # If dirty uncommitted file modified recently -> quiet rule keeps silent!
                scratch = repo_dir / "scratch.tmp"
                scratch.write_text("editing", encoding="utf-8")
                now_ts = int(time.time())
                os.utime(scratch, (now_ts, now_ts))
                self.assertEqual(scan_repo_commits(cfg, cursors, db, now=now_ts), [])
                scratch.unlink()

                # Clean repo -> enqueues debounced git_commit event
                enq = scan_repo_commits(cfg, cursors, db, now=now_ts)
                self.assertEqual(enq, ["gateway-svc"])
                # Not claimable before debounce window
                self.assertEqual(db.claim_next_events(now=now_ts + 10), [])
                # Claimable after commit_debounce_sec (300s)
                claimed = db.claim_next_events(now=now_ts + 305)
                self.assertEqual(len(claimed), 1)
                self.assertEqual(claimed[0].source, "git_commit")
            finally:
                db.close()

    def test_prompt_builder_byte_determinism_and_section_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            (hq_dir / "memory" / "contracts" / "b-contract.md").write_text("B contract\n", encoding="utf-8")
            (hq_dir / "memory" / "contracts" / "gateway-worker.md").write_text("GW contract\n", encoding="utf-8")
            cfg = load_config(hq_dir)
            payload = {"b": 2, "a": 1}

            out1 = build_prompt(hq_dir, cfg, payload)
            out2 = build_prompt(hq_dir, cfg, {"a": 1, "b": 2})
            self.assertEqual(out1, out2)

            i1 = out1.index("## 1. Repo Boundaries")
            i2 = out1.index("## 2. Relevant Contracts")
            i3 = out1.index("## 3. Active WIP")
            i4 = out1.index("## 4. Today WAL")
            i5 = out1.index("## 5. Event Payload")
            self.assertTrue(i1 < i2 < i3 < i4 < i5)
            # Alphabetical order of contracts in Section 2
            sec2 = out1[i2:i3]
            self.assertLess(sec2.index("b-contract.md"), sec2.index("gateway-worker.md"))

    def test_memory_guard_and_oss_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            repo_dir = Path(tmp) / "gateway-svc"
            (repo_dir / "api").mkdir(parents=True)
            (repo_dir / "api" / "router.py").write_text("# ok\n", encoding="utf-8")
            (hq_dir / ".hq" / "local.yaml").write_text(
                f"repo_paths:\n  gateway-svc: {repo_dir}\n", encoding="utf-8"
            )
            # Template AGENTS.example.md references worker-svc:jobs/runner.py too
            worker_dir = Path(tmp) / "worker-svc"
            (worker_dir / "jobs").mkdir(parents=True)
            (worker_dir / "jobs" / "runner.py").write_text("# ok\n", encoding="utf-8")
            (hq_dir / ".hq" / "local.yaml").write_text(
                f"repo_paths:\n  gateway-svc: {repo_dir}\n  worker-svc: {worker_dir}\n",
                encoding="utf-8",
            )
            res_ok = run_memory_guard(hq_dir)
            self.assertTrue(res_ok.ok, msg=res_ok.violations)

            # Broken logical path triggers failure
            (hq_dir / "notes" / "today.md").write_text("See `gateway-svc:api/missing.py`\n", encoding="utf-8")
            res_bad = run_memory_guard(hq_dir)
            self.assertFalse(res_bad.ok)
            self.assertTrue(any("missing.py" in v for v in res_bad.violations))

            # OSS guard catches synthetic private IP and forbidden term
            bad_repo = Path(tmp) / "bad-oss"
            bad_repo.mkdir()
            (bad_repo / "leak.txt").write_text(
                "host = 192.168." + "1.50\nbrand = one" + "deck\n",
                encoding="utf-8",
            )
            oss_bad = run_oss_guard(bad_repo, check_git_history=False)
            self.assertFalse(oss_bad.ok)
            self.assertGreaterEqual(len(oss_bad.violations), 2)

        # Verify actual hq-pilot repo passes oss_guard cleanly
        engine_root = Path(__file__).resolve().parent.parent
        oss_self = run_oss_guard(engine_root, check_git_history=False)
        self.assertTrue(oss_self.ok, msg=oss_self.violations)


if __name__ == "__main__":
    unittest.main()
