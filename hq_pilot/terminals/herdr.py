"""HerdrDriver: TerminalDriver implementation wrapping the `herdr` CLI (v0.9+)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import subprocess
import time

from hq_pilot.terminals.base import PaneInfo, TerminalDriver, shell_join


def install_herdr_guide() -> str:
    return (
        "Herdr (`herdr`) is not installed. To install Herdr:\n"
        "  1. Run: curl -fsSL https://herdr.dev/install.sh | sh\n"
        "     (or visit https://github.com/herdrdev/herdr)\n"
        "  2. Ensure `herdr` is in PATH and verify with `herdr --version`.\n"
        "  3. Until then, hq-pilot with `terminal.backend: auto` automatically uses `tmux`."
    )


class HerdrDriver(TerminalDriver):
    name = "herdr"

    def __init__(self, bin_path: str = "herdr", session: Optional[str] = None) -> None:
        self.bin_path = bin_path
        self.session = session

    def _base_cmd(self) -> List[str]:
        cmd = [self.bin_path]
        if self.session:
            cmd.extend(["--session", self.session])
        return cmd

    def _run(self, args: List[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self._base_cmd(), *args],
            capture_output=True,
            text=True,
            check=check,
        )

    def _run_json(self, args: List[str], check: bool = False) -> Dict[str, Any]:
        res = self._run(args, check=check)
        if res.returncode != 0 or not res.stdout.strip():
            return {}
        try:
            data = json.loads(res.stdout)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _server_running(self) -> bool:
        data = self._run_json(["status", "--json"], check=False)
        server = data.get("server")
        return bool(isinstance(server, dict) and server.get("running"))

    def ensure_server(self, timeout_sec: float = 5.0) -> None:
        if self._server_running():
            return
        subprocess.Popen(
            [*self._base_cmd(), "server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if self._server_running():
                return
            time.sleep(0.2)

    def _workspace_maps(self) -> tuple[Dict[str, str], Dict[str, str]]:
        """Return (ws_id -> label, label -> ws_id)."""
        ws_data = self._run_json(["workspace", "list"], check=False)
        workspaces = (ws_data.get("result") or {}).get("workspaces") or []
        id_to_label: Dict[str, str] = {}
        label_to_id: Dict[str, str] = {}
        for ws in workspaces:
            if not isinstance(ws, dict):
                continue
            wid = str(ws.get("workspace_id", ""))
            label = str(ws.get("label", ""))
            if wid:
                id_to_label[wid] = label or wid
            if label:
                label_to_id[label] = wid
        return id_to_label, label_to_id

    def _resolve_pane_id(self, name: str) -> str:
        _, label_to_id = self._workspace_maps()
        wid = label_to_id.get(name)
        args = ["pane", "list"]
        if wid:
            args.extend(["--workspace", wid])
        pane_data = self._run_json(args, check=False)
        panes = (pane_data.get("result") or {}).get("panes") or []
        if wid and panes:
            return str(panes[0].get("pane_id", name))
        for p in panes:
            if isinstance(p, dict) and str(p.get("pane_id", "")) == name:
                return name
        return name

    def run_bg(self, name: str, cwd: str | Path, cmd: List[str]) -> None:
        self.ensure_server()
        created = self._run_json(
            ["workspace", "create", "--cwd", str(cwd), "--label", name, "--no-focus"],
            check=True,
        )
        root_pane = (created.get("result") or {}).get("root_pane") or {}
        pane_id = str(root_pane.get("pane_id", ""))
        if not pane_id:
            raise RuntimeError(f"Herdr failed to create workspace pane for {name!r}")
        cmd_str = shell_join(cmd)
        self._run(["pane", "run", pane_id, cmd_str], check=True)

    def list_panes(self) -> List[PaneInfo]:
        if not self._server_running():
            return []
        id_to_label, _ = self._workspace_maps()
        pane_data = self._run_json(["pane", "list"], check=False)
        panes_raw = (pane_data.get("result") or {}).get("panes") or []
        panes: List[PaneInfo] = []
        for item in panes_raw:
            if not isinstance(item, dict):
                continue
            wid = str(item.get("workspace_id", ""))
            pid = str(item.get("pane_id", ""))
            label = id_to_label.get(wid) or pid
            panes.append(
                PaneInfo(
                    name=label,
                    status=str(item.get("agent_status", "running")),
                    attached=False,
                    cwd=str(item.get("cwd", "")),
                )
            )
        return panes

    def send_text(self, name: str, text: str, enter: bool = True) -> None:
        pane_id = self._resolve_pane_id(name)
        if enter:
            self._run(["pane", "run", pane_id, text], check=True)
        else:
            self._run(["pane", "send-text", pane_id, text], check=True)

    def read_pane(self, name: str, lines: int = 80) -> str:
        pane_id = self._resolve_pane_id(name)
        res = self._run(
            ["pane", "read", pane_id, "--source", "recent-unwrapped", "--lines", str(lines), "--raw"],
            check=False,
        )
        return res.stdout

    def attach_cmd(self, name: str) -> List[str]:
        if self.session:
            return [self.bin_path, "session", "attach", self.session]
        return [self.bin_path]

    def kill_pane(self, name: str) -> None:
        _, label_to_id = self._workspace_maps()
        wid = label_to_id.get(name)
        if wid:
            self._run(["workspace", "close", wid], check=False)
            return
        pane_id = self._resolve_pane_id(name)
        self._run(["pane", "close", pane_id], check=False)

    def is_client_attached(self, name: str) -> bool:
        for p in self.list_panes():
            if p.name == name:
                return p.attached
        return False
