"""Main `herdr-bot` (`hq`) CLI entrypoint."""

from __future__ import annotations

import argparse
from dataclasses import fields
from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import List, Optional

from hq_pilot.agents.registry import get_agent_adapter
from hq_pilot.archiver import archive_eligible_sessions
from hq_pilot.channels.feishu import detect_feishu_chats
from hq_pilot.channels.telegram import detect_telegram_chats
from hq_pilot.chat_cli import run_chat_loop
from hq_pilot.config import (
    TUNING_DESCRIPTIONS,
    TuningConfig,
    load_config,
    load_secrets_env,
    update_tuning_value,
)
from hq_pilot.daemon import HQDaemon
from hq_pilot.db import HQDatabase
from hq_pilot.guards import run_memory_guard, run_oss_guard
from hq_pilot.setup_cli import register_setup_subparser
from hq_pilot.syncer import sync_hq_repo
from hq_pilot.terminals.base import git_worktree_add
from hq_pilot.terminals.herdr import install_herdr_guide
from hq_pilot.terminals.registry import get_terminal_driver


def _templates_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "templates"


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    for sub in ("memory/contracts", "tasks", "notes/archive", ".hq/worktrees", ".hq/logs"):
        (target / sub).mkdir(parents=True, exist_ok=True)

    tdir = _templates_dir()
    copies = [
        ("hq.example.yaml", "hq.yaml"),
        ("repo-boundaries.example.yaml", "repo-boundaries.yaml"),
        ("AGENTS.example.md", "AGENTS.md"),
        ("user-notes.example.md", "memory/user-notes.md"),
        ("secrets.env.example", ".hq/secrets.env.example"),
    ]
    for src_name, dst_rel in copies:
        src = tdir / src_name
        dst = target / dst_rel
        if src.exists() and not dst.exists():
            shutil.copyfile(src, dst)

    gi = target / ".gitignore"
    if not gi.exists():
        gi.write_text(".hq/\n", encoding="utf-8")

    decisions = target / "memory" / "decisions.md"
    if not decisions.exists():
        decisions.write_text("# Architecture Decisions\n\n", encoding="utf-8")

    wip = target / "tasks" / "active-wip.md"
    if not wip.exists():
        wip.write_text("# Active Cross-Repo WIP\n\n", encoding="utf-8")

    today = target / "notes" / "today.md"
    if not today.exists():
        today.write_text("# Today WAL [UNVERIFIED / TODAY ONLY]\n\n", encoding="utf-8")

    local_ex = target / ".hq" / "local.yaml.example"
    if not local_ex.exists():
        local_ex.write_text("repo_paths: {}\nbin_overrides: {}\n", encoding="utf-8")

    print(f"Initialized HQ workspace at {target}")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    cfg = load_config(args.hq_dir)
    for name, repo in sorted(cfg.repos.items()):
        target = cfg.get_repo_path(name)
        if target.exists():
            print(f"[ok] {name} already exists at {target}")
            continue
        if not repo.git_url:
            print(f"[skip] {name} has no git_url configured")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--branch", repo.default_branch, repo.git_url, str(target)],
            check=True,
        )
        print(f"[cloned] {name} -> {target}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.hq_dir)
    print(f"Project: {cfg.project_name} ({cfg.hq_dir})")
    print(f"git: {shutil.which('git') or 'MISSING'}")
    agent_bin = cfg.local.bin_overrides.get(cfg.agent.backend) or shutil.which(cfg.agent.backend)
    print(f"agent ({cfg.agent.backend}): {agent_bin or 'NOT FOUND'}")
    herdr_path = (
        cfg.local.bin_overrides.get("herdr")
        or shutil.which("herdr")
        or (str(Path.home() / ".local" / "bin" / "herdr") if (Path.home() / ".local" / "bin" / "herdr").is_file() else None)
    )
    tmux_path = cfg.local.bin_overrides.get("tmux") or shutil.which("tmux")
    print(f"herdr: {herdr_path or 'NOT FOUND'}")
    print(f"tmux: {tmux_path or 'NOT FOUND'}")
    if not herdr_path:
        print("\n" + install_herdr_guide())
    secrets = load_secrets_env(cfg.hq_dir)
    print(f"\nsecrets.env keys: {sorted(secrets.keys()) if secrets else '(none)'}")
    return 0


def cmd_task_spawn(args: argparse.Namespace) -> int:
    repo = getattr(args, "repo_opt", None) or getattr(args, "repo_pos", None)
    slug = getattr(args, "slug_opt", None) or getattr(args, "slug_pos", None)
    prompt = (
        args.prompt_opt
        if getattr(args, "prompt_opt", None) is not None
        else getattr(args, "prompt_pos", None)
    )
    if not repo or not slug or prompt is None:
        print(
            "Usage: herdr-bot task spawn <repo> <slug> <prompt> [--hq-dir DIR]",
            file=sys.stderr,
        )
        return 2

    daemon = HQDaemon(args.hq_dir)
    try:
        sess = daemon.spawn_task(repo, slug, prompt)
        print(
            f"Spawned {sess['session_key']} | pane={sess['pane_name']} "
            f"| branch={sess['branch_name']} | worktree={sess['worktree_path']}"
        )
        return 0
    finally:
        daemon.close()


def cmd_seal(args: argparse.Namespace) -> int:
    daemon = HQDaemon(args.hq_dir)
    try:
        try:
            sess = daemon.seal_session(args.session_key, reason=args.reason)
        except KeyError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Sealed {sess['session_key']}")
        return 0
    finally:
        daemon.close()


_DURATION_UNIT_SEC = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_TOKEN_RE = re.compile(r"(\d+)\s*([smhdSMHD])")


def parse_duration_seconds(spec: str) -> int:
    """Parse human duration strings such as '30s', '15m', '2h', '1d', or '1h30m'."""
    raw = (spec or "").strip()
    if not raw:
        raise ValueError("Duration must not be empty")
    pos = 0
    total = 0
    for match in _DURATION_TOKEN_RE.finditer(raw):
        if match.start() != pos:
            raise ValueError(f"Invalid duration format: {spec!r}")
        total += int(match.group(1)) * _DURATION_UNIT_SEC[match.group(2).lower()]
        pos = match.end()
    if pos != len(raw) or total <= 0:
        raise ValueError(f"Invalid duration format: {spec!r}")
    return total


def parse_fire_at(spec: str, now: Optional[int] = None) -> int:
    """Parse --at time spec: unix_ts, 'YYYY-MM-DD HH:MM[:SS]', or 'HH:MM[:SS]'."""
    raw = (spec or "").strip()
    if not raw:
        raise ValueError("Time specification must not be empty")
    ts_now = int(now if now is not None else time.time())
    if raw.isdigit():
        return int(raw)
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ):
        try:
            return int(datetime.strptime(raw, fmt).timestamp())
        except ValueError:
            continue
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed_t = datetime.strptime(raw, fmt)
            base = datetime.fromtimestamp(ts_now)
            candidate = base.replace(
                hour=parsed_t.hour,
                minute=parsed_t.minute,
                second=parsed_t.second,
                microsecond=0,
            )
            if int(candidate.timestamp()) <= ts_now:
                candidate += timedelta(days=1)
            return int(candidate.timestamp())
        except ValueError:
            continue
    raise ValueError(f"Unsupported --at time format: {spec!r}")


