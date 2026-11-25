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

CREATE TABLE IF NOT EXISTS outbox_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    thread_id TEXT,
    text TEXT NOT NULL,
    buttons_json TEXT,
    session_key TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    sent_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_outbox_due
    ON outbox_messages (status, next_attempt_at, id);

CREATE TABLE IF NOT EXISTS timers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    kind TEXT NOT NULL,
    session_key TEXT,
    reason TEXT NOT NULL,
    fire_at INTEGER NOT NULL,
    every_sec INTEGER DEFAULT 0,
    next_fire_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    fired_count INTEGER NOT NULL DEFAULT 0,
    last_fired_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_timers_due
    ON timers (status, next_fire_at, id);
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
            error_detail = (error_msg or "").strip() or "未知错误"
            self.add_digest_item(
                level="alert",
                category="dead_event",
                message=(
                    f"任务事件 #{event_id}（{row['source']}）连续重试 {attempts} 次失败，"
                    f"已转入死信：{error_detail}"
                ),
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

    def park_event_waiting(
        self,
        event_id: int,
        payload_updates: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Park a leased event in `waiting_general` without counting as a failure attempt."""
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (int(event_id),)).fetchone()
        if not row or str(row["status"]) == "done":
            return False
        payload = json.loads(row["payload_json"])
        if payload_updates:
            payload.update(payload_updates)
        new_attempts = max(0, int(row["attempts"]) - 1)
        self.conn.execute(
            """
            UPDATE events
            SET status = 'waiting_general', lease_until = 0, attempts = ?, payload_json = ?
            WHERE id = ?
            """,
            (
                new_attempts,
                json.dumps(payload, sort_keys=True, ensure_ascii=False),
                int(event_id),
            ),
        )
        return True

    def list_waiting_events(self) -> List[EventRow]:
        rows = self.conn.execute(
            """
            SELECT * FROM events
            WHERE status = 'waiting_general'
            ORDER BY priority ASC, available_at ASC, id ASC
            """
        ).fetchall()
        return [
            EventRow(
                id=int(r["id"]),
                source=str(r["source"]),
                dedupe_key=str(r["dedupe_key"]),
                priority=int(r["priority"]),
                available_at=int(r["available_at"]),
                payload=json.loads(r["payload_json"]),
                status=str(r["status"]),
                lease_until=int(r["lease_until"] or 0),
                attempts=int(r["attempts"]),
                created_at=int(r["created_at"]),
            )
            for r in rows
        ]

    def promote_waiting_event(self, event_id: int) -> bool:
        cur = self.conn.execute(
            """
            UPDATE events
            SET status = 'pending', lease_until = 0
            WHERE id = ? AND status = 'waiting_general'
            """,
            (int(event_id),),
        )
        return cur.rowcount > 0

    def update_event_payload(
        self,
        event_id: int,
        payload_updates: Dict[str, Any],
    ) -> bool:
        row = self.conn.execute("SELECT payload_json FROM events WHERE id = ?", (int(event_id),)).fetchone()
        if not row:
            return False
        payload = json.loads(row["payload_json"])
        payload.update(payload_updates)
        self.conn.execute(
            "UPDATE events SET payload_json = ? WHERE id = ?",
            (json.dumps(payload, sort_keys=True, ensure_ascii=False), int(event_id)),
        )
        return True

    def list_open_busy_batch_events(self) -> List[EventRow]:
        """List waiting channel events belonging to the currently open (unclosed) busy batch."""
        rows = self.conn.execute(
            """
            SELECT * FROM events
            WHERE source = 'channel' AND status = 'waiting_general'
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
        result: List[EventRow] = []
        for r in rows:
            payload = json.loads(r["payload_json"])
            if payload.get("batch_closed"):
                continue
            result.append(
                EventRow(
                    id=int(r["id"]),
                    source=str(r["source"]),
                    dedupe_key=str(r["dedupe_key"]),
                    priority=int(r["priority"]),
                    available_at=int(r["available_at"]),
                    payload=payload,
                    status=str(r["status"]),
                    lease_until=int(r["lease_until"] or 0),
                    attempts=int(r["attempts"]),
                    created_at=int(r["created_at"]),
                )
            )
        return result

    def close_busy_batch_for_dispatch(self, leader_id: int) -> List[EventRow]:
        """
        Atomically freeze a channel busy batch at dispatch time so subsequent arrivals
        start a new batch/card, while followers are marked `batch_leased` and tracked on leader.
        Idempotent on leader retry / lease recovery after daemon restart.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            leader_row = self.conn.execute(
                "SELECT * FROM events WHERE id = ?", (int(leader_id),)
            ).fetchone()
            if not leader_row:
                self.conn.execute("COMMIT")
                return []

            leader_payload = json.loads(leader_row["payload_json"])
            if leader_payload.get("batch_closed") and leader_payload.get("batch_member_ids"):
                member_ids = [int(i) for i in leader_payload["batch_member_ids"]]
                placeholders = ",".join("?" for _ in member_ids)
                member_rows = self.conn.execute(
                    f"SELECT * FROM events WHERE id IN ({placeholders}) ORDER BY created_at ASC, id ASC",
                    tuple(member_ids),
                ).fetchall()
                self.conn.execute("COMMIT")
                return [
                    EventRow(
                        id=int(r["id"]),
                        source=str(r["source"]),
                        dedupe_key=str(r["dedupe_key"]),
                        priority=int(r["priority"]),
                        available_at=int(r["available_at"]),
                        payload=json.loads(r["payload_json"]),
                        status=str(r["status"]),
                        lease_until=int(r["lease_until"] or 0),
                        attempts=int(r["attempts"]),
                        created_at=int(r["created_at"]),
                    )
                    for r in member_rows
                ]

            batch_id = str(leader_payload.get("busy_batch_id") or "").strip()
            collected_rows = [leader_row]
            if batch_id:
                waiting_rows = self.conn.execute(
                    """
                    SELECT * FROM events
                    WHERE source = 'channel' AND status = 'waiting_general' AND id != ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (int(leader_id),),
                ).fetchall()
                for wr in waiting_rows:
                    wp = json.loads(wr["payload_json"])
                    if str(wp.get("busy_batch_id") or "").strip() == batch_id and not wp.get("batch_closed"):
                        collected_rows.append(wr)

            collected_rows.sort(key=lambda r: (int(r["created_at"]), int(r["id"])))
            member_ids = [int(r["id"]) for r in collected_rows]
            follower_ids = [eid for eid in member_ids if eid != int(leader_id)]

            if follower_ids:
                placeholders = ",".join("?" for _ in follower_ids)
                self.conn.execute(
                    f"UPDATE events SET status = 'batch_leased', lease_until = 0 WHERE id IN ({placeholders})",
                    tuple(follower_ids),
                )

            leader_payload["batch_closed"] = True
            leader_payload["batch_member_ids"] = member_ids
            self.conn.execute(
                "UPDATE events SET payload_json = ? WHERE id = ?",
                (json.dumps(leader_payload, sort_keys=True, ensure_ascii=False), int(leader_id)),
            )
            self.conn.execute("COMMIT")

            out: List[EventRow] = []
            for r in collected_rows:
                eid = int(r["id"])
                p_obj = leader_payload if eid == int(leader_id) else json.loads(r["payload_json"])
                st = str(r["status"]) if eid == int(leader_id) else "batch_leased"
                out.append(
                    EventRow(
                        id=eid,
                        source=str(r["source"]),
                        dedupe_key=str(r["dedupe_key"]),
                        priority=int(r["priority"]),
                        available_at=int(r["available_at"]),
                        payload=p_obj,
                        status=st,
                        lease_until=int(r["lease_until"] or 0),
                        attempts=int(r["attempts"]),
                        created_at=int(r["created_at"]),
                    )
                )
            return out
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def complete_events(self, event_ids: List[int]) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        self.conn.execute(
            f"UPDATE events SET status = 'done', lease_until = 0 WHERE id IN ({placeholders})",
            tuple(int(i) for i in event_ids),
        )

    def mark_events_dead(self, event_ids: List[int]) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        self.conn.execute(
            f"UPDATE events SET status = 'dead', lease_until = 0 WHERE id IN ({placeholders})",
            tuple(int(i) for i in event_ids),
        )

    def has_queued_channel_events(self) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM events
            WHERE source = 'channel'
              AND status IN ('pending', 'leased', 'waiting_general', 'batch_leased')
            LIMIT 1
            """
        ).fetchone()
        return row is not None

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

    # ------------------------------------------------------------------
    # Durable Outbox Retry Queue
    # ------------------------------------------------------------------
    def enqueue_outbox(
        self,
        level: str,
        text: str,
        thread_id: Optional[str] = None,
        buttons_json: Optional[str] = None,
        session_key: Optional[str] = None,
        next_attempt_at: Optional[int] = None,
        last_error: Optional[str] = None,
        now: Optional[int] = None,
    ) -> int:
        ts = int(now if now is not None else time.time())
        next_at = int(next_attempt_at if next_attempt_at is not None else ts)
        cur = self.conn.execute(
            """
            INSERT INTO outbox_messages (
                level, thread_id, text, buttons_json, session_key,
                status, attempts, next_attempt_at, last_error, created_at, sent_at
            )
            VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, NULL)
            """,
            (
                level,
                thread_id,
                text,
                buttons_json,
                session_key,
                next_at,
                last_error,
                ts,
            ),
        )
        return int(cur.lastrowid)

    def get_outbox_message(self, outbox_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM outbox_messages WHERE id = ?",
            (int(outbox_id),),
        ).fetchone()
        return dict(row) if row else None

    def list_due_outbox(
        self, now: Optional[int] = None, limit: int = 10
    ) -> List[Dict[str, Any]]:
        ts = int(now if now is not None else time.time())
        rows = self.conn.execute(
            """
            SELECT * FROM outbox_messages
            WHERE status = 'pending' AND next_attempt_at <= ?
            ORDER BY next_attempt_at ASC, id ASC
            LIMIT ?
            """,
            (ts, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_outbox_sent(
        self, outbox_id: int, now: Optional[int] = None
    ) -> None:
        ts = int(now if now is not None else time.time())
        self.conn.execute(
            """
            UPDATE outbox_messages
            SET status = 'sent', sent_at = ?, last_error = NULL
            WHERE id = ?
            """,
            (ts, int(outbox_id)),
        )

    def mark_outbox_retry(
        self,
        outbox_id: int,
        error: str,
        next_attempt_at: int,
        max_retries: int = 5,
        now: Optional[int] = None,
    ) -> str:
        ts = int(now if now is not None else time.time())
        row = self.conn.execute(
            "SELECT * FROM outbox_messages WHERE id = ?",
            (int(outbox_id),),
        ).fetchone()
        if not row:
            return "missing"
        new_attempts = int(row["attempts"]) + 1
        if new_attempts >= int(max_retries):
            self.conn.execute(
                """
                UPDATE outbox_messages
                SET status = 'dead', attempts = ?, last_error = ?
                WHERE id = ?
                """,
                (new_attempts, error, int(outbox_id)),
            )
            error_detail = (error or "").strip() or "未知错误"
            self.add_digest_item(
                level="alert",
                category="outbox_dead",
                message=(
                    f"消息 #{outbox_id}（{row['level']}）连续重试 {new_attempts} 次投递失败：{error_detail}"
                ),
                now=ts,
            )
            return "dead"
        self.conn.execute(
            """
            UPDATE outbox_messages
            SET status = 'pending', attempts = ?, next_attempt_at = ?, last_error = ?
            WHERE id = ?
            """,
            (new_attempts, int(next_attempt_at), error, int(outbox_id)),
        )
        return "pending"

    def count_recent_outbox_sent(self, since_ts: int) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM outbox_messages
            WHERE status = 'sent' AND sent_at IS NOT NULL AND sent_at >= ?
            """,
            (int(since_ts),),
        ).fetchone()
        return int(row["cnt"]) if row else 0

    # ------------------------------------------------------------------
    # Self-Scheduled Follow-up Timers
    # ------------------------------------------------------------------
    def create_timer(
        self,
        reason: str,
        fire_at: int,
        kind: str = "once",
        every_sec: int = 0,
        session_key: Optional[str] = None,
        name: Optional[str] = None,
        now: Optional[int] = None,
    ) -> Dict[str, Any]:
        if kind not in {"once", "recurring"}:
            raise ValueError(f"Unsupported timer kind: {kind!r}")
        interval = int(every_sec) if kind == "recurring" else 0
        if kind == "recurring" and interval <= 0:
            raise ValueError("Recurring timer requires every_sec > 0")
        ts = int(now if now is not None else time.time())
        fire_ts = int(fire_at)
        cur = self.conn.execute(
            """
            INSERT INTO timers (
                name, kind, session_key, reason, fire_at, every_sec,
                next_fire_at, status, fired_count, last_fired_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0, NULL, ?)
            """,
            (
                name,
                kind,
                session_key,
                reason,
                fire_ts,
                interval,
                fire_ts,
                ts,
            ),
        )
        return self.get_timer(int(cur.lastrowid))  # type: ignore[return-value]

    def get_timer(self, timer_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM timers WHERE id = ?",
            (int(timer_id),),
        ).fetchone()
        return dict(row) if row else None

    def list_timers(
        self, statuses: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = self.conn.execute(
                f"SELECT * FROM timers WHERE status IN ({placeholders}) ORDER BY next_fire_at ASC, id ASC",
                tuple(statuses),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM timers ORDER BY next_fire_at ASC, id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def cancel_timer(
        self, timer_id: int, now: Optional[int] = None
    ) -> bool:
        _ = now
        cur = self.conn.execute(
            """
            UPDATE timers
            SET status = 'cancelled'
            WHERE id = ? AND status = 'active'
            """,
            (int(timer_id),),
        )
        return cur.rowcount > 0

    def migrate_session_timers(
        self, from_session_key: str, to_session_key: str
    ) -> int:
        """Migrate all active timers bound to `from_session_key` onto `to_session_key`."""
        if not from_session_key or not to_session_key or from_session_key == to_session_key:
            return 0
        cur = self.conn.execute(
            """
            UPDATE timers
            SET session_key = ?
            WHERE session_key = ? AND status = 'active'
            """,
            (str(to_session_key), str(from_session_key)),
        )
        return int(cur.rowcount)

    def fire_due_timers(
        self, now: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Atomically fire due active timers; advance recurring ones on a drift-free grid."""
        ts = int(now if now is not None else time.time())
        fired: List[Dict[str, Any]] = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                """
                SELECT * FROM timers
                WHERE status = 'active' AND next_fire_at <= ?
                ORDER BY next_fire_at ASC, id ASC
                """,
                (ts,),
            ).fetchall()
            for r in rows:
                tid = int(r["id"])
                kind = str(r["kind"])
                every_sec = int(r["every_sec"] or 0)
                new_count = int(r["fired_count"]) + 1
                item = dict(r)
                item["scheduled_fire_at"] = int(r["next_fire_at"])
                item["fired_count"] = new_count
                item["last_fired_at"] = ts
                item["fired_at"] = ts

                if kind == "recurring" and every_sec > 0:
                    anchor = int(r["fire_at"])
                    if ts < anchor:
                        next_fire = anchor
                    else:
                        k = ((ts - anchor) // every_sec) + 1
                        next_fire = anchor + k * every_sec
                    self.conn.execute(
                        """
                        UPDATE timers
                        SET next_fire_at = ?, fired_count = ?, last_fired_at = ?
                        WHERE id = ?
                        """,
                        (next_fire, new_count, ts, tid),
                    )
                    item["next_fire_at"] = next_fire
                    item["status"] = "active"
                else:
                    self.conn.execute(
                        """
                        UPDATE timers
                        SET status = 'fired', fired_count = ?, last_fired_at = ?
                        WHERE id = ?
                        """,
                        (new_count, ts, tid),
                    )
                    item["status"] = "fired"
                fired.append(item)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return fired
