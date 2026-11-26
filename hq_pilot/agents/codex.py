"""CodexAdapter: CodingAgentAdapter implementation for OpenAI Codex CLI (`codex`)."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from hq_pilot.agents.base import (
    CodingAgentAdapter,
    HeadlessResult,
    HookEvent,
    HookEventKind,
    ProjectTrustResult,
    Turn,
)


class CodexConfigError(RuntimeError):
    """Raised when `$CODEX_HOME/config.toml` cannot be safely parsed or validated."""


def _toml_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\b", "\\b")
        .replace("\f", "\\f")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _parse_toml(text: str) -> Dict[str, Any]:
    """Parse TOML using stdlib `tomllib` (3.11+) or `tomli`/`toml` fallback."""
    parser_loads: Optional[Callable[[str], Dict[str, Any]]] = None
    for mod_name in ("tomllib", "tomli", "toml"):
        try:
            mod = __import__(mod_name)
            loads_fn = getattr(mod, "loads", None)
            if callable(loads_fn):
                parser_loads = loads_fn
                break
        except ImportError:
            continue
    if parser_loads is None:
        raise CodexConfigError(
            "No TOML parser available (need Python 3.11+ tomllib or tomli/toml)."
        )
    try:
        parsed = parser_loads(text)
    except Exception as exc:
        raise CodexConfigError(f"Invalid TOML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CodexConfigError("Parsed TOML root is not a table.")
    return parsed


def resolve_codex_home(codex_home: Optional[str | Path] = None) -> Path:
    """Resolve active Codex home directory (`$CODEX_HOME` or `~/.codex`)."""
    if codex_home is not None and str(codex_home).strip():
        return Path(codex_home).expanduser()
    env_home = os.environ.get("CODEX_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".codex"


def _normalize_project_dirs(project_dirs: Sequence[str | Path]) -> List[str]:
    unique: List[str] = []
    for d in project_dirs:
        if not d:
            continue
        resolved = str(Path(d).expanduser().resolve())
        if resolved not in unique:
            unique.append(resolved)
    return unique


def format_codex_trust_snippet(project_dirs: Sequence[str | Path]) -> str:
    """Format `[projects."<abs>"]` TOML blocks for manual addition to `config.toml`."""
    blocks: List[str] = []
    for resolved in _normalize_project_dirs(project_dirs):
        blocks.append(f"[projects.{_toml_quote(resolved)}]\ntrust_level = \"trusted\"")
    return "\n\n".join(blocks)


def _split_toml_comment(line: str) -> Tuple[str, str, str]:
    """Split a TOML line into `(code, ws_before_comment, comment)` respecting strings."""
    in_basic = False
    in_literal = False
    escaped = False
    for idx, ch in enumerate(line):
        if in_basic:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_basic = False
        elif in_literal:
            if ch == "'":
                in_literal = False
        else:
            if ch == '"':
                in_basic = True
            elif ch == "'":
                in_literal = True
            elif ch == "#":
                before = line[:idx]
                code = before.rstrip(" \t")
                ws = before[len(code):]
                return code, ws, line[idx:]
    return line.rstrip(" \t"), "", ""


def _is_table_header_line(line: str) -> bool:
    code, _, _ = _split_toml_comment(line)
    stripped = code.strip()
    return stripped.startswith("[") and stripped.endswith("]")


def _parse_project_table_header(line: str) -> Optional[str]:
    """If `line` is a `[projects.<key>]` table header, return the unescaped `<key>`."""
    code, _, _ = _split_toml_comment(line)
    stripped = code.strip()
    if not stripped.startswith("[") or stripped.startswith("[[") or not stripped.endswith("]"):
        return None
    try:
        probe = _parse_toml(f"{stripped}\n__herdr_probe__ = 1\n")
    except CodexConfigError:
        return None
    if list(probe.keys()) != ["projects"]:
        return None
    projects = probe.get("projects")
    if not isinstance(projects, dict) or len(projects) != 1:
        return None
    key, sub = next(iter(projects.items()))
    if isinstance(sub, dict) and sub == {"__herdr_probe__": 1}:
        return str(key)
    return None


def _is_projects_root_header(line: str) -> bool:
    code, _, _ = _split_toml_comment(line)
    stripped = code.strip()
    if not stripped.startswith("[") or stripped.startswith("[[") or not stripped.endswith("]"):
        return False
    try:
        probe = _parse_toml(f"{stripped}\n__herdr_probe__ = 1\n")
    except CodexConfigError:
        return False
    return probe == {"projects": {"__herdr_probe__": 1}}


def _is_trust_level_assignment(line: str) -> bool:
    code, _, _ = _split_toml_comment(line)
    if "=" not in code:
        return False
    key = code.split("=", 1)[0].strip()
    return key in {"trust_level", '"trust_level"', "'trust_level'"}


def _is_project_trusted(parsed: Dict[str, Any], abs_path: str) -> bool:
    projects = parsed.get("projects")
    if not isinstance(projects, dict):
        return False
    entry = projects.get(abs_path)
    return isinstance(entry, dict) and entry.get("trust_level") == "trusted"


def _try_update_inline_or_dotted_trust(lines: List[str], target: str) -> bool:
    """Best-effort text-level update for dotted-key or `[projects]` inline entries."""
    in_projects_table = False
    for idx, line in enumerate(lines):
        if _is_table_header_line(line):
            in_projects_table = _is_projects_root_header(line)
            continue
        code, ws_before, comment = _split_toml_comment(line)
        stripped = code.strip()
        if not stripped or "=" not in stripped:
            continue
        indent = line[: len(line) - len(line.lstrip(" \t"))]

        if in_projects_table:
            probe_src = f"[projects]\n{stripped}\n"
        else:
            probe_src = f"{stripped}\n"
        try:
            probe = _parse_toml(probe_src)
        except CodexConfigError:
            continue
        projects = probe.get("projects")
        if not isinstance(projects, dict) or target not in projects:
            continue
        entry = projects.get(target)
        if not isinstance(entry, dict):
            continue

        lhs = stripped.split("=", 1)[0].strip()
        # Dotted key ending with `.trust_level`
        if lhs.endswith("trust_level") or lhs.endswith('"trust_level"') or lhs.endswith("'trust_level'"):
            lines[idx] = f'{indent}{lhs} = "trusted"{ws_before}{comment}'
            return True

        # Single-line inline table: `<key> = { ... }`
        rhs = stripped.split("=", 1)[1].strip()
        if rhs.startswith("{") and rhs.endswith("}"):
            inner = rhs[1:-1].strip()
            if inner:
                # Re-serialize only this single inline table's entries cleanly while keeping other keys
                new_entry = dict(entry)
                new_entry["trust_level"] = "trusted"
                parts: List[str] = []
                for k, v in new_entry.items():
                    k_str = k if k.replace("_", "").replace("-", "").isalnum() else _toml_quote(str(k))
                    if isinstance(v, bool):
                        v_str = "true" if v else "false"
                    elif isinstance(v, int):
                        v_str = str(v)
                    elif isinstance(v, str):
                        v_str = _toml_quote(v)
                    else:
                        return False
                    parts.append(f"{k_str} = {v_str}")
                lines[idx] = f"{indent}{lhs} = {{ {', '.join(parts)} }}{ws_before}{comment}"
            else:
                lines[idx] = f'{indent}{lhs} = {{ trust_level = "trusted" }}{ws_before}{comment}'
            return True
    return False


def _update_codex_config_text(
    original_text: str,
    targets: List[str],
    parsed_existing: Dict[str, Any],
) -> Tuple[str, List[str], List[str]]:
    """Apply minimal text-level edits/appends to trust `targets` without reordering."""
    newline = "\r\n" if "\r\n" in original_text else "\n"
    lines = original_text.splitlines()
    projects_map = (
        parsed_existing.get("projects")
        if isinstance(parsed_existing.get("projects"), dict)
        else {}
    )

    added: List[str] = []
    updated: List[str] = []

    for target in targets:
        if target in projects_map:
            hdr_idx: Optional[int] = None
            for i, line in enumerate(lines):
                if _parse_project_table_header(line) == target:
                    hdr_idx = i
                    break

            if hdr_idx is not None:
                sec_end = len(lines)
                for j in range(hdr_idx + 1, len(lines)):
                    if _is_table_header_line(lines[j]):
                        sec_end = j
                        break

                replaced = False
                for k in range(hdr_idx + 1, sec_end):
                    if _is_trust_level_assignment(lines[k]):
                        _, ws_before, comment = _split_toml_comment(lines[k])
                        indent = lines[k][: len(lines[k]) - len(lines[k].lstrip(" \t"))]
                        lines[k] = f'{indent}trust_level = "trusted"{ws_before}{comment}'
                        replaced = True
                        break

                if not replaced:
                    insert_at = sec_end
                    while insert_at > hdr_idx + 1 and not lines[insert_at - 1].strip():
                        insert_at -= 1
                    lines.insert(insert_at, 'trust_level = "trusted"')
                updated.append(target)
                continue

            if _try_update_inline_or_dotted_trust(lines, target):
                updated.append(target)
                continue

            # Fallback append: if this conflicts with an exotic structure, post-write
            # TOML verification will catch it, roll back from backup, and raise.
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"[projects.{_toml_quote(target)}]")
            lines.append('trust_level = "trusted"')
            updated.append(target)
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"[projects.{_toml_quote(target)}]")
            lines.append('trust_level = "trusted"')
            added.append(target)

    new_text = newline.join(lines) + newline
    return new_text, added, updated


def _atomic_write_bytes(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(path.parent),
            prefix=f".{path.name}.herdr-tmp-",
            delete=False,
        ) as fh:
            tmp_path = Path(fh.name)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _create_config_backup(config_path: Path, raw_bytes: bytes, mode: int) -> Path:
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    base_name = f"{config_path.name}.bak-herdr-{ts}"
    candidate = config_path.parent / base_name
    seq = 1
    while candidate.exists():
        candidate = config_path.parent / f"{base_name}-{seq}"
        seq += 1
    _atomic_write_bytes(candidate, raw_bytes, mode)
    return candidate


def _validate_written_codex_config(text: str, required_paths: List[str]) -> None:
    parsed = _parse_toml(text)
    for p in required_paths:
        if not _is_project_trusted(parsed, p):
            raise CodexConfigError(
                f"Post-write validation failed: project {p!r} is not trusted in config.toml."
            )


def ensure_codex_projects_trusted(
    project_dirs: Sequence[str | Path],
    *,
    auto_trust: bool = True,
    codex_home: Optional[str | Path] = None,
) -> ProjectTrustResult:
    """Idempotently ensure `project_dirs` are marked trusted in `$CODEX_HOME/config.toml`.

    - Preserves all existing content, ordering, comments, and file permissions.
    - Creates a backup (`config.toml.bak-herdr-<timestamp>`) before modifying an existing file.
    - Writes atomically via temporary file + `os.replace`, then re-parses with TOML parser
      to validate; rolls back from backup and raises `CodexConfigError` on invalid result.
    - When `auto_trust=False`, performs no writes and returns the manual TOML snippet.
    """
    home_dir = resolve_codex_home(codex_home)
    config_path = home_dir / "config.toml"
    targets = _normalize_project_dirs(project_dirs)
    if not targets:
        return ProjectTrustResult(
            supported=True,
            config_path=config_path,
            auto_trust=auto_trust,
            changed=False,
        )

    existed_before = config_path.exists()
    orig_bytes = b""
    orig_text = ""
    orig_mode = 0o600
    parsed_existing: Dict[str, Any] = {}

    if existed_before:
        orig_mode = stat.S_IMODE(config_path.stat().st_mode)
        orig_bytes = config_path.read_bytes()
        orig_text = orig_bytes.decode("utf-8")
        if auto_trust:
            parsed_existing = _parse_toml(orig_text)
        else:
            try:
                parsed_existing = _parse_toml(orig_text)
            except CodexConfigError:
                parsed_existing = {}

    already_trusted: List[str] = []
    need_trust: List[str] = []
    for t in targets:
        if _is_project_trusted(parsed_existing, t):
            already_trusted.append(t)
        else:
            need_trust.append(t)

    if not need_trust:
        return ProjectTrustResult(
            supported=True,
            config_path=config_path,
            auto_trust=auto_trust,
            changed=False,
            created=False,
            already_trusted=already_trusted,
        )

    if not auto_trust:
        return ProjectTrustResult(
            supported=True,
            config_path=config_path,
            auto_trust=False,
            changed=False,
            created=False,
            already_trusted=already_trusted,
            pending_manual=need_trust,
            manual_snippet=format_codex_trust_snippet(need_trust),
        )

    new_text, added, updated = _update_codex_config_text(
        orig_text, need_trust, parsed_existing
    )
    backup_path: Optional[Path] = None
    if existed_before:
        backup_path = _create_config_backup(config_path, orig_bytes, orig_mode)

    try:
        _atomic_write_bytes(config_path, new_text.encode("utf-8"), orig_mode)
        written_text = config_path.read_text(encoding="utf-8")
        _validate_written_codex_config(written_text, targets)
    except Exception as exc:
        if existed_before and backup_path is not None and backup_path.exists():
            try:
                _atomic_write_bytes(config_path, backup_path.read_bytes(), orig_mode)
            except OSError:
                pass
        elif not existed_before:
            try:
                config_path.unlink()
            except OSError:
                pass
        if isinstance(exc, CodexConfigError):
            raise
        raise CodexConfigError(
            f"Failed to update {config_path} (rolled back): {exc}"
        ) from exc

    return ProjectTrustResult(
        supported=True,
        config_path=config_path,
        auto_trust=True,
        changed=True,
        created=(not existed_before),
        added=added,
        updated=updated,
        already_trusted=already_trusted,
        backup_path=backup_path,
    )


def _extract_codex_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: List[str] = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                itype = str(item.get("type") or "").lower()
                # Skip reasoning/encrypted items if ever present in a mixed content list
                if itype in {"reasoning", "reasoning_text", "encrypted_content"}:
                    continue
                if "text" in item and item["text"] is not None:
                    parts.append(str(item["text"]))
                elif "content" in item and item["content"] is not None:
                    parts.append(_extract_codex_text(item["content"]))
        return "\n".join(p for p in parts if p)
    if isinstance(raw, dict):
        if "text" in raw and raw["text"] is not None:
            return str(raw["text"])
        if "message" in raw and raw["message"] is not None:
            return _extract_codex_text(raw["message"])
        if "content" in raw and raw["content"] is not None:
            return _extract_codex_text(raw["content"])
    return str(raw)


def _parse_codex_rollout_line(obj: Dict[str, Any], line_no: int) -> Optional[Turn]:
    """Parse a single Codex rollout JSONL line into a normalized Turn.

    Only consumes `event_msg` user/assistant message events so that AGENTS.md
    instructions, `<environment_context>`, developer hook injections, reasoning
    blocks, and tool calls in `response_item` are never treated as dialogue turns.
    """
    if obj.get("type") != "event_msg":
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None

    ts = str(obj.get("timestamp") or "")
    ptype = str(payload.get("type") or "")

    role: Optional[str] = None
    text = ""

    if ptype == "user_message":
        role = "user"
        text = _extract_codex_text(payload.get("message")).strip()
    elif ptype == "agent_message":
        role = "assistant"
        text = _extract_codex_text(payload.get("message")).strip()
    elif ptype == "item_completed":
        item = payload.get("item")
        if isinstance(item, dict):
            itype = str(item.get("type") or "")
            if itype == "UserMessage":
                role = "user"
                text = _extract_codex_text(item.get("content")).strip()
            elif itype == "AgentMessage":
                role = "assistant"
                text = _extract_codex_text(item.get("content")).strip()

    if not role or not text:
        return None
    return Turn(role=role, text=text, timestamp=ts, line_no=line_no)


def _is_herdr_hook_handler(handler: Any, target_cmd: str) -> bool:
    if not isinstance(handler, dict):
        return False
    if handler.get("type") not in {None, "command"}:
        return False
    cmd = str(handler.get("command") or "").strip()
    if not cmd:
        return False
    return cmd == target_cmd or "hq-agent-hook" in cmd


def _merge_codex_hooks_file(workspace_dir: Path, hook_bin: str) -> Path:
    hooks_dir = workspace_dir / ".codex"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hooks_file = hooks_dir / "hooks.json"

    existing: Dict[str, Any] = {}
    if hooks_file.exists():
        try:
            loaded = json.loads(hooks_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except json.JSONDecodeError:
            existing = {}

    hooks_map = existing.get("hooks")
    if not isinstance(hooks_map, dict):
        hooks_map = {}

    cmd = f"{hook_bin} --agent codex"
    desired_handler: Dict[str, Any] = {"type": "command", "command": cmd}

    for event_name in ("SessionStart", "UserPromptSubmit", "Stop"):
        raw_groups = hooks_map.get(event_name)
        groups: List[Any] = list(raw_groups) if isinstance(raw_groups, list) else []
        new_groups: List[Any] = []
        placed = False

        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                new_groups.append(group)
                continue

            handlers = list(group["hooks"])
            has_managed = any(_is_herdr_hook_handler(h, cmd) for h in handlers)
            if not has_managed:
                new_groups.append(group)
                continue

            # Preserve non-herdr handlers in the same group; update/dedupe our handler.
            updated_handlers: List[Any] = []
            for h in handlers:
                if _is_herdr_hook_handler(h, cmd):
                    if not placed:
                        updated_handlers.append(dict(desired_handler))
                        placed = True
                else:
                    updated_handlers.append(h)

            if updated_handlers:
                updated_group = dict(group)
                updated_group["hooks"] = updated_handlers
                new_groups.append(updated_group)

        if not placed:
            new_groups.append({"hooks": [dict(desired_handler)]})

        hooks_map[event_name] = new_groups

    existing["hooks"] = hooks_map
    hooks_file.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return hooks_file


def _inspect_git_worktree(
    workspace_dir: Path,
) -> Optional[Tuple[Path, Path, Path, Optional[Path]]]:
    """Return `(toplevel, git_dir, common_dir, main_worktree_root_or_none)` if inside git."""
    try:
        res = subprocess.run(
            [
                "git",
                "-C",
                str(workspace_dir),
                "rev-parse",
                "--show-toplevel",
                "--git-dir",
                "--git-common-dir",
                "--is-bare-repository",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if res.returncode != 0:
        return None
    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    if len(lines) < 4:
        return None

    toplevel = Path(lines[0]).resolve()
    raw_git_dir = Path(lines[1])
    git_dir = (
        raw_git_dir.resolve()
        if raw_git_dir.is_absolute()
        else (workspace_dir / raw_git_dir).resolve()
    )
    raw_common_dir = Path(lines[2])
    common_dir = (
        raw_common_dir.resolve()
        if raw_common_dir.is_absolute()
        else (workspace_dir / raw_common_dir).resolve()
    )
    is_bare = lines[3].lower() == "true"

    main_root: Optional[Path] = None
    if git_dir != common_dir and not is_bare and common_dir.name == ".git":
        candidate = common_dir.parent.resolve()
        if candidate.is_dir() and candidate != toplevel:
            main_root = candidate
    return toplevel, git_dir, common_dir, main_root


def _is_git_tracked(repo_root: Path, rel_path: str) -> bool:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--error-unmatch", "--", rel_path],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return res.returncode == 0


def _ensure_git_info_exclude(common_dir: Path, pattern: str) -> bool:
    """Idempotently append `pattern` to `<common_dir>/info/exclude`, preserving existing content."""
    exclude_path = common_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    if exclude_path.exists():
        existing = exclude_path.read_text(encoding="utf-8")
        if any(line.strip() == pattern for line in existing.splitlines()):
            return False
        prefix = existing if (not existing or existing.endswith("\n")) else (existing + "\n")
        exclude_path.write_text(f"{prefix}{pattern}\n", encoding="utf-8")
        return True
    exclude_path.write_text(f"{pattern}\n", encoding="utf-8")
    return True


def _apply_codex_hooks_git_hygiene(
    repo_root: Path,
    common_dir: Path,
    hooks_file: Path,
) -> None:
    try:
        rel_path = hooks_file.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return
    if _is_git_tracked(repo_root, rel_path):
        print(
            f"[herdr-bot] warn: {hooks_file} is tracked by git; merging Codex hooks "
            "may produce a working-tree diff.",
            file=sys.stderr,
        )
        return
    _ensure_git_info_exclude(common_dir, f"/{rel_path}")


class CodexAdapter(CodingAgentAdapter):
    name = "codex"
    default_bin = "codex"
    index_filename = "AGENTS.md"

    def __init__(self, bin_path: str = "codex") -> None:
        self.bin_path = bin_path

    def build_headless_cmd(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        add_dirs: Optional[List[Path]] = None,
    ) -> List[str]:
        if session_id:
            # In codex-cli 0.163+, `--add-dir` is a parent `exec` option, not a `resume` option.
            cmd: List[str] = [self.bin_path, "exec"]
            for d in add_dirs or []:
                cmd.extend(["--add-dir", str(d)])
            cmd.extend(["resume", str(session_id), "--json"])
            if model:
                cmd.extend(["-m", str(model)])
            if effort:
                cmd.extend(["-c", f"model_reasoning_effort={_toml_quote(str(effort))}"])
            cmd.append("--skip-git-repo-check")
            cmd.append(prompt)
            return cmd

        cmd = [self.bin_path, "exec", "--json"]
        if model:
            cmd.extend(["-m", str(model)])
        if effort:
            cmd.extend(["-c", f"model_reasoning_effort={_toml_quote(str(effort))}"])
        for d in add_dirs or []:
            cmd.extend(["--add-dir", str(d)])
        cmd.append("--skip-git-repo-check")
        cmd.append(prompt)
        return cmd

    def build_interactive_cmd(
        self,
        initial_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        add_dirs: Optional[List[Path]] = None,
    ) -> List[str]:
        cmd: List[str] = [self.bin_path]
        if session_id:
            cmd.extend(["resume", str(session_id)])
        if model:
            cmd.extend(["-m", str(model)])
        if effort:
            cmd.extend(["-c", f"model_reasoning_effort={_toml_quote(str(effort))}"])
        for d in add_dirs or []:
            cmd.extend(["--add-dir", str(d)])
        if initial_prompt:
            cmd.append(initial_prompt)
        return cmd

    def extra_trust_args(
        self, trusted_dirs: List[Path], permission_mode: str = "default"
    ) -> List[str]:
        args: List[str] = []
        unique: List[str] = []
        for d in trusted_dirs:
            if not d:
                continue
            resolved = str(Path(d).expanduser().resolve())
            if resolved not in unique:
                unique.append(resolved)
        for resolved in unique:
            args.extend(["-c", f"projects.{_toml_quote(resolved)}.trust_level=\"trusted\""])

        # Codex 0.163+ requires persisted per-file `hooks.state.<path>.trusted_hash` or
        # `--dangerously-bypass-hook-trust` to run project `.codex/hooks.json` hooks.
        args.append("--dangerously-bypass-hook-trust")

        mode = (permission_mode or "default").strip().lower()
        if mode in {"turbo", "always-proceed"}:
            args.extend(["-s", "workspace-write", "-a", "never"])
        elif mode == "accept-edits":
            args.extend(["-s", "workspace-write", "-a", "on-request"])
        elif mode == "skip-all":
            args.append("--dangerously-bypass-approvals-and-sandbox")
        return args

    def ensure_project_trusted(
        self,
        project_dirs: Sequence[str | Path],
        *,
        auto_trust: bool = True,
        codex_home: Optional[str | Path] = None,
    ) -> ProjectTrustResult:
        return ensure_codex_projects_trusted(
            project_dirs,
            auto_trust=auto_trust,
            codex_home=codex_home,
        )

    def parse_headless_output(self, stdout: str) -> HeadlessResult:
        raw = (stdout or "").strip()
        if not raw:
            return HeadlessResult(reply_markdown="", agent_session_id=None)

        found_json = False
        sid: Optional[str] = None
        messages: List[str] = []

        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            found_json = True

            ev_type = str(obj.get("type") or "")
            if ev_type == "thread.started" and obj.get("thread_id"):
                sid = str(obj["thread_id"])
            elif ev_type == "item.completed" and isinstance(obj.get("item"), dict):
                item = obj["item"]
                if item.get("type") == "agent_message":
                    text = _extract_codex_text(
                        item.get("text") if item.get("text") is not None else item.get("content")
                    ).strip()
                    if text:
                        messages.append(text)
            elif ev_type == "agent_message":
                text = _extract_codex_text(
                    obj.get("text") if obj.get("text") is not None else obj.get("message")
                ).strip()
                if text:
                    messages.append(text)

        if not found_json:
            return HeadlessResult(reply_markdown=raw, agent_session_id=None)

        return HeadlessResult(
            reply_markdown=messages[-1] if messages else "",
            agent_session_id=sid,
        )

    def count_transcript_lines(self, transcript_path: str | Path) -> int:
        path = Path(transcript_path)
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8") as fh:
            return sum(1 for _ in fh)

    def read_transcript_slice(
        self, transcript_path: str | Path, from_line: int = 0
    ) -> Tuple[List[Turn], int]:
        path = Path(transcript_path)
        if not path.exists():
            return [], 0
        turns: List[Turn] = []
        total = 0
        with path.open("r", encoding="utf-8") as fh:
            for idx, line in enumerate(fh):
                total = idx + 1
                if idx < from_line:
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    t = _parse_codex_rollout_line(obj, line_no=idx)
                    if t is not None:
                        turns.append(t)
        return turns, total

    def ensure_hooks_installed(
        self, workspace_dir: str | Path, hook_bin: str
    ) -> Path:
        ws = Path(workspace_dir).expanduser().resolve()
        hooks_file = _merge_codex_hooks_file(ws, hook_bin)

        git_info = _inspect_git_worktree(ws)
        if git_info is not None:
            toplevel, _git_dir, common_dir, main_root = git_info
            _apply_codex_hooks_git_hygiene(toplevel, common_dir, hooks_file)
            if main_root is not None:
                # Codex 0.163+ in a linked git worktree requires `<wt>/.codex/` to exist
                # while loading hook definitions from `<main_repo>/.codex/hooks.json`.
                main_hooks_file = _merge_codex_hooks_file(main_root, hook_bin)
                _apply_codex_hooks_git_hygiene(main_root, common_dir, main_hooks_file)

        return hooks_file

    def parse_hook_payload(self, payload: Dict[str, Any]) -> HookEvent:
        data = payload if isinstance(payload, dict) else {}
        raw_name = str(
            data.get("hook_event_name")
            or data.get("hookEventName")
            or data.get("event")
            or data.get("type")
            or ""
        )
        kind: HookEventKind
        if raw_name == "SessionStart":
            kind = "session_start"
        elif raw_name == "UserPromptSubmit":
            kind = "user_prompt"
        elif raw_name == "Stop":
            kind = "stop"
        else:
            kind = "other"

        agent_sid = str(data.get("session_id") or "")
        transcript_path = str(data.get("transcript_path") or "")
        raw_prompt = data.get("prompt")
        user_prompt = str(raw_prompt) if raw_prompt is not None else None
        cwd = str(data["cwd"]) if data.get("cwd") else None

        return HookEvent(
            kind=kind,
            raw_event_name=raw_name,
            agent_session_id=agent_sid,
            transcript_path=transcript_path,
            user_prompt=user_prompt,
            cwd=cwd,
            raw=dict(data),
        )

    def render_hook_response(
        self,
        event: HookEvent,
        inject_messages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if event.kind == "stop":
            return {}
        if event.kind in {"session_start", "user_prompt"}:
            msgs = [str(m) for m in (inject_messages or []) if str(m)]
            if msgs:
                hook_event_name = (
                    "SessionStart" if event.kind == "session_start" else "UserPromptSubmit"
                )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": hook_event_name,
                        "additionalContext": "\n\n".join(msgs),
                    }
                }
        return {}
