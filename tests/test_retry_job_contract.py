from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.shared import db
from lecture_stt.stt.retry_job import (
    NEXT_PLAN_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    RetryJobConflictError,
    RetryJobContractError,
    RetryJobWriteDisabledError,
    build_next_retry_job_plan,
    build_retry_job_plan,
    load_retry_job_plan,
    revalidate_retry_job_plan,
    validate_next_retry_job_plan,
    validate_retry_apply_guards,
    validate_retry_job_plan,
)


class RetryJobContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.audio_root = self.root / "audio"
        self.audio_root.mkdir()
        self.db_path = self.root / "state" / "jobs.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
                "transcript_folder": str(self.root / "transcripts"),
                "error_folder": str(self.root / "errors"),
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
                        "quality": {
                            "warn_threshold": 0.55,
                            "bad_threshold": 0.70,
                        },
                    }
                },
            },
        }

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _create_retry_job(
        self,
        *,
        name: str = "retry-audio.m4a",
        audio_bytes: bytes = b"retry-audio",
        failures: int = 1,
        max_retries: int = 3,
        status: str = db.STATUS_PENDING,
        current_step: str | None = None,
        canonical_audio_path: Path | None = None,
    ) -> tuple[int, Path]:
        audio_path = canonical_audio_path or (self.audio_root / name)
        if canonical_audio_path is None:
            audio_path.write_bytes(audio_bytes)
        else:
            canonical_audio_path.parent.mkdir(parents=True, exist_ok=True)
            if not canonical_audio_path.exists() and audio_bytes:
                canonical_audio_path.write_bytes(audio_bytes)
        engine_params = {
            "transcription_failures": failures,
            "transcription_max_retries": max_retries,
        }
        step = current_step or f"전사 재시도 대기 {failures}/{max_retries}"
        job_id = db.create_job(
            self.conn,
            status=status,
            orig_inbox_path=f"/hidden/{name}",
            orig_name=name,
            canonical_base=Path(name).stem,
            canonical_audio_path=str(audio_path),
            transcript_txt_path=str(self.root / "transcripts" / f"{Path(name).stem}.txt"),
            transcript_json_path=str(self.root / "transcripts" / f"{Path(name).stem}.json"),
            engine_params=engine_params,
            current_step=step,
            progress_pct=18,
            eta_sec=None,
        )
        return job_id, audio_path

    def test_build_validate_load_and_revalidate_exact_single_candidate(self) -> None:
        job_id, audio_path = self._create_retry_job()

        plan = build_retry_job_plan(self.config)
        manifest_path = self.root / "retry-plan.json"
        manifest_path.write_text(json.dumps(plan), encoding="utf-8")
        loaded = load_retry_job_plan(manifest_path)
        revalidated = revalidate_retry_job_plan(self.config, self.conn, loaded)

        self.assertEqual(plan["schema_version"], PLAN_SCHEMA_VERSION)
        self.assertEqual(plan["expected_count"], 1)
        self.assertEqual(plan["retry_job"]["job_id"], job_id)
        self.assertEqual(plan["retry_job"]["canonical_audio_relative_path"], audio_path.name)
        self.assertEqual(plan["audio"]["relative_path"], audio_path.name)
        self.assertEqual(
            set(plan["bindings"]),
            {
                "db_path_sha256",
                "db_identity",
                "audio_root_sha256",
                "transcript_root_sha256",
            },
        )
        self.assertEqual(
            set(plan["bindings"]["db_identity"]),
            {"device", "inode", "mode", "nlink"},
        )
        self.assertNotIn(str(self.audio_root), json.dumps(plan, ensure_ascii=False))
        self.assertNotIn(str(self.db_path), json.dumps(plan, ensure_ascii=False))
        self.assertEqual(loaded, validate_retry_job_plan(plan))
        self.assertEqual(revalidated["job_id"], job_id)
        self.assertEqual(revalidated["canonical_audio_path"], audio_path)
        self.assertEqual(
            revalidated["claim_fence"]["transcript_txt_path"],
            str(self.root / "transcripts" / "retry-audio.txt"),
        )

    def test_zero_candidates_are_rejected(self) -> None:
        with self.assertRaisesRegex(RetryJobConflictError, "found 0"):
            build_retry_job_plan(self.config)

    def test_missing_retry_schema_is_reported_as_closed_contract_error(self) -> None:
        empty_db = self.root / "state" / "empty.sqlite3"
        sqlite3.connect(empty_db).close()
        config = {
            **self.config,
            "paths": {**self.config["paths"], "db_path": str(empty_db)},
        }

        with self.assertRaisesRegex(
            RetryJobConflictError,
            "unable to read the retry queue",
        ):
            build_retry_job_plan(config)

    def test_multiple_candidates_require_explicit_job_id(self) -> None:
        first_job_id, _ = self._create_retry_job(name="one.m4a")
        second_job_id, _ = self._create_retry_job(name="two.m4a", failures=2)

        with self.assertRaisesRegex(RetryJobConflictError, "pass job_id explicitly"):
            build_retry_job_plan(self.config)

        explicit = build_retry_job_plan(self.config, job_id=second_job_id)
        self.assertEqual(explicit["retry_job"]["job_id"], second_job_id)
        self.assertNotEqual(explicit["retry_job"]["job_id"], first_job_id)

    def test_next_retry_plan_returns_empty_or_oldest_candidate(self) -> None:
        empty = validate_next_retry_job_plan(build_next_retry_job_plan(self.config))
        self.assertEqual(empty["schema_version"], NEXT_PLAN_SCHEMA_VERSION)
        self.assertEqual(empty["status"], "empty")
        self.assertIsNone(empty["plan"])

        first_job_id, _ = self._create_retry_job(name="first.m4a")
        second_job_id, _ = self._create_retry_job(name="second.m4a", failures=2)
        db.update_job(self.conn, first_job_id, updated_at="2026-07-27T08:00:00+09:00")
        db.update_job(self.conn, second_job_id, updated_at="2026-07-27T09:00:00+09:00")

        planned = validate_next_retry_job_plan(build_next_retry_job_plan(self.config))
        self.assertEqual(planned["status"], "planned")
        self.assertEqual(planned["plan"]["retry_job"]["job_id"], first_job_id)

    def test_next_retry_plan_schema_tamper_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(RetryJobContractError, "must set plan to null"):
            validate_next_retry_job_plan(
                {
                    "schema_version": NEXT_PLAN_SCHEMA_VERSION,
                    "status": "empty",
                    "plan": {"unexpected": True},
                }
            )

        self._create_retry_job()
        planned = build_next_retry_job_plan(self.config)
        planned["unexpected"] = True
        with self.assertRaisesRegex(RetryJobContractError, "keys mismatch"):
            validate_next_retry_job_plan(planned)

    def test_ineligible_explicit_job_id_is_rejected(self) -> None:
        job_id, _ = self._create_retry_job(status=db.STATUS_DONE)

        with self.assertRaisesRegex(RetryJobConflictError, "eligible retry job"):
            build_retry_job_plan(self.config, job_id=job_id)

    def test_retry_counter_beyond_maximum_is_rejected(self) -> None:
        job_id, _ = self._create_retry_job(
            failures=3,
            max_retries=2,
            current_step="전사 재시도 대기 3/2",
        )

        with self.assertRaisesRegex(RetryJobConflictError, "invalid retry counters"):
            build_retry_job_plan(self.config, job_id=job_id)

    def test_transcript_outputs_must_be_canonical_direct_children(self) -> None:
        job_id, _ = self._create_retry_job()
        outside_path = self.root / "outside.txt"
        db.update_job(
            self.conn,
            job_id,
            transcript_txt_path=str(outside_path),
        )

        with self.assertRaisesRegex(
            RetryJobConflictError,
            "outside the configured transcript root",
        ):
            build_retry_job_plan(self.config, job_id=job_id)

    def test_plan_tamper_unknown_keys_and_duplicate_json_fail_closed(self) -> None:
        self._create_retry_job()
        plan = build_retry_job_plan(self.config)
        plan["unexpected"] = True
        with self.assertRaisesRegex(RetryJobContractError, "keys mismatch"):
            validate_retry_job_plan(plan)

        manifest_path = self.root / "retry-plan.json"
        manifest_path.write_text('{"schema_version":"x","schema_version":"y"}', encoding="utf-8")
        with self.assertRaisesRegex(RetryJobContractError, "duplicate JSON key"):
            load_retry_job_plan(manifest_path)

    def test_apply_guards_default_disabled_and_exact_digest(self) -> None:
        self._create_retry_job()
        plan = build_retry_job_plan(self.config)

        with self.assertRaises(RetryJobWriteDisabledError):
            validate_retry_apply_guards(
                plan,
                enabled=False,
                allow_write=True,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
            )
        with self.assertRaises(RetryJobWriteDisabledError):
            validate_retry_apply_guards(
                plan,
                enabled=True,
                allow_write=False,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
            )
        with self.assertRaisesRegex(RetryJobContractError, "expected-count 1"):
            validate_retry_apply_guards(
                plan,
                enabled=True,
                allow_write=True,
                expected_count=2,
                expected_plan_sha256=plan["plan_sha256"],
            )
        with self.assertRaisesRegex(RetryJobContractError, "exact"):
            validate_retry_apply_guards(
                plan,
                enabled=True,
                allow_write=True,
                expected_count=1,
                expected_plan_sha256="0" * 64,
            )

    def test_config_profile_drift_is_fail_closed(self) -> None:
        self._create_retry_job()
        plan = build_retry_job_plan(self.config)
        changed_config = json.loads(json.dumps(self.config))
        changed_config["profiles"]["definitions"]["general"]["version"] = "test.2"

        with self.assertRaisesRegex(
            RetryJobConflictError,
            "worker config/profile changed",
        ):
            revalidate_retry_job_plan(changed_config, self.conn, plan)

    def test_db_row_drift_is_fail_closed(self) -> None:
        job_id, _ = self._create_retry_job()
        plan = build_retry_job_plan(self.config, job_id=job_id)
        db.update_job(
            self.conn,
            job_id,
            current_step="전사 재시도 대기 2/3",
            engine_params=json.dumps(
                {"transcription_failures": 2, "transcription_max_retries": 3},
                ensure_ascii=False,
            ),
        )

        with self.assertRaisesRegex(RetryJobConflictError, "row changed after planning"):
            revalidate_retry_job_plan(self.config, self.conn, plan)

    def test_db_path_replacement_is_fail_closed_even_when_row_is_cloned(self) -> None:
        job_id, _ = self._create_retry_job()
        plan = build_retry_job_plan(self.config, job_id=job_id)

        replacement_path = self.root / "replacement.sqlite3"
        clone_conn = sqlite3.connect(str(replacement_path))
        try:
            self.conn.backup(clone_conn)
        finally:
            clone_conn.close()
        os.replace(replacement_path, self.db_path)

        with self.assertRaisesRegex(RetryJobConflictError, "jobs db path identity changed"):
            revalidate_retry_job_plan(self.config, self.conn, plan)

    def test_audio_content_change_is_fail_closed(self) -> None:
        _, audio_path = self._create_retry_job()
        plan = build_retry_job_plan(self.config)
        audio_path.write_bytes(b"mutated-audio")

        with self.assertRaisesRegex(RetryJobConflictError, "retry audio changed after planning"):
            revalidate_retry_job_plan(self.config, self.conn, plan)

    def test_audio_symlink_and_hardlink_are_rejected(self) -> None:
        target = self.audio_root / "target.m4a"
        target.write_bytes(b"target-audio")
        symlink = self.audio_root / "symlink.m4a"
        symlink.symlink_to(target)
        symlink_job_id, _ = self._create_retry_job(
            name="symlink.m4a",
            canonical_audio_path=symlink,
            audio_bytes=b"",
        )
        with self.assertRaisesRegex(RetryJobConflictError, "safely openable"):
            build_retry_job_plan(self.config)

        db.delete_job(self.conn, symlink_job_id)
        hardlink_source = self.audio_root / "hardlink-source.m4a"
        hardlink_source.write_bytes(b"hardlink-source")
        hardlink = self.audio_root / "hardlink.m4a"
        os.link(hardlink_source, hardlink)
        self._create_retry_job(name="hardlink.m4a", canonical_audio_path=hardlink, audio_bytes=b"")
        with self.assertRaisesRegex(RetryJobConflictError, "exactly one hard link"):
            build_retry_job_plan(self.config)

    def test_audio_path_escape_is_rejected(self) -> None:
        outside = self.root / "outside.m4a"
        outside.write_bytes(b"outside-audio")
        self._create_retry_job(name="escaped.m4a", canonical_audio_path=outside, audio_bytes=b"")

        with self.assertRaisesRegex(RetryJobConflictError, "outside the configured stable audio root"):
            build_retry_job_plan(self.config)

    def test_non_regular_audio_file_is_rejected(self) -> None:
        fifo = self.audio_root / "retry.fifo"
        os.mkfifo(fifo)
        self._create_retry_job(name="retry.fifo", canonical_audio_path=fifo, audio_bytes=b"")

        with self.assertRaisesRegex(RetryJobConflictError, "regular file"):
            build_retry_job_plan(self.config)

    def test_retry_metadata_mismatch_is_rejected(self) -> None:
        job_id, _ = self._create_retry_job()
        db.update_job(
            self.conn,
            job_id,
            engine_params=json.dumps(
                {"transcription_failures": 9, "transcription_max_retries": 3},
                ensure_ascii=False,
            ),
        )

        with self.assertRaisesRegex(RetryJobConflictError, "metadata does not match"):
            build_retry_job_plan(self.config, job_id=job_id)


if __name__ == "__main__":
    unittest.main()
