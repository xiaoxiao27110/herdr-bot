"""Tests for ephemeral worker contract, inline skills, verified Dreaming, distill NOOP tightening, and `dream` CLI."""

from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hq_pilot.cli import main as hq_main
from hq_pilot.daemon import (
    EPHEMERAL_WORKER_CONTRACT,
    HQDaemon,
    _has_dream_done_marker,
    _is_noop_distill_reply,
    _load_builtin_skill,
)
from hq_pilot.db import HQDatabase
from hq_pilot.guards import run_memory_guard
try:
    from tests.test_archiver_resume_cursors import _init_git_repo
    from tests.test_v2_interaction_flows import (
        IndicatorFakeChannel,
        NoHeadlessAgyAdapter,
        StatusAwareTerminal,
    )
except ModuleNotFoundError:
    from test_archiver_resume_cursors import _init_git_repo
    from test_v2_interaction_flows import (
        IndicatorFakeChannel,
        NoHeadlessAgyAdapter,
        StatusAwareTerminal,
    )


def _setup_guard_clean_hq(tmp: str, name: str = "acme-hq") -> Path:
    """Create an initialized HQ workspace whose AGENTS.md logical references resolve cleanly."""
    hq_dir = Path(tmp) / name
    hq_main(["init", str(hq_dir)])
    gw_dir = Path(tmp) / "gateway-svc"
    _init_git_repo(gw_dir)
    (gw_dir / "api").mkdir(parents=True, exist_ok=True)
    (gw_dir / "api" / "router.py").write_text("def route(): pass\n", encoding="utf-8")

    wk_dir = Path(tmp) / "worker-svc"
    _init_git_repo(wk_dir)
    (wk_dir / "jobs").mkdir(parents=True, exist_ok=True)
    (wk_dir / "jobs" / "runner.py").write_text("def run(): pass\n", encoding="utf-8")

    (hq_dir / ".hq" / "local.yaml").write_text(
        f"repo_paths:\n  gateway-svc: {gw_dir}\n  worker-svc: {wk_dir}\n",
        encoding="utf-8",
    )
    return hq_dir


