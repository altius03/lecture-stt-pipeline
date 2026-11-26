from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.downstream.semester import (  # noqa: E402
    SemesterApplyConflictError,
    SemesterManifestError,
    load_active_semester,
    apply_semester_manifest,
    plan_semester_manifest,
)
from lecture_stt.storage_v2.timetable import apply_timetable_import, plan_timetable_import  # noqa: E402
from lecture_stt.shared import db as shared_db  # noqa: E402

CANONICAL_TEMP_ROOT = "/private/tmp" if sys.platform == "darwin" else tempfile.gettempdir()


class SemesterManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(
            tempfile.mkdtemp(prefix="semester-manifest-", dir=CANONICAL_TEMP_ROOT)
        )
        self.vault_root = self.root / "vault"
        (self.vault_root / ".obsidian").mkdir(parents=True)
        self.semester_root = self.vault_root / "01_current"
        self.semester_root.mkdir(parents=True)
        self.timetable_db_path = self.root / "state" / "storage-v2.sqlite3"
        self.jobs_db_path = self.root / "state" / "jobs.sqlite3"

        source = self.root / "timetable.csv"
        source.write_text(
            "학기,과목명,과목코드,요일,시작시간,종료시간,교시,강의실\n"
            "2026-1,Data Structures,CS201,월,10:00,11:15,2교시,E101\n"
            "2026-1,Algorithms,AL201,수,14:00,15:15,4교시,E102\n",
            encoding="utf-8",
        )
        plan = plan_timetable_import(source)
        apply_timetable_import(
            source,
            self.timetable_db_path,
            expected_count=plan["expected_count"],
            expected_plan_sha256=plan["plan_sha256"],
            allow_write=True,
        )
        self.jobs_conn = shared_db.init_db(str(self.jobs_db_path))
        self.addCleanup(self.jobs_conn.close)

        for course_dir in ("cs201", "al201"):
            course_root = self.semester_root / course_dir
            (course_root / "06_lecture_notes/02_origin").mkdir(parents=True)
            (course_root / "06_lecture_notes/01_summarize").mkdir(parents=True)

    def _write_manifest(self, *, courses: list[dict[str, object]]) -> Path:
        manifest = {
            "schema_version": "lecture-stt/semester-manifest@2",
            "semester": "2026-1",
            "vault_root": str(self.vault_root),
            "semester_root": str(self.semester_root),
            "timetable_db_path": str(self.timetable_db_path),
            "origin_subdir": "06_lecture_notes/02_origin",
            "summary_subdir": "06_lecture_notes/01_summarize",
            "match_margin_minutes": 30,
            "courses": courses,
        }
        manifest_path = self.root / "semester.yaml"
        manifest_path.write_text(
            yaml.safe_dump(manifest, allow_unicode=True),
            encoding="utf-8",
        )
        return manifest_path

    def _active_snapshot_path(self, *, existing_parent: bool = True) -> Path:
        if existing_parent:
            (self.root / "state").mkdir(exist_ok=True)
            return self.root / "state" / "active-semester.json"
        return self.root / "state" / "missing-parent" / "active-semester.json"

    def _write_active_snapshot(
        self, *, activated_at: str = "2026-01-01T10:00:00+09:00"
    ) -> Path:
        active_path = self._active_snapshot_path()
        payload = {
            "schema_version": "lecture-stt/active-semester@2",
            "activated_at": activated_at,
            "activation_plan_sha256": "0" * 64,
            "manifest_source_path": str(self.root / "semester.yaml"),
            "manifest_source_sha256": "0" * 64,
            "expected_course_count": 2,
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
                },
                {
                    "course_code": "AL201",
                    "course_name": "Algorithms",
                    "course_dir": "al201",
                    "aliases": [],
                },
            ],
            "selected_timetable_courses": [
                {"course_code": "CS201", "course_name": "Data Structures"},
                {"course_code": "AL201", "course_name": "Algorithms"},
            ],
            "timetable_selection_plan_sha256": "0" * 64,
        }
        active_path.write_text(json.dumps(payload), encoding="utf-8")
        return active_path

    def _insert_done_job(self, *, job_id: int, completed_at: str, source_tag: str) -> None:
        self.jobs_conn.execute(
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
                str(self.root / f"{source_tag}.m4a"),
                f"{source_tag}.m4a",
                source_tag,
                str(self.root / f"{source_tag}.wav"),
                completed_at,
                json.dumps({"timings": {"ended_at": completed_at}}, ensure_ascii=False),
            ),
        )
        self.jobs_conn.commit()

    def _insert_invalid_done_job(self, *, job_id: int, source_tag: str) -> None:
        self.jobs_conn.execute(
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
                str(self.root / f"{source_tag}.m4a"),
                f"{source_tag}.m4a",
                source_tag,
                str(self.root / f"{source_tag}.wav"),
                "invalid-timestamp",
                "{}",
            ),
        )
        self.jobs_conn.commit()

    def test_plan_and_apply_guards_enforced(self) -> None:
        manifest_path = self._write_manifest(
            courses=[
                {
                    "course_code": "CS201",
                    "course_name": "Data Structures",
                    "course_dir": "cs201",
                    "aliases": ["CA_2"],
                },
                {
                    "course_code": "AL201",
                    "course_name": "Algorithms",
                    "course_dir": "al201",
                    "aliases": [],
                },
            ]
        )
        plan = plan_semester_manifest(manifest_path)
        active_path = self._active_snapshot_path()

        with self.assertRaisesRegex(
            SemesterApplyConflictError,
            "requires --allow-write",
        ):
            apply_semester_manifest(
                manifest_path,
                expected_course_count=plan["expected_course_count"],
                expected_plan_sha256=plan["plan_sha256"],
                allow_write=False,
                timetable_db_path=self.timetable_db_path,
                jobs_db_path=self.jobs_db_path,
                active_path=active_path,
            )

        with self.assertRaisesRegex(
            SemesterApplyConflictError,
            "expected_course_count does not match the current plan",
        ):
            apply_semester_manifest(
                manifest_path,
                expected_course_count=plan["expected_course_count"] + 1,
                expected_plan_sha256=plan["plan_sha256"],
                allow_write=True,
                timetable_db_path=self.timetable_db_path,
                jobs_db_path=self.jobs_db_path,
                active_path=active_path,
            )

        with self.assertRaisesRegex(
            SemesterApplyConflictError,
            "expected_plan_sha256 must be a canonical SHA-256",
        ):
            apply_semester_manifest(
                manifest_path,
                expected_course_count=plan["expected_course_count"],
                expected_plan_sha256="bad-hash",
                allow_write=True,
                timetable_db_path=self.timetable_db_path,
                jobs_db_path=self.jobs_db_path,
                active_path=active_path,
            )

        with self.assertRaisesRegex(
            SemesterApplyConflictError,
            "expected_plan_sha256 does not match the current plan",
        ):
            apply_semester_manifest(
                manifest_path,
                expected_course_count=plan["expected_course_count"],
                expected_plan_sha256="a" * 64,
                allow_write=True,
                timetable_db_path=self.timetable_db_path,
                jobs_db_path=self.jobs_db_path,
                active_path=active_path,
            )

        result = apply_semester_manifest(
            manifest_path,
            expected_course_count=plan["expected_course_count"],
            expected_plan_sha256=plan["plan_sha256"],
            allow_write=True,
            timetable_db_path=self.timetable_db_path,
            jobs_db_path=self.jobs_db_path,
            active_path=active_path,
        )

        self.assertTrue(result["ok"])
        active = json.loads(active_path.read_text(encoding="utf-8"))
        self.assertEqual(active["schema_version"], "lecture-stt/active-semester@2")
        self.assertEqual(active["semester"], "2026-1")
        self.assertEqual(result["active_path"], str(active_path.resolve()))

    def test_plan_requires_exact_timetable_course_set(self) -> None:
        manifest_path_missing = self._write_manifest(
            courses=[
                {
                    "course_code": "CS201",
                    "course_name": "Data Structures",
                    "course_dir": "cs201",
                    "aliases": [],
                }
            ]
        )
        with self.assertRaisesRegex(
            SemesterManifestError,
            "selected timetable.*set",
        ):
            plan_semester_manifest(manifest_path_missing)

        ph_root = self.semester_root / "ph201" / "06_lecture_notes"
        (ph_root / "02_origin").mkdir(parents=True)
        (ph_root / "01_summarize").mkdir(parents=True)
        manifest_path_extra = self._write_manifest(
            courses=[
                {
                    "course_code": "CS201",
                    "course_name": "Data Structures",
                    "course_dir": "cs201",
                    "aliases": [],
                },
                {
                    "course_code": "AL201",
                    "course_name": "Algorithms",
                    "course_dir": "al201",
                    "aliases": [],
                },
                {
                    "course_code": "PH201",
                    "course_name": "Physics",
                    "course_dir": "ph201",
                    "aliases": [],
                },
            ]
        )
        with self.assertRaisesRegex(
            SemesterManifestError,
            "selected timetable.*set",
        ):
            plan_semester_manifest(manifest_path_extra)

    def test_manifest_rejects_cross_kind_routing_token_collisions(self) -> None:
        manifest_path = self._write_manifest(
            courses=[
                {
                    "course_code": "CS201",
                    "course_name": "Data Structures",
                    "course_dir": "cs201",
                    # Normalization must make this collide with the next
                    # course's code despite case and surrounding whitespace.
                    "aliases": ["  al201  "],
                },
                {
                    "course_code": "AL201",
                    "course_name": "Algorithms",
                    "course_dir": "al201",
                    "aliases": [],
                },
            ]
        )

        with self.assertRaisesRegex(
            SemesterManifestError,
            "Routing token collision after normalization",
        ):
            plan_semester_manifest(manifest_path)

    def test_active_snapshot_rejects_alias_colliding_with_course_name(self) -> None:
        active_path = self._write_active_snapshot()
        payload = json.loads(active_path.read_text(encoding="utf-8"))
        payload["courses"][0]["aliases"] = ["  algorithms  "]
        active_path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(
            SemesterManifestError,
            "Routing token collision after normalization",
        ):
            load_active_semester(active_path)

    def test_apply_target_path_safety_and_existence(self) -> None:
        manifest_path = self._write_manifest(
            courses=[
                {
                    "course_code": "CS201",
                    "course_name": "Data Structures",
                    "course_dir": "cs201",
                    "aliases": [],
                },
                {
                    "course_code": "AL201",
                    "course_name": "Algorithms",
                    "course_dir": "al201",
                    "aliases": [],
                },
            ]
        )
        plan = plan_semester_manifest(manifest_path)

        active_path = self._active_snapshot_path(existing_parent=False)
        with self.assertRaisesRegex(
            SemesterManifestError,
            "active_path.parent does not exist",
        ):
            apply_semester_manifest(
                manifest_path,
                expected_course_count=plan["expected_course_count"],
                expected_plan_sha256=plan["plan_sha256"],
                allow_write=True,
                timetable_db_path=self.timetable_db_path,
                jobs_db_path=self.jobs_db_path,
                active_path=active_path,
            )

        real_parent = self.root / "active-real"
        real_parent.mkdir()
        symlink_parent = self.root / "active-link"
        symlink_parent.symlink_to(real_parent)
        active_symlink_path = symlink_parent / "active-semester.json"
        with self.assertRaisesRegex(
            SemesterManifestError,
            "must not contain symlink components",
        ):
            apply_semester_manifest(
                manifest_path,
                expected_course_count=plan["expected_course_count"],
                expected_plan_sha256=plan["plan_sha256"],
                allow_write=True,
                timetable_db_path=self.timetable_db_path,
                jobs_db_path=self.jobs_db_path,
                active_path=active_symlink_path,
            )

    def test_manifest_rejects_relative_path_escape(self) -> None:
        manifest_path = self._write_manifest(
            courses=[
                {
                    "course_code": "CS201",
                    "course_name": "Data Structures",
                    "course_dir": "../cs201",
                    "aliases": [],
                },
                {
                    "course_code": "AL201",
                    "course_name": "Algorithms",
                    "course_dir": "al201",
                    "aliases": [],
                },
            ]
        )
        with self.assertRaisesRegex(
            SemesterManifestError,
            "must not contain empty, '.' or '..' segments",
        ):
            plan_semester_manifest(manifest_path)

    def test_load_active_semester_rejects_naive_and_invalid_activated_at(self) -> None:
        active_path = self._write_active_snapshot()

        payload = json.loads(active_path.read_text(encoding="utf-8"))
        payload["activated_at"] = "2026-01-01T10:00:00"
        active_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(
            SemesterManifestError,
            "activated_at must include a timezone offset",
        ):
            load_active_semester(active_path)

        payload = json.loads(active_path.read_text(encoding="utf-8"))
        payload["activated_at"] = "invalid-timestamp"
        active_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(
            SemesterManifestError,
            "activated_at must be an ISO-8601 datetime",
        ):
            load_active_semester(active_path)

    def test_apply_jobs_db_idle_guard_accepts_idle_and_records_guard(self) -> None:
        manifest_path = self._write_manifest(
            courses=[
                {
                    "course_code": "CS201",
                    "course_name": "Data Structures",
                    "course_dir": "cs201",
                    "aliases": ["CA_2"],
                },
                {
                    "course_code": "AL201",
                    "course_name": "Algorithms",
                    "course_dir": "al201",
                    "aliases": [],
                },
            ]
        )
        plan = plan_semester_manifest(manifest_path)
        active_path = self._write_active_snapshot()
        before = json.loads(active_path.read_text(encoding="utf-8"))
        self._insert_done_job(
            job_id=100,
            completed_at="2025-12-31T23:00:00+09:00",
            source_tag="260101CA_2",
        )

        result = apply_semester_manifest(
            manifest_path,
            expected_course_count=plan["expected_course_count"],
            expected_plan_sha256=plan["plan_sha256"],
            allow_write=True,
            timetable_db_path=self.timetable_db_path,
            jobs_db_path=self.jobs_db_path,
            active_path=active_path,
        )

        after = json.loads(active_path.read_text(encoding="utf-8"))
        self.assertEqual(
            result["switch_guard"],
            {
                "active_stt_jobs": 0,
                "pending_postprocess_jobs": 0,
                "unqueued_done_jobs": 0,
                "invalid_unqueued_done_jobs": 0,
            },
        )
        self.assertNotEqual(before["activated_at"], after["activated_at"])

    def test_apply_jobs_db_guard_allows_old_done_and_refuses_new_unqueued_done(self) -> None:
        manifest_path = self._write_manifest(
            courses=[
                {
                    "course_code": "CS201",
                    "course_name": "Data Structures",
                    "course_dir": "cs201",
                    "aliases": ["CA_2"],
                },
                {
                    "course_code": "AL201",
                    "course_name": "Algorithms",
                    "course_dir": "al201",
                    "aliases": [],
                },
            ]
        )
        plan = plan_semester_manifest(manifest_path)
        active_path = self._write_active_snapshot()
        snapshot_before = active_path.read_text(encoding="utf-8")

        self._insert_done_job(
            job_id=101,
            completed_at="2025-12-31T23:00:00+09:00",
            source_tag="260101CA_2",
        )
        self._insert_done_job(
            job_id=102,
            completed_at="2026-01-01T10:10:00+09:00",
            source_tag="260101CA_3",
        )

        with self.assertRaisesRegex(
            SemesterApplyConflictError,
            "unqueued_done_jobs=1",
        ):
            apply_semester_manifest(
                manifest_path,
                expected_course_count=plan["expected_course_count"],
                expected_plan_sha256=plan["plan_sha256"],
                allow_write=True,
                timetable_db_path=self.timetable_db_path,
                jobs_db_path=self.jobs_db_path,
                active_path=active_path,
            )
        self.assertEqual(active_path.read_text(encoding="utf-8"), snapshot_before)

    def test_apply_jobs_db_guard_blocks_malformed_unqueued_done(self) -> None:
        manifest_path = self._write_manifest(
            courses=[
                {
                    "course_code": "CS201",
                    "course_name": "Data Structures",
                    "course_dir": "cs201",
                    "aliases": ["CA_2"],
                },
                {
                    "course_code": "AL201",
                    "course_name": "Algorithms",
                    "course_dir": "al201",
                    "aliases": [],
                },
            ]
        )
        plan = plan_semester_manifest(manifest_path)
        active_path = self._write_active_snapshot()
        snapshot_before = active_path.read_text(encoding="utf-8")

        self._insert_invalid_done_job(job_id=103, source_tag="260101CA_4")

        with self.assertRaisesRegex(
            SemesterApplyConflictError,
            "invalid_unqueued_done_jobs=1",
        ):
            apply_semester_manifest(
                manifest_path,
                expected_course_count=plan["expected_course_count"],
                expected_plan_sha256=plan["plan_sha256"],
                allow_write=True,
                timetable_db_path=self.timetable_db_path,
                jobs_db_path=self.jobs_db_path,
                active_path=active_path,
            )
        self.assertEqual(active_path.read_text(encoding="utf-8"), snapshot_before)

    def test_apply_jobs_db_guard_refuses_active_jobs(self) -> None:
        manifest_path = self._write_manifest(
            courses=[
                {
                    "course_code": "CS201",
                    "course_name": "Data Structures",
                    "course_dir": "cs201",
                    "aliases": ["CA_2"],
                },
                {
                    "course_code": "AL201",
                    "course_name": "Algorithms",
                    "course_dir": "al201",
                    "aliases": [],
                },
            ]
        )
        plan = plan_semester_manifest(manifest_path)
        active_path = self._write_active_snapshot()
        snapshot_before = active_path.read_text(encoding="utf-8")

        self.jobs_conn.execute(
            "INSERT INTO jobs (status, created_at, updated_at, orig_inbox_path, orig_name, canonical_base, canonical_audio_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "PROCESSING",
                "2026-01-01T10:10:00+09:00",
                "2026-01-01T10:10:00+09:00",
                str(self.root / "inbox"),
                "260101CA_2.m4a",
                "260101CA_2",
                str(self.root / "260101CA_2.wav"),
            ),
        )
        self.jobs_conn.commit()

        with self.assertRaisesRegex(
            SemesterApplyConflictError,
            "semester apply requires an idle pipeline",
        ):
            apply_semester_manifest(
                manifest_path,
                expected_course_count=plan["expected_course_count"],
                expected_plan_sha256=plan["plan_sha256"],
                allow_write=True,
                timetable_db_path=self.timetable_db_path,
                jobs_db_path=self.jobs_db_path,
                active_path=active_path,
            )
        self.assertEqual(active_path.read_text(encoding="utf-8"), snapshot_before)

    def test_apply_jobs_db_guard_refuses_pending_postprocess(self) -> None:
        manifest_path = self._write_manifest(
            courses=[
                {
                    "course_code": "CS201",
                    "course_name": "Data Structures",
                    "course_dir": "cs201",
                    "aliases": ["CA_2"],
                },
                {
                    "course_code": "AL201",
                    "course_name": "Algorithms",
                    "course_dir": "al201",
                    "aliases": [],
                },
            ]
        )
        plan = plan_semester_manifest(manifest_path)
        active_path = self._write_active_snapshot()
        snapshot_before = active_path.read_text(encoding="utf-8")

        self.jobs_conn.execute(
            "INSERT INTO transcript_postprocess_jobs (source_job_id, logical_stem, activation_cutoff, route_method, status, source_txt_path, source_txt_sha256, source_json_path, source_json_sha256, generator_backend, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                100,
                "260101CA_2",
                "2026-01-01T10:00:00+09:00",
                "ROUTE",
                "PENDING",
                str(self.root / "260101CA_2.txt"),
                "x" * 64,
                str(self.root / "260101CA_2.json"),
                "x" * 64,
                "codex",
                "2026-01-01T10:10:00+09:00",
                "2026-01-01T10:10:00+09:00",
            ),
        )
        self.jobs_conn.commit()

        with self.assertRaisesRegex(
            SemesterApplyConflictError,
            "semester apply requires an idle pipeline",
        ):
            apply_semester_manifest(
                manifest_path,
                expected_course_count=plan["expected_course_count"],
                expected_plan_sha256=plan["plan_sha256"],
                allow_write=True,
                timetable_db_path=self.timetable_db_path,
                jobs_db_path=self.jobs_db_path,
                active_path=active_path,
            )
        self.assertEqual(active_path.read_text(encoding="utf-8"), snapshot_before)

    def test_apply_jobs_db_guard_fails_for_missing_or_incomplete_db(self) -> None:
        manifest_path = self._write_manifest(
            courses=[
                {
                    "course_code": "CS201",
                    "course_name": "Data Structures",
                    "course_dir": "cs201",
                    "aliases": ["CA_2"],
                },
                {
                    "course_code": "AL201",
                    "course_name": "Algorithms",
                    "course_dir": "al201",
                    "aliases": [],
                },
            ]
        )
        plan = plan_semester_manifest(manifest_path)
        active_path = self._write_active_snapshot()

        missing_db = self.root / "state" / "missing-jobs.sqlite3"
        with self.assertRaisesRegex(
            SemesterManifestError,
            "jobs_db_path does not exist",
        ):
            apply_semester_manifest(
                manifest_path,
                expected_course_count=plan["expected_course_count"],
                expected_plan_sha256=plan["plan_sha256"],
                allow_write=True,
                timetable_db_path=self.timetable_db_path,
                jobs_db_path=missing_db,
                active_path=active_path,
            )

        incomplete_db = self.root / "state" / "incomplete-jobs.sqlite3"
        incomplete_conn = sqlite3.connect(str(incomplete_db))
        incomplete_conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY)")
        incomplete_conn.commit()
        incomplete_conn.close()
        with self.assertRaisesRegex(
            SemesterApplyConflictError,
            "semester apply jobs DB is missing required tables",
        ):
            apply_semester_manifest(
                manifest_path,
                expected_course_count=plan["expected_course_count"],
                expected_plan_sha256=plan["plan_sha256"],
                allow_write=True,
                timetable_db_path=self.timetable_db_path,
                jobs_db_path=incomplete_db,
                active_path=active_path,
            )


if __name__ == "__main__":
    unittest.main()
