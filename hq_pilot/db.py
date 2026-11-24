"""SQLite transactional storage for events, 4-state sessions, dispatches, and alerts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
import hashlib
import json
import sqlite3
import time


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    priority INTEGER NOT NULL,
    available_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    lease_until INTEGER DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_claim
    ON events (status, available_at, priority, id);

CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT PRIMARY KEY,
    track TEXT NOT NULL,
    agent_backend TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    pinned INTEGER NOT NULL DEFAULT 0,
    pinned_reason TEXT,
    suspended_msg_id TEXT,
    channel_thread_id TEXT,
    topic_slug TEXT,
    repo_name TEXT,
    branch_name TEXT,
    worktree_path TEXT,
    pane_name TEXT,
    agent_session_id TEXT UNIQUE,
    transcript_path TEXT,
    turn_count INTEGER NOT NULL DEFAULT 0,
    reply_line_offset INTEGER NOT NULL DEFAULT 0,
    archive_blocked_reason TEXT,
    created_at INTEGER NOT NULL,
    sealed_at INTEGER,
    archived_at INTEGER,
    last_active_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS daemon_dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    agent_session_id TEXT,
    input_sha256 TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispatches_lookup
    ON daemon_dispatches (agent_session_id, input_sha256);

CREATE TABLE IF NOT EXISTS deferred_actions (
    channel_msg_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    level TEXT NOT NULL,
    options_json TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    resolved_at INTEGER
);

CREATE TABLE IF NOT EXISTS digest_buffer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    sent_at INTEGER
);

CREATE TABLE IF NOT EXISTS meta_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS local_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    thread_id TEXT,
    text TEXT NOT NULL,
    buttons_json TEXT,
    is_callback INTEGER DEFAULT 0,
    callback_data TEXT,
    reply_to_msg_id TEXT,
    consumed INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_local_messages_poll
    ON local_messages (direction, consumed, id);

CREATE TABLE IF NOT EXISTS local_threads (
    thread_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    closed INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
"""


