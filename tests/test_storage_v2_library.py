from __future__ import annotations

import tempfile
import unicodedata
import unittest
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.storage_v2.library import (
    RecordingLibraryNotFoundError,
    disabled_recording_library_list,
    list_recordings,
    read_recording_detail,
)
from lecture_stt.storage_v2.repository import apply_migration


class StorageV2LibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "storage-v2.sqlite3"
        self.conn = apply_migration(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _insert_recording(
        self,
        storage_key: str,
        *,
        original_name_nfc: str | None = None,
        recorded_at: str = "2026-07-23T09:00:00+09:00",
        received_at: str = "2026-07-23T09:01:00+09:00",
        source_state: str = "available",
        ingest_bytes: int | None = 1024,
        source_mime: str | None = "audio/mp4",
        archived_at: str | None = None,
    ) -> int:
        file_name = original_name_nfc or unicodedata.normalize(
            "NFC",
            f"{storage_key}.m4a",
        )
        cursor = self.conn.execute(
            """
            INSERT INTO recordings(
                storage_key,
                original_name_raw,
                original_name_nfc,
                source_relpath,
                source_state,
                ingest_bytes,
                source_mime,
                recorded_at,
                received_at,
                archived_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                storage_key,
                file_name,
                file_name,
                f"source/{storage_key}.m4a",
                source_state,
                ingest_bytes,
                source_mime,
                recorded_at,
                received_at,
                archived_at,
            ),
        )
        return int(cursor.lastrowid)

    def _insert_job(
        self,
        recording_id: int,
        job_key: str,
        *,
        status: str = "done",
        progress: int = 100,
        is_current: bool = True,
        requested_profile: str | None = "lecture",
        requested_profile_version: str | None = "2026-07-23.1",
        queued_at: str = "2026-07-23T09:02:00+09:00",
        started_at: str | None = "2026-07-23T09:03:00+09:00",
        finished_at: str | None = "2026-07-23T09:10:00+09:00",
        archived_at: str | None = None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO transcription_jobs(
                recording_id,
                job_key,
                job_relpath,
                requested_profile,
                requested_profile_version,
                status,
                progress,
                is_current,
                queued_at,
                started_at,
                finished_at,
                archived_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recording_id,
                job_key,
                f"jobs/{job_key}",
                requested_profile,
                requested_profile_version,
                status,
                progress,
                int(is_current),
                queued_at,
                started_at,
                finished_at,
                archived_at,
            ),
        )
        return int(cursor.lastrowid)

    def _insert_title(
        self,
        recording_id: int,
        title: str,
        *,
        source: str = "manual",
        is_current: bool = True,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO recording_titles(
                recording_id,
                title,
                title_source,
                is_current
            )
            VALUES (?, ?, ?, ?)
            """,
            (recording_id, title, source, int(is_current)),
        )

    def _insert_context(
        self,
        recording_id: int,
        *,
        context_type: str = "class_session",
        label: str = "자료구조",
        semester: str = "2026-2",
        course_name: str = "자료구조",
        course_code: str = "CS202",
        session_date: str = "2026-07-23",
        period_label: str = "3교시",
        period_index: int = 3,
        source: str = "schedule_import",
        is_selected: bool = True,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO recording_contexts(
                recording_id,
                context_type,
                label,
                semester,
                course_name,
                course_code,
                session_date,
                period_label,
                period_index,
                source,
                is_selected
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recording_id,
                context_type,
                label,
                semester,
                course_name,
                course_code,
                session_date,
                period_label,
                period_index,
                source,
                int(is_selected),
            ),
        )

    def _insert_artifact(
        self,
        recording_id: int,
        job_id: int,
        artifact_kind: str,
        revision: int,
        *,
        bytes_count: int | None = 512,
        mime_type: str | None = "text/plain",
        is_latest: bool = True,
        archived_at: str | None = None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO artifacts(
                recording_id,
                job_id,
                artifact_kind,
                revision,
                path_rel,
                content_sha256,
                bytes,
                mime_type,
                is_latest,
                archived_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recording_id,
                job_id,
                artifact_kind,
                revision,
                f"artifacts/{job_id}/{artifact_kind}-{revision}.txt",
                "a" * 64,
                bytes_count,
                mime_type,
                int(is_latest),
                archived_at,
            ),
        )
        return int(cursor.lastrowid)

    def _insert_review(
        self,
        recording_id: int,
        *,
        job_id: int | None = None,
        artifact_id: int | None = None,
        status: str = "open",
        severity: str | None = "medium",
        reason_code: str = "quality_low",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO review_items(
                recording_id,
                job_id,
                artifact_id,
                status,
                severity,
                reason_code
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (recording_id, job_id, artifact_id, status, severity, reason_code),
        )

    def test_disabled_payload_is_fail_closed(self) -> None:
        payload = disabled_recording_library_list(limit=50, offset=0)

        self.assertFalse(payload["available"])
        self.assertEqual(
            payload["schema_version"],
            "storage-v2/recording-library-list@1",
        )
        self.assertEqual(payload["disabled_reason"], "recording_library_disabled")
        self.assertEqual(payload["filters"], {"limit": 50, "offset": 0})
        self.assertEqual(payload["counts"]["recordings"], 0)
        self.assertEqual(payload["summaries"], [])

    def test_list_returns_metadata_only_recording_summaries(self) -> None:
        archived_recording_id = self._insert_recording(
            "archived_recording",
            archived_at="2026-07-23T00:00:00+09:00",
        )
        archived_job_id = self._insert_job(
            archived_recording_id,
            "job_archived",
        )
        self._insert_review(archived_recording_id, job_id=archived_job_id)

        recording_id = self._insert_recording(
            "lecture_recording",
            original_name_nfc="강의.m4a",
        )
        self._insert_title(recording_id, "알고리즘 3교시")
        self._insert_context(recording_id)
        job_id = self._insert_job(recording_id, "job_current", status="done")
        self._insert_artifact(recording_id, job_id, "summary_markdown", 1)
        self._insert_artifact(
            recording_id,
            job_id,
            "metadata",
            1,
            archived_at="2026-07-23T10:00:00+09:00",
        )
        self._insert_review(
            recording_id,
            job_id=job_id,
            status="open",
            reason_code="quality_low",
        )
        self.conn.commit()

        payload = list_recordings(self.db_path, limit=50, offset=0)

        self.assertTrue(payload["available"])
        self.assertEqual(payload["counts"]["recordings"], 1)
        self.assertEqual(payload["counts"]["done"], 1)
        self.assertEqual(payload["counts"]["open_reviews"], 1)
        self.assertEqual(payload["total"], 1)
        summary = payload["summaries"][0]
        self.assertEqual(summary["storage_key"], "lecture_recording")
        self.assertEqual(summary["display_name"], "알고리즘 3교시")
        self.assertEqual(summary["original_name_nfc"], "강의.m4a")
        self.assertEqual(summary["source_state"], "available")
        self.assertEqual(summary["current_title"]["source"], "manual")
        self.assertEqual(summary["selected_context"]["course_name"], "자료구조")
        self.assertEqual(summary["current_job"]["job_key"], "job_current")
        self.assertEqual(summary["artifact_count"], 1)
        self.assertEqual(summary["open_review_count"], 1)
        self.assertNotIn("id", summary)
        self.assertNotIn("content_sha256", summary)
        self.assertNotIn("source_relpath", summary)

    def test_detail_returns_nested_metadata_only_payload(self) -> None:
        recording_id = self._insert_recording("detail_recording")
        self._insert_context(recording_id, context_type="meeting", source="manual")
        job_old_id = self._insert_job(
            recording_id,
            "job_old",
            status="needs_review",
            progress=87,
            is_current=False,
            requested_profile="meeting",
            finished_at=None,
        )
        job_current_id = self._insert_job(
            recording_id,
            "job_current",
            status="done",
            progress=100,
            is_current=True,
        )
        transcript_id = self._insert_artifact(
            recording_id,
            job_current_id,
            "transcript_raw_text",
            1,
        )
        self._insert_artifact(
            recording_id,
            job_current_id,
            "summary_markdown",
            2,
            mime_type="text/markdown",
        )
        self._insert_review(
            recording_id,
            job_id=job_current_id,
            artifact_id=transcript_id,
            status="triaged",
            severity="high",
            reason_code="needs_correction",
        )
        self._insert_review(
            recording_id,
            job_id=job_old_id,
            status="resolved",
            severity="low",
            reason_code="legacy_issue",
        )
        archived_job_id = self._insert_job(
            recording_id,
            "job_archived",
            status="canceled",
            is_current=False,
            archived_at="2026-07-23T11:00:00+09:00",
        )
        archived_artifact_id = self._insert_artifact(
            recording_id,
            archived_job_id,
            "metadata",
            1,
        )
        self._insert_review(
            recording_id,
            job_id=archived_job_id,
            artifact_id=archived_artifact_id,
            status="dismissed",
            severity="medium",
            reason_code="archived_hidden",
        )
        private_value = "PRIVATE_TRANSCRIPT_BODY_SENTINEL"
        self.conn.execute(
            """
            UPDATE transcription_jobs
            SET config_json = ?, error_message = ?
            WHERE id = ?
            """,
            ('{"private":"PRIVATE_TRANSCRIPT_BODY_SENTINEL"}', private_value, job_current_id),
        )
        self.conn.execute(
            """
            UPDATE review_items
            SET detail_json = ?
            WHERE recording_id = ? AND reason_code = 'needs_correction'
            """,
            ('{"body":"PRIVATE_TRANSCRIPT_BODY_SENTINEL"}', recording_id),
        )
        self.conn.execute(
            """
            UPDATE artifacts
            SET path_rel = ?, content_sha256 = ?
            WHERE id = ?
            """,
            (
                "private/PRIVATE_TRANSCRIPT_BODY_SENTINEL.txt",
                "b" * 64,
                transcript_id,
            ),
        )
        self.conn.commit()

        payload = read_recording_detail(self.db_path, "detail_recording")

        self.assertTrue(payload["available"])
        self.assertEqual(payload["recording"]["storage_key"], "detail_recording")
        self.assertEqual(payload["recording"]["source"]["mime_type"], "audio/mp4")
        self.assertEqual(payload["counts"]["jobs"], 2)
        self.assertEqual(payload["counts"]["artifacts"], 2)
        self.assertEqual(payload["counts"]["reviews"], 3)
        self.assertEqual(payload["counts"]["open_reviews"], 1)
        self.assertFalse(payload["limits"]["jobs_truncated"])
        self.assertEqual(len(payload["jobs"]), 2)
        current_job = payload["jobs"][0]
        self.assertEqual(current_job["job_key"], "job_current")
        self.assertTrue(current_job["is_current"])
        self.assertEqual(current_job["artifacts"][0]["stage"], "transcript")
        self.assertEqual(current_job["artifacts"][1]["stage"], "summary")
        review = {
            item["reason_code"]: item for item in payload["reviews"]
        }["needs_correction"]
        self.assertEqual(review["job_key"], "job_current")
        self.assertEqual(review["artifact_kind"], "transcript_raw_text")
        self.assertEqual(review["artifact_revision"], 1)
        archived_review = {
            item["reason_code"]: item for item in payload["reviews"]
        }["archived_hidden"]
        self.assertIsNone(archived_review["job_key"])
        self.assertIsNone(archived_review["artifact_kind"])
        self.assertIsNone(archived_review["artifact_revision"])
        encoded = str(payload)
        self.assertNotIn("content_sha256", encoded)
        self.assertNotIn("path_rel", encoded)
        self.assertNotIn("error_message", encoded)
        self.assertNotIn("config_json", encoded)
        self.assertNotIn(private_value, encoded)
        self.assertNotIn("b" * 64, encoded)

    def test_detail_enforces_bounds_and_missing_recording_errors(self) -> None:
        recording_id = self._insert_recording("bounded_recording")
        for index in range(52):
            job_id = self._insert_job(
                recording_id,
                f"job_{index}",
                status="queued" if index % 2 == 0 else "processing",
                progress=index % 100,
                is_current=index == 51,
                queued_at=f"2026-07-23T09:{index % 60:02d}:00+09:00",
                started_at=None,
                finished_at=None,
            )
            self._insert_artifact(
                recording_id,
                job_id,
                "metadata",
                1,
            )
        for index in range(101):
            self._insert_review(
                recording_id,
                status="open" if index % 2 == 0 else "triaged",
                severity="medium",
                reason_code=f"review_{index}",
            )
        self.conn.commit()

        payload = read_recording_detail(self.db_path, "bounded_recording")

        self.assertEqual(len(payload["jobs"]), 50)
        self.assertTrue(payload["limits"]["jobs_truncated"])
        self.assertTrue(payload["limits"]["artifacts_truncated"])
        self.assertEqual(len(payload["reviews"]), 100)
        self.assertTrue(payload["limits"]["reviews_truncated"])
        with self.assertRaisesRegex(ValueError, "storage_key"):
            read_recording_detail(self.db_path, "bad/key")
        with self.assertRaises(RecordingLibraryNotFoundError):
            read_recording_detail(self.db_path, "missing_recording")

    def test_detail_does_not_claim_artifact_truncation_for_empty_omitted_jobs(
        self,
    ) -> None:
        recording_id = self._insert_recording("jobs_without_artifacts")
        for index in range(51):
            self._insert_job(
                recording_id,
                f"empty_job_{index}",
                status="queued",
                progress=0,
                is_current=index == 50,
                queued_at=f"2026-07-23T10:{index % 60:02d}:00+09:00",
                started_at=None,
                finished_at=None,
            )
        self.conn.commit()

        payload = read_recording_detail(
            self.db_path,
            "jobs_without_artifacts",
        )

        self.assertTrue(payload["limits"]["jobs_truncated"])
        self.assertEqual(payload["counts"]["artifacts"], 0)
        self.assertFalse(payload["limits"]["artifacts_truncated"])

    def test_detail_artifact_limit_preserves_current_job_priority(self) -> None:
        recording_id = self._insert_recording("artifact_priority")
        older_job_id = self._insert_job(
            recording_id,
            "job_older",
            is_current=False,
            queued_at="2026-07-23T08:00:00+09:00",
        )
        current_job_id = self._insert_job(
            recording_id,
            "job_current_priority",
            is_current=True,
            queued_at="2026-07-23T09:00:00+09:00",
        )
        for revision in range(1, 502):
            self._insert_artifact(
                recording_id,
                older_job_id,
                "source_copy",
                revision,
                is_latest=revision == 501,
            )
        self._insert_artifact(
            recording_id,
            current_job_id,
            "summary_markdown",
            1,
        )
        self.conn.commit()

        payload = read_recording_detail(
            self.db_path,
            "artifact_priority",
        )

        self.assertEqual(payload["counts"]["artifacts"], 502)
        self.assertTrue(payload["limits"]["artifacts_truncated"])
        self.assertEqual(payload["jobs"][0]["job_key"], "job_current_priority")
        self.assertEqual(
            [
                artifact["artifact_kind"]
                for artifact in payload["jobs"][0]["artifacts"]
            ],
            ["summary_markdown"],
        )
        self.assertEqual(
            sum(len(job["artifacts"]) for job in payload["jobs"]),
            500,
        )
