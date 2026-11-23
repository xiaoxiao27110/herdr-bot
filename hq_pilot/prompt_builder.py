"""Prefix-Cache-friendly deterministic prompt builder."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import json

from hq_pilot.config import HQConfig


def _read_optional(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _select_contracts(
    hq_dir: Path,
    config: HQConfig,
    matched_repos: Optional[List[str]] = None,
) -> List[Path]:
    contracts_dir = hq_dir / "memory" / "contracts"
    if not contracts_dir.exists():
        return []
    all_contracts = sorted(p for p in contracts_dir.glob("*.md") if p.is_file())
    if not matched_repos:
        return all_contracts

    repo_set: Set[str] = set(matched_repos)
    selected_names: Set[str] = set()
    for rname in repo_set:
        boundary = config.boundaries.repos.get(rname)
        if boundary:
            for dep in boundary.downstream:
                if dep.contract:
                    selected_names.add(Path(dep.contract).name)
                if dep.repo:
                    repo_set.add(dep.repo)

    filtered: List[Path] = []
    for p in all_contracts:
        if p.name in selected_names:
            filtered.append(p)
            continue
        stem_tokens = set(p.stem.lower().split("-"))
        if any(r.lower() in p.stem.lower() or r.lower() in stem_tokens for r in repo_set):
            filtered.append(p)
    return filtered


def build_prompt(
    hq_dir: str | Path,
    config: HQConfig,
    event_payload: Dict[str, Any],
    matched_repos: Optional[List[str]] = None,
) -> str:
    """
    Deterministically assemble the initial user prompt in strict Prefix-Cache order:
      1. repo-boundaries.yaml
      2. Sorted matching memory/contracts/*.md
      3. tasks/active-wip.md
      4. notes/today.md
      5. event_payload (canonical JSON)
    """
    root = Path(hq_dir).resolve()
    sections: List[str] = []

    boundaries_text = _read_optional(root / "repo-boundaries.yaml").rstrip()
    sections.append("## 1. Repo Boundaries\n```yaml\n" + boundaries_text + "\n```")

    contract_paths = _select_contracts(root, config, matched_repos=matched_repos)
    contract_blocks: List[str] = []
    for cp in contract_paths:
        rel = cp.relative_to(root).as_posix()
        body = _read_optional(cp).rstrip()
        contract_blocks.append(f"### {rel}\n{body}")
    sections.append(
        "## 2. Relevant Contracts\n"
        + ("\n\n".join(contract_blocks) if contract_blocks else "(none)")
    )

    wip_text = _read_optional(root / "tasks" / "active-wip.md").rstrip()
    sections.append("## 3. Active WIP\n" + (wip_text or "(empty)"))

    today_text = _read_optional(root / "notes" / "today.md").rstrip()
    sections.append("## 4. Today WAL\n" + (today_text or "(empty)"))

    payload_json = json.dumps(
        event_payload, sort_keys=True, ensure_ascii=False, indent=2
    )
    sections.append("## 5. Event Payload\n```json\n" + payload_json + "\n```")

    return "\n\n".join(sections) + "\n"
