"""HerdrDriver: TerminalDriver implementation wrapping `hc` CLI."""

from __future__ import annotations

from pathlib import Path
from typing import List
import json
import subprocess

from hq_pilot.terminals.base import PaneInfo, TerminalDriver, shell_join


def install_herdr_guide() -> str:
    return (
        "Herdr (`hc`) is not installed. To install Herdr:\n"
        "  1. Run: curl -fsSL https://herdr.dev/install.sh | sh\n"
        "     (or visit https://github.com/herdrdev/herdr)\n"
        "  2. Ensure `hc` is in PATH and verify with `hc --version`.\n"
        "  3. Until then, hq-pilot with `terminal.backend: auto` automatically uses `tmux`."
    )


class HerdrDriver(TerminalDriver):
    name = "herdr"

    def __init__(self, bin_path: str = "hc") -> None:
        self.bin_path = bin_path

    def _run(self, args: List[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.bin_path, *args],
            capture_output=True,
            text=True,
            check=check,
        )

    def run_bg(self, name: str, cwd: str | Path, cmd: List[str]) -> None:
        cmd_str = shell_join(cmd)
        self._run(["run", "--bg", "--name", name, "--cwd", str(cwd), "--", cmd_str], check=True)

    def list_panes(self) -> List[PaneInfo]:
        res = self._run(["ls", "--json"], check=False)
        if res.returncode == 0 and res.stdout.strip():
            try:
                items = json.loads(res.stdout)
                if isinstance(items, list):
                    panes: List[PaneInfo] = []
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        panes.append(
                            PaneInfo(
                                name=str(item.get("name", "")),
                                status=str(item.get("status", "running")),
                                attached=bool(item.get("attached", False)),
                                cwd=str(item.get("cwd", "")),
                            )
                        )
                    return panes
            except json.JSONDecodeError:
                pass
        res_txt = self._run(["ls"], check=False)
        panes = []
        for line in res_txt.stdout.splitlines():
            parts = line.strip().split()
            if parts and not parts[0].lower().startswith("name"):
                panes.append(
                    PaneInfo(
                        name=parts[0],
                        status=parts[1].lower() if len(parts) > 1 else "running",
                        attached=("attached" in line.lower()),
                    )
                )
        return panes

    def send_text(self, name: str, text: str, enter: bool = True) -> None:
        args = ["send", name, text]
        if not enter:
            args.append("--no-enter")
        self._run(args, check=True)

    def read_pane(self, name: str, lines: int = 80) -> str:
        res = self._run(["read", name, "--lines", str(lines)], check=False)
        return res.stdout

    def attach_cmd(self, name: str) -> List[str]:
        return [self.bin_path, "attach", name]

    def kill_pane(self, name: str) -> None:
        self._run(["kill", name], check=False)

    def is_client_attached(self, name: str) -> bool:
        for p in self.list_panes():
            if p.name == name:
                return p.attached
        return False
