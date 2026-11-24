"""LocalChannel: SQLite-backed MessagingChannel for local terminal chat."""

from __future__ import annotations

import json
from typing import List, Optional

from hq_pilot.channels.base import InboundMessage, InlineButton, MessagingChannel
from hq_pilot.db import HQDatabase


class LocalChannel(MessagingChannel):
    """Local SQLite-backed messaging channel consumed by `hq chat`."""

    name = "local"

    def __init__(self, db: HQDatabase) -> None:
        self.db = db

    def poll_updates(self, timeout: int = 10) -> List[InboundMessage]:
        _ = timeout  # Non-blocking local SQLite poll.
        rows = self.db.consume_local_inbound()
        messages: List[InboundMessage] = []
        for row in rows:
            row_id = int(row["id"])
            is_cb = bool(row.get("is_callback"))
            reply_to = row.get("reply_to_msg_id")
            # For button callbacks, message_id should point to the target card message
            # so deferred_actions lookup resolves cleanly.
            msg_id = str(reply_to) if (is_cb and reply_to) else f"local:{row_id}"
            messages.append(
                InboundMessage(
                    update_id=row_id,
                    chat_id="local",
                    thread_id=str(row["thread_id"]) if row.get("thread_id") else None,
                    sender_id="local-user",
                    text=str(row.get("text") or ""),
                    is_callback=is_cb,
                    callback_id=f"local-cb:{row_id}" if is_cb else None,
                    callback_data=row.get("callback_data"),
                    message_id=msg_id,
                    reply_to_msg_id=str(reply_to) if reply_to else None,
                )
            )
        return messages

    def send_message(
        self,
        text: str,
        thread_id: Optional[str] = None,
        buttons: Optional[List[List[InlineButton]]] = None,
    ) -> Optional[str]:
        buttons_json: Optional[str] = None
        if buttons:
            serialized = [
                [{"text": btn.text, "callback_data": btn.callback_data} for btn in row]
                for row in buttons
            ]
            buttons_json = json.dumps(serialized, ensure_ascii=False)
        row_id = self.db.add_local_message(
            direction="outbound",
            text=text,
            thread_id=str(thread_id) if thread_id else None,
            buttons_json=buttons_json,
        )
        return f"local:{row_id}"

    def create_thread(
        self, title: str, thread_id: Optional[str] = None
    ) -> Optional[str]:
        return self.db.create_local_thread(title=title, thread_id=thread_id)

    def close_thread(self, thread_id: str) -> bool:
        return self.db.close_local_thread(thread_id)

    def answer_callback(self, callback_id: str, text: str = "") -> bool:
        ack_text = f"[callback:{callback_id}] {text}".strip()
        self.db.add_local_message(
            direction="outbound",
            text=ack_text,
            consumed=1,
        )
        return True
