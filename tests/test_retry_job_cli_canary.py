from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.shared import db  # noqa: E402
from lecture_stt.stt.main import STTPipeline  # noqa: E402
from lecture_stt.stt.retry_job import build_retry_job_plan  # noqa: E402


class RetryJobCliCanaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.watch_root = self.root / "watch"
        self.audio_root = self.root / "audio"
        self.transcript_root = self.root / "transcripts"
        self.error_root = self.root / "errors"
        self.tmp_root = self.root / "tmp"
        self.db_path = self.root / "state" / "jobs.sqlite3"
        for path in (
            self.watch_root,
            self.audio_root,
            self.transcript_root,
            self.error_root,
            self.tmp_root,
            self.db_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

        self.config = {
            "app": {
                "polling_interval_sec": 1,
                "stable_for_sec": 1,
                "stale_processing_hours": 6,
                "transcribe_max_retries": 3,
            },
            "paths": {
                "watch_folder": str(self.watch_root),
                "stable_audio_folder": str(self.audio_root),
                "transcript_folder": str(self.transcript_root),
                "error_folder": str(self.error_root),
                "tmp_dir": str(self.tmp_root),
                "db_path": str(self.db_path),
            },
            "engine": {"engine": "faster-whisper"},
            "transcribe": {
                "model_size": "tiny",
                "device": "cpu",
                "compute_type": "int8",
                "language": "ko",
                "task": "transcribe",
                "beam_size": 1,
                "vad_filter": False,
                "word_timestamps": False,
                "condition_on_previous_text": False,
                "keep_model_loaded": False,
            },
            "ffmpeg": {"binary_path": "/usr/bin/false"},
            "logging": {
                "file": str(self.root / "canary.log"),
                "max_bytes": 64 * 1024,
                "backup_count": 1,
            },
            "notification": {"provider": "auto", "enabled": False},
        }
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(self.config, ensure_ascii=False),
            encoding="utf-8",
        )
        self.audio_path = self.audio_root / "retry-canary.m4a"
        self.audio_path.write_bytes(b"isolated-retry-canary-audio")
        conn = db.init_db(str(self.db_path))
        try:
            self.job_id = db.create_job(
                conn,
                status=db.STATUS_PENDING,
                orig_inbox_path=str(self.watch_root / self.audio_path.name),
                orig_name=self.audio_path.name,
                canonical_base=self.audio_path.stem,
                canonical_audio_path=str(self.audio_path),
                transcript_txt_path=str(
                    self.transcript_root / f"{self.audio_path.stem}.txt"
                ),
                transcript_json_path=str(
                    self.transcript_root / f"{self.audio_path.stem}.json"
                ),
                engine_params={
                    "transcription_failures": 1,
                    "transcription_max_retries": 3,
                },
                current_step="전사 재시도 대기 1/3",
                progress_pct=18,
            )
        finally:
            conn.close()
        self.kill_switch = self.root / "controller.stop"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_plan(self, name: str) -> tuple[dict, Path]:
        plan = build_retry_job_plan(self.config, job_id=self.job_id)
        path = self.root / name
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return plan, path

    def _run_apply(
        self,
        plan: dict,
        manifest_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        existing_python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(SRC_ROOT)
            if not existing_python_path
            else os.pathsep.join((str(SRC_ROOT), existing_python_path))
        )
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "lecture_stt.stt.main",
                "--config",
                str(self.config_path),
                "--retry-job-manifest",
                str(manifest_path),
                "--enable-retry-job",
                "--allow-write",
                "--expected-count",
                "1",
                "--expected-plan-sha256",
                plan["plan_sha256"],
                "--controller-kill-switch",
                str(self.kill_switch),
            ],
            cwd=REPO_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def _row(self):
        conn = db.init_db(str(self.db_path))
        try:
            row = db.get_job(conn, self.job_id)
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def test_plan_apply_replay_tamper_kill_switch_and_crash_recovery(self) -> None:
        first_plan, first_manifest = self._write_plan("retry-first.json")

        applied = self._run_apply(first_plan, first_manifest)

        self.assertEqual(applied.returncode, 75, applied.stderr)
        result = json.loads(applied.stdout)
        self.assertEqual(result["status"], "retry_pending")
        self.assertEqual(result["job_id"], self.job_id)
        first_row = self._row()
        self.assertEqual(first_row["status"], db.STATUS_PENDING)
        self.assertEqual(first_row["current_step"], "전사 재시도 대기 2/3")

        replayed = self._run_apply(first_plan, first_manifest)
        self.assertEqual(replayed.returncode, 2)
        self.assertIn("row changed after planning", replayed.stderr)

        tamper_plan, tamper_manifest = self._write_plan("retry-tamper.json")
        before_tamper_row = self._row()
        self.audio_path.write_bytes(b"changed-after-plan")
        tampered = self._run_apply(tamper_plan, tamper_manifest)
        self.assertEqual(tampered.returncode, 2)
        self.assertIn("retry audio changed after planning", tampered.stderr)
        self.assertEqual(self._row(), before_tamper_row)

        stopped_plan, stopped_manifest = self._write_plan("retry-stopped.json")
        self.kill_switch.write_text("stop\n", encoding="utf-8")
        before_stop_row = self._row()
        stopped = self._run_apply(stopped_plan, stopped_manifest)
        self.assertEqual(stopped.returncode, 2)
        self.assertIn("kill switch is active", stopped.stderr)
        self.assertEqual(self._row(), before_stop_row)
        self.kill_switch.unlink()

        conn = db.init_db(str(self.db_path))
        try:
            self.assertTrue(db.claim_job_for_processing(conn, self.job_id))
        finally:
            conn.close()
        logger = logging.getLogger(f"lecture_stt.retry_canary.{id(self)}")
        logger.handlers = [logging.NullHandler()]
        pipeline = STTPipeline(config=self.config, logger=logger)
        try:
            pipeline.startup_recovery()
        finally:
            pipeline.conn.close()
        recovered = self._row()
        self.assertEqual(recovered["status"], db.STATUS_PENDING)
        self.assertEqual(recovered["current_step"], "전사 재시도 대기 2/3")
        replacement_plan = build_retry_job_plan(self.config, job_id=self.job_id)
        self.assertNotEqual(
            replacement_plan["plan_sha256"],
            stopped_plan["plan_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
