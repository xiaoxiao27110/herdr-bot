"""Registry and CompositeChannel for MessagingChannel implementations."""

from __future__ import annotations

from typing import Dict, List, Optional

from hq_pilot.channels.base import InboundMessage, InlineButton, MessagingChannel
from hq_pilot.channels.local import LocalChannel
from hq_pilot.channels.telegram import TelegramChannel
from hq_pilot.db import HQDatabase


class CompositeChannel(MessagingChannel):
    """Fan-out MessagingChannel that mirrors outbound messages across backends."""

    name = "composite"

    def __init__(self, channels: List[MessagingChannel]) -> None:
        self.channels = list(channels)

    def poll_updates(self, timeout: int = 10) -> List[InboundMessage]:
        combined: List[InboundMessage] = []
        for ch in self.channels:
            combined.extend(ch.poll_updates(timeout=timeout))
        return combined

    def send_message(
        self,
        text: str,
        thread_id: Optional[str] = None,
        buttons: Optional[List[List[InlineButton]]] = None,
    ) -> Optional[str]:
        first_id: Optional[str] = None
        for ch in self.channels:
            mid = ch.send_message(text, thread_id=thread_id, buttons=buttons)
            if first_id is None and mid is not None:
                first_id = mid
        return first_id

    def create_thread(self, title: str) -> Optional[str]:
        primary_tid: Optional[str] = None
        for ch in self.channels:
            if isinstance(ch, LocalChannel):
                tid = ch.create_thread(title, thread_id=primary_tid)
            else:
                tid = ch.create_thread(title)
            if primary_tid is None and tid is not None:
                primary_tid = tid
        return primary_tid

    def close_thread(self, thread_id: str) -> bool:
        ok = False
        for ch in self.channels:
            if ch.close_thread(thread_id):
                ok = True
        return ok

    def answer_callback(self, callback_id: str, text: str = "") -> bool:
        if callback_id.startswith("local-cb:"):
            ok = False
            for ch in self.channels:
                if isinstance(ch, LocalChannel) and ch.answer_callback(callback_id, text=text):
                    ok = True
            return ok
        ok = False
        for ch in self.channels:
            if not isinstance(ch, LocalChannel) and ch.answer_callback(callback_id, text=text):
                ok = True
        return ok


def get_channel(
    backend: str,
    secrets: Optional[Dict[str, str]] = None,
    allowed_chat_id: Optional[str] = None,
    db: Optional[HQDatabase] = None,
) -> Optional[MessagingChannel]:
    name = (backend or "telegram").strip().lower()
    sec = secrets or {}
    if name == "local":
        if db is None:
            raise ValueError("LocalChannel requires a db instance")
        return LocalChannel(db)
    if name == "telegram":
        token = sec.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = allowed_chat_id or sec.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            if db is not None:
                return LocalChannel(db)
            return None
        api_base = sec.get("TELEGRAM_API_BASE", "").strip() or "https://api.telegram.org"
        tg = TelegramChannel(bot_token=token, allowed_chat_id=chat_id, api_base=api_base)
        if db is not None:
            return CompositeChannel([tg, LocalChannel(db)])
        return tg
    if name == "none":
        return None
    raise ValueError(f"Unsupported channel backend: {backend!r}")
