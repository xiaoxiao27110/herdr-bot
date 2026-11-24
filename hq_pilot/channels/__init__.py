"""Pluggable messaging channels."""

from hq_pilot.channels.base import InboundMessage, InlineButton, MessagingChannel
from hq_pilot.channels.feishu import FeishuChannel, detect_feishu_chats
from hq_pilot.channels.local import LocalChannel
from hq_pilot.channels.registry import CompositeChannel, get_channel
from hq_pilot.channels.telegram import TelegramChannel, detect_telegram_chats

__all__ = [
    "CompositeChannel",
    "FeishuChannel",
    "InboundMessage",
    "InlineButton",
    "LocalChannel",
    "MessagingChannel",
    "TelegramChannel",
    "detect_feishu_chats",
    "detect_telegram_chats",
    "get_channel",
]
