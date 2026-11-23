"""Tests for TuningConfig, hq config list/set, hot-reload, and logical path resolution."""

from pathlib import Path
import tempfile
import time
import unittest

from hq_pilot.cli import main as hq_main
from hq_pilot.config import TuningConfig, load_config, update_tuning_value


class TestConfigAndTuning(unittest.TestCase):
    def test_tuning_defaults_match_spec(self) -> None:
        t = TuningConfig()
        self.assertEqual(t.index_max_lines, 200)
        self.assertEqual(t.index_max_bytes, 25600)
        self.assertEqual(t.today_wal_max_lines, 60)
        self.assertEqual(t.dream_cron, "23:30")
        self.assertEqual(t.dream_min_interval_hours, 24)
        self.assertEqual(t.dream_min_sessions, 5)
        self.assertTrue(t.dream_force_nightly)
        self.assertEqual(t.digest_cron, "09:30")
        self.assertEqual(t.inbox_lease_ttl_sec, 900)
        self.assertEqual(t.inbox_max_attempts, 3)
        self.assertEqual(t.commit_debounce_sec, 300)
        self.assertEqual(t.active_repo_quiet_minutes, 15)
        self.assertEqual(t.pane_blocked_notify_sec, 60)
        self.assertEqual(t.deferred_approval_ttl_hours, 24)
        self.assertEqual(t.general_idle_seal_minutes, 60)
        self.assertEqual(t.general_max_turns, 20)
        self.assertEqual(t.distill_idle_minutes, 20)
        self.assertEqual(t.distill_min_new_turns, 3)
        self.assertEqual(t.dirty_worktree_warn_hours, 48)
        self.assertEqual(t.git_sync_debounce_minutes, 120)

    def test_cli_config_list_and_set_and_hot_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            self.assertEqual(hq_main(["init", str(hq_dir)]), 0)
            self.assertEqual(hq_main(["config", "list", "--hq-dir", str(hq_dir)]), 0)

            cfg = load_config(hq_dir)
            self.assertEqual(cfg.tuning.commit_debounce_sec, 300)
            self.assertTrue(cfg.tuning.dream_force_nightly)

            # Ensure mtime advances deterministically
            time.sleep(0.02)
            self.assertEqual(
                hq_main(["config", "set", "commit_debounce_sec", "120", "--hq-dir", str(hq_dir)]),
                0,
            )
            self.assertEqual(
                hq_main(["config", "set", "dream_force_nightly", "false", "--hq-dir", str(hq_dir)]),
                0,
            )
            self.assertTrue(cfg.reload_if_changed())
            self.assertEqual(cfg.tuning.commit_debounce_sec, 120)
            self.assertFalse(cfg.tuning.dream_force_nightly)

            with self.assertRaises(ValueError):
                update_tuning_value(hq_dir, "commit_debounce_sec", "not-an-int")
            with self.assertRaises(KeyError):
                update_tuning_value(hq_dir, "nonexistent_key", "1")

    def test_logical_path_resolution_with_local_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            cfg = load_config(hq_dir)
            default_target = cfg.resolve_logical_path("gateway-svc:api/router.py")
            self.assertEqual(
                default_target,
                (hq_dir / ".hq" / "repos" / "gateway-svc" / "api" / "router.py").resolve(),
            )

            override_dir = Path(tmp) / "custom-gateway"
            override_dir.mkdir(parents=True)
            (hq_dir / ".hq" / "local.yaml").write_text(
                f"repo_paths:\n  gateway-svc: {override_dir}\n",
                encoding="utf-8",
            )
            cfg.reload_if_changed()
            self.assertEqual(
                cfg.resolve_logical_path("gateway-svc:api/router.py"),
                (override_dir / "api" / "router.py").resolve(),
            )


if __name__ == "__main__":
    unittest.main()