def cmd_timer_set(args: argparse.Namespace) -> int:
    in_spec = getattr(args, "in_spec", None)
    at_spec = getattr(args, "at_spec", None)
    every_spec = getattr(args, "every", None)

    if in_spec and at_spec:
        print("Cannot specify both --in and --at.", file=sys.stderr)
        return 2

    now_ts = int(time.time())
    try:
        every_sec = parse_duration_seconds(every_spec) if every_spec else 0
        kind = "recurring" if every_spec else "once"
        if in_spec:
            fire_at = now_ts + parse_duration_seconds(in_spec)
        elif at_spec:
            fire_at = parse_fire_at(at_spec, now=now_ts)
        elif every_spec:
            fire_at = now_ts + every_sec
        else:
            print("Must specify --in, --at, or --every.", file=sys.stderr)
            return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    session_key = (
        args.session
        if args.session is not None
        else (os.environ.get("HQ_SESSION_KEY") or None)
    )
    cfg = load_config(args.hq_dir)
    db = HQDatabase(cfg.hq_dir / ".hq" / "runtime.sqlite")
    try:
        timer = db.create_timer(
            reason=args.reason,
            fire_at=fire_at,
            kind=kind,
            every_sec=every_sec,
            session_key=session_key,
            name=args.name,
            now=now_ts,
        )
    finally:
        db.close()

    next_iso = datetime.fromtimestamp(int(timer["next_fire_at"])).strftime("%Y-%m-%d %H:%M:%S")
    every_note = f" | every={timer['every_sec']}s" if timer["kind"] == "recurring" else ""
    name_note = f" | name={timer['name']}" if timer.get("name") else ""
    print(
        f"Scheduled timer #{timer['id']} | kind={timer['kind']} "
        f"| next_fire_at={next_iso} ({timer['next_fire_at']}){every_note} "
        f"| session={timer.get('session_key') or 'general'}{name_note} "
        f"| reason={timer['reason']}"
    )
    return 0


