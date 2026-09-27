from __future__ import annotations

import tempfile
import json
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.downstream import worker as downstream_worker  # noqa: E402
from lecture_stt.downstream.lib import DownstreamConfig, SubjectRoute  # noqa: E402
from lecture_stt.downstream.semester import SemesterManifestError  # noqa: E402
from lecture_stt.downstream.worker import (  # noqa: E402
    ScanStatsReporter,
    load_transcript_worker_config,
    load_worker_config,
)

CANONICAL_TEMP_ROOT = "/private/tmp" if sys.platform == "darwin" else tempfile.gettempdir()


class DownstreamWorkerReportingTests(unittest.TestCase):
    def _config(self, root: Path, *, log_routine_scan_events: bool) -> DownstreamConfig:
        return DownstreamConfig(
            correction_dir=root / "03_correction",
            summary_dir=root / "04_summarize",
            gh_current_semester_root=root / "GH_archive",
            obsidian_semester_root=root / "Obsidian",
            db_path=root / "state" / "jobs.sqlite3",
            log_jsonl_path=root / "logs" / "downstream.jsonl",
            lock_path=root / "state" / "downstream.lock",
            scan_interval_sec=1,
            stable_for_sec=1,
            subjects={"DS": SubjectRoute("DS", "DS", "Data Structures")},
            stats_heartbeat_scans=3,
            log_routine_scan_events=log_routine_scan_events,
        )

    def test_scan_stats_reporter_suppresses_unchanged_stats_until_heartbeat(self) -> None:
        logger = mock.Mock()
        reporter = ScanStatsReporter(logger, heartbeat_scans=3)
        stats = {
            "correction_delivered": 0,
            "summary_delivered": 0,
            "blocked": 26,
            "incomplete": 3,
            "conflicts": 26,
            "errors": 12,
        }

        reporter.log(stats, dry_run=False)
        reporter.log(dict(stats), dry_run=False)
        reporter.log(dict(stats), dry_run=False)
        reporter.log(dict(stats), dry_run=False)
        reporter.log({**stats, "errors": 13}, dry_run=False)

        self.assertEqual(logger.info.call_count, 3)
        first_message = logger.info.call_args_list[0].args[0]
        heartbeat_message = logger.info.call_args_list[1].args[0]
        changed_message = logger.info.call_args_list[2].args[0]
        self.assertIn("downstream scan stats", first_message)
        self.assertIn("heartbeat", heartbeat_message)
        self.assertIn("changed", changed_message)

    def test_run_worker_suppresses_scan_stats_stdout_when_routine_logging_disabled(self) -> None:
        class FakeDistributor:
            def __init__(self, config: DownstreamConfig, *, dry_run: bool) -> None:
                self.config = config
                self.dry_run = dry_run

            def scan_once(self) -> dict[str, int]:
                return {"correction_delivered": 0, "summary_delivered": 0}

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as tmp:
            config = self._config(Path(tmp), log_routine_scan_events=False)
            logger = mock.Mock()
            with (
                mock.patch.object(downstream_worker, "DownstreamDistributor", FakeDistributor),
                mock.patch.object(downstream_worker, "logger", logger),
            ):
                downstream_worker.run_worker(config, dry_run=False, run_once=True)

        logger.info.assert_not_called()

    def test_run_worker_emits_scan_stats_stdout_when_routine_logging_enabled(self) -> None:
        class FakeDistributor:
            def __init__(self, config: DownstreamConfig, *, dry_run: bool) -> None:
                self.config = config
                self.dry_run = dry_run

            def scan_once(self) -> dict[str, int]:
                return {"correction_delivered": 1, "summary_delivered": 0}

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as tmp:
            config = self._config(Path(tmp), log_routine_scan_events=True)
            logger = mock.Mock()
            with (
                mock.patch.object(downstream_worker, "DownstreamDistributor", FakeDistributor),
                mock.patch.object(downstream_worker, "logger", logger),
            ):
                downstream_worker.run_worker(config, dry_run=False, run_once=True)

        logger.info.assert_called_once()
        self.assertIn("downstream scan stats initial", logger.info.call_args.args[0])


