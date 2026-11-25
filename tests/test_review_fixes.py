"""Tests for round-1 review fixes: agy camelCase hooks, Track C spawn->send->Stop, defer resume, local_bin."""

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from hq_pilot.agents.agy import AgyAdapter
from hq_pilot.channels.base import InboundMessage
from hq_pilot.cli import main as hq_main
from hq_pilot.config import load_config
from hq_pilot.daemon import HQDaemon
from hq_pilot.db import HQDatabase
from hq_pilot.hook_cli import handle_hook_event, main as hook_main
from hq_pilot.notify_cli import deliver_notification
from hq_pilot.terminals.base import PaneInfo, TerminalDriver
from hq_pilot.terminals.herdr import install_herdr_guide

try:
    from tests.test_adapters_and_channels import FakeChannel
    from tests.test_archiver_resume_cursors import _init_git_repo
except ModuleNotFoundError:
    from test_adapters_and_channels import FakeChannel
    from test_archiver_resume_cursors import _init_git_repo


class RecordingTerminal(TerminalDriver):
    name = "rec-term"

    def __init__(self, reply_fn=None):
        self.bg_calls = []
        self.start_agent_calls = []
        self.prompt_calls = []
        self.sent_texts = []
        self.panes = {}
        self.reply_fn = reply_fn

    def run_bg(self, name, cwd, cmd):
        self.bg_calls.append({"name": name, "cwd": str(cwd), "cmd": cmd})
        self.panes[name] = PaneInfo(name=name, status="running", attached=False, cwd=str(cwd))

    def start_agent(self, name, cwd, kind, agent_args=None, env=None, timeout_ms=60000):
        self.start_agent_calls.append({
            "name": name,
            "cwd": str(cwd),
            "kind": kind,
            "agent_args": list(agent_args or []),
            "env": dict(env or {}),
            "timeout_ms": timeout_ms,
        })
        super().start_agent(
            name=name,
            cwd=cwd,
            kind=kind,
            agent_args=agent_args,
            env=env,
            timeout_ms=timeout_ms,
        )

    def prompt_agent(self, name, text, wait=True, timeout_ms=300000):
        self.prompt_calls.append({"name": name, "text": text, "wait": wait, "timeout_ms": timeout_ms})
        super().prompt_agent(name=name, text=text, wait=wait, timeout_ms=timeout_ms)
        if self.reply_fn is not None:
            return self.reply_fn(name, text)
        return ""

    def list_panes(self):
        return list(self.panes.values())

    def send_text(self, name, text, enter=True):
        self.sent_texts.append({"name": name, "text": text, "enter": enter})

    def read_pane(self, name, lines=80):
        return ""

    def attach_cmd(self, name):
        return ["rec", "attach", name]

    def kill_pane(self, name):
        self.panes.pop(name, None)

    def is_client_attached(self, name):
        p = self.panes.get(name)
        return bool(p and p.attached)


