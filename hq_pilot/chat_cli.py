"""Interactive local terminal chat (`hq chat`) backed by LocalChannel SQLite tables."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from hq_pilot.config import load_config
from hq_pilot.daemon import HQDaemon
from hq_pilot.db import HQDatabase


HELP_TEXT = """Commands:
  <text>                  Send message to current topic (#General by default)
  /topics                 List all local topics and status
  /general                Switch back to #General
  /topic <thread_id>      Switch to a specific topic (e.g. /topic topic-1)
  /pick <msg_id> <idx>    Click option <idx> (1-based) on a card message
  /reply <msg_id> <text>  Reply to a specific card message
  /task <repo> <slug> <prompt>  Spawn a Track C task (sent to daemon)
  /done                   Seal current topic session (sent to daemon)
  /new                    Start a new session (sent to daemon)
  /help                   Show this help message
  /quit                   Exit chat (also Ctrl-D / Ctrl-C)"""


def is_daemon_running(hq_dir: str | Path) -> Optional[int]:
    """Return active daemon PID if `.hq/daemon.pid` points to a live process."""
    pid_file = Path(hq_dir).expanduser().resolve() / ".hq" / "daemon.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        if pid <= 0:
            return None
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def parse_msg_id(raw: str) -> Optional[int]:
    """Parse message id from `12`, `local:12`, `msg#12`, or `#12`."""
    s = (raw or "").strip()
    for prefix in ("local:", "msg#", "#"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    if not s.isdigit():
        return None
    return int(s)


def _flatten_buttons(buttons_json: Optional[str]) -> List[Dict[str, str]]:
    if not buttons_json:
        return []
    try:
        rows = json.loads(buttons_json)
    except json.JSONDecodeError:
        return []
    flat: List[Dict[str, str]] = []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, list):
                for btn in row:
                    if isinstance(btn, dict):
                        flat.append(
                            {
                                "text": str(btn.get("text") or ""),
                                "callback_data": str(btn.get("callback_data") or ""),
                            }
                        )
    return flat


def format_outbound_message(row: Dict[str, Any]) -> str:
    """Format an outbound `local_messages` row for terminal display."""
    msg_id = row["id"]
    tid = row.get("thread_id")
    thread_label = f"#{tid}" if tid else "#General"
    lines = [f"[msg#{msg_id}][{thread_label}] {row.get('text', '')}"]
    buttons = _flatten_buttons(row.get("buttons_json"))
    if buttons:
        opts = "  ".join(f"[{idx}] {b['text']}" for idx, b in enumerate(buttons, start=1))
        lines.append(f"   Options: {opts}")
    return "\n".join(lines)


class ChatSession:
    """Stateful command & message handler for `hq chat`."""

    def __init__(self, db: HQDatabase) -> None:
        self.db = db
        self.current_thread_id: Optional[str] = None

    def prompt_label(self) -> str:
        label = f"#{self.current_thread_id}" if self.current_thread_id else "#General"
        return f"[{label}] > "

    def handle_line(self, raw_line: str, now: Optional[int] = None) -> Tuple[bool, List[str]]:
        line = (raw_line or "").strip()
        if not line:
            return True, []

        if line in {"/quit", "/exit"}:
            return False, ["Bye."]

        if line == "/help":
            return True, [HELP_TEXT]

        if line == "/general":
            self.current_thread_id = None
            return True, ["Switched to #General"]

        if line == "/topics":
            threads = self.db.list_local_threads()
            if not threads:
                return True, ["No local topics yet."]
            out = ["Topics:"]
            for t in threads:
                state = "closed" if int(t.get("closed") or 0) else "open"
                out.append(f"  - #{t['thread_id']} | {t['title']} ({state})")
            return True, out

        if line == "/topic" or line.startswith("/topic "):
            arg = line[6:].strip()
            if not arg:
                return True, ["Usage: /topic <thread_id>"]
            tid = arg[1:] if arg.startswith("#") else arg
            self.current_thread_id = tid
            return True, [f"Switched to #{tid}"]

        if line == "/pick" or line.startswith("/pick "):
            parts = line.split()
            if len(parts) != 3 or not parts[2].isdigit():
                return True, ["Usage: /pick <msg_id> <choice_idx>"]
            mid = parse_msg_id(parts[1])
            choice_idx = int(parts[2])
            if mid is None:
                return True, [f"Invalid message id: {parts[1]}"]
            msg_row = self.db.get_local_message(mid)
            if not msg_row:
                return True, [f"Message msg#{mid} not found."]
            buttons = _flatten_buttons(msg_row.get("buttons_json"))
            if not buttons:
                return True, [f"Message msg#{mid} has no selectable options."]
            if choice_idx < 1 or choice_idx > len(buttons):
                return True, [f"Choice index {choice_idx} out of range (1..{len(buttons)})."]
            chosen = buttons[choice_idx - 1]
            target_thread = msg_row.get("thread_id") or self.current_thread_id
            self.db.add_local_message(
                direction="inbound",
                thread_id=target_thread,
                text="",
                is_callback=True,
                callback_data=chosen["callback_data"],
                reply_to_msg_id=f"local:{mid}",
                now=now,
            )
            return True, [f"Picked [{choice_idx}] {chosen['text']} on msg#{mid}"]

        if line == "/reply" or line.startswith("/reply "):
            parts = line.split(None, 2)
            if len(parts) < 3:
                return True, ["Usage: /reply <msg_id> <text>"]
            mid = parse_msg_id(parts[1])
            if mid is None:
                return True, [f"Invalid message id: {parts[1]}"]
            msg_row = self.db.get_local_message(mid)
            target_thread = (
                msg_row.get("thread_id") if msg_row else None
            ) or self.current_thread_id
            self.db.add_local_message(
                direction="inbound",
                thread_id=target_thread,
                text=parts[2],
                reply_to_msg_id=f"local:{mid}",
                now=now,
            )
            return True, [f"Replied to msg#{mid}"]

        # Default: send message (including /task, /done, /new, or plain text) to daemon.
        self.db.add_local_message(
            direction="inbound",
            thread_id=self.current_thread_id,
            text=line,
            now=now,
        )
        return True, []


def run_chat_loop(hq_dir: str | Path, standalone: bool = False) -> int:
    """Run the interactive `hq chat` terminal loop."""
    cfg = load_config(hq_dir)
    db_path = cfg.hq_dir / ".hq" / "runtime.sqlite"
    db = HQDatabase(db_path)
    session = ChatSession(db)
    stop_event = threading.Event()
    print_lock = threading.Lock()

    daemon_pid = is_daemon_running(cfg.hq_dir)
    standalone_thread: Optional[threading.Thread] = None
    if standalone:
        def _standalone_worker() -> None:
            daemon = HQDaemon(cfg.hq_dir)
            try:
                while not stop_event.is_set():
                    try:
                        daemon.run_once()
                    except Exception as exc:
                        with print_lock:
                            print(f"\n[standalone-error] {exc}", file=sys.stderr, flush=True)
                    stop_event.wait(0.5)
            finally:
                daemon.close()

        standalone_thread = threading.Thread(target=_standalone_worker, daemon=True)
        standalone_thread.start()
        print("[standalone] Embedded hq-daemon loop started (poll=0.5s).")
    elif daemon_pid is None:
        print(
            "[notice] hq-daemon is not running (no active daemon.pid). "
            f"Start it with `hq up --hq-dir {cfg.hq_dir}` or pass `--standalone`."
        )
    else:
        print(f"[connected] hq-daemon active (pid={daemon_pid}).")

    recent = db.list_recent_local_outbound(limit=15)
    last_seen_id = 0
    if recent:
        print("--- Recent messages ---")
        for row in recent:
            print(format_outbound_message(row))
            last_seen_id = max(last_seen_id, int(row["id"]))
        print("-----------------------")

    state = {"last_id": last_seen_id}

    def _poll_outbound() -> None:
        poll_db = HQDatabase(db_path)
        try:
            while not stop_event.is_set():
                rows = poll_db.list_local_outbound(after_id=state["last_id"], limit=50)
                if rows:
                    with print_lock:
                        sys.stdout.write("\n")
                        for row in rows:
                            print(format_outbound_message(row), flush=True)
                            state["last_id"] = max(state["last_id"], int(row["id"]))
                        sys.stdout.write(session.prompt_label())
                        sys.stdout.flush()
                stop_event.wait(0.5)
        finally:
            poll_db.close()

    poll_thread = threading.Thread(target=_poll_outbound, daemon=True)
    poll_thread.start()

    print("Type /help for commands, /quit or Ctrl-D to exit.")
    try:
        while True:
            try:
                raw = input(session.prompt_label())
            except (EOFError, KeyboardInterrupt):
                print("\nBye.")
                break
            keep_going, outputs = session.handle_line(raw)
            with print_lock:
                for line in outputs:
                    print(line, flush=True)
            if not keep_going:
                break
    finally:
        stop_event.set()
        poll_thread.join(timeout=2.0)
        if standalone_thread is not None:
            standalone_thread.join(timeout=3.0)
        db.close()
    return 0