class TestDreamAndDistillHardening(unittest.TestCase):
    def test_ephemeral_worker_contract_and_skill_inlining_with_graceful_fallback(self) -> None:
        """All ephemeral prompts prepend Worker contract; dream/distill/commit inline their SKILL.md and degrade cleanly if missing."""
        self.assertEqual(_load_builtin_skill("nonexistent-skill-xyz"), "")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_load_builtin_skill("dream-consolidate", skills_root=Path(tmp)), "")

            hq_dir = _setup_guard_clean_hq(tmp)
            tfile = Path(tmp) / "sess.jsonl"
            tfile.write_text(
                json.dumps({"role": "user", "content": "check api"}) + "\n"
                + json.dumps({"role": "assistant", "content": "done"}) + "\n",
                encoding="utf-8",
            )
            (hq_dir / "notes" / "today.md").write_text(
                "# Today WAL [UNVERIFIED / TODAY ONLY]\n\n- [DECISION] Keep v1 endpoint\n",
                encoding="utf-8",
            )

            term = StatusAwareTerminal(
                reply_fn=lambda name, text: (
                    "DREAM_DONE"
                    if name.startswith("hq-bg-dream-")
                    else "NOOP"
                    if name.startswith("hq-bg-distill-")
                    else "### 🚀 昨日推进与决策\n- 完成联调\n"
                )
            )
            ch = IndicatorFakeChannel()
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=ch,
            )
            try:
                daemon.db.enqueue_event(
                    source="git_commit",
                    dedupe_key="gc:skill:1",
                    priority=20,
                    available_at=5000,
                    payload={"repo": "gateway-svc", "matched_files": ["api/router.py"]},
                    now=5000,
                )
                daemon.db.enqueue_event(
                    source="session_distill",
                    dedupe_key="sd:skill:1",
                    priority=30,
                    available_at=5000,
                    payload={
                        "session_key": "general:1",
                        "agent_session_id": "conv-skill-1",
                        "transcript_path": str(tfile),
                        "from_line": 0,
                        "to_line": 2,
                        "turns": [{"role": "user", "text": "check api"}],
                    },
                    now=5000,
                )
                daemon.run_once(now=5000)

                commit_md = hq_dir / ".hq" / "prompts" / "commit-5000.md"
                self.assertTrue(commit_md.exists())
                commit_text = commit_md.read_text(encoding="utf-8")
                self.assertTrue(commit_text.startswith(EPHEMERAL_WORKER_CONTRACT))
                self.assertIn("严禁调用 `invoke_subagent`", commit_text)
                self.assertIn("# Boundary Triage Skill", commit_text)

                distill_md = hq_dir / ".hq" / "prompts" / "distill-5000.md"
                self.assertTrue(distill_md.exists())
                distill_text = distill_md.read_text(encoding="utf-8")
                self.assertTrue(distill_text.startswith(EPHEMERAL_WORKER_CONTRACT))
                self.assertIn("# Session Distill Skill", distill_text)
                self.assertIn("只输出单独一行 `NOOP`", distill_text)

                # Morning digest ephemeral prompt also gets the Worker contract header
                daemon.run_morning_digest(now=5100)
                digest_md = hq_dir / ".hq" / "prompts" / "digest-5100.md"
                self.assertTrue(digest_md.exists())
                digest_text = digest_md.read_text(encoding="utf-8")
                self.assertTrue(digest_text.startswith(EPHEMERAL_WORKER_CONTRACT))

                # Dream prompt gets Worker contract + dream-consolidate SKILL + Phase 1-3/Phase 4 & DREAM_DONE rules
                res = daemon.run_dream(now=5200, archive=False)
                self.assertTrue(res.ok)
                dream_md = hq_dir / ".hq" / "prompts" / "dream-5200.md"
                self.assertTrue(dream_md.exists())
                dream_text = dream_md.read_text(encoding="utf-8")
                self.assertTrue(dream_text.startswith(EPHEMERAL_WORKER_CONTRACT))
                self.assertIn("# Dream Consolidate Skill", dream_text)
                self.assertIn("Phase 1~3", dream_text)
                self.assertIn("严禁修改或清空 `notes/today.md`", dream_text)
                self.assertIn("DREAM_DONE", dream_text)
                self.assertIn("herdr-bot guard --hq-dir", dream_text)

                # Degrade gracefully when skills directory is missing
                with patch("hq_pilot.daemon._BUILTIN_SKILLS_DIR", Path(tmp) / "missing-skills"):
                    res_degraded = daemon.run_dream(now=5300, archive=False)
                    self.assertTrue(res_degraded.ok)
                    degraded_text = (hq_dir / ".hq" / "prompts" / "dream-5300.md").read_text(
                        encoding="utf-8"
                    )
                    self.assertTrue(degraded_text.startswith(EPHEMERAL_WORKER_CONTRACT))
                    self.assertNotIn("# Dream Consolidate Skill", degraded_text)
            finally:
                daemon.close()

    def test_dream_missing_marker_does_not_archive_and_writes_alert(self) -> None:
        """If dream worker exits without DREAM_DONE (e.g. spawned subagent), today.md is preserved and dream_error alert is logged."""
        self.assertFalse(_has_dream_done_marker(""))
        self.assertFalse(_has_dream_done_marker("已派发子 Agent，等待完成回执"))
        self.assertFalse(_has_dream_done_marker("失败，未输出 DREAM_DONE 标记"))
        self.assertTrue(_has_dream_done_marker("整理完毕\nDREAM_DONE"))
        self.assertTrue(_has_dream_done_marker("核对完成\nDREAM_DONE: no-op 无新增需晋升条目"))
        self.assertTrue(_has_dream_done_marker("`DREAM_DONE`"))

        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = _setup_guard_clean_hq(tmp)
            original_today = (
                "# Today WAL [UNVERIFIED / TODAY ONLY]\n\n"
                "- [DECISION] Must not be lost when dreaming fails\n"
            )
            (hq_dir / "notes" / "today.md").write_text(original_today, encoding="utf-8")

            term = StatusAwareTerminal(
                reply_fn=lambda name, text: "已读取记忆并派发子 Agent 处理，等待回执。"
            )
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=IndicatorFakeChannel(),
            )
            try:
                daemon.db.set_meta("sessions_since_dream", "3", now=1000)
                ts = int(datetime(2026, 11, 26, 23, 40, 0).timestamp())
                daemon.run_nightly_pipeline(now=ts)

                # today.md preserved untouched and NOT archived
                self.assertEqual((hq_dir / "notes" / "today.md").read_text(encoding="utf-8"), original_today)
                self.assertFalse((hq_dir / "notes" / "archive" / "2026-11-26.md").exists())

                # Dream metadata not advanced
                self.assertIsNone(daemon.db.get_meta("last_dream_at"))
                self.assertIsNone(daemon.db.get_meta("last_dream_success_at"))
                self.assertEqual(daemon.db.get_meta("sessions_since_dream"), "3")

                # Alert queued for next morning digest
                alerts = [
                    it for it in daemon.db.list_pending_digest_items()
                    if it["category"] == "dream_error"
                ]
                self.assertEqual(len(alerts), 1)
                self.assertEqual(alerts[0]["level"], "alert")
                self.assertIn("DREAM_DONE", alerts[0]["message"])
                self.assertIn("未归档", alerts[0]["message"])
            finally:
                daemon.close()

    def test_dream_guard_failure_blocks_archive_and_writes_alert(self) -> None:
        """If dream outputs DREAM_DONE but memory_guard fails, today.md is preserved and dream_error alert records violations."""
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = _setup_guard_clean_hq(tmp)
            original_today = "# Today WAL [UNVERIFIED / TODAY ONLY]\n\n- [WIP] Guard failure test\n"
            (hq_dir / "notes" / "today.md").write_text(original_today, encoding="utf-8")
            (hq_dir / "memory" / "decisions.md").write_text(
                "# Architecture Decisions\n\n- Broken ref: `gateway-svc:api/nonexistent.py`\n",
                encoding="utf-8",
            )

            term = StatusAwareTerminal(reply_fn=lambda name, text: "已更新决策\nDREAM_DONE")
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=IndicatorFakeChannel(),
            )
            try:
                daemon.db.set_meta("sessions_since_dream", "2", now=1000)
                ts = int(datetime(2026, 11, 26, 23, 40, 0).timestamp())
                daemon.run_nightly_pipeline(now=ts)

                self.assertEqual((hq_dir / "notes" / "today.md").read_text(encoding="utf-8"), original_today)
                self.assertFalse((hq_dir / "notes" / "archive" / "2026-11-26.md").exists())
                self.assertIsNone(daemon.db.get_meta("last_dream_success_at"))
                self.assertEqual(daemon.db.get_meta("sessions_since_dream"), "2")

                alerts = [
                    it for it in daemon.db.list_pending_digest_items()
                    if it["category"] == "dream_error"
                ]
                self.assertEqual(len(alerts), 1)
                self.assertIn("memory_guard", alerts[0]["message"])
                self.assertIn("nonexistent.py", alerts[0]["message"])
            finally:
                daemon.close()

    def test_dream_success_archives_today_updates_metas_even_when_today_exceeds_wal_limit(self) -> None:
        """Verified DREAM_DONE + guard pass archives today.md (even >60 lines), resets WAL, and updates last_dream_success_at."""
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = _setup_guard_clean_hq(tmp)
            # 70 lines (> default today_wal_max_lines=60) must NOT deadlock archiving
            long_lines = ["# Today WAL [UNVERIFIED / TODAY ONLY]"] + [
                f"- [DECISION] Verified rule #{i}" for i in range(1, 70)
            ]
            (hq_dir / "notes" / "today.md").write_text("\n".join(long_lines) + "\n", encoding="utf-8")

            term = StatusAwareTerminal(reply_fn=lambda name, text: "完成晋升\nDREAM_DONE")
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=IndicatorFakeChannel(),
            )
            try:
                daemon.db.set_meta("sessions_since_dream", "5", now=1000)
                ts = int(datetime(2026, 11, 26, 23, 40, 0).timestamp())
                daemon.run_nightly_pipeline(now=ts)

                # Archived & reset
                arch_path = hq_dir / "notes" / "archive" / "2026-11-26.md"
                self.assertTrue(arch_path.exists())
                self.assertIn("Verified rule #69", arch_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    (hq_dir / "notes" / "today.md").read_text(encoding="utf-8"),
                    "# Today WAL [UNVERIFIED / TODAY ONLY]\n\n",
                )

                # Metadata updated & no alerts
                self.assertEqual(daemon.db.get_meta("last_dream_at"), str(ts))
                self.assertEqual(daemon.db.get_meta("last_dream_success_at"), str(ts))
                self.assertEqual(daemon.db.get_meta("sessions_since_dream"), "0")
                self.assertEqual(daemon.db.list_pending_digest_items(), [])

                # Full guard passes after reset
                self.assertTrue(run_memory_guard(hq_dir).ok)
            finally:
                daemon.close()

    def test_dream_prompt_includes_archives_after_last_dream_success_at(self) -> None:
        """Dream prompt attaches all archives when never succeeded, and only archives dated after last_dream_success_at otherwise."""
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = _setup_guard_clean_hq(tmp)
            arch_dir = hq_dir / "notes" / "archive"
            (arch_dir / "2026-11-24.md").write_text("- [DECISION] Archive Nov 24\n", encoding="utf-8")
            (arch_dir / "2026-11-26.md").write_text("- [DECISION] Archive Nov 26\n", encoding="utf-8")

            term = StatusAwareTerminal(reply_fn=lambda name, text: "DREAM_DONE: no-op 已核验")
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=IndicatorFakeChannel(),
            )
            try:
                # Never succeeded -> both archives included
                res1 = daemon.run_dream(now=10000, archive=False)
                self.assertTrue(res1.ok)
                self.assertEqual(res1.included_archives, ["2026-11-24.md", "2026-11-26.md"])
                p1 = (hq_dir / ".hq" / "prompts" / "dream-10000.md").read_text(encoding="utf-8")
                self.assertIn("Archive Nov 24", p1)
                self.assertIn("Archive Nov 26", p1)

                # Set last_dream_success_at to 2026-11-24 23:30 -> only 2026-11-26.md included
                nov24_ts = int(datetime(2026, 11, 24, 23, 30, 0).timestamp())
                daemon.db.set_meta("last_dream_success_at", str(nov24_ts), now=nov24_ts)
                res2 = daemon.run_dream(now=20000, archive=False)
                self.assertTrue(res2.ok)
                self.assertEqual(res2.included_archives, ["2026-11-26.md"])
                p2 = (hq_dir / ".hq" / "prompts" / "dream-20000.md").read_text(encoding="utf-8")
                self.assertNotIn("Archive Nov 24", p2)
                self.assertIn("Archive Nov 26", p2)

                # Set last_dream_success_at to 2026-11-26 23:30 -> neither archive included
                nov26_ts = int(datetime(2026, 11, 26, 23, 30, 0).timestamp())
                daemon.db.set_meta("last_dream_success_at", str(nov26_ts), now=nov26_ts)
                res3 = daemon.run_dream(now=30000, archive=False)
                self.assertTrue(res3.ok)
                self.assertEqual(res3.included_archives, [])
                p3 = (hq_dir / ".hq" / "prompts" / "dream-30000.md").read_text(encoding="utf-8")
                self.assertNotIn("Archive Nov 24", p3)
                self.assertNotIn("Archive Nov 26", p3)
            finally:
                daemon.close()

    def test_distill_noop_tightening_blocks_noop_and_verbose_explanations(self) -> None:
        """Both exact NOOP and verbose '已核对…结论：无新增…已跳过写入' are skipped without touching today.md."""
        self.assertTrue(_is_noop_distill_reply("NOOP"))
        self.assertTrue(_is_noop_distill_reply("`NOOP`"))
        self.assertTrue(_is_noop_distill_reply("  noop.  "))
        verbose_noop = (
            "已读取并核对 [distill-1795707773.md](file:///tmp/distill-1795707773.md) 中的增量会话内容：\n\n"
            "- **结论**：该增量仅包含只读调研回执消息，**无新增 `[DECISION]`、`[CONTRACT_CHANGE]` 或 `[WIP]` 高信号条目**。\n"
            "- **动作**：无需更新 `notes/today.md`、`tasks/active-wip.md` 或跨仓契约文档，已跳过写入。"
        )
        self.assertTrue(_is_noop_distill_reply(verbose_noop))
        self.assertTrue(
            _is_noop_distill_reply("- [distill-1.md](file:///tmp/d.md) 核对完毕，已跳过写入。")
        )
        self.assertFalse(_is_noop_distill_reply("- [DECISION] 保留兼容字段"))
        self.assertFalse(_is_noop_distill_reply("- `[CONTRACT_CHANGE]` `gateway-svc@abc1234` 新增可选参数"))
        self.assertFalse(_is_noop_distill_reply("无新增 `[DECISION]`\n- `[WIP]` 跟进验证"))

        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = _setup_guard_clean_hq(tmp)
            tfile = Path(tmp) / "distill.jsonl"
            tfile.write_text(
                "\n".join(json.dumps({"role": "user", "content": f"m{i}"}) for i in range(6)) + "\n",
                encoding="utf-8",
            )
            replies = iter([
                "NOOP",
                verbose_noop,
                "- [DECISION] 明确保留双版本协议兼容",
            ])
            term = StatusAwareTerminal(reply_fn=lambda name, text: next(replies))
            daemon = HQDaemon(
                hq_dir,
                agent_adapter=NoHeadlessAgyAdapter(),
                terminal_driver=term,
                channel=IndicatorFakeChannel(),
            )
            try:
                before = (hq_dir / "notes" / "today.md").read_text(encoding="utf-8")
                for idx, to_line in enumerate((2, 4), start=1):
                    daemon.db.enqueue_event(
                        source="session_distill",
                        dedupe_key=f"sd:noop:{idx}",
                        priority=30,
                        available_at=4000 + idx,
                        payload={
                            "session_key": "general:1",
                            "agent_session_id": "conv-noop-tight",
                            "transcript_path": str(tfile),
                            "from_line": to_line - 2,
                            "to_line": to_line,
                            "turns": [{"role": "user", "text": f"m{idx}"}],
                        },
                        now=4000 + idx,
                    )
                summary = daemon.run_once(now=4010)
                self.assertEqual(summary.events_processed, 2)
                self.assertEqual((hq_dir / "notes" / "today.md").read_text(encoding="utf-8"), before)
                self.assertEqual(
                    daemon.cursors.get_transcript_cursor("conv-noop-tight")["last_summarized_line"],
                    4,
                )

                # Real decision entry is written normally
                daemon.db.enqueue_event(
                    source="session_distill",
                    dedupe_key="sd:real:3",
                    priority=30,
                    available_at=4020,
                    payload={
                        "session_key": "general:1",
                        "agent_session_id": "conv-noop-tight",
                        "transcript_path": str(tfile),
                        "from_line": 4,
                        "to_line": 6,
                        "turns": [{"role": "user", "text": "m5"}],
                    },
                    now=4020,
                )
                daemon.run_once(now=4020)
                after = (hq_dir / "notes" / "today.md").read_text(encoding="utf-8")
                self.assertIn("- [DECISION] 明确保留双版本协议兼容", after)
                self.assertNotIn("NOOP", after)
                self.assertNotIn("已跳过写入", after)
            finally:
                daemon.close()

    def test_cli_dream_no_archive_and_failure_exit_code(self) -> None:
        """`herdr-bot dream --hq-dir <dir> --no-archive` runs ephemeral Dreaming without archiving today.md and returns non-zero on failure."""
        with tempfile.TemporaryDirectory() as tmp:
            hq_dir = _setup_guard_clean_hq(tmp)
            original_today = "# Today WAL [UNVERIFIED / TODAY ONLY]\n\n- [WIP] Ongoing task\n"
            (hq_dir / "notes" / "today.md").write_text(original_today, encoding="utf-8")
            (hq_dir / "notes" / "archive" / "2026-11-24.md").write_text(
                "- [DECISION] Earlier decision\n", encoding="utf-8"
            )

            term_ok = StatusAwareTerminal(reply_fn=lambda name, text: "已晋升决策\nDREAM_DONE")
            buf_out = io.StringIO()
            with (
                patch("hq_pilot.daemon.get_agent_adapter", return_value=NoHeadlessAgyAdapter()),
                patch("hq_pilot.daemon.get_terminal_driver", return_value=term_ok),
                patch("hq_pilot.daemon.get_channel", return_value=IndicatorFakeChannel()),
                patch("sys.stdout", buf_out),
            ):
                rc = hq_main(["dream", "--hq-dir", str(hq_dir), "--no-archive"])

            self.assertEqual(rc, 0)
            self.assertIn("DREAM_DONE", buf_out.getvalue())
            self.assertIn("archived=False", buf_out.getvalue())
            self.assertIn("2026-11-24.md", buf_out.getvalue())
            # today.md kept intact
            self.assertEqual((hq_dir / "notes" / "today.md").read_text(encoding="utf-8"), original_today)
            # ephemeral dream pane started & killed
            self.assertEqual(len(term_ok.start_agent_calls), 1)
            pane_used = term_ok.start_agent_calls[0]["name"]
            self.assertTrue(pane_used.startswith("hq-bg-dream-"))
            self.assertIn(pane_used, term_ok.killed_panes)

            db = HQDatabase(hq_dir / ".hq" / "runtime.sqlite")
            try:
                self.assertIsNotNone(db.get_meta("last_dream_success_at"))
                self.assertIsNone(db.get_meta("last_dream_at"))
            finally:
                db.close()

            # Missing DREAM_DONE -> CLI exits 1
            term_bad = StatusAwareTerminal(reply_fn=lambda name, text: "未完成")
            buf_err = io.StringIO()
            with (
                patch("hq_pilot.daemon.get_agent_adapter", return_value=NoHeadlessAgyAdapter()),
                patch("hq_pilot.daemon.get_terminal_driver", return_value=term_bad),
                patch("hq_pilot.daemon.get_channel", return_value=IndicatorFakeChannel()),
                patch("sys.stderr", buf_err),
            ):
                rc_bad = hq_main(["dream", "--hq-dir", str(hq_dir), "--no-archive"])
            self.assertEqual(rc_bad, 1)
            self.assertIn("dream: FAIL", buf_err.getvalue())
            self.assertEqual((hq_dir / "notes" / "today.md").read_text(encoding="utf-8"), original_today)


if __name__ == "__main__":
    unittest.main()