class TestReviewFixes(unittest.TestCase):
    def test_agy_hooks_merge_and_camelcase_stdout_with_hq_session_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / ".agents").mkdir(parents=True)
            (ws / ".agents" / "hooks.json").write_text(
                json.dumps({"other-tool": {"Stop": [{"command": "echo ok"}]}}),
                encoding="utf-8",
            )
            adapter = AgyAdapter()
            hfile = adapter.ensure_hooks_installed(ws, "/usr/local/bin/hq-agent-hook")
            data = json.loads(hfile.read_text(encoding="utf-8"))
            self.assertIn("other-tool", data)
            self.assertIn("hq-pilot", data)
            self.assertNotIn("hooks", data)

            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            db.create_session("task:gw:auth:100", "C", "agy", status="active", now=100)
            db.record_dispatch("task:gw:auth:100", None, "initial prompt", now=100)
            db.close()

            tfile = Path(tmp) / "transcript.jsonl"
            tfile.write_text(
                json.dumps({"role": "user", "content": "initial prompt"}) + "\n"
                + json.dumps({"role": "assistant", "content": "Auth fixed."}) + "\n",
                encoding="utf-8",
            )

            pre_payload = json.dumps({
                "hookEventName": "PreInvocation",
                "conversationId": "conv-camel-1",
                "transcriptPath": str(tfile),
                "lastUserInput": "initial prompt",
                "workspacePaths": [str(ws)],
            })
            stop_payload = json.dumps({
                "hookEventName": "Stop",
                "conversationId": "conv-camel-1",
                "transcriptPath": str(tfile),
            })

            with patch.dict(os.environ, {"HQ_SESSION_KEY": "task:gw:auth:100"}):
                out_pre = io.StringIO()
                with patch("sys.stdin", io.StringIO(pre_payload)), patch("sys.stdout", out_pre):
                    rc1 = hook_main(["--agent", "agy", "--hq-dir", str(hq_dir)])
                self.assertEqual(rc1, 0)
                self.assertEqual(json.loads(out_pre.getvalue()), {"injectSteps": []})

                out_stop = io.StringIO()
                with patch("sys.stdin", io.StringIO(stop_payload)), patch("sys.stdout", out_stop):
                    rc2 = hook_main(["--agent", "agy", "--hq-dir", str(hq_dir)])
                self.assertEqual(rc2, 0)
                self.assertEqual(json.loads(out_stop.getvalue()), {"decision": "allow_stop"})

            db2 = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                sess = db2.get_session("task:gw:auth:100")
                self.assertEqual(sess["agent_session_id"], "conv-camel-1")
                self.assertEqual(sess["transcript_path"], str(tfile))
                # Daemon dispatch matched -> NOT pinned, and no bogus local:conv-camel-1 created
                self.assertEqual(sess["status"], "active")
                self.assertIsNone(db2.get_session("local:conv-camel-1"))
            finally:
                db2.close()

    def test_track_c_spawn_send_text_and_stop_hook_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            repo_dir = Path(tmp) / "gateway-svc"
            _init_git_repo(repo_dir)
            (hq_dir / ".hq" / "local.yaml").write_text(
                f"repo_paths:\n  gateway-svc: {repo_dir}\n", encoding="utf-8"
            )

            term = RecordingTerminal()
            ch = FakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=AgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                sess = daemon.spawn_task("gateway-svc", "fix-cors", "Fix CORS headers", now=500)
                self.assertEqual(sess["track"], "TOPIC")
                self.assertEqual(sess["pane_name"], "hq-fix-cors")
                self.assertEqual(sess["channel_thread_id"], "thread-101")
                self.assertEqual(len(term.bg_calls), 1)
                self.assertIn(f"HQ_SESSION_KEY={sess['session_key']}", term.bg_calls[0]["cmd"])
                self.assertTrue((Path(sess["worktree_path"]) / ".agents" / "hooks.json").exists())

                # Follow-up message in Topic routes to live pane via prompt_agent/send_text
                ch.updates.append(
                    InboundMessage(
                        update_id=10,
                        chat_id="c1",
                        thread_id="thread-101",
                        sender_id="u1",
                        text="Also add unit tests",
                    )
                )
                summary = daemon.run_once(now=510)
                self.assertEqual(summary.events_processed, 1)
                self.assertEqual(term.sent_texts[-1]["text"], "Also add unit tests")

                # Stop hook pushes assistant reply to thread-101
                tfile = Path(tmp) / "t_cors.jsonl"
                tfile.write_text(
                    json.dumps({"role": "user", "content": "Also add unit tests"}) + "\n"
                    + json.dumps({"role": "assistant", "content": "CORS + unit tests done."}) + "\n",
                    encoding="utf-8",
                )
                with patch.dict(os.environ, {"HQ_SESSION_KEY": sess["session_key"]}):
                    res = handle_hook_event(
                        "agy",
                        {
                            "hookEventName": "Stop",
                            "conversationId": "conv-cors-1",
                            "transcriptPath": str(tfile),
                        },
                        hq_dir=hq_dir,
                        channel=ch,
                        now=520,
                    )
                self.assertTrue(res["reply_pushed"])
                self.assertEqual(ch.sent[-1]["text"], "CORS + unit tests done.")
                self.assertEqual(ch.sent[-1]["thread_id"], "thread-101")
            finally:
                daemon.close()

    def test_deferred_button_callback_resumes_suspended_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            ch = FakeChannel()

            def forbid_runner(cmd, cwd):
                raise AssertionError(f"Headless cmd_runner must never be called: {cmd}")

            term = RecordingTerminal(reply_fn=lambda name, text: "Applied choice!")
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=AgyAdapter(),
                terminal_driver=term,
                channel=ch,
                cmd_runner=forbid_runner,
            )
            try:
                daemon.db.create_session(
                    "event:triage:1",
                    "A",
                    "agy",
                    status="active",
                    agent_session_id="conv-defer-1",
                    channel_thread_id="thread-55",
                    now=100,
                )
                msg_id = deliver_notification(
                    hq_dir,
                    "review",
                    "Breaking API change?",
                    session_key="event:triage:1",
                    options=["Accept", "Hold"],
                    channel=ch,
                    now=110,
                )
                self.assertIsNotNone(msg_id)
                self.assertEqual(daemon.db.get_session("event:triage:1")["suspended_msg_id"], msg_id)

                ch.updates.append(
                    InboundMessage(
                        update_id=50,
                        chat_id="c1",
                        thread_id="thread-55",
                        sender_id="u1",
                        text="",
                        is_callback=True,
                        callback_id="cb-defer",
                        callback_data="defer:event:triage:1:1",
                        message_id=msg_id,
                    )
                )
                summary = daemon.run_once(now=120)
                self.assertEqual(summary.events_processed, 1)
                self.assertEqual(term.prompt_calls[-1]["name"], "hq-general")
                self.assertIn("Accept", term.prompt_calls[-1]["text"])
                sess_after = daemon.db.get_session("event:triage:1")
                self.assertIsNone(sess_after["suspended_msg_id"])
                self.assertEqual(sess_after["status"], "sealed")
                self.assertTrue(any(m["text"] == "已收到你的选择：Accept" for m in ch.sent))
                self.assertEqual(ch.sent[-1]["text"], "Applied choice!")

                # Clicking the same card again returns expired/processed notice without re-enqueuing
                ch.updates.append(
                    InboundMessage(
                        update_id=51,
                        chat_id="c1",
                        thread_id="thread-55",
                        sender_id="u1",
                        text="",
                        is_callback=True,
                        callback_id="cb-defer-dup",
                        callback_data="defer:event:triage:1:1",
                        message_id=msg_id,
                    )
                )
                summary2 = daemon.run_once(now=125)
                self.assertEqual(summary2.events_processed, 0)
                self.assertEqual(
                    ch.sent[-1]["text"],
                    "该操作卡片已过期或已处理，请直接回复最新消息。",
                )
            finally:
                daemon.close()

    def test_local_yaml_bin_suffix_and_herdr_official_url(self) -> None:
        guide = install_herdr_guide()
        self.assertIn("https://github.com/herdrdev/herdr", guide)
        self.assertIn("https://herdr.dev/install.sh", guide)
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "acme-hq"
            hq_main(["init", str(hq_dir)])
            (hq_dir / ".hq" / "local.yaml").write_text(
                "agy_bin: /opt/bin/agy\nhc_bin: /opt/bin/hc\n",
                encoding="utf-8",
            )
            cfg = load_config(hq_dir)
            self.assertEqual(cfg.local.bin_overrides["agy"], "/opt/bin/agy")
            self.assertEqual(cfg.local.bin_overrides["hc"], "/opt/bin/hc")


if __name__ == "__main__":
    unittest.main()
