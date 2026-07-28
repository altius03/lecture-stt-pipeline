from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.shared import db, utils
from lecture_stt.stt import main as stt_main
from lecture_stt.stt import terminal_error_recovery as terminal_recovery
from lecture_stt.stt.terminal_error_recovery import (
    PLAN_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    TerminalErrorRecoveryConflictError,
    TerminalErrorRecoveryContractError,
    TerminalErrorRecoveryWriteDisabledError,
    apply_terminal_error_recovery_plan,
    build_terminal_error_recovery_plan,
    load_terminal_error_recovery_plan,
    revalidate_terminal_error_recovery_plan,
    validate_terminal_error_recovery_apply_guards,
    validate_terminal_error_recovery_plan,
)


class TerminalErrorRecoveryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.audio_root = self.root / "audio"
        self.error_root = self.root / "errors"
        self.transcript_root = self.root / "transcripts"
        self.db_path = self.root / "state" / "jobs.sqlite3"
        for directory in (
            self.audio_root,
            self.error_root,
            self.transcript_root,
            self.db_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.conn = db.init_db(str(self.db_path))
        self.config = {
            "app": {
                "stable_for_sec": 60,
                "stale_processing_hours": 6,
                "transcribe_max_retries": 3,
            },
            "paths": {
                "watch_folder": str(self.root / "watch"),
                "stable_audio_folder": str(self.audio_root),
                "transcript_folder": str(self.transcript_root),
                "error_folder": str(self.error_root),
                "tmp_dir": str(self.root / "tmp"),
                "db_path": str(self.db_path),
            },
            "engine": {"engine": "faster-whisper"},
            "transcribe": {
                "model_size": "large-v3",
                "device": "cpu",
                "compute_type": "int8",
                "language": "ko",
                "task": "transcribe",
                "beam_size": 5,
                "vad_filter": False,
                "word_timestamps": False,
                "condition_on_previous_text": True,
                "keep_model_loaded": False,
            },
            "ffmpeg": {"binary_path": "/usr/bin/false"},
            "profiles": {
                "active": "general",
                "definitions": {
                    "general": {
                        "version": "test.1",
                        "transcribe": {"initial_prompt": ""},
                        "quality": {"warn_threshold": 0.55, "bad_threshold": 0.70},
                    }
                },
            },
        }

    def tearDown(self) -> None:
        self.conn.close()
        self.tmpdir.cleanup()

    def _create_terminal_error_job(
        self,
        *,
        name: str = "job-201.m4a",
        payload: bytes = b"error-audio",
    ) -> tuple[int, Path]:
        source = self.error_root / name
        source.write_bytes(payload)
        sha256 = utils.compute_sha256(source)
        canonical_base = Path(name).stem
        job_id = db.create_job(
            self.conn,
            status=db.STATUS_ERROR,
            orig_inbox_path=f"/hidden/{name}",
            orig_name=name,
            canonical_base=canonical_base,
            canonical_audio_path=str(source),
            transcript_txt_path=str(self.transcript_root / f"{canonical_base}.txt"),
            transcript_json_path=str(self.transcript_root / f"{canonical_base}.json"),
            engine_params={
                "transcription_failures": 3,
                "transcription_max_retries": 3,
                "last_error_message": "boom",
            },
            current_step="실패: 전사 실행",
            progress_pct=0,
            eta_sec=None,
        )
        db.update_job(
            self.conn,
            job_id,
            sha256=sha256,
            error_message="boom",
            error_trace="traceback...",
            ended_at="2026-07-28T09:30:00+09:00",
            current_step="실패: 전사 실행",
            status=db.STATUS_ERROR,
            canonical_audio_path=str(source),
        )
        return job_id, source

    def test_plan_validate_load_and_apply_rearm_exact_error_row(self) -> None:
        job_id, source = self._create_terminal_error_job()
        plan = build_terminal_error_recovery_plan(self.config, job_id=job_id)
        manifest_path = self.root / "terminal-recovery-plan.json"
        manifest_path.write_text(json.dumps(plan), encoding="utf-8")

        loaded = load_terminal_error_recovery_plan(manifest_path)
        self.assertEqual(plan["schema_version"], PLAN_SCHEMA_VERSION)
        self.assertEqual(plan["expected_count"], 1)
        self.assertEqual(loaded, validate_terminal_error_recovery_plan(plan))
        self.assertEqual(plan["error_job"]["job_id"], job_id)
        self.assertEqual(plan["source"]["relative_path"], source.name)
        self.assertEqual(plan["target"]["relative_path"], source.name)

        result = apply_terminal_error_recovery_plan(self.config, self.conn, loaded)
        self.assertEqual(result["schema_version"], RESULT_SCHEMA_VERSION)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["job_id"], job_id)

        row = db.get_job(self.conn, job_id)
        assert row is not None
        self.assertEqual(row["status"], db.STATUS_PENDING)
        self.assertEqual(row["current_step"], "전사 재시도 대기 1/3")
        self.assertEqual(row["canonical_audio_path"], str(self.audio_root / source.name))
        self.assertIsNone(row["started_at"])
        self.assertIsNone(row["ended_at"])
        self.assertTrue(source.exists())
        self.assertTrue((self.audio_root / source.name).exists())

        metadata = json.loads(row["engine_params"])
        self.assertEqual(metadata["transcription_failures"], 1)
        self.assertEqual(metadata["transcription_max_retries"], 3)
        self.assertEqual(
            metadata["terminal_error_recovery"]["plan_sha256"],
            plan["plan_sha256"],
        )
        self.assertEqual(
            metadata["terminal_error_recovery"]["source_error_relative_path"],
            source.name,
        )

    def test_apply_guards_require_enable_write_count_and_exact_digest(self) -> None:
        job_id, _ = self._create_terminal_error_job()
        plan = build_terminal_error_recovery_plan(self.config, job_id=job_id)

        with self.assertRaises(TerminalErrorRecoveryWriteDisabledError):
            validate_terminal_error_recovery_apply_guards(
                plan,
                enabled=False,
                allow_write=True,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
            )
        with self.assertRaises(TerminalErrorRecoveryWriteDisabledError):
            validate_terminal_error_recovery_apply_guards(
                plan,
                enabled=True,
                allow_write=False,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
            )
        with self.assertRaisesRegex(TerminalErrorRecoveryContractError, "expected-count 1"):
            validate_terminal_error_recovery_apply_guards(
                plan,
                enabled=True,
                allow_write=True,
                expected_count=2,
                expected_plan_sha256=plan["plan_sha256"],
            )
        with self.assertRaisesRegex(TerminalErrorRecoveryContractError, "exact"):
            validate_terminal_error_recovery_apply_guards(
                plan,
                enabled=True,
                allow_write=True,
                expected_count=1,
                expected_plan_sha256="0" * 64,
            )

    def test_replay_of_same_exact_manifest_is_skipped(self) -> None:
        job_id, _ = self._create_terminal_error_job()
        plan = build_terminal_error_recovery_plan(self.config, job_id=job_id)

        first = apply_terminal_error_recovery_plan(self.config, self.conn, plan)
        second = apply_terminal_error_recovery_plan(self.config, self.conn, plan)

        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "skipped")

    def test_source_tamper_after_plan_is_fail_closed(self) -> None:
        job_id, source = self._create_terminal_error_job()
        plan = build_terminal_error_recovery_plan(self.config, job_id=job_id)
        source.write_bytes(b"mutated")

        with self.assertRaisesRegex(TerminalErrorRecoveryConflictError, "source changed after planning"):
            revalidate_terminal_error_recovery_plan(self.config, self.conn, plan)

    def test_row_tamper_after_plan_is_fail_closed(self) -> None:
        job_id, _ = self._create_terminal_error_job()
        plan = build_terminal_error_recovery_plan(self.config, job_id=job_id)
        db.update_job(self.conn, job_id, error_message="other")

        with self.assertRaisesRegex(TerminalErrorRecoveryConflictError, "row changed after planning"):
            apply_terminal_error_recovery_plan(self.config, self.conn, plan)

    def test_target_tamper_before_apply_is_fail_closed(self) -> None:
        job_id, source = self._create_terminal_error_job()
        plan = build_terminal_error_recovery_plan(self.config, job_id=job_id)
        (self.audio_root / source.name).write_bytes(b"wrong-target")

        with self.assertRaisesRegex(
            TerminalErrorRecoveryConflictError,
            "target exists but does not match the plan",
        ):
            apply_terminal_error_recovery_plan(self.config, self.conn, plan)

    def test_existing_exact_copy_forward_completes_row_transition(self) -> None:
        job_id, source = self._create_terminal_error_job()
        plan = build_terminal_error_recovery_plan(self.config, job_id=job_id)
        target = self.audio_root / source.name
        target.write_bytes(source.read_bytes())

        result = apply_terminal_error_recovery_plan(self.config, self.conn, plan)
        self.assertEqual(result["status"], "applied")
        row = db.get_job(self.conn, job_id)
        assert row is not None
        self.assertEqual(row["status"], db.STATUS_PENDING)
        self.assertEqual(row["canonical_audio_path"], str(target))

    def test_copy_handles_short_write_until_full_chunk_is_persisted(self) -> None:
        job_id, source = self._create_terminal_error_job(payload=b"abcdefghij")
        plan = build_terminal_error_recovery_plan(self.config, job_id=job_id)
        original_write = terminal_recovery.os.write
        short_write_seen = {"done": False}

        def short_write(fd: int, payload: bytes) -> int:
            if not short_write_seen["done"] and len(payload) > 1:
                short_write_seen["done"] = True
                return original_write(fd, payload[:3])
            return original_write(fd, payload)

        with mock.patch.object(terminal_recovery.os, "write", side_effect=short_write):
            result = apply_terminal_error_recovery_plan(self.config, self.conn, plan)

        self.assertEqual(result["status"], "applied")
        self.assertTrue(short_write_seen["done"])
        self.assertEqual(
            (self.audio_root / source.name).read_bytes(),
            source.read_bytes(),
        )

    def test_source_tamper_after_second_revalidate_is_fail_closed(self) -> None:
        job_id, source = self._create_terminal_error_job()
        plan = build_terminal_error_recovery_plan(self.config, job_id=job_id)
        original_revalidate = terminal_recovery.revalidate_terminal_error_recovery_plan
        call_count = {"value": 0}

        def tampering_revalidate(config, conn, manifest):
            result = original_revalidate(config, conn, manifest)
            call_count["value"] += 1
            if call_count["value"] == 2:
                source.write_bytes(b"mutated-after-second-pass")
            return result

        with mock.patch.object(
            terminal_recovery,
            "revalidate_terminal_error_recovery_plan",
            side_effect=tampering_revalidate,
        ):
            with self.assertRaisesRegex(
                TerminalErrorRecoveryConflictError,
                "source changed after second revalidation",
            ):
                apply_terminal_error_recovery_plan(self.config, self.conn, plan)

    def test_kill_switch_drift_after_copy_is_fail_closed(self) -> None:
        job_id, source = self._create_terminal_error_job()
        plan = build_terminal_error_recovery_plan(self.config, job_id=job_id)
        kill_switch = self.root / "controller.stop"
        kill_switch.write_text("stop\n", encoding="utf-8")
        original_copy = terminal_recovery._copy_source_to_target

        def copy_then_clear(**kwargs):
            original_copy(**kwargs)
            kill_switch.unlink()

        with mock.patch.object(
            terminal_recovery,
            "_copy_source_to_target",
            side_effect=copy_then_clear,
        ):
            with self.assertRaisesRegex(
                TerminalErrorRecoveryConflictError,
                "kill switch changed before apply",
            ):
                apply_terminal_error_recovery_plan(
                    self.config,
                    self.conn,
                    plan,
                    kill_switch_path=kill_switch,
                )

        row = db.get_job(self.conn, job_id)
        assert row is not None
        self.assertEqual(row["status"], db.STATUS_ERROR)
        self.assertTrue((self.audio_root / source.name).exists())

    def test_manifest_schema_tamper_is_rejected(self) -> None:
        job_id, _ = self._create_terminal_error_job()
        plan = build_terminal_error_recovery_plan(self.config, job_id=job_id)
        plan["plan_sha256"] = "00" * 32
        with self.assertRaisesRegex(
            TerminalErrorRecoveryContractError,
            "plan SHA-256 does not match its closed payload",
        ):
            validate_terminal_error_recovery_plan(plan)

        manifest_path = self.root / "duplicate-terminal-recovery-plan.json"
        manifest_path.write_text('{"schema_version":"x","schema_version":"y"}', encoding="utf-8")
        with self.assertRaisesRegex(
            TerminalErrorRecoveryContractError,
            "duplicate JSON key",
        ):
            load_terminal_error_recovery_plan(manifest_path)

    def test_config_root_drift_is_fail_closed(self) -> None:
        job_id, _ = self._create_terminal_error_job()
        plan = build_terminal_error_recovery_plan(self.config, job_id=job_id)
        changed_config = json.loads(json.dumps(self.config))
        changed_config["paths"]["error_folder"] = str(self.root / "new-errors")
        (self.root / "new-errors").mkdir(parents=True, exist_ok=True)

        with self.assertRaisesRegex(
            TerminalErrorRecoveryConflictError,
            "db/audio/error/transcript root binding changed after planning",
        ):
            revalidate_terminal_error_recovery_plan(changed_config, self.conn, plan)

    def test_transcript_reference_drift_is_fail_closed(self) -> None:
        job_id, _ = self._create_terminal_error_job()
        plan = build_terminal_error_recovery_plan(self.config, job_id=job_id)
        db.update_job(
            self.conn,
            job_id,
            transcript_txt_path=str(self.transcript_root / "changed.txt"),
            transcript_json_path=str(self.transcript_root / "changed.json"),
        )

        with self.assertRaisesRegex(
            TerminalErrorRecoveryConflictError,
            "row changed after planning",
        ):
            apply_terminal_error_recovery_plan(self.config, self.conn, plan)

    def test_wrong_status_row_is_fail_closed(self) -> None:
        job_id, _ = self._create_terminal_error_job()
        plan = build_terminal_error_recovery_plan(self.config, job_id=job_id)
        db.update_job(self.conn, job_id, status=db.STATUS_DONE, current_step="전체 완료")

        with self.assertRaisesRegex(
            TerminalErrorRecoveryConflictError,
            "row changed after planning",
        ):
            apply_terminal_error_recovery_plan(self.config, self.conn, plan)


class TerminalErrorRecoveryMainIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.audio_root = self.root / "audio"
        self.error_root = self.root / "errors"
        self.transcript_root = self.root / "transcripts"
        self.db_path = self.root / "state" / "jobs.sqlite3"
        for directory in (
            self.audio_root,
            self.error_root,
            self.transcript_root,
            self.db_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.conn = db.init_db(str(self.db_path))
        self.config = {
            "app": {
                "stable_for_sec": 60,
                "stale_processing_hours": 6,
                "transcribe_max_retries": 3,
            },
            "paths": {
                "watch_folder": str(self.root / "watch"),
                "stable_audio_folder": str(self.audio_root),
                "transcript_folder": str(self.transcript_root),
                "error_folder": str(self.error_root),
                "tmp_dir": str(self.root / "tmp"),
                "db_path": str(self.db_path),
            },
            "engine": {"engine": "faster-whisper"},
            "transcribe": {"model_size": "tiny"},
            "ffmpeg": {"binary_path": "/usr/bin/true"},
            "logging": {
                "file": str(self.root / "app.log"),
                "max_bytes": 1024,
                "backup_count": 1,
            },
            "notification": {"provider": "auto", "enabled": False},
        }
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text("paths:\n  db_path: stub\n", encoding="utf-8")
        self.logger = logging.getLogger(f"lecture_stt.test.terminal-recovery.{id(self)}")
        self.logger.handlers = []
        self.logger.addHandler(logging.NullHandler())
        self.logger.setLevel(logging.INFO)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmpdir.cleanup()

    def _create_terminal_error_job(self) -> tuple[int, Path]:
        source = self.error_root / "job-201.m4a"
        source.write_bytes(b"error-audio")
        job_id = db.create_job(
            self.conn,
            status=db.STATUS_ERROR,
            orig_inbox_path="/hidden/job-201.m4a",
            orig_name="job-201.m4a",
            canonical_base="job-201",
            canonical_audio_path=str(source),
            transcript_txt_path=str(self.transcript_root / "job-201.txt"),
            transcript_json_path=str(self.transcript_root / "job-201.json"),
            engine_params={"transcription_failures": 3, "transcription_max_retries": 3},
            current_step="실패: 전사 실행",
            progress_pct=0,
            eta_sec=None,
        )
        db.update_job(
            self.conn,
            job_id,
            sha256=utils.compute_sha256(source),
            error_message="boom",
            error_trace="trace",
            ended_at="2026-07-28T09:30:00+09:00",
            current_step="실패: 전사 실행",
            status=db.STATUS_ERROR,
            canonical_audio_path=str(source),
        )
        return job_id, source

    def _run_main(self, *args: str) -> tuple[str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["lecture-stt", *args]),
            mock.patch.object(stt_main, "load_config", return_value=self.config),
            mock.patch.object(stt_main, "validate_config", return_value=self.config),
            mock.patch.object(stt_main, "setup_logging", return_value=self.logger),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            stt_main.main()
        return stdout.getvalue(), stderr.getvalue()

    def test_cli_plan_and_apply_require_active_kill_switch(self) -> None:
        job_id, _ = self._create_terminal_error_job()
        stdout, _ = self._run_main(
            "--config",
            str(self.config_path),
            "--plan-terminal-error-recovery",
            str(job_id),
        )
        plan = json.loads(stdout)
        self.assertEqual(plan["error_job"]["job_id"], job_id)

        manifest_path = self.root / "terminal-recovery-manifest.json"
        manifest_path.write_text(json.dumps(plan), encoding="utf-8")

        with self.assertRaises(SystemExit) as inactive_exc:
            self._run_main(
                "--config",
                str(self.config_path),
                "--terminal-error-recovery-manifest",
                str(manifest_path),
                "--enable-terminal-error-recovery",
                "--allow-write",
                "--expected-count",
                "1",
                "--expected-plan-sha256",
                plan["plan_sha256"],
                "--controller-kill-switch",
                str(self.root / "controller.stop"),
            )
        self.assertEqual(inactive_exc.exception.code, 2)

        kill_switch = self.root / "controller.stop"
        kill_switch.write_text("stop\n", encoding="utf-8")
        stdout, _ = self._run_main(
            "--config",
            str(self.config_path),
            "--terminal-error-recovery-manifest",
            str(manifest_path),
            "--enable-terminal-error-recovery",
            "--allow-write",
            "--expected-count",
            "1",
            "--expected-plan-sha256",
            plan["plan_sha256"],
            "--controller-kill-switch",
            str(kill_switch),
        )
        result = json.loads(stdout)
        self.assertEqual(result["status"], "applied")
        row = db.get_job(self.conn, job_id)
        assert row is not None
        self.assertEqual(row["status"], db.STATUS_PENDING)


if __name__ == "__main__":
    unittest.main()
