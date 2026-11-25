"""Abstract TerminalDriver protocol and standard Git worktree helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import shlex
import subprocess


class AgentBlockedError(RuntimeError):
    """Raised when an interactive agent pane enters `blocked` status (awaiting approval)."""

    def __init__(self, pane_name: str, detail: str = "") -> None:
        self.pane_name = pane_name
        self.detail = detail
        msg = f"Agent pane {pane_name!r} is blocked"
        if detail:
            msg = f"{msg}: {detail}"
        super().__init__(msg)


@dataclass
class PaneInfo:
    name: str
    status: str = "running"  # running | blocked | exited | unknown
    attached: bool = False
    cwd: str = ""


class TerminalDriver(ABC):
    name: str

    @abstractmethod
    def run_bg(
        self,
        name: str,
        cwd: str | Path,
        cmd: List[str],
    ) -> None:
        """Start a command in a persistent background pane/session."""

    @abstractmethod
    def list_panes(self) -> List[PaneInfo]:
        """List active managed panes/sessions."""

    @abstractmethod
    def send_text(self, name: str, text: str, enter: bool = True) -> None:
        """Send input text to a running pane/session."""

    @abstractmethod
    def read_pane(self, name: str, lines: int = 80) -> str:
        """Read recent output from a pane/session."""

    @abstractmethod
    def attach_cmd(self, name: str) -> List[str]:
        """Return CLI command to attach interactively to a pane/session."""

    @abstractmethod
    def kill_pane(self, name: str) -> None:
        """Gracefully terminate a pane/session."""

    @abstractmethod
    def is_client_attached(self, name: str) -> bool:
        """Return True if a human terminal client is currently attached."""

    def start_agent(
        self,
        name: str,
        cwd: str | Path,
        kind: str,
        agent_args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        timeout_ms: int = 60000,
    ) -> None:
        """Start an interactive coding agent in a persistent managed pane/session."""
        cmd: List[str] = []
        if env:
            cmd.append("env")
            for k, v in sorted(env.items()):
                cmd.append(f"{k}={v}")
        cmd.append(kind)
        if agent_args:
            cmd.extend(agent_args)
        self.run_bg(name=name, cwd=cwd, cmd=cmd)

    def prompt_agent(
        self,
        name: str,
        text: str,
        wait: bool = True,
        timeout_ms: int = 300000,
    ) -> str:
        """Submit a prompt to an interactive agent pane and optionally wait for completion."""
        self.send_text(name=name, text=text, enter=True)
        return ""


def _run_git(cwd: Path, args: List[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def git_worktree_add(
    repo_path: str | Path,
    worktree_path: str | Path,
    branch_name: str,
    base_branch: Optional[str] = None,
) -> Path:
    """Create or re-attach a git worktree for `branch_name` without force-deleting anything."""
    repo = Path(repo_path).resolve()
    wt = Path(worktree_path).resolve()
    wt.parent.mkdir(parents=True, exist_ok=True)
    if wt.exists():
        return wt

    # Check if branch already exists locally
    branch_exists = (
        _run_git(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"], check=False).returncode
        == 0
    )
    if branch_exists:
        _run_git(repo, ["worktree", "add", str(wt), branch_name], check=True)
    else:
        cmd = ["worktree", "add", "-b", branch_name, str(wt)]
        if base_branch:
            cmd.append(base_branch)
        _run_git(repo, cmd, check=True)
    return wt


def git_worktree_remove(repo_path: str | Path, worktree_path: str | Path) -> None:
    """Safely remove a clean git worktree (never uses --force)."""
    repo = Path(repo_path).resolve()
    wt = Path(worktree_path).resolve()
    if not wt.exists():
        return
    _run_git(repo, ["worktree", "remove", str(wt)], check=True)


def is_worktree_clean(worktree_path: str | Path) -> Tuple[bool, str]:
    """Return (True, '') if worktree has no modified/untracked files; else (False, status_output)."""
    wt = Path(worktree_path).resolve()
    if not wt.exists():
        return True, ""
    res = _run_git(wt, ["status", "--porcelain"], check=True)
    out = res.stdout.strip()
    return (out == "", out)


def has_unpushed_commits(
    worktree_path: str | Path, default_branch: str = "main"
) -> Tuple[bool, str]:
    """Check if HEAD has local commits not in origin or default_branch."""
    wt = Path(worktree_path).resolve()
    if not wt.exists():
        return False, ""

    # Check if origin remote exists
    remotes = _run_git(wt, ["remote"], check=False).stdout.splitlines()
    if "origin" in [r.strip() for r in remotes]:
        res = _run_git(
            wt,
            ["rev-list", "HEAD", "--not", "--remotes=origin", default_branch],
            check=False,
        )
        if res.returncode == 0:
            commits = res.stdout.strip()
            return (commits != "", commits)

    # Fallback when no origin remote: compare against local default_branch
    has_default = (
        _run_git(wt, ["show-ref", "--verify", "--quiet", f"refs/heads/{default_branch}"], check=False).returncode
        == 0
    )
    if has_default:
        res = _run_git(wt, ["rev-list", f"{default_branch}..HEAD"], check=False)
        if res.returncode == 0:
            commits = res.stdout.strip()
            return (commits != "", commits)
    return False, ""


def get_current_branch(worktree_path: str | Path) -> str:
    wt = Path(worktree_path).resolve()
    res = _run_git(wt, ["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    return res.stdout.strip() if res.returncode == 0 else ""


def shell_join(cmd: List[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)
