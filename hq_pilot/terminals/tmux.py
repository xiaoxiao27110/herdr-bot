"""TmuxDriver: Fallback TerminalDriver implementation wrapping `tmux`."""

from __future__ import annotations

from pathlib import Path
from typing import List
import subprocess

from hq_pilot.terminals.base import PaneInfo, TerminalDriver, shell_join


class TmuxDriver(TerminalDriver):
    name = "tmux"

    def __init__(self, bin_path: str = "tmux") -> None:
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
        self._run(
            ["new-session", "-d", "-s", name, "-c", str(cwd), cmd_str],
            check=True,
        )

    def list_panes(self) -> List[PaneInfo]:
        res = self._run(
            ["list-sessions", "-F", "#{session_name}\t#{session_attached}"],
            check=False,
        )
        if res.returncode != 0:
            return []
        panes: List[PaneInfo] = []
        for line in res.stdout.splitlines():
            if "\t" not in line:
                continue
            sname, attached_str = line.split("\t", 1)
            panes.append(
                PaneInfo(
                    name=sname.strip(),
                    status="running",
                    attached=(attached_str.strip() not in {"", "0"}),
                )
            )
        return panes

    def send_text(self, name: str, text: str, enter: bool = True) -> None:
        self._run(["send-keys", "-t", name, "-l", text], check=True)
        if enter:
            self._run(["send-keys", "-t", name, "Enter"], check=True)

    def read_pane(self, name: str, lines: int = 80) -> str:
        res = self._run(
            ["capture-pane", "-p", "-t", name, "-S", f"-{int(lines)}"],
            check=False,
        )
        return res.stdout

    def attach_cmd(self, name: str) -> List[str]:
        return [self.bin_path, "attach-session", "-t", name]

    def kill_pane(self, name: str) -> None:
        self._run(["kill-session", "-t", name], check=False)

    def is_client_attached(self, name: str) -> bool:
        res = self._run(
            ["list-sessions", "-F", "#{session_name}\t#{session_attached}"],
            check=False,
        )
        if res.returncode != 0:
            return False
        for line in res.stdout.splitlines():
            if "\t" not in line:
                continue
            sname, attached_str = line.split("\t", 1)
            if sname.strip() == name:
                return attached_str.strip() not in {"", "0"}
        return False
