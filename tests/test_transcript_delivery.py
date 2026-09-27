from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.downstream import transcript_delivery as td  # noqa: E402
from lecture_stt.downstream.course_storage import (  # noqa: E402
    CourseStorageError,
    descendant_relative_path,
    ensure_course_storage_parent,
)
from lecture_stt.downstream.postprocess import (  # noqa: E402
    GENERATOR_BACKEND_FAKE,
    GeneratorSettings,
    PostprocessExecutionError,
    PostprocessValidationError,
)
from lecture_stt.downstream.semester import load_active_semester  # noqa: E402
from lecture_stt.shared import db as shared_db  # noqa: E402
from lecture_stt.storage_v2.timetable import (  # noqa: E402
    apply_timetable_import,
    plan_timetable_import,
)

CANONICAL_TEMP_ROOT = "/private/tmp" if sys.platform == "darwin" else tempfile.gettempdir()


class TranscriptPostprocessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(
            tempfile.mkdtemp(prefix="transcript-postprocess-", dir=CANONICAL_TEMP_ROOT)
        )
        self.addCleanup(shutil.rmtree, self.root, True)
        self.db_path = self.root / "jobs.sqlite3"
        self.conn = shared_db.init_db(str(self.db_path))
        self.addCleanup(self.conn.close)

        self.vault_root = self.root / "vault"
        self.semester_root = self.vault_root / "01_current"
        self.course_root = self.semester_root / "cs201"
        self.origin_dir = self.course_root / "06_lecture_notes/02_origin"
        self.summary_dir = self.course_root / "06_lecture_notes/01_summarize"
        self.correction_dir = self.root / "03_correction"
        self.summary_staging_dir = self.root / "04_summarize"
        self.course_correction_dir = self.correction_dir / "2026-1" / "cs201"
        self.course_summary_staging_dir = self.summary_staging_dir / "2026-1" / "cs201"
        (self.vault_root / ".obsidian").mkdir(parents=True)
        for path in (
            self.origin_dir,
            self.summary_dir,
            self.correction_dir,
            self.summary_staging_dir,
        ):
            path.mkdir(parents=True)

        self.timetable_db_path = self.root / "state" / "storage-v2.sqlite3"
        timetable_source = self.root / "timetable.csv"
        timetable_source.write_text(
            "학기,과목명,과목코드,요일,시작시간,종료시간,교시,강의실\n"
            "2026-1,Data Structures,CS201,목,10:00,11:15,2교시,E101\n",
            encoding="utf-8",
        )
        timetable_plan = plan_timetable_import(timetable_source)
        apply_timetable_import(
            timetable_source,
            self.timetable_db_path,
            expected_count=timetable_plan["expected_count"],
            expected_plan_sha256=timetable_plan["plan_sha256"],
            allow_write=True,
        )

        self.active_path = self.root / "active-semester.json"
        self._write_active_snapshot(
            semester_root=self.semester_root,
            course_dir="cs201",
            plan_sha256=timetable_plan["plan_sha256"],
        )
        load_active_semester(self.active_path)

        generator = GeneratorSettings(
            backend=GENERATOR_BACKEND_FAKE,
            codex_binary=None,
            model="test-generator",
            reasoning_effort="low",
            timeout_sec=30,
            max_attempts=3,
            temp_root=self.root / "tmp",
            max_correction_chars=100_000,
            max_summary_chars=100_000,
        )
        self.settings = td.TranscriptPostprocessSettings(
            active_semester_path=self.active_path,
            correction_staging_dir=self.correction_dir,
            summary_staging_dir=self.summary_staging_dir,
            generator=generator,
        )
        self.distributor = td.TranscriptDeliveryDistributor(
            self.conn,
            self.settings,
        )

    def _write_active_snapshot(
        self,
        *,
        semester_root: Path,
        course_dir: str,
        plan_sha256: str,
    ) -> None:
        payload = {
            "schema_version": "lecture-stt/active-semester@2",
            "activated_at": "2026-01-01T10:00:00+09:00",
            "activation_plan_sha256": "0" * 64,
            "manifest_source_path": str(self.root / "semester.yaml"),
            "manifest_source_sha256": "0" * 64,
            "expected_course_count": 1,
            "semester": "2026-1",
            "vault_root": str(self.vault_root),
            "semester_root": str(semester_root),
            "timetable_db_path": str(self.timetable_db_path),
            "origin_subdir": "06_lecture_notes/02_origin",
            "summary_subdir": "06_lecture_notes/01_summarize",
            "match_margin_minutes": 30,
            "courses": [
                {
                    "course_code": "CS201",
                    "course_name": "Data Structures",
                    "course_dir": course_dir,
                    "aliases": ["CA_2"],
                }
            ],
            "selected_timetable_courses": [
                {"course_code": "CS201", "course_name": "Data Structures"}
            ],
            "timetable_selection_plan_sha256": plan_sha256,
        }
        self.active_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _source_json() -> str:
        return json.dumps(
            {
                "text": "자료 구조를 설명합니다. 스택은 후입선출입니다.",
                "language": "ko",
                "segments": [
                    {
                        "id": 0,
                        "start": 0.0,
                        "end": 2.0,
                        "text": "자료 구조를 설명합니다.",
                    },
                    {
                        "id": 1,
                        "start": 2.0,
                        "end": 4.0,
                        "text": "스택은 후입선출입니다.",
                    },
                ],
            },
            ensure_ascii=False,
        )

    def _insert_done_job(
        self,
        *,
        canonical_base: str,
        recorded_at: str,
        profile_key: str = "LECTURE",
        source_suffix: str = "job",
        source_txt: str = "자료 구조를 설명합니다.\n스택은 후입선출입니다.\n",
        source_json: str | None = None,
        missing_source_file: bool = False,
        orig_name: str | None = None,
    ) -> tuple[int, Path, Path]:
        source_txt_path = self.root / f"{source_suffix}.txt"
        source_json_path = self.root / f"{source_suffix}.json"
        if not missing_source_file:
            source_txt_path.write_text(source_txt, encoding="utf-8")
            source_json_path.write_text(
                source_json if source_json is not None else self._source_json(),
                encoding="utf-8",
            )
        engine_params = {
            "metadata": {"recorded_at": recorded_at},
            "profile": {"key": profile_key},
        }
        cursor = self.conn.execute(
            """
            INSERT INTO jobs(
                status, created_at, updated_at, orig_inbox_path, orig_name,
                canonical_base, canonical_audio_path, transcript_txt_path,
                transcript_json_path, engine_params, started_at, ended_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "DONE",
                recorded_at,
                recorded_at,
                str(self.root / "inbox"),
                orig_name or f"{canonical_base}.m4a",
                canonical_base,
                str(self.root / f"{source_suffix}.wav"),
                str(source_txt_path),
                str(source_json_path),
                json.dumps(engine_params, ensure_ascii=False),
                recorded_at,
                recorded_at,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid), source_txt_path, source_json_path

    def _insert_invalid_done_job(
        self,
        *,
        canonical_base: str,
        source_suffix: str,
    ) -> tuple[int, Path, Path]:
        source_txt_path = self.root / f"{source_suffix}.txt"
        source_json_path = self.root / f"{source_suffix}.json"
        source_txt_path.write_text("broken", encoding="utf-8")
        source_json_path.write_text("{}",
                                   encoding="utf-8")
        engine_params = {}
        cursor = self.conn.execute(
            """
            INSERT INTO jobs(
                status, created_at, updated_at, orig_inbox_path, orig_name,
                canonical_base, canonical_audio_path, transcript_txt_path,
                transcript_json_path, engine_params, started_at, ended_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "DONE",
                "invalid-timestamp",
                "invalid-timestamp",
                str(self.root / "inbox"),
                f"{canonical_base}.m4a",
                canonical_base,
                str(self.root / f"{source_suffix}.wav"),
                str(source_txt_path),
                str(source_json_path),
                json.dumps(engine_params, ensure_ascii=False),
                "invalid-timestamp",
                "invalid-timestamp",
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid), source_txt_path, source_json_path

    def _queue_job(
        self,
        *,
        canonical_base: str = "260101CA_2",
        recorded_at: str = "2026-01-01T10:30:00+09:00",
        profile_key: str = "LECTURE",
        source_suffix: str = "job",
        missing_source_file: bool = False,
    ) -> tuple[int, object]:
        source_job_id, _, _ = self._insert_done_job(
            canonical_base=canonical_base,
            recorded_at=recorded_at,
            profile_key=profile_key,
            source_suffix=source_suffix,
            missing_source_file=missing_source_file,
        )
        td.enqueue_completed_job(
            self.conn,
            source_job_id=source_job_id,
            settings=self.settings,
        )
        return (
            source_job_id,
            shared_db.get_transcript_postprocess_job(self.conn, source_job_id),
        )

    def test_routes_alias_and_pins_all_postprocess_paths(self) -> None:
        _, row = self._queue_job(source_suffix="alias")

        self.assertEqual(row["status"], td.TRANSCRIPT_DELIVERY_PENDING)
        self.assertEqual(row["route_method"], "filename_alias")
        self.assertEqual(row["course_code"], "CS201")
        self.assertEqual(Path(row["manifest_source_path"]), self.root / "semester.yaml")
        self.assertEqual(row["manifest_source_sha256"], "0" * 64)
        self.assertEqual(Path(row["origin_dir_path"]), self.origin_dir)
        self.assertEqual(Path(row["summary_dir_path"]), self.summary_dir)
        self.assertEqual(
            Path(row["correction_stage_txt_path"]),
            self.course_correction_dir / "260101CA_2.txt",
        )
        self.assertEqual(
            Path(row["summary_stage_md_path"]),
            self.course_summary_staging_dir / "260101CA_2.md",
        )

    def test_orig_name_route_keeps_canonical_logical_stem(self) -> None:
        canonical_base = "260101recording__120000__abc123"
        source_job_id, _, _ = self._insert_done_job(
            canonical_base=canonical_base,
            orig_name="260101CA_2.m4a",
            recorded_at="2026-01-01T10:30:00+09:00",
            source_suffix="orig-name-route",
        )

        status = td.enqueue_completed_job(
            self.conn,
            source_job_id=source_job_id,
            settings=self.settings,
        )
        row = shared_db.get_transcript_postprocess_job(self.conn, source_job_id)

        self.assertEqual(status, td.TRANSCRIPT_DELIVERY_PENDING)
        self.assertEqual(row["route_method"], "filename_alias")
        self.assertEqual(row["logical_stem"], canonical_base)
        self.assertTrue(
            str(row["correction_stage_txt_path"]).endswith(f"/{canonical_base}.txt")
        )

    def test_timetable_fallback_preserves_distinct_canonical_stems(self) -> None:
        canonical_bases = ("260101recording-a", "260101recording-b")
        source_job_ids = []
        for index, canonical_base in enumerate(canonical_bases, start=1):
            source_job_id, _, _ = self._insert_done_job(
                canonical_base=canonical_base,
                recorded_at="2026-01-01T10:30:00+09:00",
                source_suffix=f"timetable-fallback-{index}",
            )
            td.enqueue_completed_job(
                self.conn,
                source_job_id=source_job_id,
                settings=self.settings,
            )
            source_job_ids.append(source_job_id)

        rows = [
            shared_db.get_transcript_postprocess_job(self.conn, source_job_id)
            for source_job_id in source_job_ids
        ]
        self.assertEqual(
            [row["route_method"] for row in rows],
            ["timetable_unique_match", "timetable_unique_match"],
        )
        self.assertEqual([row["logical_stem"] for row in rows], list(canonical_bases))

    def test_nonlecture_is_skipped_and_unmatched_lecture_is_unrouted(self) -> None:
        _, skipped = self._queue_job(
            canonical_base="260101GENERAL",
            profile_key="GENERAL",
            source_suffix="general",
        )
        _, unrouted = self._queue_job(
            canonical_base="260101LECTURE",
            recorded_at="2026-01-01T23:00:00+09:00",
            source_suffix="unrouted",
        )

        self.assertEqual(skipped["status"], td.TRANSCRIPT_DELIVERY_SKIPPED)
        self.assertEqual(unrouted["status"], td.TRANSCRIPT_DELIVERY_UNROUTED)
        self.assertEqual(unrouted["last_error_code"], "UNROUTED_TIMETABLE")

    def test_pre_activation_job_is_not_queued(self) -> None:
        source_job_id, _, _ = self._insert_done_job(
            canonical_base="260101CA_2",
            recorded_at="2026-01-01T09:59:59+09:00",
            source_suffix="early",
        )

        result = td.enqueue_completed_job(
            self.conn,
            source_job_id=source_job_id,
            settings=self.settings,
        )

        self.assertIsNone(result)
        self.assertIsNone(
            shared_db.get_transcript_postprocess_job(self.conn, source_job_id)
        )

    def test_reconcile_respects_cutoff_and_isolates_missing_source(self) -> None:
        self._insert_done_job(
            canonical_base="260101CA_2",
            recorded_at="2026-01-01T09:59:59+09:00",
            source_suffix="early-reconcile",
        )
        good_id, _, _ = self._insert_done_job(
            canonical_base="260101CA_2",
            recorded_at="2026-01-01T10:30:00+09:00",
            source_suffix="good-reconcile",
        )
        broken_id, _, _ = self._insert_done_job(
            canonical_base="260101CA_2",
            recorded_at="2026-01-01T10:45:00+09:00",
            source_suffix="broken-reconcile",
            missing_source_file=True,
        )

        result = td.reconcile_completed_jobs(
            self.conn,
            settings=self.settings,
        )

        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["errors"], 1)
        by_id = {item["source_job_id"]: item for item in result["results"]}
        self.assertEqual(by_id[good_id]["status"], td.TRANSCRIPT_DELIVERY_PENDING)
        self.assertEqual(by_id[broken_id]["status"], "error")

    def test_reconcile_skips_invalid_completion_timestamp_and_queues_valid_row(self) -> None:
        malformed_id, _, _ = self._insert_invalid_done_job(
            canonical_base="260101CA_3",
            source_suffix="malformed-reconcile",
        )
        valid_id, _, _ = self._insert_done_job(
            canonical_base="260101CA_2",
            recorded_at="2026-01-01T10:30:00+09:00",
            source_suffix="good-reconcile-2",
        )

        result = td.reconcile_completed_jobs(
            self.conn,
            settings=self.settings,
        )

        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["errors"], 1)
        by_id = {item["source_job_id"]: item for item in result["results"]}
        self.assertEqual(by_id[malformed_id]["status"], "error")
        self.assertEqual(
            by_id[malformed_id]["error_code"],
            "INVALID_COMPLETION_TIMESTAMP",
        )
        self.assertEqual(by_id[valid_id]["status"], td.TRANSCRIPT_DELIVERY_PENDING)

    def test_reconcile_dry_run_does_not_create_queue_row(self) -> None:
        source_job_id, _, _ = self._insert_done_job(
            canonical_base="260101CA_2",
            recorded_at="2026-01-01T10:30:00+09:00",
            source_suffix="dry-reconcile",
        )

        result = td.reconcile_completed_jobs(
            self.conn,
            settings=self.settings,
            dry_run=True,
        )

        self.assertEqual(result["queued"], 1)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["results"][0]["course_code"], "CS201")
        self.assertIsNone(
            shared_db.get_transcript_postprocess_job(self.conn, source_job_id)
        )

    def test_reconcile_limit_does_not_starve_valid_row_behind_broken_rows(self) -> None:
        broken_ids = []
        for index in range(3):
            source_job_id, _, _ = self._insert_done_job(
                canonical_base=f"260101broken-{index}",
                recorded_at="2026-01-01T10:30:00+09:00",
                source_suffix=f"starvation-broken-{index}",
                missing_source_file=True,
            )
            broken_ids.append(source_job_id)
        valid_id, _, _ = self._insert_done_job(
            canonical_base="260101valid-later",
            recorded_at="2026-01-01T10:30:00+09:00",
            source_suffix="starvation-valid",
        )

        result = td.reconcile_completed_jobs(
            self.conn,
            settings=self.settings,
            limit=2,
        )

        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["errors"], len(broken_ids))
        self.assertIsNotNone(
            shared_db.get_transcript_postprocess_job(self.conn, valid_id)
        )

    def _load_legacy_compat_settings(
        self,
        legacy_activation_cutoff: str,
    ) -> td.TranscriptPostprocessSettings:
        config = {
            "active_semester_manifest": str(self.active_path),
            "correction_staging_dir": str(self.correction_dir),
            "summary_staging_dir": str(self.summary_staging_dir),
            "activation_cutoff": legacy_activation_cutoff,
            "generator": {
                "backend": "fake",
                "model": "test",
                "reasoning_effort": "low",
                "timeout_sec": 30,
                "max_attempts": 3,
                "max_correction_chars": 100_000,
                "max_summary_chars": 100_000,
            },
        }
        return td.load_postprocess_settings(
            config,
            base_dir=self.root,
            env={},
            default_tmp_root=self.root / "tmp",
            allow_fake_backend=True,
        )

    def test_legacy_activation_cutoff_in_config_is_ignored_and_snapshot_cutoff_is_pinned(self) -> None:
        settings = self._load_legacy_compat_settings(
            legacy_activation_cutoff="2026-01-01T11:00:00+09:00"
        )
        source_job_id, _, _ = self._insert_done_job(
            canonical_base="260101CA_2",
            recorded_at="2026-01-01T10:30:00+09:00",
            source_suffix="legacy-cutoff",
        )

        status = td.enqueue_completed_job(
            self.conn,
            source_job_id=source_job_id,
            settings=settings,
        )
        current = shared_db.get_transcript_postprocess_job(self.conn, source_job_id)

        self.assertEqual(status, td.TRANSCRIPT_DELIVERY_PENDING)
        self.assertIsNotNone(current)
        self.assertEqual(current["activation_cutoff"], "2026-01-01T10:00:00+09:00")
        self.assertNotEqual(current["activation_cutoff"], "2026-01-01T11:00:00+09:00")

    def test_full_pipeline_stages_correction_and_summary_then_delivers(self) -> None:
        source_job_id, row = self._queue_job(source_suffix="complete")
        source_txt = Path(row["source_txt_path"])
        source_json = Path(row["source_json_path"])
        source_txt_before = source_txt.read_bytes()
        source_json_before = source_json.read_bytes()

        result = self.distributor.process_pending()
        current = shared_db.get_transcript_postprocess_job(self.conn, source_job_id)

        self.assertEqual(result["delivered"], 1)
        self.assertEqual(current["status"], td.TRANSCRIPT_DELIVERY_DELIVERED)
        self.assertEqual(current["correction_status"], td.STAGE_READY)
        self.assertEqual(current["summary_status"], td.STAGE_READY)
        self.assertEqual(source_txt.read_bytes(), source_txt_before)
        self.assertEqual(source_json.read_bytes(), source_json_before)

        corrected_txt = self.course_correction_dir / "260101CA_2.txt"
        corrected_json = self.course_correction_dir / "260101CA_2.json"
        staged_summary = self.course_summary_staging_dir / "260101CA_2.md"
        final_txt = self.origin_dir / "260101CA_2.txt"
        final_json = self.origin_dir / "260101CA_2.json"
        final_summary = self.summary_dir / "260101CA_2.md"
        for path in (
            corrected_txt,
            corrected_json,
            staged_summary,
            final_txt,
            final_json,
            final_summary,
        ):
            self.assertTrue(path.is_file(), path)
        self.assertEqual(final_txt.read_bytes(), corrected_txt.read_bytes())
        self.assertEqual(final_json.read_bytes(), corrected_json.read_bytes())
        self.assertEqual(final_summary.read_bytes(), staged_summary.read_bytes())
        self.assertIn("## 주요 개념", final_summary.read_text(encoding="utf-8"))
        self.assertEqual(self.distributor.process_pending()["processed"], 0)

    def test_existing_flat_stage_paths_remain_pinned_and_process_in_place(self) -> None:
        source_job_id, row = self._queue_job(source_suffix="legacy-flat")
        logical_stem = str(row["logical_stem"])
        flat_txt = self.correction_dir / f"{logical_stem}.txt"
        flat_json = self.correction_dir / f"{logical_stem}.json"
        flat_summary = self.summary_staging_dir / f"{logical_stem}.md"
        shared_db.update_transcript_postprocess_job(
            self.conn,
            source_job_id,
            correction_stage_txt_path=str(flat_txt),
            correction_stage_json_path=str(flat_json),
            summary_stage_md_path=str(flat_summary),
        )

        status = td.enqueue_completed_job(
            self.conn,
            source_job_id=source_job_id,
            settings=self.settings,
        )
        result = self.distributor.process_pending()

        self.assertEqual(status, td.TRANSCRIPT_DELIVERY_PENDING)
        self.assertEqual(result["delivered"], 1)
        self.assertTrue(flat_txt.is_file())
        self.assertTrue(flat_json.is_file())
        self.assertTrue(flat_summary.is_file())
        self.assertFalse((self.correction_dir / "2026-1").exists())
        self.assertFalse((self.summary_staging_dir / "2026-1").exists())

    def test_destination_conflict_refuses_overwrite(self) -> None:
        source_job_id, _ = self._queue_job(source_suffix="conflict")
        destination = self.origin_dir / "260101CA_2.txt"
        destination.write_text("do not overwrite", encoding="utf-8")

        result = self.distributor.process_pending()
        current = shared_db.get_transcript_postprocess_job(self.conn, source_job_id)

        self.assertEqual(result["conflict"], 1)
        self.assertEqual(current["status"], td.TRANSCRIPT_DELIVERY_CONFLICT)
        self.assertEqual(destination.read_text(encoding="utf-8"), "do not overwrite")
        self.assertFalse((self.origin_dir / "260101CA_2.json").exists())

    def test_source_drift_is_terminal_and_source_is_not_rewritten(self) -> None:
        source_job_id, row = self._queue_job(source_suffix="drift")
        source_path = Path(row["source_txt_path"])
        source_path.write_text("changed after queue", encoding="utf-8")

        result = self.distributor.process_pending()
        current = shared_db.get_transcript_postprocess_job(self.conn, source_job_id)

        self.assertEqual(result["error"], 1)
        self.assertEqual(current["status"], td.TRANSCRIPT_DELIVERY_ERROR)
        self.assertEqual(current["last_error_code"], "SOURCE_TXT_DRIFT")
        self.assertEqual(source_path.read_text(encoding="utf-8"), "changed after queue")
        self.assertEqual(list(self.origin_dir.iterdir()), [])

    def test_codex_execution_retries_then_becomes_terminal(self) -> None:
        source_job_id, _ = self._queue_job(source_suffix="retry")

        with mock.patch.object(
            td,
            "generate_correction",
            side_effect=PostprocessExecutionError("transient"),
        ):
            statuses = [
                self.distributor.process_pending()["results"][0]["status"]
                for _ in range(3)
            ]

        current = shared_db.get_transcript_postprocess_job(self.conn, source_job_id)
        self.assertEqual(
            statuses,
            [
                td.TRANSCRIPT_DELIVERY_PENDING,
                td.TRANSCRIPT_DELIVERY_PENDING,
                td.TRANSCRIPT_DELIVERY_ERROR,
            ],
        )
        self.assertEqual(current["correction_attempt_count"], 3)
        self.assertEqual(current["last_error_code"], "CODEX_EXEC_FAILED")

    def test_invalid_generated_content_requires_review_without_delivery(self) -> None:
        source_job_id, _ = self._queue_job(source_suffix="needs-review")

        with mock.patch.object(
            td,
            "generate_correction",
            side_effect=PostprocessValidationError("bad ids"),
        ):
            result = self.distributor.process_pending()

        current = shared_db.get_transcript_postprocess_job(self.conn, source_job_id)
        self.assertEqual(result["needs_review"], 1)
        self.assertEqual(current["status"], td.TRANSCRIPT_DELIVERY_NEEDS_REVIEW)
        self.assertEqual(current["correction_status"], td.TRANSCRIPT_DELIVERY_NEEDS_REVIEW)
        self.assertEqual(list(self.origin_dir.iterdir()), [])

    def test_summary_failure_retries_from_ready_correction(self) -> None:
        source_job_id, _ = self._queue_job(source_suffix="summary-retry")

        with mock.patch.object(
            td,
            "generate_summary",
            side_effect=PostprocessExecutionError("transient"),
        ):
            first = self.distributor.process_pending()

        current = shared_db.get_transcript_postprocess_job(self.conn, source_job_id)
        self.assertEqual(first["results"][0]["status"], td.TRANSCRIPT_DELIVERY_PENDING)
        self.assertEqual(current["correction_status"], td.STAGE_READY)
        self.assertEqual(current["summary_status"], td.STAGE_PENDING)
        self.assertEqual(current["correction_attempt_count"], 1)
        self.assertEqual(current["summary_attempt_count"], 1)
        self.assertTrue((self.course_correction_dir / "260101CA_2.txt").is_file())
        self.assertEqual(list(self.origin_dir.iterdir()), [])

    def test_json_first_correction_stage_is_adopted_after_crash_window(self) -> None:
        source_job_id, row = self._queue_job(source_suffix="correction-crash")
        source_payload = json.loads(Path(row["source_json_path"]).read_text(encoding="utf-8"))
        correction = td.generate_correction(
            self.settings.generator,
            logical_stem=str(row["logical_stem"]),
            course_name=str(row["course_name"]),
            source_json_payload=source_payload,
        )
        txt_sha = hashlib.sha256(correction.transcript_txt_bytes).hexdigest()
        json_sha = hashlib.sha256(correction.transcript_json_bytes).hexdigest()
        shared_db.update_transcript_postprocess_job(
            self.conn,
            source_job_id,
            correction_stage_txt_sha256=txt_sha,
            correction_stage_json_sha256=json_sha,
        )
        Path(row["correction_stage_json_path"]).parent.mkdir(parents=True)
        Path(row["correction_stage_json_path"]).write_bytes(
            correction.transcript_json_bytes
        )

        with mock.patch.object(
            td,
            "generate_correction",
            side_effect=AssertionError("correction must be adopted, not regenerated"),
        ):
            result = self.distributor.process_pending()

        current = shared_db.get_transcript_postprocess_job(self.conn, source_job_id)
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(current["status"], td.TRANSCRIPT_DELIVERY_DELIVERED)
        self.assertEqual(current["correction_attempt_count"], 0)
        self.assertEqual(
            Path(row["correction_stage_txt_path"]).read_bytes(),
            correction.transcript_txt_bytes,
        )

    def test_summary_stage_is_adopted_after_crash_window(self) -> None:
        source_job_id, row = self._queue_job(source_suffix="summary-crash")
        source_payload = json.loads(Path(row["source_json_path"]).read_text(encoding="utf-8"))
        correction = td.generate_correction(
            self.settings.generator,
            logical_stem=str(row["logical_stem"]),
            course_name=str(row["course_name"]),
            source_json_payload=source_payload,
        )
        Path(row["correction_stage_json_path"]).parent.mkdir(parents=True)
        Path(row["summary_stage_md_path"]).parent.mkdir(parents=True)
        Path(row["correction_stage_json_path"]).write_bytes(
            correction.transcript_json_bytes
        )
        Path(row["correction_stage_txt_path"]).write_bytes(
            correction.transcript_txt_bytes
        )
        shared_db.update_transcript_postprocess_job(
            self.conn,
            source_job_id,
            correction_status=td.STAGE_READY,
            summary_status=td.STAGE_PENDING,
            correction_stage_txt_sha256=hashlib.sha256(
                correction.transcript_txt_bytes
            ).hexdigest(),
            correction_stage_json_sha256=hashlib.sha256(
                correction.transcript_json_bytes
            ).hexdigest(),
        )
        summary = td.generate_summary(
            self.settings.generator,
            logical_stem=str(row["logical_stem"]),
            course_name=str(row["course_name"]),
            corrected_text=correction.transcript_text,
        )
        Path(row["summary_stage_md_path"]).write_bytes(summary.markdown_bytes)
        shared_db.update_transcript_postprocess_job(
            self.conn,
            source_job_id,
            summary_stage_md_sha256=hashlib.sha256(summary.markdown_bytes).hexdigest(),
        )

        with mock.patch.object(
            td,
            "generate_summary",
            side_effect=AssertionError("summary must be adopted, not regenerated"),
        ):
            result = self.distributor.process_pending()

        current = shared_db.get_transcript_postprocess_job(self.conn, source_job_id)
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(current["status"], td.TRANSCRIPT_DELIVERY_DELIVERED)
        self.assertEqual(current["summary_attempt_count"], 0)

    def test_unpinned_preexisting_stage_file_is_a_conflict(self) -> None:
        source_job_id, row = self._queue_job(source_suffix="foreign-stage")
        Path(row["correction_stage_json_path"]).parent.mkdir(parents=True)
        Path(row["correction_stage_json_path"]).write_text("{}", encoding="utf-8")

        result = self.distributor.process_pending()
        current = shared_db.get_transcript_postprocess_job(self.conn, source_job_id)

        self.assertEqual(result["conflict"], 1)
        self.assertEqual(current["status"], td.TRANSCRIPT_DELIVERY_CONFLICT)

    def test_queued_route_survives_active_snapshot_change(self) -> None:
        source_job_id, _ = self._queue_job(source_suffix="pinned")
        new_semester_root = self.vault_root / "02_next"
        new_notes = new_semester_root / "new-course" / "06_lecture_notes"
        (new_notes / "02_origin").mkdir(parents=True)
        (new_notes / "01_summarize").mkdir(parents=True)
        active_payload = json.loads(self.active_path.read_text(encoding="utf-8"))
        active_payload["semester_root"] = str(new_semester_root)
        active_payload["courses"][0]["course_dir"] = "new-course"
        self.active_path.write_text(json.dumps(active_payload), encoding="utf-8")

        result = self.distributor.process_pending()
        current = shared_db.get_transcript_postprocess_job(self.conn, source_job_id)

        self.assertEqual(result["delivered"], 1)
        self.assertEqual(Path(current["origin_dir_path"]), self.origin_dir)
        self.assertTrue((self.origin_dir / "260101CA_2.txt").is_file())
        self.assertEqual(list((new_notes / "02_origin").iterdir()), [])

    def test_dry_run_does_not_mutate_queue_or_files(self) -> None:
        source_job_id, _ = self._queue_job(source_suffix="dry-process")
        before = dict(shared_db.get_transcript_postprocess_job(self.conn, source_job_id))
        dry_distributor = td.TranscriptDeliveryDistributor(
            self.conn,
            self.settings,
            dry_run=True,
        )

        result = dry_distributor.process_pending()
        after = dict(shared_db.get_transcript_postprocess_job(self.conn, source_job_id))

        self.assertEqual(result["processed"], 1)
        self.assertEqual(before, after)
        self.assertEqual(list(self.correction_dir.iterdir()), [])
        self.assertEqual(list(self.summary_staging_dir.iterdir()), [])
        self.assertEqual(list(self.origin_dir.iterdir()), [])
        self.assertFalse((self.correction_dir / "2026-1").exists())
        self.assertFalse((self.summary_staging_dir / "2026-1").exists())

    def test_course_scoped_parent_rejects_symlink_and_non_directory_components(self) -> None:
        blocked_root = self.root / "blocked"
        blocked_root.mkdir()
        (blocked_root / "2026-1").write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(CourseStorageError, "directory components"):
            ensure_course_storage_parent(
                blocked_root,
                semester="2026-1",
                course_dir="cs201",
                field="blocked_root",
                create=True,
            )

        symlink_root = self.root / "symlinked"
        symlink_root.mkdir()
        real_target = self.root / "real-course"
        real_target.mkdir()
        (symlink_root / "2026-1").mkdir()
        (symlink_root / "2026-1" / "cs201").symlink_to(real_target, target_is_directory=True)
        with self.assertRaisesRegex(CourseStorageError, "symlink components"):
            ensure_course_storage_parent(
                symlink_root,
                semester="2026-1",
                course_dir="cs201",
                field="symlink_root",
                create=False,
            )

    def test_descendant_path_accepts_equivalent_macos_root_alias(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("macOS /tmp alias is unavailable")
        canonical_root = self.root / "alias-root"
        (canonical_root / "2026-1" / "cs201").mkdir(parents=True)
        alias_root = Path(str(canonical_root).replace("/private/tmp/", "/tmp/", 1))
        if alias_root.resolve() != canonical_root.resolve():
            self.skipTest("macOS /tmp alias is unavailable")

        relative = descendant_relative_path(
            canonical_root,
            alias_root / "2026-1" / "cs201" / "lecture.json",
            field="transcript_path",
            expected_filename="lecture.json",
        )

        self.assertEqual(relative, "2026-1/cs201/lecture.json")

    def test_fake_backend_cannot_be_enabled_from_config(self) -> None:
        config = {
            "active_semester_manifest": str(self.active_path),
            "activation_cutoff": "2026-01-01T10:00:00+09:00",
            "correction_staging_dir": str(self.correction_dir),
            "summary_staging_dir": str(self.summary_staging_dir),
            "generator": {
                "backend": "fake",
                "allow_fake_backend": True,
                "model": "test",
                "reasoning_effort": "low",
                "timeout_sec": 30,
                "max_attempts": 3,
                "max_correction_chars": 100_000,
                "max_summary_chars": 100_000,
            },
        }

        with self.assertRaisesRegex(td.TranscriptDeliveryError, "internal tests"):
            td.load_postprocess_settings(
                config,
                base_dir=self.root,
                env={},
                default_tmp_root=self.root / "tmp",
            )

    def test_readonly_open_refuses_missing_database(self) -> None:
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(td.TranscriptDeliveryUnsafePathError):
            td.open_delivery_db(missing, readonly=True)
        self.assertFalse(missing.exists())

    def test_exclusive_fallback_never_overwrites_existing_file(self) -> None:
        destination = self.root / "existing.txt"
        destination.write_bytes(b"original")
        with mock.patch.object(
            td.os,
            "link",
            side_effect=OSError(errno.EXDEV, "cross-device link"),
        ):
            with self.assertRaises(FileExistsError):
                td._write_bytes_no_overwrite(b"replacement", destination)
        self.assertEqual(destination.read_bytes(), b"original")

    def test_no_overwrite_output_is_private_under_common_umask(self) -> None:
        destination = self.root / "private-output.txt"
        observed_temp_modes: list[int] = []
        original_link = os.link

        def link_and_capture_mode(src: Path, dst: Path) -> None:
            observed_temp_modes.append(stat.S_IMODE(Path(src).stat().st_mode))
            original_link(src, dst)

        previous_umask = os.umask(0o022)
        try:
            with mock.patch.object(td.os, "link", side_effect=link_and_capture_mode):
                td._write_bytes_no_overwrite(b"private", destination)
        finally:
            os.umask(previous_umask)

        self.assertEqual(observed_temp_modes, [0o600])
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
