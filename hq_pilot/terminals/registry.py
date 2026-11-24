"""Registry for TerminalDriver implementations."""

from __future__ import annotations

from pathlib import Path
import shutil
from typing import Optional

from hq_pilot.terminals.base import TerminalDriver
from hq_pilot.terminals.herdr import HerdrDriver
from hq_pilot.terminals.tmux import TmuxDriver


def find_herdr_bin(bin_path: Optional[str] = None) -> Optional[str]:
    if bin_path:
        return bin_path
    found = shutil.which("herdr")
    if found:
        return found
    local_bin = Path.home() / ".local" / "bin" / "herdr"
    if local_bin.is_file():
        return str(local_bin)
    return None


def get_terminal_driver(
    backend: str = "auto", bin_path: Optional[str] = None
) -> TerminalDriver:
    name = (backend or "auto").strip().lower()
    if name == "auto":
        herdr_bin = find_herdr_bin(bin_path)
        if herdr_bin:
            return HerdrDriver(bin_path=herdr_bin)
        return TmuxDriver(bin_path=bin_path or "tmux")
    if name == "herdr":
        return HerdrDriver(bin_path=find_herdr_bin(bin_path) or "herdr")
    if name == "tmux":
        return TmuxDriver(bin_path=bin_path or "tmux")
    raise ValueError(f"Unsupported terminal backend: {backend!r}")
