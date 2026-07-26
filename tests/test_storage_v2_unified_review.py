from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.storage_v2.repository import apply_migration  # noqa: E402
from lecture_stt.storage_v2.unified_review import (  # noqa: E402
    read_unified_review_feed,
)


class UnifiedReviewFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "storage-v2.sqlite3"
        self.conn = apply_migration(self.db_path)
        self._seed()

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _insert_recording(
        self,
        storage_key: str,
        *,
        title: str | None = None,
        recorded_at: str,
        archived_at: str | None = None,
    ) -> int:
        original_name = unicodedata.normalize("NFD", f"{storage_key} 녹음")
        cursor = self.conn.execute(
            """
            INSERT INTO recordings(
                storage_key,
                original_name_raw,
                original_name_nfc,
                source_relpath,
                source_state,
                recorded_at,
                received_at,
                archived_at,
                created_at
            )
            VALUES (?, ?, ?, ?, 'available', ?, ?, ?, ?)
            """,
            (
                storage_key,
                original_name,
                original_name,
                f"private/source/{storage_key}.m4a",
                recorded_at,
                recorded_at,
                archived_at,
                recorded_at,
            ),
        )
        recording_id = int(cursor.lastrowid)
        if title is not None:
            self.conn.execute(
                """
                INSERT INTO recording_titles(
                    recording_id,
                    title,
                    title_source,
                    is_current,
                    created_at
                )
                VALUES (?, ?, 'manual', 1, ?)
                """,
                (recording_id, title, recorded_at),
            )
        return recording_id

    def _insert_review(
        self,
        recording_id: int,
        *,
        status: str = "open",
        created_at: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO review_items(
                recording_id,
                status,
                severity,
                reason_code,
                detail_json,
                created_at
            )
            VALUES (?, ?, 'medium', 'quality_low', ?, ?)
            """,
            (
                recording_id,
                status,
                json.dumps(
                    {"body": "PRIVATE_BODY_SENTINEL", "path": str(self.root)},
                    ensure_ascii=False,
                ),
                created_at,
            ),
        )
        return int(cursor.lastrowid)

    def _insert_proposal(
        self,
        recording_id: int,
        review_item_id: int,
        *,
        proposal_id: int | None = None,
        classification_reason: str = "recorded_at_missing",
        proposed_title: str,
        semester: str,
        updated_at: str,
    ) -> int:
        if proposal_id is None:
            cursor = self.conn.execute(
                """
                INSERT INTO recording_classification_proposals(
                    recording_id,
                    review_item_id,
                    status,
                    classification_reason,
                    proposed_title,
                    context_type,
                    semester,
                    detail_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, 'suggested', ?, ?, 'memo', ?, ?, ?, ?)
                """,
                (
                    recording_id,
                    review_item_id,
                    classification_reason,
                    proposed_title,
                    semester,
                    json.dumps(
                        {"source_path": str(self.root), "digest": "a" * 64},
                        ensure_ascii=False,
                    ),
                    updated_at,
                    updated_at,
                ),
            )
            return int(cursor.lastrowid)
        self.conn.execute(
            """
            INSERT INTO recording_classification_proposals(
                id,
                recording_id,
                review_item_id,
                status,
                classification_reason,
                proposed_title,
                context_type,
                semester,
                detail_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 'suggested', ?, ?, 'memo', ?, ?, ?, ?)
            """,
            (
                proposal_id,
                recording_id,
                review_item_id,
                classification_reason,
                proposed_title,
                semester,
                json.dumps(
                    {"source_path": str(self.root), "digest": "b" * 64},
                    ensure_ascii=False,
                ),
                updated_at,
                updated_at,
            ),
        )
        return proposal_id

    def _insert_title_suggestion(
        self,
        recording_id: int,
        review_item_id: int,
        *,
        proposal_id: int | None = None,
        proposed_title: str,
        updated_at: str,
    ) -> int:
        job_id = int(
            self.conn.execute(
                """
                INSERT INTO transcription_jobs(
                    recording_id,
                    job_key,
                    job_relpath,
                    status,
                    progress,
                    is_current
                )
                VALUES (?, ?, ?, 'done', 100, 1)
                """,
                (
                    recording_id,
                    f"title_job_{recording_id}",
                    f"jobs/title_job_{recording_id}",
                ),
            ).lastrowid
        )
        artifact_id = int(
            self.conn.execute(
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
                VALUES (?, ?, 'transcript_raw_text', 1, ?, ?, 128, 'text/plain', 1)
                """,
                (
                    recording_id,
                    job_id,
                    f"jobs/title_job_{recording_id}/transcript.txt",
                    "a" * 64,
                ),
            ).lastrowid
        )
        detail_json = json.dumps(
            {
                "classification_status": None,
                "context_type": "general",
                "generator_version": "deterministic-keywords-v1",
                "schema_version": "storage-v2/title-suggestion-review@1",
                "suggestion_reason": "content_topic",
                "transcript_revision": 1,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if proposal_id is None:
            cursor = self.conn.execute(
                """
                INSERT INTO recording_title_proposals(
                    recording_id,
                    transcript_artifact_id,
                    classification_proposal_id,
                    review_item_id,
                    status,
                    suggestion_reason,
                    proposed_title,
                    confidence,
                    generator_version,
                    detail_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, NULL, ?, 'suggested', 'content_topic', ?, 0.77, ?, ?, ?, ?)
                """,
                (
                    recording_id,
                    artifact_id,
                    review_item_id,
                    proposed_title,
                    "deterministic-keywords-v1",
                    detail_json,
                    updated_at,
                    updated_at,
                ),
            )
            return int(cursor.lastrowid)
        self.conn.execute(
            """
            INSERT INTO recording_title_proposals(
                id,
                recording_id,
                transcript_artifact_id,
                classification_proposal_id,
                review_item_id,
                status,
                suggestion_reason,
                proposed_title,
                confidence,
                generator_version,
                detail_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, NULL, ?, 'suggested', 'content_topic', ?, 0.77, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                recording_id,
                artifact_id,
                review_item_id,
                proposed_title,
                "deterministic-keywords-v1",
                detail_json,
                updated_at,
                updated_at,
            ),
        )
        return proposal_id

    def _insert_archive_case(
        self,
        case_key: str,
        *,
        review_status: str,
        logical_stem: str,
        subject_abbr: str | None,
        updated_at: str,
        classification: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO archive_evidence_cases(
                case_key,
                legacy_delivery_key,
                logical_stem,
                subject_abbr,
                review_status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case_key,
                f"delivery-{case_key}",
                logical_stem,
                subject_abbr,
                review_status,
                updated_at,
                updated_at,
            ),
        )
        case_id = int(
            self.conn.execute(
                "SELECT id FROM archive_evidence_cases WHERE case_key = ?",
                (case_key,),
            ).fetchone()[0]
        )
        self.conn.execute(
            """
            INSERT INTO archive_evidence_captures(
                case_id,
                capture_key,
                reconciliation_classification,
                legacy_database_sha256,
                source_fingerprint,
                plan_sha256,
                manifest_relpath,
                snapshot_json,
                captured_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case_id,
                f"capture-{case_key}",
                classification,
                "1" * 64,
                "2" * 64,
                "3" * 64,
                f"private/captures/{case_key}.json",
                json.dumps(
                    {"body": "PRIVATE_BODY_SENTINEL", "root": str(self.root)},
                    ensure_ascii=False,
                ),
                updated_at,
            ),
        )
        self.conn.execute(
            """
            INSERT INTO archive_evidence_revisions(
                case_id,
                artifact_kind,
                content_sha256,
                bytes,
                mime_type,
                path_rel,
                created_at
            )
            VALUES (?, 'summary_markdown', ?, 123, 'text/markdown', ?, ?)
            """,
            (
                case_id,
                "4" * 64,
                f"private/revisions/{case_key}.md",
                updated_at,
            ),
        )

    def _seed(self) -> None:
        rec_proposal_new = self._insert_recording(
            "rec_proposal_new",
            recorded_at="2026-07-25T09:30:00+09:00",
        )
        rec_remaining = self._insert_recording(
            "rec_remaining",
            title=unicodedata.normalize("NFD", "남은 검토"),
            recorded_at="2026-07-25T09:00:00+09:00",
        )
        rec_far = self._insert_recording(
            "rec_far",
            recorded_at="2026-07-25T07:00:00+09:00",
        )
        rec_duplicate = self._insert_recording(
            "rec_duplicate",
            recorded_at="2026-07-25T08:00:00+09:00",
        )
        rec_archived = self._insert_recording(
            "rec_archived",
            recorded_at="2026-07-25T12:00:00+09:00",
            archived_at="2026-07-25T12:30:00+09:00",
        )

        review_new = self._insert_review(
            rec_proposal_new,
            created_at="2026-07-25T09:20:00+09:00",
        )
        self._insert_review(
            rec_remaining,
            status="triaged",
            created_at="2026-07-25T08:50:00+09:00",
        )
        review_far = self._insert_review(
            rec_far,
            created_at="2026-07-25T06:55:00+09:00",
        )
        review_duplicate = self._insert_review(
            rec_duplicate,
            created_at="2026-07-25T07:55:00+09:00",
        )
        review_archived = self._insert_review(
            rec_archived,
            created_at="2026-07-25T12:10:00+09:00",
        )

        self._insert_proposal(
            rec_proposal_new,
            review_new,
            proposal_id=1,
            proposed_title=unicodedata.normalize("NFD", "새 제안"),
            semester="2026-2",
            updated_at="2026-07-25T09:30:00+09:00",
        )
        self._insert_proposal(
            rec_far,
            review_far,
            proposal_id=2,
            proposed_title="늦은 제안",
            semester="2026-2",
            updated_at="2026-07-25T07:00:00+09:00",
        )
        self._insert_proposal(
            rec_duplicate,
            review_duplicate,
            proposal_id=3,
            proposed_title="중복 제안 A",
            semester="2026-1",
            updated_at="2026-07-25T08:00:00+09:00",
        )
        self._insert_proposal(
            rec_duplicate,
            review_duplicate,
            proposal_id=4,
            proposed_title="중복 제안 B",
            semester="2026-2",
            updated_at="2026-07-25T08:00:00+09:00",
        )
        self._insert_proposal(
            rec_archived,
            review_archived,
            proposal_id=5,
            proposed_title="숨겨진 제안",
            semester="2026-2",
            updated_at="2026-07-25T12:00:00+09:00",
        )

        self._insert_archive_case(
            "case_open",
            review_status="open",
            logical_stem=unicodedata.normalize("NFD", "강의노트"),
            subject_abbr=unicodedata.normalize("NFD", "자료"),
            updated_at="2026-07-25T10:00:00+09:00",
            classification="manual_review",
        )
        self._insert_archive_case(
            "case_triaged",
            review_status="triaged",
            logical_stem="정리본",
            subject_abbr="DS",
            updated_at="2026-07-25T11:00:00+09:00",
            classification="blocked",
        )
        self._insert_archive_case(
            "case_resolved",
            review_status="resolved",
            logical_stem="제외됨",
            subject_abbr="X",
            updated_at="2026-07-25T12:00:00+09:00",
            classification="verified_delivered",
        )
        self.conn.commit()

    def test_full_table_dedup_uses_suggested_review_ids_outside_current_page(self) -> None:
        payload = read_unified_review_feed(
            self.db_path,
            archive_enabled=True,
            timetable_enabled=True,
            title_enabled=False,
            recording_enabled=True,
            limit=2,
            offset=0,
        )

        self.assertTrue(payload["available"])
        self.assertEqual(payload["total"], 7)
        self.assertEqual(
            [item["id"] for item in payload["items"]],
            ["archive:case_open", "timetable:1"],
        )
        recording_source = next(
            source for source in payload["sources"] if source["source"] == "recording"
        )
        self.assertEqual(recording_source["total_count"], 1)
        self.assertTrue(recording_source["truncated"])
        self.assertIn("full-table distinct", recording_source["note"])
        self.assertIn("원본 open review 4건", recording_source["note"])
        self.assertIn("linked suggested review 3건", recording_source["note"])
        self.assertIn("remaining review 1건", recording_source["note"])

    def test_title_source_dedupes_recording_reviews_and_counts_multiple_unlinked_reviews(
        self,
    ) -> None:
        recording_id = self._insert_recording(
            "rec_title",
            recorded_at="2026-07-25T09:45:00+09:00",
        )
        linked_review_id = self._insert_review(
            recording_id,
            created_at="2026-07-25T09:40:00+09:00",
        )
        self._insert_review(
            recording_id,
            created_at="2026-07-25T09:41:00+09:00",
        )
        self._insert_review(
            recording_id,
            status="triaged",
            created_at="2026-07-25T09:42:00+09:00",
        )
        title_proposal_id = self._insert_title_suggestion(
            recording_id,
            linked_review_id,
            proposed_title="제목 제안",
            updated_at="2026-07-25T09:45:00+09:00",
        )
        self.conn.commit()

        payload = read_unified_review_feed(
            self.db_path,
            archive_enabled=True,
            timetable_enabled=True,
            title_enabled=True,
            recording_enabled=True,
            limit=5,
            offset=0,
        )

        self.assertEqual(payload["schema_version"], "storage-v2/unified-review-feed@2")
        self.assertEqual(payload["total"], 9)
        self.assertEqual(
            [item["id"] for item in payload["items"]],
            [
                "archive:case_open",
                f"title:{title_proposal_id}",
                "recording:rec_title",
                "timetable:1",
                "recording:rec_remaining",
            ],
        )
        sources = {source["source"]: source for source in payload["sources"]}
        self.assertEqual(sources["title"]["total_count"], 1)
        self.assertEqual(sources["title"]["visible_count"], 1)
        self.assertEqual(sources["recording"]["total_count"], 2)
        self.assertIn("open review 7건", sources["recording"]["note"])
        self.assertIn(
            "timetable linked suggested review 3건",
            sources["recording"]["note"],
        )
        self.assertIn(
            "title linked suggested review 1건",
            sources["recording"]["note"],
        )
        self.assertIn(
            "combined distinct 4건",
            sources["recording"]["note"],
        )
        self.assertIn("remaining review 3건", sources["recording"]["note"])

    def test_title_source_fails_closed_when_proposal_identity_exceeds_public_integer_limit(
        self,
    ) -> None:
        recording_id = self._insert_recording(
            "rec_title_unsafe",
            recorded_at="2026-07-25T09:45:00+09:00",
        )
        linked_review_id = self._insert_review(
            recording_id,
            created_at="2026-07-25T09:40:00+09:00",
        )
        self._insert_title_suggestion(
            recording_id,
            linked_review_id,
            proposal_id=9_007_199_254_740_992,
            proposed_title="위험한 제목 제안",
            updated_at="2026-07-25T09:45:00+09:00",
        )
        self.conn.commit()

        with self.assertRaisesRegex(
            ValueError,
            "public integer limit",
        ):
            read_unified_review_feed(
                self.db_path,
                archive_enabled=False,
                timetable_enabled=False,
                title_enabled=True,
                recording_enabled=False,
                limit=10,
                offset=0,
            )

    def test_pagination_is_stable_across_groups_timestamps_and_ids(self) -> None:
        first = read_unified_review_feed(
            self.db_path,
            archive_enabled=True,
            timetable_enabled=True,
            title_enabled=False,
            recording_enabled=True,
            limit=3,
            offset=0,
        )
        second = read_unified_review_feed(
            self.db_path,
            archive_enabled=True,
            timetable_enabled=True,
            title_enabled=False,
            recording_enabled=True,
            limit=3,
            offset=3,
        )
        third = read_unified_review_feed(
            self.db_path,
            archive_enabled=True,
            timetable_enabled=True,
            title_enabled=False,
            recording_enabled=True,
            limit=3,
            offset=6,
        )
        beyond = read_unified_review_feed(
            self.db_path,
            archive_enabled=True,
            timetable_enabled=True,
            title_enabled=False,
            recording_enabled=True,
            limit=3,
            offset=99,
        )

        self.assertEqual(
            [item["id"] for item in first["items"]],
            ["archive:case_open", "timetable:1", "recording:rec_remaining"],
        )
        self.assertEqual(
            [item["id"] for item in second["items"]],
            ["timetable:3", "timetable:4", "timetable:2"],
        )
        self.assertEqual(
            [item["id"] for item in third["items"]],
            ["archive:case_triaged"],
        )
        self.assertEqual(beyond["total"], 7)
        self.assertEqual(beyond["items"], [])
        self.assertTrue(all(
            source["visible_count"] == 0
            for source in beyond["sources"]
        ))
        self.assertTrue(all(
            source["truncated"]
            for source in beyond["sources"]
            if source["available"] and source["total_count"]
        ))

    def test_large_proposal_volume_stays_bounded_and_preserves_exact_total(self) -> None:
        for index in range(120):
            recording_id = self._insert_recording(
                f"bulk_{index:03d}",
                recorded_at="2026-07-24T12:00:00+09:00",
            )
            review_id = self._insert_review(
                recording_id,
                created_at="2026-07-24T12:00:00+09:00",
            )
            self._insert_proposal(
                recording_id,
                review_id,
                proposed_title=f"대량 제안 {index:03d}",
                semester=f"2026-{(index % 2) + 1}",
                updated_at="2026-07-24T12:00:00+09:00",
            )
        self.conn.commit()

        payload = read_unified_review_feed(
            self.db_path,
            archive_enabled=True,
            timetable_enabled=True,
            title_enabled=False,
            recording_enabled=True,
            limit=5,
            offset=0,
        )

        self.assertEqual(len(payload["items"]), 5)
        self.assertEqual(payload["total"], 127)
        timetable_source = next(
            source for source in payload["sources"] if source["source"] == "timetable"
        )
        recording_source = next(
            source for source in payload["sources"] if source["source"] == "recording"
        )
        self.assertEqual(timetable_source["total_count"], 124)
        self.assertEqual(recording_source["total_count"], 1)

    def test_disabled_sources_have_separate_ledger_notes(self) -> None:
        payload = read_unified_review_feed(
            self.db_path,
            archive_enabled=False,
            timetable_enabled=True,
            title_enabled=False,
            recording_enabled=False,
            limit=10,
            offset=0,
        )

        self.assertTrue(payload["available"])
        self.assertEqual(payload["total"], 4)
        sources = {source["source"]: source for source in payload["sources"]}
        self.assertEqual(sources["archive"]["note"], "archive_review_disabled")
        self.assertFalse(sources["archive"]["available"])
        self.assertEqual(sources["recording"]["note"], "recording_library_disabled")
        self.assertFalse(sources["recording"]["available"])
        self.assertTrue(sources["timetable"]["available"])
        self.assertEqual(sources["timetable"]["total_count"], 4)

    def test_payload_is_metadata_only_and_normalizes_nfc_titles(self) -> None:
        payload = read_unified_review_feed(
            self.db_path,
            archive_enabled=True,
            timetable_enabled=True,
            title_enabled=False,
            recording_enabled=True,
            limit=20,
            offset=0,
        )

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("PRIVATE_BODY_SENTINEL", serialized)
        self.assertNotIn("review_item_id", serialized)
        self.assertNotIn("recording_id", serialized)
        self.assertNotIn("digest", serialized)
        open_case = next(item for item in payload["items"] if item["id"] == "archive:case_open")
        new_proposal = next(item for item in payload["items"] if item["id"] == "timetable:1")
        recording_item = next(
            item for item in payload["items"] if item["id"] == "recording:rec_remaining"
        )
        self.assertEqual(open_case["title"], unicodedata.normalize("NFC", "자료 · 강의노트"))
        self.assertEqual(new_proposal["title"], unicodedata.normalize("NFC", "새 제안"))
        self.assertEqual(recording_item["title"], unicodedata.normalize("NFC", "남은 검토"))

    def test_missing_database_and_all_disabled_fail_closed(self) -> None:
        missing = self.root / "missing.sqlite3"
        missing_payload = read_unified_review_feed(
            missing,
            archive_enabled=True,
            timetable_enabled=False,
            title_enabled=True,
            recording_enabled=True,
            limit=10,
            offset=0,
        )
        disabled_payload = read_unified_review_feed(
            self.db_path,
            archive_enabled=False,
            timetable_enabled=False,
            title_enabled=False,
            recording_enabled=False,
            limit=10,
            offset=0,
        )

        self.assertFalse(missing_payload["available"])
        self.assertEqual(missing_payload["total"], 0)
        self.assertEqual(
            [source["note"] for source in missing_payload["sources"]],
            [
                "storage_v2_db_unavailable",
                "timetable_api_disabled",
                "storage_v2_db_unavailable",
                "storage_v2_db_unavailable",
            ],
        )
        self.assertFalse(disabled_payload["available"])
        self.assertEqual(
            [source["note"] for source in disabled_payload["sources"]],
            [
                "archive_review_disabled",
                "timetable_api_disabled",
                "title_review_disabled",
                "recording_library_disabled",
            ],
        )

    def test_limit_and_offset_validation_remains_bounded(self) -> None:
        count_only = read_unified_review_feed(
            self.db_path,
            archive_enabled=True,
            timetable_enabled=True,
            title_enabled=False,
            recording_enabled=True,
            limit=0,
            offset=0,
        )
        self.assertEqual(count_only["total"], 7)
        self.assertEqual(count_only["items"], [])
        self.assertTrue(all(
            source["truncated"]
            for source in count_only["sources"]
            if source["available"] and source["total_count"]
        ))

        with self.assertRaisesRegex(ValueError, "limit must be between 0 and 200"):
            read_unified_review_feed(
                self.db_path,
                archive_enabled=True,
                timetable_enabled=True,
                title_enabled=False,
                recording_enabled=True,
                limit=201,
                offset=0,
            )
        with self.assertRaisesRegex(ValueError, "offset must be between 0 and 100000"):
            read_unified_review_feed(
                self.db_path,
                archive_enabled=True,
                timetable_enabled=True,
                title_enabled=False,
                recording_enabled=True,
                limit=10,
                offset=100_001,
            )

    def test_page_timestamp_must_be_valid_iso_text(self) -> None:
        self.conn.execute(
            """
            UPDATE archive_evidence_cases
            SET updated_at = '2026-07-25 10:00:00'
            WHERE case_key = 'case_open'
            """
        )
        self.conn.commit()
        default_sqlite_timestamp = read_unified_review_feed(
            self.db_path,
            archive_enabled=True,
            timetable_enabled=True,
            title_enabled=False,
            recording_enabled=True,
            limit=10,
            offset=0,
        )
        open_case = next(
            item
            for item in default_sqlite_timestamp["items"]
            if item["id"] == "archive:case_open"
        )
        self.assertEqual(open_case["timestamp"], "2026-07-25 10:00:00")

        self.conn.execute(
            """
            UPDATE archive_evidence_cases
            SET updated_at = 'not-a-timestamp'
            WHERE case_key = 'case_open'
            """
        )
        self.conn.commit()

        with self.assertRaisesRegex(ValueError, "valid ISO timestamp"):
            read_unified_review_feed(
                self.db_path,
                archive_enabled=True,
                timetable_enabled=True,
                title_enabled=False,
                recording_enabled=True,
                limit=10,
                offset=0,
            )