class DownstreamWorkerConfigTests(unittest.TestCase):
    def _write_config(self, root: Path, *, override_line: str) -> Path:
        config_path = root / "config.yaml"
        config_path.write_text(
            (
                "paths:\n"
                f"  db_path: {root / 'state' / 'jobs.sqlite3'}\n"
                "downstream:\n"
                f"  correction_folder: {root / '03_correction'}\n"
                f"  summary_folder: {root / '04_summarize'}\n"
                f"  gh_current_semester_root: {root / 'GH_archive'}\n"
                f"  obsidian_semester_root: {root / 'Obsidian'}\n"
                f"  {override_line}\n"
            ),
            encoding="utf-8",
        )
        return config_path

    def test_numeric_zero_stats_heartbeat_scans_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as tmp:
            config_path = self._write_config(Path(tmp), override_line="stats_heartbeat_scans: 0")

            with self.assertRaisesRegex(ValueError, "downstream.stats_heartbeat_scans"):
                load_worker_config(str(config_path))

    def test_numeric_zero_log_suppression_max_keys_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as tmp:
            config_path = self._write_config(Path(tmp), override_line="log_suppression_max_keys: 0")

            with self.assertRaisesRegex(ValueError, "downstream.log_suppression_max_keys"):
                load_worker_config(str(config_path))

    def test_log_routine_scan_events_accepts_boolean_string_false(self) -> None:
        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as tmp:
            config_path = self._write_config(Path(tmp), override_line='log_routine_scan_events: "false"')

            config = load_worker_config(str(config_path))

        self.assertFalse(config.log_routine_scan_events)

    def test_log_routine_scan_events_rejects_invalid_boolean_string(self) -> None:
        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as tmp:
            config_path = self._write_config(Path(tmp), override_line="log_routine_scan_events: maybe")

            with self.assertRaisesRegex(ValueError, "downstream.log_routine_scan_events"):
                load_worker_config(str(config_path))

    def test_default_downstream_jsonl_rotation_matches_retention_policy(self) -> None:
        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as tmp:
            config_path = self._write_config(Path(tmp), override_line="log_routine_scan_events: false")

            config = load_worker_config(str(config_path))

        self.assertEqual(config.log_jsonl_max_bytes, 10 * 1024 * 1024)
        self.assertEqual(config.log_jsonl_backup_count, 5)


