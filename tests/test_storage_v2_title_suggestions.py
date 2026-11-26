from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unicodedata
import unittest
from unittest import mock

from lecture_stt.storage_v2 import cli
from lecture_stt.storage_v2 import title_suggestions as title_module
from lecture_stt.storage_v2.repository import apply_migration, connect_v2
from lecture_stt.storage_v2.title_suggestions import (
    TitleSuggestionConflictError,
    TitleSuggestionWriteDisabledError,
    apply_title_suggestion_confirmation,
    apply_title_suggestions,
    list_title_suggestions,
    plan_title_suggestion_confirmation,
    plan_title_suggestions,
    read_title_suggestion,
    reject_title_suggestion,
)
from lecture_stt.storage_v2.verifier import (
    _title_suggestion_integrity_issues,
)


class StorageV2TitleSuggestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "storage-v2.sqlite3"
        self.records_root = self.root / "records"
        self.records_root.mkdir()
        conn = apply_migration(self.db_path)
        conn.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_recording(
        self,
        storage_key: str,
        transcript: str | bytes,
        *,
        recorded_at: str | None = "2026-07-25T10:00:00+09:00",
        title_source: str | None = None,
        context_type: str | None = None,
        context_label: str | None = None,
    ) -> dict[str, object]:
        payload = (
            transcript.encode("utf-8")
            if isinstance(transcript, str)
            else transcript
        )
        job_key = f"job_{storage_key}"
        transcript_relpath = f"jobs/{job_key}/transcript.txt"
        transcript_path = (
            self.records_root / storage_key / transcript_relpath
        )
        transcript_path.parent.mkdir(parents=True)
        transcript_path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        conn = connect_v2(self.db_path)
        try:
            recording_id = int(
                conn.execute(
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
                    (
                        storage_key,
                        f"{storage_key}.m4a",
                        f"{storage_key}.m4a",
                        recorded_at,
                    ),
                ).lastrowid
            )
            if title_source is not None:
                conn.execute(
                    """
                    INSERT INTO recording_titles(
                        recording_id,
                        title,
                        title_source,
                        is_current
                    )
                    VALUES (?, '사용자 정본', ?, 1)
                    """,
                    (recording_id, title_source),
                )
            if context_type is not None:
                conn.execute(
                    """
                    INSERT INTO recording_contexts(
                        recording_id,
                        context_type,
                        label,
                        source,
                        is_selected
                    )
                    VALUES (?, ?, ?, 'manual', 1)
                    """,
                    (recording_id, context_type, context_label),
                )
            job_id = int(
                conn.execute(
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
                        job_key,
                        f"jobs/{job_key}",
                    ),
                ).lastrowid
            )
            artifact_id = int(
                conn.execute(
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
                    VALUES (
                        ?,
                        ?,
                        'transcript_raw_text',
                        1,
                        ?,
                        ?,
                        ?,
                        'text/plain',
                        1
                    )
                    """,
                    (
                        recording_id,
                        job_id,
                        transcript_relpath,
                        digest,
                        len(payload),
                    ),
                ).lastrowid
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "recording_id": recording_id,
            "job_id": job_id,
            "artifact_id": artifact_id,
            "transcript_path": transcript_path,
        }

    def _seed_unique_classification(
        self,
        recording_id: int,
        *,
        course_name: str = "자료구조",
        proposed_title: str = "2026-07-25 자료구조 2교시",
    ) -> int:
        source_sha = "a" * 64
        entries_sha = "b" * 64
        entry_key = "c" * 64
        conn = connect_v2(self.db_path)
        try:
            schedule_import_id = int(
                conn.execute(
                    """
                    INSERT INTO schedule_imports(
                        semester,
                        source_format,
                        source_sha256,
                        entries_sha256,
                        row_count
                    )
                    VALUES ('2026-1', 'csv', ?, ?, 1)
                    """,
                    (source_sha, entries_sha),
                ).lastrowid
            )
            entry_id = int(
                conn.execute(
                    """
                    INSERT INTO schedule_entries(
                        schedule_import_id,
                        row_index,
                        entry_key,
                        semester,
                        course_name,
                        course_code,
                        weekday,
                        start_time,
                        end_time,
                        period_label,
                        period_index,
                        classroom
                    )
                    VALUES (
                        ?,
                        1,
                        ?,
                        '2026-1',
                        ?,
                        'CS201',
                        'fri',
                        '10:00',
                        '11:15',
                        '2교시',
                        2,
                        'E101'
                    )
                    """,
                    (schedule_import_id, entry_key, course_name),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO schedule_semester_selections(
                    semester,
                    schedule_import_id,
                    selection_plan_sha256
                )
                VALUES ('2026-1', ?, ?)
                """,
                (schedule_import_id, "d" * 64),
            )
            detail = json.dumps(
                {
                    "schema_version": (
                        "storage-v2/timetable-classification-review@1"
                    ),
                    "semester": "2026-1",
                    "classification_reason": "unique_time_match",
                    "candidate_entry_keys": [entry_key],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            review_id = int(
                conn.execute(
                    """
                    INSERT INTO review_items(
                        recording_id,
                        status,
                        severity,
                        reason_code,
                        detail_json
                    )
                    VALUES (
                        ?,
                        'open',
                        'medium',
                        'schedule_classification_suggested',
                        ?
                    )
                    """,
                    (recording_id, detail),
                ).lastrowid
            )
            proposal_id = int(
                conn.execute(
                    """
                    INSERT INTO recording_classification_proposals(
                        recording_id,
                        schedule_entry_id,
                        review_item_id,
                        status,
                        classification_reason,
                        proposed_title,
                        context_type,
                        label,
                        semester,
                        course_name,
                        course_code,
                        session_date,
                        weekday,
                        start_time,
                        end_time,
                        period_label,
                        period_index,
                        classroom,
                        confidence,
                        detail_json
                    )
                    VALUES (
                        ?,
                        ?,
                        ?,
                        'suggested',
                        'unique_time_match',
                        ?,
                        'class_session',
                        ?,
                        '2026-1',
                        ?,
                        'CS201',
                        '2026-07-25',
                        'fri',
                        '10:00',
                        '11:15',
                        '2교시',
                        2,
                        'E101',
                        0.75,
                        ?
                    )
                    """,
                    (
                        recording_id,
                        entry_id,
                        review_id,
                        proposed_title,
                        course_name,
                        course_name,
                        detail,
                    ),
                ).lastrowid
            )
            conn.commit()
            return proposal_id
        finally:
            conn.close()

    def _apply_current_plan(
        self,
        *,
        storage_keys: list[str] | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        plan = plan_title_suggestions(
            self.db_path,
            self.records_root,
            storage_keys=storage_keys,
        )
        result = apply_title_suggestions(
            self.db_path,
            self.records_root,
            storage_keys=storage_keys,
            expected_count=int(plan["expected_count"]),
            expected_plan_sha256=str(plan["plan_sha256"]),
            suggestions_enabled=True,
            allow_write=True,
        )
        return plan, result

    def test_generic_content_plan_is_nfc_metadata_only_and_review_only(self) -> None:
        self._seed_recording(
            "rec_meeting",
            (
                "민감한 원문 전체 문장입니다. "
                "보안 대시보드 품질 지표 보안 대시보드 분류 현황"
            ),
            context_type="meeting",
            context_label="주간 운영 회의",
        )

        plan, result = self._apply_current_plan()

        self.assertEqual(plan["expected_count"], 1)
        case = plan["cases"][0]
        self.assertEqual(case["suggestion_reason"], "content_topic")
        self.assertEqual(
            case["proposed_title"],
            "2026-07-25 주간 운영 회의 보안 · 대시보드 · 민감한",
        )
        self.assertEqual(result["created"], 1)
        self.assertFalse(result["canonical_metadata_changed"])
        serialized = json.dumps(plan, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(str(self.records_root), serialized)
        self.assertNotIn("transcript_path", serialized)
        self.assertNotIn("content_sha256", serialized)
        self.assertNotIn("artifact_id", serialized)
        self.assertNotIn("민감한 원문 전체 문장입니다", serialized)

        conn = connect_v2(self.db_path, readonly=True)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM recording_titles"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM recording_contexts"
                ).fetchone()[0],
                1,
            )
            proposal = conn.execute(
                """
                SELECT proposed_title, status, detail_json
                FROM recording_title_proposals
                """
            ).fetchone()
            self.assertEqual(proposal["status"], "suggested")
            self.assertNotIn(
                "민감한 원문 전체 문장입니다",
                str(proposal["detail_json"]),
            )
        finally:
            conn.close()

    def test_exact_course_signal_uses_school_date_course_period_title(self) -> None:
        seeded = self._seed_recording(
            "rec_class",
            "오늘 자료구조 시간에는 그래프 탐색 자료구조 알고리즘을 다룹니다.",
        )
        self._seed_unique_classification(
            int(seeded["recording_id"]),
        )

        plan = plan_title_suggestions(
            self.db_path,
            self.records_root,
        )

        self.assertEqual(plan["expected_count"], 1)
        case = plan["cases"][0]
        self.assertEqual(
            case["suggestion_reason"],
            "schedule_content_match",
        )
        self.assertEqual(
            case["proposed_title"],
            "2026-07-25 자료구조 2교시",
        )
        self.assertEqual(case["confidence"], 0.9)

    def test_course_code_requires_an_exact_token_boundary(self) -> None:
        seeded = self._seed_recording(
            "rec_course_code_boundary",
            "CS2010 프로젝트 프로젝트 회의 일정",
        )
        self._seed_unique_classification(
            int(seeded["recording_id"]),
        )

        plan = plan_title_suggestions(
            self.db_path,
            self.records_root,
        )

        self.assertEqual(
            plan["cases"][0]["suggestion_reason"],
            "content_topic",
        )
        self.assertIsNone(
            plan["cases"][0]["classification_status"],
        )

    def test_unconfirmed_schedule_content_falls_back_to_topic(self) -> None:
        seeded = self._seed_recording(
            "rec_not_course",
            "배포 일정 보안 점검 배포 일정 운영 회의",
        )
        self._seed_unique_classification(
            int(seeded["recording_id"]),
        )

        plan = plan_title_suggestions(
            self.db_path,
            self.records_root,
        )

        self.assertEqual(
            plan["cases"][0]["suggestion_reason"],
            "content_topic",
        )
        self.assertIsNone(
            plan["cases"][0]["classification_status"],
        )

    def test_manual_and_schedule_current_titles_are_protected(self) -> None:
        self._seed_recording(
            "rec_manual",
            "보안 대시보드 보안 지표 품질 분포",
            title_source="manual",
        )
        self._seed_recording(
            "rec_schedule",
            "자료구조 그래프 자료구조 탐색 알고리즘",
            title_source="schedule",
        )

        plan = plan_title_suggestions(
            self.db_path,
            self.records_root,
        )

        self.assertEqual(plan["expected_count"], 0)
        self.assertEqual(
            plan["coverage"]["current_title_protected"],
            2,
        )

    def test_database_must_stay_outside_records_root(self) -> None:
        nested_db = self.records_root / "unsafe.sqlite3"
        conn = apply_migration(nested_db)
        conn.close()
        with self.assertRaisesRegex(
            ValueError,
            "outside the records root",
        ):
            plan_title_suggestions(
                nested_db,
                self.records_root,
            )

    def test_records_root_cannot_be_symlink(self) -> None:
        linked_root = self.root / "records_link"
        linked_root.symlink_to(self.records_root)
        with self.assertRaisesRegex(
            ValueError,
            "must be a non-symlink directory",
        ):
            plan_title_suggestions(
                self.db_path,
                linked_root,
            )

    def test_hidden_artifact_evidence_changes_guard_digest(self) -> None:
        seeded = self._seed_recording(
            "rec_digest",
            "보안 대시보드 보안 품질 대시보드 분류",
        )
        first = plan_title_suggestions(
            self.db_path,
            self.records_root,
        )
        path = Path(seeded["transcript_path"])
        replacement = (
            "보안 대시보드 보안 품질 대시보드 분류 추가"
        ).encode("utf-8")
        path.write_bytes(replacement)
        conn = connect_v2(self.db_path)
        try:
            conn.execute(
                """
                UPDATE artifacts
                SET content_sha256 = ?, bytes = ?
                WHERE id = ?
                """,
                (
                    hashlib.sha256(replacement).hexdigest(),
                    len(replacement),
                    seeded["artifact_id"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

        second = plan_title_suggestions(
            self.db_path,
            self.records_root,
        )

        self.assertEqual(first["cases"], second["cases"])
        self.assertNotEqual(
            first["plan_sha256"],
            second["plan_sha256"],
        )

    def test_hidden_inference_metadata_changes_guard_digest(self) -> None:
        seeded = self._seed_recording(
            "rec_inference_digest",
            "보안 대시보드 보안 품질 대시보드 분류",
            recorded_at="2026-07-25T10:00:00+09:00",
        )
        first = plan_title_suggestions(
            self.db_path,
            self.records_root,
        )
        conn = connect_v2(self.db_path)
        try:
            conn.execute(
                """
                UPDATE recordings
                SET recorded_at = '2026-07-25T11:00:00+09:00'
                WHERE id = ?
                """,
                (seeded["recording_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        second = plan_title_suggestions(
            self.db_path,
            self.records_root,
        )

        self.assertEqual(first["cases"], second["cases"])
        self.assertNotEqual(
            first["plan_sha256"],
            second["plan_sha256"],
        )

    def test_apply_requires_enable_allow_count_and_digest_before_writes(self) -> None:
        self._seed_recording(
            "rec_guard",
            "보안 대시보드 보안 품질 대시보드 분류",
        )
        plan = plan_title_suggestions(
            self.db_path,
            self.records_root,
        )
        kwargs = {
            "expected_count": plan["expected_count"],
            "expected_plan_sha256": plan["plan_sha256"],
        }
        with self.assertRaises(TitleSuggestionWriteDisabledError):
            apply_title_suggestions(
                self.db_path,
                self.records_root,
                **kwargs,
            )
        with self.assertRaises(TitleSuggestionWriteDisabledError):
            apply_title_suggestions(
                self.db_path,
                self.records_root,
                suggestions_enabled=True,
                **kwargs,
            )
        with self.assertRaises(TitleSuggestionConflictError):
            apply_title_suggestions(
                self.db_path,
                self.records_root,
                expected_count=2,
                expected_plan_sha256=plan["plan_sha256"],
                suggestions_enabled=True,
                allow_write=True,
            )
        with self.assertRaises(TitleSuggestionConflictError):
            apply_title_suggestions(
                self.db_path,
                self.records_root,
                expected_count=plan["expected_count"],
                expected_plan_sha256="0" * 64,
                suggestions_enabled=True,
                allow_write=True,
            )
        conn = connect_v2(self.db_path, readonly=True)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM recording_title_proposals"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM review_items"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_apply_is_idempotent_and_conflicts_with_different_active_row(self) -> None:
        self._seed_recording(
            "rec_idempotent",
            "보안 대시보드 보안 품질 대시보드 분류",
        )
        plan, first = self._apply_current_plan()
        second = apply_title_suggestions(
            self.db_path,
            self.records_root,
            expected_count=plan["expected_count"],
            expected_plan_sha256=plan["plan_sha256"],
            suggestions_enabled=True,
            allow_write=True,
        )
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["skipped"], 1)
        conn = connect_v2(self.db_path)
        try:
            conn.execute(
                """
                UPDATE recording_title_proposals
                SET proposed_title = '다른 활성 제안'
                """
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(TitleSuggestionConflictError):
            apply_title_suggestions(
                self.db_path,
                self.records_root,
                expected_count=plan["expected_count"],
                expected_plan_sha256=plan["plan_sha256"],
                suggestions_enabled=True,
                allow_write=True,
            )

    def test_idempotent_apply_rechecks_linked_review_row(self) -> None:
        self._seed_recording(
            "rec_review_idempotent",
            "보안 대시보드 보안 품질 대시보드 분류",
        )
        plan, _ = self._apply_current_plan()
        conn = connect_v2(self.db_path)
        try:
            conn.execute(
                """
                UPDATE review_items
                SET severity = 'low'
                WHERE reason_code = 'title_suggestion_content_topic'
                """
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaisesRegex(
            TitleSuggestionConflictError,
            "review_severity",
        ):
            apply_title_suggestions(
                self.db_path,
                self.records_root,
                expected_count=plan["expected_count"],
                expected_plan_sha256=plan["plan_sha256"],
                suggestions_enabled=True,
                allow_write=True,
            )

    def test_reject_then_reapply_generates_new_active_suggestion(self) -> None:
        self._seed_recording(
            "rec_reapply",
            "회의 일정 회의 배포 일정 품질",
        )
        plan = plan_title_suggestions(
            self.db_path,
            self.records_root,
        )
        first = apply_title_suggestions(
            self.db_path,
            self.records_root,
            expected_count=plan["expected_count"],
            expected_plan_sha256=plan["plan_sha256"],
            suggestions_enabled=True,
            allow_write=True,
        )
        self.assertEqual(first["created"], 1)

        proposal_id = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]["id"]
        reject_title_suggestion(
            self.db_path,
            proposal_id,
            status_writes_enabled=True,
            allow_write=True,
        )

        second = apply_title_suggestions(
            self.db_path,
            self.records_root,
            expected_count=plan["expected_count"],
            expected_plan_sha256=plan["plan_sha256"],
            suggestions_enabled=True,
            allow_write=True,
        )
        self.assertEqual(second["created"], 1)
        self.assertEqual(second["skipped"], 0)

        snapshot = list_title_suggestions(self.db_path, status=None)
        self.assertEqual(
            snapshot["counts"],
            {"suggested": 1, "confirmed": 0, "rejected": 1},
        )

    def test_apply_final_recheck_rolls_back_if_transcript_changes(self) -> None:
        seeded = self._seed_recording(
            "rec_final_recheck",
            "보안 대시보드 보안 품질 대시보드 분류",
        )
        plan = plan_title_suggestions(
            self.db_path,
            self.records_root,
        )
        original_build_plan = title_module._build_plan
        call_count = 0

        def build_plan_with_change(*args: object, **kwargs: object):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                Path(seeded["transcript_path"]).write_text(
                    "commit 직전 외부 변경",
                    encoding="utf-8",
                )
            return original_build_plan(*args, **kwargs)

        with mock.patch.object(
            title_module,
            "_build_plan",
            side_effect=build_plan_with_change,
        ):
            with self.assertRaises(TitleSuggestionConflictError):
                apply_title_suggestions(
                    self.db_path,
                    self.records_root,
                    expected_count=plan["expected_count"],
                    expected_plan_sha256=plan["plan_sha256"],
                    suggestions_enabled=True,
                    allow_write=True,
                )

        conn = connect_v2(self.db_path, readonly=True)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM recording_title_proposals"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM review_items"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_symlink_hardlink_hash_and_utf8_fail_closed(self) -> None:
        cases: list[tuple[str, str]] = [
            ("symlink", "보안 대시보드 보안 품질"),
            ("hardlink", "회의 일정 회의 배포 일정"),
            ("hash", "개인 메모 개인 아이디어 메모"),
            ("utf8", "정상 텍스트 정상 키워드"),
        ]
        seeded = {
            name: self._seed_recording(f"rec_{name}", transcript)
            for name, transcript in cases
        }

        symlink_path = Path(seeded["symlink"]["transcript_path"])
        symlink_target = self.root / "symlink-target.txt"
        symlink_target.write_bytes(symlink_path.read_bytes())
        symlink_path.unlink()
        symlink_path.symlink_to(symlink_target)
        with self.assertRaises(TitleSuggestionConflictError):
            plan_title_suggestions(
                self.db_path,
                self.records_root,
                storage_keys=["rec_symlink"],
            )

        hardlink_path = Path(seeded["hardlink"]["transcript_path"])
        os.link(hardlink_path, self.root / "hardlink-copy.txt")
        with self.assertRaises(TitleSuggestionConflictError):
            plan_title_suggestions(
                self.db_path,
                self.records_root,
                storage_keys=["rec_hardlink"],
            )

        Path(seeded["hash"]["transcript_path"]).write_text(
            "변조된 원문",
            encoding="utf-8",
        )
        with self.assertRaises(TitleSuggestionConflictError):
            plan_title_suggestions(
                self.db_path,
                self.records_root,
                storage_keys=["rec_hash"],
            )

        invalid = b"\xff\xfe\xfd"
        utf8_path = Path(seeded["utf8"]["transcript_path"])
        utf8_path.write_bytes(invalid)
        conn = connect_v2(self.db_path)
        try:
            conn.execute(
                """
                UPDATE artifacts
                SET content_sha256 = ?, bytes = ?
                WHERE id = ?
                """,
                (
                    hashlib.sha256(invalid).hexdigest(),
                    len(invalid),
                    seeded["utf8"]["artifact_id"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(TitleSuggestionConflictError):
            plan_title_suggestions(
                self.db_path,
                self.records_root,
                storage_keys=["rec_utf8"],
            )

    def test_confirmation_and_rejection_are_audit_only(self) -> None:
        self._seed_recording(
            "rec_confirm",
            "보안 대시보드 보안 품질 대시보드 분류",
        )
        self._apply_current_plan()
        proposal = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]
        confirmation = plan_title_suggestion_confirmation(
            self.db_path,
            self.records_root,
            proposal["id"],
        )
        with self.assertRaises(TitleSuggestionWriteDisabledError):
            apply_title_suggestion_confirmation(
                self.db_path,
                self.records_root,
                proposal["id"],
                expected_count=1,
                expected_plan_sha256=confirmation["plan_sha256"],
                allow_write=True,
            )
        applied = apply_title_suggestion_confirmation(
            self.db_path,
            self.records_root,
            proposal["id"],
            expected_count=1,
            expected_plan_sha256=confirmation["plan_sha256"],
            confirmations_enabled=True,
            allow_write=True,
        )
        self.assertEqual(applied["status"], "confirmed")
        self.assertFalse(applied["canonical_metadata_changed"])

        conn = connect_v2(self.db_path, readonly=True)
        try:
            proposal_row = conn.execute(
                """
                SELECT
                    pt.status AS proposal_status,
                    r.status AS review_status
                FROM recording_title_proposals AS pt
                JOIN review_items AS r
                  ON r.id = pt.review_item_id
                 AND r.recording_id = pt.recording_id
                WHERE pt.id = ?
                """,
                (proposal["id"],),
            ).fetchone()
            self.assertEqual(proposal_row["proposal_status"], "confirmed")
            self.assertEqual(proposal_row["review_status"], "resolved")
        finally:
            conn.close()

        conn = connect_v2(self.db_path, readonly=True)
        try:
            recording_id = conn.execute(
                """
                SELECT recording_id
                FROM recording_title_proposals
                WHERE id = ?
                """,
                (proposal["id"],),
            ).fetchone()[0]
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM recording_titles WHERE recording_id = ?",
                    (recording_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM recording_contexts WHERE recording_id = ?",
                    (recording_id,),
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

        repeated = apply_title_suggestion_confirmation(
            self.db_path,
            self.records_root,
            proposal["id"],
            expected_count=1,
            expected_plan_sha256=confirmation["plan_sha256"],
            confirmations_enabled=True,
            allow_write=True,
        )
        self.assertEqual(repeated["action"], "skipped")

        self._seed_recording(
            "rec_reject",
            "회의 일정 회의 배포 일정 품질",
        )
        self._apply_current_plan(storage_keys=["rec_reject"])
        rejected_id = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]["id"]
        with self.assertRaises(TitleSuggestionWriteDisabledError):
            reject_title_suggestion(
                self.db_path,
                rejected_id,
                allow_write=True,
            )
        rejected = reject_title_suggestion(
            self.db_path,
            rejected_id,
            status_writes_enabled=True,
            allow_write=True,
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertFalse(rejected["canonical_metadata_changed"])

        conn = connect_v2(self.db_path, readonly=True)
        try:
            rejected_row = conn.execute(
                """
                SELECT
                    pt.status AS proposal_status,
                    r.status AS review_status
                FROM recording_title_proposals AS pt
                JOIN review_items AS r
                  ON r.id = pt.review_item_id
                 AND r.recording_id = pt.recording_id
                WHERE pt.id = ?
                """,
                (rejected_id,),
            ).fetchone()
            self.assertEqual(rejected_row["proposal_status"], "rejected")
            self.assertEqual(rejected_row["review_status"], "dismissed")
        finally:
            conn.close()

        conn = connect_v2(self.db_path, readonly=True)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM recording_titles"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM recording_contexts"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_confirmation_fails_closed_when_transcript_changes(self) -> None:
        seeded = self._seed_recording(
            "rec_confirm_stale",
            "보안 대시보드 보안 품질 대시보드 분류",
        )
        self._apply_current_plan()
        proposal = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]
        confirmation = plan_title_suggestion_confirmation(
            self.db_path,
            self.records_root,
            proposal["id"],
        )
        Path(seeded["transcript_path"]).write_text(
            "확정 직전 변조된 전사",
            encoding="utf-8",
        )

        with self.assertRaises(TitleSuggestionConflictError):
            apply_title_suggestion_confirmation(
                self.db_path,
                self.records_root,
                proposal["id"],
                expected_count=1,
                expected_plan_sha256=confirmation["plan_sha256"],
                confirmations_enabled=True,
                allow_write=True,
            )

        conn = connect_v2(self.db_path, readonly=True)
        try:
            row = conn.execute(
                """
                SELECT
                    proposal.status AS proposal_status,
                    review.status AS review_status,
                    proposal.confirmation_plan_sha256,
                    proposal.confirmed_at
                FROM recording_title_proposals AS proposal
                JOIN review_items AS review
                  ON review.id = proposal.review_item_id
                 AND review.recording_id = proposal.recording_id
                WHERE proposal.id = ?
                """,
                (proposal["id"],),
            ).fetchone()
            self.assertEqual(row["proposal_status"], "suggested")
            self.assertEqual(row["review_status"], "open")
            self.assertIsNone(row["confirmation_plan_sha256"])
            self.assertIsNone(row["confirmed_at"])
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM recording_titles"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_confirmation_rechecks_manual_title_protection(self) -> None:
        seeded = self._seed_recording(
            "rec_confirm_manual",
            "보안 대시보드 보안 품질 대시보드 분류",
        )
        self._apply_current_plan()
        proposal = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]
        conn = connect_v2(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO recording_titles(
                    recording_id,
                    title,
                    title_source,
                    is_current
                )
                VALUES (?, '사용자 확정 제목', 'manual', 1)
                """,
                (seeded["recording_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaisesRegex(
            TitleSuggestionConflictError,
            "no longer supported",
        ):
            plan_title_suggestion_confirmation(
                self.db_path,
                self.records_root,
                proposal["id"],
            )

    def test_confirmation_digest_guards_same_output_inference_change(self) -> None:
        seeded = self._seed_recording(
            "rec_confirm_inference",
            "보안 대시보드 보안 품질 대시보드 분류",
            recorded_at="2026-07-25T10:00:00+09:00",
        )
        self._apply_current_plan()
        proposal = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]
        confirmation = plan_title_suggestion_confirmation(
            self.db_path,
            self.records_root,
            proposal["id"],
        )
        conn = connect_v2(self.db_path)
        try:
            conn.execute(
                """
                UPDATE recordings
                SET recorded_at = '2026-07-25T11:00:00+09:00'
                WHERE id = ?
                """,
                (seeded["recording_id"],),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(TitleSuggestionConflictError):
            apply_title_suggestion_confirmation(
                self.db_path,
                self.records_root,
                proposal["id"],
                expected_count=1,
                expected_plan_sha256=confirmation["plan_sha256"],
                confirmations_enabled=True,
                allow_write=True,
            )

    def test_confirmation_precommit_recheck_rolls_back_transition(self) -> None:
        seeded = self._seed_recording(
            "rec_confirm_precommit",
            "보안 대시보드 보안 품질 대시보드 분류",
        )
        self._apply_current_plan()
        proposal = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]
        confirmation = plan_title_suggestion_confirmation(
            self.db_path,
            self.records_root,
            proposal["id"],
        )
        original_read = title_module._read_transcript_text
        call_count = 0

        def read_with_precommit_change(
            *args: object,
            **kwargs: object,
        ) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                Path(seeded["transcript_path"]).write_text(
                    "commit 직전 변조된 전사",
                    encoding="utf-8",
                )
            return original_read(*args, **kwargs)

        with mock.patch.object(
            title_module,
            "_read_transcript_text",
            side_effect=read_with_precommit_change,
        ):
            with self.assertRaises(TitleSuggestionConflictError):
                apply_title_suggestion_confirmation(
                    self.db_path,
                    self.records_root,
                    proposal["id"],
                    expected_count=1,
                    expected_plan_sha256=confirmation["plan_sha256"],
                    confirmations_enabled=True,
                    allow_write=True,
                )

        conn = connect_v2(self.db_path, readonly=True)
        try:
            row = conn.execute(
                """
                SELECT
                    proposal.status AS proposal_status,
                    review.status AS review_status,
                    proposal.confirmation_plan_sha256,
                    proposal.confirmed_at
                FROM recording_title_proposals AS proposal
                JOIN review_items AS review
                  ON review.id = proposal.review_item_id
                 AND review.recording_id = proposal.recording_id
                WHERE proposal.id = ?
                """,
                (proposal["id"],),
            ).fetchone()
            self.assertEqual(row["proposal_status"], "suggested")
            self.assertEqual(row["review_status"], "open")
            self.assertIsNone(row["confirmation_plan_sha256"])
            self.assertIsNone(row["confirmed_at"])
        finally:
            conn.close()

    def test_confirmed_replay_recomputes_stable_evidence_digest(self) -> None:
        self._seed_recording(
            "rec_confirm_replay_digest",
            "보안 대시보드 보안 품질 대시보드 분류",
        )
        self._apply_current_plan()
        proposal = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]
        confirmation = plan_title_suggestion_confirmation(
            self.db_path,
            self.records_root,
            proposal["id"],
        )
        apply_title_suggestion_confirmation(
            self.db_path,
            self.records_root,
            proposal["id"],
            expected_count=1,
            expected_plan_sha256=confirmation["plan_sha256"],
            confirmations_enabled=True,
            allow_write=True,
        )

        conn = connect_v2(self.db_path)
        try:
            conn.execute(
                """
                UPDATE recording_title_proposals
                SET proposed_title = '변조된 확정 제안'
                WHERE id = ?
                """,
                (proposal["id"],),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(TitleSuggestionConflictError):
            apply_title_suggestion_confirmation(
                self.db_path,
                self.records_root,
                proposal["id"],
                expected_count=1,
                expected_plan_sha256=confirmation["plan_sha256"],
                confirmations_enabled=True,
                allow_write=True,
            )

    def test_reject_refuses_a_relinked_unrelated_review(self) -> None:
        self._seed_recording(
            "rec_reject_relinked",
            "보안 대시보드 보안 품질 대시보드 분류",
        )
        self._apply_current_plan()
        proposal = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]
        conn = connect_v2(self.db_path)
        try:
            original = conn.execute(
                """
                SELECT
                    proposal.recording_id,
                    proposal.review_item_id,
                    artifact.job_id,
                    proposal.transcript_artifact_id
                FROM recording_title_proposals AS proposal
                JOIN artifacts AS artifact
                  ON artifact.id = proposal.transcript_artifact_id
                 AND artifact.recording_id = proposal.recording_id
                WHERE proposal.id = ?
                """,
                (proposal["id"],),
            ).fetchone()
            unrelated_review_id = int(
                conn.execute(
                    """
                    INSERT INTO review_items(
                        recording_id,
                        job_id,
                        artifact_id,
                        status,
                        severity,
                        reason_code,
                        detail_json
                    )
                    VALUES (?, ?, ?, 'open', 'medium', 'other_issue', '{}')
                    """,
                    (
                        original["recording_id"],
                        original["job_id"],
                        original["transcript_artifact_id"],
                    ),
                ).lastrowid
            )
            conn.execute(
                """
                UPDATE recording_title_proposals
                SET review_item_id = ?
                WHERE id = ?
                """,
                (unrelated_review_id, proposal["id"]),
            )
            conn.commit()
            original_review_id = int(original["review_item_id"])
        finally:
            conn.close()

        with self.assertRaisesRegex(
            TitleSuggestionConflictError,
            "review evidence",
        ):
            reject_title_suggestion(
                self.db_path,
                proposal["id"],
                status_writes_enabled=True,
                allow_write=True,
            )

        conn = connect_v2(self.db_path, readonly=True)
        try:
            statuses = {
                int(row["id"]): str(row["status"])
                for row in conn.execute(
                    """
                    SELECT id, status
                    FROM review_items
                    WHERE id IN (?, ?)
                    """,
                    (original_review_id, unrelated_review_id),
                ).fetchall()
            }
            self.assertEqual(statuses[original_review_id], "open")
            self.assertEqual(statuses[unrelated_review_id], "open")
            self.assertEqual(
                conn.execute(
                    """
                    SELECT status
                    FROM recording_title_proposals
                    WHERE id = ?
                    """,
                    (proposal["id"],),
                ).fetchone()[0],
                "suggested",
            )
        finally:
            conn.close()

    def test_snapshot_and_integrity_check_expose_no_artifact_locator(self) -> None:
        self._seed_recording(
            "rec_snapshot",
            "개인 메모 아이디어 개인 메모 설계",
            context_type="memo",
        )
        self._apply_current_plan()
        snapshot = list_title_suggestions(self.db_path)
        serialized = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertNotIn(str(self.records_root), serialized)
        self.assertNotIn("path_rel", serialized)
        self.assertNotIn("sha256", serialized)
        self.assertNotIn("artifact_id", serialized)
        self.assertEqual(
            set(snapshot["proposals"][0].keys()),
            {
                "id",
                "storage_key",
                "status",
                "suggestion_reason",
                "proposed_title",
                "confidence",
                "generator_version",
                "review_status",
                "transcript_revision",
                "classification_status",
                "context_type",
                "created_at",
                "updated_at",
                "confirmed_at",
                "canonical_metadata_changed",
            },
        )

        conn = connect_v2(self.db_path)
        try:
            self.assertEqual(
                _title_suggestion_integrity_issues(conn),
                [],
            )
            conn.execute(
                """
                UPDATE review_items
                SET detail_json = '{}'
                WHERE reason_code LIKE 'title_suggestion_%'
                """
            )
            conn.commit()
            issue_codes = {
                issue["code"]
                for issue in _title_suggestion_integrity_issues(conn)
            }
            self.assertIn(
                "title_suggestion_detail_invalid",
                issue_codes,
            )
        finally:
            conn.close()

    def test_list_fails_closed_when_lifecycle_columns_are_tampered(self) -> None:
        self._seed_recording(
            "rec_lifecycle",
            "개인 메모 아이디어 개인 메모 설계",
            context_type="memo",
        )
        self._apply_current_plan()
        proposal_id = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]["id"]

        conn = connect_v2(self.db_path)
        try:
            conn.execute(
                """
                UPDATE review_items
                SET status = 'dismissed',
                    resolved_at = CURRENT_TIMESTAMP
                WHERE id = (
                    SELECT review_item_id
                    FROM recording_title_proposals
                    WHERE id = ?
                )
                """,
                (proposal_id,),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaisesRegex(
            TitleSuggestionConflictError,
            "lifecycle is inconsistent",
        ):
            list_title_suggestions(self.db_path, status="suggested")

    def test_public_proposal_id_must_stay_within_js_safe_integer_limit(self) -> None:
        self._seed_recording(
            "rec_unsafe_id",
            "개인 메모 아이디어 개인 메모 설계",
            context_type="memo",
        )
        self._apply_current_plan()
        proposal_id = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]["id"]
        unsafe_id = 9_007_199_254_740_992

        conn = connect_v2(self.db_path)
        try:
            conn.execute(
                """
                UPDATE recording_title_proposals
                SET id = ?
                WHERE id = ?
                """,
                (unsafe_id, proposal_id),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaisesRegex(
            ValueError,
            "public integer limit",
        ):
            list_title_suggestions(self.db_path, status="suggested")
        with self.assertRaisesRegex(
            ValueError,
            "public integer limit",
        ):
            read_title_suggestion(self.db_path, unsafe_id)

    def test_detail_is_metadata_only_and_normalizes_linked_classification_strings(
        self,
    ) -> None:
        seeded = self._seed_recording(
            "rec_detail",
            "자료구조 강의에서 그래프를 다루는 자료구조 수업",
        )
        self._seed_unique_classification(
            int(seeded["recording_id"]),
            course_name=unicodedata.normalize("NFD", "자료구조"),
            proposed_title=unicodedata.normalize(
                "NFD",
                "2026-07-25 자료구조 2교시",
            ),
        )
        self._apply_current_plan(storage_keys=["rec_detail"])
        proposal_id = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]["id"]

        detail = read_title_suggestion(self.db_path, proposal_id)

        serialized = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(str(self.records_root), serialized)
        self.assertNotIn("path_rel", serialized)
        self.assertNotIn("sha256", serialized)
        self.assertNotIn("artifact_id", serialized)
        self.assertNotIn("recording_id", serialized)
        self.assertNotIn("review_item_id", serialized)
        self.assertEqual(
            detail["proposal"]["proposed_title"],
            "2026-07-25 자료구조 2교시",
        )
        self.assertEqual(
            detail["linked_classification"]["course_name"],
            "자료구조",
        )

    def test_integrity_catches_invalid_classification_link(self) -> None:
        seeded = self._seed_recording(
            "rec_class_bad",
            "자료구조 강의에서 그래프를 다루는 자료구조 수업",
        )
        classification_id = self._seed_unique_classification(
            int(seeded["recording_id"]),
        )
        self._apply_current_plan(storage_keys=["rec_class_bad"])

        conn = connect_v2(self.db_path)
        try:
            conn.execute(
                """
                UPDATE recording_classification_proposals
                SET status = 'rejected'
                WHERE id = ?
                """,
                (classification_id,),
            )
            conn.commit()

            issue_codes = {
                issue["code"]
                for issue in _title_suggestion_integrity_issues(conn)
            }
            self.assertIn(
                "title_suggestion_classification_link_invalid",
                issue_codes,
            )
        finally:
            conn.close()

    def test_integrity_flags_confirmed_transcript_that_is_no_longer_current(
        self,
    ) -> None:
        seeded = self._seed_recording(
            "rec_confirmed_history",
            "보안 대시보드 보안 품질 대시보드 분류",
        )
        self._apply_current_plan()
        proposal = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]
        confirmation = plan_title_suggestion_confirmation(
            self.db_path,
            self.records_root,
            proposal["id"],
        )
        apply_title_suggestion_confirmation(
            self.db_path,
            self.records_root,
            proposal["id"],
            expected_count=1,
            expected_plan_sha256=confirmation["plan_sha256"],
            confirmations_enabled=True,
            allow_write=True,
        )

        conn = connect_v2(self.db_path)
        try:
            conn.execute(
                "UPDATE artifacts SET is_latest = 0 WHERE id = ?",
                (seeded["artifact_id"],),
            )
            conn.execute(
                "UPDATE transcription_jobs SET is_current = 0 WHERE id = ?",
                (seeded["job_id"],),
            )
            conn.commit()
            issue_codes = {
                issue["code"]
                for issue in _title_suggestion_integrity_issues(conn)
            }
            self.assertIn(
                "active_title_suggestion_transcript_not_current",
                issue_codes,
            )
        finally:
            conn.close()

    def test_integrity_allows_rejected_classification_history(self) -> None:
        seeded = self._seed_recording(
            "rec_rejected_class_history",
            "자료구조 강의에서 그래프를 다루는 자료구조 수업",
        )
        classification_id = self._seed_unique_classification(
            int(seeded["recording_id"]),
        )
        self._apply_current_plan(
            storage_keys=["rec_rejected_class_history"],
        )
        proposal_id = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]["id"]
        reject_title_suggestion(
            self.db_path,
            proposal_id,
            status_writes_enabled=True,
            allow_write=True,
        )

        conn = connect_v2(self.db_path)
        try:
            conn.execute(
                """
                UPDATE recording_classification_proposals
                SET status = 'rejected'
                WHERE id = ?
                """,
                (classification_id,),
            )
            conn.commit()
            issue_codes = {
                issue["code"]
                for issue in _title_suggestion_integrity_issues(conn)
            }
            self.assertNotIn(
                "title_suggestion_classification_link_invalid",
                issue_codes,
            )
        finally:
            conn.close()

    def test_cli_plan_and_guarded_apply_use_only_isolated_paths(self) -> None:
        self._seed_recording(
            "rec_cli",
            "보안 대시보드 보안 품질 대시보드 분류",
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = cli.main(
                [
                    "plan-title-suggestions",
                    "--v2-db",
                    str(self.db_path),
                    "--records-root",
                    str(self.records_root),
                    "--json",
                ]
            )
        self.assertEqual(exit_code, 0)
        plan = json.loads(stdout.getvalue())

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            refused = cli.main(
                [
                    "apply-title-suggestions",
                    "--v2-db",
                    str(self.db_path),
                    "--records-root",
                    str(self.records_root),
                    "--expected-count",
                    str(plan["expected_count"]),
                    "--expected-plan-sha256",
                    plan["plan_sha256"],
                    "--allow-write",
                    "--json",
                ]
            )
        self.assertEqual(refused, 2)
        self.assertIn("disabled", stderr.getvalue())

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            applied = cli.main(
                [
                    "apply-title-suggestions",
                    "--v2-db",
                    str(self.db_path),
                    "--records-root",
                    str(self.records_root),
                    "--expected-count",
                    str(plan["expected_count"]),
                    "--expected-plan-sha256",
                    plan["plan_sha256"],
                    "--enable-title-suggestions",
                    "--allow-write",
                    "--json",
                ]
            )
        self.assertEqual(applied, 0)
        self.assertEqual(json.loads(stdout.getvalue())["created"], 1)

        proposal_id = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]["id"]
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            confirmation_planned = cli.main(
                [
                    "plan-title-suggestion-confirmation",
                    "--v2-db",
                    str(self.db_path),
                    "--records-root",
                    str(self.records_root),
                    "--proposal-id",
                    str(proposal_id),
                    "--json",
                ]
            )
        self.assertEqual(confirmation_planned, 0)
        confirmation = json.loads(stdout.getvalue())

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            confirmation_applied = cli.main(
                [
                    "apply-title-suggestion-confirmation",
                    "--v2-db",
                    str(self.db_path),
                    "--records-root",
                    str(self.records_root),
                    "--proposal-id",
                    str(proposal_id),
                    "--expected-count",
                    "1",
                    "--expected-plan-sha256",
                    confirmation["plan_sha256"],
                    "--enable-confirmation",
                    "--allow-write",
                    "--json",
                ]
            )
        self.assertEqual(confirmation_applied, 0)
        self.assertEqual(
            json.loads(stdout.getvalue())["action"],
            "confirmed",
        )


if __name__ == "__main__":
    unittest.main()