def normalize_input_hash(text: str) -> str:
    """Compute canonical SHA-256 fingerprint of normalized prompt/input text."""
    normalized = " ".join((text or "").strip().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class EventRow:
    id: int
    source: str
    dedupe_key: str
    priority: int
    available_at: int
    payload: Dict[str, Any]
    status: str
    lease_until: int
    attempts: int
    created_at: int


class HQDatabase:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA busy_timeout=5000;")
        self.conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # Events & Inbox Lease
    # ------------------------------------------------------------------
    def enqueue_event(
        self,
        source: str,
        dedupe_key: str,
        priority: int,
        payload: Dict[str, Any],
        available_at: Optional[int] = None,
        now: Optional[int] = None,
    ) -> Optional[int]:
        ts = int(now if now is not None else time.time())
        avail = int(available_at if available_at is not None else ts)
        payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO events
            (source, dedupe_key, priority, available_at, payload_json, status, lease_until, attempts, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', 0, 0, ?)
            """,
            (source, dedupe_key, priority, avail, payload_json, ts),
        )
        if cur.rowcount == 0:
            return None
        return cur.lastrowid

    def claim_next_events(
        self,
        limit: int = 5,
        now: Optional[int] = None,
        lease_ttl_sec: int = 900,
    ) -> List[EventRow]:
        ts = int(now if now is not None else time.time())
        lease_until = ts + int(lease_ttl_sec)
        claimed: List[EventRow] = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                """
                SELECT * FROM events
                WHERE (
                    (status = 'pending' AND available_at <= ?)
                    OR (status = 'leased' AND lease_until <= ?)
                )
                ORDER BY priority ASC, available_at ASC, id ASC
                LIMIT ?
                """,
                (ts, ts, limit),
            ).fetchall()
            for r in rows:
                new_attempts = int(r["attempts"]) + 1
                self.conn.execute(
                    """
                    UPDATE events
                    SET status = 'leased', lease_until = ?, attempts = ?
                    WHERE id = ?
                    """,
                    (lease_until, new_attempts, r["id"]),
                )
                claimed.append(
                    EventRow(
                        id=int(r["id"]),
                        source=str(r["source"]),
                        dedupe_key=str(r["dedupe_key"]),
                        priority=int(r["priority"]),
                        available_at=int(r["available_at"]),
                        payload=json.loads(r["payload_json"]),
                        status="leased",
                        lease_until=lease_until,
                        attempts=new_attempts,
                        created_at=int(r["created_at"]),
                    )
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return claimed

    def complete_event(self, event_id: int) -> None:
        self.conn.execute(
            "UPDATE events SET status = 'done', lease_until = 0 WHERE id = ?",
            (event_id,),
        )

    def fail_event(
        self,
        event_id: int,
        max_attempts: int = 3,
        now: Optional[int] = None,
        error_msg: str = "",
    ) -> str:
        ts = int(now if now is not None else time.time())
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if not row:
            return "missing"
        attempts = int(row["attempts"])
        if attempts >= max_attempts:
            self.conn.execute(
                "UPDATE events SET status = 'dead', lease_until = 0 WHERE id = ?",
                (event_id,),
            )
            self.add_digest_item(
                level="alert",
                category="dead_event",
                message=f"Event #{event_id} ({row['source']}:{row['dedupe_key']}) moved to dead after {attempts} attempts. {error_msg}".strip(),
                now=ts,
            )
            return "dead"
        backoff = min(3600, 30 * (2 ** max(0, attempts - 1)))
        self.conn.execute(
            "UPDATE events SET status = 'pending', lease_until = 0, available_at = ? WHERE id = ?",
            (ts + backoff, event_id),
        )
        return "pending"

    def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Sessions (4-state: active | sealed | pinned | archived)
    # ------------------------------------------------------------------
    def create_session(
        self,
        session_key: str,
        track: str,
        agent_backend: str,
        status: str = "active",
        pinned: bool = False,
        pinned_reason: Optional[str] = None,
        channel_thread_id: Optional[str] = None,
        topic_slug: Optional[str] = None,
        repo_name: Optional[str] = None,
        branch_name: Optional[str] = None,
        worktree_path: Optional[str] = None,
        pane_name: Optional[str] = None,
        agent_session_id: Optional[str] = None,
        transcript_path: Optional[str] = None,
        now: Optional[int] = None,
    ) -> Dict[str, Any]:
        ts = int(now if now is not None else time.time())
        if pinned:
            status = "pinned"
        self.conn.execute(
            """
            INSERT INTO sessions (
                session_key, track, agent_backend, status, pinned, pinned_reason,
                channel_thread_id, topic_slug, repo_name, branch_name, worktree_path,
                pane_name, agent_session_id, transcript_path, turn_count,
                reply_line_offset, created_at, last_active_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
            """,
            (
                session_key,
                track,
                agent_backend,
                status,
                1 if pinned else 0,
                pinned_reason,
                channel_thread_id,
                topic_slug,
                repo_name,
                branch_name,
                worktree_path,
                pane_name,
                agent_session_id,
                transcript_path,
                ts,
                ts,
            ),
        )
        return self.get_session(session_key)  # type: ignore[return-value]

    def get_session(self, session_key: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
        ).fetchone()
        return dict(row) if row else None

    def get_session_by_agent_id(self, agent_session_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE agent_session_id = ?", (agent_session_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_session_by_thread(
        self, channel_thread_id: str, include_archived: bool = False
    ) -> Optional[Dict[str, Any]]:
        if include_archived:
            row = self.conn.execute(
                "SELECT * FROM sessions WHERE channel_thread_id = ? ORDER BY created_at DESC LIMIT 1",
                (str(channel_thread_id),),
            ).fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT * FROM sessions
                WHERE channel_thread_id = ? AND status != 'archived'
                ORDER BY created_at DESC LIMIT 1
                """,
                (str(channel_thread_id),),
            ).fetchone()
        return dict(row) if row else None

    def find_session(self, id_or_slug: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            """
            SELECT * FROM sessions
            WHERE session_key = ? OR agent_session_id = ? OR topic_slug = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (id_or_slug, id_or_slug, id_or_slug),
        ).fetchone()
        return dict(row) if row else None

    def list_sessions(
        self, statuses: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = self.conn.execute(
                f"SELECT * FROM sessions WHERE status IN ({placeholders}) ORDER BY last_active_at DESC",
                tuple(statuses),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM sessions ORDER BY last_active_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_session(self, session_key: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields.keys())
        vals = list(fields.values()) + [session_key]
        self.conn.execute(f"UPDATE sessions SET {cols} WHERE session_key = ?", vals)

    def pin_session(
        self, session_key: str, reason: str, now: Optional[int] = None
    ) -> None:
        ts = int(now if now is not None else time.time())
        self.conn.execute(
            """
            UPDATE sessions
            SET status = 'pinned', pinned = 1, pinned_reason = ?, last_active_at = ?
            WHERE session_key = ?
            """,
            (reason, ts, session_key),
        )

    def unpin_session(
        self, session_key: str, target_status: str = "active", now: Optional[int] = None
    ) -> None:
        ts = int(now if now is not None else time.time())
        self.conn.execute(
            """
            UPDATE sessions
            SET status = ?, pinned = 0, pinned_reason = NULL, last_active_at = ?
            WHERE session_key = ?
            """,
            (target_status, ts, session_key),
        )

    def seal_session(self, session_key: str, now: Optional[int] = None) -> bool:
        """Seal an active session. Pinned sessions are not auto-sealed."""
        ts = int(now if now is not None else time.time())
        cur = self.conn.execute(
            """
            UPDATE sessions
            SET status = 'sealed', sealed_at = ?, last_active_at = ?
            WHERE session_key = ? AND status = 'active' AND pinned = 0
            """,
            (ts, ts, session_key),
        )
        return cur.rowcount > 0

    def archive_session(self, session_key: str, now: Optional[int] = None) -> None:
        ts = int(now if now is not None else time.time())
        self.conn.execute(
            """
            UPDATE sessions
            SET status = 'archived', archived_at = ?, archive_blocked_reason = NULL
            WHERE session_key = ?
            """,
            (ts, session_key),
        )

    def list_archivable_sessions(self) -> List[Dict[str, Any]]:
        """Only status='sealed' AND pinned=0 AND suspended_msg_id IS NULL."""
        rows = self.conn.execute(
            """
            SELECT * FROM sessions
            WHERE status = 'sealed' AND pinned = 0 AND suspended_msg_id IS NULL
            ORDER BY sealed_at ASC, created_at ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Daemon Dispatch Fingerprints
    # ------------------------------------------------------------------
    def record_dispatch(
        self,
        session_key: str,
        agent_session_id: Optional[str],
        text: str,
        now: Optional[int] = None,
    ) -> str:
        ts = int(now if now is not None else time.time())
        digest = normalize_input_hash(text)
        self.conn.execute(
            """
            INSERT INTO daemon_dispatches (session_key, agent_session_id, input_sha256, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_key, agent_session_id, digest, ts),
        )
        return digest

    def is_daemon_dispatch(
        self,
        text: str,
        session_key: Optional[str] = None,
        agent_session_id: Optional[str] = None,
    ) -> bool:
        digest = normalize_input_hash(text)
        if session_key and agent_session_id:
            row = self.conn.execute(
                """
                SELECT 1 FROM daemon_dispatches
                WHERE input_sha256 = ? AND (session_key = ? OR agent_session_id = ?)
                LIMIT 1
                """,
                (digest, session_key, agent_session_id),
            ).fetchone()
        elif agent_session_id:
            row = self.conn.execute(
                "SELECT 1 FROM daemon_dispatches WHERE input_sha256 = ? AND agent_session_id = ? LIMIT 1",
                (digest, agent_session_id),
            ).fetchone()
        elif session_key:
            row = self.conn.execute(
                "SELECT 1 FROM daemon_dispatches WHERE input_sha256 = ? AND session_key = ? LIMIT 1",
                (digest, session_key),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT 1 FROM daemon_dispatches WHERE input_sha256 = ? LIMIT 1",
                (digest,),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Deferred Actions
    # ------------------------------------------------------------------
    def create_deferred_action(
        self,
        channel_msg_id: str,
        session_key: str,
        level: str,
        options: List[Dict[str, str]],
        ttl_hours: int = 24,
        now: Optional[int] = None,
    ) -> None:
        ts = int(now if now is not None else time.time())
        expires_at = ts + ttl_hours * 3600
        self.conn.execute(
            """
            INSERT OR REPLACE INTO deferred_actions
            (channel_msg_id, session_key, level, options_json, expires_at, resolved_at)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                str(channel_msg_id),
                session_key,
                level,
                json.dumps(options, ensure_ascii=False),
                expires_at,
            ),
        )
        self.update_session(session_key, suspended_msg_id=str(channel_msg_id))

    def get_deferred_action(self, channel_msg_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM deferred_actions WHERE channel_msg_id = ?",
            (str(channel_msg_id),),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["options"] = json.loads(d["options_json"])
        return d

    def resolve_deferred_action(
        self, channel_msg_id: str, now: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        ts = int(now if now is not None else time.time())
        action = self.get_deferred_action(channel_msg_id)
        if not action:
            return None
        self.conn.execute(
            "UPDATE deferred_actions SET resolved_at = ? WHERE channel_msg_id = ?",
            (ts, str(channel_msg_id)),
        )
        self.conn.execute(
            "UPDATE sessions SET suspended_msg_id = NULL WHERE session_key = ? AND suspended_msg_id = ?",
            (action["session_key"], str(channel_msg_id)),
        )
        return action

    def expire_deferred_actions(self, now: Optional[int] = None) -> int:
        ts = int(now if now is not None else time.time())
        rows = self.conn.execute(
            "SELECT channel_msg_id, session_key FROM deferred_actions WHERE resolved_at IS NULL AND expires_at <= ?",
            (ts,),
        ).fetchall()
        for r in rows:
            self.conn.execute(
                "UPDATE deferred_actions SET resolved_at = ? WHERE channel_msg_id = ?",
                (ts, r["channel_msg_id"]),
            )
            self.conn.execute(
                "UPDATE sessions SET suspended_msg_id = NULL WHERE session_key = ? AND suspended_msg_id = ?",
                (r["session_key"], r["channel_msg_id"]),
            )
        return len(rows)

    # ------------------------------------------------------------------
    # Digest Buffer & Meta State
    # ------------------------------------------------------------------
    def add_digest_item(
        self,
        level: str,
        category: str,
        message: str,
        now: Optional[int] = None,
    ) -> int:
        ts = int(now if now is not None else time.time())
        cur = self.conn.execute(
            """
            INSERT INTO digest_buffer (level, category, message, created_at, sent_at)
            VALUES (?, ?, ?, ?, NULL)
            """,
            (level, category, message, ts),
        )
        return int(cur.lastrowid)

    def list_pending_digest_items(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM digest_buffer WHERE sent_at IS NULL ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_digest_items_sent(self, item_ids: List[int], now: Optional[int] = None) -> None:
        if not item_ids:
            return
        ts = int(now if now is not None else time.time())
        placeholders = ",".join("?" for _ in item_ids)
        self.conn.execute(
            f"UPDATE digest_buffer SET sent_at = ? WHERE id IN ({placeholders})",
            [ts, *item_ids],
        )

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM meta_state WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str, now: Optional[int] = None) -> None:
        ts = int(now if now is not None else time.time())
        self.conn.execute(
            """
            INSERT INTO meta_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, str(value), ts),
        )

    def delete_meta(self, key: str) -> None:
        self.conn.execute("DELETE FROM meta_state WHERE key = ?", (key,))

    # ------------------------------------------------------------------
    # Local Channel Storage (local_messages & local_threads)
    # ------------------------------------------------------------------
    def add_local_message(
        self,
        direction: str,
        text: str,
        thread_id: Optional[str] = None,
        buttons_json: Optional[str] = None,
        is_callback: bool = False,
        callback_data: Optional[str] = None,
        reply_to_msg_id: Optional[str] = None,
        consumed: int = 0,
        now: Optional[int] = None,
    ) -> int:
        ts = int(now if now is not None else time.time())
        cur = self.conn.execute(
            """
            INSERT INTO local_messages (
                direction, thread_id, text, buttons_json,
                is_callback, callback_data, reply_to_msg_id, consumed, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                direction,
                thread_id,
                text,
                buttons_json,
                1 if is_callback else 0,
                callback_data,
                reply_to_msg_id,
                int(consumed),
                ts,
            ),
        )
        return int(cur.lastrowid)

    def consume_local_inbound(self, limit: int = 50) -> List[Dict[str, Any]]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                """
                SELECT * FROM local_messages
                WHERE direction = 'inbound' AND consumed = 0
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            if rows:
                ids = [int(r["id"]) for r in rows]
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE local_messages SET consumed = 1 WHERE id IN ({placeholders})",
                    ids,
                )
            self.conn.execute("COMMIT")
            return [dict(r) for r in rows]
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def list_local_outbound(
        self, after_id: int = 0, limit: int = 50
    ) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM local_messages
            WHERE direction = 'outbound' AND id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (int(after_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_recent_local_outbound(self, limit: int = 15) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM (
                SELECT * FROM local_messages
                WHERE direction = 'outbound'
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id ASC
            """,
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_local_message(self, msg_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM local_messages WHERE id = ?",
            (int(msg_id),),
        ).fetchone()
        return dict(row) if row else None

    def create_local_thread(
        self,
        title: str,
        thread_id: Optional[str] = None,
        now: Optional[int] = None,
    ) -> str:
        ts = int(now if now is not None else time.time())
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            tid = thread_id
            if not tid:
                count_row = self.conn.execute(
                    "SELECT COUNT(*) AS cnt FROM local_threads"
                ).fetchone()
                idx = int(count_row["cnt"]) + 1 if count_row else 1
                while True:
                    candidate = f"topic-{idx}"
                    exists = self.conn.execute(
                        "SELECT 1 FROM local_threads WHERE thread_id = ?",
                        (candidate,),
                    ).fetchone()
                    if not exists:
                        tid = candidate
                        break
                    idx += 1
            self.conn.execute(
                """
                INSERT OR IGNORE INTO local_threads (thread_id, title, closed, created_at)
                VALUES (?, ?, 0, ?)
                """,
                (tid, title, ts),
            )
            self.conn.execute("COMMIT")
            return str(tid)
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def close_local_thread(self, thread_id: str) -> bool:
        cur = self.conn.execute(
            "UPDATE local_threads SET closed = 1 WHERE thread_id = ?",
            (str(thread_id),),
        )
        return cur.rowcount > 0

    def list_local_threads(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM local_threads ORDER BY created_at ASC, thread_id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
