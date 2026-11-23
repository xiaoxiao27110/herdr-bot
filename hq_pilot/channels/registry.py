"""Registry for MessagingChannel implementations."""

from __future__ import annotations

from typing import Dict, Optional

from hq_pilot.channels.base import MessagingChannel
from hq_pilot.channels.telegram import TelegramChannel


def get_channel(
    backend: str,
    secrets: Optional[Dict[str, str]] = None,
    allowed_chat_id: Optional[str] = None,
) -> Optional[MessagingChannel]:
    name = (backend or "telegram").strip().lower()
    sec = secrets or {}
    if name == "telegram":
        token = sec.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = allowed_chat_id or sec.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return None
        return TelegramChannel(bot_token=token, allowed_chat_id=chat_id)
    if name == "none":
        return None
    raise ValueError(f"Unsupported channel backend: {backend!r}")
