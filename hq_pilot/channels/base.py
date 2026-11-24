"""Abstract MessagingChannel protocol for external chat and approval surfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class InlineButton:
    text: str
    callback_data: str


@dataclass
class InboundMessage:
    update_id: int
    chat_id: str
    thread_id: Optional[str]
    sender_id: str
    text: str
    is_callback: bool = False
    callback_id: Optional[str] = None
    callback_data: Optional[str] = None
    message_id: Optional[str] = None
    reply_to_msg_id: Optional[str] = None


class MessagingChannel(ABC):
    name: str

    @abstractmethod
    def poll_updates(self, timeout: int = 10) -> List[InboundMessage]:
        """Poll for new authorized inbound messages or button callbacks."""

    @abstractmethod
    def send_message(
        self,
        text: str,
        thread_id: Optional[str] = None,
        buttons: Optional[List[List[InlineButton]]] = None,
    ) -> Optional[str]:
        """Send a message (optionally in a topic/thread with inline buttons) and return message_id."""

    @abstractmethod
    def create_thread(self, title: str) -> Optional[str]:
        """Create a new topic/thread and return thread_id."""

    @abstractmethod
    def close_thread(self, thread_id: str) -> bool:
        """Close/archive a topic/thread."""

    @abstractmethod
    def answer_callback(self, callback_id: str, text: str = "") -> bool:
        """Acknowledge a button callback."""

    def send_working_indicator(
        self,
        text: str = "⏳ 收到，正在处理中...",
        thread_id: Optional[str] = None,
    ) -> Optional[str]:
        """Optional hook: send an in-progress placeholder message/card and return its id."""
        return None

    def finish_working_indicator(
        self,
        message_id: str,
        final_text: str,
        thread_id: Optional[str] = None,
    ) -> bool:
        """Optional hook: update the in-progress placeholder in-place with the final reply."""
        return False
