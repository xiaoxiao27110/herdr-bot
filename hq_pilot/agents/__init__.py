"""Pluggable coding agent adapters."""

from hq_pilot.agents.base import (
    CodingAgentAdapter,
    HeadlessResult,
    HookEvent,
    HookEventKind,
    ProjectTrustResult,
    Turn,
)
from hq_pilot.agents.registry import (
    get_agent_adapter,
    list_agent_backends,
    register_agent_adapter,
)

__all__ = [
    "CodingAgentAdapter",
    "HeadlessResult",
    "HookEvent",
    "HookEventKind",
    "ProjectTrustResult",
    "Turn",
    "get_agent_adapter",
    "list_agent_backends",
    "register_agent_adapter",
]
