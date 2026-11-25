"""0-Token Gatekeeper: boundary path matching, quiet-repo checks, manual-touch pinning, and distill scanning."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
import hashlib
import json
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


_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])"
)
_BORDER_LINE_RE = re.compile(r"^[\s─━═┄┅┈┉\-_=+|┌┐└┘├┤┬┴┼╭╮╰╯╔╗╚╝║]+$")
_TUI_CHROME_RE = re.compile(
    r"(?:\?\s*for\s+shortcuts|(?:^|\s)(?:auto|plan|ask)\s*·\s*)", re.IGNORECASE
)
_NUMBERED_OPT_RE = re.compile(
    r"^\s*(?:(?P<cursor>[❯›>▶▸►•●])\s*)?(?P<num>[1-9])[\.\)\]]\s+(?P<label>.+?)\s*$"
)


def parse_blocked_prompt(raw_screen: str) -> Dict[str, Any]:
    """Clean visible terminal output and parse interactive approval options & key sequences."""
    cleaned_raw = _ANSI_ESCAPE_RE.sub("", raw_screen or "").replace("\r\n", "\n").replace("\r", "")
    lines: List[str] = []
    for line in cleaned_raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _BORDER_LINE_RE.match(line):
            continue
        lines.append(line.rstrip())

    while lines:
        tail = lines[-1].strip()
        if tail in {">", "❯", "›"} or _TUI_CHROME_RE.search(tail):
            lines.pop()
            continue
        break

    recent_lines = lines[-20:]
    summary = "\n".join(recent_lines).strip()
    if len(summary) > 1200:
        summary = "...\n" + summary[-(1200 - 4):]

    # Detect the last contiguous numbered menu (2..4 options starting at 1) near the bottom
    groups: List[Dict[str, Any]] = []
    current_group: Optional[Dict[str, Any]] = None
    for idx, line in enumerate(recent_lines):
        m = _NUMBERED_OPT_RE.match(line)
        if not m:
            if current_group is not None and (idx - int(current_group["last_idx"])) > 2:
                groups.append(current_group)
                current_group = None
            continue
        num = int(m.group("num"))
        cursor = bool(m.group("cursor"))
        label = m.group("label").strip()
        if num == 1:
            if current_group is not None:
                groups.append(current_group)
            current_group = {
                "items": [{"num": 1, "label": label, "cursor": cursor}],
                "last_idx": idx,
                "overflow": False,
            }
        elif current_group is not None and num == len(current_group["items"]) + 1:
            if num > 4:
                current_group["overflow"] = True
            current_group["items"].append({"num": num, "label": label, "cursor": cursor})
            current_group["last_idx"] = idx
        else:
            if current_group is not None:
                groups.append(current_group)
                current_group = None
    if current_group is not None:
        groups.append(current_group)

    chosen_items: Optional[List[Dict[str, Any]]] = None
    n_recent = len(recent_lines)
    for grp in reversed(groups):
        if grp["overflow"]:
            continue
        items = grp["items"]
        if 2 <= len(items) <= 4 and (n_recent - 1 - int(grp["last_idx"])) <= 8:
            chosen_items = items
            break

    options: List[Dict[str, Any]] = []
    has_menu = False
    if chosen_items:
        has_menu = True
        highlighted_idx = 0
        for i, item in enumerate(chosen_items):
            if item["cursor"]:
                highlighted_idx = i
                break
        for i, item in enumerate(chosen_items):
            delta = i - highlighted_idx
            if delta == 0:
                keys = ["enter"]
            elif delta > 0:
                keys = ["down"] * delta + ["enter"]
            else:
                keys = ["up"] * (-delta) + ["enter"]
            short_lbl = " ".join(str(item["label"]).split())
            if len(short_lbl) > 28:
                short_lbl = short_lbl[:25] + "..."
            num = int(item["num"])
            options.append(
                {
                    "id": f"opt_{num}",
                    "label": f"{num}. {short_lbl}",
                    "keys": keys,
                    "negative": False,
                }
            )
        options.append(
            {
                "id": "cancel",
                "label": "❌ 取消",
                "keys": ["esc"],
                "negative": True,
            }
        )
    else:
        options = [
            {
                "id": "allow",
                "label": "✅ 允许",
                "keys": ["enter"],
                "negative": False,
            },
            {
                "id": "deny",
                "label": "❌ 拒绝",
                "keys": ["esc"],
                "negative": True,
            },
        ]

    screen_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:16]
    return {
        "summary": summary,
        "screen_hash": screen_hash,
        "options": options,
        "has_menu": has_menu,
    }


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
    notify_sec = int(config.tuning.pane_blocked_notify_sec)
    for sess in db.list_sessions(statuses=["active"]):
        if int(sess.get("pinned") or 0) == 1:
            continue
        pname = sess.get("pane_name")
        if not pname or pname not in panes:
            continue
        skey = str(sess["session_key"])
        pane = panes[pname]
        if pane.status != "blocked":
            if db.get_meta(f"blocked_approval_card:{skey}") is not None:
                db.delete_meta(f"blocked_approval_card:{skey}")
                prev_ep = int(db.get_meta(f"blocked_epoch:{skey}") or "0")
                db.set_meta(f"blocked_epoch:{skey}", str(prev_ep + 1), now=ts)
            if db.get_meta(f"blocked_last_resolved:{skey}") is not None:
                db.delete_meta(f"blocked_last_resolved:{skey}")
            continue
        last_active = int(sess.get("last_active_at") or 0)
        if notify_sec > 0 and (ts - last_active) < notify_sec:
            continue

        resolved_raw = db.get_meta(f"blocked_last_resolved:{skey}")
        if resolved_raw:
            try:
                resolved_info = json.loads(resolved_raw)
                r_at = int(resolved_info.get("resolved_at") or 0)
                r_hash = str(resolved_info.get("screen_hash") or "")
                if (ts - r_at) < 10:
                    raw_vis = terminal_driver.read_visible_pane(str(pname), lines=30)
                    cur_hash = parse_blocked_prompt(raw_vis)["screen_hash"]
                    if cur_hash == r_hash:
                        continue
            except Exception:
                pass

        epoch = (db.get_meta(f"blocked_epoch:{skey}") or "0").strip() or "0"
        eid = db.enqueue_event(
            source="pane_blocked",
            dedupe_key=f"pane_blocked:{skey}:{last_active}:{epoch}",
            priority=10,
            available_at=ts,
            payload={
                "session_key": skey,
                "pane_name": pname,
                "channel_thread_id": sess.get("channel_thread_id"),
                "repo_name": sess.get("repo_name"),
                "branch_name": sess.get("branch_name"),
            },
            now=ts,
        )
        if eid is not None:
            enqueued.append(skey)
    return enqueued
