"""Pluggable terminal drivers and git worktree helpers."""

from hq_pilot.terminals.base import (
    PaneInfo,
    TerminalDriver,
    get_current_branch,
    git_worktree_add,
    git_worktree_remove,
    has_unpushed_commits,
    is_worktree_clean,
)
from hq_pilot.terminals.herdr import HerdrDriver, install_herdr_guide
from hq_pilot.terminals.registry import get_terminal_driver
from hq_pilot.terminals.tmux import TmuxDriver

__all__ = [
    "PaneInfo",
    "TerminalDriver",
    "HerdrDriver",
    "TmuxDriver",
    "install_herdr_guide",
    "get_terminal_driver",
    "git_worktree_add",
    "git_worktree_remove",
    "is_worktree_clean",
    "has_unpushed_commits",
    "get_current_branch",
]
