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
            active_panes = {
                str(s["pane_name"])
                for s in db.list_sessions(statuses=["active", "pinned"])
                if s.get("pane_name") and str(s["session_key"]) != skey
            }
            if str(pane_name) not in active_panes:
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
                        reason_detail.append(f"存在未提交修改：{status_out}")
                    if unpushed:
                        reason_detail.append(f"存在未推送提交：{unpushed_out}")
                    slug_label = str(sess.get("topic_slug") or skey)
                    reason_text = "；".join(reason_detail)
                    db.update_session(
                        skey,
                        archive_blocked_reason="dirty_or_unpushed",
                    )
                    db.add_digest_item(
                        level="alert",
                        category="dirty_worktree",
                        message=(
                            f"工作区 `{slug_label}` 存在未提交修改/未推送提交，"
                            f"已跳过夜间清理（原因：{reason_text}）"
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
                f"会话 `{skey}` 已完成夜间归档。\n"
                f"已保留分支：`{branch_name or '无'}`\n"
                f"随时可通过以下命令恢复：`hq resume {skey}`"
            )
            try:
                channel.send_message(receipt, thread_id=str(thread_id))
                channel.close_thread(str(thread_id))
            except Exception as exc:
                report.errors.append(f"{skey} channel receipt: {exc}")

    return report
