from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.storage_v2.archive_review import (  # noqa: E402
    ArchiveReviewConflictError,
    ArchiveReviewNotFoundError,
    ArchiveReviewWriteDisabledError,
    apply_archive_review_promotion,
    list_archive_review_cases,
    plan_archive_review_promotion,
    read_archive_review_case,
    update_archive_review_status,
)
from lecture_stt.storage_v2.repository import apply_migration  # noqa: E402


class ArchiveReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "storage-v2.sqlite3"
        conn = apply_migration(self.db_path)
        conn.close()
        self._seed()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _seed(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO recordings(
                    storage_key,
                    original_name_raw,
                    original_name_nfc,
                    source_relpath,
                    source_state
                )
                VALUES ('target_record', '원본.m4a', '원본.m4a', 'source/unavailable.json', 'missing')
                """
            )
            conn.execute(
                """
                INSERT INTO archive_evidence_cases(
                    case_key,
                    legacy_delivery_key,
                    logical_stem,
                    subject_abbr
                )
                VALUES ('case_a', 'delivery-a', '260430DStr_2', 'DStr')
                """
            )
            conn.execute(
                """
                INSERT INTO archive_evidence_cases(
                    case_key,
                    legacy_delivery_key,
                    logical_stem,
                    subject_abbr
                )
                VALUES ('case_blocked', 'delivery-blocked', '260501DS_1', 'DS')
                """
            )
            case_a = int(
                conn.execute(
                    "SELECT id FROM archive_evidence_cases WHERE case_key = 'case_a'"
                ).fetchone()[0]
            )
            case_blocked = int(
                conn.execute(
                    "SELECT id FROM archive_evidence_cases WHERE case_key = 'case_blocked'"
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO archive_evidence_captures(
                    case_id,
                    capture_key,
                    reconciliation_classification,
                    legacy_database_sha256,
                    source_fingerprint,
                    plan_sha256,
                    manifest_relpath,
                    snapshot_json
                )
                VALUES (?, 'capture_a', 'verified_delivered', ?, ?, ?, ?, ?)
                """,
                (
                    case_a,
                    "a" * 64,
                    "b" * 64,
                    "c" * 64,
                    "cases/case_a/captures/capture_a.json",
                    json.dumps(
                        {
                            "private_absolute_path": "/Users/example/private/archive",
                            "body": "PRIVATE_BODY_SENTINEL",
                        }
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO archive_evidence_captures(
                    case_id,
                    capture_key,
                    reconciliation_classification,
                    legacy_database_sha256,
                    source_fingerprint,
                    plan_sha256,
                    manifest_relpath,
                    snapshot_json
                )
                VALUES (?, 'capture_blocked', 'blocked', ?, ?, ?, ?, '{}')
                """,
                (
                    case_blocked,
                    "d" * 64,
                    "e" * 64,
                    "f" * 64,
                    "cases/case_blocked/captures/capture_blocked.json",
                ),
            )
            capture_a = int(
                conn.execute(
                    "SELECT id FROM archive_evidence_captures WHERE capture_key = 'capture_a'"
                ).fetchone()[0]
            )
            capture_blocked = int(
                conn.execute(
                    "SELECT id FROM archive_evidence_captures WHERE capture_key = 'capture_blocked'"
                ).fetchone()[0]
            )

            revisions = [
                (
                    case_a,
                    "correction_text",
                    "1" * 64,
                    101,
                    "text/plain",
                    f"cases/case_a/revisions/correction_text/{'1' * 64}.txt",
                ),
                (
                    case_a,
                    "correction_text",
                    "2" * 64,
                    202,
                    "text/plain",
                    f"cases/case_a/revisions/correction_text/{'2' * 64}.txt",
                ),
                (
                    case_a,
                    "correction_json",
                    "3" * 64,
                    303,
                    "application/json",
                    f"cases/case_a/revisions/correction_json/{'3' * 64}.json",
                ),
                (
                    case_a,
                    "summary_markdown",
                    "4" * 64,
                    404,
                    "text/markdown",
                    f"cases/case_a/revisions/summary_markdown/{'4' * 64}.md",
                ),
                (
                    case_blocked,
                    "summary_markdown",
                    "5" * 64,
                    505,
                    "text/markdown",
                    f"cases/case_blocked/revisions/summary_markdown/{'5' * 64}.md",
                ),
            ]
            conn.executemany(
                """
                INSERT INTO archive_evidence_revisions(
                    case_id,
                    artifact_kind,
                    content_sha256,
                    bytes,
                    mime_type,
                    path_rel
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                revisions,
            )

            revision_rows = {
                (str(row["artifact_kind"]), str(row["content_sha256"])): int(row["id"])
                for row in conn.execute(
                    """
                    SELECT id, artifact_kind, content_sha256
                    FROM archive_evidence_revisions
                    """
                ).fetchall()
            }
            observations = [
                (
                    capture_a,
                    case_a,
                    revision_rows[("correction_text", "1" * 64)],
                    "current_gh",
                    "current_gh",
                    "private/course/correction.txt",
                    "1" * 64,
                    "1" * 64,
                    "matches_ledger",
                ),
                (
                    capture_a,
                    case_a,
                    revision_rows[("correction_text", "2" * 64)],
                    "current_obsidian",
                    "current_obsidian",
                    "private/course/correction.txt",
                    "1" * 64,
                    "2" * 64,
                    "differs_from_ledger",
                ),
                (
                    capture_a,
                    case_a,
                    revision_rows[("correction_json", "3" * 64)],
                    "current_gh",
                    "current_gh",
                    "private/course/correction.json",
                    "3" * 64,
                    "3" * 64,
                    "matches_ledger",
                ),
                (
                    capture_a,
                    case_a,
                    revision_rows[("summary_markdown", "4" * 64)],
                    "current_gh",
                    "current_gh",
                    "private/course/summary.md",
                    None,
                    "4" * 64,
                    "unclaimed",
                ),
                (
                    capture_blocked,
                    case_blocked,
                    revision_rows[("summary_markdown", "5" * 64)],
                    "current_gh",
                    "current_gh",
                    "private/course/blocked.md",
                    "0" * 64,
                    "5" * 64,
                    "differs_from_ledger",
                ),
            ]
            conn.executemany(
                """
                INSERT INTO archive_evidence_observations(
                    capture_id,
                    case_id,
                    revision_id,
                    source_role,
                    source_root_label,
                    source_relpath,
                    claimed_sha256,
                    observed_sha256,
                    relationship
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                observations,
            )

    def _selection_mapping(self) -> dict[str, int]:
        detail = read_archive_review_case(self.db_path, "case_a")
        return {
            "correction_text": next(
                int(item["revision_id"])
                for item in detail["revisions"]
                if item["artifact_kind"] == "correction_text"
                and item["content_sha256"] == "1" * 64
            ),
            "correction_json": next(
                int(item["revision_id"])
                for item in detail["revisions"]
                if item["artifact_kind"] == "correction_json"
            ),
            "summary_markdown": next(
                int(item["revision_id"])
                for item in detail["revisions"]
                if item["artifact_kind"] == "summary_markdown"
            ),
        }

    def test_list_is_bounded_filterable_and_metadata_only(self) -> None:
        payload = list_archive_review_cases(
            self.db_path,
            review_status="open",
            limit=1,
            offset=0,
        )

        self.assertTrue(payload["available"])
        self.assertEqual(payload["total"], 2)
        self.assertEqual(len(payload["cases"]), 1)
        self.assertEqual(payload["cases"][0]["review_status"], "open")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("/Users/example", serialized)
        self.assertNotIn("PRIVATE_BODY_SENTINEL", serialized)

        with self.assertRaisesRegex(ValueError, "limit"):
            list_archive_review_cases(self.db_path, limit=101)

    def test_missing_database_list_is_unavailable_without_creating_it(self) -> None:
        missing = self.root / "missing.sqlite3"

        payload = list_archive_review_cases(missing)

        self.assertFalse(payload["available"])
        self.assertFalse(missing.exists())

    def test_detail_compares_revisions_and_suggests_without_confirming(self) -> None:
        payload = read_archive_review_case(self.db_path, "case_a")

        self.assertEqual(payload["case"]["logical_stem"], "260430DStr_2")
        self.assertEqual(len(payload["revisions"]), 4)
        self.assertEqual(payload["confirmed_selections"], [])
        suggestions = {
            item["artifact_kind"]: item
            for item in payload["canonical_suggestions"]
        }
        self.assertEqual(
            suggestions["correction_text"]["reason_code"],
            "unique_matches_ledger",
        )
        self.assertEqual(
            suggestions["summary_markdown"]["reason_code"],
            "unique_latest_capture_current",
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("private/course", serialized)
        self.assertNotIn("/Users/example", serialized)
        self.assertNotIn("PRIVATE_BODY_SENTINEL", serialized)

    def test_multiple_ledger_matches_stay_unresolved(self) -> None:
        with self._connect() as conn:
            case_id = int(
                conn.execute(
                    "SELECT id FROM archive_evidence_cases WHERE case_key = 'case_a'"
                ).fetchone()[0]
            )
            capture_id = int(
                conn.execute(
                    "SELECT id FROM archive_evidence_captures WHERE capture_key = 'capture_a'"
                ).fetchone()[0]
            )
            revision_id = int(
                conn.execute(
                    """
                    SELECT id
                    FROM archive_evidence_revisions
                    WHERE case_id = ?
                      AND artifact_kind = 'correction_text'
                      AND content_sha256 = ?
                    """,
                    (case_id, "2" * 64),
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO archive_evidence_observations(
                    capture_id,
                    case_id,
                    revision_id,
                    source_role,
                    source_root_label,
                    source_relpath,
                    claimed_sha256,
                    observed_sha256,
                    relationship
                )
                VALUES (?, ?, ?, 'historical', 'old', 'old/correction.txt', ?, ?, 'matches_ledger')
                """,
                (capture_id, case_id, revision_id, "2" * 64, "2" * 64),
            )

        detail = read_archive_review_case(self.db_path, "case_a")
        suggestion = next(
            item
            for item in detail["canonical_suggestions"]
            if item["artifact_kind"] == "correction_text"
        )
        self.assertEqual(suggestion["status"], "unresolved")
        self.assertEqual(suggestion["reason_code"], "multiple_ledger_matches")
        self.assertIsNone(suggestion["revision_id"])

    def test_status_update_requires_guard_and_valid_transition(self) -> None:
        with self.assertRaises(ArchiveReviewWriteDisabledError):
            update_archive_review_status(
                self.db_path,
                "case_a",
                "triaged",
            )
        with self.assertRaises(ArchiveReviewConflictError):
            update_archive_review_status(
                self.db_path,
                "case_a",
                "resolved",
                allow_write=True,
            )

        triaged = update_archive_review_status(
            self.db_path,
            "case_a",
            "triaged",
            allow_write=True,
        )
        self.assertTrue(triaged["changed"])
        resolved = update_archive_review_status(
            self.db_path,
            "case_a",
            "resolved",
            allow_write=True,
        )
        self.assertEqual(resolved["review_status"], "resolved")

    def test_promotion_plan_is_stable_and_rejects_cross_case_revision(self) -> None:
        selections = self._selection_mapping()
        first = plan_archive_review_promotion(
            self.db_path,
            "case_a",
            target_storage_key="target_record",
            selected_revisions=selections,
        )
        second = plan_archive_review_promotion(
            self.db_path,
            "case_a",
            target_storage_key="target_record",
            selected_revisions=selections,
        )
        self.assertEqual(first["mode"], "read_only")
        self.assertEqual(first["expected_count"], 1)
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(len(first["selected_revisions"]), 3)

        blocked_revision = int(
            read_archive_review_case(
                self.db_path,
                "case_blocked",
            )["revisions"][0]["revision_id"]
        )
        selections["summary_markdown"] = blocked_revision
        with self.assertRaises(ArchiveReviewConflictError):
            plan_archive_review_promotion(
                self.db_path,
                "case_a",
                target_storage_key="target_record",
                selected_revisions=selections,
            )

    def test_blocked_case_cannot_be_promoted(self) -> None:
        blocked_detail = read_archive_review_case(
            self.db_path,
            "case_blocked",
        )
        selection = {
            "summary_markdown": int(
                blocked_detail["revisions"][0]["revision_id"]
            )
        }
        with self.assertRaisesRegex(
            ArchiveReviewConflictError,
            "Blocked",
        ):
            plan_archive_review_promotion(
                self.db_path,
                "case_blocked",
                target_storage_key="target_record",
                selected_revisions=selection,
            )

    def test_revision_without_provenance_cannot_be_promoted(self) -> None:
        selections = self._selection_mapping()
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM archive_evidence_observations
                WHERE revision_id = ?
                """,
                (selections["summary_markdown"],),
            )

        with self.assertRaisesRegex(
            ArchiveReviewConflictError,
            "no provenance observation",
        ):
            plan_archive_review_promotion(
                self.db_path,
                "case_a",
                target_storage_key="target_record",
                selected_revisions=selections,
            )

    def test_promotion_requires_all_guards_and_rejects_stale_plan(self) -> None:
        selections = self._selection_mapping()
        plan = plan_archive_review_promotion(
            self.db_path,
            "case_a",
            target_storage_key="target_record",
            selected_revisions=selections,
        )

        with self.assertRaises(ArchiveReviewWriteDisabledError):
            apply_archive_review_promotion(
                self.db_path,
                "case_a",
                target_storage_key="target_record",
                selected_revisions=selections,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
                allow_write=True,
            )
        with self.assertRaises(ArchiveReviewWriteDisabledError):
            apply_archive_review_promotion(
                self.db_path,
                "case_a",
                target_storage_key="target_record",
                selected_revisions=selections,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
                promotions_enabled=True,
            )
        with self.assertRaises(ArchiveReviewConflictError):
            apply_archive_review_promotion(
                self.db_path,
                "case_a",
                target_storage_key="target_record",
                selected_revisions=selections,
                expected_count=2,
                expected_plan_sha256=plan["plan_sha256"],
                promotions_enabled=True,
                allow_write=True,
            )

        update_archive_review_status(
            self.db_path,
            "case_a",
            "triaged",
            allow_write=True,
        )
        with self.assertRaisesRegex(
            ArchiveReviewConflictError,
            "plan changed",
        ):
            apply_archive_review_promotion(
                self.db_path,
                "case_a",
                target_storage_key="target_record",
                selected_revisions=selections,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
                promotions_enabled=True,
                allow_write=True,
            )

    def test_successful_metadata_promotion_is_auditable_and_idempotent(self) -> None:
        selections = self._selection_mapping()
        plan = plan_archive_review_promotion(
            self.db_path,
            "case_a",
            target_storage_key="target_record",
            selected_revisions=selections,
        )

        result = apply_archive_review_promotion(
            self.db_path,
            "case_a",
            target_storage_key="target_record",
            selected_revisions=selections,
            expected_count=1,
            expected_plan_sha256=plan["plan_sha256"],
            promotions_enabled=True,
            allow_write=True,
        )

        self.assertEqual(result["status"], "promoted")
        detail = read_archive_review_case(self.db_path, "case_a")
        self.assertEqual(detail["case"]["review_status"], "resolved")
        self.assertEqual(
            detail["case"]["promoted_storage_key"],
            "target_record",
        )
        self.assertEqual(len(detail["confirmed_selections"]), 3)
        self.assertTrue(
            all(
                row["promotion_plan_sha256"] == plan["plan_sha256"]
                for row in detail["confirmed_selections"]
            )
        )

        second = apply_archive_review_promotion(
            self.db_path,
            "case_a",
            target_storage_key="target_record",
            selected_revisions=selections,
            expected_count=1,
            expected_plan_sha256=plan["plan_sha256"],
            promotions_enabled=True,
            allow_write=True,
        )
        self.assertEqual(second["status"], "skipped")

    def test_missing_detail_and_target_recording_are_not_found(self) -> None:
        with self.assertRaises(ArchiveReviewNotFoundError):
            read_archive_review_case(self.db_path, "missing_case")

        with self.assertRaises(ArchiveReviewNotFoundError):
            plan_archive_review_promotion(
                self.db_path,
                "case_a",
                target_storage_key="missing_record",
                selected_revisions=self._selection_mapping(),
            )


if __name__ == "__main__":
    unittest.main()