def cmd_timer_list(args: argparse.Namespace) -> int:
    cfg = load_config(args.hq_dir)
    db = HQDatabase(cfg.hq_dir / ".hq" / "runtime.sqlite")
    try:
        timers = db.list_timers(statuses=None if args.all else ["active"])
    finally:
        db.close()

    if not timers:
        print("No timers found.")
        return 0

    print(f"Timers ({len(timers)}):")
    for t in timers:
        next_iso = datetime.fromtimestamp(int(t["next_fire_at"])).strftime("%Y-%m-%d %H:%M:%S")
        every_note = f" every={t['every_sec']}s" if t["kind"] == "recurring" else ""
        name_note = f" name={t['name']}" if t.get("name") else ""
        print(
            f"  - #{t['id']} | status={t['status']} | kind={t['kind']}{every_note} "
            f"| next={next_iso} ({t['next_fire_at']}) | fired={t['fired_count']} "
            f"| session={t.get('session_key') or 'general'}{name_note} "
            f"| reason={t['reason']}"
        )
    return 0


def cmd_timer_cancel(args: argparse.Namespace) -> int:
    cfg = load_config(args.hq_dir)
    db = HQDatabase(cfg.hq_dir / ".hq" / "runtime.sqlite")
    try:
        ok = db.cancel_timer(int(args.timer_id))
    finally:
        db.close()
    if not ok:
        print(f"Timer #{args.timer_id} not found or not active.", file=sys.stderr)
        return 1
    print(f"Cancelled timer #{args.timer_id}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config(args.hq_dir)
    if args.config_action == "list":
        defaults = TuningConfig()
        print(f"{'KEY':<28} {'CURRENT':<12} {'DEFAULT':<12} DESCRIPTION")
        print("-" * 88)
        for f in fields(TuningConfig):
            cur_val = getattr(cfg.tuning, f.name)
            def_val = getattr(defaults, f.name)
            desc = TUNING_DESCRIPTIONS.get(f.name, "")
            print(f"{f.name:<28} {str(cur_val):<12} {str(def_val):<12} {desc}")
        return 0

    if args.config_action == "set":
        old_val, new_val = update_tuning_value(args.hq_dir, args.key, args.value)
        print(f"Updated {args.key}: {old_val!r} -> {new_val!r}")
        return 0
    return 1


def cmd_up(args: argparse.Namespace) -> int:
    daemon = HQDaemon(args.hq_dir)
    daemon.run_forever(poll_interval=args.interval)
    return 0


def cmd_run_once(args: argparse.Namespace) -> int:
    daemon = HQDaemon(args.hq_dir)
    try:
        summary = daemon.run_once()
        print(
            f"events_processed={summary.events_processed} "
            f"events_failed={summary.events_failed} "
            f"commits={summary.commits_enqueued} "
            f"distill={summary.distill_enqueued} "
            f"nightly={summary.nightly_ran} "
            f"digest={summary.digest_sent}"
        )
    finally:
        daemon.close()
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    pid_file = Path(args.hq_dir).expanduser().resolve() / ".hq" / "daemon.pid"
    if not pid_file.exists():
        print("Daemon is not running (no daemon.pid).")
        return 0
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    os.kill(pid, signal.SIGTERM)
    print(f"Sent SIGTERM to daemon pid {pid}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.hq_dir)
    db = HQDatabase(cfg.hq_dir / ".hq" / "runtime.sqlite")
    try:
        sessions = db.list_sessions()
        print(f"Sessions ({len(sessions)}):")
        for s in sessions:
            print(
                f"  - {s['session_key']} | track={s['track']} | status={s['status']} "
                f"| pinned={s['pinned']} | branch={s.get('branch_name') or '-'} "
                f"| agent_id={s.get('agent_session_id') or '-'}"
            )
    finally:
        db.close()
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    cfg = load_config(args.hq_dir)
    db = HQDatabase(cfg.hq_dir / ".hq" / "runtime.sqlite")
    adapter = get_agent_adapter(cfg.agent.backend)
    try:
        sess = db.find_session(args.id_or_slug)
        if not sess:
            print(f"Session not found: {args.id_or_slug}", file=sys.stderr)
            return 1

        skey = str(sess["session_key"])
        wt_str = sess.get("worktree_path")
        repo_name = sess.get("repo_name")
        branch_name = sess.get("branch_name")
        if wt_str and repo_name and branch_name:
            wt_path = Path(str(wt_str))
            if not wt_path.exists():
                repo_path = cfg.get_repo_path(str(repo_name))
                base_branch = cfg.repos[str(repo_name)].default_branch if str(repo_name) in cfg.repos else "main"
                git_worktree_add(repo_path, wt_path, str(branch_name), base_branch=base_branch)
                print(f"Rebuilt worktree at {wt_path} on branch {branch_name}")

        db.pin_session(skey, reason="resumed")
        cmd = adapter.build_interactive_cmd(
            session_id=sess.get("agent_session_id"),
            model=cfg.agent.model,
            effort=cfg.agent.default_effort,
        )
        print("Resume command:", " ".join(cmd))
        if not args.dry_run:
            cwd = Path(str(wt_str)) if wt_str and Path(str(wt_str)).exists() else cfg.hq_dir
            os.chdir(cwd)
            os.execvp(cmd[0], cmd)
        return 0
    finally:
        db.close()


def cmd_pin(args: argparse.Namespace) -> int:
    cfg = load_config(args.hq_dir)
    db = HQDatabase(cfg.hq_dir / ".hq" / "runtime.sqlite")
    try:
        sess = db.find_session(args.id_or_slug)
        if not sess:
            print(f"Session not found: {args.id_or_slug}", file=sys.stderr)
            return 1
        db.pin_session(str(sess["session_key"]), reason="cli_pin")
        print(f"Pinned {sess['session_key']}")
        return 0
    finally:
        db.close()


def cmd_unpin(args: argparse.Namespace) -> int:
    cfg = load_config(args.hq_dir)
    db = HQDatabase(cfg.hq_dir / ".hq" / "runtime.sqlite")
    try:
        sess = db.find_session(args.id_or_slug)
        if not sess:
            print(f"Session not found: {args.id_or_slug}", file=sys.stderr)
            return 1
        db.unpin_session(str(sess["session_key"]), target_status="sealed")
        print(f"Unpinned {sess['session_key']} -> sealed")
        return 0
    finally:
        db.close()


def cmd_archive(args: argparse.Namespace) -> int:
    cfg = load_config(args.hq_dir)
    db = HQDatabase(cfg.hq_dir / ".hq" / "runtime.sqlite")
    term = get_terminal_driver(cfg.terminal.backend)
    try:
        if args.id_or_slug:
            sess = db.find_session(args.id_or_slug)
            if not sess:
                print(f"Session not found: {args.id_or_slug}", file=sys.stderr)
                return 1
            db.unpin_session(str(sess["session_key"]), target_status="sealed")
        report = archive_eligible_sessions(cfg, db, terminal_driver=term)
        print(f"Archived: {report.archived}; Blocked dirty: {report.blocked_dirty}; Errors: {report.errors}")
        return 0 if not report.errors else 1
    finally:
        db.close()


def cmd_sync(args: argparse.Namespace) -> int:
    res = sync_hq_repo(args.hq_dir, push=not args.no_push)
    print(res.message)
    return 0


def cmd_guard(args: argparse.Namespace) -> int:
    res = run_memory_guard(args.hq_dir)
    if res.ok:
        print("memory_guard: PASS")
        return 0
    for v in res.violations:
        print(f"[FAIL] {v}", file=sys.stderr)
    return 1


def cmd_oss_check(args: argparse.Namespace) -> int:
    res = run_oss_guard(args.repo, check_git_history=not args.no_git_history)
    if res.ok:
        print("oss_guard: PASS (0 violations)")
        return 0
    for v in res.violations:
        print(f"[VIOLATION] {v}", file=sys.stderr)
    return 1


def cmd_chat(args: argparse.Namespace) -> int:
    return run_chat_loop(args.hq_dir, standalone=bool(args.standalone))


def cmd_telegram_detect_chat(args: argparse.Namespace) -> int:
    cfg = load_config(args.hq_dir)
    secrets = load_secrets_env(cfg.hq_dir)
    token = secrets.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print(
            "TELEGRAM_BOT_TOKEN is not set in .hq/secrets.env",
            file=sys.stderr,
        )
        return 1
    api_base = secrets.get("TELEGRAM_API_BASE", "").strip() or "https://api.telegram.org"
    chats = detect_telegram_chats(token, api_base=api_base)
    if not chats:
        print("No recent Telegram updates found. Send a message in your target group/chat and retry.")
        return 0
    for c in chats:
        print(
            f"chat_id={c['chat_id']} | type={c['type']} "
            f"| is_forum={c['is_forum']} | title={c['title']}"
        )
    return 0


def cmd_feishu_detect_chat(args: argparse.Namespace) -> int:
    cfg = load_config(args.hq_dir)
    secrets = load_secrets_env(cfg.hq_dir)
    app_id = secrets.get("FEISHU_APP_ID", "").strip()
    app_secret = secrets.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        print(
            "FEISHU_APP_ID and FEISHU_APP_SECRET must be set in .hq/secrets.env",
            file=sys.stderr,
        )
        return 1
    api_base = secrets.get("FEISHU_API_BASE", "").strip() or "https://open.feishu.cn"
    res = detect_feishu_chats(
        app_id,
        app_secret,
        api_base=api_base,
        listen_seconds=max(0, int(args.timeout)),
    )
    print("Feishu tenant_access_token: OK")
    group_chats = res.get("group_chats") or []
    seen_chats = res.get("seen_chats") or []
    if group_chats:
        print(f"Joined group chats ({len(group_chats)}):")
        for c in group_chats:
            print(
                f"  chat_id={c['chat_id']} | mode={c.get('chat_mode', 'group')} "
                f"| name={c.get('name', '(unnamed)')}"
            )
    else:
        print("Joined group chats: (none)")
    if seen_chats:
        print(f"Detected inbound chats ({len(seen_chats)}):")
        for c in seen_chats:
            print(
                f"  chat_id={c['chat_id']} | type={c.get('chat_type', 'unknown')} "
                f"| sender_id={c.get('sender_id', '')}"
            )
    return 0


def build_parser(prog: Optional[str] = None) -> argparse.ArgumentParser:
    invoked = Path(sys.argv[0]).name if sys.argv else ""
    prog_name = prog or (invoked if invoked in {"herdr-bot", "hq"} else "herdr-bot")
    p = argparse.ArgumentParser(
        prog=prog_name,
        description="herdr-bot (alias: hq) 24/7 multi-repo orchestrator CLI for native coding agents",
    )
    sub = p.add_subparsers(dest="command", required=True)

    register_setup_subparser(sub)

    sp_init = sub.add_parser("init", help="Initialize a new HQ memory repository")
    sp_init.add_argument("dir", nargs="?", default=".")
    sp_init.set_defaults(func=cmd_init)

    sp_boot = sub.add_parser("bootstrap", help="Clone missing configured repos")
    sp_boot.add_argument("--hq-dir", default=".")
    sp_boot.set_defaults(func=cmd_bootstrap)

    sp_doc = sub.add_parser("doctor", help="Check host dependencies and tools")
    sp_doc.add_argument("--hq-dir", default=".")
    sp_doc.set_defaults(func=cmd_doctor)

    sp_cfg = sub.add_parser("config", help="View or update tuning configuration")
    cfg_sub = sp_cfg.add_subparsers(dest="config_action", required=True)
    sp_cfg_list = cfg_sub.add_parser("list", help="List all tuning parameters")
    sp_cfg_list.add_argument("--hq-dir", default=".")
    sp_cfg_list.set_defaults(func=cmd_config)
    sp_cfg_set = cfg_sub.add_parser("set", help="Set a tuning parameter in hq.yaml")
    sp_cfg_set.add_argument("key")
    sp_cfg_set.add_argument("value")
    sp_cfg_set.add_argument("--hq-dir", default=".")
    sp_cfg_set.set_defaults(func=cmd_config)

    sp_up = sub.add_parser("up", help="Start hq-daemon event loop")
    sp_up.add_argument("--hq-dir", default=".")
    sp_up.add_argument("--interval", type=float, default=2.0)
    sp_up.set_defaults(func=cmd_up)

    sp_ro = sub.add_parser("run-once", help="Run a single daemon tick")
    sp_ro.add_argument("--hq-dir", default=".")
    sp_ro.set_defaults(func=cmd_run_once)

    sp_stop = sub.add_parser("stop", help="Stop running daemon")
    sp_stop.add_argument("--hq-dir", default=".")
    sp_stop.set_defaults(func=cmd_stop)

    sp_st = sub.add_parser("status", help="Show tracked sessions")
    sp_st.add_argument("--hq-dir", default=".")
    sp_st.set_defaults(func=cmd_status)

    sp_res = sub.add_parser("resume", help="Resume a session (rebuilding worktree if archived)")
    sp_res.add_argument("id_or_slug")
    sp_res.add_argument("--hq-dir", default=".")
    sp_res.add_argument("--dry-run", action="store_true")
    sp_res.set_defaults(func=cmd_resume)

    sp_pin = sub.add_parser("pin", help="Pin a session to exempt it from nightly archive")
    sp_pin.add_argument("id_or_slug")
    sp_pin.add_argument("--hq-dir", default=".")
    sp_pin.set_defaults(func=cmd_pin)

    sp_unpin = sub.add_parser("unpin", help="Unpin a session")
    sp_unpin.add_argument("id_or_slug")
    sp_unpin.add_argument("--hq-dir", default=".")
    sp_unpin.set_defaults(func=cmd_unpin)

    sp_seal = sub.add_parser(
        "seal",
        help="Seal a session (preserving worktree until nightly safe archive)",
    )
    sp_seal.add_argument("session_key", help="Session key, agent session ID, or topic slug")
    sp_seal.add_argument("--reason", default="", help="Optional reason recorded in seal notice")
    sp_seal.add_argument("--hq-dir", default=".")
    sp_seal.set_defaults(func=cmd_seal)

    sp_arch = sub.add_parser("archive", help="Archive eligible sealed sessions")
    sp_arch.add_argument("id_or_slug", nargs="?")
    sp_arch.add_argument("--hq-dir", default=".")
    sp_arch.set_defaults(func=cmd_archive)

    sp_sync = sub.add_parser("sync", help="Git commit and push HQ memory repository")
    sp_sync.add_argument("--hq-dir", default=".")
    sp_sync.add_argument("--no-push", action="store_true")
    sp_sync.set_defaults(func=cmd_sync)

    sp_guard = sub.add_parser("guard", help="Run memory guard checks")
    sp_guard.add_argument("--hq-dir", default=".")
    sp_guard.set_defaults(func=cmd_guard)

    sp_oss = sub.add_parser("oss-check", help="Scan engine repo for open-source purity violations")
    sp_oss.add_argument("--repo", default=".")
    sp_oss.add_argument("--no-git-history", action="store_true")
    sp_oss.set_defaults(func=cmd_oss_check)

    sp_task = sub.add_parser("task", help="Manage #Topic interactive worktree tasks")
    task_sub = sp_task.add_subparsers(dest="task_action", required=True)
    sp_task_spawn = task_sub.add_parser("spawn", help="Spawn a #Topic worktree + Herdr pane + thread")
    sp_task_spawn.add_argument("repo_pos", nargs="?", metavar="repo", help="Target repository name")
    sp_task_spawn.add_argument("slug_pos", nargs="?", metavar="slug", help="Short task slug")
    sp_task_spawn.add_argument("prompt_pos", nargs="?", metavar="prompt", help="Initial task prompt")
    sp_task_spawn.add_argument("--repo", dest="repo_opt", default=None, help="Target repository name")
    sp_task_spawn.add_argument("--slug", dest="slug_opt", default=None, help="Short task slug")
    sp_task_spawn.add_argument("--prompt", dest="prompt_opt", default=None, help="Initial task prompt")
    sp_task_spawn.add_argument("--hq-dir", default=".")
    sp_task_spawn.set_defaults(func=cmd_task_spawn)

    sp_timer = sub.add_parser("timer", help="Manage self-scheduled agent follow-up timers")
    timer_sub = sp_timer.add_subparsers(dest="timer_action", required=True)

    sp_timer_set = timer_sub.add_parser("set", help="Schedule a one-shot or recurring wake timer")
    sp_timer_set.add_argument("--reason", required=True, help="Follow-up reason injected on wake")
    sp_timer_set.add_argument("--in", dest="in_spec", default=None, help="Relative delay (e.g. 30s, 15m, 2h, 1d)")
    sp_timer_set.add_argument("--at", dest="at_spec", default=None, help="Target time (HH:MM, YYYY-MM-DD HH:MM, or unix_ts)")
    sp_timer_set.add_argument("--every", default=None, help="Recurring interval (e.g. 15m, 1h, 1d)")
    sp_timer_set.add_argument("--session", default=None, help="Target session_key (defaults to $HQ_SESSION_KEY)")
    sp_timer_set.add_argument("--name", default=None, help="Optional human-readable label")
    sp_timer_set.add_argument("--hq-dir", default=".")
    sp_timer_set.set_defaults(func=cmd_timer_set)

    sp_timer_list = timer_sub.add_parser("list", help="List scheduled timers")
    sp_timer_list.add_argument("--all", action="store_true", help="Include fired and cancelled timers")
    sp_timer_list.add_argument("--hq-dir", default=".")
    sp_timer_list.set_defaults(func=cmd_timer_list)

    sp_timer_cancel = timer_sub.add_parser("cancel", help="Cancel an active timer")
    sp_timer_cancel.add_argument("timer_id", type=int, help="Numeric timer ID")
    sp_timer_cancel.add_argument("--hq-dir", default=".")
    sp_timer_cancel.set_defaults(func=cmd_timer_cancel)

    sp_chat = sub.add_parser("chat", help="Open interactive local terminal chat")
    sp_chat.add_argument("--hq-dir", default=".")
    sp_chat.add_argument(
        "--standalone",
        action="store_true",
        help="Run embedded hq-daemon loop in background thread",
    )
    sp_chat.set_defaults(func=cmd_chat)

    sp_tg = sub.add_parser("telegram", help="Telegram helper utilities")
    tg_sub = sp_tg.add_subparsers(dest="telegram_action", required=True)
    sp_tg_detect = tg_sub.add_parser(
        "detect-chat",
        help="Detect Telegram chat IDs from recent bot updates",
    )
    sp_tg_detect.add_argument("--hq-dir", default=".")
    sp_tg_detect.set_defaults(func=cmd_telegram_detect_chat)

    sp_fs = sub.add_parser("feishu", help="Feishu/Lark helper utilities")
    fs_sub = sp_fs.add_subparsers(dest="feishu_action", required=True)
    sp_fs_detect = fs_sub.add_parser(
        "detect-chat",
        help="Validate Feishu credentials, list joined chats, and optionally capture DM chat_id",
    )
    sp_fs_detect.add_argument("--hq-dir", default=".")
    sp_fs_detect.add_argument("--timeout", type=int, default=30)
    sp_fs_detect.set_defaults(func=cmd_feishu_detect_chat)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
