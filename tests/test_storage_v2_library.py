from __future__ import annotations

import hashlib
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
    RecordingLibraryConflictError,
    RecordingLibraryNotFoundError,
    RecordingLibraryUnavailableError,
    disabled_recording_library_list,
    list_recordings,
    read_recording_detail,
    read_recording_transcript_preview,
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
        path_rel: str | None = None,
        content_sha256: str = "a" * 64,
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
                path_rel
                or f"artifacts/{job_id}/{artifact_kind}-{revision}.txt",
                content_sha256,
                bytes_count,
                mime_type,
                int(is_latest),
                archived_at,
            ),
        )
        return int(cursor.lastrowid)

    def _write_record_file(
        self,
        storage_key: str,
        relative_path: str,
        payload: str,
    ) -> tuple[Path, int, str]:
        return self._write_record_bytes(
            storage_key,
            relative_path,
            payload.encode("utf-8"),
        )

    def _write_record_bytes(
        self,
        storage_key: str,
        relative_path: str,
        payload: bytes,
    ) -> tuple[Path, int, str]:
        records_root = self.root / "records"
        target = records_root / storage_key / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return records_root, len(payload), hashlib.sha256(payload).hexdigest()

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
        self.assertEqual(
            payload["capabilities"],
            {"transcript_preview": False},
        )
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
        self.assertEqual(
            payload["capabilities"],
            {"transcript_preview": False},
        )
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

    def test_transcript_preview_returns_public_text_payload(self) -> None:
        recording_id = self._insert_recording(
            "preview_recording",
            original_name_nfc="미리보기.m4a",
        )
        self._insert_title(recording_id, "자료구조 1강")
        job_id = self._insert_job(
            recording_id,
            "job_preview",
            status="done",
            progress=100,
        )
        transcript_text = unicodedata.normalize(
            "NFD",
            "첫 줄입니다.\n강의 노트",
        )
        path_rel = "jobs/job_preview/transcript.txt"
        records_root, bytes_count, digest = self._write_record_file(
            "preview_recording",
            path_rel,
            transcript_text,
        )
        self._insert_artifact(
            recording_id,
            job_id,
            "transcript_raw_text",
            1,
            path_rel=path_rel,
            content_sha256=digest,
            bytes_count=bytes_count,
            mime_type=(
                "text/plain; charset=utf-8; "
                "provenance=historical-transcript-recovery"
            ),
        )
        self.conn.commit()

        payload = read_recording_transcript_preview(
            self.db_path,
            records_root,
            "preview_recording",
        )

        self.assertEqual(
            payload["schema_version"],
            "storage-v2/transcript-preview@1",
        )
        self.assertTrue(payload["available"])
        self.assertEqual(
            payload["recording"],
            {
                "storage_key": "preview_recording",
                "display_name": "자료구조 1강",
            },
        )
        self.assertEqual(payload["transcript"]["job_key"], "job_preview")
        self.assertEqual(payload["transcript"]["revision"], 1)
        self.assertEqual(payload["transcript"]["bytes"], bytes_count)
        self.assertEqual(
            payload["transcript"]["characters"],
            len(transcript_text),
        )
        self.assertEqual(payload["transcript"]["text"], transcript_text)
        self.assertEqual(
            payload["transcript"]["text"].encode("utf-8"),
            transcript_text.encode("utf-8"),
        )
        encoded = str(payload)
        self.assertNotIn("path_rel", encoded)
        self.assertNotIn("content_sha256", encoded)
        self.assertNotIn("recording_id", encoded)

    def test_transcript_preview_rejects_ineligible_or_mismatched_transcript(self) -> None:
        recording_id = self._insert_recording("preview_missing")
        job_id = self._insert_job(
            recording_id,
            "job_processing",
            status="processing",
            progress=42,
        )
        self._insert_artifact(
            recording_id,
            job_id,
            "transcript_raw_text",
            1,
        )
        self.conn.commit()
        (self.root / "records").mkdir(parents=True, exist_ok=True)

        with self.assertRaises(RecordingLibraryNotFoundError):
            read_recording_transcript_preview(
                self.db_path,
                self.root / "records",
                "preview_missing",
            )

        recording_ok_id = self._insert_recording("preview_conflict")
        job_ok_id = self._insert_job(recording_ok_id, "job_done")
        path_rel = "jobs/job_done/transcript.txt"
        records_root, bytes_count, _digest = self._write_record_file(
            "preview_conflict",
            path_rel,
            "본문은 바뀌었습니다.",
        )
        self._insert_artifact(
            recording_ok_id,
            job_ok_id,
            "transcript_raw_text",
            1,
            path_rel=path_rel,
            content_sha256="b" * 64,
            bytes_count=bytes_count,
        )
        self.conn.commit()

        with self.assertRaises(RecordingLibraryConflictError):
            read_recording_transcript_preview(
                self.db_path,
                records_root,
                "preview_conflict",
            )

    def test_transcript_preview_fails_closed_when_db_is_inside_records_root(self) -> None:
        nested_root = self.root / "nested-records"
        nested_root.mkdir(parents=True, exist_ok=True)
        nested_db_path = nested_root / "storage-v2.sqlite3"
        nested_conn = apply_migration(nested_db_path)
        try:
            recording_id = int(
                nested_conn.execute(
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
                        received_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "preview_nested",
                        "preview_nested.m4a",
                        "preview_nested.m4a",
                        "source/preview_nested.m4a",
                        "available",
                        1,
                        "audio/mp4",
                        "2026-07-23T09:00:00+09:00",
                        "2026-07-23T09:01:00+09:00",
                    ),
                ).lastrowid
            )
            job_id = int(
                nested_conn.execute(
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
                        finished_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        recording_id,
                        "job_nested",
                        "jobs/job_nested",
                        "lecture",
                        "2026-07-23.1",
                        "done",
                        100,
                        1,
                        "2026-07-23T09:02:00+09:00",
                        "2026-07-23T09:03:00+09:00",
                        "2026-07-23T09:04:00+09:00",
                    ),
                ).lastrowid
            )
            transcript_text = "안전 경계 확인"
            path_rel = "jobs/job_nested/transcript.txt"
            target = nested_root / "preview_nested" / path_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            encoded = transcript_text.encode("utf-8")
            target.write_bytes(encoded)
            nested_conn.execute(
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
                    is_latest
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recording_id,
                    job_id,
                    "transcript_raw_text",
                    1,
                    path_rel,
                    hashlib.sha256(encoded).hexdigest(),
                    len(encoded),
                    "text/plain",
                    1,
                ),
            )
            nested_conn.commit()

            with self.assertRaises(RecordingLibraryUnavailableError):
                read_recording_transcript_preview(
                    nested_db_path,
                    nested_root,
                    "preview_nested",
                )
        finally:
            nested_conn.close()

    def test_transcript_preview_rejects_oversize_payload(self) -> None:
        recording_id = self._insert_recording("preview_oversize")
        job_id = self._insert_job(recording_id, "job_oversize")
        path_rel = "jobs/job_oversize/transcript.txt"
        records_root, bytes_count, digest = self._write_record_file(
            "preview_oversize",
            path_rel,
            "1234567890",
        )
        self._insert_artifact(
            recording_id,
            job_id,
            "transcript_raw_text",
            1,
            path_rel=path_rel,
            content_sha256=digest,
            bytes_count=bytes_count,
        )
        self.conn.commit()

        with self.assertRaises(RecordingLibraryConflictError):
            read_recording_transcript_preview(
                self.db_path,
                records_root,
                "preview_oversize",
                max_bytes=4,
            )

    def test_transcript_preview_rejects_symlink_and_hardlink_artifacts(self) -> None:
        symlink_recording_id = self._insert_recording("preview_symlink")
        symlink_job_id = self._insert_job(symlink_recording_id, "job_symlink")
        source_rel = "jobs/job_symlink/transcript-source.txt"
        records_root, bytes_count, digest = self._write_record_file(
            "preview_symlink",
            source_rel,
            "symlink target",
        )
        symlink_target = records_root / "preview_symlink" / source_rel
        path_rel = "jobs/job_symlink/transcript.txt"
        symlink_path = records_root / "preview_symlink" / path_rel
        symlink_path.symlink_to(symlink_target)
        self._insert_artifact(
            symlink_recording_id,
            symlink_job_id,
            "transcript_raw_text",
            1,
            path_rel=path_rel,
            content_sha256=digest,
            bytes_count=bytes_count,
        )
        self.conn.commit()

        with self.assertRaises(RecordingLibraryUnavailableError):
            read_recording_transcript_preview(
                self.db_path,
                records_root,
                "preview_symlink",
            )

        hardlink_recording_id = self._insert_recording("preview_hardlink")
        hardlink_job_id = self._insert_job(hardlink_recording_id, "job_hardlink")
        source_rel = "jobs/job_hardlink/transcript-source.txt"
        records_root, bytes_count, digest = self._write_record_file(
            "preview_hardlink",
            source_rel,
            "hardlink target",
        )
        source_path = records_root / "preview_hardlink" / source_rel
        hardlink_rel = "jobs/job_hardlink/transcript.txt"
        hardlink_path = records_root / "preview_hardlink" / hardlink_rel
        hardlink_path.parent.mkdir(parents=True, exist_ok=True)
        hardlink_path.hardlink_to(source_path)
        self._insert_artifact(
            hardlink_recording_id,
            hardlink_job_id,
            "transcript_raw_text",
            1,
            path_rel=hardlink_rel,
            content_sha256=digest,
            bytes_count=bytes_count,
        )
        self.conn.commit()

        with self.assertRaises(RecordingLibraryUnavailableError):
            read_recording_transcript_preview(
                self.db_path,
                records_root,
                "preview_hardlink",
            )

    def test_transcript_preview_rejects_invalid_utf8_and_nul(self) -> None:
        invalid_recording_id = self._insert_recording("preview_invalid_utf8")
        invalid_job_id = self._insert_job(invalid_recording_id, "job_invalid_utf8")
        invalid_rel = "jobs/job_invalid_utf8/transcript.txt"
        records_root, bytes_count, digest = self._write_record_bytes(
            "preview_invalid_utf8",
            invalid_rel,
            b"\xff\xfe\xfd",
        )
        self._insert_artifact(
            invalid_recording_id,
            invalid_job_id,
            "transcript_raw_text",
            1,
            path_rel=invalid_rel,
            content_sha256=digest,
            bytes_count=bytes_count,
        )
        self.conn.commit()

        with self.assertRaises(RecordingLibraryConflictError):
            read_recording_transcript_preview(
                self.db_path,
                records_root,
                "preview_invalid_utf8",
            )

        nul_recording_id = self._insert_recording("preview_nul")
        nul_job_id = self._insert_job(nul_recording_id, "job_nul")
        nul_rel = "jobs/job_nul/transcript.txt"
        records_root, bytes_count, digest = self._write_record_bytes(
            "preview_nul",
            nul_rel,
            b"line1\x00line2",
        )
        self._insert_artifact(
            nul_recording_id,
            nul_job_id,
            "transcript_raw_text",
            1,
            path_rel=nul_rel,
            content_sha256=digest,
            bytes_count=bytes_count,
        )
        self.conn.commit()

        with self.assertRaises(RecordingLibraryConflictError):
            read_recording_transcript_preview(
                self.db_path,
                records_root,
                "preview_nul",
            )

    def test_transcript_preview_rejects_noncanonical_path_and_mime(self) -> None:
        path_recording_id = self._insert_recording("preview_path_shape")
        path_job_id = self._insert_job(path_recording_id, "job_path_shape")
        path_rel = "jobs/job_path_shape/transcript-copy.txt"
        records_root, bytes_count, digest = self._write_record_file(
            "preview_path_shape",
            path_rel,
            "canonical path required",
        )
        self._insert_artifact(
            path_recording_id,
            path_job_id,
            "transcript_raw_text",
            1,
            path_rel=path_rel,
            content_sha256=digest,
            bytes_count=bytes_count,
            mime_type="text/plain",
        )
        self.conn.commit()

        with self.assertRaises(RecordingLibraryConflictError):
            read_recording_transcript_preview(
                self.db_path,
                records_root,
                "preview_path_shape",
            )

        mime_recording_id = self._insert_recording("preview_mime_shape")
        mime_job_id = self._insert_job(mime_recording_id, "job_mime_shape")
        mime_rel = "jobs/job_mime_shape/transcript.txt"
        records_root, bytes_count, digest = self._write_record_file(
            "preview_mime_shape",
            mime_rel,
            "canonical mime required",
        )
        self._insert_artifact(
            mime_recording_id,
            mime_job_id,
            "transcript_raw_text",
            1,
            path_rel=mime_rel,
            content_sha256=digest,
            bytes_count=bytes_count,
            mime_type="application/json",
        )
        self.conn.commit()

        with self.assertRaises(RecordingLibraryConflictError):
            read_recording_transcript_preview(
                self.db_path,
                records_root,
                "preview_mime_shape",
            )

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
