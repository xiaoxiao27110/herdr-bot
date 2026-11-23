"""Registry for pluggable CodingAgentAdapter implementations."""

from __future__ import annotations

from typing import Optional

from hq_pilot.agents.agy import AgyAdapter
from hq_pilot.agents.base import CodingAgentAdapter


def get_agent_adapter(
    backend: str, bin_path: Optional[str] = None
) -> CodingAgentAdapter:
    name = (backend or "agy").strip().lower()
    if name == "agy":
        return AgyAdapter(bin_path=bin_path or "agy")
    raise ValueError(f"Unsupported coding agent backend: {backend!r}")
