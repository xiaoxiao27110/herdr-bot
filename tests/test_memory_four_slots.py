"""Tests for the four business memory slots: User instructions, User notes, Past messages, Task progress."""

from __future__ import annotations

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from hq_pilot.agents.agy import AgyAdapter
from hq_pilot.cli import main as hq_main
from hq_pilot.config import load_config
from hq_pilot.daemon import HQDaemon
from hq_pilot.prompt_builder import build_prompt

try:
    from tests.test_adapters_and_channels import DummyTerminal, FakeChannel
    from tests.test_archiver_resume_cursors import _init_git_repo
except ModuleNotFoundError:
    from test_adapters_and_channels import DummyTerminal, FakeChannel
    from test_archiver_resume_cursors import _init_git_repo


class TestMemoryFourSlots(unittest.TestCase):
    def test_init_and_setup_create_user_notes_and_agents_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_init = Path(tmp) / "init-hq"
            rc = hq_main(["init", str(hq_init)])
            self.assertEqual(rc, 0)

            user_notes = hq_init / "memory" / "user-notes.md"
            self.assertTrue(user_notes.is_file())
            un_text = user_notes.read_text(encoding="utf-8")
            self.assertIn("## Role & Context", un_text)
            self.assertIn("## Preferences", un_text)
            self.assertIn("## Corrections & Guardrails", un_text)

            agents_text = (hq_init / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("memory/user-notes.md", agents_text)

            cfg = load_config(hq_init)
            self.assertEqual(cfg.tuning.past_messages_limit, 5)

            # Verify hq setup --non-interactive also seeds memory/user-notes.md
            repo_a = Path(tmp) / "svc-a"
            _init_git_repo(repo_a)
            hq_setup = Path(tmp) / "setup-hq"
            with patch("sys.stdout", io.StringIO()):
                rc_setup = hq_main(
                    [
                        "setup",
                        "--non-interactive",
                        "--hq-dir",
                        str(hq_setup),
                        "--project-name",
                        "demo-four-slots",
                        "--repo",
                        f"svc-a={repo_a}",
                        "--channel",
                        "local",
                    ]
                )
            self.assertEqual(rc_setup, 0)
            setup_notes = hq_setup / "memory" / "user-notes.md"
            self.assertTrue(setup_notes.is_file())
            self.assertIn("## Role & Context", setup_notes.read_text(encoding="utf-8"))
            self.assertIn(
                "memory/user-notes.md",
                (hq_setup / "AGENTS.md").read_text(encoding="utf-8"),
            )

    def test_user_notes_inline_vs_pointer_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            cfg = load_config(hq_dir)

            # <= 40 lines & no memory/user-notes/ subdir -> inline directly
            notes_path = hq_dir / "memory" / "user-notes.md"
            notes_path.write_text(
                "# User Notes\n\n## Role & Context\n- Platform engineer\n",
                encoding="utf-8",
            )
            prompt_inline = build_prompt(hq_dir, cfg, {"text": "hi"})
            self.assertIn("## 1.5 User Notes\n# User Notes", prompt_inline)
            self.assertIn("- Platform engineer", prompt_inline)

            # > 40 lines -> index pointer only
            long_lines = ["# User Notes"] + [f"- item {i}" for i in range(1, 45)]
            notes_path.write_text("\n".join(long_lines) + "\n", encoding="utf-8")
            prompt_long = build_prompt(hq_dir, cfg, {"text": "hi"})
            self.assertIn("## 1.5 User Notes\nSee `memory/user-notes.md` (45 lines)", prompt_long)
            self.assertNotIn("- item 44", prompt_long)

            # <= 40 lines but memory/user-notes/ directory exists -> index pointer only
            notes_path.write_text("# User Notes\n- short\n", encoding="utf-8")
            (hq_dir / "memory" / "user-notes").mkdir(parents=True, exist_ok=True)
            prompt_dir = build_prompt(hq_dir, cfg, {"text": "hi"})
            self.assertIn("## 1.5 User Notes\nSee `memory/user-notes.md`", prompt_dir)
            self.assertNotIn("- short", prompt_dir)

    def test_build_prompt_recent_outbound_truncation_and_prefix_cache_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            cfg = load_config(hq_dir)

            base_prompt = build_prompt(hq_dir, cfg, {"text": "ping"})
            self.assertNotIn("## 4.5 Recent Messages Sent to User", base_prompt)

            long_msg = "A" * 300
            recent = [
                {"thread_id": None, "text": "Already sent morning digest."},
                {"thread_id": "topic-42", "text": f"Line1\n   {long_msg}"},
            ]
            with_recent = build_prompt(
                hq_dir, cfg, {"text": "ping"}, recent_outbound=recent
            )

            # Static prefix up through section 4 is byte-identical (Prefix Cache friendly)
            marker = "## 5. Event Payload"
            base_static = base_prompt[: base_prompt.index(marker)]
            recent_idx = with_recent.index("## 4.5 Recent Messages Sent to User (Do Not Repeat)")
            self.assertEqual(with_recent[:recent_idx], base_static)

            # Section order strictly preserved
            i1 = with_recent.index("## 1. Repo Boundaries")
            i15 = with_recent.index("## 1.5 User Notes")
            i2 = with_recent.index("## 2. Relevant Contracts")
            i3 = with_recent.index("## 3. Active WIP")
            i4 = with_recent.index("## 4. Today WAL")
            i45 = recent_idx
            i5 = with_recent.index(marker)
            self.assertTrue(i1 < i15 < i2 < i3 < i4 < i45 < i5)

            # Thread tags and <= 240 char truncation
            sec45 = with_recent[i45:i5]
            self.assertIn("- [thread=general] Already sent morning digest.", sec45)
            topic_lines = [ln for ln in sec45.splitlines() if ln.startswith("- [thread=topic-42] ")]
            self.assertEqual(len(topic_lines), 1)
            body = topic_lines[0].split("- [thread=topic-42] ", 1)[1]
            self.assertEqual(len(body), 240)
            self.assertTrue(body.endswith("..."))

    def test_spawn_task_and_seal_session_maintain_task_progress_and_wip_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            repo_dir = Path(tmp) / "gateway-svc"
            _init_git_repo(repo_dir)
            (hq_dir / ".hq" / "local.yaml").write_text(
                f"python_bin: {sys.executable}\nrepo_paths:\n  gateway-svc: {repo_dir}\n",
                encoding="utf-8",
            )

            daemon = HQDaemon(
                hq_dir,
                agent_adapter=AgyAdapter(),
                terminal_driver=DummyTerminal(),
                channel=FakeChannel(),
            )
            try:
                sess = daemon.spawn_task(
                    "gateway-svc",
                    "fix-auth",
                    "Fix token refresh race condition",
                    now=1000,
                )
                task_file = hq_dir / "tasks" / "fix-auth.md"
                wip_file = hq_dir / "tasks" / "active-wip.md"

                self.assertTrue(task_file.is_file())
                task_text = task_file.read_text(encoding="utf-8")
                self.assertIn("# Task: fix-auth", task_text)
                self.assertIn("- repo: gateway-svc", task_text)
                self.assertIn("- branch: hq/fix-auth", task_text)
                self.assertIn(f"- session_key: {sess['session_key']}", task_text)
                self.assertIn("- status: active", task_text)
                self.assertIn("## Done", task_text)
                self.assertIn("## Left", task_text)

                wip_text = wip_file.read_text(encoding="utf-8")
                self.assertIn(
                    "- [fix-auth](tasks/fix-auth.md) | repo=gateway-svc | branch=hq/fix-auth | status=active",
                    wip_text,
                )

                # Simulate agent recording progress before seal
                task_file.write_text(
                    task_text.replace("## Done\n-", "## Done\n- Added mutex lock"),
                    encoding="utf-8",
                )

                sealed = daemon.seal_session(sess["session_key"], reason="verified", now=1200)
                self.assertEqual(sealed["status"], "sealed")

                task_after = task_file.read_text(encoding="utf-8")
                self.assertIn("- status: sealed", task_after)
                self.assertNotIn("- status: active", task_after)
                self.assertIn("- Added mutex lock", task_after)

                wip_after = wip_file.read_text(encoding="utf-8")
                self.assertIn(
                    "- [fix-auth](tasks/fix-auth.md) | repo=gateway-svc | branch=hq/fix-auth | status=sealed",
                    wip_after,
                )
                self.assertNotIn("status=active", wip_after)
            finally:
                daemon.close()

    def test_topic_wake_loads_task_progress_while_general_loads_wip_index_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            repo_dir = Path(tmp) / "gateway-svc"
            _init_git_repo(repo_dir)
            (hq_dir / ".hq" / "local.yaml").write_text(
                f"python_bin: {sys.executable}\nrepo_paths:\n  gateway-svc: {repo_dir}\n",
                encoding="utf-8",
            )

            term = DummyTerminal()
            ch = FakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=AgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                # Seed a recent outbound message in local_messages
                daemon.db.add_local_message(
                    direction="outbound",
                    text="Prior update already sent to user",
                    thread_id="thread-101",
                    now=900,
                )

                sess = daemon.spawn_task(
                    "gateway-svc",
                    "rate-limit",
                    "Add token bucket limiter",
                    now=1000,
                )
                thread_id = sess["channel_thread_id"]
                self.assertIsNotNone(thread_id)

                # Simulate topic pane having exited so next channel event wakes and rebuilds prompt
                term.panes.pop(sess["pane_name"], None)
                term.prompted.clear()

                # 1) Channel event on #General -> loads Active WIP index only, NOT Task Progress
                daemon.db.enqueue_event(
                    source="channel",
                    dedupe_key="ch-general-1",
                    priority=10,
                    payload={"text": "status?", "thread_id": None},
                    now=1100,
                )
                # 2) Channel event on #Topic -> loads both Active WIP index AND Task Progress (rate-limit)
                daemon.db.enqueue_event(
                    source="channel",
                    dedupe_key="ch-topic-1",
                    priority=10,
                    payload={"text": "continue work", "thread_id": thread_id},
                    now=1101,
                )

                daemon.run_once(now=1105)

                general_prompts = [p["text"] for p in term.prompted if p["name"] == "hq-general"]
                topic_prompts = [p["text"] for p in term.prompted if p["name"] == sess["pane_name"]]
                self.assertEqual(len(general_prompts), 1)
                self.assertEqual(len(topic_prompts), 1)

                gp = general_prompts[0]
                self.assertIn("## 3. Active WIP", gp)
                self.assertIn("- [rate-limit](tasks/rate-limit.md)", gp)
                self.assertNotIn("## 3.5 Task Progress", gp)
                self.assertIn("## 4.5 Recent Messages Sent to User (Do Not Repeat)", gp)
                self.assertIn("[thread=thread-101] Prior update already sent to user", gp)

                tp = topic_prompts[0]
                self.assertIn("## 3. Active WIP", tp)
                self.assertIn("## 3.5 Task Progress (rate-limit)", tp)
                self.assertIn("# Task: rate-limit", tp)
                self.assertIn("Add token bucket limiter", tp)
                self.assertIn("## 4.5 Recent Messages Sent to User (Do Not Repeat)", tp)
            finally:
                daemon.close()


if __name__ == "__main__":
    unittest.main()
