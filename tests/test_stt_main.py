from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.stt import main as stt_main  # noqa: E402


class SttMainControlCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.db_path = self.root / "custom-state" / "jobs.sqlite3"
        self.pause_path = self.db_path.parent / "paused"
        self.fallback_pause = self.root / "default-state" / "paused"
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(
            (
                "paths:\n"
                f"  db_path: {self.db_path}\n"
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _run_main(self, *args: str) -> str:
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["lecture-stt", *args]),
            mock.patch.object(stt_main.utils, "pause_flag_path", return_value=self.fallback_pause),
            redirect_stdout(stdout),
        ):
            stt_main.main()
        return stdout.getvalue()

    def test_status_uses_configured_pause_path(self) -> None:
        output = self._run_main("--status", "--config", str(self.config_path))
        self.assertIn(f"Pause flag: {self.pause_path}", output)
        self.assertNotIn(str(self.fallback_pause), output)

    def test_pause_and_resume_use_configured_pause_path(self) -> None:
        pause_output = self._run_main("--pause", "--config", str(self.config_path))
        self.assertTrue(self.pause_path.exists())
        self.assertFalse(self.fallback_pause.exists())
        self.assertIn(f"Paused: pause flag created at {self.pause_path}", pause_output)

        resume_output = self._run_main("--resume", "--config", str(self.config_path))
        self.assertFalse(self.pause_path.exists())
        self.assertFalse(self.fallback_pause.exists())
        self.assertIn(f"Resumed: pause flag removed at {self.pause_path}", resume_output)

    def test_status_resolves_env_backed_db_path(self) -> None:
        env_db_path = self.root / "env-state" / "jobs.sqlite3"
        self.config_path.write_text(
            (
                "paths:\n"
                "  db_path: ${STATE_ROOT}/jobs.sqlite3\n"
            ),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"STATE_ROOT": str(env_db_path.parent)}, clear=False):
            output = self._run_main("--status", "--config", str(self.config_path))

        self.assertIn(f"Pause flag: {env_db_path.parent / 'paused'}", output)


class _FakeNotifier:
    def notify_detected(self, payload: dict) -> None:
        pass

    def notify_moved(self, payload: dict) -> None:
        pass

    def notify_transcript_generated(self, payload: dict) -> None:
        pass

    def notify_success(self, payload: dict) -> None:
        pass

    def notify_completed(self, payload: dict) -> None:
        pass

    def notify_error(self, payload: dict) -> None:
        pass


class _RecordingNotifier(_FakeNotifier):
    def __init__(self) -> None:
        self.errors: list[dict] = []

    def notify_error(self, payload: dict) -> None:
        self.errors.append(dict(payload))


class _FakeWorker:
    def __init__(self, params, ffmpeg_path: str, tmp_dir: str):
        self.params = params
        self.ffmpeg_path = ffmpeg_path
        self.tmp_dir = Path(tmp_dir)

    def transcribe_file(self, src_audio: Path, canonical_base: str, progress_callback=None) -> tuple[list[dict], str, float, float, Path]:
        return ([{"id": 0, "start": 0.0, "end": 1.0, "text": "테스트"}], "테스트", 0.1, 0.2, self.tmp_dir / "temp.wav")

    def cleanup_tmp(self, wav_path: Path) -> None:
        pass


class _FailingWorker(_FakeWorker):
    def __init__(self, tmp_dir: Path, *, failures_before_success: int | None):
        self.tmp_dir = Path(tmp_dir)
        self.failures_before_success = failures_before_success
        self.calls = 0

    def transcribe_file(self, src_audio: Path, canonical_base: str, progress_callback=None) -> tuple[list[dict], str, float, float, Path]:
        self.calls += 1
        if self.failures_before_success is None or self.calls <= self.failures_before_success:
            raise RuntimeError("api_key=sk-secret-1234567890 transient STT failure")
        return ([{"id": 0, "start": 0.0, "end": 1.0, "text": "재시도 성공"}], "재시도 성공", 0.1, 0.2, self.tmp_dir / "retry.wav")

    def cleanup_tmp(self, wav_path: Path) -> None:
        pass


class SttPipelineBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.watch_dir = self.root / "watch"
        self.audio_dir = self.root / "audio"
        self.transcript_dir = self.root / "transcripts"
        self.error_dir = self.root / "errors"
        self.tmp_dir = self.root / "tmp"
        self.db_path = self.root / "state" / "jobs.sqlite3"
        for directory in (
            self.watch_dir,
            self.audio_dir,
            self.transcript_dir,
            self.error_dir,
            self.tmp_dir,
            self.db_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.logger = logging.getLogger(f"lecture_stt.test.{id(self)}")
        self.logger.handlers = []
        self.logger.addHandler(logging.NullHandler())
        self.logger.setLevel(logging.INFO)
        self.config = {
            "app": {
                "polling_interval_sec": 10,
                "stable_for_sec": 1,
                "stale_processing_hours": 6,
            },
            "paths": {
                "watch_folder": str(self.watch_dir),
                "stable_audio_folder": str(self.audio_dir),
                "transcript_folder": str(self.transcript_dir),
                "error_folder": str(self.error_dir),
                "tmp_dir": str(self.tmp_dir),
                "db_path": str(self.db_path),
            },
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
            },
            "ffmpeg": {"binary_path": "/usr/bin/true"},
            "logging": {"file": str(self.root / "app.log"), "max_bytes": 1024, "backup_count": 1},
            "notification": {"provider": "auto", "enabled": False},
        }

        worker_patcher = mock.patch.object(stt_main, "STTWorker", _FakeWorker)
        notifier_patcher = mock.patch.object(stt_main, "build_notifier", return_value=_FakeNotifier())
        self.addCleanup(worker_patcher.stop)
        self.addCleanup(notifier_patcher.stop)
        worker_patcher.start()
        notifier_patcher.start()

        self.pipeline = stt_main.STTPipeline(config=self.config, logger=self.logger)

    def tearDown(self) -> None:
        self.pipeline.conn.close()
        self.tmpdir.cleanup()

    def test_process_job_uses_local_staging_before_canonical_move(self) -> None:
        source_path = self.watch_dir / "sample.m4a"
        source_path.write_bytes(b"fake-audio")

        with mock.patch.object(stt_main.utils, "safe_move_file", wraps=stt_main.utils.safe_move_file) as mocked_move:
            self.pipeline.process_job(source_path)

        self.assertGreaterEqual(mocked_move.call_count, 2)
        first_src, first_dst = mocked_move.call_args_list[0].args
        second_src, second_dst = mocked_move.call_args_list[1].args
        self.assertEqual(Path(first_src), source_path)
        self.assertEqual(Path(first_dst).parent, self.pipeline.staging_dir)
        self.assertEqual(Path(second_src).parent, self.pipeline.staging_dir)
        self.assertEqual(Path(second_dst).parent, self.audio_dir)

        rows = self.pipeline.conn.execute("SELECT status, current_step FROM jobs").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], stt_main.STATUS_DONE)
        self.assertEqual(rows[0]["current_step"], "전체 완료")

    def test_process_job_skips_missing_source_when_competing_job_exists(self) -> None:
        source_path = self.watch_dir / "race.m4a"
        canonical_audio = self.audio_dir / "race.m4a"
        self.pipeline.conn.execute(
            "INSERT INTO jobs (status, created_at, updated_at, orig_inbox_path, orig_name, canonical_base, "
            "canonical_audio_path, transcript_txt_path, transcript_json_path, current_step, progress_pct) "
            "VALUES (?, datetime('now'), datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stt_main.STATUS_PROCESSING,
                str(source_path),
                source_path.name,
                "race",
                str(canonical_audio),
                str(self.transcript_dir / "race.txt"),
                str(self.transcript_dir / "race.json"),
                "파일 이동",
                20,
            ),
        )
        self.pipeline.conn.commit()

        self.pipeline.process_job(source_path)

        rows = self.pipeline.conn.execute(
            "SELECT id, status, error_message FROM jobs ORDER BY id ASC"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], stt_main.STATUS_PROCESSING)

    def test_startup_recovery_requeues_staged_inputs_and_clears_stale_jobs(self) -> None:
        staged_path = self.pipeline.staging_dir / "recover.m4a"
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_bytes(b"staged-audio")
        source_path = self.watch_dir / "recover.m4a"

        self.pipeline.conn.execute(
            "INSERT INTO jobs (status, created_at, updated_at, orig_inbox_path, orig_name, canonical_base, "
            "canonical_audio_path, transcript_txt_path, transcript_json_path, current_step, progress_pct) "
            "VALUES (?, datetime('now'), datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stt_main.STATUS_PENDING,
                str(source_path),
                source_path.name,
                "recover",
                str(staged_path),
                str(self.transcript_dir / "recover.txt"),
                str(self.transcript_dir / "recover.json"),
                "로컬 staging",
                15,
            ),
        )
        self.pipeline.conn.commit()

        self.pipeline.startup_recovery()

        self.assertTrue(source_path.exists())
        self.assertFalse(staged_path.exists())
        remaining = self.pipeline.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_startup_recovery_requeues_pending_canonical_audio_jobs(self) -> None:
        canonical_audio = self.audio_dir / "stuck.m4a"
        canonical_audio.write_bytes(b"claimed-audio")
        source_path = self.watch_dir / "stuck.m4a"

        self.pipeline.conn.execute(
            "INSERT INTO jobs (status, created_at, updated_at, orig_inbox_path, orig_name, canonical_base, "
            "canonical_audio_path, transcript_txt_path, transcript_json_path, current_step, progress_pct) "
            "VALUES (?, datetime('now'), datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stt_main.STATUS_PENDING,
                str(source_path),
                source_path.name,
                "stuck",
                str(canonical_audio),
                str(self.transcript_dir / "stuck.txt"),
                str(self.transcript_dir / "stuck.json"),
                "파일 이동",
                30,
            ),
        )
        self.pipeline.conn.commit()

        self.pipeline.startup_recovery()

        self.assertTrue(source_path.exists())
        self.assertFalse(canonical_audio.exists())
        remaining = self.pipeline.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_startup_recovery_keeps_retryable_transcription_jobs_for_next_scan(self) -> None:
        canonical_audio = self.audio_dir / "retry-pending.m4a"
        canonical_audio.write_bytes(b"claimed-audio")
        source_path = self.watch_dir / "retry-pending.m4a"

        self.pipeline.conn.execute(
            "INSERT INTO jobs (status, created_at, updated_at, orig_inbox_path, orig_name, canonical_base, "
            "canonical_audio_path, transcript_txt_path, transcript_json_path, engine_params, current_step, progress_pct) "
            "VALUES (?, datetime('now'), datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stt_main.STATUS_PENDING,
                str(source_path),
                source_path.name,
                "retry-pending",
                str(canonical_audio),
                str(self.transcript_dir / "retry-pending.txt"),
                str(self.transcript_dir / "retry-pending.json"),
                json.dumps({"transcription_failures": 1, "transcription_max_retries": 2}),
                "전사 재시도 대기 1/2",
                18,
            ),
        )
        self.pipeline.conn.commit()

        self.pipeline.startup_recovery()

        self.assertTrue(canonical_audio.exists())
        self.assertFalse(source_path.exists())
        row = self.pipeline.conn.execute(
            "SELECT status, current_step FROM jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["status"], stt_main.STATUS_PENDING)
        self.assertEqual(row["current_step"], "전사 재시도 대기 1/2")

    def test_startup_recovery_restores_processing_retry_marker_for_next_scan(self) -> None:
        canonical_audio = self.audio_dir / "retry-processing.m4a"
        canonical_audio.write_bytes(b"claimed-audio")
        source_path = self.watch_dir / "retry-processing.m4a"

        self.pipeline.conn.execute(
            "INSERT INTO jobs (status, created_at, updated_at, started_at, orig_inbox_path, orig_name, canonical_base, "
            "canonical_audio_path, transcript_txt_path, transcript_json_path, engine_params, current_step, progress_pct) "
            "VALUES (?, datetime('now'), datetime('now'), datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stt_main.STATUS_PROCESSING,
                str(source_path),
                source_path.name,
                "retry-processing",
                str(canonical_audio),
                str(self.transcript_dir / "retry-processing.txt"),
                str(self.transcript_dir / "retry-processing.json"),
                json.dumps({"transcription_failures": 1, "transcription_max_retries": 2}),
                "전사 시작/진행",
                31,
            ),
        )
        self.pipeline.conn.commit()

        self.pipeline.startup_recovery()

        recovered = self.pipeline.conn.execute(
            "SELECT status, current_step, progress_pct, canonical_audio_path FROM jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(recovered["status"], stt_main.STATUS_PENDING)
        self.assertEqual(recovered["current_step"], "전사 재시도 대기 1/2")
        self.assertEqual(recovered["progress_pct"], 18)
        self.assertEqual(Path(recovered["canonical_audio_path"]), canonical_audio)
        self.assertTrue(canonical_audio.exists())
        self.assertFalse(source_path.exists())

        self.assertEqual(self.pipeline.process_retryable_jobs(), 1)
        done = self.pipeline.conn.execute(
            "SELECT status, current_step FROM jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(done["status"], stt_main.STATUS_DONE)
        self.assertEqual(done["current_step"], "전체 완료")

    def test_transcription_retries_transient_failures_before_success(self) -> None:
        source_path = self.watch_dir / "retry-success.m4a"
        source_path.write_bytes(b"fake-audio")
        worker = _FailingWorker(self.tmp_dir, failures_before_success=2)

        with mock.patch.object(self.pipeline, "worker", worker):
            self.pipeline.process_job(source_path)
            first_row = self.pipeline.conn.execute(
                "SELECT status, current_step, canonical_audio_path, engine_params FROM jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(worker.calls, 1)
            self.assertEqual(first_row["status"], stt_main.STATUS_PENDING)
            self.assertEqual(first_row["current_step"], "전사 재시도 대기 1/2")
            self.assertEqual(json.loads(first_row["engine_params"])["transcription_failures"], 1)
            self.assertTrue((self.audio_dir / "retry-success.m4a").exists())
            self.assertFalse((self.error_dir / "retry-success.m4a").exists())

            self.assertEqual(self.pipeline.process_retryable_jobs(), 1)
            second_row = self.pipeline.conn.execute(
                "SELECT status, current_step, engine_params FROM jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(worker.calls, 2)
            self.assertEqual(second_row["status"], stt_main.STATUS_PENDING)
            self.assertEqual(second_row["current_step"], "전사 재시도 대기 2/2")
            self.assertEqual(json.loads(second_row["engine_params"])["transcription_failures"], 2)

            self.assertEqual(self.pipeline.process_retryable_jobs(), 1)

        self.assertEqual(worker.calls, 3)
        row = self.pipeline.conn.execute(
            "SELECT status, canonical_audio_path, transcript_txt_path, transcript_json_path, "
            "error_message, engine_params FROM jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], stt_main.STATUS_DONE)
        self.assertIsNone(row["error_message"])
        self.assertFalse(source_path.exists())
        self.assertTrue((self.audio_dir / "retry-success.m4a").exists())
        self.assertFalse((self.error_dir / "retry-success.m4a").exists())
        self.assertEqual(Path(row["canonical_audio_path"]).parent, self.audio_dir)
        self.assertTrue(Path(row["transcript_txt_path"]).exists())
        self.assertTrue(Path(row["transcript_json_path"]).exists())
        self.assertIn("재시도 성공", Path(row["transcript_txt_path"]).read_text(encoding="utf-8"))
        payload = json.loads(Path(row["transcript_json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["metadata"]["canonical_base"], "retry-success")
        self.assertEqual(json.loads(row["engine_params"])["transcription_failures_before_success"], 2)

    def test_redacts_authorization_bearer_and_jwt_tokens(self) -> None:
        sample_jwt = ".".join([
            "eyJhbGciOiJIUzI1NiJ9",
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
            "sgntr",
        ])
        text = (
            f"Authorization: Bearer {sample_jwt}; "
            f"authorization=Bearer {sample_jwt}; "
            f"token={sample_jwt}; api_key=sk-secret-token-1234567890"
        )

        redacted = stt_main._redact_sensitive_text(text)

        self.assertNotIn("Bearer eyJ", redacted)
        self.assertNotIn(sample_jwt, redacted)
        self.assertNotIn("sk-secret-token", redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 4)

    def test_transcription_final_failure_moves_audio_to_errors_and_redacts_secret(self) -> None:
        source_path = self.watch_dir / "retry-fail.m4a"
        source_path.write_bytes(b"fake-audio")
        worker = _FailingWorker(self.tmp_dir, failures_before_success=None)
        notifier = _RecordingNotifier()

        with (
            mock.patch.object(self.pipeline, "worker", worker),
            mock.patch.object(self.pipeline, "notifier", notifier),
        ):
            self.pipeline.process_job(source_path)
            self.assertEqual(worker.calls, 1)
            first_row = self.pipeline.conn.execute(
                "SELECT status, current_step FROM jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(first_row["status"], stt_main.STATUS_PENDING)
            self.assertEqual(first_row["current_step"], "전사 재시도 대기 1/2")

            self.assertEqual(self.pipeline.process_retryable_jobs(), 1)
            self.assertEqual(worker.calls, 2)
            second_row = self.pipeline.conn.execute(
                "SELECT status, current_step FROM jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(second_row["status"], stt_main.STATUS_PENDING)
            self.assertEqual(second_row["current_step"], "전사 재시도 대기 2/2")

            with self.assertLogs(self.logger.name, level="ERROR") as captured:
                self.assertEqual(self.pipeline.process_retryable_jobs(), 1)

        self.assertEqual(worker.calls, 3)
        row = self.pipeline.conn.execute(
            "SELECT status, canonical_audio_path, transcript_txt_path, transcript_json_path, "
            "error_message, error_trace, current_step, engine_params FROM jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], stt_main.STATUS_ERROR)
        self.assertEqual(row["current_step"], "실패: 전사 실행")
        self.assertEqual(json.loads(row["engine_params"])["transcription_failures"], 3)
        error_audio = self.error_dir / "retry-fail.m4a"
        self.assertTrue(error_audio.exists())
        self.assertFalse(source_path.exists())
        self.assertFalse((self.audio_dir / "retry-fail.m4a").exists())
        self.assertEqual(Path(row["canonical_audio_path"]), error_audio)
        self.assertFalse(Path(row["transcript_txt_path"]).exists())
        self.assertFalse(Path(row["transcript_json_path"]).exists())
        self.assertEqual(len(notifier.errors), 1)

        sensitive_surfaces = [
            row["error_message"] or "",
            row["error_trace"] or "",
            json.dumps(notifier.errors[0], ensure_ascii=False),
            "\n".join(captured.output),
        ]
        for surface in sensitive_surfaces:
            self.assertNotIn("sk-", surface)
            self.assertIn("[REDACTED]", surface)

    def test_main_acquires_single_instance_lock_in_state_dir(self) -> None:
        config_path = self.root / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")
        lock_args: list[tuple[Path, bool]] = []

        class _LockRecorder:
            def __init__(self, path: Path, *, blocking: bool = False):
                lock_args.append((path, blocking))

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

        fake_config = {
            "paths": {"db_path": str(self.db_path)},
            "logging": {"file": str(self.root / "app.log"), "max_bytes": 1024, "backup_count": 1},
        }
        pipeline_mock = mock.Mock()
        with (
            mock.patch.object(sys, "argv", ["lecture-stt", "--config", str(config_path)]),
            mock.patch.dict(os.environ, {"LECTURE_STT_LOCK_WAIT": ""}, clear=False),
            mock.patch.object(stt_main, "load_config", return_value={}),
            mock.patch.object(stt_main, "validate_config", return_value=fake_config),
            mock.patch.object(stt_main, "setup_logging", return_value=self.logger),
            mock.patch.object(stt_main, "SingleInstanceLock", _LockRecorder),
            mock.patch.object(stt_main, "STTPipeline", return_value=pipeline_mock),
        ):
            stt_main.main()

        self.assertEqual(lock_args, [(self.db_path.parent / "stt.lock", False)])
        pipeline_mock.run.assert_called_once_with(run_once=False)

    def test_main_waits_for_single_instance_lock_when_env_enabled(self) -> None:
        config_path = self.root / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")
        lock_args: list[tuple[Path, bool]] = []

        class _LockRecorder:
            def __init__(self, path: Path, *, blocking: bool = False):
                lock_args.append((path, blocking))

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

        fake_config = {
            "paths": {"db_path": str(self.db_path)},
            "logging": {"file": str(self.root / "app.log"), "max_bytes": 1024, "backup_count": 1},
        }
        pipeline_mock = mock.Mock()
        with (
            mock.patch.object(sys, "argv", ["lecture-stt", "--config", str(config_path)]),
            mock.patch.dict(os.environ, {"LECTURE_STT_LOCK_WAIT": "1"}, clear=False),
            mock.patch.object(stt_main, "load_config", return_value={}),
            mock.patch.object(stt_main, "validate_config", return_value=fake_config),
            mock.patch.object(stt_main, "setup_logging", return_value=self.logger),
            mock.patch.object(stt_main, "SingleInstanceLock", _LockRecorder),
            mock.patch.object(stt_main, "STTPipeline", return_value=pipeline_mock),
        ):
            stt_main.main()

        self.assertEqual(lock_args, [(self.db_path.parent / "stt.lock", True)])
        pipeline_mock.run.assert_called_once_with(run_once=False)
