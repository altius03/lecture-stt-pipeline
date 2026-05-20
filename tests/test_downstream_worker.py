from __future__ import annotations

import tempfile
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
from lecture_stt.downstream.worker import ScanStatsReporter, load_worker_config  # noqa: E402


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

        with tempfile.TemporaryDirectory() as tmp:
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

        with tempfile.TemporaryDirectory() as tmp:
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
        with tempfile.TemporaryDirectory() as tmp:
            config_path = self._write_config(Path(tmp), override_line="stats_heartbeat_scans: 0")

            with self.assertRaisesRegex(ValueError, "downstream.stats_heartbeat_scans"):
                load_worker_config(str(config_path))

    def test_numeric_zero_log_suppression_max_keys_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = self._write_config(Path(tmp), override_line="log_suppression_max_keys: 0")

            with self.assertRaisesRegex(ValueError, "downstream.log_suppression_max_keys"):
                load_worker_config(str(config_path))

    def test_log_routine_scan_events_accepts_boolean_string_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = self._write_config(Path(tmp), override_line='log_routine_scan_events: "false"')

            config = load_worker_config(str(config_path))

        self.assertFalse(config.log_routine_scan_events)

    def test_log_routine_scan_events_rejects_invalid_boolean_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = self._write_config(Path(tmp), override_line="log_routine_scan_events: maybe")

            with self.assertRaisesRegex(ValueError, "downstream.log_routine_scan_events"):
                load_worker_config(str(config_path))

    def test_default_downstream_jsonl_rotation_matches_retention_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = self._write_config(Path(tmp), override_line="log_routine_scan_events: false")

            config = load_worker_config(str(config_path))

        self.assertEqual(config.log_jsonl_max_bytes, 10 * 1024 * 1024)
        self.assertEqual(config.log_jsonl_backup_count, 5)


if __name__ == "__main__":
    unittest.main()
