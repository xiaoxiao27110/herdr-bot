"""HerdrDriver: TerminalDriver implementation wrapping the `herdr` CLI (v0.9+)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from hq_pilot.terminals.base import AgentBlockedError, PaneInfo, TerminalDriver, shell_join


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
        "  3. Until then, herdr-bot with `terminal.backend: auto` automatically uses `tmux`."
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

    def _list_workspaces(self) -> List[Dict[str, Any]]:
        ws_data = self._run_json(["workspace", "list"], check=False)
        workspaces = (ws_data.get("result") or {}).get("workspaces") or []
        return [ws for ws in workspaces if isinstance(ws, dict)]

    def _workspace_maps(self) -> tuple[Dict[str, str], Dict[str, str]]:
        """Return (ws_id -> label, label -> ws_id)."""
        id_to_label: Dict[str, str] = {}
        label_to_id: Dict[str, str] = {}
        for ws in self._list_workspaces():
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

    @staticmethod
    def _is_agent_record_live(agent: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(agent, dict):
            return False
        st = str(agent.get("agent_status") or "").strip().lower()
        return st != "exited"

    def _find_live_agent(
        self, name: str, agents: Optional[List[Dict[str, Any]]] = None
    ) -> Optional[Dict[str, Any]]:
        agent_name = sanitize_herdr_agent_name(name)
        records = agents if agents is not None else self._list_agents()
        for item in records:
            if str(item.get("name", "")) in {name, agent_name} and self._is_agent_record_live(item):
                return item
        return None

    def _close_stale_shell_workspaces(
        self, name: str, agents: List[Dict[str, Any]]
    ) -> None:
        """Close single-pane empty-shell workspaces matching `label == name` with no live agents."""
        live_agent_wids: set[str] = set()
        for ag in agents:
            if not self._is_agent_record_live(ag):
                continue
            ag_wid = str(ag.get("workspace_id") or "").strip()
            if not ag_wid:
                ag_pid = str(ag.get("pane_id") or "").strip()
                if ":" in ag_pid:
                    ag_wid = ag_pid.split(":", 1)[0]
            if ag_wid:
                live_agent_wids.add(ag_wid)

        for ws in self._list_workspaces():
            wid = str(ws.get("workspace_id") or "").strip()
            if not wid or str(ws.get("label") or "") != name:
                continue
            if wid in live_agent_wids:
                continue
            raw_pane_count = ws.get("pane_count")
            if (
                not isinstance(raw_pane_count, int)
                or isinstance(raw_pane_count, bool)
                or raw_pane_count != 1
            ):
                continue
            raw_tab_count = ws.get("tab_count")
            if raw_tab_count is not None and (
                not isinstance(raw_tab_count, int)
                or isinstance(raw_tab_count, bool)
                or raw_tab_count != 1
            ):
                continue
            ws_status = str(ws.get("agent_status") or "").strip().lower()
            if ws_status not in {"unknown", "exited", ""}:
                continue
            self._run(["workspace", "close", wid], check=False)

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
        agents = self._list_agents()
        existing = self._find_live_agent(name, agents=agents)
        if existing is not None:
            return
        self._close_stale_shell_workspaces(name, agents)

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

    def start_agent_in_pane(
        self,
        name: str,
        pane_id: str,
        kind: str,
        agent_args: Optional[List[str]] = None,
        timeout_ms: int = 60000,
    ) -> None:
        """Start an interactive coding agent in-place inside an existing Herdr shell pane."""
        self.ensure_server()
        pid = (pane_id or "").strip()
        if not pid:
            raise ValueError("pane_id must not be empty for start_agent_in_pane")
        agent_name = sanitize_herdr_agent_name(name)
        start_cmd = [
            "agent",
            "start",
            agent_name,
            "--kind",
            kind,
            "--pane",
            pid,
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
                f"Herdr agent start failed for {agent_name!r} in pane {pid!r} "
                f"(rc={res.returncode}): {err_detail}"
            )

    @staticmethod
    def _parse_json_payload(raw: str) -> Dict[str, Any]:
        text = (raw or "").strip()
        if not text:
            return {}
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
        return {}

    @classmethod
    def _extract_blocked_detail(cls, stdout: str, stderr: str) -> Optional[str]:
        for raw in (stdout, stderr):
            data = cls._parse_json_payload(raw)
            if not data:
                continue
            err = data.get("error")
            err_code = ""
            err_msg = ""
            if isinstance(err, dict):
                err_code = str(err.get("code") or err.get("error_code") or "").strip().lower()
                err_msg = str(err.get("message") or err.get("detail") or "").strip()
            elif isinstance(err, str):
                err_code = err.strip().lower()
                err_msg = err.strip()
            if not err_code:
                err_code = str(data.get("code") or data.get("error_code") or "").strip().lower()

            result = data.get("result") if isinstance(data.get("result"), dict) else {}
            agent_obj = result.get("agent") if isinstance(result.get("agent"), dict) else {}
            if not agent_obj and isinstance(data.get("agent"), dict):
                agent_obj = data["agent"]
            agent_status = str(
                agent_obj.get("agent_status")
                or result.get("agent_status")
                or data.get("agent_status")
                or ""
            ).strip().lower()

            if err_code == "agent_blocked" or agent_status == "blocked":
                return err_msg or (raw or "").strip()
        return None

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
        blocked_detail = self._extract_blocked_detail(res.stdout, res.stderr)
        if blocked_detail is not None:
            raise AgentBlockedError(pane_name=agent_name, detail=blocked_detail)
        if res.returncode != 0:
            err_detail = (res.stderr or res.stdout or "").strip()
            raise RuntimeError(
                f"Herdr agent prompt failed for {agent_name!r} (rc={res.returncode}): {err_detail}"
            )
        return ""

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
            is_live = (ag is not None) and (status.strip().lower() != "exited")
            panes.append(
                PaneInfo(
                    name=label,
                    status=status,
                    attached=False,
                    cwd=str(item.get("cwd", "")),
                    agent_live=is_live,
                    pane_id=pid,
                    workspace_id=wid,
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
                        agent_live=is_live,
                        pane_id=pid,
                        workspace_id=str(ag.get("workspace_id") or wid),
                    )
                )
                seen_names.add(ag_name)
        for ag in agents_raw:
            ag_name = str(ag.get("name", ""))
            if ag_name and ag_name not in seen_names:
                ag_status = str(ag.get("agent_status") or "running")
                ag_pid = str(ag.get("pane_id", ""))
                ag_wid = str(
                    ag.get("workspace_id")
                    or (ag_pid.split(":", 1)[0] if ":" in ag_pid else "")
                )
                panes.append(
                    PaneInfo(
                        name=ag_name,
                        status=ag_status,
                        attached=False,
                        cwd=str(ag.get("cwd", "")),
                        agent_live=(ag_status.strip().lower() != "exited"),
                        pane_id=ag_pid,
                        workspace_id=ag_wid,
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

    def send_keys(self, name: str, keys: List[str]) -> None:
        clean_keys = [str(k).strip() for k in keys if str(k).strip()]
        if not clean_keys:
            return
        agent_name = sanitize_herdr_agent_name(name)
        res = self._run(["agent", "send-keys", agent_name, *clean_keys], check=False)
        if res.returncode == 0:
            return
        pane_id = self._resolve_pane_id(name)
        res2 = self._run(["pane", "send-keys", pane_id, *clean_keys], check=False)
        if res2.returncode != 0:
            err_detail = (res2.stderr or res2.stdout or res.stderr or res.stdout or "").strip()
            raise RuntimeError(
                f"Herdr send-keys failed for {name!r} (rc={res2.returncode}): {err_detail}"
            )

    def read_visible_pane(self, name: str, lines: int = 30) -> str:
        agent_name = sanitize_herdr_agent_name(name)
        res = self._run(
            [
                "agent",
                "read",
                agent_name,
                "--source",
                "visible",
                "--lines",
                str(int(lines)),
                "--format",
                "text",
            ],
            check=False,
        )
        if res.returncode == 0:
            out = res.stdout or ""
            stripped = out.strip()
            if stripped.startswith("{"):
                try:
                    obj = json.loads(stripped)
                    if isinstance(obj, dict) and isinstance(obj.get("result"), dict):
                        r = obj["result"]
                        if isinstance(r.get("text"), str):
                            return r["text"]
                        if isinstance(r.get("content"), str):
                            return r["content"]
                        if isinstance(r.get("lines"), list):
                            return "\n".join(str(x) for x in r["lines"])
                except json.JSONDecodeError:
                    pass
            return out
        return self.read_pane(name, lines=lines)

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
