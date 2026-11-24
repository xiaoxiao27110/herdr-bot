"""Abstract CodingAgentAdapter protocol for pluggable coding agent CLIs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


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


class CodingAgentAdapter(ABC):
    name: str
    index_filename: str

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

    def extra_trust_args(self, trusted_dirs: List[Path]) -> List[str]:
        """Optional per-session CLI flags to trust managed workspaces/worktrees."""
        return []
