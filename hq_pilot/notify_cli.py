"""`hq-notify` CLI for 4-level outbox delivery (digest | notify | question | review)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional
import time

from hq_pilot.channels.base import InlineButton, MessagingChannel
from hq_pilot.channels.registry import get_channel
from hq_pilot.config import load_config, load_secrets_env
from hq_pilot.db import HQDatabase


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
        msg_id = ch.send_message(prefix + message, thread_id=thread_id, buttons=buttons) if ch else None

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
