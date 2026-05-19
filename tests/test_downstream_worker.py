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

from lecture_stt.downstream.worker import ScanStatsReporter, load_worker_config  # noqa: E402


class DownstreamWorkerReportingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
