"""Decoupled cursor tracking (.hq/cursors.json) for git heads and session transcripts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
import json
import os
import tempfile


class CursorStore:
    def __init__(self, cursors_path: str | Path) -> None:
        self.path = Path(cursors_path)
        self.data: Dict[str, Any] = {"git_heads": {}, "transcripts": {}}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.data = {"git_heads": {}, "transcripts": {}}
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
        self.data = {
            "git_heads": dict(raw.get("git_heads") or {}),
            "transcripts": dict(raw.get("transcripts") or {}),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.data, indent=2, sort_keys=True, ensure_ascii=False)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".cursors-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.write("\n")
            os.replace(tmp_name, self.path)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    def get_git_head(self, repo_name: str) -> str:
        return str(self.data["git_heads"].get(repo_name, ""))

    def set_git_head(self, repo_name: str, sha: str) -> None:
        self.data["git_heads"][repo_name] = sha
        self.save()

    def get_transcript_cursor(self, agent_session_id: str) -> Dict[str, Any]:
        entry = self.data["transcripts"].get(agent_session_id)
        if not isinstance(entry, dict):
            return {
                "last_summarized_line": 0,
                "transcript_path": "",
                "session_key": "",
            }
        return {
            "last_summarized_line": int(entry.get("last_summarized_line", 0)),
            "transcript_path": str(entry.get("transcript_path", "")),
            "session_key": str(entry.get("session_key", "")),
        }

    def advance_transcript_cursor(
        self,
        agent_session_id: str,
        new_line: int,
        transcript_path: str = "",
        session_key: str = "",
    ) -> None:
        prev = self.get_transcript_cursor(agent_session_id)
        self.data["transcripts"][agent_session_id] = {
            "last_summarized_line": max(prev["last_summarized_line"], int(new_line)),
            "transcript_path": transcript_path or prev["transcript_path"],
            "session_key": session_key or prev["session_key"],
        }
        self.save()
