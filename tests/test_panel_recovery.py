from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock

import yaml

from lecture_stt.downstream import panel_recovery as panel
from lecture_stt.downstream import transcript_delivery as delivery
from lecture_stt.downstream.worker import load_transcript_worker_config
from tests import test_transcript_delivery as fixtures


class PanelRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TranscriptPostprocessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db_path = self.fixture.db_path
        self.config = self.fixture.root / "config.yaml"
        self.settings = {
            "paths": {"db_path": str(self.db_path)},
            "transcript_delivery": {
                "enabled": True, "active_semester_manifest": str(self.fixture.active_path),
                "correction_staging_dir": str(self.fixture.correction_dir),
                "summary_staging_dir": str(self.fixture.summary_staging_dir),
                "generator": {"codex_binary": sys.executable, "temp_root": str(self.fixture.root / "tmp")},
            },
        }
        self.write_config()

    def write_config(self):
        self.config.write_text(yaml.safe_dump(self.settings), encoding="utf-8")

    def queue(self, name, *, profile="general"):
        return self.fixture._queue_job(canonical_base=name, source_suffix=name,
                                      recorded_at="2026-01-02T23:50:00+09:00", profile_key=profile)[0]

    def detail(self, job_id):
        return panel.get_recovery(self.config, self.db_path, job_id=job_id)

    def listing(self, view="missing", *, offset=0, limit=20):
        return panel.list_recovery(self.config, self.db_path, view=view, offset=offset, limit=limit)

    def submit(self, detail, course="CS201"):
        return panel.apply_recovery(self.config, self.db_path, job_id=detail["job"]["id"],
                                    course_code=course, source_txt_sha256=detail["source_txt_sha256"],
                                    source_json_sha256=detail["source_json_sha256"])

    def test_listing_is_bounded_and_safe_to_read_with_generator_offline(self):
        first = self.queue("광명동 4")
        second = self.queue("금요일 야간", profile="lecture")
        routed = self.queue("260101CA_2")
        self.settings["transcript_delivery"]["generator"]["codex_binary"] = "/missing/test-binary"
        self.write_config()
        self.fixture.conn.execute("UPDATE jobs SET engine_params='{}' WHERE id=?", (first,))
        self.fixture.conn.commit()
        page = self.listing(limit=1)
        self.assertEqual((page["enabled"], page["total"], len(page["items"])), (True, 2, 1))
        one, two = page["items"][0], self.listing(offset=1, limit=1)["items"][0]
        rows = {item["id"]: item for item in (one, two)}
        self.assertEqual(set(rows), {first, second})
        self.assertEqual(rows[second]["recorded_at"], "2026-01-02T23:50:00+09:00")
        self.assertIsNone(rows[first]["recorded_at"])
        self.assertNotEqual(rows[first]["reason"], rows[second]["reason"])
        self.assertEqual(rows[first]["title"], "광명동 4.m4a")
        details = self.detail(second)
        self.assertTrue(details["can_requeue"])
        self.assertEqual(details["courses"], [{"code": "CS201", "name": "Data Structures"}])
        for response in (page, details):
            serialized = json.dumps(response, ensure_ascii=False)
            self.assertNotIn(str(self.fixture.root), serialized)
            self.assertNotIn("후입선출", serialized)
        with self.assertRaises(panel.RecoveryNotFoundError):
            self.detail(routed)
        for args in ({"view": "all"}, {"offset": -1}, {"limit": 101}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.listing(**args)

    def test_confirm_repeat_and_track_worker_delivery(self):
        jid = self.queue("장소 녹음")
        detail = self.detail(jid)
        self.assertEqual(self.submit(detail), {"id": jid, "status": "PENDING"})
        self.assertEqual(self.submit(detail)["status"], "PENDING")
        self.assertEqual(self.listing()["total"], 0)
        self.assertEqual(self.listing("confirmed")["items"][0]["course_code"], "CS201")
        confirmed = self.detail(jid)
        self.assertFalse(confirmed["can_requeue"])
        settings = load_transcript_worker_config(str(self.config)).postprocess_settings
        worker = delivery.TranscriptDeliveryDistributor(self.fixture.conn, settings)
        correction = {"segments": [{"id": i, "text": text} for i, text in
                                   enumerate(("자료 구조를 설명합니다.", "스택은 후입선출입니다."))]}
        summary = {"title": "자료 구조", "overview": "스택", "key_points": [], "details": [],
                   "examples": [], "cautions": [], "professor_points": [], "review_questions": []}
        with mock.patch("lecture_stt.downstream.postprocess._run_codex_json", side_effect=[correction, summary]) as gen:
            self.assertEqual(worker.process_pending()["delivered"], 1)
            self.assertEqual(self.submit(detail)["status"], "DELIVERED")
            self.assertEqual(worker.process_pending()["processed"], 0)
        self.assertEqual(gen.call_count, 2)
        result = self.detail(jid)["job"]
        self.assertEqual((result["status"], result["correction_status"], result["summary_status"],
                          result["delivery_status"]), ("DELIVERED", "READY", "READY", "DELIVERED"))
        with self.assertRaises(panel.RecoveryConflictError):
            self.submit(detail, "OTHER")

    def test_changes_between_read_and_apply_do_not_requeue(self):
        jid = self.queue("별칭 없음")
        details = self.detail(jid)
        src = self.fixture.conn.execute("SELECT source_txt_path FROM transcript_postprocess_jobs WHERE source_job_id=?",
                                        (jid,)).fetchone()[0]
        old = Path(src).read_bytes()
        Path(src).write_text("원전사가 바뀜", encoding="utf-8")
        with self.assertRaises(panel.RecoveryConflictError):
            self.submit(details)
        Path(src).write_bytes(old)
        with self.assertRaises(panel.RecoveryConflictError):
            self.submit(details, "UNKNOWN")
        snapshot = self.fixture.active_path
        snapshot.write_bytes(snapshot.read_bytes() + b"\n")
        self.assertFalse(self.detail(jid)["can_requeue"])
        with self.assertRaises(panel.RecoveryConflictError):
            self.submit(details)
        self.assertEqual(self.detail(jid)["job"]["status"], "SKIPPED")

    def test_reserved_artifacts_and_missing_rows_are_not_actionable(self):
        jid = self.queue("과목 확인 대기")
        details = self.detail(jid)
        with self.fixture.conn:
            self.fixture.conn.execute(
                "UPDATE transcript_postprocess_jobs SET correction_stage_json_sha256=? WHERE source_job_id=?",
                ("a" * 64, jid),
            )
        self.assertFalse(self.detail(jid)["can_requeue"])
        with self.assertRaises(panel.RecoveryConflictError):
            self.submit(details)
        with self.fixture.conn:
            self.fixture.conn.execute("DELETE FROM transcript_postprocess_jobs WHERE source_job_id=?", (jid,))
        with self.assertRaises(panel.RecoveryNotFoundError):
            self.submit(details)

    def test_disabled_db_drift_and_unrelated_error_are_closed(self):
        jid = self.queue("수업 없음")
        detail = self.detail(jid)
        with closing(sqlite3.connect(self.db_path)) as other, other:
            other.execute("UPDATE transcript_postprocess_jobs SET status='ERROR', course_code='CS201', "
                          "last_error_code='FILESYSTEM_ERROR' WHERE source_job_id=?", (jid,))
        with self.assertRaises((panel.RecoveryNotFoundError, panel.RecoveryConflictError)):
            self.submit(detail)
        self.settings["transcript_delivery"]["enabled"] = False
        self.write_config()
        self.assertEqual(self.listing(), {"enabled": False, "total": 0, "items": []})
        with self.assertRaises(panel.RecoveryDisabledError):
            self.detail(jid)
        with self.assertRaises(panel.RecoveryDisabledError):
            self.submit(detail)
        self.settings["transcript_delivery"]["enabled"] = True
        self.settings["paths"]["db_path"] = str(self.fixture.root / "changed.sqlite3")
        self.write_config()
        with self.assertRaises((ValueError, RuntimeError)):
            self.listing()
        self.assertFalse((self.fixture.root / "changed.sqlite3").exists())
