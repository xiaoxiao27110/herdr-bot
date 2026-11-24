"""FeishuChannel: Feishu/Lark Open API (outbound) + WebSocket long-connection (inbound)."""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from hq_pilot.channels.base import InboundMessage, InlineButton, MessagingChannel

RequestFn = Callable[
    [str, str, Optional[Dict[str, Any]], Dict[str, str], int],
    Dict[str, Any],
]


def _import_lark_oapi() -> Any:
    """Lazily import lark_oapi so core zero-dependency tests do not require it."""
    try:
        import lark_oapi  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "lark-oapi is required for Feishu WebSocket long-connection. "
            "Install it with: pip install lark-oapi"
        ) from exc
    return lark_oapi


def _build_interactive_card(
    text: str,
    buttons: Optional[List[List[InlineButton]]] = None,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a Feishu Interactive Card (schema 2.0) payload."""
    elements: List[Dict[str, Any]] = []
    if text:
        elements.append({"tag": "markdown", "content": text})

    if buttons:
        for row in buttons:
            if not row:
                continue
            columns: List[Dict[str, Any]] = []
            for idx, btn in enumerate(row):
                columns.append(
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": btn.text},
                                "type": "primary" if idx == 0 else "default",
                                "value": {"callback_data": btn.callback_data},
                            }
                        ],
                    }
                )
            elements.append(
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "columns": columns,
                }
            )

    card: Dict[str, Any] = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": elements},
    }
    if title:
        card["header"] = {
            "title": {"tag": "plain_text", "content": title[:128]},
            "template": "blue",
        }
    return card


class FeishuChannel(MessagingChannel):
    """Feishu/Lark MessagingChannel using Open API HTTP + WebSocket long-connection."""

    name = "feishu"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        allowed_chat_id: Optional[str],
        api_base: str = "https://open.feishu.cn",
        request_fn: Optional[RequestFn] = None,
        auto_start_ws: Optional[bool] = None,
    ) -> None:
        self.app_id = (app_id or "").strip()
        self.app_secret = (app_secret or "").strip()
        self.allowed_chat_id = (
            str(allowed_chat_id).strip() if allowed_chat_id is not None else ""
        )
        self.api_base = (api_base or "https://open.feishu.cn").rstrip("/")
        self._request_fn = request_fn
        # Avoid spawning real network WS threads when using a mock request_fn unless explicitly enabled.
        self._auto_start_ws = (
            (request_fn is None) if auto_start_ws is None else bool(auto_start_ws)
        )

        self._token_lock = threading.Lock()
        self._tenant_token: str = ""
        self._token_expires_at: float = 0.0

        self._queue: "queue.Queue[InboundMessage]" = queue.Queue()
        self._update_seq: int = 0
        self._seq_lock = threading.Lock()
        self._seen_chats: Dict[str, Dict[str, str]] = {}
        self._seen_lock = threading.Lock()

        self._ws_lock = threading.Lock()
        self._ws_started: bool = False
        self._ws_thread: Optional[threading.Thread] = None

    def _next_update_id(self) -> int:
        with self._seq_lock:
            self._update_seq += 1
            return self._update_seq

    def _record_seen_chat(
        self, chat_id: str, chat_type: str = "", sender_id: str = ""
    ) -> None:
        cid = (chat_id or "").strip()
        if not cid:
            return
        with self._seen_lock:
            if cid not in self._seen_chats:
                self._seen_chats[cid] = {
                    "chat_id": cid,
                    "chat_type": chat_type or "unknown",
                    "sender_id": sender_id,
                }

    def list_seen_chats(self) -> List[Dict[str, str]]:
        with self._seen_lock:
            return list(self._seen_chats.values())

    def _http_call(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 20,
    ) -> Dict[str, Any]:
        url = f"{self.api_base}{path}"
        hdrs: Dict[str, str] = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "hq-pilot/1.0",
        }
        if headers:
            hdrs.update(headers)

        if self._request_fn is not None:
            return self._request_fn(method, url, body, hdrs, timeout)

        data = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None
            else None
        )
        req = urllib.request.Request(
            url,
            data=data,
            headers=hdrs,
            method=method.upper(),
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                return {"code": -1, "msg": "invalid response"}
            return parsed

    def get_tenant_access_token(self, force_refresh: bool = False) -> str:
        if not self.app_id or not self.app_secret:
            raise RuntimeError("Feishu app_id / app_secret is not configured")

        now = time.time()
        with self._token_lock:
            if (
                not force_refresh
                and self._tenant_token
                and now < self._token_expires_at
            ):
                return self._tenant_token

            resp = self._http_call(
                "POST",
                "/open-apis/auth/v3/tenant_access_token/internal",
                body={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=15,
            )
            if int(resp.get("code", -1)) != 0 or not resp.get("tenant_access_token"):
                raise RuntimeError(f"Feishu tenant_access_token error: {resp}")

            token = str(resp["tenant_access_token"])
            expire_sec = int(resp.get("expire", 7200) or 7200)
            margin = 300 if expire_sec > 300 else max(1, expire_sec // 2)
            self._tenant_token = token
            self._token_expires_at = time.time() + max(1, expire_sec - margin)
            return token

    def _call_api(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        timeout: int = 20,
    ) -> Dict[str, Any]:
        token = self.get_tenant_access_token()
        resp = self._http_call(
            method,
            path,
            body=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        # Refresh token once if expired/invalid (Feishu codes 99991663 / 99991661).
        if int(resp.get("code", 0)) in {99991661, 99991663}:
            token = self.get_tenant_access_token(force_refresh=True)
            resp = self._http_call(
                method,
                path,
                body=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
        return resp

    def is_chat_authorized(self, chat_id: str) -> bool:
        """Fail-closed chat whitelist check."""
        if not self.allowed_chat_id:
            return False
        return str(chat_id).strip() == self.allowed_chat_id

    def handle_message_event(self, data: Any) -> Optional[InboundMessage]:
        """Parse a Feishu im.message.receive_v1 event and enqueue if authorized."""
        event = getattr(data, "event", None)
        if event is None and isinstance(data, dict):
            event = data.get("event")
        if event is None:
            return None

        sender = getattr(event, "sender", None)
        if sender is None and isinstance(event, dict):
            sender = event.get("sender")
        sender_type = str(
            (sender.get("sender_type") if isinstance(sender, dict) else getattr(sender, "sender_type", ""))
            or ""
        ).strip()
        if sender_type in {"app", "bot"}:
            return None

        sender_id_obj = (
            sender.get("sender_id") if isinstance(sender, dict) else getattr(sender, "sender_id", None)
        )
        if isinstance(sender_id_obj, dict):
            sender_id = str(
                sender_id_obj.get("open_id") or sender_id_obj.get("user_id") or ""
            ).strip()
        else:
            sender_id = str(
                getattr(sender_id_obj, "open_id", None)
                or getattr(sender_id_obj, "user_id", None)
                or ""
            ).strip()

        msg = event.get("message") if isinstance(event, dict) else getattr(event, "message", None)
        if msg is None:
            return None

        def _mget(key: str) -> Any:
            return msg.get(key) if isinstance(msg, dict) else getattr(msg, key, None)

        chat_id = str(_mget("chat_id") or "").strip()
        chat_type = str(_mget("chat_type") or "").strip()
        self._record_seen_chat(chat_id, chat_type=chat_type, sender_id=sender_id)
        if not self.is_chat_authorized(chat_id):
            return None

        raw_content = str(_mget("content") or "").strip()
        text = ""
        if raw_content:
            try:
                parsed = json.loads(raw_content)
                if isinstance(parsed, dict) and "text" in parsed:
                    text = str(parsed.get("text") or "").strip()
                else:
                    text = raw_content
            except json.JSONDecodeError:
                text = raw_content
        if not text:
            return None

        message_id = str(_mget("message_id") or "").strip() or None
        root_id = str(_mget("root_id") or "").strip() or None
        parent_id = str(_mget("parent_id") or "").strip() or None

        inbound = InboundMessage(
            update_id=self._next_update_id(),
            chat_id=chat_id,
            thread_id=root_id,
            sender_id=sender_id,
            text=text,
            is_callback=False,
            message_id=message_id,
            reply_to_msg_id=parent_id,
        )
        self._queue.put(inbound)
        return inbound

    def handle_card_action(self, data: Any) -> Any:
        """Parse a Feishu card.action.trigger callback event and enqueue if authorized."""
        event = getattr(data, "event", None)
        if event is None and isinstance(data, dict):
            event = data.get("event")

        inbound: Optional[InboundMessage] = None
        if event is not None:
            context = event.get("context") if isinstance(event, dict) else getattr(event, "context", None)
            operator = event.get("operator") if isinstance(event, dict) else getattr(event, "operator", None)
            action = event.get("action") if isinstance(event, dict) else getattr(event, "action", None)
            token = str(
                (event.get("token") if isinstance(event, dict) else getattr(event, "token", "")) or ""
            ).strip()

            chat_id = str(
                (context.get("open_chat_id") if isinstance(context, dict) else getattr(context, "open_chat_id", ""))
                or ""
            ).strip()
            open_message_id = str(
                (context.get("open_message_id") if isinstance(context, dict) else getattr(context, "open_message_id", ""))
                or ""
            ).strip()
            sender_id = str(
                (
                    (operator.get("open_id") or operator.get("user_id"))
                    if isinstance(operator, dict)
                    else (getattr(operator, "open_id", None) or getattr(operator, "user_id", None))
                )
                or ""
            ).strip()
            self._record_seen_chat(chat_id, chat_type="card", sender_id=sender_id)

            value = action.get("value") if isinstance(action, dict) else getattr(action, "value", None)
            callback_data = ""
            if isinstance(value, dict):
                callback_data = str(value.get("callback_data") or "").strip()

            if self.is_chat_authorized(chat_id) and callback_data:
                inbound = InboundMessage(
                    update_id=self._next_update_id(),
                    chat_id=chat_id,
                    thread_id=None,
                    sender_id=sender_id,
                    text="",
                    is_callback=True,
                    callback_id=token or open_message_id,
                    callback_data=callback_data,
                    message_id=open_message_id or None,
                )
                self._queue.put(inbound)

        try:
            from lark_oapi.event.callback.model.p2_card_action_trigger import (  # type: ignore[import-not-found]
                P2CardActionTriggerResponse,
            )

            return P2CardActionTriggerResponse(
                {
                    "toast": {
                        "type": "info" if inbound is not None else "warning",
                        "content": "Received" if inbound is not None else "Unauthorized chat",
                    }
                }
            )
        except ImportError:
            return {"toast": {"type": "info", "content": "Received"}}

    def start(self) -> None:
        """Lazily start the background lark_oapi WebSocket client thread."""
        if not self.app_id or not self.app_secret:
            raise RuntimeError("Feishu app_id / app_secret is not configured")

        with self._ws_lock:
            if self._ws_started:
                return
            lark = _import_lark_oapi()
            handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self.handle_message_event)
                .register_p2_card_action_trigger(self.handle_card_action)
                .build()
            )
            ws_client = lark.ws.Client(
                self.app_id,
                self.app_secret,
                event_handler=handler,
                domain=self.api_base,
                auto_reconnect=True,
            )

            def _run_ws() -> None:
                ws_client.start()

            t = threading.Thread(target=_run_ws, name="feishu-ws-client", daemon=True)
            t.start()
            self._ws_thread = t
            self._ws_started = True

    def poll_updates(self, timeout: int = 10) -> List[InboundMessage]:
        if not self.app_id or not self.app_secret or not self.allowed_chat_id:
            return []

        if self._auto_start_ws and not self._ws_started:
            self.start()

        items: List[InboundMessage] = []
        try:
            if timeout > 0:
                first = self._queue.get(timeout=timeout)
            else:
                first = self._queue.get_nowait()
            items.append(first)
        except queue.Empty:
            return items

        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return items

    def send_message(
        self,
        text: str,
        thread_id: Optional[str] = None,
        buttons: Optional[List[List[InlineButton]]] = None,
    ) -> Optional[str]:
        if not self.allowed_chat_id:
            raise RuntimeError("Feishu allowed_chat_id is not configured (fail-closed)")

        if buttons:
            msg_type = "interactive"
            content_obj = _build_interactive_card(text, buttons=buttons)
        else:
            msg_type = "text"
            content_obj = {"text": text}
        content_str = json.dumps(content_obj, ensure_ascii=False)

        tid = str(thread_id).strip() if thread_id is not None else ""
        if tid:
            resp = self._call_api(
                "POST",
                f"/open-apis/im/v1/messages/{tid}/reply",
                body={
                    "msg_type": msg_type,
                    "content": content_str,
                    "reply_in_thread": True,
                },
            )
        else:
            resp = self._call_api(
                "POST",
                "/open-apis/im/v1/messages?receive_id_type=chat_id",
                body={
                    "receive_id": self.allowed_chat_id,
                    "msg_type": msg_type,
                    "content": content_str,
                },
            )

        if int(resp.get("code", -1)) != 0:
            return None
        data = resp.get("data") or {}
        mid = data.get("message_id")
        return str(mid) if mid is not None else None

    def create_thread(self, title: str) -> Optional[str]:
        if not self.allowed_chat_id:
            raise RuntimeError("Feishu allowed_chat_id is not configured (fail-closed)")

        card = _build_interactive_card(
            text=f"Task thread created: **{title}**",
            title=title,
        )
        try:
            resp = self._call_api(
                "POST",
                "/open-apis/im/v1/messages?receive_id_type=chat_id",
                body={
                    "receive_id": self.allowed_chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False),
                },
            )
        except urllib.error.HTTPError:
            return None
        if int(resp.get("code", -1)) != 0:
            return None
        data = resp.get("data") or {}
        mid = data.get("message_id")
        return str(mid) if mid is not None else None

    def close_thread(self, thread_id: str) -> bool:
        if not self.allowed_chat_id or not thread_id or not str(thread_id).strip():
            return False
        tid = str(thread_id).strip()
        try:
            resp = self._call_api(
                "POST",
                f"/open-apis/im/v1/messages/{tid}/reply",
                body={
                    "msg_type": "text",
                    "content": json.dumps({"text": "已归档"}, ensure_ascii=False),
                    "reply_in_thread": True,
                },
            )
        except urllib.error.HTTPError:
            return False
        return int(resp.get("code", -1)) == 0

    def answer_callback(self, callback_id: str, text: str = "") -> bool:
        # Feishu card.action.trigger responses are returned synchronously by the WS handler.
        return bool(callback_id)

    def list_bot_chats(self, page_size: int = 50) -> List[Dict[str, Any]]:
        """List group chats that the bot has joined."""
        resp = self._call_api(
            "GET",
            f"/open-apis/im/v1/chats?page_size={int(page_size)}",
            body=None,
        )
        if int(resp.get("code", -1)) != 0:
            return []
        data = resp.get("data") or {}
        items = data.get("items") or []
        result: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("chat_id") or "").strip()
            if not cid:
                continue
            result.append(
                {
                    "chat_id": cid,
                    "name": str(item.get("name") or "(unnamed)"),
                    "chat_mode": str(item.get("chat_mode") or "group"),
                }
            )
        return result


def detect_feishu_chats(
    app_id: str,
    app_secret: str,
    api_base: str = "https://open.feishu.cn",
    listen_seconds: int = 0,
    request_fn: Optional[RequestFn] = None,
) -> Dict[str, Any]:
    """Validate Feishu credentials, list joined group chats, and optionally listen via WS for DM chat_id."""
    probe = FeishuChannel(
        app_id=app_id,
        app_secret=app_secret,
        allowed_chat_id=None,
        api_base=api_base,
        request_fn=request_fn,
    )
    probe.get_tenant_access_token()
    group_chats = probe.list_bot_chats()

    seen_chats: List[Dict[str, str]] = []
    if listen_seconds > 0 and request_fn is None:
        probe.start()
        deadline = time.time() + listen_seconds
        while time.time() < deadline:
            seen_chats = probe.list_seen_chats()
            if seen_chats:
                break
            time.sleep(0.5)
        seen_chats = probe.list_seen_chats()

    return {
        "group_chats": group_chats,
        "seen_chats": seen_chats,
    }
