"""AgyAdapter: V1 CodingAgentAdapter implementation for `agy` CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import json

from hq_pilot.agents.base import CodingAgentAdapter, HeadlessResult, Turn


def _extract_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: List[str] = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                elif "content" in item:
                    parts.append(_extract_text(item["content"]))
        return "\n".join(p for p in parts if p)
    if isinstance(raw, dict):
        if "text" in raw:
            return str(raw["text"])
        if "parts" in raw:
            return _extract_text(raw["parts"])
        if "content" in raw:
            return _extract_text(raw["content"])
    return str(raw)


def _parse_transcript_line(obj: Dict[str, Any], line_no: int) -> Optional[Turn]:
    # New format: {"role": "user"|"assistant", "content": ..., "timestamp": ...}
    # Legacy format: {"type": "user"|"model"|"assistant", "message": {"role": ..., "content": ...}, "ts": ...}
    ts = str(obj.get("timestamp") or obj.get("ts") or obj.get("created_at") or "")
    role = obj.get("role")
    content = obj.get("content")
    if content is None:
        content = obj.get("text")

    if not role and isinstance(obj.get("message"), dict):
        msg = obj["message"]
        role = msg.get("role") or obj.get("type")
        content = msg.get("content") if msg.get("content") is not None else msg.get("parts")

    if not role:
        raw_type = str(obj.get("type") or "").lower()
        if raw_type in {"user", "human"}:
            role = "user"
        elif raw_type in {"assistant", "model", "agent"}:
            role = "assistant"
        elif raw_type == "system":
            role = "system"

    if not role:
        return None
    norm_role = str(role).lower()
    if norm_role in {"model", "agent"}:
        norm_role = "assistant"
    elif norm_role == "human":
        norm_role = "user"

    text = _extract_text(content).strip()
    if not text:
        return None
    return Turn(role=norm_role, text=text, timestamp=ts, line_no=line_no)


class AgyAdapter(CodingAgentAdapter):
    name = "agy"
    index_filename = "AGENTS.md"

    def __init__(self, bin_path: str = "agy") -> None:
        self.bin_path = bin_path

    def build_headless_cmd(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        add_dirs: Optional[List[Path]] = None,
    ) -> List[str]:
        cmd = [self.bin_path, "-p", "--output-format", "json"]
        if session_id:
            cmd.extend(["--conversation", str(session_id)])
        if model:
            cmd.extend(["--model", str(model)])
        if effort:
            cmd.extend(["--effort", str(effort)])
        for d in add_dirs or []:
            cmd.extend(["--add-dir", str(d)])
        cmd.append(prompt)
        return cmd

    def build_interactive_cmd(
        self,
        initial_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        add_dirs: Optional[List[Path]] = None,
    ) -> List[str]:
        cmd = [self.bin_path]
        if session_id:
            cmd.extend(["--conversation", str(session_id)])
        if model:
            cmd.extend(["--model", str(model)])
        if effort:
            cmd.extend(["--effort", str(effort)])
        for d in add_dirs or []:
            cmd.extend(["--add-dir", str(d)])
        if initial_prompt:
            cmd.extend(["-i", initial_prompt])
        return cmd

    def parse_headless_output(self, stdout: str) -> HeadlessResult:
        raw = (stdout or "").strip()
        if not raw:
            return HeadlessResult(reply_markdown="", agent_session_id=None)

        parsed: Optional[Dict[str, Any]] = None
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                parsed = obj
        except json.JSONDecodeError:
            for line in reversed(raw.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        parsed = obj
                        break
                except json.JSONDecodeError:
                    continue

        if parsed is None:
            return HeadlessResult(reply_markdown=raw, agent_session_id=None)

        reply = ""
        for key in ("reply", "response", "result", "text", "output", "message"):
            if key in parsed and parsed[key] is not None:
                reply = _extract_text(parsed[key])
                if reply:
                    break

        sid = None
        for key in ("conversationId", "conversation_id", "sessionId", "session_id"):
            if parsed.get(key):
                sid = str(parsed[key])
                break

        return HeadlessResult(reply_markdown=reply, agent_session_id=sid)

    def count_transcript_lines(self, transcript_path: str | Path) -> int:
        path = Path(transcript_path)
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8") as fh:
            return sum(1 for _ in fh)

    def read_transcript_slice(
        self, transcript_path: str | Path, from_line: int = 0
    ) -> Tuple[List[Turn], int]:
        path = Path(transcript_path)
        if not path.exists():
            return [], 0
        turns: List[Turn] = []
        total = 0
        with path.open("r", encoding="utf-8") as fh:
            for idx, line in enumerate(fh):
                total = idx + 1
                if idx < from_line:
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    t = _parse_transcript_line(obj, line_no=idx)
                    if t is not None:
                        turns.append(t)
        return turns, total

    def ensure_hooks_installed(
        self, workspace_dir: str | Path, hook_bin: str
    ) -> Path:
        ws = Path(workspace_dir)
        hooks_dir = ws / ".agents"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hooks_file = hooks_dir / "hooks.json"

        cmd = f"{hook_bin} --agent agy"
        config = {
            "hooks": {
                "SessionStart": [{"command": cmd}],
                "PreInvocation": [{"command": cmd}],
                "Stop": [{"command": cmd}],
            }
        }
        hooks_file.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return hooks_file
