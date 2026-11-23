"""Registry for TerminalDriver implementations."""

from __future__ import annotations

import shutil
from typing import Optional

from hq_pilot.terminals.base import TerminalDriver
from hq_pilot.terminals.herdr import HerdrDriver
from hq_pilot.terminals.tmux import TmuxDriver


def get_terminal_driver(
    backend: str = "auto", bin_path: Optional[str] = None
) -> TerminalDriver:
    name = (backend or "auto").strip().lower()
    if name == "auto":
        if shutil.which("hc"):
            return HerdrDriver(bin_path=bin_path or "hc")
        return TmuxDriver(bin_path=bin_path or "tmux")
    if name == "herdr":
        return HerdrDriver(bin_path=bin_path or "hc")
    if name == "tmux":
        return TmuxDriver(bin_path=bin_path or "tmux")
    raise ValueError(f"Unsupported terminal backend: {backend!r}")
