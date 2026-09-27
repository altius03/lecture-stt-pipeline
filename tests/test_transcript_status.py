from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.downstream import transcript_status
from lecture_stt.shared import db as shared_db


class TranscriptStatusCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.root = Path(self.tmpdir.name)

        self.db_path = self.root / "jobs.sqlite3"
        self.conn = shared_db.init_db(str(self.db_path))
        self.addCleanup(self.conn.close)
        self.addCleanup(self.tmpdir.cleanup)

        self.vault_root = self.root / "vault"
        (self.vault_root / ".obsidian").mkdir(parents=True)
        self.semester_root = self.vault_root / "01_current"
        self.course_root = self.semester_root / "cs201"
        self.summary_dir = self.course_root / "06_lecture_notes" / "01_summarize"
        self.origin_dir = self.course_root / "06_lecture_notes" / "02_origin"
        self.summary_dir.mkdir(parents=True)
        self.origin_dir.mkdir(parents=True)

        self.timetable_db_path = self.root / "storage-v2.sqlite3"
        sqlite3.connect(str(self.timetable_db_path)).close()

        self.active_path = self._write_active_snapshot()

    def _write_active_snapshot(self, *, activated_at: str = "2026-01-01T10:00:00+09:00") -> Path:
        payload = {
            "schema_version": "lecture-stt/active-semester@2",
            "activated_at": activated_at,
            "activation_plan_sha256": "0" * 64,
            "manifest_source_path": str(self.root / "semester.yaml"),
            "manifest_source_sha256": "0" * 64,
            "expected_course_count": 1,
            "semester": "2026-1",
            "vault_root": str(self.vault_root),
            "semester_root": str(self.semester_root),
            "timetable_db_path": str(self.timetable_db_path),
            "origin_subdir": "06_lecture_notes/02_origin",
            "summary_subdir": "06_lecture_notes/01_summarize",
            "match_margin_minutes": 30,
            "courses": [
                {
                    "course_code": "CS201",
                    "course_name": "Data Structures",
                    "course_dir": "cs201",
                    "aliases": ["CA_2"],
                }
            ],
            "selected_timetable_courses": [
                {"course_code": "CS201", "course_name": "Data Structures"},
            ],
            "timetable_selection_plan_sha256": "0" * 64,
        }
        (self.root / "semester.yaml").write_text(
            "course: data",
            encoding="utf-8",
        )
        active_path = self.root / "active-semester.json"
        active_path.write_text(json.dumps(payload), encoding="utf-8")
        return active_path

    def _run_summary(self, *extra_args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = transcript_status.main(
                [
                    "--db-path",
                    str(self.db_path),
                    "--active-path",
                    str(self.active_path),
                    "summary",
                    *extra_args,
                ]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def _insert_transcript_postprocess_row(self, *, status: str) -> None:
        shared_db.insert_transcript_postprocess_job_if_absent(
            self.conn,
            260101,
            logical_stem="260101CA_2",
            semester="2026-1",
            activation_cutoff="2026-01-01T10:00:00+09:00",
            route_method="filename_alias",
            status=status,
            source_txt_path=str(self.root / "260101CA_2.txt"),
            source_txt_sha256="x" * 64,
            source_json_path=str(self.root / "260101CA_2.json"),
            source_json_sha256="x" * 64,
            generator_backend="fake",
        )

    def _insert_done_jobs_row(self, *, job_id: int, completed_at: str) -> None:
        self.conn.execute(
            """
            INSERT INTO jobs (
                id,
                status,
                created_at,
                updated_at,
                orig_inbox_path,
                orig_name,
                canonical_base,
                canonical_audio_path,
                ended_at,
                engine_params
            ) VALUES (?, 'DONE', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                completed_at,
                completed_at,
                str(self.root / f"{job_id}.m4a"),
                f"{job_id}.m4a",
                str(job_id),
                str(self.root / f"{job_id}.wav"),
                completed_at,
                json.dumps({"timings": {"ended_at": completed_at}}, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def _insert_invalid_done_job(self, *, job_id: int) -> None:
        self.conn.execute(
            """
            INSERT INTO jobs (
                id,
                status,
                created_at,
                updated_at,
                orig_inbox_path,
                orig_name,
                canonical_base,
                canonical_audio_path,
                ended_at,
                engine_params
            ) VALUES (?, 'DONE', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                "invalid-timestamp",
                "invalid-timestamp",
                str(self.root / f"{job_id}.m4a"),
                f"{job_id}.m4a",
                str(job_id),
                str(self.root / f"{job_id}.wav"),
                "invalid-timestamp",
                "{}",
            ),
        )
        self.conn.commit()

    def test_summary_status_healthy(self) -> None:
        self._insert_transcript_postprocess_row(status="DELIVERED")

        code, stdout, stderr = self._run_summary("--limit", "10")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("overall_status: HEALTHY", stdout)
        self.assertIn("pending_postprocess_jobs: 0", stdout)
        self.assertIn("active_stt_jobs: 0", stdout)
        self.assertIn("total_rows: 1", stdout)

    def test_summary_status_busy_with_pending_postprocess(self) -> None:
        self._insert_transcript_postprocess_row(status="PENDING")

        code, stdout, stderr = self._run_summary("--limit", "10")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("overall_status: BUSY", stdout)
        self.assertIn("pending_postprocess_jobs: 1", stdout)
        self.assertIn("stt_problem_jobs: 0", stdout)

    def test_summary_status_attention_when_problem_rows(self) -> None:
        self._insert_transcript_postprocess_row(status="ERROR")

        code, stdout, stderr = self._run_summary("--limit", "10")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("overall_status: ATTENTION", stdout)
        self.assertIn("postprocess_problem_jobs: 1", stdout)
        self.assertIn("Recent problems", stdout)

    def test_summary_attention_when_unqueued_done_after_cutoff(self) -> None:
        self._insert_transcript_postprocess_row(status="DELIVERED")
        self._insert_done_jobs_row(job_id=260102, completed_at="2026-01-01T10:10:00+09:00")

        code, stdout, stderr = self._run_summary("--limit", "10")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("overall_status: ATTENTION", stdout)
        self.assertIn("unqueued_done_jobs: 1", stdout)
        self.assertIn("total_rows: 1", stdout)

    def test_summary_attention_when_invalid_unqueued_done_jobs(self) -> None:
        self._insert_invalid_done_job(job_id=260103)

        code, stdout, stderr = self._run_summary("--limit", "10")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("overall_status: ATTENTION", stdout)
        self.assertIn("unqueued_done_jobs: 0", stdout)
        self.assertIn("invalid_unqueued_done_jobs: 1", stdout)
        self.assertIn("total_rows: 0", stdout)

    def test_summary_is_read_only_and_does_not_mutate_db(self) -> None:
        before_rows = self.conn.execute(
            "SELECT COUNT(*) AS total FROM transcript_postprocess_jobs"
        ).fetchone()["total"]

        code, _, _ = self._run_summary("--limit", "10")

        after_rows = self.conn.execute(
            "SELECT COUNT(*) AS total FROM transcript_postprocess_jobs"
        ).fetchone()["total"]
        self.assertEqual(code, 0)
        self.assertEqual(before_rows, after_rows)


if __name__ == "__main__":
    unittest.main()
