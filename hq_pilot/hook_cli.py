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


def _hook_stdout_response(
    event_type: str, inject_steps: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    if event_type == "Stop":
        return {"decision": "allow_stop"}
    return {"injectSteps": list(inject_steps or [])}


_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", flags=re.DOTALL)


def _extract_user_request_text(raw: str) -> str:
    text = raw or ""
    m = _USER_REQUEST_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _transcript_has_user_input(adapter: Any, tpath: str) -> bool:
    if not tpath:
        return False
    p = Path(tpath)
    if not p.exists():
        return False
    turns, _ = adapter.read_transcript_slice(str(p), from_line=0)
    return any(t.role == "user" and t.text.strip() for t in turns)


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

    if os.environ.get("HQ_EPHEMERAL") == "1":
        if event_type != "Stop":
            return {
                "status": "ignored",
                "reason": "ephemeral_bg_worker",
                "event": event_type,
            }
        reply_key = os.environ.get("HQ_EPHEMERAL_REPLY_KEY", "").strip()
        transcript_path = str(
            payload.get("transcriptPath") or payload.get("transcript_path") or ""
        )
        if reply_key and transcript_path and Path(transcript_path).exists():
            cfg = load_config(root)
            adapter = get_agent_adapter(agent_name)
            turns, _ = adapter.read_transcript_slice(transcript_path, from_line=0)
            assistant_msgs = [
                t.text for t in turns if t.role == "assistant" and t.text.strip()
            ]
            if assistant_msgs:
                db = HQDatabase(cfg.hq_dir / ".hq" / "runtime.sqlite")
                try:
                    db.set_meta(reply_key, assistant_msgs[-1], now=ts)
                finally:
                    db.close()
        return {
            "status": "ok",
            "event": "Stop",
            "ephemeral": True,
            "reply_pushed": False,
        }

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
        herdr_pane_id = os.environ.get("HERDR_PANE_ID", "").strip()
        herdr_ws_id = os.environ.get("HERDR_WORKSPACE_ID", "").strip()
        if not herdr_ws_id and ":" in herdr_pane_id:
            herdr_ws_id = herdr_pane_id.split(":", 1)[0]

        is_manual_herdr = (not env_session_key) and bool(herdr_pane_id) and bool(agent_sid)
        manual_meta_key = f"herdr_manual_pane:{herdr_pane_id}" if is_manual_herdr else ""
        existing_manual_binding: Optional[Dict[str, Any]] = None
        bound_cid = ""
        if is_manual_herdr:
            raw_binding = db.get_meta(manual_meta_key)
            if raw_binding:
                try:
                    parsed_binding = json.loads(raw_binding)
                    if isinstance(parsed_binding, dict):
                        existing_manual_binding = parsed_binding
                        bound_cid = str(parsed_binding.get("conversation_id") or "").strip()
                except json.JSONDecodeError:
                    existing_manual_binding = None

        has_user_prompt = bool(str(user_prompt or "").strip())
        _transcript_user_checked: Optional[bool] = None

        def _has_transcript_user(tpath_candidate: str) -> bool:
            nonlocal _transcript_user_checked
            if _transcript_user_checked is None:
                _transcript_user_checked = _transcript_has_user_input(adapter, tpath_candidate)
            return _transcript_user_checked

        if env_session_key:
            sess = db.get_session(env_session_key)
            if (
                sess is not None
                and str(sess.get("track") or "") in {"GENERAL", "B"}
                and str(sess.get("status") or "") in {"sealed", "archived"}
            ):
                active_general = None
                for candidate in db.list_sessions(statuses=["active"]):
                    if str(candidate.get("track") or "") in {"GENERAL", "B"}:
                        active_general = candidate
                        break
                if active_general is not None:
                    existing_by_sid = (
                        db.get_session_by_agent_id(agent_sid) if agent_sid else None
                    )
                    if (
                        existing_by_sid is not None
                        and str(existing_by_sid["session_key"])
                        != str(active_general["session_key"])
                    ):
                        sess = existing_by_sid
                    else:
                        sess = active_general
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
        if (
            sess is None
            and is_manual_herdr
            and bound_cid
            and agent_sid != bound_cid
            and not has_user_prompt
            and not _has_transcript_user(transcript_path)
        ):
            return {
                "status": "ignored",
                "reason": "manual_pane_subagent",
                "event": event_type,
            }
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

        if is_manual_herdr and str(sess.get("track") or "") not in {"GENERAL", "B"}:
            effective_tpath = transcript_path or str(sess.get("transcript_path") or "")
            is_confirmed_top_level = (
                (event_type in {"PreInvocation", "UserPromptSubmit"} and has_user_prompt)
                or (event_type == "Stop" and _has_transcript_user(effective_tpath))
                or (
                    event_type == "SessionStart"
                    and (
                        existing_manual_binding is None
                        or not bound_cid
                        or bound_cid == agent_sid
                        or has_user_prompt
                    )
                )
            )
            if is_confirmed_top_level:
                raw_cwd = payload.get("cwd") or payload.get("workspace")
                if (
                    not raw_cwd
                    and isinstance(payload.get("workspacePaths"), list)
                    and payload["workspacePaths"]
                ):
                    raw_cwd = payload["workspacePaths"][0]
                if raw_cwd:
                    cwd_str = str(Path(str(raw_cwd)).expanduser().resolve())
                elif existing_manual_binding and existing_manual_binding.get("cwd"):
                    cwd_str = str(existing_manual_binding["cwd"])
                else:
                    cwd_str = str(root)
                binding_record: Dict[str, Any] = {
                    "pane_id": herdr_pane_id,
                    "workspace_id": herdr_ws_id,
                    "conversation_id": agent_sid,
                    "session_key": skey,
                    "cwd": cwd_str,
                    "updated_at": ts,
                }
                if existing_manual_binding and existing_manual_binding.get("workspace_label"):
                    binding_record["workspace_label"] = str(
                        existing_manual_binding["workspace_label"]
                    )
                db.set_meta(
                    manual_meta_key,
                    json.dumps(binding_record, ensure_ascii=False),
                    now=ts,
                )

        is_general_sess = str(sess.get("track") or "") in {"GENERAL", "B"}
        pane_name_cur = str(sess.get("pane_name") or "").strip()
        daemon_waiting_cur = db.get_meta(f"daemon_waiting:{skey}") == "1" or (
            bool(pane_name_cur)
            and db.get_meta(f"daemon_waiting_pane:{pane_name_cur}") == "1"
        )
        pending_approval_cur = db.get_meta(f"pending_approval_reply:{skey}") == "1"

        pinned_reason = None
        inject_steps: List[Dict[str, Any]] = []
        if event_type in {"PreInvocation", "UserPromptSubmit", "SessionStart"} and user_prompt:
            cleaned_prompt = _extract_user_request_text(str(user_prompt))
            pinned_reason = detect_manual_touch(
                db,
                sess,
                input_text=cleaned_prompt,
                now=ts,
            )
            if event_type in {"PreInvocation", "UserPromptSubmit"}:
                if pinned_reason == "manual_input":
                    db.set_meta(f"last_input_origin:{skey}", "local_tty", now=ts)
                    db.delete_meta(f"pending_approval_reply:{skey}")
                elif daemon_waiting_cur or pending_approval_cur:
                    db.set_meta(f"last_input_origin:{skey}", "daemon", now=ts)

        # Inject post-rotation handoff summary on local Herdr turn (SessionStart with user input or PreInvocation)
        if (
            is_general_sess
            and event_type in {"SessionStart", "PreInvocation"}
            and not daemon_waiting_cur
            and db.get_meta("general_handoff_pending") == "1"
            and (event_type == "PreInvocation" or bool(user_prompt))
        ):
            handoff_text = (db.get_meta("general_handoff_summary") or "").strip()
            handoff_reason = (db.get_meta("general_handoff_reason") or "night").strip()
            db.delete_meta("general_handoff_pending")
            if handoff_text:
                header = (
                    "[昨日会话交接摘要]"
                    if handoff_reason in {"night", "1", ""}
                    else "[上一轮会话交接摘要]"
                )
                inject_steps.append(
                    {
                        "systemMessage": {
                            "systemMessage": f"{header}\n{handoff_text}"
                        }
                    }
                )

        if event_type == "Stop":
            tpath = transcript_path or sess.get("transcript_path")
            reply_pushed = False
            if tpath and Path(str(tpath)).exists():
                tpath_obj = Path(str(tpath))
                offset = int(sess.get("reply_line_offset") or 0)
                turns, total_lines = adapter.read_transcript_slice(str(tpath_obj), from_line=offset)
                user_msgs = [t.text for t in turns if t.role == "user"]
                assistant_msgs = [t.text for t in turns if t.role == "assistant"]
                new_turns = int(sess.get("turn_count") or 0) + len(user_msgs)
                db.update_session(
                    skey,
                    reply_line_offset=total_lines,
                    turn_count=new_turns,
                    last_active_at=ts,
                )
                try:
                    db.set_meta(
                        f"last_stop_mtime:{skey}",
                        str(int(tpath_obj.stat().st_mtime)),
                        now=ts,
                    )
                except OSError:
                    pass
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

                    pending_approval = db.get_meta(f"pending_approval_reply:{skey}") == "1"
                    allow_push = False
                    consume_origin = False
                    if not daemon_waiting and (thread_id or is_general):
                        if last_origin == "local_tty" and not pending_approval:
                            allow_push = False
                        elif is_general and not thread_id:
                            if pending_approval:
                                allow_push = True
                                consume_origin = True
                            elif (
                                has_new_user_msg
                                and last_user_is_daemon
                                and last_origin == "daemon"
                            ):
                                allow_push = True
                                consume_origin = True
                            elif has_new_user_msg and not last_user_is_daemon:
                                db.set_meta(f"last_input_origin:{skey}", "local_tty", now=ts)
                                allow_push = False
                            else:
                                # Pure background SYSTEM_MESSAGE / subagent / timer wake with no new daemon user_msg
                                allow_push = False
                        else:
                            # #Topic with thread_id
                            if pending_approval:
                                allow_push = True
                                consume_origin = True
                            elif has_new_user_msg and not last_user_is_daemon:
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
                    if consume_origin or (not daemon_waiting and pending_approval):
                        db.delete_meta(f"pending_approval_reply:{skey}")
                        db.delete_meta(f"last_input_origin:{skey}")
            if not daemon_waiting_cur and pending_approval_cur:
                db.delete_meta(f"pending_approval_reply:{skey}")
                db.delete_meta(f"last_input_origin:{skey}")
            return {"status": "ok", "event": "Stop", "session_key": skey, "reply_pushed": reply_pushed}

        return {
            "status": "ok",
            "event": event_type,
            "session_key": skey,
            "pinned_reason": pinned_reason,
            "inject_steps": inject_steps,
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
    inject_steps = result.get("inject_steps") if isinstance(result, dict) else None
    print(json.dumps(_hook_stdout_response(event_type, inject_steps=inject_steps), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
