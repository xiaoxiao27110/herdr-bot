"""TelegramChannel: Pure stdlib Telegram Bot HTTP API implementation."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
import json
import urllib.error
import urllib.request

from hq_pilot.channels.base import InboundMessage, InlineButton, MessagingChannel


class TelegramChannel(MessagingChannel):
    name = "telegram"

    def __init__(
        self,
        bot_token: str,
        allowed_chat_id: Optional[str],
        api_base: str = "https://api.telegram.org",
        request_fn: Optional[Callable[[str, Dict[str, Any], int], Dict[str, Any]]] = None,
    ) -> None:
        self.bot_token = (bot_token or "").strip()
        self.allowed_chat_id = (
            str(allowed_chat_id).strip() if allowed_chat_id is not None else ""
        )
        self.api_base = api_base.rstrip("/")
        self._request_fn = request_fn
        self._offset: int = 0

    def _call_api(
        self, method: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 20
    ) -> Dict[str, Any]:
        if not self.bot_token:
            raise RuntimeError("Telegram bot_token is not configured")
        url = f"{self.api_base}/bot{self.bot_token}/{method}"
        body = payload or {}
        if self._request_fn is not None:
            return self._request_fn(url, body, timeout)

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                return {"ok": False, "result": None}
            return parsed

    def is_chat_authorized(self, chat_id: str) -> bool:
        """Fail-closed chat whitelist check."""
        if not self.allowed_chat_id:
            return False
        return str(chat_id).strip() == self.allowed_chat_id

    def poll_updates(self, timeout: int = 10) -> List[InboundMessage]:
        if not self.bot_token or not self.allowed_chat_id:
            return []
        payload: Dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if self._offset > 0:
            payload["offset"] = self._offset

        resp = self._call_api("getUpdates", payload, timeout=timeout + 10)
        if not resp.get("ok"):
            return []

        messages: List[InboundMessage] = []
        for upd in resp.get("result") or []:
            if not isinstance(upd, dict):
                continue
            uid = int(upd.get("update_id", 0))
            if uid >= self._offset:
                self._offset = uid + 1

            cb = upd.get("callback_query")
            if isinstance(cb, dict):
                msg = cb.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if not self.is_chat_authorized(chat_id):
                    continue
                thread_id = msg.get("message_thread_id")
                messages.append(
                    InboundMessage(
                        update_id=uid,
                        chat_id=chat_id,
                        thread_id=str(thread_id) if thread_id is not None else None,
                        sender_id=str((cb.get("from") or {}).get("id", "")),
                        text="",
                        is_callback=True,
                        callback_id=str(cb.get("id", "")),
                        callback_data=str(cb.get("data", "")),
                        message_id=str(msg.get("message_id", "")),
                    )
                )
                continue

            msg = upd.get("message")
            if isinstance(msg, dict):
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if not self.is_chat_authorized(chat_id):
                    continue
                text = str(msg.get("text") or "").strip()
                if not text:
                    continue
                thread_id = msg.get("message_thread_id")
                messages.append(
                    InboundMessage(
                        update_id=uid,
                        chat_id=chat_id,
                        thread_id=str(thread_id) if thread_id is not None else None,
                        sender_id=str((msg.get("from") or {}).get("id", "")),
                        text=text,
                        is_callback=False,
                        message_id=str(msg.get("message_id", "")),
                    )
                )
        return messages

    def send_message(
        self,
        text: str,
        thread_id: Optional[str] = None,
        buttons: Optional[List[List[InlineButton]]] = None,
    ) -> Optional[str]:
        if not self.allowed_chat_id:
            raise RuntimeError("Telegram allowed_chat_id is not configured (fail-closed)")
        payload: Dict[str, Any] = {
            "chat_id": self.allowed_chat_id,
            "text": text,
        }
        if thread_id is not None and str(thread_id).strip() != "":
            try:
                payload["message_thread_id"] = int(thread_id)
            except ValueError:
                payload["message_thread_id"] = str(thread_id)

        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [
                        {"text": btn.text, "callback_data": btn.callback_data}
                        for btn in row
                    ]
                    for row in buttons
                ]
            }

        resp = self._call_api("sendMessage", payload)
        if not resp.get("ok"):
            return None
        result = resp.get("result") or {}
        mid = result.get("message_id")
        return str(mid) if mid is not None else None

    def create_thread(self, title: str) -> Optional[str]:
        if not self.allowed_chat_id:
            raise RuntimeError("Telegram allowed_chat_id is not configured (fail-closed)")
        resp = self._call_api(
            "createForumTopic",
            {"chat_id": self.allowed_chat_id, "name": title[:128]},
        )
        if not resp.get("ok"):
            return None
        result = resp.get("result") or {}
        tid = result.get("message_thread_id")
        return str(tid) if tid is not None else None

    def close_thread(self, thread_id: str) -> bool:
        if not self.allowed_chat_id:
            return False
        payload: Dict[str, Any] = {"chat_id": self.allowed_chat_id}
        try:
            payload["message_thread_id"] = int(thread_id)
        except ValueError:
            payload["message_thread_id"] = str(thread_id)
        resp = self._call_api("closeForumTopic", payload)
        return bool(resp.get("ok"))

    def answer_callback(self, callback_id: str, text: str = "") -> bool:
        payload: Dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        resp = self._call_api("answerCallbackQuery", payload)
        return bool(resp.get("ok"))