class TranscriptWorkerConfigTests(unittest.TestCase):
    def _active_snapshot(self, root: Path) -> Path:
        active_root = root / "vault"
        (active_root / ".obsidian").mkdir(parents=True)
        semester_root = active_root / "01_current"
        course_root = semester_root / "cs201"
        (course_root / "06_lecture_notes" / "02_origin").mkdir(parents=True)
        (course_root / "06_lecture_notes" / "01_summarize").mkdir(parents=True)
        payload = {
            "schema_version": "lecture-stt/active-semester@2",
            "activated_at": "2026-01-01T00:00:00+09:00",
            "activation_plan_sha256": "0" * 64,
            "manifest_source_path": str(root / "semester.yaml"),
            "manifest_source_sha256": "0" * 64,
            "expected_course_count": 1,
            "semester": "2026-1",
            "vault_root": str(active_root),
            "semester_root": str(semester_root),
            "timetable_db_path": str(root / "state" / "timetable.sqlite3"),
            "origin_subdir": "06_lecture_notes/02_origin",
            "summary_subdir": "06_lecture_notes/01_summarize",
            "match_margin_minutes": 30,
            "courses": [
                {
                    "course_code": "CS201",
                    "course_name": "Data Structures",
                    "course_dir": "cs201",
                    "aliases": ["CA_2"],
                }
            ],
            "selected_timetable_courses": [
                {"course_code": "CS201", "course_name": "Data Structures"}
            ],
            "timetable_selection_plan_sha256": "0" * 64,
        }
        path = root / "active-semester.json"
        (root / "state").mkdir(parents=True, exist_ok=True)
        (root / "state" / "timetable.sqlite3").touch()
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _write_transcript_config(
        self,
        root: Path,
        *,
        active_path: Path,
        enabled: bool,
        scan_interval_sec: str | int = 30,
        batch_size: str | int = 2,
    ) -> Path:
        correction_dir = root / "03_correction"
        summary_dir = root / "04_summarize"
        correction_dir.mkdir(parents=True, exist_ok=True)
        summary_dir.mkdir(parents=True, exist_ok=True)
        config_path = root / "config.yaml"
        config_path.write_text(
            (
                "paths:\n"
                f"  db_path: {root / 'state' / 'jobs.sqlite3'}\n"
                "transcript_delivery:\n"
                f"  enabled: {'true' if enabled else 'false'}\n"
                f"  active_semester_manifest: {active_path}\n"
                "  activation_cutoff: '2026-01-01T00:00:00+09:00'\n"
                f"  correction_staging_dir: {correction_dir}\n"
                f"  summary_staging_dir: {summary_dir}\n"
                "  generator:\n"
                "    backend: codex_cli\n"
                "    codex_binary: /usr/bin/true\n"
                "    model: test-model\n"
                "    reasoning_effort: low\n"
                "    timeout_sec: 30\n"
                "    max_attempts: 3\n"
                "    max_correction_chars: 100000\n"
                "    max_summary_chars: 100000\n"
                f"  scan_interval_sec: {scan_interval_sec}\n"
                f"  batch_size: {batch_size}\n"
                f"  log_jsonl_path: {root / 'logs' / 'transcript-delivery.jsonl'}\n"
                "  log_jsonl_max_bytes: 1024\n"
                "  log_jsonl_backup_count: 1\n"
                f"  lock_path: {root / 'state' / 'transcript-delivery.lock'}\n"
            ),
            encoding="utf-8",
        )
        return config_path

    def test_enabled_false_does_not_require_active_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as tmp:
            root = Path(tmp)
            config_path = self._write_transcript_config(
                root,
                active_path=Path(root / "missing-active.json"),
                enabled=False,
            )

            config = load_transcript_worker_config(str(config_path))

        self.assertFalse(config.enabled)

    def test_enabled_true_rejects_missing_active_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as tmp:
            root = Path(tmp)
            config_path = self._write_transcript_config(
                root,
                active_path=Path(root / "missing-active.json"),
                enabled=True,
            )

            with self.assertRaisesRegex(
                SemesterManifestError,
                "active_semester_manifest does not exist|does not exist",
            ):
                load_transcript_worker_config(str(config_path))

    def test_enabled_true_requires_positive_integer_interval_and_batch(self) -> None:
        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as tmp:
            root = Path(tmp)
            active_path = self._active_snapshot(root)
            config_path = self._write_transcript_config(
                root,
                active_path=active_path,
                enabled=True,
                scan_interval_sec=0,
                batch_size=2,
            )
            with self.assertRaisesRegex(
                ValueError,
                "transcript_delivery\\.scan_interval_sec must be greater than 0",
            ):
                load_transcript_worker_config(str(config_path))

            config_path = self._write_transcript_config(
                root,
                active_path=active_path,
                enabled=True,
                scan_interval_sec=30,
                batch_size=0,
            )
            with self.assertRaisesRegex(
                ValueError,
                "transcript_delivery.batch_size must be greater than 0",
            ):
                load_transcript_worker_config(str(config_path))

    def test_dry_run_plans_reconciliation_without_mutating_db_or_files(self) -> None:
        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as tmp:
            root = Path(tmp)
            active_path = self._active_snapshot(root)
            source_txt = root / "source.txt"
            source_json = root / "source.json"
            source_txt.write_text("dry-run lecture", encoding="utf-8")
            source_json.write_text('{"text":"dry-run lecture"}', encoding="utf-8")
            db_path = root / "state" / "jobs.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    orig_name TEXT NOT NULL,
                    canonical_base TEXT NOT NULL,
                    transcript_txt_path TEXT,
                    transcript_json_path TEXT,
                    engine_params TEXT
                );
                """
            )
            timestamp = "2026-01-01T10:30:00+09:00"
            conn.execute(
                """
                INSERT INTO jobs(
                    id, status, created_at, updated_at, started_at, ended_at,
                    orig_name, canonical_base, transcript_txt_path,
                    transcript_json_path, engine_params
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "DONE",
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    "260101CA_2.m4a",
                    "260101CA_2",
                    str(source_txt),
                    str(source_json),
                    json.dumps(
                        {
                            "profile": {"key": "LECTURE"},
                            "metadata": {"recorded_at": timestamp},
                        }
                    ),
                ),
            )
            conn.commit()
            conn.close()
            before = db_path.read_bytes()
            log_path = root / "logs" / "transcript-delivery.jsonl"
            lock_path = root / "state" / "transcript-delivery.lock"
            config_path = self._write_transcript_config(
                root,
                active_path=active_path,
                enabled=True,
                batch_size=10,
            )
            config = load_transcript_worker_config(str(config_path))

            downstream_worker.run_transcript_worker(
                config,
                dry_run=True,
                run_once=True,
            )

            self.assertEqual(db_path.read_bytes(), before)
            verification = sqlite3.connect(db_path)
            try:
                table_count = verification.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'transcript_postprocess_jobs'"
                ).fetchone()[0]
            finally:
                verification.close()
            self.assertEqual(table_count, 0)
            origin_dir = root / "vault" / "01_current" / "cs201" / "06_lecture_notes" / "02_origin"
            self.assertEqual(list(origin_dir.iterdir()), [])
            self.assertFalse(log_path.exists())
            self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
