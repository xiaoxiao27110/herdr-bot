"""Configuration loader, hot-reloading, tuning parameters, and logical path resolver."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import os
import yaml


TUNING_DESCRIPTIONS: Dict[str, str] = {
    "index_max_lines": "Max lines allowed in index file (e.g. AGENTS.md)",
    "index_max_bytes": "Max bytes allowed in index file (25 KB default)",
    "today_wal_max_lines": "Max lines allowed in notes/today.md WAL hot buffer",
    "dream_cron": "Nightly 3-stage closeout, dreaming & archiving time (HH:MM)",
    "dream_min_interval_hours": "Auto-Dream minimum interval gate in hours",
    "dream_min_sessions": "Auto-Dream minimum sessions gate",
    "dream_force_nightly": "If true, always run Dreaming at dream_cron regardless of gates",
    "digest_cron": "Daily L0 Digest morning report time (HH:MM)",
    "inbox_lease_ttl_sec": "Event inbox lease TTL in seconds (15 min default)",
    "inbox_max_attempts": "Max event delivery attempts before moving to dead letter",
    "commit_debounce_sec": "Git sensitive commit debounce window in seconds",
    "active_repo_quiet_minutes": "Quiet window in minutes when repo has recent uncommitted edits",
    "pane_blocked_notify_sec": "Delay in seconds before notifying on blocked managed pane (0 = immediate)",
    "deferred_approval_ttl_hours": "TTL in hours for deferred approval cards",
    "general_idle_seal_minutes": "Idle timeout in minutes before rotating #General session with handoff summary",
    "general_max_turns": "Max turns before marking #General for quiet daytime /new rotation",
    "general_rotate_transcript_mb": "Transcript size threshold in MB before marking #General for daytime /new rotation",
    "general_rotate_quiet_minutes": "Continuous quiet minutes required before executing daytime size/turns #General /new rotation",
    "general_nightly_reset": "If true, mark #General for in-place /new rotation after nightly closeout",
    "general_rotation_quiet_minutes": "Continuous quiet minutes required before executing due nightly #General /new rotation",
    "transcript_busy_window_sec": "Seconds since post-Stop transcript write within which a session is treated as busy",
    "distill_idle_minutes": "Idle threshold in minutes for incremental session distillation",
    "distill_min_new_turns": "Minimum new turns required for incremental distillation",
    "dirty_worktree_warn_hours": "Hours before dirty worktree triggers morning digest warning",
    "git_sync_debounce_minutes": "Debounce window in minutes for HQ repo auto git sync",
    "past_messages_limit": "Max recent outbound messages injected into prompt to avoid repetition",
    "quiet_hours": "Nightly quiet hours window (HH:MM-HH:MM, supports overnight e.g. 23:00-08:00; empty disables)",
    "outbox_max_retries": "Max backoff retry attempts for failed outbox deliveries before moving to dead",
    "outbox_max_per_minute": "Max non-urgent outbox deliveries per minute to prevent flooding / 429",
}


@dataclass
class TuningConfig:
    index_max_lines: int = 200
    index_max_bytes: int = 25600
    today_wal_max_lines: int = 60
    dream_cron: str = "23:30"
    dream_min_interval_hours: int = 24
    dream_min_sessions: int = 5
    dream_force_nightly: bool = True
    digest_cron: str = "09:30"
    inbox_lease_ttl_sec: int = 900
    inbox_max_attempts: int = 3
    commit_debounce_sec: int = 900
    active_repo_quiet_minutes: int = 15
    pane_blocked_notify_sec: int = 0
    deferred_approval_ttl_hours: int = 24
    general_idle_seal_minutes: int = 1440
    general_max_turns: int = 60
    general_rotate_transcript_mb: int = 8
    general_rotate_quiet_minutes: int = 30
    general_nightly_reset: bool = True
    general_rotation_quiet_minutes: int = 60
    transcript_busy_window_sec: int = 120
    distill_idle_minutes: int = 60
    distill_min_new_turns: int = 8
    dirty_worktree_warn_hours: int = 48
    git_sync_debounce_minutes: int = 120
    past_messages_limit: int = 5
    quiet_hours: str = ""
    outbox_max_retries: int = 5
    outbox_max_per_minute: int = 12

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "TuningConfig":
        if not data:
            return cls()
        kwargs: Dict[str, Any] = {}
        default_obj = cls()
        for f in fields(cls):
            if f.name in data and data[f.name] is not None:
                default_val = getattr(default_obj, f.name)
                raw = data[f.name]
                if isinstance(default_val, bool):
                    if isinstance(raw, str):
                        kwargs[f.name] = raw.strip().lower() in {"1", "true", "yes", "on"}
                    else:
                        kwargs[f.name] = bool(raw)
                elif isinstance(default_val, int):
                    kwargs[f.name] = int(raw)
                else:
                    kwargs[f.name] = str(raw)
        return cls(**kwargs)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentConfig:
    backend: str = "agy"
    index_file: str = "AGENTS.md"
    default_effort: str = "high"
    light_effort: str = "low"
    model: Optional[str] = None
    permission_mode: str = "default"


@dataclass
class TerminalConfig:
    backend: str = "auto"


@dataclass
class ChannelConfig:
    backend: str = "telegram"
    allowed_chat_id: Optional[str] = None


@dataclass
class RepoConfig:
    name: str
    git_url: str = ""
    default_branch: str = "main"


@dataclass
class DownstreamDep:
    repo: str
    contract: str = ""
    impact: str = ""


@dataclass
class RepoBoundary:
    name: str
    git_url: str = ""
    default_branch: str = "main"
    watch_paths: List[str] = field(default_factory=list)
    ignore_paths: List[str] = field(default_factory=list)
    downstream: List[DownstreamDep] = field(default_factory=list)


@dataclass
class BoundariesConfig:
    version: int = 1
    repos: Dict[str, RepoBoundary] = field(default_factory=dict)
    raw_text: str = ""


@dataclass
class LocalConfig:
    repo_paths: Dict[str, str] = field(default_factory=dict)
    bin_overrides: Dict[str, str] = field(default_factory=dict)


@dataclass
class HQConfig:
    hq_dir: Path
    version: int = 1
    project_name: str = "my-project"
    agent: AgentConfig = field(default_factory=AgentConfig)
    terminal: TerminalConfig = field(default_factory=TerminalConfig)
    channel: ChannelConfig = field(default_factory=ChannelConfig)
    repos: Dict[str, RepoConfig] = field(default_factory=dict)
    tuning: TuningConfig = field(default_factory=TuningConfig)
    boundaries: BoundariesConfig = field(default_factory=BoundariesConfig)
    local: LocalConfig = field(default_factory=LocalConfig)
    _mtimes: Dict[str, float] = field(default_factory=dict, repr=False)

    def get_repo_path(self, repo_name: str) -> Path:
        if repo_name in self.local.repo_paths:
            return Path(self.local.repo_paths[repo_name]).expanduser().resolve()
        return (self.hq_dir / ".hq" / "repos" / repo_name).resolve()

    def resolve_logical_path(self, logical_path: str) -> Path:
        """Resolve '<repo>:<relpath>' using .hq/local.yaml or default .hq/repos/<repo>."""
        if ":" not in logical_path:
            return (self.hq_dir / logical_path).resolve()
        repo_name, rel = logical_path.split(":", 1)
        rel = rel.lstrip("/")
        return (self.get_repo_path(repo_name) / rel).resolve()

    def reload_if_changed(self) -> bool:
        """Check mtimes of hq.yaml, repo-boundaries.yaml, and .hq/local.yaml; reload if changed."""
        tracked = [
            self.hq_dir / "hq.yaml",
            self.hq_dir / "repo-boundaries.yaml",
            self.hq_dir / ".hq" / "local.yaml",
        ]
        changed = False
        for p in tracked:
            mtime = p.stat().st_mtime if p.exists() else -1.0
            if self._mtimes.get(str(p)) != mtime:
                changed = True
                break
        if not changed:
            return False
        fresh = load_config(self.hq_dir)
        self.version = fresh.version
        self.project_name = fresh.project_name
        self.agent = fresh.agent
        self.terminal = fresh.terminal
        self.channel = fresh.channel
        self.repos = fresh.repos
        self.tuning = fresh.tuning
        self.boundaries = fresh.boundaries
        self.local = fresh.local
        self._mtimes = fresh._mtimes
        return True


def _read_yaml_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def load_secrets_env(hq_dir: Path) -> Dict[str, str]:
    secrets_path = hq_dir / ".hq" / "secrets.env"
    result: Dict[str, str] = {}
    if not secrets_path.exists():
        return result
    for line in secrets_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def load_config(hq_dir: str | Path) -> HQConfig:
    root = Path(hq_dir).expanduser().resolve()
    hq_yaml_path = root / "hq.yaml"
    boundaries_path = root / "repo-boundaries.yaml"
    local_path = root / ".hq" / "local.yaml"

    mtimes: Dict[str, float] = {}
    for p in (hq_yaml_path, boundaries_path, local_path):
        mtimes[str(p)] = p.stat().st_mtime if p.exists() else -1.0

    hq_data = _read_yaml_file(hq_yaml_path)
    agent_raw = hq_data.get("agent") or {}
    term_raw = hq_data.get("terminal") or {}
    chan_raw = hq_data.get("channel") or {}
    repos_raw = hq_data.get("repos") or {}
    tuning_raw = hq_data.get("tuning") or {}

    agent_cfg = AgentConfig(
        backend=str(agent_raw.get("backend", "agy")),
        index_file=str(agent_raw.get("index_file", "AGENTS.md")),
        default_effort=str(agent_raw.get("default_effort", "high")),
        light_effort=str(agent_raw.get("light_effort", "low")),
        model=agent_raw.get("model"),
        permission_mode=str(agent_raw.get("permission_mode", "default") or "default").strip(),
    )
    term_cfg = TerminalConfig(backend=str(term_raw.get("backend", "auto")))
    chan_cfg = ChannelConfig(
        backend=str(chan_raw.get("backend", "telegram")),
        allowed_chat_id=(
            str(chan_raw["allowed_chat_id"])
            if chan_raw.get("allowed_chat_id") is not None
            else None
        ),
    )

    repos: Dict[str, RepoConfig] = {}
    for rname, rdata in repos_raw.items():
        rdata = rdata or {}
        repos[str(rname)] = RepoConfig(
            name=str(rname),
            git_url=str(rdata.get("git_url", "")),
            default_branch=str(rdata.get("default_branch", "main")),
        )

    tuning_cfg = TuningConfig.from_dict(tuning_raw)

    b_text = boundaries_path.read_text(encoding="utf-8") if boundaries_path.exists() else ""
    b_data = yaml.safe_load(b_text) if b_text.strip() else {}
    if not isinstance(b_data, dict):
        b_data = {}
    b_repos: Dict[str, RepoBoundary] = {}
    for rname, binfo in (b_data.get("repos") or {}).items():
        binfo = binfo or {}
        deps = [
            DownstreamDep(
                repo=str(d.get("repo", "")),
                contract=str(d.get("contract", "")),
                impact=str(d.get("impact", "")),
            )
            for d in (binfo.get("downstream") or [])
            if isinstance(d, dict)
        ]
        b_repos[str(rname)] = RepoBoundary(
            name=str(rname),
            git_url=str(binfo.get("git_url", repos.get(str(rname), RepoConfig(str(rname))).git_url)),
            default_branch=str(
                binfo.get(
                    "default_branch",
                    repos.get(str(rname), RepoConfig(str(rname))).default_branch,
                )
            ),
            watch_paths=[str(p) for p in (binfo.get("watch_paths") or [])],
            ignore_paths=[str(p) for p in (binfo.get("ignore_paths") or [])],
            downstream=deps,
        )
    boundaries_cfg = BoundariesConfig(
        version=int(b_data.get("version", 1)),
        repos=b_repos,
        raw_text=b_text,
    )

    local_data = _read_yaml_file(local_path)
    bin_overrides = {
        str(k): str(v) for k, v in (local_data.get("bin_overrides") or {}).items()
    }
    for k, v in local_data.items():
        if isinstance(k, str) and k.endswith("_bin") and v:
            tool_name = k[:-4]
            if tool_name and tool_name not in bin_overrides:
                bin_overrides[tool_name] = str(v)
    local_cfg = LocalConfig(
        repo_paths={str(k): str(v) for k, v in (local_data.get("repo_paths") or {}).items()},
        bin_overrides=bin_overrides,
    )

    return HQConfig(
        hq_dir=root,
        version=int(hq_data.get("version", 1)),
        project_name=str(hq_data.get("project_name", root.name)),
        agent=agent_cfg,
        terminal=term_cfg,
        channel=chan_cfg,
        repos=repos,
        tuning=tuning_cfg,
        boundaries=boundaries_cfg,
        local=local_cfg,
        _mtimes=mtimes,
    )


def parse_tuning_value(key: str, raw_val: str) -> Any:
    """Validate and convert string value for a TuningConfig key."""
    default_obj = TuningConfig()
    field_map = {f.name: f for f in fields(TuningConfig)}
    if key not in field_map:
        raise KeyError(f"Unknown tuning key: {key}")
    default_val = getattr(default_obj, key)
    if isinstance(default_val, bool):
        lowered = raw_val.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        raise ValueError(f"Invalid boolean value for {key}: {raw_val!r}")
    if isinstance(default_val, int):
        try:
            return int(raw_val)
        except ValueError as exc:
            raise ValueError(f"Invalid integer value for {key}: {raw_val!r}") from exc
    return str(raw_val)


def update_tuning_value(hq_dir: str | Path, key: str, raw_val: str) -> Tuple[Any, Any]:
    """Update a tuning parameter in hq.yaml and return (old_val, new_val)."""
    root = Path(hq_dir).expanduser().resolve()
    hq_yaml_path = root / "hq.yaml"
    data = _read_yaml_file(hq_yaml_path)
    cfg = load_config(root)
    new_val = parse_tuning_value(key, raw_val)
    old_val = getattr(cfg.tuning, key)
    tuning_block = data.get("tuning")
    if not isinstance(tuning_block, dict):
        tuning_block = {}
    tuning_block[key] = new_val
    data["tuning"] = tuning_block
    hq_yaml_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return old_val, new_val
