"""Parameterized adapter contract tests, open registry tests, and agy regression locks."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pytest

from hq_pilot.agents.agy import AgyAdapter
from hq_pilot.agents.base import CodingAgentAdapter, HeadlessResult, HookEvent, Turn
from hq_pilot.agents.registry import (
    _ADAPTER_REGISTRY,
    get_agent_adapter,
    list_agent_backends,
    register_agent_adapter,
)
from hq_pilot.cli import main as hq_main
from hq_pilot.db import HQDatabase
from hq_pilot.hook_cli import main as hook_main


class SampleDynamicAdapter(CodingAgentAdapter):
    """Test adapter loaded dynamically via `module:ClassName`."""

    name = "sample-dynamic"
    default_bin = "sample-cli"
    index_filename = "SAMPLE.md"

    def __init__(self, bin_path: str = "sample-cli", extra_flag: str = "") -> None:
        self.bin_path = bin_path
        self.extra_flag = extra_flag

    def build_headless_cmd(self, prompt, session_id=None, model=None, effort=None, add_dirs=None):
        cmd = [self.bin_path, "run"]
        if self.extra_flag:
            cmd.append(self.extra_flag)
        cmd.append(prompt)
        return cmd

    def build_interactive_cmd(self, initial_prompt=None, session_id=None, model=None, effort=None, add_dirs=None):
        return [self.bin_path]

    def parse_headless_output(self, stdout: str) -> HeadlessResult:
        return HeadlessResult(reply_markdown=stdout.strip(), agent_session_id=None)

    def read_transcript_slice(self, transcript_path, from_line: int = 0):
        return [], 0

    def count_transcript_lines(self, transcript_path) -> int:
        return 0

    def ensure_hooks_installed(self, workspace_dir, hook_bin: str) -> Path:
        p = Path(workspace_dir) / ".sample" / "hooks.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"hook_bin": hook_bin}), encoding="utf-8")
        return p


class NotAnAdapter:
    pass


@pytest.mark.parametrize("backend", list_agent_backends())
def test_builtin_adapter_contract(backend: str, tmp_path: Path) -> None:
    adapter = get_agent_adapter(backend, bin_path=f"/opt/bin/{backend}")
    assert isinstance(adapter, CodingAgentAdapter)
    assert adapter.name == backend
    assert isinstance(adapter.index_filename, str) and adapter.index_filename
    assert isinstance(adapter.default_bin, str) and adapter.default_bin

    desc = adapter.describe()
    assert desc["name"] == backend
    assert desc["index_filename"] == adapter.index_filename
    assert desc["default_bin"] == adapter.default_bin

    # Commands
    hcmd = adapter.build_headless_cmd(
        "hello world",
        session_id="sid-1",
        model="m-test",
        effort="low",
        add_dirs=[tmp_path / "extra"],
    )
    assert hcmd[0] == f"/opt/bin/{backend}"
    assert "hello world" in hcmd
    assert "sid-1" in hcmd

    icmd = adapter.build_interactive_cmd(
        initial_prompt="start",
        session_id="sid-2",
        model="m-test",
        effort="high",
        add_dirs=[tmp_path / "extra"],
    )
    assert icmd[0] == f"/opt/bin/{backend}"
    assert "start" in icmd
    assert "sid-2" in icmd

    for mode in ("default", "accept-edits", "turbo", "skip-all"):
        trust_args = adapter.extra_trust_args([tmp_path], permission_mode=mode)
        assert isinstance(trust_args, list)
        assert all(isinstance(x, str) for x in trust_args)

    # Headless parsing edge cases
    empty = adapter.parse_headless_output("")
    assert isinstance(empty, HeadlessResult)
    assert empty.reply_markdown == ""
    assert empty.agent_session_id is None

    plain = adapter.parse_headless_output("non-json fallback text")
    assert plain.reply_markdown == "non-json fallback text"

    # Missing transcript file
    missing = tmp_path / "does-not-exist.jsonl"
    assert adapter.count_transcript_lines(missing) == 0
    assert adapter.read_transcript_slice(missing, from_line=0) == ([], 0)

    # Hooks install is valid JSON and idempotent
    ws = tmp_path / f"ws-{backend}"
    ws.mkdir()
    hpath1 = adapter.ensure_hooks_installed(ws, "/usr/local/bin/hq-agent-hook")
    assert hpath1.is_file()
    raw1 = hpath1.read_text(encoding="utf-8")
    parsed1 = json.loads(raw1)
    assert isinstance(parsed1, dict)

    hpath2 = adapter.ensure_hooks_installed(ws, "/usr/local/bin/hq-agent-hook")
    assert hpath2 == hpath1
    assert json.loads(hpath2.read_text(encoding="utf-8")) == parsed1

    # Hook normalization & response rendering are valid JSON dicts
    for ev_payload in (
        {"hookEventName": "SessionStart", "hook_event_name": "SessionStart", "session_id": "s1", "conversationId": "s1"},
        {"hookEventName": "Stop", "hook_event_name": "Stop", "session_id": "s1", "conversationId": "s1"},
    ):
        ev = adapter.parse_hook_payload(ev_payload)
        assert isinstance(ev, HookEvent)
        assert ev.kind in {"session_start", "user_prompt", "stop", "other"}
        resp = adapter.render_hook_response(ev, ["ctx"])
        assert isinstance(resp, dict)
        json.dumps(resp)


class TestRegistryAndAgyRegression(unittest.TestCase):
    def test_builtin_registry_lists_agy_and_codex(self) -> None:
        backends = list_agent_backends()
        self.assertIn("agy", backends)
        self.assertIn("codex", backends)

    def test_decorator_registration_and_validation(self) -> None:
        key = "unit-temp-adapter"
        try:
            @register_agent_adapter(key)
            class _TempAdapter(SampleDynamicAdapter):
                name = key

            inst = get_agent_adapter(key, bin_path="/tmp/custom-bin")
            self.assertIsInstance(inst, _TempAdapter)
            self.assertEqual(inst.bin_path, "/tmp/custom-bin")
        finally:
            _ADAPTER_REGISTRY.pop(key, None)

        with self.assertRaises(ValueError):
            register_agent_adapter("")

        with self.assertRaises(TypeError):
            register_agent_adapter("bad-cls")(NotAnAdapter)  # type: ignore[arg-type]

    def test_dynamic_module_class_loading_and_error_paths(self) -> None:
        spec = f"{SampleDynamicAdapter.__module__}:SampleDynamicAdapter"
        inst = get_agent_adapter(spec, bin_path="/opt/sample", extra_flag="--fast")
        self.assertIsInstance(inst, SampleDynamicAdapter)
        self.assertEqual(inst.bin_path, "/opt/sample")
        self.assertEqual(inst.extra_flag, "--fast")
        self.assertEqual(inst.build_headless_cmd("hi"), ["/opt/sample", "run", "--fast", "hi"])

        # Unknown built-in lists available backends
        with self.assertRaises(ValueError) as ctx:
            get_agent_adapter("nonexistent-backend")
        self.assertIn("agy", str(ctx.exception))
        self.assertIn("codex", str(ctx.exception))

        # Malformed dynamic specs
        with self.assertRaises(ValueError):
            get_agent_adapter("only.module:")
        with self.assertRaises(ValueError):
            get_agent_adapter(":OnlyClass")

        # Missing module / missing class / non-subclass
        with self.assertRaises(ValueError):
            get_agent_adapter("no_such_module_xyz_123:Adapter")
        with self.assertRaises(ValueError):
            get_agent_adapter(f"{SampleDynamicAdapter.__module__}:DoesNotExist")
        with self.assertRaises(TypeError):
            get_agent_adapter(f"{NotAnAdapter.__module__}:NotAnAdapter")

    def test_agy_hook_stdout_json_regression_lock(self) -> None:
        """Lock exact agy hook JSON byte semantics across SessionStart, PreInvocation, Stop, and handoff."""
        adapter = AgyAdapter()
        self.assertEqual(
            adapter.render_hook_response(HookEvent(kind="stop", raw_event_name="Stop"), []),
            {"decision": "allow_stop"},
        )
        self.assertEqual(
            adapter.render_hook_response(HookEvent(kind="user_prompt", raw_event_name="PreInvocation"), []),
            {"injectSteps": []},
        )
        self.assertEqual(
            adapter.render_hook_response(
                HookEvent(kind="user_prompt", raw_event_name="PreInvocation"),
                ["[昨日会话交接摘要]\n要点A"],
            ),
            {"injectSteps": [{"systemMessage": {"systemMessage": "[昨日会话交接摘要]\n要点A"}}]},
        )

        # End-to-end hook_main stdout check (with handoff + no_hq_dir Stop fallback)
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = Path(tmp) / "agy-hq"
            hq_main(["init", str(hq_dir)])
            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                db.create_session("general:1", "GENERAL", "agy", status="active", now=100)
                db.set_meta("general_handoff_pending", "1", now=100)
                db.set_meta("general_handoff_summary", "昨日推进总结", now=100)
                db.set_meta("general_handoff_reason", "night", now=100)
            finally:
                db.close()

            with patch.dict(os.environ, {"HQ_SESSION_KEY": "general:1"}):
                out_pre = io.StringIO()
                pre_in = json.dumps(
                    {
                        "hookEventName": "PreInvocation",
                        "conversationId": "conv-agy-lock",
                        "lastUserInput": "<USER_REQUEST>早安</USER_REQUEST>",
                    }
                )
                with patch("sys.stdin", io.StringIO(pre_in)), patch("sys.stdout", out_pre):
                    self.assertEqual(hook_main(["--agent", "agy", "--hq-dir", str(hq_dir)]), 0)
                self.assertEqual(
                    out_pre.getvalue().strip(),
                    json.dumps(
                        {
                            "injectSteps": [
                                {"systemMessage": {"systemMessage": "[昨日会话交接摘要]\n昨日推进总结"}}
                            ]
                        },
                        ensure_ascii=False,
                    ),
                )

                out_stop = io.StringIO()
                stop_in = json.dumps(
                    {"hookEventName": "Stop", "conversationId": "conv-agy-lock"}
                )
                with patch("sys.stdin", io.StringIO(stop_in)), patch("sys.stdout", out_stop):
                    self.assertEqual(hook_main(["--agent", "agy", "--hq-dir", str(hq_dir)]), 0)
                self.assertEqual(out_stop.getvalue().strip(), '{"decision": "allow_stop"}')

            # Fallback without --agent and without hq.yaml defaults to agy semantics
            out_fallback = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=False),
                patch("sys.stdin", io.StringIO(json.dumps({"hookEventName": "Stop"}))),
                patch("sys.stdout", out_fallback),
            ):
                os.environ.pop("HQ_DIR", None)
                self.assertEqual(hook_main([]), 0)
            self.assertEqual(out_fallback.getvalue().strip(), '{"decision": "allow_stop"}')


if __name__ == "__main__":
    unittest.main()
