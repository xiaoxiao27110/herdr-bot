"""Interactive and non-interactive `herdr-bot setup` onboarding wizard."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple
import yaml

from hq_pilot.config import TuningConfig, _read_yaml_file, load_secrets_env
from hq_pilot.guards import run_memory_guard

InputFn = Callable[[str], str]


def _detect_executable(
    name: str,
    explicit: str = "",
    existing_override: str = "",
) -> str:
    for candidate in (explicit, existing_override):
        if candidate:
            p = Path(candidate).expanduser()
            if p.is_file() and os.access(p, os.X_OK):
                return str(p.resolve())
            found = shutil.which(candidate)
            if found:
                return str(Path(found).resolve())
    which_hit = shutil.which(name)
    if which_hit:
        return str(Path(which_hit).resolve())
    local_bin = Path.home() / ".local" / "bin" / name
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin.resolve())
    return ""


def inspect_git_repo(repo_path: Path) -> Tuple[str, str]:
    """Return `(git_url, default_branch)` inspected from a local git repository."""
    git_url = ""
    url_res = subprocess.run(
        ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if url_res.returncode == 0 and url_res.stdout.strip():
        git_url = url_res.stdout.strip()

    default_branch = ""
    head_ref = subprocess.run(
        ["git", "-C", str(repo_path), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if head_ref.returncode == 0 and head_ref.stdout.strip():
        raw_ref = head_ref.stdout.strip()
        default_branch = raw_ref.split("/", 1)[1] if "/" in raw_ref else raw_ref

    if not default_branch:
        cur_branch = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if cur_branch.returncode == 0:
            candidate = cur_branch.stdout.strip()
            if candidate and candidate != "HEAD" and not candidate.startswith("hq/"):
                default_branch = candidate

    return git_url, (default_branch or "main")


def parse_repo_spec(spec: str) -> Tuple[str, Path]:
    """Parse `name=path` or `path` into `(repo_name, resolved_path)`."""
    raw = (spec or "").strip()
    if not raw:
        raise ValueError("Empty --repo specification")
    if "=" in raw:
        name, path_str = raw.split("=", 1)
        name = name.strip()
        path = Path(path_str.strip()).expanduser().resolve()
    else:
        path = Path(raw).expanduser().resolve()
        name = path.name
    if not name:
        raise ValueError(f"Invalid repository name in spec: {spec!r}")
    if not path.exists() or not path.is_dir():
        raise ValueError(f"Repository path does not exist: {path}")
    return name, path


def _prompt_with_default(prompt_fn: InputFn, label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    ans = prompt_fn(f"{label}{suffix}: ").strip()
    return ans if ans else default


def _build_agents_md(
    project_name: str,
    repo_meta: Dict[str, Dict[str, Any]],
) -> str:
    lines: List[str] = [
        f"# Project Memory Index ({project_name})",
        "",
        "> Keep this index concise (`<= 200` lines, `<= 25600` bytes). Use `<repo>:<relpath>` logical paths.",
        "",
        "## Repositories",
    ]
    if repo_meta:
        for rname, info in repo_meta.items():
            rpath = Path(info["path"])
            branch = info["default_branch"]
            if (rpath / "README.md").is_file():
                lines.append(f"- `{rname}`: default branch `{branch}` (`{rname}:README.md`)")
            else:
                lines.append(f"- `{rname}`: default branch `{branch}`")
    else:
        lines.append("- Add managed repositories in `hq.yaml` and `.hq/local.yaml`.")

    lines.extend(
        [
            "",
            "## Key Contracts & Decisions",
            "- Cross-repo contracts live in `memory/contracts/`.",
            "- Architecture decisions & invariants live in `memory/decisions.md`.",
            "- User context, preferences & corrections live in `memory/user-notes.md`.",
            "- Active cross-repo work is tracked in `tasks/active-wip.md`.",
            "",
            "## #General & #Topic Auto-Orchestration",
            "- **轻量问答直回**：打招呼、轻量问答、状态查询、临时汇报直接在 `#General` 回复，不开 Topic。",
            "- **独立开发自动开 Topic**：遇到需要修改代码、跑测试或长链路排查的独立开发需求时，`#General` 自主执行 `herdr-bot task spawn <repo> <slug> \"<需求>\" --hq-dir \"$HQ_DIR\"` 拉起独立 worktree + Herdr 面板 + 话题卡片。",
            "- **验收后自动封口**：`#Topic` 完成并验收通过后，Agent 可调用 `herdr-bot seal \"$HQ_SESSION_KEY\" --hq-dir \"$HQ_DIR\"` 自主封口（否则空闲超时也会自动封口）。",
            "",
        ]
    )
    return "\n".join(lines)


def run_setup(
    args: argparse.Namespace,
    input_fn: InputFn = input,
) -> int:
    non_interactive = bool(getattr(args, "non_interactive", False))

    raw_hq_dir = getattr(args, "hq_dir", None)
    if not raw_hq_dir:
        if non_interactive:
            raw_hq_dir = "."
        else:
            raw_hq_dir = _prompt_with_default(
                input_fn,
                "HQ workspace directory",
                str(Path.home() / "herdr-hq"),
            )
    hq_dir = Path(raw_hq_dir).expanduser().resolve()
    hq_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("memory/contracts", "tasks", "notes/archive", ".hq/worktrees", ".hq/logs"):
        (hq_dir / sub).mkdir(parents=True, exist_ok=True)

    existing_hq = _read_yaml_file(hq_dir / "hq.yaml")
    existing_local = _read_yaml_file(hq_dir / ".hq" / "local.yaml")
    existing_secrets = load_secrets_env(hq_dir)

    default_proj = str(existing_hq.get("project_name") or hq_dir.name)
    project_name = getattr(args, "project_name", None)
    if not project_name:
        if non_interactive:
            project_name = default_proj
        else:
            project_name = _prompt_with_default(input_fn, "Project name", default_proj)

    repo_specs: List[str] = list(getattr(args, "repos", None) or [])
    if not repo_specs and not non_interactive and not (existing_hq.get("repos") or {}):
        print("Add local Git repositories (enter `name=/path/to/repo` or `/path/to/repo`, empty line to finish):")
        while True:
            entry = input_fn("  Repo: ").strip()
            if not entry:
                break
            repo_specs.append(entry)

    repo_meta: Dict[str, Dict[str, Any]] = {}
    existing_repos_raw = existing_hq.get("repos") if isinstance(existing_hq.get("repos"), dict) else {}
    existing_repo_paths = (
        existing_local.get("repo_paths") if isinstance(existing_local.get("repo_paths"), dict) else {}
    )
    for rname, rcfg in (existing_repos_raw or {}).items():
        rcfg = rcfg or {}
        rpath_str = str((existing_repo_paths or {}).get(rname, ""))
        repo_meta[str(rname)] = {
            "git_url": str(rcfg.get("git_url", "")),
            "default_branch": str(rcfg.get("default_branch", "main")),
            "path": rpath_str,
        }

    for spec in repo_specs:
        rname, rpath = parse_repo_spec(spec)
        git_url, default_branch = inspect_git_repo(rpath)
        repo_meta[rname] = {
            "git_url": git_url,
            "default_branch": default_branch,
            "path": str(rpath),
        }

    channel = getattr(args, "channel", None)
    if not channel:
        if getattr(args, "feishu_app_id", "") or getattr(args, "feishu_chat_id", ""):
            channel = "feishu"
        elif getattr(args, "telegram_bot_token", "") or getattr(args, "telegram_chat_id", ""):
            channel = "telegram"
        elif non_interactive:
            prev_chan = (existing_hq.get("channel") or {}).get("backend")
            channel = str(prev_chan) if prev_chan in {"feishu", "telegram", "local"} else "local"
        else:
            prev_chan = str((existing_hq.get("channel") or {}).get("backend") or "feishu")
            chosen = _prompt_with_default(
                input_fn,
                "Messaging channel (feishu | telegram | local)",
                prev_chan,
            ).lower()
            channel = chosen if chosen in {"feishu", "telegram", "local"} else "local"

    feishu_app_id = (getattr(args, "feishu_app_id", "") or existing_secrets.get("FEISHU_APP_ID", "")).strip()
    feishu_app_secret = (
        getattr(args, "feishu_app_secret", "") or existing_secrets.get("FEISHU_APP_SECRET", "")
    ).strip()
    feishu_chat_id = (getattr(args, "feishu_chat_id", "") or existing_secrets.get("FEISHU_CHAT_ID", "")).strip()
    tg_token = (
        getattr(args, "telegram_bot_token", "") or existing_secrets.get("TELEGRAM_BOT_TOKEN", "")
    ).strip()
    tg_chat_id = (getattr(args, "telegram_chat_id", "") or existing_secrets.get("TELEGRAM_CHAT_ID", "")).strip()

    if not non_interactive:
        if channel == "feishu":
            feishu_app_id = _prompt_with_default(input_fn, "Feishu App ID (FEISHU_APP_ID)", feishu_app_id)
            feishu_app_secret = _prompt_with_default(
                input_fn, "Feishu App Secret (FEISHU_APP_SECRET)", feishu_app_secret
            )
            feishu_chat_id = _prompt_with_default(
                input_fn, "Feishu Allowed Chat ID (FEISHU_CHAT_ID, e.g. oc_xxx)", feishu_chat_id
            )
        elif channel == "telegram":
            tg_token = _prompt_with_default(input_fn, "Telegram Bot Token (TELEGRAM_BOT_TOKEN)", tg_token)
            tg_chat_id = _prompt_with_default(
                input_fn, "Telegram Allowed Chat ID (TELEGRAM_CHAT_ID)", tg_chat_id
            )

    allowed_chat_id = ""
    if channel == "feishu":
        allowed_chat_id = feishu_chat_id
    elif channel == "telegram":
        allowed_chat_id = tg_chat_id

    # 1. Write hq.yaml
    agent_block = existing_hq.get("agent") if isinstance(existing_hq.get("agent"), dict) else {}
    term_block = existing_hq.get("terminal") if isinstance(existing_hq.get("terminal"), dict) else {}
    tuning_cfg = TuningConfig.from_dict(
        existing_hq.get("tuning") if isinstance(existing_hq.get("tuning"), dict) else None
    )

    hq_yaml_obj: Dict[str, Any] = {
        "version": int(existing_hq.get("version", 1)),
        "project_name": project_name,
        "agent": {
            "backend": str(agent_block.get("backend", "agy")),
            "index_file": str(agent_block.get("index_file", "AGENTS.md")),
            "default_effort": str(agent_block.get("default_effort", "high")),
            "light_effort": str(agent_block.get("light_effort", "low")),
            **(
                {"model": agent_block["model"]}
                if agent_block.get("model") is not None
                else {}
            ),
        },
        "terminal": {
            "backend": str(term_block.get("backend", "auto")),
        },
        "channel": {
            "backend": channel,
            "allowed_chat_id": allowed_chat_id,
        },
        "repos": {
            rname: {
                "git_url": info["git_url"],
                "default_branch": info["default_branch"],
            }
            for rname, info in repo_meta.items()
        },
        "tuning": tuning_cfg.to_dict(),
    }
    (hq_dir / "hq.yaml").write_text(
        yaml.safe_dump(hq_yaml_obj, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # 2. Write / update repo-boundaries.yaml
    boundaries_path = hq_dir / "repo-boundaries.yaml"
    existing_boundaries = _read_yaml_file(boundaries_path)
    existing_b_repos = (
        existing_boundaries.get("repos") if isinstance(existing_boundaries.get("repos"), dict) else {}
    )
    b_repos_out: Dict[str, Any] = {}
    for rname, info in repo_meta.items():
        if rname in existing_b_repos and isinstance(existing_b_repos[rname], dict):
            entry = dict(existing_b_repos[rname])
            entry.setdefault("default_branch", info["default_branch"])
            b_repos_out[rname] = entry
        else:
            b_repos_out[rname] = {
                "default_branch": info["default_branch"],
                "watch_paths": ["**"],
                "ignore_paths": ["**/*.md", "tests/**"],
                "downstream": [],
            }
    boundaries_path.write_text(
        yaml.safe_dump({"version": 1, "repos": b_repos_out}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # 3. Initialize markdown scaffolds & AGENTS.md
    gi = hq_dir / ".gitignore"
    if not gi.exists():
        gi.write_text(".hq/\n", encoding="utf-8")

    decisions = hq_dir / "memory" / "decisions.md"
    if not decisions.exists():
        decisions.write_text("# Architecture Decisions\n\n", encoding="utf-8")

    user_notes = hq_dir / "memory" / "user-notes.md"
    if not user_notes.exists():
        tpl_user_notes = Path(__file__).resolve().parent.parent / "templates" / "user-notes.example.md"
        if tpl_user_notes.is_file():
            shutil.copyfile(tpl_user_notes, user_notes)
        else:
            user_notes.write_text(
                "# User Notes\n\n## Role & Context\n-\n\n## Preferences\n-\n\n## Corrections & Guardrails\n-\n",
                encoding="utf-8",
            )

    wip = hq_dir / "tasks" / "active-wip.md"
    if not wip.exists():
        wip.write_text("# Active Cross-Repo WIP\n\n", encoding="utf-8")

    today = hq_dir / "notes" / "today.md"
    if not today.exists():
        today.write_text("# Today WAL [UNVERIFIED / TODAY ONLY]\n\n", encoding="utf-8")

    agents_md_path = hq_dir / str(hq_yaml_obj["agent"]["index_file"])
    if not agents_md_path.exists():
        agents_md_path.write_text(
            _build_agents_md(project_name, repo_meta),
            encoding="utf-8",
        )

    # 4. Write .hq/local.yaml with detected host binaries & repo_paths
    existing_overrides = (
        existing_local.get("bin_overrides")
        if isinstance(existing_local.get("bin_overrides"), dict)
        else {}
    )
    python_bin = (
        getattr(args, "python_bin", "")
        or os.environ.get("HQ_PYTHON", "")
        or str(existing_local.get("python_bin") or "")
        or sys.executable
    )
    python_bin = str(Path(python_bin).expanduser().resolve())

    agy_bin = _detect_executable(
        "agy",
        explicit=getattr(args, "agy_bin", ""),
        existing_override=str(existing_local.get("agy_bin") or existing_overrides.get("agy") or ""),
    )
    herdr_bin = _detect_executable(
        "herdr",
        explicit=getattr(args, "herdr_bin", ""),
        existing_override=str(existing_local.get("herdr_bin") or existing_overrides.get("herdr") or ""),
    )

    repo_paths_out: Dict[str, str] = {}
    for rname, info in repo_meta.items():
        if info.get("path"):
            repo_paths_out[rname] = str(info["path"])

    bin_overrides_out: Dict[str, str] = {
        str(k): str(v) for k, v in existing_overrides.items() if v
    }
    if agy_bin:
        bin_overrides_out["agy"] = agy_bin
    if herdr_bin:
        bin_overrides_out["herdr"] = herdr_bin

    local_yaml_obj: Dict[str, Any] = {
        "python_bin": python_bin,
    }
    if agy_bin:
        local_yaml_obj["agy_bin"] = agy_bin
    if herdr_bin:
        local_yaml_obj["herdr_bin"] = herdr_bin
    local_yaml_obj["repo_paths"] = repo_paths_out
    local_yaml_obj["bin_overrides"] = bin_overrides_out

    local_yaml_path = hq_dir / ".hq" / "local.yaml"
    local_yaml_path.parent.mkdir(parents=True, exist_ok=True)
    local_yaml_path.write_text(
        yaml.safe_dump(local_yaml_obj, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # 5. Write .hq/secrets.env with 0600 permissions
    merged_secrets = dict(existing_secrets)
    merged_secrets["FEISHU_APP_ID"] = feishu_app_id
    merged_secrets["FEISHU_APP_SECRET"] = feishu_app_secret
    merged_secrets["FEISHU_CHAT_ID"] = feishu_chat_id
    merged_secrets["TELEGRAM_BOT_TOKEN"] = tg_token
    merged_secrets["TELEGRAM_CHAT_ID"] = tg_chat_id

    secret_lines = [
        "# Generated by `herdr-bot setup` (chmod 0600)",
        f"FEISHU_APP_ID={merged_secrets.get('FEISHU_APP_ID', '')}",
        f"FEISHU_APP_SECRET={merged_secrets.get('FEISHU_APP_SECRET', '')}",
        f"FEISHU_CHAT_ID={merged_secrets.get('FEISHU_CHAT_ID', '')}",
        "",
        f"TELEGRAM_BOT_TOKEN={merged_secrets.get('TELEGRAM_BOT_TOKEN', '')}",
        f"TELEGRAM_CHAT_ID={merged_secrets.get('TELEGRAM_CHAT_ID', '')}",
    ]
    reserved_keys = {
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_CHAT_ID",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    }
    extra_keys = sorted(k for k in merged_secrets.keys() if k not in reserved_keys)
    if extra_keys:
        secret_lines.append("")
        for k in extra_keys:
            secret_lines.append(f"{k}={merged_secrets[k]}")

    secrets_path = hq_dir / ".hq" / "secrets.env"
    secrets_path.write_text("\n".join(secret_lines) + "\n", encoding="utf-8")
    os.chmod(secrets_path, 0o600)

    guard_res = run_memory_guard(hq_dir)
    if not guard_res.ok:
        for v in guard_res.violations:
            print(f"[WARN] memory_guard: {v}", file=sys.stderr)

    print(f"Configured herdr-bot workspace at {hq_dir}")
    print(f"  - Config:  {hq_dir / 'hq.yaml'} (channel={channel}, repos={len(repo_meta)})")
    print(f"  - Local:   {local_yaml_path} (python={python_bin}, agy={agy_bin or 'NOT FOUND'}, herdr={herdr_bin or 'NOT FOUND'})")
    print(f"  - Secrets: {secrets_path} (mode 0600)")
    print("\nNext steps:")
    print(f"  1. Start the orchestrator daemon:\n     herdr-bot up --hq-dir {hq_dir}")
    print("  2. Attach to live interactive agent panes anytime:\n     herdr")
    return 0


def register_setup_subparser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    sp = sub.add_parser(
        "setup",
        help="Interactive or non-interactive onboarding wizard for herdr-bot",
    )
    sp.add_argument("--hq-dir", default=None, help="Target HQ workspace directory")
    sp.add_argument("--project-name", default=None, help="Project name written to hq.yaml")
    sp.add_argument(
        "--repo",
        action="append",
        dest="repos",
        default=[],
        metavar="NAME=PATH",
        help="Register a local Git repository (`name=/path/to/repo` or `/path/to/repo`, repeatable)",
    )
    sp.add_argument(
        "--channel",
        choices=["feishu", "telegram", "local"],
        default=None,
        help="Primary messaging channel backend",
    )
    sp.add_argument("--feishu-app-id", default="", help="Feishu App ID")
    sp.add_argument("--feishu-app-secret", default="", help="Feishu App Secret")
    sp.add_argument("--feishu-chat-id", default="", help="Feishu allowed chat_id (e.g. oc_xxx)")
    sp.add_argument("--telegram-bot-token", default="", help="Telegram Bot Token")
    sp.add_argument("--telegram-chat-id", default="", help="Telegram allowed chat_id")
    sp.add_argument("--python-bin", default="", help="Override python_bin in .hq/local.yaml")
    sp.add_argument("--agy-bin", default="", help="Override agy_bin in .hq/local.yaml")
    sp.add_argument("--herdr-bin", default="", help="Override herdr_bin in .hq/local.yaml")
    sp.add_argument(
        "--non-interactive",
        action="store_true",
        help="Run without interactive prompts using provided flags and defaults",
    )
    sp.set_defaults(func=run_setup)
