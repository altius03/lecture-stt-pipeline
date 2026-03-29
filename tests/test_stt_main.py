from __future__ import annotations

import io
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


class _FakeWorker:
    def __init__(self, params, ffmpeg_path: str, tmp_dir: str):
        self.params = params
        self.ffmpeg_path = ffmpeg_path
        self.tmp_dir = Path(tmp_dir)

    def transcribe_file(self, src_audio: Path, canonical_base: str, progress_callback=None):
        return ([{"id": 0, "start": 0.0, "end": 1.0, "text": "테스트"}], "테스트", 0.1, 0.2, self.tmp_dir / "temp.wav")

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

    def test_main_acquires_single_instance_lock_in_state_dir(self) -> None:
        config_path = self.root / "config.yaml"
        config_path.write_text("{}", encoding="utf-8")
        lock_paths: list[Path] = []

        class _LockRecorder:
            def __init__(self, path: Path):
                lock_paths.append(path)

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
            mock.patch.object(stt_main, "load_config", return_value={}),
            mock.patch.object(stt_main, "validate_config", return_value=fake_config),
            mock.patch.object(stt_main, "setup_logging", return_value=self.logger),
            mock.patch.object(stt_main, "SingleInstanceLock", _LockRecorder),
            mock.patch.object(stt_main, "STTPipeline", return_value=pipeline_mock),
        ):
            stt_main.main()

        self.assertEqual(lock_paths, [self.db_path.parent / "stt.lock"])
        pipeline_mock.run.assert_called_once_with(run_once=False)
