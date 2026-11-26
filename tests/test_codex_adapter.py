"""Unit and end-to-end tests for CodexAdapter, rollout parsing, and Codex hook flows."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

from hq_pilot.agents.base import HookEvent
from hq_pilot.agents.codex import (
    CodexAdapter,
    CodexConfigError,
    _parse_toml,
    ensure_codex_projects_trusted,
)
from hq_pilot.agents.registry import get_agent_adapter
from hq_pilot.cli import main as hq_main
from hq_pilot.config import load_config
from hq_pilot.daemon import HQDaemon
from hq_pilot.db import HQDatabase
from hq_pilot.hook_cli import handle_hook_event, main as hook_main
from hq_pilot.service_cli import render_systemd_unit

try:
    from tests.test_adapters_and_channels import DummyTerminal, FakeChannel
except ModuleNotFoundError:
    from test_adapters_and_channels import DummyTerminal, FakeChannel

REPO_ROOT = Path(__file__).resolve().parent.parent


def _sample_codex_0163_rollout() -> list[dict]:
    """Desensitized fixture modeled on real codex-cli 0.163.0 rollout JSONL."""
    return [
        {
            "timestamp": "2026-11-26T06:55:06.100Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {"id": "01a216a2-ef11-7f90-9a90-7a1cf159acf0", "cwd": "/tmp/ws"},
        },
        {
            "timestamp": "2026-11-26T06:55:06.200Z",
            "ordinal": 1,
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-11-26T06:55:07.609Z",
            "ordinal": 2,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "<skills_instructions>\nSkills...\n</skills_instructions>"}],
            },
        },
        {
            "timestamp": "2026-11-26T06:55:07.610Z",
            "ordinal": 3,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "# AGENTS.md instructions for /tmp/ws\n\n<INSTRUCTIONS>...</INSTRUCTIONS>"},
                    {"type": "input_text", "text": "<environment_context>\n  <cwd>/tmp/ws</cwd>\n</environment_context>"},
                ],
            },
        },
        {
            "timestamp": "2026-11-26T06:55:08.149Z",
            "ordinal": 4,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "Injected hook additionalContext"}],
            },
        },
        {
            "timestamp": "2026-11-26T06:55:08.695Z",
            "ordinal": 5,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Check service health"}],
            },
        },
        {
            "timestamp": "2026-11-26T06:55:08.696Z",
            "ordinal": 6,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "01a216a2-ef11-7f90-9a90-7a1cf159acf0",
                "turn_id": "turn-1",
                "item": {
                    "type": "UserMessage",
                    "id": "u-1",
                    "content": [{"type": "text", "text": "Check service health"}],
                },
            },
        },
        {
            "timestamp": "2026-11-26T06:55:09.100Z",
            "ordinal": 7,
            "type": "response_item",
            "payload": {"type": "function_call", "name": "shell", "arguments": "{\"cmd\":\"uptime\"}"},
        },
        {
            "timestamp": "2026-11-26T06:55:09.200Z",
            "ordinal": 8,
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "c1", "output": "up 10 days"},
        },
        {
            "timestamp": "2026-11-26T06:55:09.904Z",
            "ordinal": 9,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-1",
                "item": {
                    "type": "Reasoning",
                    "id": "r-1",
                    "summary_text": [],
                    "raw_content": ["Internal chain of thought"],
                },
            },
        },
        {
            "timestamp": "2026-11-26T06:55:10.011Z",
            "ordinal": 10,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-1",
                "item": {
                    "type": "AgentMessage",
                    "id": "a-1",
                    "content": [{"type": "Text", "text": "All services are healthy."}],
                },
            },
        },
        {
            "timestamp": "2026-11-26T06:55:10.012Z",
            "ordinal": 11,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "All services are healthy."}],
            },
        },
        {
            "timestamp": "2026-11-26T06:55:10.581Z",
            "ordinal": 12,
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": "All services are healthy.",
            },
        },
    ]


class TestCodexAdapter(unittest.TestCase):
    def test_command_construction_headless_interactive_and_trust_args(self) -> None:
        adapter = CodexAdapter(bin_path="/usr/local/bin/codex")
        self.assertEqual(adapter.name, "codex")
        self.assertEqual(adapter.default_bin, "codex")
        self.assertEqual(adapter.index_filename, "AGENTS.md")

        # 1. Headless new session
        cmd_new = adapter.build_headless_cmd(
            "summarize changes",
            model="gpt-5-codex",
            effort="low",
            add_dirs=[Path("/tmp/r1"), Path("/tmp/r2")],
        )
        self.assertEqual(
            cmd_new,
            [
                "/usr/local/bin/codex",
                "exec",
                "--json",
                "-m",
                "gpt-5-codex",
                "-c",
                'model_reasoning_effort="low"',
                "--add-dir",
                "/tmp/r1",
                "--add-dir",
                "/tmp/r2",
                "--skip-git-repo-check",
                "summarize changes",
            ],
        )

        # 2. Headless resume (`--add-dir` precedes `resume`, matching codex-cli 0.163+)
        cmd_res = adapter.build_headless_cmd(
            "continue task",
            session_id="01a2-thread",
            model="gpt-5-codex",
            effort="high",
            add_dirs=[Path("/tmp/r1")],
        )
        self.assertEqual(
            cmd_res,
            [
                "/usr/local/bin/codex",
                "exec",
                "--add-dir",
                "/tmp/r1",
                "resume",
                "01a2-thread",
                "--json",
                "-m",
                "gpt-5-codex",
                "-c",
                'model_reasoning_effort="high"',
                "--skip-git-repo-check",
                "continue task",
            ],
        )

        # 3. Interactive new & resume
        icmd_new = adapter.build_interactive_cmd(
            initial_prompt="hello",
            model="gpt-5-codex",
            effort="high",
            add_dirs=[Path("/tmp/repo-a")],
        )
        self.assertEqual(
            icmd_new,
            [
                "/usr/local/bin/codex",
                "-m",
                "gpt-5-codex",
                "-c",
                'model_reasoning_effort="high"',
                "--add-dir",
                "/tmp/repo-a",
                "hello",
            ],
        )

        icmd_res = adapter.build_interactive_cmd(
            initial_prompt="follow up",
            session_id="01a2-thread",
            model="gpt-5-codex",
            effort="low",
        )
        self.assertEqual(
            icmd_res,
            [
                "/usr/local/bin/codex",
                "resume",
                "01a2-thread",
                "-m",
                "gpt-5-codex",
                "-c",
                'model_reasoning_effort="low"',
                "follow up",
            ],
        )

        # 4. Permission modes & trusted_dirs deduplication
        with tempfile.TemporaryDirectory() as tmp:
            d1 = Path(tmp) / "ws"
            d2 = Path(tmp) / "repo"
            d1.mkdir()
            d2.mkdir()
            r1 = str(d1.resolve())
            r2 = str(d2.resolve())

            t_default = adapter.extra_trust_args([d1, d1, d2], permission_mode="default")
            self.assertEqual(
                t_default,
                [
                    "-c",
                    f'projects."{r1}".trust_level="trusted"',
                    "-c",
                    f'projects."{r2}".trust_level="trusted"',
                    "--dangerously-bypass-hook-trust",
                ],
            )

            t_accept = adapter.extra_trust_args([d1], permission_mode="accept-edits")
            self.assertEqual(t_accept[-4:], ["-s", "workspace-write", "-a", "on-request"])

            t_turbo = adapter.extra_trust_args([d1], permission_mode="turbo")
            self.assertEqual(t_turbo[-4:], ["-s", "workspace-write", "-a", "never"])

            t_always = adapter.extra_trust_args([d1], permission_mode="always-proceed")
            self.assertEqual(t_always[-4:], ["-s", "workspace-write", "-a", "never"])

            t_skip = adapter.extra_trust_args([d1], permission_mode="skip-all")
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", t_skip)
            self.assertNotIn("-s", t_skip)
            self.assertNotIn("-a", t_skip)

    def test_parse_headless_output_jsonl_and_fallback(self) -> None:
        adapter = CodexAdapter()
        jsonl_stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"01a216a2-ef11-7f90-9a90-7a1cf159acf0"}',
                '{"type":"item.completed","item":{"id":"item_0","type":"error","message":"hook warning"}}',
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"ls","status":"completed"}}',
                '{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"Intermediate commentary"}}',
                '{"type":"item.completed","item":{"id":"item_3","type":"agent_message","text":"Final concise answer."}}',
                '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":20}}',
            ]
        )
        res = adapter.parse_headless_output(jsonl_stdout)
        self.assertEqual(res.agent_session_id, "01a216a2-ef11-7f90-9a90-7a1cf159acf0")
        self.assertEqual(res.reply_markdown, "Final concise answer.")

        # Non-JSON plain text degrades cleanly
        plain = adapter.parse_headless_output("plain text fallback reply\n")
        self.assertEqual(plain.reply_markdown, "plain text fallback reply")
        self.assertIsNone(plain.agent_session_id)

        # Empty output
        empty = adapter.parse_headless_output("   \n")
        self.assertEqual(empty.reply_markdown, "")
        self.assertIsNone(empty.agent_session_id)

    def test_rollout_transcript_slicing_filters_noise_and_keeps_physical_cursor(self) -> None:
        adapter = CodexAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            # 1. Codex 0.163.0 item_completed rollout format
            rfile = Path(tmp) / "rollout-2026-11-26T14-55-06-01a2.jsonl"
            records = _sample_codex_0163_rollout()
            rfile.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(adapter.count_transcript_lines(rfile), len(records))

            turns, total = adapter.read_transcript_slice(rfile, from_line=0)
            self.assertEqual(total, len(records))
            self.assertEqual(
                [(t.role, t.text, t.line_no) for t in turns],
                [
                    ("user", "Check service health", 6),
                    ("assistant", "All services are healthy.", 10),
                ],
            )

            # Incremental slice after turn 1 returns empty until new turn appended
            turns_empty, total2 = adapter.read_transcript_slice(rfile, from_line=total)
            self.assertEqual(turns_empty, [])
            self.assertEqual(total2, len(records))

            # Append second turn using event_msg user_message / agent_message format
            extra = [
                {
                    "timestamp": "2026-11-26T06:56:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "Run tests"},
                },
                {
                    "timestamp": "2026-11-26T06:56:05.000Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "42 passed.", "phase": None},
                },
            ]
            with rfile.open("a", encoding="utf-8") as fh:
                for rec in extra:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

            turns_next, total3 = adapter.read_transcript_slice(rfile, from_line=total2)
            self.assertEqual(total3, len(records) + 2)
            self.assertEqual(
                [(t.role, t.text, t.line_no) for t in turns_next],
                [
                    ("user", "Run tests", len(records)),
                    ("assistant", "42 passed.", len(records) + 1),
                ],
            )

    def test_ensure_hooks_installed_merges_and_is_idempotent(self) -> None:
        adapter = CodexAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            hooks_file = ws / ".codex" / "hooks.json"
            hooks_file.parent.mkdir(parents=True)
            hooks_file.write_text(
                json.dumps(
                    {
                        "customTopKey": True,
                        "hooks": {
                            "PreToolUse": [
                                {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/audit"}]}
                            ],
                            "Stop": [
                                {"hooks": [{"type": "command", "command": "/usr/bin/user-stop-hook"}]}
                            ],
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            installed = adapter.ensure_hooks_installed(ws, "/opt/bin/hq-agent-hook")
            self.assertEqual(installed, hooks_file)
            cfg1 = json.loads(hooks_file.read_text(encoding="utf-8"))
            self.assertTrue(cfg1["customTopKey"])
            self.assertEqual(len(cfg1["hooks"]["PreToolUse"]), 1)
            self.assertEqual(len(cfg1["hooks"]["Stop"]), 2)
            self.assertEqual(
                cfg1["hooks"]["Stop"][0]["hooks"][0]["command"],
                "/usr/bin/user-stop-hook",
            )
            self.assertEqual(
                cfg1["hooks"]["Stop"][1]["hooks"][0],
                {"type": "command", "command": "/opt/bin/hq-agent-hook --agent codex"},
            )
            for ev in ("SessionStart", "UserPromptSubmit", "Stop"):
                self.assertIn(ev, cfg1["hooks"])

            # Repeated call with same path is idempotent (no duplicate entries)
            adapter.ensure_hooks_installed(ws, "/opt/bin/hq-agent-hook")
            cfg2 = json.loads(hooks_file.read_text(encoding="utf-8"))
            self.assertEqual(cfg1, cfg2)

            # Updating hook_bin replaces existing herdr-bot entry in-place while keeping user hooks
            adapter.ensure_hooks_installed(ws, "/new/bin/hq-agent-hook")
            cfg3 = json.loads(hooks_file.read_text(encoding="utf-8"))
            self.assertEqual(len(cfg3["hooks"]["Stop"]), 2)
            self.assertEqual(
                cfg3["hooks"]["Stop"][1]["hooks"][0]["command"],
                "/new/bin/hq-agent-hook --agent codex",
            )

    def test_hook_payload_parsing_and_response_rendering(self) -> None:
        adapter = CodexAdapter()

        ev_start = adapter.parse_hook_payload(
            {
                "hook_event_name": "SessionStart",
                "session_id": "sid-1",
                "transcript_path": "/tmp/rollout.jsonl",
                "cwd": "/tmp/ws",
                "source": "startup",
            }
        )
        self.assertEqual(ev_start.kind, "session_start")
        self.assertEqual(ev_start.agent_session_id, "sid-1")
        self.assertEqual(ev_start.transcript_path, "/tmp/rollout.jsonl")
        self.assertEqual(ev_start.cwd, "/tmp/ws")
        self.assertEqual(adapter.render_hook_response(ev_start, []), {})
        self.assertEqual(
            adapter.render_hook_response(ev_start, ["hello ctx"]),
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "hello ctx",
                }
            },
        )

        ev_prompt = adapter.parse_hook_payload(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "sid-1",
                "transcript_path": "/tmp/rollout.jsonl",
                "prompt": "fix the bug",
                "cwd": "/tmp/ws",
            }
        )
        self.assertEqual(ev_prompt.kind, "user_prompt")
        self.assertEqual(ev_prompt.user_prompt, "fix the bug")
        self.assertEqual(adapter.render_hook_response(ev_prompt, []), {})
        self.assertEqual(
            adapter.render_hook_response(ev_prompt, ["part1", "part2"]),
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "part1\n\npart2",
                }
            },
        )

        ev_stop = adapter.parse_hook_payload(
            {
                "hook_event_name": "Stop",
                "session_id": "sid-1",
                "transcript_path": "/tmp/rollout.jsonl",
                "last_assistant_message": "done",
                "stop_hook_active": False,
                "cwd": "/tmp/ws",
            }
        )
        self.assertEqual(ev_stop.kind, "stop")
        # Codex stop output schema forbids unknown fields and decision="allow_stop"
        self.assertEqual(adapter.render_hook_response(ev_stop, ["ignored"]), {})

        ev_other = adapter.parse_hook_payload({"hook_event_name": "PreToolUse"})
        self.assertEqual(ev_other.kind, "other")
        self.assertEqual(adapter.render_hook_response(ev_other, []), {})

    def test_hook_cli_end_to_end_codex_lifecycle_handoff_and_ephemeral(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "codex-hq"
            hq_main(["init", str(hq_dir)])
            hq_yaml = hq_dir / "hq.yaml"
            hq_data = yaml.safe_load(hq_yaml.read_text(encoding="utf-8"))
            hq_data["agent"]["backend"] = "codex"
            hq_yaml.write_text(yaml.safe_dump(hq_data, sort_keys=False), encoding="utf-8")

            rollout = Path(tmp) / "rollout-codex.jsonl"
            rollout.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in _sample_codex_0163_rollout()) + "\n",
                encoding="utf-8",
            )

            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                db.create_session(
                    "general:1",
                    "GENERAL",
                    "codex",
                    status="active",
                    pane_name="hq-general",
                    now=100,
                )
                db.record_dispatch("general:1", None, "Check service health", now=105)
                db.set_meta("last_input_origin:general:1", "daemon", now=105)
                db.set_meta("general_handoff_pending", "1", now=105)
                db.set_meta("general_handoff_summary", "Yesterday shipped codex backend.", now=105)
                db.set_meta("general_handoff_reason", "night", now=105)
            finally:
                db.close()

            ch = FakeChannel()
            with patch.dict(os.environ, {"HQ_SESSION_KEY": "general:1"}):
                # 1. SessionStart binds session_id and transcript_path
                r_start = handle_hook_event(
                    "codex",
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "codex-sid-1",
                        "transcript_path": str(rollout),
                        "cwd": str(hq_dir),
                        "source": "startup",
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=110,
                )
                self.assertEqual(r_start["status"], "ok")
                self.assertEqual(r_start["event_kind"], "session_start")
                self.assertEqual(r_start["hook_response"], {})

                # 2. UserPromptSubmit via hook_main WITHOUT --agent (auto-inferred as codex from hq.yaml!)
                #    Also verifies general handoff summary injection into Codex additionalContext!
                prompt_stdin = json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "codex-sid-1",
                        "transcript_path": str(rollout),
                        "prompt": "Check service health",
                        "cwd": str(hq_dir),
                    }
                )
                out_prompt = io.StringIO()
                with patch("sys.stdin", io.StringIO(prompt_stdin)), patch("sys.stdout", out_prompt):
                    rc_prompt = hook_main(["--hq-dir", str(hq_dir)])
                self.assertEqual(rc_prompt, 0)
                prompt_resp = json.loads(out_prompt.getvalue())
                self.assertEqual(
                    prompt_resp["hookSpecificOutput"]["hookEventName"],
                    "UserPromptSubmit",
                )
                self.assertIn("昨日会话交接摘要", prompt_resp["hookSpecificOutput"]["additionalContext"])
                self.assertIn("Yesterday shipped codex backend.", prompt_resp["hookSpecificOutput"]["additionalContext"])

                # 3. Stop pushes daemon reply to channel and returns {} (valid Codex stop output)
                r_stop = handle_hook_event(
                    "codex",
                    {
                        "hook_event_name": "Stop",
                        "session_id": "codex-sid-1",
                        "transcript_path": str(rollout),
                        "cwd": str(hq_dir),
                        "stop_hook_active": False,
                        "last_assistant_message": "All services are healthy.",
                    },
                    hq_dir=hq_dir,
                    channel=ch,
                    now=120,
                )
                self.assertEqual(r_stop["status"], "ok")
                self.assertTrue(r_stop["reply_pushed"])
                self.assertEqual(len(ch.sent), 1)
                self.assertEqual(ch.sent[0]["text"], "All services are healthy.")

                # Verify hook_main stdout for Stop is `{}`
                out_stop = io.StringIO()
                stop_stdin = json.dumps(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "codex-sid-1",
                        "transcript_path": str(rollout),
                        "cwd": str(hq_dir),
                    }
                )
                with patch("sys.stdin", io.StringIO(stop_stdin)), patch("sys.stdout", out_stop):
                    rc_stop = hook_main(["--hq-dir", str(hq_dir)])
                self.assertEqual(rc_stop, 0)
                self.assertEqual(json.loads(out_stop.getvalue()), {})

            # Verify DB session state updated
            db2 = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                sess = db2.get_session("general:1")
                self.assertEqual(sess["agent_session_id"], "codex-sid-1")
                self.assertEqual(sess["transcript_path"], str(rollout))
                self.assertEqual(sess["turn_count"], 1)
                self.assertEqual(sess["reply_line_offset"], len(_sample_codex_0163_rollout()))
            finally:
                db2.close()

            # 4. Ephemeral background worker Stop stores assistant reply under reply_key
            with patch.dict(
                os.environ,
                {"HQ_EPHEMERAL": "1", "HQ_EPHEMERAL_REPLY_KEY": "ephemeral_reply:bg-codex-1"},
            ):
                r_eph = handle_hook_event(
                    "codex",
                    {
                        "hook_event_name": "Stop",
                        "session_id": "eph-sid-1",
                        "transcript_path": str(rollout),
                        "cwd": str(hq_dir),
                    },
                    hq_dir=hq_dir,
                    now=130,
                )
            self.assertTrue(r_eph["ephemeral"])
            db3 = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                self.assertEqual(
                    db3.get_meta("ephemeral_reply:bg-codex-1"),
                    "All services are healthy.",
                )
            finally:
                db3.close()

    def test_setup_service_and_install_support_codex_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            fake_bin_dir = Path(tmp) / "custom-codex-bin"
            fake_bin_dir.mkdir()
            fake_codex = fake_bin_dir / "codex"
            fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_codex.chmod(0o755)

            hq_dir = Path(tmp) / "codex-ws"
            isolated_codex_home = fake_home / ".codex"
            out = io.StringIO()
            with (
                patch.dict(os.environ, {"CODEX_HOME": str(isolated_codex_home)}),
                patch("sys.stdout", out),
            ):
                rc = hq_main(
                    [
                        "setup",
                        "--non-interactive",
                        "--hq-dir",
                        str(hq_dir),
                        "--project-name",
                        "codex-proj",
                        "--agent-backend",
                        "codex",
                        "--agent-bin",
                        str(fake_codex),
                        "--channel",
                        "local",
                        "--python-bin",
                        sys.executable,
                    ]
                )
            self.assertEqual(rc, 0)
            cfg = load_config(hq_dir)
            self.assertEqual(cfg.agent.backend, "codex")
            self.assertTrue(cfg.agent.auto_trust_project)
            self.assertEqual(cfg.get_agent_bin_override(), str(fake_codex.resolve()))
            adapter = get_agent_adapter(cfg.agent.backend, bin_path=cfg.get_agent_bin_override())
            self.assertIsInstance(adapter, CodexAdapter)
            self.assertEqual(adapter.bin_path, str(fake_codex.resolve()))

            # setup auto-trusted hq_dir in isolated CODEX_HOME/config.toml
            codex_cfg_path = isolated_codex_home / "config.toml"
            self.assertTrue(codex_cfg_path.is_file())
            parsed_cfg = _parse_toml(codex_cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(
                parsed_cfg["projects"][str(hq_dir.resolve())]["trust_level"],
                "trusted",
            )

            # Systemd service PATH includes custom codex binary dir
            unit = render_systemd_unit(hq_dir, user_home=fake_home, python_bin=sys.executable)
            path_line = next(l for l in unit.splitlines() if l.startswith('Environment="PATH='))
            self.assertIn(str(fake_bin_dir.resolve()), path_line)

            # install.sh --agent codex succeeds and reports [ok] codex
            inst_env = dict(os.environ)
            inst_env["HOME"] = str(fake_home)
            inst_env["PATH"] = f"{fake_bin_dir}:{inst_env.get('PATH', '')}"
            inst_res = subprocess.run(
                ["sh", str(REPO_ROOT / "install.sh"), "--python", sys.executable, "--agent", "codex"],
                input="n\n",
                capture_output=True,
                text=True,
                env=inst_env,
                check=False,
            )
            self.assertEqual(inst_res.returncode, 0, msg=inst_res.stderr + inst_res.stdout)
            self.assertIn("[ok] codex:", inst_res.stdout)


class TestCodexProjectTrust(unittest.TestCase):
    def test_create_new_config_toml_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            proj = Path(tmp) / "my-hq"
            proj.mkdir()

            res1 = ensure_codex_projects_trusted([proj], codex_home=codex_home)
            self.assertTrue(res1.supported)
            self.assertTrue(res1.changed)
            self.assertTrue(res1.created)
            self.assertIsNone(res1.backup_path)
            self.assertEqual(res1.added, [str(proj.resolve())])
            self.assertEqual(res1.updated, [])

            cfg_path = codex_home / "config.toml"
            self.assertTrue(cfg_path.is_file())
            self.assertEqual(stat.S_IMODE(cfg_path.stat().st_mode), 0o600)
            parsed = _parse_toml(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["projects"][str(proj.resolve())]["trust_level"], "trusted")
            self.assertEqual(list(codex_home.glob("config.toml.bak-herdr-*")), [])

            # Second call is strictly idempotent: no rewrite, no backup, same mtime
            before_bytes = cfg_path.read_bytes()
            before_mtime = cfg_path.stat().st_mtime_ns
            res2 = ensure_codex_projects_trusted([proj], codex_home=codex_home)
            self.assertFalse(res2.changed)
            self.assertFalse(res2.created)
            self.assertIsNone(res2.backup_path)
            self.assertEqual(res2.already_trusted, [str(proj.resolve())])
            self.assertEqual(cfg_path.read_bytes(), before_bytes)
            self.assertEqual(cfg_path.stat().st_mtime_ns, before_mtime)
            self.assertEqual(list(codex_home.glob("config.toml.bak-herdr-*")), [])

    def test_preserve_comments_order_permissions_and_backup_on_modify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            cfg_path = codex_home / "config.toml"

            p_untrusted = (Path(tmp) / "repo-untrusted").resolve()
            p_partial = (Path(tmp) / "repo-partial").resolve()
            p_already = (Path(tmp) / "repo-already").resolve()
            p_new = (Path(tmp) / "中文 项目").resolve()
            for p in (p_untrusted, p_partial, p_already, p_new):
                p.mkdir()

            original = (
                "# Top user comment - must survive untouched\n"
                'model = "gpt-5-codex"\n'
                'model_reasoning_effort = "high" # inline comment\n'
                "\n"
                "[model_providers.custom]\n"
                'name = "custom"\n'
                'base_url = "https://example.invalid/v1"\n'
                "\n"
                f'[projects."{p_already}"]\n'
                'trust_level = "trusted"\n'
                "\n"
                f'[projects."{p_untrusted}"] # untrusted header comment\n'
                'trust_level = "untrusted" # keep inline comment\n'
                "\n"
                f'[projects."{p_partial}"]\n'
                'sandbox_mode = "workspace-write"\n'
                "\n"
                "[tui]\n"
                "# bottom section comment\n"
                "screen_reader_detection_done = true\n"
            )
            cfg_path.write_text(original, encoding="utf-8")
            os.chmod(cfg_path, 0o640)

            res = ensure_codex_projects_trusted(
                [p_already, p_untrusted, p_partial, p_new],
                codex_home=codex_home,
            )
            self.assertTrue(res.changed)
            self.assertFalse(res.created)
            self.assertEqual(res.already_trusted, [str(p_already)])
            self.assertEqual(res.updated, [str(p_untrusted), str(p_partial)])
            self.assertEqual(res.added, [str(p_new)])
            self.assertIsNotNone(res.backup_path)
            assert res.backup_path is not None
            self.assertTrue(res.backup_path.name.startswith("config.toml.bak-herdr-"))
            self.assertEqual(res.backup_path.read_text(encoding="utf-8"), original)

            # File permissions preserved
            self.assertEqual(stat.S_IMODE(cfg_path.stat().st_mode), 0o640)

            updated_text = cfg_path.read_text(encoding="utf-8")
            # All comments & ordering preserved
            self.assertIn("# Top user comment - must survive untouched", updated_text)
            self.assertIn('model_reasoning_effort = "high" # inline comment', updated_text)
            self.assertIn(f'[projects."{p_untrusted}"] # untrusted header comment', updated_text)
            self.assertIn('trust_level = "trusted" # keep inline comment', updated_text)
            self.assertIn("# bottom section comment", updated_text)
            self.assertLess(
                updated_text.index("[model_providers.custom]"),
                updated_text.index("[tui]"),
            )
            self.assertLess(
                updated_text.index("[tui]"),
                updated_text.index(f'[projects."{p_new}"]'),
            )

            parsed = _parse_toml(updated_text)
            for p in (p_already, p_untrusted, p_partial, p_new):
                self.assertEqual(parsed["projects"][str(p)]["trust_level"], "trusted")
            self.assertEqual(parsed["projects"][str(p_partial)]["sandbox_mode"], "workspace-write")

            # Re-running now is a complete no-op (no second backup created)
            res_again = ensure_codex_projects_trusted(
                [p_already, p_untrusted, p_partial, p_new],
                codex_home=codex_home,
            )
            self.assertFalse(res_again.changed)
            self.assertEqual(len(list(codex_home.glob("config.toml.bak-herdr-*"))), 1)

    def test_invalid_toml_rejected_and_post_write_failure_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            cfg_path = codex_home / "config.toml"
            proj = (Path(tmp) / "proj").resolve()
            proj.mkdir()

            # 1. Existing invalid TOML is rejected before touching file
            broken = 'model = "unterminated\n'
            cfg_path.write_text(broken, encoding="utf-8")
            with self.assertRaises(CodexConfigError):
                ensure_codex_projects_trusted([proj], codex_home=codex_home)
            self.assertEqual(cfg_path.read_text(encoding="utf-8"), broken)
            self.assertEqual(list(codex_home.glob("config.toml.bak-herdr-*")), [])

            # 2. Valid file + simulated invalid write triggers post-write rollback from backup
            valid_orig = '# user file\nmodel = "gpt-5-codex"\n'
            cfg_path.write_text(valid_orig, encoding="utf-8")
            os.chmod(cfg_path, 0o640)
            with patch(
                "hq_pilot.agents.codex._update_codex_config_text",
                return_value=('model = "broken\n[projects.\n', [str(proj)], []),
            ):
                with self.assertRaises(CodexConfigError):
                    ensure_codex_projects_trusted([proj], codex_home=codex_home)

            # Original content & mode restored atomically from backup
            self.assertEqual(cfg_path.read_text(encoding="utf-8"), valid_orig)
            self.assertEqual(stat.S_IMODE(cfg_path.stat().st_mode), 0o640)
            self.assertEqual(len(list(codex_home.glob("config.toml.bak-herdr-*"))), 1)

            # 3. Newly created file failing validation is removed cleanly
            fresh_home = Path(tmp) / "fresh-codex-home"
            with patch(
                "hq_pilot.agents.codex._update_codex_config_text",
                return_value=('not valid toml ===\n', [str(proj)], []),
            ):
                with self.assertRaises(CodexConfigError):
                    ensure_codex_projects_trusted([proj], codex_home=fresh_home)
            self.assertFalse((fresh_home / "config.toml").exists())

    def test_auto_trust_disabled_only_prints_manual_snippet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            hq_dir = (Path(tmp) / "manual-hq").resolve()

            res = ensure_codex_projects_trusted(
                [hq_dir],
                auto_trust=False,
                codex_home=codex_home,
            )
            self.assertTrue(res.supported)
            self.assertFalse(res.auto_trust)
            self.assertFalse(res.changed)
            self.assertFalse((codex_home / "config.toml").exists())
            self.assertIn(f'[projects."{hq_dir}"]', res.manual_snippet)
            self.assertIn('trust_level = "trusted"', res.manual_snippet)

            # setup --agent-backend codex --no-auto-trust-project writes switch=false and prints snippet
            out = io.StringIO()
            with (
                patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}),
                patch("sys.stdout", out),
            ):
                rc = hq_main(
                    [
                        "setup",
                        "--non-interactive",
                        "--hq-dir",
                        str(hq_dir),
                        "--project-name",
                        "manual-proj",
                        "--agent-backend",
                        "codex",
                        "--channel",
                        "local",
                        "--no-auto-trust-project",
                        "--python-bin",
                        sys.executable,
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertFalse((codex_home / "config.toml").exists())
            self.assertFalse(load_config(hq_dir).agent.auto_trust_project)
            stdout_text = out.getvalue()
            self.assertIn("agent.auto_trust_project=false", stdout_text)
            self.assertIn(f'[projects."{hq_dir}"]', stdout_text)
            self.assertIn('trust_level = "trusted"', stdout_text)

    def test_setup_and_daemon_auto_trust_hq_repo_and_worktree_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            codex_home = tmp_path / "codex-home"
            hq_dir = tmp_path / "hq"
            repo_dir = tmp_path / "app-repo"
            repo_dir.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_dir), check=True)
            (repo_dir / "README.md").write_text("# app\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)

            # 1. setup trusts both hq_dir and registered repo_dir
            with (
                patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}),
                patch("sys.stdout", io.StringIO()),
            ):
                rc = hq_main(
                    [
                        "setup",
                        "--non-interactive",
                        "--hq-dir",
                        str(hq_dir),
                        "--project-name",
                        "codex-multi",
                        "--repo",
                        f"app={repo_dir}",
                        "--agent-backend",
                        "codex",
                        "--channel",
                        "local",
                        "--python-bin",
                        sys.executable,
                    ]
                )
            self.assertEqual(rc, 0)
            cfg_path = codex_home / "config.toml"
            parsed = _parse_toml(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["projects"][str(hq_dir)]["trust_level"], "trusted")
            self.assertEqual(parsed["projects"][str(repo_dir)]["trust_level"], "trusted")

            # 2. daemon.spawn_task trusts newly created git worktree BEFORE starting codex
            term = DummyTerminal()
            ch = FakeChannel()
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                daemon = HQDaemon(hq_dir, terminal_driver=term, channel=ch)
                try:
                    # Remove config.toml to prove daemon also trusts HQ on _ensure_general_pane
                    cfg_path.unlink()
                    daemon._ensure_general_pane(now=1000)
                    parsed_gen = _parse_toml(cfg_path.read_text(encoding="utf-8"))
                    self.assertEqual(parsed_gen["projects"][str(hq_dir)]["trust_level"], "trusted")
                    self.assertEqual(parsed_gen["projects"][str(repo_dir)]["trust_level"], "trusted")

                    # Spawn a #Topic worktree task
                    sess = daemon.spawn_task("app", "fix-auth", "Fix auth flow", now=1010)
                    wt_path = Path(str(sess["worktree_path"])).resolve()
                    parsed_wt = _parse_toml(cfg_path.read_text(encoding="utf-8"))
                    self.assertEqual(parsed_wt["projects"][str(wt_path)]["trust_level"], "trusted")
                    self.assertEqual(parsed_wt["projects"][str(repo_dir)]["trust_level"], "trusted")
                    self.assertTrue((wt_path / ".codex" / "hooks.json").is_file())
                finally:
                    daemon.close()

            # 3. daemon with auto_trust_project=false does NOT touch config.toml and prints snippet
            codex_home_manual = tmp_path / "codex-home-manual"
            hq_yaml_path = hq_dir / "hq.yaml"
            hq_data = yaml.safe_load(hq_yaml_path.read_text(encoding="utf-8"))
            hq_data["agent"]["auto_trust_project"] = False
            hq_yaml_path.write_text(yaml.safe_dump(hq_data, sort_keys=False), encoding="utf-8")

            out_daemon = io.StringIO()
            term2 = DummyTerminal()
            with (
                patch.dict(os.environ, {"CODEX_HOME": str(codex_home_manual)}),
                patch("sys.stdout", out_daemon),
            ):
                daemon2 = HQDaemon(hq_dir, terminal_driver=term2, channel=FakeChannel())
                try:
                    daemon2._run_ephemeral_background_task(
                        "Check status", effort="low", now=1020, tag="check"
                    )
                finally:
                    daemon2.close()
            self.assertFalse((codex_home_manual / "config.toml").exists())
            self.assertIn("agent.auto_trust_project=false", out_daemon.getvalue())
            self.assertIn(f'[projects."{hq_dir}"]', out_daemon.getvalue())

    def test_linked_worktree_hooks_merge_main_repo_and_exclude_idempotently(self) -> None:
        adapter = CodexAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            repo = tmp_path / "main-repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo), check=True)
            (repo / "README.md").write_text("# main\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

            # Seed existing user hook in main repo (untracked) and custom exclude entry
            main_hooks = repo / ".codex" / "hooks.json"
            main_hooks.parent.mkdir(parents=True)
            main_hooks.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/user-pre"}]}
                            ],
                            "Stop": [
                                {"hooks": [{"type": "command", "command": "/usr/bin/user-stop"}]}
                            ],
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            exclude_file = repo / ".git" / "info" / "exclude"
            exclude_file.parent.mkdir(parents=True, exist_ok=True)
            exclude_file.write_text("# user exclude\n*.log\n", encoding="utf-8")

            wt = tmp_path / "hq" / ".hq" / "worktrees" / "main-repo-feat"
            wt.parent.mkdir(parents=True)
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "-b", "hq/feat", str(wt)],
                check=True,
                capture_output=True,
            )

            err_buf = io.StringIO()
            with patch("sys.stderr", err_buf):
                installed = adapter.ensure_hooks_installed(wt, "/opt/bin/hq-agent-hook")
            self.assertEqual(installed, wt / ".codex" / "hooks.json")
            self.assertEqual(err_buf.getvalue(), "")

            # Both worktree and main repo have merged hooks; main repo user hooks preserved
            wt_cfg = json.loads((wt / ".codex" / "hooks.json").read_text(encoding="utf-8"))
            main_cfg = json.loads(main_hooks.read_text(encoding="utf-8"))
            for ev in ("SessionStart", "UserPromptSubmit", "Stop"):
                self.assertIn(ev, wt_cfg["hooks"])
                self.assertIn(ev, main_cfg["hooks"])
            self.assertEqual(len(main_cfg["hooks"]["PreToolUse"]), 1)
            self.assertEqual(main_cfg["hooks"]["Stop"][0]["hooks"][0]["command"], "/usr/bin/user-stop")
            self.assertEqual(
                main_cfg["hooks"]["Stop"][1]["hooks"][0]["command"],
                "/opt/bin/hq-agent-hook --agent codex",
            )

            # info/exclude preserves existing rules and appends /.codex/hooks.json once
            excl_text = exclude_file.read_text(encoding="utf-8")
            self.assertIn("# user exclude", excl_text)
            self.assertIn("*.log", excl_text)
            self.assertEqual(
                [ln.strip() for ln in excl_text.splitlines()].count("/.codex/hooks.json"),
                1,
            )

            # Both main repo and linked worktree have clean git status
            st_main = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            st_wt = subprocess.run(
                ["git", "-C", str(wt), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(st_main, "")
            self.assertEqual(st_wt, "")

            # Re-running is strictly idempotent (no duplicate hooks or exclude lines)
            adapter.ensure_hooks_installed(wt, "/opt/bin/hq-agent-hook")
            self.assertEqual(json.loads(main_hooks.read_text(encoding="utf-8")), main_cfg)
            self.assertEqual(
                [ln.strip() for ln in exclude_file.read_text(encoding="utf-8").splitlines()].count(
                    "/.codex/hooks.json"
                ),
                1,
            )

    def test_tracked_codex_hooks_merges_and_emits_warning(self) -> None:
        adapter = CodexAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            repo = tmp_path / "tracked-repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo), check=True)

            tracked_hooks = repo / ".codex" / "hooks.json"
            tracked_hooks.parent.mkdir(parents=True)
            tracked_hooks.write_text(
                json.dumps(
                    {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/team-stop"}]}]}},
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", ".codex/hooks.json"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-m", "track hooks"], cwd=str(repo), check=True, capture_output=True)

            wt = tmp_path / "hq" / ".hq" / "worktrees" / "tracked-repo-feat"
            wt.parent.mkdir(parents=True)
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "-b", "hq/feat", str(wt)],
                check=True,
                capture_output=True,
            )

            err_buf = io.StringIO()
            with patch("sys.stderr", err_buf):
                adapter.ensure_hooks_installed(wt, "/opt/bin/hq-agent-hook")

            warn_text = err_buf.getvalue()
            self.assertIn("tracked by git", warn_text)
            self.assertIn(str(tracked_hooks), warn_text)
            self.assertIn(str(wt / ".codex" / "hooks.json"), warn_text)

            # Still merged while preserving team hook
            merged_main = json.loads(tracked_hooks.read_text(encoding="utf-8"))
            self.assertEqual(merged_main["hooks"]["Stop"][0]["hooks"][0]["command"], "/usr/bin/team-stop")
            self.assertEqual(
                merged_main["hooks"]["Stop"][1]["hooks"][0]["command"],
                "/opt/bin/hq-agent-hook --agent codex",
            )

            # Tracked file is NOT added to info/exclude
            exclude_file = repo / ".git" / "info" / "exclude"
            excl_text = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
            self.assertNotIn("/.codex/hooks.json", excl_text)

    def test_manual_codex_in_main_repo_never_misbinds_or_pushes_and_worktree_discovers_hq(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            hq_dir = tmp_path / "hq"
            hq_main(["init", str(hq_dir)])
            hq_yaml = hq_dir / "hq.yaml"
            hq_data = yaml.safe_load(hq_yaml.read_text(encoding="utf-8"))
            hq_data["agent"]["backend"] = "codex"
            hq_yaml.write_text(yaml.safe_dump(hq_data, sort_keys=False), encoding="utf-8")

            ext_repo = tmp_path / "external-main-repo"
            ext_repo.mkdir()
            rollout = tmp_path / "rollout-manual.jsonl"
            rollout.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in _sample_codex_0163_rollout()) + "\n",
                encoding="utf-8",
            )

            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                db.create_session("general:1", "GENERAL", "codex", status="active", pane_name="hq-general", now=100)
                db.create_session(
                    "task:app:feat:100",
                    "TOPIC",
                    "codex",
                    status="active",
                    repo_name="app",
                    topic_slug="feat",
                    pane_name="hq-feat",
                    now=100,
                )
            finally:
                db.close()

            ch = FakeChannel()
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in {"HQ_DIR", "HQ_SESSION_KEY", "HERDR_PANE_ID", "HERDR_WORKSPACE_ID", "HQ_EPHEMERAL"}
            }

            # 1. Manual codex in external main repo (no HQ_DIR / no HQ_SESSION_KEY): safely ignored!
            with patch.dict(os.environ, clean_env, clear=True):
                for ev_name in ("SessionStart", "UserPromptSubmit", "Stop"):
                    payload = {
                        "hook_event_name": ev_name,
                        "session_id": "manual-ext-sid",
                        "transcript_path": str(rollout),
                        "prompt": "manual edit",
                        "cwd": str(ext_repo),
                    }
                    res = handle_hook_event("codex", payload, hq_dir=None, channel=ch, now=120)
                    self.assertEqual(res["status"], "ignored")
                    self.assertEqual(res["reason"], "no_hq_dir")

                    out_buf = io.StringIO()
                    with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", out_buf):
                        self.assertEqual(hook_main(["--agent", "codex"]), 0)
                    self.assertEqual(json.loads(out_buf.getvalue()), {})

            self.assertEqual(ch.sent, [])
            db_chk1 = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                self.assertIsNone(db_chk1.get_session_by_agent_id("manual-ext-sid"))
                self.assertIsNone(db_chk1.get_session("general:1")["agent_session_id"])
                self.assertIsNone(db_chk1.get_session("task:app:feat:100")["agent_session_id"])
                self.assertEqual(db_chk1.list_meta_prefix("herdr_manual_pane:"), {})
            finally:
                db_chk1.close()

            # 2. Manual codex inside HQ/.hq/repos/app (no HQ_SESSION_KEY, no HERDR_PANE_ID):
            #    must NOT bind #General/#Topic, must NOT write manual pane binding, must NOT push to channel.
            in_hq_repo = hq_dir / ".hq" / "repos" / "app"
            in_hq_repo.mkdir(parents=True)
            with patch.dict(os.environ, clean_env, clear=True):
                for ev_name in ("SessionStart", "UserPromptSubmit", "Stop"):
                    payload = {
                        "hook_event_name": ev_name,
                        "session_id": "manual-inhq-sid",
                        "transcript_path": str(rollout),
                        "prompt": "local check",
                        "cwd": str(in_hq_repo),
                    }
                    res = handle_hook_event("codex", payload, hq_dir=None, channel=ch, now=130)
                    if ev_name == "Stop":
                        self.assertFalse(res.get("reply_pushed"))

            self.assertEqual(ch.sent, [])
            db_chk2 = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                self.assertIsNone(db_chk2.get_session("general:1")["agent_session_id"])
                self.assertIsNone(db_chk2.get_session("task:app:feat:100")["agent_session_id"])
                self.assertEqual(db_chk2.list_meta_prefix("herdr_manual_pane:"), {})
            finally:
                db_chk2.close()

            # 3. Managed worktree under HQ/.hq/worktrees/... discovers HQ_DIR from cwd even without HQ_DIR env
            wt_dir = hq_dir / ".hq" / "worktrees" / "app-feat"
            wt_dir.mkdir(parents=True)
            wt_env = dict(clean_env)
            wt_env["HQ_SESSION_KEY"] = "task:app:feat:100"
            with patch.dict(os.environ, wt_env, clear=True):
                res_wt = handle_hook_event(
                    "codex",
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "wt-topic-sid",
                        "transcript_path": str(rollout),
                        "cwd": str(wt_dir),
                    },
                    hq_dir=None,
                    channel=ch,
                    now=140,
                )
            self.assertEqual(res_wt["status"], "ok")
            self.assertEqual(res_wt["session_key"], "task:app:feat:100")
            db_chk3 = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                self.assertEqual(db_chk3.get_session("task:app:feat:100")["agent_session_id"], "wt-topic-sid")
            finally:
                db_chk3.close()


if __name__ == "__main__":
    unittest.main()
