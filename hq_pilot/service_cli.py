"""Systemd user-service management (`herdr-bot service install|status|restart|uninstall`)."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Optional

from hq_pilot.agents.registry import get_agent_adapter
from hq_pilot.config import HQConfig, load_config


_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")


def normalize_service_name(name: str) -> str:
    """Validate and normalize a systemd unit base name (without `.service` suffix)."""
    raw = (name or "").strip()
    if raw.endswith(".service"):
        raw = raw[: -len(".service")]
    if not raw or raw in {".", ".."} or not _SERVICE_NAME_RE.match(raw):
        raise ValueError(f"Invalid systemd service name: {name!r}")
    return raw


def _systemd_env_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _systemd_exec_arg(value: str) -> str:
    if any(ch.isspace() or ch in {'"', "'", "\\"} for ch in value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _resolve_python_bin(cfg: HQConfig) -> str:
    candidates = [
        cfg.local.bin_overrides.get("python", ""),
        os.environ.get("HQ_PYTHON", ""),
        sys.executable,
    ]
    for raw in candidates:
        item = (raw or "").strip()
        if not item:
            continue
        expanded = os.path.abspath(os.path.expanduser(item))
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    return os.path.abspath(sys.executable)


def _resolve_herdr_bot_script() -> str:
    return str((Path(__file__).resolve().parent.parent / "bin" / "herdr-bot").resolve())


def _dir_of_executable(
    tool_name: str,
    overrides: Dict[str, str],
) -> Optional[str]:
    override = (overrides.get(tool_name) or "").strip()
    if override:
        expanded = Path(override).expanduser()
        if expanded.is_absolute() or os.sep in override:
            return str(expanded.parent)
        found = shutil.which(override)
        if found:
            return str(Path(found).parent)
    found = shutil.which(tool_name)
    if found:
        return str(Path(found).parent)
    return None


def build_service_path(
    *,
    python_bin: str,
    user_home: Path,
    cfg: HQConfig,
) -> str:
    """Build a deterministic, deduplicated PATH for non-interactive systemd execution."""
    std_dirs = ("/usr/local/bin", "/usr/bin", "/bin")
    candidates: List[str] = [
        str(Path(python_bin).parent),
        str(user_home / ".local" / "bin"),
    ]
    agent_tools: List[str] = [cfg.agent.backend]
    try:
        adapter = get_agent_adapter(
            cfg.agent.backend,
            bin_path=cfg.get_agent_bin_override(),
        )
        desc = adapter.describe()
        for t in (desc.get("name"), desc.get("default_bin")):
            if t and str(t) not in agent_tools:
                agent_tools.append(str(t))
    except Exception:
        pass

    for tool in ("herdr", *agent_tools, "git", "tmux"):
        if not tool:
            continue
        tool_dir = _dir_of_executable(tool, cfg.local.bin_overrides)
        if tool_dir and tool_dir not in std_dirs:
            candidates.append(tool_dir)
    candidates.extend(std_dirs)

    seen: set[str] = set()
    deduped: List[str] = []
    for entry in candidates:
        norm = (entry or "").strip()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        deduped.append(norm)
    return ":".join(deduped)


def render_systemd_unit(
    hq_dir: str | Path,
    *,
    user_home: Optional[Path] = None,
    python_bin: Optional[str] = None,
    herdr_bot_script: Optional[str] = None,
) -> str:
    """Render a `systemd --user` unit file for `herdr-bot up`."""
    resolved_hq = Path(hq_dir).expanduser().resolve()
    cfg = load_config(resolved_hq)
    home = (user_home or Path.home()).expanduser().resolve()
    py_bin = (
        os.path.abspath(os.path.expanduser(python_bin))
        if python_bin
        else _resolve_python_bin(cfg)
    )
    script_path = (
        str(Path(herdr_bot_script).expanduser().resolve())
        if herdr_bot_script
        else _resolve_herdr_bot_script()
    )
    path_env = build_service_path(python_bin=py_bin, user_home=home, cfg=cfg)
    exec_start = " ".join(
        [
            _systemd_exec_arg(py_bin),
            _systemd_exec_arg(script_path),
            "up",
            "--hq-dir",
            _systemd_exec_arg(str(resolved_hq)),
        ]
    )
    lines = [
        "[Unit]",
        f"Description=herdr-bot 24/7 R&D orchestrator ({cfg.project_name})",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={resolved_hq}",
        f"Environment={_systemd_env_quote(f'HOME={home}')}",
        f"Environment={_systemd_env_quote(f'PATH={path_env}')}",
        f"ExecStart={exec_start}",
        "Restart=always",
        "RestartSec=5",
        "KillMode=process",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _systemd_user_unit_path(name: str, user_home: Optional[Path] = None) -> Path:
    base = normalize_service_name(name)
    home = (user_home or Path.home()).expanduser().resolve()
    return home / ".config" / "systemd" / "user" / f"{base}.service"


def _run_cmd(cmd: List[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(
            cmd,
            127,
            stdout="",
            stderr=str(exc),
        )


def cmd_service_install(args: argparse.Namespace) -> int:
    try:
        svc_name = normalize_service_name(args.name)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    unit_text = render_systemd_unit(args.hq_dir)
    if args.render_only:
        sys.stdout.write(unit_text)
        return 0

    unit_filename = f"{svc_name}.service"
    unit_path = _systemd_user_unit_path(svc_name)
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit_text, encoding="utf-8")
    print(f"Wrote systemd user unit: {unit_path}")

    reload_res = _run_cmd(["systemctl", "--user", "daemon-reload"])
    if reload_res.returncode != 0:
        err = (reload_res.stderr or reload_res.stdout or "daemon-reload failed").strip()
        print(f"Error: systemctl --user daemon-reload failed: {err}", file=sys.stderr)
        return reload_res.returncode or 1

    enable_res = _run_cmd(["systemctl", "--user", "enable", "--now", unit_filename])
    if enable_res.returncode != 0:
        err = (enable_res.stderr or enable_res.stdout or "enable --now failed").strip()
        print(f"Error: systemctl --user enable --now {unit_filename} failed: {err}", file=sys.stderr)
        return enable_res.returncode or 1
    print(f"Enabled and started {unit_filename}")

    current_user = getpass.getuser()
    linger_res = _run_cmd(["loginctl", "enable-linger", current_user])
    if linger_res.returncode == 0:
        print(f"Enabled systemd user linger for {current_user} (boot auto-start active).")
    else:
        detail = (linger_res.stderr or linger_res.stdout or "").strip()
        hint = f" ({detail})" if detail else ""
        print(
            f"[note] Could not enable user linger for {current_user}{hint}. "
            f"To allow boot-time startup before login, ask an administrator to run: "
            f"sudo loginctl enable-linger {current_user}"
        )
    return 0


def cmd_service_status(args: argparse.Namespace) -> int:
    try:
        svc_name = normalize_service_name(args.name)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    unit_filename = f"{svc_name}.service"
    res = _run_cmd(["systemctl", "--user", "status", unit_filename, "--no-pager"])
    if res.stdout:
        sys.stdout.write(res.stdout)
    if res.stderr:
        sys.stderr.write(res.stderr)
    return res.returncode


def cmd_service_restart(args: argparse.Namespace) -> int:
    try:
        svc_name = normalize_service_name(args.name)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    unit_filename = f"{svc_name}.service"
    res = _run_cmd(["systemctl", "--user", "restart", unit_filename])
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "restart failed").strip()
        print(f"Error: systemctl --user restart {unit_filename} failed: {err}", file=sys.stderr)
        return res.returncode or 1
    print(f"Restarted {unit_filename}")
    return 0


def cmd_service_uninstall(args: argparse.Namespace) -> int:
    try:
        svc_name = normalize_service_name(args.name)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    unit_filename = f"{svc_name}.service"
    unit_path = _systemd_user_unit_path(svc_name)

    disable_res = _run_cmd(["systemctl", "--user", "disable", "--now", unit_filename])
    if disable_res.returncode == 0:
        print(f"Disabled and stopped {unit_filename}")
    else:
        detail = (disable_res.stderr or disable_res.stdout or "").strip()
        if detail:
            print(f"[note] systemctl disable --now {unit_filename}: {detail}")

    if unit_path.exists():
        unit_path.unlink()
        print(f"Removed systemd user unit: {unit_path}")
    else:
        print(f"Unit file not present: {unit_path}")

    _run_cmd(["systemctl", "--user", "daemon-reload"])
    return 0


def register_service_subparser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    sp_svc = sub.add_parser(
        "service",
        help="Manage systemd --user background service for auto-start on boot",
    )
    svc_sub = sp_svc.add_subparsers(dest="service_action", required=True)

    sp_install = svc_sub.add_parser(
        "install",
        help="Install and enable systemd --user service unit",
    )
    sp_install.add_argument("--hq-dir", default=".", help="Target HQ workspace directory")
    sp_install.add_argument("--name", default="herdr-bot", help="Systemd service name")
    sp_install.add_argument(
        "--render-only",
        action="store_true",
        help="Print generated unit file to stdout without installing",
    )
    sp_install.set_defaults(func=cmd_service_install)

    sp_status = svc_sub.add_parser("status", help="Show systemd --user service status")
    sp_status.add_argument("--name", default="herdr-bot", help="Systemd service name")
    sp_status.set_defaults(func=cmd_service_status)

    sp_restart = svc_sub.add_parser("restart", help="Restart systemd --user service")
    sp_restart.add_argument("--name", default="herdr-bot", help="Systemd service name")
    sp_restart.set_defaults(func=cmd_service_restart)

    sp_uninstall = svc_sub.add_parser(
        "uninstall",
        help="Disable, stop, and remove systemd --user service unit",
    )
    sp_uninstall.add_argument("--name", default="herdr-bot", help="Systemd service name")
    sp_uninstall.set_defaults(func=cmd_service_uninstall)
