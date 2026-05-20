from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.downstream.lib import (  # noqa: E402
    CORRECTION_STATUS_CONFLICT,
    CORRECTION_STATUS_DELIVERED,
    CORRECTION_STATUS_ERROR,
    CORRECTION_STATUS_INCOMPLETE,
    SUMMARY_STATUS_BLOCKED,
    SUMMARY_STATUS_CONFLICT,
    SUMMARY_STATUS_DELIVERED,
    DownstreamConfig,
    DownstreamDistributor,
    JsonlLogger,
    default_subject_routes,
)
from lecture_stt.shared import db  # noqa: E402


class DownstreamDistributorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.correction_dir = self.root / "03_correction"
        self.summary_dir = self.root / "04_summarize"
        self.gh_root = self.root / "GH_archive" / "01_TUK" / "01_current_semester"
        self.obsidian_root = self.root / "obsidian" / "StudyVaults" / "2-1"
        self.log_jsonl = self.root / "state" / "logs" / "downstream.jsonl"
        self.lock_path = self.root / "state" / "downstream.lock"
        self.db_path = self.root / "state" / "jobs.sqlite3"

        for directory in (
            self.correction_dir,
            self.summary_dir,
            self.gh_root,
            self.obsidian_root,
            self.log_jsonl.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.conn = db.init_db(str(self.db_path))
        self.config = DownstreamConfig(
            correction_dir=self.correction_dir,
            summary_dir=self.summary_dir,
            gh_current_semester_root=self.gh_root,
            obsidian_semester_root=self.obsidian_root,
            db_path=self.db_path,
            log_jsonl_path=self.log_jsonl,
            lock_path=self.lock_path,
            scan_interval_sec=1,
            stable_for_sec=0,
            subjects=default_subject_routes(),
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.tmpdir.cleanup()

    def _make_distributor(self, *, dry_run: bool = False) -> DownstreamDistributor:
        return DownstreamDistributor(self.config, dry_run=dry_run, conn=self.conn)

    def _write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _write_json(self, path: Path, content: str) -> None:
        self._write_text(path, content)

    def _route(self, abbr: str):
        return self.config.subjects[abbr]

    def test_correction_pair_is_delivered_as_a_unit(self) -> None:
        stem = "260316LC_1"
        self._write_text(self.correction_dir / f"{stem}.txt", "lecture text")
        self._write_json(self.correction_dir / f"{stem}.json", '{"segments":[{"text":"lecture text"}]}')

        distributor = self._make_distributor()
        stats = distributor.scan_once()
        distributor.close()

        route = self._route("LC")
        self.assertEqual(stats["correction_delivered"], 1)
        self.assertTrue((route.gh_origin_dir(self.gh_root) / f"{stem}.txt").exists())
        self.assertTrue((route.gh_origin_dir(self.gh_root) / f"{stem}.json").exists())
        self.assertFalse((self.correction_dir / f"{stem}.txt").exists())
        self.assertFalse((self.correction_dir / f"{stem}.json").exists())

        row = db.get_delivery(self.conn, stem)
        self.assertIsNotNone(row)
        self.assertEqual(row["correction_status"], CORRECTION_STATUS_DELIVERED)
        self.assertEqual(int(row["tuk_origin_done"]), 1)

    def test_summary_requires_and_follows_correction_delivery(self) -> None:
        stem = "260316DStr_2"
        self._write_text(self.correction_dir / f"{stem}.txt", "corrected transcript")
        self._write_json(self.correction_dir / f"{stem}.json", '{"segments":[{"text":"corrected transcript"}]}')
        self._write_text(self.summary_dir / f"{stem}.md", "# summary")

        distributor = self._make_distributor()
        stats = distributor.scan_once()
        distributor.close()

        route = self._route("DStr")
        self.assertEqual(stats["correction_delivered"], 1)
        self.assertEqual(stats["summary_delivered"], 1)
        self.assertTrue((route.gh_summary_dir(self.gh_root) / f"{stem}.md").exists())
        self.assertTrue((route.obsidian_summary_dir(self.obsidian_root) / f"{stem}.md").exists())
        self.assertFalse((self.summary_dir / f"{stem}.md").exists())

        row = db.get_delivery(self.conn, stem)
        self.assertEqual(row["summary_status"], SUMMARY_STATUS_DELIVERED)
        self.assertEqual(int(row["tuk_summary_done"]), 1)
        self.assertEqual(int(row["obsidian_done"]), 1)

    def test_rerun_is_idempotent_with_identical_existing_content(self) -> None:
        stem = "260316OOP_1"
        txt_source = self.correction_dir / f"{stem}.txt"
        json_source = self.correction_dir / f"{stem}.json"
        md_source = self.summary_dir / f"{stem}.md"
        self._write_text(txt_source, "same correction")
        self._write_json(json_source, '{"segments":[{"text":"same correction"}]}')
        self._write_text(md_source, "# same summary")

        distributor = self._make_distributor()
        distributor.scan_once()
        distributor.close()

        self._write_text(txt_source, "same correction")
        self._write_json(json_source, '{"segments":[{"text":"same correction"}]}')
        self._write_text(md_source, "# same summary")

        distributor = self._make_distributor()
        stats = distributor.scan_once()
        distributor.close()

        self.assertEqual(stats["correction_delivered"], 1)
        self.assertEqual(stats["summary_delivered"], 1)
        self.assertFalse(txt_source.exists())
        self.assertFalse(json_source.exists())
        self.assertFalse(md_source.exists())

    def test_summary_conflict_never_overwrites_destination(self) -> None:
        stem = "260316LA_1"
        self._write_text(self.correction_dir / f"{stem}.txt", "correction")
        self._write_json(self.correction_dir / f"{stem}.json", '{"segments":[{"text":"correction"}]}')
        self._write_text(self.summary_dir / f"{stem}.md", "# new summary")

        route = self._route("LA")
        existing_destination = route.gh_summary_dir(self.gh_root) / f"{stem}.md"
        self._write_text(existing_destination, "# old summary")

        distributor = self._make_distributor()
        stats = distributor.scan_once()
        distributor.close()

        self.assertEqual(stats["conflicts"], 1)
        self.assertEqual(existing_destination.read_text(encoding="utf-8"), "# old summary")
        self.assertTrue((self.summary_dir / f"{stem}.md").exists())

        row = db.get_delivery(self.conn, stem)
        self.assertEqual(row["summary_status"], SUMMARY_STATUS_CONFLICT)

    def test_correction_conflict_rolls_back_first_copy(self) -> None:
        stem = "260316LC_3"
        self._write_text(self.correction_dir / f"{stem}.txt", "new correction text")
        self._write_json(self.correction_dir / f"{stem}.json", '{"segments":[{"text":"new correction text"}]}')

        route = self._route("LC")
        json_destination = route.gh_origin_dir(self.gh_root) / f"{stem}.json"
        self._write_json(json_destination, '{"segments":[{"text":"old correction text"}]}')

        distributor = self._make_distributor()
        stats = distributor.scan_once()
        distributor.close()

        txt_destination = route.gh_origin_dir(self.gh_root) / f"{stem}.txt"
        self.assertEqual(stats["conflicts"], 1)
        self.assertFalse(txt_destination.exists())
        self.assertEqual(json_destination.read_text(encoding="utf-8"), '{"segments":[{"text":"old correction text"}]}')
        self.assertTrue((self.correction_dir / f"{stem}.txt").exists())
        self.assertTrue((self.correction_dir / f"{stem}.json").exists())

        row = db.get_delivery(self.conn, stem)
        self.assertEqual(row["correction_status"], CORRECTION_STATUS_CONFLICT)
        self.assertEqual(int(row["tuk_origin_done"]), 0)

    def test_summary_partial_success_is_persisted_when_obsidian_conflicts(self) -> None:
        stem = "260316DStr_3"
        self._write_text(self.correction_dir / f"{stem}.txt", "correction")
        self._write_json(self.correction_dir / f"{stem}.json", '{"segments":[{"text":"correction"}]}')
        self._write_text(self.summary_dir / f"{stem}.md", "# summary")

        route = self._route("DStr")
        obsidian_destination = route.obsidian_summary_dir(self.obsidian_root) / f"{stem}.md"
        self._write_text(obsidian_destination, "# existing conflict")

        distributor = self._make_distributor()
        stats = distributor.scan_once()
        distributor.close()

        tuk_destination = route.gh_summary_dir(self.gh_root) / f"{stem}.md"
        self.assertEqual(stats["correction_delivered"], 1)
        self.assertEqual(stats["conflicts"], 1)
        self.assertTrue(tuk_destination.exists())
        self.assertEqual(obsidian_destination.read_text(encoding="utf-8"), "# existing conflict")
        self.assertTrue((self.summary_dir / f"{stem}.md").exists())

        row = db.get_delivery(self.conn, stem)
        self.assertEqual(row["summary_status"], SUMMARY_STATUS_CONFLICT)
        self.assertEqual(int(row["tuk_summary_done"]), 1)
        self.assertEqual(int(row["obsidian_done"]), 0)

    def test_incomplete_correction_pair_is_not_distributed(self) -> None:
        stem = "260316DS_1"
        self._write_text(self.correction_dir / f"{stem}.txt", "only txt exists")

        distributor = self._make_distributor()
        stats = distributor.scan_once()
        distributor.close()

        self.assertEqual(stats["incomplete"], 1)
        self.assertTrue((self.correction_dir / f"{stem}.txt").exists())

        row = db.get_delivery(self.conn, stem)
        self.assertEqual(row["correction_status"], CORRECTION_STATUS_INCOMPLETE)

    def test_unknown_subject_is_recorded_as_error(self) -> None:
        stem = "260316ZZ_1"
        self._write_text(self.correction_dir / f"{stem}.txt", "bad subject")
        self._write_json(self.correction_dir / f"{stem}.json", '{"segments":[{"text":"bad subject"}]}')

        distributor = self._make_distributor()
        stats = distributor.scan_once()
        distributor.close()

        self.assertEqual(stats["errors"], 1)
        row = db.get_delivery(self.conn, stem)
        self.assertEqual(row["correction_status"], CORRECTION_STATUS_ERROR)
        self.assertEqual(row["subject_abbr"], "ZZ")

    def test_repeated_invalid_correction_is_not_logged_every_scan(self) -> None:
        stem = "260316ZZ_1"
        self._write_text(self.correction_dir / f"{stem}.txt", "bad subject")
        self._write_json(self.correction_dir / f"{stem}.json", '{"segments":[{"text":"bad subject"}]}')

        distributor = self._make_distributor()
        first_stats = distributor.scan_once()
        second_stats = distributor.scan_once()
        distributor.close()

        self.assertEqual(first_stats["errors"], 1)
        self.assertEqual(second_stats["errors"], 1)
        content = self.log_jsonl.read_text(encoding="utf-8")
        self.assertEqual(content.count('"event": "correction_invalid"'), 1)

    def test_repeated_correction_conflict_is_not_logged_every_scan(self) -> None:
        stem = "260316LC_9"
        self._write_text(self.correction_dir / f"{stem}.txt", "new correction")
        self._write_json(self.correction_dir / f"{stem}.json", '{"segments":[{"text":"new correction"}]}')
        route = self._route("LC")
        self._write_text(route.gh_origin_dir(self.gh_root) / f"{stem}.txt", "existing correction")

        distributor = self._make_distributor()
        first_stats = distributor.scan_once()
        second_stats = distributor.scan_once()
        distributor.close()

        self.assertEqual(first_stats["conflicts"], 1)
        self.assertEqual(second_stats["conflicts"], 1)
        content = self.log_jsonl.read_text(encoding="utf-8")
        self.assertEqual(content.count('"event": "correction_conflict"'), 1)

    def test_repeating_problem_suppression_cache_is_bounded(self) -> None:
        bounded_config = replace(self.config, log_suppression_max_keys=2)
        distributor = DownstreamDistributor(bounded_config, conn=self.conn)

        distributor._log_repeating_problem_once("manual_problem", logical_stem="a", error_code="E")
        distributor._log_repeating_problem_once("manual_problem", logical_stem="b", error_code="E")
        distributor._log_repeating_problem_once("manual_problem", logical_stem="a", error_code="E")
        distributor._log_repeating_problem_once("manual_problem", logical_stem="c", error_code="E")
        distributor._log_repeating_problem_once("manual_problem", logical_stem="a", error_code="E")
        distributor.close()

        content = self.log_jsonl.read_text(encoding="utf-8")
        self.assertEqual(content.count('"event": "manual_problem"'), 4)
        self.assertLessEqual(len(distributor._logged_repeating_problem_events), 2)

    def test_routine_scan_events_skip_stdout_and_jsonl_by_default(self) -> None:
        distributor = self._make_distributor()
        with mock.patch("lecture_stt.downstream.lib.logger") as mocked_logger:
            distributor._log_event("scan_started", dry_run=False)
        distributor.close()

        mocked_logger.info.assert_not_called()
        self.assertFalse(self.log_jsonl.exists())

    def test_scan_completed_jsonl_logs_initial_changed_and_heartbeat_only(self) -> None:
        quiet_config = replace(self.config, stats_heartbeat_scans=2)
        distributor = DownstreamDistributor(quiet_config, conn=self.conn)
        base_stats = {
            "correction_delivered": 0,
            "summary_delivered": 0,
            "blocked": 0,
            "incomplete": 0,
            "conflicts": 0,
            "errors": 4,
        }
        changed_stats = {**base_stats, "errors": 5}

        distributor._log_event("scan_completed", dry_run=False, stats=base_stats)
        distributor._log_event("scan_completed", dry_run=False, stats=base_stats)
        distributor._log_event("scan_completed", dry_run=False, stats=base_stats)
        distributor._log_event("scan_completed", dry_run=False, stats=changed_stats)
        distributor.close()

        records = [
            json.loads(line)
            for line in self.log_jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual([record["event"] for record in records], ["scan_completed"] * 3)
        self.assertEqual(records[0]["stats"], base_stats)
        self.assertEqual(records[1]["stats"], base_stats)
        self.assertEqual(records[1]["suppressed_scan_count"], 2)
        self.assertEqual(records[2]["stats"], changed_stats)

    def test_routine_scan_events_can_be_logged_to_stdout(self) -> None:
        noisy_config = replace(self.config, log_routine_scan_events=True)
        distributor = DownstreamDistributor(noisy_config, conn=self.conn)
        with mock.patch("lecture_stt.downstream.lib.logger") as mocked_logger:
            distributor._log_event("scan_started", dry_run=False)
        distributor.close()

        mocked_logger.info.assert_called_once()

    def test_jsonl_logger_rotates_when_size_guard_is_exceeded(self) -> None:
        logger = JsonlLogger(self.log_jsonl, enabled=True, max_bytes=120, backup_count=2)

        logger.write(event="first", payload="x" * 120)
        logger.write(event="second", payload="y" * 120)

        rotated_path = self.log_jsonl.with_name(f"{self.log_jsonl.name}.1")
        self.assertTrue(rotated_path.exists())
        self.assertIn('"event": "first"', rotated_path.read_text(encoding="utf-8"))
        self.assertIn('"event": "second"', self.log_jsonl.read_text(encoding="utf-8"))

    def test_jsonl_logger_backup_count_zero_uses_unique_rotated_paths(self) -> None:
        logger = JsonlLogger(self.log_jsonl, enabled=True, max_bytes=1, backup_count=0)

        logger.write(event="first", payload="x")
        logger.write(event="second", payload="y")
        logger.write(event="third", payload="z")

        rotated_paths = sorted(self.log_jsonl.parent.glob(f"{self.log_jsonl.name}.*"))
        self.assertEqual(len(rotated_paths), 2)
        self.assertEqual(len({path.name for path in rotated_paths}), 2)
        self.assertIn('"event": "third"', self.log_jsonl.read_text(encoding="utf-8"))

    def test_summary_blocks_without_correction_history(self) -> None:
        stem = "260316Unix_1"
        self._write_text(self.summary_dir / f"{stem}.md", "# summary only")

        distributor = self._make_distributor()
        stats = distributor.scan_once()
        distributor.close()

        self.assertEqual(stats["blocked"], 1)
        self.assertTrue((self.summary_dir / f"{stem}.md").exists())
        row = db.get_delivery(self.conn, stem)
        self.assertEqual(row["summary_status"], SUMMARY_STATUS_BLOCKED)

    def test_summary_unblocks_after_legacy_correction_backfill(self) -> None:
        stem = "260316LA_2"
        self._write_text(self.summary_dir / f"{stem}.md", "# summary only")

        distributor = self._make_distributor()
        first_stats = distributor.scan_once()
        distributor.close()

        route = self._route("LA")
        self._write_text(route.gh_origin_dir(self.gh_root) / f"{stem}.txt", "legacy correction")
        self._write_json(route.gh_origin_dir(self.gh_root) / f"{stem}.json", '{"segments":[{"text":"legacy correction"}]}')

        distributor = self._make_distributor()
        second_stats = distributor.scan_once()
        distributor.close()

        self.assertEqual(first_stats["blocked"], 1)
        self.assertEqual(second_stats["summary_delivered"], 1)
        self.assertFalse((self.summary_dir / f"{stem}.md").exists())
        self.assertTrue((route.gh_summary_dir(self.gh_root) / f"{stem}.md").exists())
        self.assertTrue((route.obsidian_summary_dir(self.obsidian_root) / f"{stem}.md").exists())

        row = db.get_delivery(self.conn, stem)
        self.assertEqual(row["summary_status"], SUMMARY_STATUS_DELIVERED)
        self.assertEqual(int(row["tuk_summary_done"]), 1)
        self.assertEqual(int(row["obsidian_done"]), 1)

    def test_dry_run_does_not_mutate_sources_or_destinations(self) -> None:
        stem = "260316LC_2"
        txt_source = self.correction_dir / f"{stem}.txt"
        json_source = self.correction_dir / f"{stem}.json"
        md_source = self.summary_dir / f"{stem}.md"
        self._write_text(txt_source, "dry correction")
        self._write_json(json_source, '{"segments":[{"text":"dry correction"}]}')
        self._write_text(md_source, "# dry summary")

        distributor = self._make_distributor(dry_run=True)
        stats = distributor.scan_once()
        distributor.close()

        route = self._route("LC")
        self.assertEqual(stats["correction_delivered"], 1)
        self.assertEqual(stats["summary_delivered"], 1)
        self.assertTrue(txt_source.exists())
        self.assertTrue(json_source.exists())
        self.assertTrue(md_source.exists())
        self.assertFalse((route.gh_origin_dir(self.gh_root) / f"{stem}.txt").exists())
        self.assertFalse((route.gh_summary_dir(self.gh_root) / f"{stem}.md").exists())
        self.assertFalse((route.obsidian_summary_dir(self.obsidian_root) / f"{stem}.md").exists())
        self.assertFalse(self.log_jsonl.exists())

        count = self.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        self.assertEqual(count, 0)

    def test_cleanup_failure_does_not_mark_correction_delivered(self) -> None:
        stem = "260316Unix_2"
        self._write_text(self.correction_dir / f"{stem}.txt", "cleanup correction")
        self._write_json(self.correction_dir / f"{stem}.json", '{"segments":[{"text":"cleanup correction"}]}')
        self._write_text(self.summary_dir / f"{stem}.md", "# cleanup summary")

        distributor = self._make_distributor()
        with mock.patch.object(
            distributor,
            "_delete_sources",
            side_effect=PermissionError("simulated cleanup failure"),
        ):
            stats = distributor.scan_once()
        distributor.close()

        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["blocked"], 1)
        self.assertTrue((self.correction_dir / f"{stem}.txt").exists())
        self.assertTrue((self.correction_dir / f"{stem}.json").exists())
        self.assertTrue((self.summary_dir / f"{stem}.md").exists())

        row = db.get_delivery(self.conn, stem)
        self.assertEqual(row["correction_status"], CORRECTION_STATUS_ERROR)
        self.assertEqual(row["last_error_code"], "SOURCE_CLEANUP_FAILED")
        self.assertEqual(int(row["tuk_origin_done"]), 1)
        self.assertEqual(row["summary_status"], SUMMARY_STATUS_BLOCKED)


if __name__ == "__main__":
    unittest.main()
