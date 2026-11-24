"""`hq-notify` CLI for 4-level outbox delivery (digest | notify | question | review)."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import List, Optional
import time

from hq_pilot.channels.base import InlineButton, MessagingChannel
from hq_pilot.channels.registry import get_channel
from hq_pilot.config import load_config, load_secrets_env
from hq_pilot.db import HQDatabase


def _parse_hhmm(value: str) -> Optional[int]:
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def is_in_quiet_hours(quiet_hours: str, now: int) -> bool:
    """Return True if local time at `now` falls within `quiet_hours` ('HH:MM-HH:MM')."""
    raw = (quiet_hours or "").strip()
    if not raw or "-" not in raw:
        return False
    parts = raw.split("-")
    if len(parts) != 2:
        return False
    start_min = _parse_hhmm(parts[0])
    end_min = _parse_hhmm(parts[1])
    if start_min is None or end_min is None or start_min == end_min:
        return False
    dt = datetime.fromtimestamp(int(now))
    cur_min = dt.hour * 60 + dt.minute
    if start_min < end_min:
        return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min


def deliver_notification(
    hq_dir: str | Path,
    level: str,
    message: str,
    session_key: Optional[str] = None,
    options: Optional[List[str]] = None,
    channel: Optional[MessagingChannel] = None,
    now: Optional[int] = None,
) -> Optional[str]:
    ts = int(now if now is not None else time.time())
    cfg = load_config(hq_dir)
    db = HQDatabase(cfg.hq_dir / ".hq" / "runtime.sqlite")
    try:
        lvl = level.lower().strip()
        if lvl == "digest":
            db.add_digest_item(level="digest", category="agent_digest", message=message, now=ts)
            return None

        if lvl == "notify" and is_in_quiet_hours(cfg.tuning.quiet_hours, ts):
            db.add_digest_item(
                level="digest",
                category="quiet_hours_downgrade",
                message=message,
                now=ts,
            )
            return None

        ch = channel
        if ch is None:
            secrets = load_secrets_env(cfg.hq_dir)
            ch = get_channel(
                cfg.channel.backend,
                secrets=secrets,
                allowed_chat_id=cfg.channel.allowed_chat_id,
                db=db,
            )

        thread_id = None
        if session_key:
            sess = db.get_session(session_key)
            if sess:
                thread_id = sess.get("channel_thread_id")

        buttons = None
        opt_objs = []
        if lvl in {"question", "review"} and options:
            row: List[InlineButton] = []
            for idx, opt in enumerate(options, start=1):
                cid = f"defer:{session_key or 'global'}:{idx}"
                row.append(InlineButton(text=opt, callback_data=cid))
                opt_objs.append({"id": str(idx), "label": opt})
            buttons = [row]

        prefix = f"[{lvl.upper()}] "
        full_text = prefix + message
        msg_id: Optional[str] = None
        if ch is not None:
            send_err: Optional[str] = None
            try:
                msg_id = ch.send_message(full_text, thread_id=thread_id, buttons=buttons)
                if not msg_id:
                    send_err = "channel.send_message returned None"
            except Exception as exc:
                send_err = str(exc) or exc.__class__.__name__
                msg_id = None

            if not msg_id:
                buttons_json = (
                    json.dumps(
                        [
                            [{"text": b.text, "callback_data": b.callback_data} for b in r]
                            for r in buttons
                        ],
                        ensure_ascii=False,
                    )
                    if buttons
                    else None
                )
                db.enqueue_outbox(
                    level=lvl,
                    text=full_text,
                    thread_id=thread_id,
                    buttons_json=buttons_json,
                    session_key=session_key,
                    next_attempt_at=ts,
                    last_error=send_err,
                    now=ts,
                )
                return None

        if lvl in {"question", "review"} and msg_id and session_key:
            db.create_deferred_action(
                channel_msg_id=msg_id,
                session_key=session_key,
                level=lvl,
                options=opt_objs,
                ttl_hours=cfg.tuning.deferred_approval_ttl_hours,
                now=ts,
            )
        return msg_id
    finally:
        db.close()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="hq-notify")
    p.add_argument("--hq-dir", default=".")
    p.add_argument("--level", choices=["digest", "notify", "question", "review"], required=True)
    p.add_argument("--message", required=True)
    p.add_argument("--session", default=None)
    p.add_argument("--option", action="append", dest="options", default=[])
    args = p.parse_args(argv)
    deliver_notification(
        hq_dir=args.hq_dir,
        level=args.level,
        message=args.message,
        session_key=args.session,
        options=args.options or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
