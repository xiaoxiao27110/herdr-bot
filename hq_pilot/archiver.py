"""Nightly safe archiver (23:30) enforcing the 4-never-delete rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
import time

from hq_pilot.channels.base import MessagingChannel
from hq_pilot.config import HQConfig
from hq_pilot.db import HQDatabase
from hq_pilot.terminals.base import (
    TerminalDriver,
    git_worktree_remove,
    has_unpushed_commits,
    is_worktree_clean,
)


@dataclass
class ArchiveReport:
    archived: List[str] = field(default_factory=list)
    blocked_dirty: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def archive_eligible_sessions(
    config: HQConfig,
    db: HQDatabase,
    terminal_driver: Optional[TerminalDriver] = None,
    channel: Optional[MessagingChannel] = None,
    now: Optional[int] = None,
) -> ArchiveReport:
    """
    Archive only `status='sealed' AND pinned=0 AND suspended_msg_id IS NULL` sessions.
    Never removes dirty/unpushed worktrees, never uses --force, never deletes branches or transcripts.
    """
    ts = int(now if now is not None else time.time())
    report = ArchiveReport()
    candidates = db.list_archivable_sessions()

    for sess in candidates:
        skey = str(sess["session_key"])
        pane_name = sess.get("pane_name")
        worktree_str = sess.get("worktree_path")
        repo_name = sess.get("repo_name")
        branch_name = sess.get("branch_name") or ""
        thread_id = sess.get("channel_thread_id")

        if terminal_driver is not None and pane_name:
            terminal_driver.kill_pane(str(pane_name))

        if worktree_str:
            wt_path = Path(str(worktree_str))
            if wt_path.exists():
                clean, status_out = is_worktree_clean(wt_path)
                default_branch = "main"
                if repo_name and str(repo_name) in config.repos:
                    default_branch = config.repos[str(repo_name)].default_branch
                unpushed, unpushed_out = has_unpushed_commits(
                    wt_path, default_branch=default_branch
                )
                if not clean or unpushed:
                    reason_detail = []
                    if not clean:
                        reason_detail.append(f"uncommitted changes: {status_out}")
                    if unpushed:
                        reason_detail.append(f"unpushed commits: {unpushed_out}")
                    db.update_session(
                        skey,
                        archive_blocked_reason="dirty_or_unpushed",
                    )
                    db.add_digest_item(
                        level="alert",
                        category="dirty_worktree",
                        message=(
                            f"[ALERT] Session `{skey}` (branch `{branch_name}`) "
                            f"blocked from nightly archive: {'; '.join(reason_detail)}. "
                            f"Worktree preserved at `{wt_path}`."
                        ),
                        now=ts,
                    )
                    report.blocked_dirty.append(skey)
                    continue

                if repo_name:
                    repo_path = config.get_repo_path(str(repo_name))
                    try:
                        git_worktree_remove(repo_path, wt_path)
                    except Exception as exc:
                        report.errors.append(f"{skey}: {exc}")
                        continue

        db.archive_session(skey, now=ts)
        report.archived.append(skey)

        if channel is not None and thread_id:
            receipt = (
                f"Archived session `{skey}`.\n"
                f"Branch preserved: `{branch_name or '(none)'}`\n"
                f"Resume anytime with: `hq resume {skey}`"
            )
            try:
                channel.send_message(receipt, thread_id=str(thread_id))
                channel.close_thread(str(thread_id))
            except Exception as exc:
                report.errors.append(f"{skey} channel receipt: {exc}")

    return report
