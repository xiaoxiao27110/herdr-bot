"""Auto-reexec into configured project python_bin from HQ_PYTHON or .hq/local.yaml."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import List, Optional


def _extract_hq_dir(argv: List[str]) -> Optional[Path]:
    for idx, arg in enumerate(argv):
        if arg == "--hq-dir" and idx + 1 < len(argv):
            return Path(argv[idx + 1]).expanduser().resolve()
        if arg.startswith("--hq-dir="):
            return Path(arg.split("=", 1)[1]).expanduser().resolve()
    env_dir = os.environ.get("HQ_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    cwd = Path.cwd()
    if (cwd / "hq.yaml").exists():
        return cwd
    return None


def _read_python_bin_from_local_yaml(hq_dir: Path) -> Optional[str]:
    local_yaml = hq_dir / ".hq" / "local.yaml"
    if not local_yaml.exists():
        return None
    try:
        for line in local_yaml.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("python_bin:"):
                val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    except OSError:
        return None
    return None


def maybe_reexec_python(script_file: str) -> None:
    """If HQ_PYTHON or .hq/local.yaml python_bin differs from current interpreter, re-exec."""
    if os.environ.get("HQ_REEXECED") == "1":
        return
    target = os.environ.get("HQ_PYTHON", "").strip() or None
    if not target:
        hq_dir = _extract_hq_dir(sys.argv[1:])
        if hq_dir is not None:
            target = _read_python_bin_from_local_yaml(hq_dir)
    if not target:
        return
    target_path = Path(target).expanduser()
    if not target_path.exists() or not os.access(target_path, os.X_OK):
        return
    try:
        if target_path.resolve() == Path(sys.executable).resolve():
            return
    except OSError:
        return
    os.environ["HQ_REEXECED"] = "1"
    os.execv(str(target_path), [str(target_path), script_file, *sys.argv[1:]])
