"""Dual Guards: memory_guard (line/byte & logical path checks) and oss_guard (open-source purity scan)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Pattern, Tuple
import re
import subprocess

from hq_pilot.config import HQConfig, load_config


@dataclass
class GuardResult:
    ok: bool
    violations: List[str] = field(default_factory=list)


def run_memory_guard(
    hq_dir: str | Path,
    config: Optional[HQConfig] = None,
    *,
    check_today_wal_lines: bool = True,
) -> GuardResult:
    root = Path(hq_dir).expanduser().resolve()
    cfg = config or load_config(root)
    violations: List[str] = []

    index_path = root / cfg.agent.index_file
    if index_path.exists():
        raw_bytes = index_path.read_bytes()
        lines = raw_bytes.decode("utf-8", errors="replace").splitlines()
        if len(lines) > cfg.tuning.index_max_lines:
            violations.append(
                f"{cfg.agent.index_file} exceeds max lines: {len(lines)} > {cfg.tuning.index_max_lines}"
            )
        if len(raw_bytes) > cfg.tuning.index_max_bytes:
            violations.append(
                f"{cfg.agent.index_file} exceeds max bytes: {len(raw_bytes)} > {cfg.tuning.index_max_bytes}"
            )

    today_path = root / "notes" / "today.md"
    if check_today_wal_lines and today_path.exists():
        t_lines = today_path.read_text(encoding="utf-8").splitlines()
        if len(t_lines) > cfg.tuning.today_wal_max_lines:
            violations.append(
                f"notes/today.md exceeds max lines: {len(t_lines)} > {cfg.tuning.today_wal_max_lines}"
            )

    known_repos = set(cfg.repos.keys()) | set(cfg.boundaries.repos.keys())
    if known_repos:
        repo_alt = "|".join(re.escape(r) for r in sorted(known_repos))
        ref_re = re.compile(rf"(?<![A-Za-z0-9_.\-/:])`?((?:{repo_alt}):[A-Za-z0-9_.\-/]+)`?(?!:)")
        md_files: List[Path] = []
        if index_path.exists():
            md_files.append(index_path)
        for sub in ("memory", "tasks", "notes"):
            subdir = root / sub
            if subdir.exists():
                md_files.extend(sorted(subdir.rglob("*.md")))

        for md in md_files:
            text = md.read_text(encoding="utf-8", errors="replace")
            for idx, line in enumerate(text.splitlines(), start=1):
                for m in ref_re.finditer(line):
                    ref = m.group(1)
                    resolved = cfg.resolve_logical_path(ref)
                    if not resolved.exists():
                        rel_md = md.relative_to(root).as_posix()
                        violations.append(
                            f"{rel_md}:{idx} broken logical path '{ref}' -> {resolved}"
                        )

    return GuardResult(ok=(len(violations) == 0), violations=violations)


# Build forbidden patterns via concatenation so this source file itself remains clean.
def _build_oss_rules() -> List[Tuple[str, Pattern[str]]]:
    rules: List[Tuple[str, Pattern[str]]] = [
        (
            "private_ipv4_10",
            re.compile(r"(?<![0-9.])10\.(?:\d{1,3}\.){2}\d{1,3}(?![0-9.])"),
        ),
        (
            "private_ipv4_172",
            re.compile(r"(?<![0-9.])172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}(?![0-9.])"),
        ),
        (
            "private_ipv4_192_168",
            re.compile(r"(?<![0-9.])192\.168\.\d{1,3}\.\d{1,3}(?![0-9.])"),
        ),
        (
            "internal_domain_xcube",
            re.compile(r"x" + r"cube\.com", re.IGNORECASE),
        ),
        (
            "internal_domain_aidz",
            re.compile(r"ai-" + r"dz\.top", re.IGNORECASE),
        ),
        (
            "internal_domain_aijjw",
            re.compile(r"ai" + r"jjw\.top", re.IGNORECASE),
        ),
        (
            "forbidden_brand_term",
            re.compile(r"one" + r"deck", re.IGNORECASE),
        ),
        (
            "personal_path_linux",
            re.compile(r"/data/" + r"home/"),
        ),
        (
            "personal_path_macos",
            re.compile(r"/User" + r"s/"),
        ),
        (
            "api_key_prefix",
            re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
        ),
        (
            "telegram_bot_token",
            re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),
        ),
    ]
    return rules


def _iter_scannable_files(repo_root: Path) -> List[Path]:
    if (repo_root / ".git").exists():
        res = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=str(repo_root),
            capture_output=True,
            check=False,
        )
        if res.returncode == 0:
            files: List[Path] = []
            for raw in res.stdout.split(b"\x00"):
                if not raw:
                    continue
                p = repo_root / raw.decode("utf-8", errors="replace")
                if p.is_file():
                    files.append(p)
            return files

    ignored_dirs = {".git", "__pycache__", ".hq", ".pytest_cache", "dist", "build"}
    collected: List[Path] = []
    for p in sorted(repo_root.rglob("*")):
        if any(part in ignored_dirs for part in p.parts):
            continue
        if p.is_file():
            collected.append(p)
    return collected


def run_oss_guard(
    engine_dir: str | Path, check_git_history: bool = True
) -> GuardResult:
    root = Path(engine_dir).expanduser().resolve()
    rules = _build_oss_rules()
    violations: List[str] = []

    for fpath in _iter_scannable_files(root):
        try:
            raw = fpath.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        rel = fpath.relative_to(root).as_posix()
        for line_no, line in enumerate(text.splitlines(), start=1):
            for rule_name, pat in rules:
                if pat.search(line):
                    violations.append(f"{rel}:{line_no} matched [{rule_name}]")

    if check_git_history and (root / ".git").exists():
        log_res = subprocess.run(
            ["git", "log", "--all", "--format=%H %s", "-n", "100"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
        if log_res.returncode == 0:
            for line in log_res.stdout.splitlines():
                for rule_name, pat in rules:
                    if pat.search(line):
                        violations.append(f"git-log matched [{rule_name}]: {line[:80]}")

    return GuardResult(ok=(len(violations) == 0), violations=violations)
