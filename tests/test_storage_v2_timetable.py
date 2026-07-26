from __future__ import annotations

import json
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import unicodedata
import unittest
from contextlib import redirect_stderr, redirect_stdout


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.storage_v2.repository import apply_migration  # noqa: E402
from lecture_stt.storage_v2 import cli  # noqa: E402
from lecture_stt.storage_v2.timetable import (  # noqa: E402
    TimetableConflictError,
    TimetableWriteDisabledError,
    apply_classification_confirmation,
    apply_recording_classifications,
    apply_timetable_import,
    list_classification_proposals,
    list_timetable_entries,
    plan_classification_confirmation,
    plan_recording_classifications,
    plan_timetable_import,
    read_classification_proposal,
    update_classification_status,
)
from lecture_stt.storage_v2.verifier import verify_library  # noqa: E402


class StorageV2TimetableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "storage-v2.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_csv(self, rows: list[str]) -> Path:
        path = self.root / "시간표.csv"
        path.write_text(
            "\n".join(
                [
                    "학기,과목명,과목코드,요일,시작시간,종료시간,교시,강의실",
                    *rows,
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def _apply_schedule(self, source: Path) -> dict:
        plan = plan_timetable_import(source)
        return apply_timetable_import(
            source,
            self.db_path,
            expected_count=plan["expected_count"],
            expected_plan_sha256=plan["plan_sha256"],
            allow_write=True,
        )

    def _seed_recordings(self) -> None:
        conn = apply_migration(self.db_path)
        try:
            conn.executemany(
                """
                INSERT INTO recordings(
                    storage_key,
                    original_name_raw,
                    original_name_nfc,
                    source_relpath,
                    source_state,
                    recorded_at
                )
                VALUES (?, ?, ?, 'source/unavailable.json', 'missing', ?)
                """,
                [
                    (
                        "rec_unique",
                        "강의.m4a",
                        "강의.m4a",
                        "2026-03-02T10:10:00+09:00",
                    ),
                    (
                        "rec_no_match",
                        "대화.m4a",
                        "대화.m4a",
                        "2026-03-03T18:00:00+09:00",
                    ),
                    (
                        "rec_missing_time",
                        "메모.m4a",
                        "메모.m4a",
                        None,
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def test_csv_plan_normalizes_korean_fields_and_nfc_without_source_path(self) -> None:
        nfd_course = unicodedata.normalize("NFD", "자료구조")
        source = self._write_csv(
            [f"2026-1,{nfd_course},CS201,월요일,10:00,11:15,2교시,E동 101호"]
        )

        plan = plan_timetable_import(source)

        self.assertEqual(plan["semester"], "2026-1")
        self.assertEqual(plan["expected_count"], 1)
        self.assertEqual(plan["entries"][0]["course_name"], "자료구조")
        self.assertEqual(plan["entries"][0]["weekday"], "mon")
        self.assertEqual(plan["entries"][0]["period_index"], 2)
        serialized = json.dumps(plan, ensure_ascii=False)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("source_path", serialized)

    def test_json_plan_accepts_global_semester_and_rejects_unknown_fields(self) -> None:
        source = self.root / "schedule.json"
        source.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "semester": "2026-1",
                    "entries": [
                        {
                            "course_name": "운영체제",
                            "weekday": "Tue",
                            "start_time": "13:00",
                            "end_time": "14:15",
                            "period": 4,
                            "classroom": "공학관 201",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        plan = plan_timetable_import(source)
        self.assertEqual(plan["entries"][0]["period_label"], "4교시")

        source.write_text(
            json.dumps(
                [
                    {
                        "semester": "2026-1",
                        "course_name": "운영체제",
                        "weekday": "화",
                        "start_time": "13:00",
                        "end_time": "14:15",
                        "period": 4,
                        "classroom": "공학관 201",
                        "typo": "must fail",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Unsupported timetable fields"):
            plan_timetable_import(source)

    def test_plan_rejects_invalid_time_and_duplicate_entries(self) -> None:
        invalid = self._write_csv(
            ["2026-1,자료구조,CS201,월,25:00,26:00,2,E101"]
        )
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            plan_timetable_import(invalid)

        duplicate = self._write_csv(
            [
                "2026-1,자료구조,CS201,월,10:00,11:15,2,E101",
                "2026-1,자료구조,CS201,월,10:00,11:15,2교시,E101",
            ]
        )
        with self.assertRaisesRegex(ValueError, "duplicate normalized"):
            plan_timetable_import(duplicate)

        too_long = self._write_csv(
            [f"2026-1,{'가' * 257},CS201,월,10:00,11:15,2,E101"]
        )
        with self.assertRaisesRegex(ValueError, "256-character"):
            plan_timetable_import(too_long)

    def test_import_requires_all_guards_before_creating_database(self) -> None:
        source = self._write_csv(
            ["2026-1,자료구조,CS201,월,10:00,11:15,2,E101"]
        )
        plan = plan_timetable_import(source)

        with self.assertRaises(TimetableWriteDisabledError):
            apply_timetable_import(
                source,
                self.db_path,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
            )
        self.assertFalse(self.db_path.exists())

        with self.assertRaises(TimetableConflictError):
            apply_timetable_import(
                source,
                self.db_path,
                expected_count=2,
                expected_plan_sha256=plan["plan_sha256"],
                allow_write=True,
            )
            self.assertFalse(self.db_path.exists())

    def test_import_is_idempotent_and_list_is_metadata_only(self) -> None:
        source = self._write_csv(
            [
                "2026-1,자료구조,CS201,월,10:00,11:15,2,E101",
                "2026-1,운영체제,CS301,화,13:00,14:15,4,E202",
            ]
        )

        first = self._apply_schedule(source)
        second = self._apply_schedule(source)
        listing = list_timetable_entries(self.db_path, semester="2026-1")

        self.assertEqual(first["action"], "imported")
        self.assertEqual(second["action"], "skipped")
        self.assertEqual(listing["total"], 2)
        self.assertEqual(listing["entries"][0]["course_name"], "자료구조")
        serialized = json.dumps(listing, ensure_ascii=False)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("source_sha256", serialized)

    def test_new_import_replaces_active_semester_without_deleting_history(self) -> None:
        self._seed_recordings()
        source = self._write_csv(
            ["2026-1,자료구조,CS201,월,10:00,11:15,2,E101"]
        )
        first = self._apply_schedule(source)
        source.write_text(
            "\n".join(
                [
                    "학기,과목명,과목코드,요일,시작시간,종료시간,교시,강의실",
                    "2026-1,알고리즘,CS202,월,15:00,16:15,6,E102",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        second = self._apply_schedule(source)

        listing = list_timetable_entries(self.db_path, semester="2026-1")
        plan = plan_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
        )

        self.assertEqual(first["action"], "imported")
        self.assertEqual(second["action"], "imported")
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["entries"][0]["course_name"], "알고리즘")
        self.assertEqual(
            plan["cases"][0]["classification_reason"],
            "no_time_match",
        )
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM schedule_imports").fetchone()[0],
                2,
            )

    def test_import_rejects_digest_mismatch(self) -> None:
        source = self._write_csv(
            ["2026-1,자료구조,CS201,월,10:00,11:15,2,E101"]
        )
        plan = plan_timetable_import(source)
        source.write_text(
            "학기,과목명,과목코드,요일,시작시간,종료시간,교시,강의실\n"
            "2026-1,운영체제,CS301,화,13:00,14:15,4,E202\n",
            encoding="utf-8",
        )
        with self.assertRaises(TimetableConflictError):
            apply_timetable_import(
                source,
                self.db_path,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
                allow_write=True,
            )
        self.assertFalse(self.db_path.exists())

    def test_classification_plan_is_conservative(self) -> None:
        self._seed_recordings()
        self._apply_schedule(
            self._write_csv(
                ["2026-1,자료구조,CS201,월,10:00,11:15,2,E101"]
            )
        )

        plan = plan_recording_classifications(
            self.db_path,
            semester="2026-1",
            margin_minutes=0,
        )
        cases = {case["storage_key"]: case for case in plan["cases"]}

        self.assertEqual(
            cases["rec_unique"]["classification_reason"],
            "unique_time_match",
        )
        self.assertEqual(
            cases["rec_unique"]["proposed_title"],
            "2026-03-02 자료구조 2교시",
        )
        self.assertEqual(cases["rec_unique"]["suggestion_status"], "suggested")
        self.assertEqual(
            cases["rec_no_match"]["classification_reason"],
            "no_time_match",
        )
        self.assertEqual(
            cases["rec_missing_time"]["classification_reason"],
            "recorded_at_missing",
        )
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM recording_classification_proposals"
                ).fetchone()[0],
                0,
            )

    def test_classification_apply_creates_reviews_without_canonical_metadata(self) -> None:
        self._seed_recordings()
        self._apply_schedule(
            self._write_csv(
                ["2026-1,자료구조,CS201,월,10:00,11:15,2,E101"]
            )
        )
        plan = plan_recording_classifications(
            self.db_path,
            semester="2026-1",
            margin_minutes=0,
        )

        result = apply_recording_classifications(
            self.db_path,
            semester="2026-1",
            margin_minutes=0,
            expected_count=plan["expected_count"],
            expected_plan_sha256=plan["plan_sha256"],
            allow_write=True,
        )
        repeated = apply_recording_classifications(
            self.db_path,
            semester="2026-1",
            margin_minutes=0,
            expected_count=plan["expected_count"],
            expected_plan_sha256=plan["plan_sha256"],
            allow_write=True,
        )

        self.assertFalse(result["canonical_metadata_changed"])
        self.assertEqual(result["suggested"], 3)
        self.assertEqual(repeated["skipped"], 3)
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM review_items").fetchone()[0],
                3,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM recording_titles").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM recording_contexts").fetchone()[0],
                0,
            )

    def test_confirmed_classification_replay_is_skipped(self) -> None:
        self._seed_recordings()
        self._apply_schedule(
            self._write_csv(
                ["2026-1,자료구조,CS201,월,10:00,11:15,2,E101"]
            )
        )
        classification = plan_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
        )
        apply_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
            expected_count=1,
            expected_plan_sha256=classification["plan_sha256"],
            allow_write=True,
        )
        proposal = list_classification_proposals(self.db_path)["proposals"][0]
        confirmed = apply_classification_confirmation(
            self.db_path,
            proposal["id"],
            expected_count=1,
            expected_plan_sha256=plan_classification_confirmation(
                self.db_path,
                proposal["id"],
            )["plan_sha256"],
            confirmations_enabled=True,
            allow_write=True,
        )
        replay = apply_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
            expected_count=1,
            expected_plan_sha256=classification["plan_sha256"],
            allow_write=True,
        )

        self.assertEqual(confirmed["action"], "confirmed")
        self.assertEqual(replay["suggested"], 0)
        self.assertEqual(replay["skipped"], 1)
        proposals = list_classification_proposals(self.db_path)
        self.assertEqual(proposals["counts"], {"suggested": 0, "confirmed": 1, "rejected": 0})

    def test_rejected_proposal_can_be_dismissed_idempotently(self) -> None:
        self._seed_recordings()
        self._apply_schedule(
            self._write_csv(
                ["2026-1,자료구조,CS201,월,10:00,11:15,2,E101"]
            )
        )
        classification = plan_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
        )
        apply_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
            expected_count=1,
            expected_plan_sha256=classification["plan_sha256"],
            allow_write=True,
        )
        proposal_id = list_classification_proposals(self.db_path)["proposals"][0]["id"]
        first = update_classification_status(
            self.db_path,
            proposal_id,
            status="rejected",
            status_writes_enabled=True,
            allow_write=True,
        )
        repeated = update_classification_status(
            self.db_path,
            proposal_id,
            status="rejected",
            status_writes_enabled=True,
            allow_write=True,
        )

        self.assertEqual(first["action"], "rejected")
        self.assertEqual(repeated["action"], "skipped")
        detail = read_classification_proposal(self.db_path, proposal_id)
        self.assertEqual(detail["proposal"]["status"], "rejected")
        self.assertEqual(detail["proposal"]["review_status"], "dismissed")

    def test_rejected_status_still_allows_new_active_suggestion(self) -> None:
        self._seed_recordings()
        self._apply_schedule(
            self._write_csv(
                ["2026-1,자료구조,CS201,월,10:00,11:15,2,E101"]
            )
        )
        classification = plan_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
        )
        apply_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
            expected_count=1,
            expected_plan_sha256=classification["plan_sha256"],
            allow_write=True,
        )
        first_id = list_classification_proposals(self.db_path)["proposals"][0]["id"]
        update_classification_status(
            self.db_path,
            first_id,
            status="rejected",
            status_writes_enabled=True,
            allow_write=True,
        )

        second = apply_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
            expected_count=1,
            expected_plan_sha256=classification["plan_sha256"],
            allow_write=True,
        )
        proposals = list_classification_proposals(self.db_path)

        self.assertEqual(second["suggested"], 1)
        self.assertEqual(second["skipped"], 0)
        self.assertEqual(proposals["counts"], {"suggested": 1, "confirmed": 0, "rejected": 1})
        self.assertEqual(proposals["total"], 2)
        ids = {proposal["id"] for proposal in proposals["proposals"]}
        self.assertNotEqual(first_id, max(ids))

    def test_partial_unique_active_constraint_is_present(self) -> None:
        self._seed_recordings()
        source = self._write_csv(
            ["2026-1,자료구조,CS201,월,10:00,11:15,2,E101"]
        )
        self._apply_schedule(source)
        with sqlite3.connect(self.db_path) as conn:
            index_sql = conn.execute(
                """
                SELECT sql
                FROM sqlite_master
                WHERE type = 'index'
                  AND name = 'recording_classification_proposals_one_active_per_recording_semester'
                """
            ).fetchone()
            self.assertIsNotNone(index_sql)
            self.assertIsNotNone(index_sql[0])
            self.assertIn(
                "where status in ('suggested', 'confirmed')",
                str(index_sql[0]).lower(),
            )

    def test_ambiguous_match_enters_review_and_cannot_be_confirmed(self) -> None:
        self._seed_recordings()
        self._apply_schedule(
            self._write_csv(
                [
                    "2026-1,자료구조,CS201,월,10:00,11:15,2,E101",
                    "2026-1,알고리즘,CS202,월,10:00,11:15,2,E102",
                ]
            )
        )
        plan = plan_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
        )
        self.assertEqual(
            plan["cases"][0]["classification_reason"],
            "ambiguous_time_match",
        )
        apply_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
            expected_count=1,
            expected_plan_sha256=plan["plan_sha256"],
            allow_write=True,
        )
        proposals = list_classification_proposals(self.db_path)
        proposal_id = proposals["proposals"][0]["id"]
        with self.assertRaisesRegex(TimetableConflictError, "Unresolved"):
            plan_classification_confirmation(self.db_path, proposal_id)

    def test_explicit_confirmation_is_guarded_audit_only_and_idempotent(self) -> None:
        self._seed_recordings()
        self._apply_schedule(
            self._write_csv(
                ["2026-1,자료구조,CS201,월,10:00,11:15,2,E101"]
            )
        )
        classification = plan_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
        )
        apply_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
            expected_count=1,
            expected_plan_sha256=classification["plan_sha256"],
            allow_write=True,
        )
        proposal_id = list_classification_proposals(self.db_path)["proposals"][0][
            "id"
        ]
        confirmation = plan_classification_confirmation(
            self.db_path,
            proposal_id,
        )
        self.assertEqual(
            confirmation["materialization"],
            "confirmation_audit_only",
        )

        with self.assertRaises(TimetableWriteDisabledError):
            apply_classification_confirmation(
                self.db_path,
                proposal_id,
                expected_count=1,
                expected_plan_sha256=confirmation["plan_sha256"],
                confirmations_enabled=False,
                allow_write=True,
            )
        applied = apply_classification_confirmation(
            self.db_path,
            proposal_id,
            expected_count=1,
            expected_plan_sha256=confirmation["plan_sha256"],
            confirmations_enabled=True,
            allow_write=True,
        )
        repeated = apply_classification_confirmation(
            self.db_path,
            proposal_id,
            expected_count=1,
            expected_plan_sha256=confirmation["plan_sha256"],
            confirmations_enabled=True,
            allow_write=True,
        )

        self.assertEqual(applied["action"], "confirmed")
        self.assertEqual(repeated["action"], "skipped")
        detail = read_classification_proposal(self.db_path, proposal_id)
        self.assertEqual(detail["proposal"]["status"], "confirmed")
        self.assertEqual(detail["proposal"]["review_status"], "resolved")
        self.assertFalse(detail["canonical_metadata_changed"])
        rerun = apply_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
            expected_count=1,
            expected_plan_sha256=classification["plan_sha256"],
            allow_write=True,
        )
        self.assertEqual(rerun["skipped"], 1)
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM recording_titles").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM recording_contexts").fetchone()[0],
                0,
            )

    def test_confirmation_rejects_suggestion_from_replaced_timetable(self) -> None:
        self._seed_recordings()
        source = self._write_csv(
            ["2026-1,자료구조,CS201,월,10:00,11:15,2,E101"]
        )
        self._apply_schedule(source)
        classification = plan_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
        )
        apply_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
            expected_count=1,
            expected_plan_sha256=classification["plan_sha256"],
            allow_write=True,
        )
        proposal_id = list_classification_proposals(self.db_path)["proposals"][0][
            "id"
        ]
        source.write_text(
            "\n".join(
                [
                    "학기,과목명,과목코드,요일,시작시간,종료시간,교시,강의실",
                    "2026-1,알고리즘,CS202,월,15:00,16:15,6,E102",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self._apply_schedule(source)

        with self.assertRaisesRegex(TimetableConflictError, "changed"):
            plan_classification_confirmation(self.db_path, proposal_id)

        with self.assertRaises(TimetableWriteDisabledError):
            update_classification_status(
                self.db_path,
                proposal_id,
                status="rejected",
                status_writes_enabled=False,
                allow_write=True,
            )
        rejected = update_classification_status(
            self.db_path,
            proposal_id,
            status="rejected",
            status_writes_enabled=True,
            allow_write=True,
        )
        repeated = update_classification_status(
            self.db_path,
            proposal_id,
            status="rejected",
            status_writes_enabled=True,
            allow_write=True,
        )
        self.assertEqual(rejected["action"], "rejected")
        self.assertEqual(repeated["action"], "skipped")
        old_detail = read_classification_proposal(self.db_path, proposal_id)
        self.assertEqual(old_detail["proposal"]["status"], "rejected")
        self.assertEqual(old_detail["proposal"]["review_status"], "dismissed")

        replacement_plan = plan_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
        )
        replacement = apply_recording_classifications(
            self.db_path,
            semester="2026-1",
            storage_keys=["rec_unique"],
            margin_minutes=0,
            expected_count=1,
            expected_plan_sha256=replacement_plan["plan_sha256"],
            allow_write=True,
        )
        self.assertEqual(replacement["suggested"], 1)
        proposals = list_classification_proposals(self.db_path)
        self.assertEqual(proposals["counts"]["rejected"], 1)
        self.assertEqual(proposals["counts"]["suggested"], 1)
        self.assertEqual(proposals["total"], 2)
        records_root = self.root / "records"
        records_root.mkdir()
        verified = verify_library(self.db_path, records_root)
        self.assertNotIn(
            "rejected_classification_review_not_dismissed",
            {issue["code"] for issue in verified["issues"]},
        )

    def test_library_verifier_checks_timetable_entry_digest(self) -> None:
        self._apply_schedule(
            self._write_csv(
                ["2026-1,자료구조,CS201,월,10:00,11:15,2,E101"]
            )
        )
        records_root = self.root / "records"
        records_root.mkdir()

        verified = verify_library(self.db_path, records_root)
        self.assertTrue(verified["ok"], verified["issues"])

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO schedule_imports(
                    semester,
                    source_format,
                    source_sha256,
                    entries_sha256,
                    row_count
                )
                VALUES ('2026-2', 'csv', ?, ?, 1)
                """,
                ("a" * 64, "b" * 64),
            )
            conn.execute(
                """
                INSERT INTO schedule_entries(
                    schedule_import_id,
                    row_index,
                    entry_key,
                    semester,
                    course_name,
                    weekday,
                    start_time,
                    end_time,
                    period_label,
                    period_index,
                    classroom
                )
                VALUES (?, 1, ?, '2026-2', '운영체제', 'tue',
                        '13:00', '14:15', '4교시', 4, 'E202')
                """,
                (int(cursor.lastrowid), "c" * 64),
            )
            conn.commit()

        tampered = verify_library(self.db_path, records_root)
        self.assertFalse(tampered["ok"])
        self.assertIn(
            "timetable_import_entries_digest_mismatch",
            {issue["code"] for issue in tampered["issues"]},
        )

    def test_cli_plan_and_apply_require_current_digest_and_write_guard(self) -> None:
        source = self._write_csv(
            ["2026-1,자료구조,CS201,월,10:00,11:15,2,E101"]
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = cli.main(
                ["plan-timetable", "--source", str(source), "--json"]
            )
        self.assertEqual(exit_code, 0)
        plan = json.loads(stdout.getvalue())

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            refused = cli.main(
                [
                    "apply-timetable",
                    "--source",
                    str(source),
                    "--v2-db",
                    str(self.db_path),
                    "--expected-count",
                    "1",
                    "--expected-plan-sha256",
                    plan["plan_sha256"],
                    "--json",
                ]
            )
        self.assertEqual(refused, 2)
        self.assertFalse(self.db_path.exists())

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            applied = cli.main(
                [
                    "apply-timetable",
                    "--source",
                    str(source),
                    "--v2-db",
                    str(self.db_path),
                    "--expected-count",
                    "1",
                    "--expected-plan-sha256",
                    plan["plan_sha256"],
                    "--allow-write",
                    "--json",
                ]
            )
        self.assertEqual(applied, 0)
        self.assertEqual(json.loads(stdout.getvalue())["action"], "imported")


if __name__ == "__main__":
    unittest.main()
