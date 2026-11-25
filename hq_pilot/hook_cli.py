"""`hq-agent-hook` fast (<10ms) lifecycle hook entrypoint for coding agents."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, List, Optional

from hq_pilot.agents.registry import get_agent_adapter
from hq_pilot.channels.base import MessagingChannel
from hq_pilot.channels.registry import get_channel
from hq_pilot.config import load_config, load_secrets_env
from hq_pilot.db import HQDatabase
from hq_pilot.gatekeeper import detect_manual_touch


def _discover_hq_dir(explicit: Optional[str], payload: Dict[str, Any]) -> Optional[Path]:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_dir = os.environ.get("HQ_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    cwd_str = payload.get("cwd") or payload.get("workspace")
    if not cwd_str and isinstance(payload.get("workspacePaths"), list) and payload["workspacePaths"]:
        cwd_str = payload["workspacePaths"][0]
    if cwd_str:
        cur = Path(str(cwd_str)).resolve()
        for parent in [cur, *cur.parents]:
            if (parent / "hq.yaml").exists():
                return parent
    return None


def _hook_stdout_response(event_type: str) -> Dict[str, Any]:
    if event_type == "Stop":
        return {"decision": "allow_stop"}
    return {"injectSteps": []}


_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", flags=re.DOTALL)


def _extract_user_request_text(raw: str) -> str:
    text = raw or ""
    m = _USER_REQUEST_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def handle_hook_event(
    agent_name: str,
    payload: Dict[str, Any],
    hq_dir: Optional[str | Path] = None,
    channel: Optional[MessagingChannel] = None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    ts = int(now if now is not None else time.time())
    event_type = str(
        payload.get("hookEventName")
        or payload.get("hook_event_name")
        or payload.get("event")
        or payload.get("type")
        or ""
    )
    root = _discover_hq_dir(str(hq_dir) if hq_dir else None, payload)
    if root is None or not (root / "hq.yaml").exists():
        return {"status": "ignored", "reason": "no_hq_dir", "event": event_type}

    cfg = load_config(root)
    db = HQDatabase(cfg.hq_dir / ".hq" / "runtime.sqlite")
    adapter = get_agent_adapter(agent_name)
    try:
        agent_sid = str(
            payload.get("conversationId")
            or payload.get("conversation_id")
            or payload.get("session_id")
            or ""
        )
        transcript_path = str(
            payload.get("transcriptPath") or payload.get("transcript_path") or ""
        )
        user_prompt = (
            payload.get("lastUserInput")
            or payload.get("prompt")
            or payload.get("user_prompt")
            or payload.get("input")
        )

        sess = None
        env_session_key = os.environ.get("HQ_SESSION_KEY", "").strip()
        if env_session_key:
            sess = db.get_session(env_session_key)
            if (
                sess is not None
                and agent_sid
                and sess.get("agent_session_id")
                and str(sess["agent_session_id"]) != agent_sid
            ):
                return {
                    "status": "ignored",
                    "reason": "subagent_session_mismatch",
                    "event": event_type,
                }
        if sess is None and agent_sid:
            sess = db.get_session_by_agent_id(agent_sid)
        if sess is None and agent_sid:
            # Local direct session started outside daemon -> default pinned!
            sess = db.create_session(
                session_key=f"local:{agent_sid}",
                track="LOCAL",
                agent_backend=agent_name,
                status="pinned",
                pinned=True,
                pinned_reason="local_direct",
                agent_session_id=agent_sid,
                transcript_path=transcript_path or None,
                now=ts,
            )

        if sess is None:
            return {"status": "ignored", "reason": "no_session", "event": event_type}

        skey = str(sess["session_key"])
        updates: Dict[str, Any] = {"last_active_at": ts}
        if agent_sid and not sess.get("agent_session_id"):
            updates["agent_session_id"] = agent_sid
        if transcript_path and not sess.get("transcript_path"):
            updates["transcript_path"] = transcript_path
        db.update_session(skey, **updates)
        if updates:
            sess = db.get_session(skey) or sess

        pinned_reason = None
        if event_type in {"PreInvocation", "UserPromptSubmit", "SessionStart"} and user_prompt:
            cleaned_prompt = _extract_user_request_text(str(user_prompt))
            pinned_reason = detect_manual_touch(
                db,
                sess,
                input_text=cleaned_prompt,
                now=ts,
            )
            if event_type in {"PreInvocation", "UserPromptSubmit"}:
                origin = "local_tty" if pinned_reason == "manual_input" else "daemon"
                db.set_meta(f"last_input_origin:{skey}", origin, now=ts)

        if event_type == "Stop":
            tpath = transcript_path or sess.get("transcript_path")
            reply_pushed = False
            if tpath and Path(str(tpath)).exists():
                offset = int(sess.get("reply_line_offset") or 0)
                turns, total_lines = adapter.read_transcript_slice(str(tpath), from_line=offset)
                user_msgs = [t.text for t in turns if t.role == "user"]
                assistant_msgs = [t.text for t in turns if t.role == "assistant"]
                new_turns = int(sess.get("turn_count") or 0) + len(user_msgs)
                db.update_session(
                    skey,
                    reply_line_offset=total_lines,
                    turn_count=new_turns,
                    last_active_at=ts,
                )
                if assistant_msgs:
                    latest_reply = assistant_msgs[-1]
                    pane_name = str(sess.get("pane_name") or "").strip()
                    db.set_meta(f"last_reply:{skey}", latest_reply, now=ts)
                    if pane_name:
                        db.set_meta(f"last_reply_pane:{pane_name}", latest_reply, now=ts)
                    daemon_waiting = db.get_meta(f"daemon_waiting:{skey}") == "1" or (
                        bool(pane_name)
                        and db.get_meta(f"daemon_waiting_pane:{pane_name}") == "1"
                    )
                    thread_id = sess.get("channel_thread_id")
                    is_general = str(sess.get("track") or "") in {"GENERAL", "B"}
                    last_origin = db.get_meta(f"last_input_origin:{skey}")
                    sess_agent_sid = str(sess.get("agent_session_id") or "") or None
                    has_new_user_msg = bool(user_msgs)
                    last_user_is_daemon = False
                    if has_new_user_msg:
                        cleaned_last_user = _extract_user_request_text(user_msgs[-1])
                        if cleaned_last_user:
                            last_user_is_daemon = db.is_daemon_dispatch(
                                text=cleaned_last_user,
                                session_key=skey,
                                agent_session_id=sess_agent_sid,
                            )

                    allow_push = False
                    if not daemon_waiting and (thread_id or is_general):
                        if last_origin == "local_tty":
                            allow_push = False
                        elif is_general and not thread_id:
                            if has_new_user_msg and not last_user_is_daemon:
                                db.set_meta(f"last_input_origin:{skey}", "local_tty", now=ts)
                                allow_push = False
                            else:
                                allow_push = last_origin == "daemon"
                        else:
                            # #Topic with thread_id
                            if has_new_user_msg and not last_user_is_daemon:
                                if (
                                    last_origin == "daemon"
                                    or str(sess.get("pinned_reason") or "") == "manual_input"
                                ):
                                    db.set_meta(f"last_input_origin:{skey}", "local_tty", now=ts)
                                    allow_push = False
                                else:
                                    allow_push = True
                            else:
                                allow_push = True

                    if allow_push:
                        ch = channel
                        if ch is None:
                            secrets = load_secrets_env(cfg.hq_dir)
                            ch = get_channel(
                                cfg.channel.backend,
                                secrets=secrets,
                                allowed_chat_id=cfg.channel.allowed_chat_id,
                                db=db,
                            )
                        if ch is not None:
                            ch.send_message(
                                latest_reply,
                                thread_id=str(thread_id) if thread_id else None,
                            )
                            reply_pushed = True
            return {"status": "ok", "event": "Stop", "session_key": skey, "reply_pushed": reply_pushed}

        return {
            "status": "ok",
            "event": event_type,
            "session_key": skey,
            "pinned_reason": pinned_reason,
        }
    finally:
        db.close()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="hq-agent-hook")
    p.add_argument("--agent", default="agy")
    p.add_argument("--hq-dir", default=None)
    args = p.parse_args(argv)

    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    payload: Dict[str, Any] = {}
    if raw.strip():
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                payload = obj
        except json.JSONDecodeError:
            payload = {}

    result = handle_hook_event(args.agent, payload, hq_dir=args.hq_dir)
    event_type = str(
        result.get("event")
        or payload.get("hookEventName")
        or payload.get("hook_event_name")
        or ""
    )
    print(json.dumps(_hook_stdout_response(event_type)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
