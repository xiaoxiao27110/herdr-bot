"""0-Token Gatekeeper: boundary path matching, quiet-repo checks, manual-touch pinning, and distill scanning."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional
import re
import subprocess
import time

from hq_pilot.agents.base import CodingAgentAdapter
from hq_pilot.config import HQConfig, RepoBoundary
from hq_pilot.cursors import CursorStore
from hq_pilot.db import HQDatabase
from hq_pilot.terminals.base import TerminalDriver


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert a POSIX glob with ** support to a compiled regex."""
    pat = pattern.strip().lstrip("./")
    i = 0
    n = len(pat)
    out: List[str] = ["^"]
    while i < n:
        c = pat[i]
        if c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                if i + 2 < n and pat[i + 2] == "/":
                    out.append("(?:.+/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c in ".^$+{}[]|()\\":
            out.append("\\" + c)
        else:
            out.append(c)
        i += 1
    out.append("$")
    return re.compile("".join(out))


def glob_match(rel_path: str, pattern: str) -> bool:
    norm = rel_path.strip().lstrip("./").replace("\\", "/")
    return _glob_to_regex(pattern).match(norm) is not None


def match_boundary_paths(changed_files: List[str], boundary: RepoBoundary) -> List[str]:
    """Return subset of `changed_files` matching watch_paths and not excluded by ignore_paths."""
    if not boundary.watch_paths:
        return []
    matched: List[str] = []
    for f in changed_files:
        norm = f.strip().lstrip("./").replace("\\", "/")
        if not norm:
            continue
        if any(glob_match(norm, ig) for ig in boundary.ignore_paths):
            continue
        if any(glob_match(norm, wp) for wp in boundary.watch_paths):
            matched.append(norm)
    return matched


def is_repo_quiet(
    repo_path: Path, quiet_minutes: int, now: Optional[int] = None
) -> bool:
    """Return False if repo has uncommitted edits modified within `quiet_minutes`."""
    if not repo_path.exists():
        return True
    ts = int(now if now is not None else time.time())
    cutoff = ts - quiet_minutes * 60
    res = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return True
    for line in res.stdout.splitlines():
        if len(line) < 4:
            continue
        rel = line[3:].strip().split(" -> ")[-1].strip('"')
        target = repo_path / rel
        if target.exists():
            try:
                if target.stat().st_mtime >= cutoff:
                    return False
            except OSError:
                continue
    return True


def scan_repo_commits(
    config: HQConfig,
    cursors: CursorStore,
    db: HQDatabase,
    terminal_driver: Optional[TerminalDriver] = None,
    now: Optional[int] = None,
) -> List[str]:
    """Scan configured repos for new commits; skip non-matching commits at 0-token."""
    ts = int(now if now is not None else time.time())
    enqueued_repos: List[str] = []

    active_working_repos = set()
    if terminal_driver is not None:
        pane_names = {p.name for p in terminal_driver.list_panes() if p.status == "running"}
        for s in db.list_sessions(statuses=["active"]):
            if s.get("pane_name") in pane_names and s.get("repo_name"):
                active_working_repos.add(str(s["repo_name"]))

    repo_names = sorted(set(config.repos.keys()) | set(config.boundaries.repos.keys()))
    for repo_name in repo_names:
        repo_path = config.get_repo_path(repo_name)
        if not (repo_path / ".git").exists():
            continue

        if repo_name in active_working_repos:
            continue
        if not is_repo_quiet(repo_path, config.tuning.active_repo_quiet_minutes, now=ts):
            continue

        head_res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if head_res.returncode != 0:
            continue
        head_sha = head_res.stdout.strip()
        if not head_sha:
            continue

        last_sha = cursors.get_git_head(repo_name)
        if not last_sha:
            cursors.set_git_head(repo_name, head_sha)
            continue
        if last_sha == head_sha:
            continue

        diff_res = subprocess.run(
            ["git", "diff", "--name-only", f"{last_sha}..{head_sha}"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if diff_res.returncode != 0:
            cursors.set_git_head(repo_name, head_sha)
            continue

        changed_files = [l.strip() for l in diff_res.stdout.splitlines() if l.strip()]
        boundary = config.boundaries.repos.get(repo_name)
        matched = match_boundary_paths(changed_files, boundary) if boundary else []

        # Always advance git head cursor so we never re-scan the same commits
        cursors.set_git_head(repo_name, head_sha)

        if not matched:
            continue

        db.enqueue_event(
            source="git_commit",
            dedupe_key=f"git_commit:{repo_name}:{head_sha}",
            priority=20,
            available_at=ts + config.tuning.commit_debounce_sec,
            payload={
                "repo": repo_name,
                "from_sha": last_sha,
                "to_sha": head_sha,
                "matched_files": matched,
                "downstream": [
                    {"repo": d.repo, "contract": d.contract, "impact": d.impact}
                    for d in (boundary.downstream if boundary else [])
                ],
            },
            now=ts,
        )
        enqueued_repos.append(repo_name)
    return enqueued_repos


def detect_manual_touch(
    db: HQDatabase,
    session: Dict[str, object],
    terminal_driver: Optional[TerminalDriver] = None,
    input_text: Optional[str] = None,
    now: Optional[int] = None,
) -> Optional[str]:
    """Detect manual non-daemon input or terminal attach; auto-pin non-#General sessions."""
    ts = int(now if now is not None else time.time())
    session_key = str(session["session_key"])
    agent_sid = str(session.get("agent_session_id") or "") or None
    is_general = str(session.get("track") or "") in {"GENERAL", "B"}

    if input_text is not None and input_text.strip():
        is_daemon = db.is_daemon_dispatch(
            text=input_text,
            session_key=session_key,
            agent_session_id=agent_sid,
        )
        if not is_daemon:
            if not is_general:
                db.pin_session(session_key, reason="manual_input", now=ts)
            return "manual_input"

    pane_name = session.get("pane_name")
    if terminal_driver is not None and pane_name:
        if terminal_driver.is_client_attached(str(pane_name)):
            if not is_general:
                db.pin_session(session_key, reason="terminal_attach", now=ts)
            return "terminal_attach"

    return None


def scan_distill_candidates(
    db: HQDatabase,
    cursors: CursorStore,
    agent_adapter: CodingAgentAdapter,
    config: HQConfig,
    now: Optional[int] = None,
    force_all: bool = False,
) -> List[str]:
    """Scan active, pinned, and sealed sessions for unsummarized transcript slices."""
    ts = int(now if now is not None else time.time())
    enqueued: List[str] = []
    min_turns = 1 if force_all else config.tuning.distill_min_new_turns
    idle_sec = 0 if force_all else config.tuning.distill_idle_minutes * 60

    for sess in db.list_sessions(statuses=["active", "pinned", "sealed"]):
        agent_sid = sess.get("agent_session_id")
        tpath = sess.get("transcript_path")
        if not agent_sid or not tpath:
            continue
        last_active = int(sess.get("last_active_at") or 0)
        if (ts - last_active) < idle_sec:
            continue

        cur = cursors.get_transcript_cursor(str(agent_sid))
        from_line = int(cur["last_summarized_line"])
        turns, total_lines = agent_adapter.read_transcript_slice(str(tpath), from_line=from_line)
        if len(turns) < min_turns:
            continue

        dedupe_key = f"distill:{agent_sid}:{from_line}:{total_lines}"
        eid = db.enqueue_event(
            source="session_distill",
            dedupe_key=dedupe_key,
            priority=30,
            available_at=ts,
            payload={
                "session_key": sess["session_key"],
                "agent_session_id": str(agent_sid),
                "transcript_path": str(tpath),
                "from_line": from_line,
                "to_line": total_lines,
                "new_turns_count": len(turns),
                "turns": [
                    {"role": t.role, "text": t.text, "timestamp": t.timestamp}
                    for t in turns
                ],
            },
            now=ts,
        )
        if eid is not None:
            enqueued.append(str(sess["session_key"]))
    return enqueued


def scan_pane_blocked(
    db: HQDatabase,
    terminal_driver: TerminalDriver,
    config: HQConfig,
    now: Optional[int] = None,
) -> List[str]:
    """Enqueue pane_blocked events for non-pinned active panes blocked >= threshold."""
    ts = int(now if now is not None else time.time())
    panes = {p.name: p for p in terminal_driver.list_panes()}
    enqueued: List[str] = []
    for sess in db.list_sessions(statuses=["active"]):
        if int(sess.get("pinned") or 0) == 1:
            continue
        pname = sess.get("pane_name")
        if not pname or pname not in panes:
            continue
        pane = panes[pname]
        if pane.status != "blocked":
            continue
        last_active = int(sess.get("last_active_at") or 0)
        if (ts - last_active) < config.tuning.pane_blocked_notify_sec:
            continue
        eid = db.enqueue_event(
            source="pane_blocked",
            dedupe_key=f"pane_blocked:{sess['session_key']}:{last_active}",
            priority=10,
            available_at=ts,
            payload={
                "session_key": sess["session_key"],
                "pane_name": pname,
                "channel_thread_id": sess.get("channel_thread_id"),
                "repo_name": sess.get("repo_name"),
                "branch_name": sess.get("branch_name"),
            },
            now=ts,
        )
        if eid is not None:
            enqueued.append(str(sess["session_key"]))
    return enqueued
