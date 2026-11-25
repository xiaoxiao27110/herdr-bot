"""Standard Git CLI synchronization for the HQ memory repository."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import time


@dataclass
class SyncResult:
    committed: bool
    pushed: bool
    message: str


def sync_hq_repo(
    hq_dir: str | Path,
    commit_message: str | None = None,
    push: bool = True,
) -> SyncResult:
    root = Path(hq_dir).expanduser().resolve()
    if not (root / ".git").exists():
        return SyncResult(committed=False, pushed=False, message="Not a git repository")

    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True, capture_output=True, text=True)
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(root),
        check=False,
    )
    committed = False
    if diff.returncode != 0:
        msg = commit_message or time.strftime("herdr-bot: sync memory %Y-%m-%d %H:%M:%S")
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(root),
            check=True,
            capture_output=True,
            text=True,
        )
        committed = True

    pushed = False
    if push:
        remotes = subprocess.run(
            ["git", "remote"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        ).stdout.splitlines()
        if remotes:
            subprocess.run(
                ["git", "push"],
                cwd=str(root),
                check=True,
                capture_output=True,
                text=True,
            )
            pushed = True

    return SyncResult(
        committed=committed,
        pushed=pushed,
        message=f"committed={committed}, pushed={pushed}",
    )
