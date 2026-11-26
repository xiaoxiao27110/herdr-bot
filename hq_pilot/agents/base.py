"""Abstract CodingAgentAdapter protocol for pluggable coding agent CLIs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

HookEventKind = Literal["session_start", "user_prompt", "stop", "other"]


@dataclass
class Turn:
    role: str
    text: str
    timestamp: str = ""
    line_no: int = 0


@dataclass
class HeadlessResult:
    reply_markdown: str
    agent_session_id: Optional[str] = None


@dataclass
class HookEvent:
    """Normalized lifecycle hook event emitted by a coding agent CLI."""

    kind: HookEventKind
    raw_event_name: str = ""
    agent_session_id: str = ""
    transcript_path: str = ""
    user_prompt: Optional[str] = None
    cwd: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectTrustResult:
    """Outcome of ensuring project-level trust configuration for a coding agent."""

    supported: bool = False
    config_path: Optional[Path] = None
    auto_trust: bool = True
    changed: bool = False
    created: bool = False
    added: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    already_trusted: List[str] = field(default_factory=list)
    pending_manual: List[str] = field(default_factory=list)
    backup_path: Optional[Path] = None
    manual_snippet: str = ""


def detect_executable_bin(
    name: str,
    explicit: str = "",
    existing_override: str = "",
) -> str:
    """Locate an executable binary from explicit arg, override, PATH, or ~/.local/bin."""
    for candidate in (explicit, existing_override):
        if candidate:
            p = Path(candidate).expanduser()
            if p.is_file() and os.access(p, os.X_OK):
                return str(p.resolve())
            found = shutil.which(candidate)
            if found:
                return str(Path(found).resolve())
    if name:
        which_hit = shutil.which(name)
        if which_hit:
            return str(Path(which_hit).resolve())
        local_bin = Path.home() / ".local" / "bin" / name
        if local_bin.is_file() and os.access(local_bin, os.X_OK):
            return str(local_bin.resolve())
    return ""


class CodingAgentAdapter(ABC):
    name: str
    index_filename: str
    default_bin: str = ""

    @abstractmethod
    def build_headless_cmd(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        add_dirs: Optional[List[Path]] = None,
    ) -> List[str]:
        """Construct command for a single-turn headless invocation."""

    @abstractmethod
    def build_interactive_cmd(
        self,
        initial_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        add_dirs: Optional[List[Path]] = None,
    ) -> List[str]:
        """Construct command to launch or resume an interactive session."""

    @abstractmethod
    def parse_headless_output(self, stdout: str) -> HeadlessResult:
        """Extract unified reply markdown and agent session ID from stdout."""

    @abstractmethod
    def read_transcript_slice(
        self, transcript_path: str | Path, from_line: int = 0
    ) -> Tuple[List[Turn], int]:
        """Read normalized Turn slice starting at 0-based line index `from_line`."""

    @abstractmethod
    def count_transcript_lines(self, transcript_path: str | Path) -> int:
        """Return total number of lines in the transcript file."""

    @abstractmethod
    def ensure_hooks_installed(
        self, workspace_dir: str | Path, hook_bin: str
    ) -> Path:
        """Install lifecycle hooks for this coding agent in `workspace_dir`."""

    def extra_trust_args(
        self, trusted_dirs: List[Path], permission_mode: str = "default"
    ) -> List[str]:
        """Optional per-session CLI flags to trust managed workspaces/worktrees."""
        return []

    def ensure_project_trusted(
        self,
        project_dirs: Sequence[str | Path],
        *,
        auto_trust: bool = True,
    ) -> ProjectTrustResult:
        """Optional hook to persist project-level trust (e.g. Codex `$CODEX_HOME/config.toml`)."""
        _ = (project_dirs, auto_trust)
        return ProjectTrustResult(supported=False, auto_trust=auto_trust)

    def parse_hook_payload(self, payload: Dict[str, Any]) -> HookEvent:
        """Normalize raw hook JSON payload from this agent CLI into a HookEvent."""
        data = payload if isinstance(payload, dict) else {}
        raw_name = str(
            data.get("hookEventName")
            or data.get("hook_event_name")
            or data.get("event")
            or data.get("type")
            or ""
        )
        kind: HookEventKind
        if raw_name == "SessionStart":
            kind = "session_start"
        elif raw_name in {"PreInvocation", "UserPromptSubmit"}:
            kind = "user_prompt"
        elif raw_name == "Stop":
            kind = "stop"
        else:
            kind = "other"

        agent_sid = str(
            data.get("conversationId")
            or data.get("conversation_id")
            or data.get("session_id")
            or ""
        )
        transcript_path = str(
            data.get("transcriptPath") or data.get("transcript_path") or ""
        )
        raw_prompt = (
            data.get("lastUserInput")
            if data.get("lastUserInput") is not None
            else (
                data.get("prompt")
                if data.get("prompt") is not None
                else (
                    data.get("user_prompt")
                    if data.get("user_prompt") is not None
                    else data.get("input")
                )
            )
        )
        user_prompt = str(raw_prompt) if raw_prompt is not None else None

        cwd_val = data.get("cwd") or data.get("workspace")
        if (
            not cwd_val
            and isinstance(data.get("workspacePaths"), list)
            and data["workspacePaths"]
        ):
            cwd_val = data["workspacePaths"][0]
        cwd = str(cwd_val) if cwd_val else None

        return HookEvent(
            kind=kind,
            raw_event_name=raw_name,
            agent_session_id=agent_sid,
            transcript_path=transcript_path,
            user_prompt=user_prompt,
            cwd=cwd,
            raw=dict(data),
        )

    def render_hook_response(
        self,
        event: HookEvent,
        inject_messages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Render the stdout JSON response expected by this agent CLI's hook protocol."""
        return {}

    def detect_bin(self, explicit: str = "", existing_override: str = "") -> str:
        """Detect the host binary path for this coding agent."""
        binary_name = self.default_bin or getattr(self, "bin_path", "") or self.name
        return detect_executable_bin(
            binary_name,
            explicit=explicit,
            existing_override=existing_override,
        )

    def describe(self) -> Dict[str, Any]:
        """Return human-readable metadata about this adapter for setup/service/doctor."""
        return {
            "name": self.name,
            "default_bin": self.default_bin or self.name,
            "bin_path": getattr(self, "bin_path", self.default_bin or self.name),
            "index_filename": self.index_filename,
        }
