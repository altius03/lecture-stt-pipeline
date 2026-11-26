from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.stt.single_job import (
    PLAN_SCHEMA_VERSION,
    SingleJobContractError,
    SingleJobConflictError,
    SingleJobWriteDisabledError,
    build_single_job_plan,
    load_single_job_plan,
    revalidate_single_job_plan,
    validate_apply_guards,
    validate_single_job_plan,
)


class SingleJobContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.watch_root = self.root / "inbox"
        self.watch_root.mkdir()
        self.source = self.watch_root / "강의.m4a"
        self.source.write_bytes(b"stable audio bytes")
        source_stat = self.source.stat()
        os.utime(
            self.source,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns - 120_000_000_000),
        )
        self.config = {
            "app": {
                "stable_for_sec": 90,
                "stale_processing_hours": 6,
                "transcribe_max_retries": 2,
            },
            "paths": {
                "watch_folder": str(self.watch_root),
                "stable_audio_folder": str(self.root / "audio"),
                "transcript_folder": str(self.root / "transcripts"),
                "error_folder": str(self.root / "errors"),
                "tmp_dir": str(self.root / "tmp"),
                "db_path": str(self.root / "state" / "jobs.sqlite3"),
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
        self._age_source(self.source)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _age_source(self, path: Path, *, age_sec: int = 120) -> None:
        now = os.path.getmtime(path)
        aged = now - age_sec
        os.utime(path, (aged, aged))

    def test_build_validate_and_revalidate_closed_plan(self) -> None:
        plan = build_single_job_plan(self.config, self.source.name)

        self.assertEqual(plan["schema_version"], PLAN_SCHEMA_VERSION)
        self.assertEqual(plan["expected_count"], 1)
        self.assertEqual(plan["source"]["relative_path"], self.source.name)
        self.assertNotIn("stable audio bytes", json.dumps(plan, ensure_ascii=False))
        self.assertEqual(
            revalidate_single_job_plan(self.config, plan),
            self.source,
        )

    def test_source_change_after_plan_is_fail_closed(self) -> None:
        plan = build_single_job_plan(self.config, self.source.name)
        self.source.write_bytes(b"changed")
        self._age_source(self.source)

        with self.assertRaisesRegex(
            SingleJobConflictError,
            "source changed after planning",
        ):
            revalidate_single_job_plan(self.config, plan)

    def test_fresh_source_is_rejected_until_stability_window_passes(self) -> None:
        fresh_source = self.watch_root / "fresh.m4a"
        fresh_source.write_bytes(b"still-arriving")
        with self.assertRaisesRegex(SingleJobConflictError, "stable_for_sec=90"):
            build_single_job_plan(self.config, fresh_source.name)

    def test_worker_profile_change_after_plan_is_fail_closed(self) -> None:
        plan = build_single_job_plan(self.config, self.source.name)
        changed_config = json.loads(json.dumps(self.config))
        changed_config["profiles"]["definitions"]["general"]["version"] = "test.2"

        with self.assertRaisesRegex(
            SingleJobConflictError,
            "worker config/profile changed",
        ):
            revalidate_single_job_plan(changed_config, plan)

    def test_plan_digest_and_unknown_keys_are_fail_closed(self) -> None:
        plan = build_single_job_plan(self.config, self.source.name)
        plan["source"]["size_bytes"] += 1
        with self.assertRaisesRegex(SingleJobContractError, "SHA-256"):
            validate_single_job_plan(plan)

        plan = build_single_job_plan(self.config, self.source.name)
        plan["unexpected"] = True
        with self.assertRaisesRegex(SingleJobContractError, "keys mismatch"):
            validate_single_job_plan(plan)

    def test_nested_absolute_symlink_and_hardlink_sources_are_rejected(self) -> None:
        nested = self.watch_root / "nested"
        nested.mkdir()
        (nested / "audio.m4a").write_bytes(b"audio")
        with self.assertRaisesRegex(SingleJobContractError, "direct child"):
            build_single_job_plan(self.config, "nested/audio.m4a")
        with self.assertRaisesRegex(SingleJobContractError, "direct child"):
            build_single_job_plan(self.config, str(self.source))

        symlink = self.watch_root / "link.m4a"
        symlink.symlink_to(self.source)
        with self.assertRaisesRegex(SingleJobConflictError, "safely openable"):
            build_single_job_plan(self.config, symlink.name)

        hardlink = self.watch_root / "hardlink.m4a"
        os.link(self.source, hardlink)
        with self.assertRaisesRegex(SingleJobConflictError, "exactly one hard link"):
            build_single_job_plan(self.config, hardlink.name)

    def test_source_name_rejects_traversal_path(self) -> None:
        with self.assertRaisesRegex(
            SingleJobContractError,
            "direct child",
        ):
            build_single_job_plan(self.config, "../강의.m4a")

    def test_watcher_excluded_temporary_source_name_is_rejected(self) -> None:
        temporary_source = self.watch_root / ".upload.part"
        temporary_source.write_bytes(b"partial")
        with self.assertRaisesRegex(SingleJobContractError, "polling watcher"):
            build_single_job_plan(self.config, temporary_source.name)

    def test_special_source_file_is_rejected_without_blocking(self) -> None:
        fifo = self.watch_root / "audio.fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(SingleJobConflictError, "regular file"):
            build_single_job_plan(self.config, fifo.name)

    def test_fresh_source_before_stability_window_is_rejected(self) -> None:
        fresh = self.watch_root / "fresh.m4a"
        fresh.write_bytes(b"fresh")
        with self.assertRaisesRegex(
            SingleJobConflictError,
            "stable_for_sec",
        ):
            build_single_job_plan(self.config, fresh.name)

    def test_manifest_loader_rejects_duplicate_json_keys(self) -> None:
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(
            '{"schema_version":"x","schema_version":"y"}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SingleJobContractError, "duplicate JSON key"):
            load_single_job_plan(manifest_path)

    def test_config_change_after_plan_is_fail_closed(self) -> None:
        plan = build_single_job_plan(self.config, self.source.name)
        changed_config = json.loads(json.dumps(self.config))
        changed_config["app"]["transcribe_max_retries"] = 9

        with self.assertRaisesRegex(
            SingleJobConflictError,
            "worker config/profile changed",
        ):
            revalidate_single_job_plan(changed_config, plan)

    def test_manifest_loader_accepts_exact_plan_and_rejects_symlink(self) -> None:
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(
            json.dumps(build_single_job_plan(self.config, self.source.name)),
            encoding="utf-8",
        )
        self.assertEqual(load_single_job_plan(manifest_path)["expected_count"], 1)

        symlink_path = self.root / "manifest-link.json"
        symlink_path.symlink_to(manifest_path)
        with self.assertRaisesRegex(SingleJobContractError, "invalid single-job manifest"):
            load_single_job_plan(symlink_path)

    def test_execution_guards_require_enable_write_count_and_exact_digest(self) -> None:
        plan = build_single_job_plan(self.config, self.source.name)

        with self.assertRaises(SingleJobWriteDisabledError):
            validate_apply_guards(
                plan,
                enabled=False,
                allow_write=True,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
            )
        with self.assertRaises(SingleJobWriteDisabledError):
            validate_apply_guards(
                plan,
                enabled=True,
                allow_write=False,
                expected_count=1,
                expected_plan_sha256=plan["plan_sha256"],
            )
        with self.assertRaisesRegex(SingleJobContractError, "expected-count 1"):
            validate_apply_guards(
                plan,
                enabled=True,
                allow_write=True,
                expected_count=2,
                expected_plan_sha256=plan["plan_sha256"],
            )
        with self.assertRaisesRegex(SingleJobContractError, "exact"):
            validate_apply_guards(
                plan,
                enabled=True,
                allow_write=True,
                expected_count=1,
                expected_plan_sha256="0" * 64,
            )

        validate_apply_guards(
            plan,
            enabled=True,
            allow_write=True,
            expected_count=1,
            expected_plan_sha256=plan["plan_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
