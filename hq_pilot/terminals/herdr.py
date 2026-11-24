"""HerdrDriver: TerminalDriver implementation wrapping the `herdr` CLI (v0.9+)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from hq_pilot.terminals.base import PaneInfo, TerminalDriver, shell_join


_HERDR_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_TUI_BORDER_LINE_RE = re.compile(r"^[─━═]{3,}$")
_TUI_MODE_STATUS_RE = re.compile(r"(?:^|\s)(?:auto|plan|ask)\s*·\s*", re.IGNORECASE)


def clean_agent_terminal_output(raw: str) -> str:
    """Strip trailing interactive TUI chrome (borders, empty prompt, shortcuts/model status bar)."""
    if not raw:
        return ""
    lines = raw.splitlines()
    while lines:
        stripped = lines[-1].strip()
        if not stripped:
            lines.pop()
            continue
        if _TUI_BORDER_LINE_RE.match(stripped):
            lines.pop()
            continue
        if stripped in {">", "❯", "›"}:
            lines.pop()
            continue
        if "? for shortcuts" in stripped.lower() or _TUI_MODE_STATUS_RE.search(stripped):
            lines.pop()
            continue
        break
    return "\n".join(lines).strip()


def sanitize_herdr_agent_name(name: str) -> str:
    """Normalize an arbitrary pane/session name to Herdr's `[a-z][a-z0-9_-]{0,31}` rule."""
    raw = (name or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", raw)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if not cleaned:
        cleaned = "hq-agent"
    if not cleaned[0].isalpha():
        cleaned = f"a-{cleaned}"
    if len(cleaned) > 32:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]
        prefix = cleaned[:25].rstrip("-") or "a"
        cleaned = f"{prefix}-{digest}"
    return cleaned


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

    def _list_agents(self) -> List[Dict[str, Any]]:
        data = self._run_json(["agent", "list"], check=False)
        agents = (data.get("result") or {}).get("agents") or []
        return [a for a in agents if isinstance(a, dict)]

    def _find_live_agent(self, name: str) -> Optional[Dict[str, Any]]:
        agent_name = sanitize_herdr_agent_name(name)
        for item in self._list_agents():
            if str(item.get("name", "")) in {name, agent_name}:
                return item
        return None

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
        agent = self._find_live_agent(name)
        if agent and agent.get("pane_id"):
            return str(agent["pane_id"])
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

    def start_agent(
        self,
        name: str,
        cwd: str | Path,
        kind: str,
        agent_args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        timeout_ms: int = 60000,
    ) -> None:
        self.ensure_server()
        agent_name = sanitize_herdr_agent_name(name)
        existing = self._find_live_agent(agent_name)
        if existing is not None:
            return

        ws_cmd = [
            "workspace",
            "create",
            "--cwd",
            str(cwd),
            "--label",
            name,
            "--no-focus",
        ]
        if env:
            for k, v in sorted(env.items()):
                ws_cmd.extend(["--env", f"{k}={v}"])
        created = self._run_json(ws_cmd, check=True)
        root_pane = (created.get("result") or {}).get("root_pane") or {}
        pane_id = str(root_pane.get("pane_id", ""))
        if not pane_id:
            raise RuntimeError(f"Herdr failed to create workspace pane for {name!r}")

        # Give newly spawned shell a brief moment to reach its interactive prompt.
        time.sleep(0.3)

        start_cmd = [
            "agent",
            "start",
            agent_name,
            "--kind",
            kind,
            "--pane",
            pane_id,
            "--timeout",
            str(int(timeout_ms)),
        ]
        if agent_args:
            start_cmd.append("--")
            start_cmd.extend(agent_args)

        res = self._run(start_cmd, check=False)
        if res.returncode != 0:
            err_detail = (res.stderr or res.stdout or "").strip()
            raise RuntimeError(
                f"Herdr agent start failed for {agent_name!r} (rc={res.returncode}): {err_detail}"
            )

    def prompt_agent(
        self,
        name: str,
        text: str,
        wait: bool = True,
        timeout_ms: int = 300000,
    ) -> str:
        agent_name = sanitize_herdr_agent_name(name)
        cmd = ["agent", "prompt", agent_name, text]
        if wait:
            cmd.extend(["--wait", "--timeout", str(int(timeout_ms))])
        res = self._run(cmd, check=False)
        if res.returncode != 0:
            err_detail = (res.stderr or res.stdout or "").strip()
            raise RuntimeError(
                f"Herdr agent prompt failed for {agent_name!r} (rc={res.returncode}): {err_detail}"
            )
        if not wait:
            return ""
        read_res = self._run(
            ["agent", "read", agent_name, "--source", "recent-unwrapped", "--lines", "120"],
            check=False,
        )
        if read_res.returncode == 0 and read_res.stdout.strip():
            return clean_agent_terminal_output(read_res.stdout)
        return clean_agent_terminal_output(self.read_pane(name, lines=120))

    def list_panes(self) -> List[PaneInfo]:
        if not self._server_running():
            return []
        id_to_label, _ = self._workspace_maps()
        agents_raw = self._list_agents()
        agent_by_pane: Dict[str, Dict[str, Any]] = {}
        for ag in agents_raw:
            pid = str(ag.get("pane_id", ""))
            if pid:
                agent_by_pane[pid] = ag

        pane_data = self._run_json(["pane", "list"], check=False)
        panes_raw = (pane_data.get("result") or {}).get("panes") or []
        panes: List[PaneInfo] = []
        seen_names: set[str] = set()
        for item in panes_raw:
            if not isinstance(item, dict):
                continue
            wid = str(item.get("workspace_id", ""))
            pid = str(item.get("pane_id", ""))
            ag = agent_by_pane.get(pid)
            ag_name = str(ag.get("name", "")) if ag else ""
            label = id_to_label.get(wid) or ag_name or pid
            status = str(
                (ag.get("agent_status") if ag else None)
                or item.get("agent_status")
                or "running"
            )
            panes.append(
                PaneInfo(
                    name=label,
                    status=status,
                    attached=False,
                    cwd=str(item.get("cwd", "")),
                )
            )
            seen_names.add(label)
            if ag_name and ag_name not in seen_names:
                panes.append(
                    PaneInfo(
                        name=ag_name,
                        status=status,
                        attached=False,
                        cwd=str(ag.get("cwd") or item.get("cwd", "")),
                    )
                )
                seen_names.add(ag_name)
        for ag in agents_raw:
            ag_name = str(ag.get("name", ""))
            if ag_name and ag_name not in seen_names:
                panes.append(
                    PaneInfo(
                        name=ag_name,
                        status=str(ag.get("agent_status") or "running"),
                        attached=False,
                        cwd=str(ag.get("cwd", "")),
                    )
                )
                seen_names.add(ag_name)
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
        agent = self._find_live_agent(name)
        if agent and agent.get("workspace_id"):
            self._run(["workspace", "close", str(agent["workspace_id"])], check=False)
            return
        pane_id = self._resolve_pane_id(name)
        self._run(["pane", "close", pane_id], check=False)

    def is_client_attached(self, name: str) -> bool:
        for p in self.list_panes():
            if p.name == name:
                return p.attached
        return False
