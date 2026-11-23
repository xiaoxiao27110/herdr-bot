"""Pluggable coding agent adapters."""

from hq_pilot.agents.base import CodingAgentAdapter, HeadlessResult, Turn
from hq_pilot.agents.registry import get_agent_adapter

__all__ = ["CodingAgentAdapter", "HeadlessResult", "Turn", "get_agent_adapter"]
