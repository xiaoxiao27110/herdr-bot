"""Tests for herdr-bot auto-topic lifecycle, seal CLI, setup wizard, and installer."""

from __future__ import annotations

import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

from hq_pilot.agents.agy import AgyAdapter
from hq_pilot.cli import build_parser, main as hq_main
from hq_pilot.config import load_config, load_secrets_env, update_tuning_value
from hq_pilot.daemon import HQDaemon
from hq_pilot.db import HQDatabase
from hq_pilot.guards import run_memory_guard
from hq_pilot.prompt_builder import build_prompt
from hq_pilot.setup_cli import run_setup

try:
    from tests.test_adapters_and_channels import DummyTerminal, FakeChannel
    from tests.test_archiver_resume_cursors import _init_git_repo
except ModuleNotFoundError:
    from test_adapters_and_channels import DummyTerminal, FakeChannel
    from test_archiver_resume_cursors import _init_git_repo


REPO_ROOT = Path(__file__).resolve().parent.parent


class TestHerdrBotPolish(unittest.TestCase):
    def test_topic_sessions_are_not_auto_sealed_on_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])

            wt_active = hq_dir / ".hq" / "worktrees" / "wt-active"
            wt_pinned = hq_dir / ".hq" / "worktrees" / "wt-pinned"
            wt_active.mkdir(parents=True, exist_ok=True)
            wt_pinned.mkdir(parents=True, exist_ok=True)

            ch = FakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=AgyAdapter(),
                terminal_driver=DummyTerminal(),
                channel=ch,
            )
            try:
                daemon.db.create_session(
                    "task:gw:feat-a:100",
                    "TOPIC",
                    "agy",
                    status="active",
                    worktree_path=str(wt_active),
                    now=1000,
                )
                daemon.db.create_session(
                    "task:gw:legacy-c:100",
                    "C",
                    "agy",
                    status="active",
                    now=1000,
                )
                daemon.db.create_session(
                    "task:gw:pinned-b:100",
                    "TOPIC",
                    "agy",
                    status="pinned",
                    pinned=True,
                    pinned_reason="manual_input",
                    worktree_path=str(wt_pinned),
                    now=1000,
                )

                # Even after 48 hours idle, #Topic sessions stay active (no idle auto-seal, no Feishu push)
                daemon.run_once(now=1000 + 48 * 3600)
                self.assertEqual(daemon.db.get_session("task:gw:feat-a:100")["status"], "active")
                self.assertEqual(daemon.db.get_session("task:gw:legacy-c:100")["status"], "active")
                self.assertTrue(wt_active.exists())

                pinned_after = daemon.db.get_session("task:gw:pinned-b:100")
                self.assertEqual(pinned_after["status"], "pinned")
                self.assertEqual(pinned_after["pinned"], 1)
                self.assertTrue(wt_pinned.exists())
                self.assertEqual(ch.sent, [])
            finally:
                daemon.close()

    def test_seal_method_cli_and_positional_task_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            repo_dir = Path(tmp) / "gateway-svc"
            _init_git_repo(repo_dir)
            (hq_dir / ".hq" / "local.yaml").write_text(
                f"python_bin: {sys.executable}\nrepo_paths:\n  gateway-svc: {repo_dir}\n",
                encoding="utf-8",
            )

            ch = FakeChannel()
            term = DummyTerminal()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=AgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                sess = daemon.spawn_task("gateway-svc", "auto-close", "Implement feature", now=500)
                skey = sess["session_key"]
                wt_path = Path(sess["worktree_path"])
                self.assertTrue(wt_path.exists())
                self.assertIsNone(sess["channel_thread_id"])
                self.assertEqual(ch.created_threads, [])

                sealed = daemon.seal_session(skey, reason="tests green", now=600)
                self.assertEqual(sealed["status"], "sealed")
                self.assertTrue(wt_path.exists())
                # Silent seal: no Feishu message pushed, task progress + WIP updated to sealed
                self.assertEqual(ch.sent, [])
                self.assertIn("- status: sealed", (hq_dir / "tasks" / "auto-close.md").read_text(encoding="utf-8"))
                self.assertIn("status=sealed", (hq_dir / "tasks" / "active-wip.md").read_text(encoding="utf-8"))
                today_text = (hq_dir / "notes" / "today.md").read_text(encoding="utf-8")
                self.assertNotIn("[WIP] Topic auto-close 已封口", today_text)
            finally:
                daemon.close()

            # CLI seal via slug and error on unknown session
            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            db.create_session(
                "task:gateway-svc:cli-seal:700",
                "TOPIC",
                "agy",
                status="active",
                topic_slug="cli-seal",
                now=700,
            )
            db.close()

            rc_ok = hq_main(["seal", "cli-seal", "--reason", "done via cli", "--hq-dir", str(hq_dir)])
            self.assertEqual(rc_ok, 0)
            db2 = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                self.assertEqual(db2.get_session("task:gateway-svc:cli-seal:700")["status"], "sealed")
            finally:
                db2.close()

            err_buf = io.StringIO()
            with patch("sys.stderr", err_buf):
                rc_missing = hq_main(["seal", "no-such-session", "--hq-dir", str(hq_dir)])
            self.assertEqual(rc_missing, 1)

            # Verify positional `task spawn <repo> <slug> "<prompt>"` parser compatibility
            parser = build_parser(prog="herdr-bot")
            parsed_pos = parser.parse_args(
                ["task", "spawn", "gateway-svc", "pos-slug", "Fix bug", "--hq-dir", str(hq_dir)]
            )
            self.assertEqual(parsed_pos.repo_pos, "gateway-svc")
            self.assertEqual(parsed_pos.slug_pos, "pos-slug")
            self.assertEqual(parsed_pos.prompt_pos, "Fix bug")

            parsed_flags = parser.parse_args(
                [
                    "task",
                    "spawn",
                    "--repo",
                    "gateway-svc",
                    "--slug",
                    "flag-slug",
                    "--prompt",
                    "Fix bug",
                ]
            )
            self.assertEqual(parsed_flags.repo_opt, "gateway-svc")
            self.assertEqual(parsed_flags.slug_opt, "flag-slug")
            self.assertEqual(parsed_flags.prompt_opt, "Fix bug")

    def test_setup_non_interactive_and_interactive_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_a = Path(tmp) / "api-svc"
            _init_git_repo(repo_a)
            subprocess.run(
                ["git", "checkout", "-b", "develop"],
                cwd=str(repo_a),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/example/api-svc.git"],
                cwd=str(repo_a),
                check=True,
            )

            repo_b = Path(tmp) / "worker-svc"
            _init_git_repo(repo_b)

            hq_dir = Path(tmp) / "my-herdr-hq"
            out_buf = io.StringIO()
            with patch("sys.stdout", out_buf):
                rc = hq_main(
                    [
                        "setup",
                        "--non-interactive",
                        "--hq-dir",
                        str(hq_dir),
                        "--project-name",
                        "demo-platform",
                        "--repo",
                        f"api-svc={repo_a}",
                        "--repo",
                        str(repo_b),
                        "--channel",
                        "feishu",
                        "--feishu-app-id",
                        "cli_demo_app",
                        "--feishu-app-secret",
                        "sec_demo_val",
                        "--feishu-chat-id",
                        "oc_demo_chat",
                        "--python-bin",
                        sys.executable,
                    ]
                )
            self.assertEqual(rc, 0)
            stdout_text = out_buf.getvalue()
            self.assertIn("herdr-bot up --hq-dir", stdout_text)
            self.assertIn("herdr", stdout_text)

            cfg = load_config(hq_dir)
            self.assertEqual(cfg.project_name, "demo-platform")
            self.assertEqual(cfg.channel.backend, "feishu")
            self.assertEqual(cfg.channel.allowed_chat_id, "oc_demo_chat")
            self.assertEqual(cfg.repos["api-svc"].git_url, "https://github.com/example/api-svc.git")
            self.assertEqual(cfg.repos["api-svc"].default_branch, "develop")
            self.assertEqual(cfg.repos["worker-svc"].git_url, "")
            self.assertEqual(cfg.repos["worker-svc"].default_branch, "main")
            self.assertFalse(hasattr(cfg.tuning, "topic_idle_seal_minutes"))
            self.assertEqual(cfg.tuning.general_idle_seal_minutes, 1440)

            local_data = yaml.safe_load((hq_dir / ".hq" / "local.yaml").read_text(encoding="utf-8"))
            self.assertEqual(Path(local_data["python_bin"]).resolve(), Path(sys.executable).resolve())
            self.assertEqual(Path(local_data["repo_paths"]["api-svc"]).resolve(), repo_a.resolve())
            self.assertEqual(Path(local_data["repo_paths"]["worker-svc"]).resolve(), repo_b.resolve())

            secrets_path = hq_dir / ".hq" / "secrets.env"
            self.assertTrue(secrets_path.exists())
            self.assertEqual(stat.S_IMODE(secrets_path.stat().st_mode), 0o600)
            secrets = load_secrets_env(hq_dir)
            self.assertEqual(secrets["FEISHU_APP_ID"], "cli_demo_app")
            self.assertEqual(secrets["FEISHU_APP_SECRET"], "sec_demo_val")
            self.assertEqual(secrets["FEISHU_CHAT_ID"], "oc_demo_chat")

            guard = run_memory_guard(hq_dir)
            self.assertTrue(guard.ok, msg=guard.violations)

            # Test interactive wizard flow with mocked input_fn
            hq_inter = Path(tmp) / "interactive-hq"
            answers = iter(
                [
                    str(hq_inter),
                    "interactive-proj",
                    f"api-svc={repo_a}",
                    "",
                    "local",
                ]
            )
            parser = build_parser(prog="herdr-bot")
            inter_args = parser.parse_args(["setup", "--python-bin", sys.executable])
            with patch("sys.stdout", io.StringIO()):
                rc2 = run_setup(inter_args, input_fn=lambda _prompt: next(answers))
            self.assertEqual(rc2, 0)
            cfg2 = load_config(hq_inter)
            self.assertEqual(cfg2.project_name, "interactive-proj")
            self.assertEqual(cfg2.channel.backend, "local")
            self.assertTrue(run_memory_guard(hq_inter).ok)

    def test_prompt_builder_agents_template_and_pyproject_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "demo-hq"
            hq_main(["init", str(hq_dir)])
            cfg = load_config(hq_dir)
            prompt = build_prompt(hq_dir, cfg, {"text": "ping"})
            self.assertIn("herdr-bot task spawn <repo> <slug>", prompt)
            self.assertIn('herdr-bot seal "$HQ_SESSION_KEY"', prompt)

            agents_md = (hq_dir / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("herdr-bot task spawn", agents_md)
            self.assertIn("herdr-bot seal", agents_md)

        pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('name = "herdr-bot"', pyproject_text)
        self.assertIn('herdr-bot = "hq_pilot.cli:main"', pyproject_text)
        self.assertIn('hq = "hq_pilot.cli:main"', pyproject_text)

    def test_bin_herdr_bot_and_install_sh(self) -> None:
        env = dict(os.environ)
        env["HQ_PYTHON"] = sys.executable

        res_hb = subprocess.run(
            [str(REPO_ROOT / "bin" / "herdr-bot"), "--help"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(res_hb.returncode, 0, msg=res_hb.stderr)
        self.assertIn("usage: herdr-bot", res_hb.stdout)
        self.assertIn("setup", res_hb.stdout)
        self.assertIn("seal", res_hb.stdout)

        res_hq = subprocess.run(
            [str(REPO_ROOT / "bin" / "hq"), "--help"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(res_hq.returncode, 0, msg=res_hq.stderr)
        self.assertIn("usage: hq", res_hq.stdout)

        sh_check = subprocess.run(
            ["sh", "-n", str(REPO_ROOT / "install.sh")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(sh_check.returncode, 0, msg=sh_check.stderr)

        with tempfile.TemporaryDirectory() as fake_home:
            bin_dir = Path(fake_home) / ".local" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            for stub in ("agy", "herdr"):
                p = bin_dir / stub
                p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                p.chmod(0o755)

            inst_env = dict(os.environ)
            inst_env["HOME"] = fake_home
            inst_env["PATH"] = f"{bin_dir}:{inst_env.get('PATH', '')}"
            inst_res = subprocess.run(
                ["sh", str(REPO_ROOT / "install.sh"), "--python", sys.executable],
                input="n\n",
                capture_output=True,
                text=True,
                env=inst_env,
                check=False,
            )
            self.assertEqual(inst_res.returncode, 0, msg=inst_res.stderr + inst_res.stdout)
            self.assertTrue((bin_dir / "herdr-bot").is_symlink())
            self.assertTrue((bin_dir / "hq").is_symlink())
            self.assertIn("herdr-bot setup", inst_res.stdout)


if __name__ == "__main__":
    unittest.main()
