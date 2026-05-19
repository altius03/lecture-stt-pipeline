from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.downstream import status as distribute_status  # noqa: E402
from lecture_stt.shared import db  # noqa: E402


class DistributeStatusCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.db_path = self.root / "jobs.sqlite3"
        self.conn = db.init_db(str(self.db_path))
        db.upsert_delivery(
            self.conn,
            "260316LC_1",
            subject_abbr="LC",
            correction_status="DELIVERED",
            summary_status="DELIVERED",
            tuk_origin_done=1,
            tuk_summary_done=1,
            obsidian_done=1,
            last_error_code=None,
            updated_at="2026-03-16T18:00:00+09:00",
        )
        db.upsert_delivery(
            self.conn,
            "260316DS_2",
            subject_abbr="DS",
            correction_status="INCOMPLETE",
            summary_status="BLOCKED",
            tuk_origin_done=0,
            tuk_summary_done=0,
            obsidian_done=0,
            last_error_code="INCOMPLETE_CORRECTION_PAIR",
            updated_at="2026-03-16T18:01:00+09:00",
        )
        db.upsert_delivery(
            self.conn,
            "260316DStr_3",
            subject_abbr="DStr",
            correction_status="DELIVERED",
            summary_status="CONFLICT",
            tuk_origin_done=1,
            tuk_summary_done=1,
            obsidian_done=0,
            last_error_code="CONFLICT",
            updated_at="2026-03-16T18:02:00+09:00",
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.tmpdir.cleanup()

    def _run(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = distribute_status.main(["--db-path", str(self.db_path), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def _write_text(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_resolve_db_path_override_skips_worker_config_import(self) -> None:
        args = distribute_status.parse_args(["--db-path", str(self.db_path), "summary"])
        with mock.patch.object(
            distribute_status,
            "_load_worker_config_from_module",
            side_effect=AssertionError("worker config import should be skipped"),
        ):
            resolved = distribute_status.resolve_db_path(args)
        self.assertEqual(resolved, self.db_path)

    def test_summary_shows_aggregate_counts_and_problem_rows(self) -> None:
        code, stdout, _ = self._run("summary", "--limit", "5")
        self.assertEqual(code, 0)
        self.assertIn("Deliveries summary", stdout)
        self.assertIn("total_rows: 3", stdout)
        self.assertIn("problem_rows: 2", stdout)
        self.assertIn("- DELIVERED: 2", stdout)
        self.assertIn("- BLOCKED: 1", stdout)
        self.assertIn("Problem reason counts", stdout)
        self.assertIn("- CONFLICT: 1", stdout)
        self.assertIn("- INCOMPLETE_CORRECTION_PAIR: 1", stdout)
        self.assertIn("260316DStr_3", stdout)
        self.assertIn("260316DS_2", stdout)

    def test_list_only_problems_filters_rows(self) -> None:
        code, stdout, _ = self._run("list", "--only-problems", "--limit", "10")
        self.assertEqual(code, 0)
        self.assertIn("260316DStr_3", stdout)
        self.assertIn("260316DS_2", stdout)
        self.assertNotIn("260316LC_1", stdout)

    def test_show_prints_one_row_as_json(self) -> None:
        code, stdout, _ = self._run("show", "260316LC_1")
        self.assertEqual(code, 0)
        self.assertIn('"logical_stem": "260316LC_1"', stdout)
        self.assertIn('"summary_status": "DELIVERED"', stdout)

    def test_show_returns_error_for_missing_stem(self) -> None:
        code, _, stderr = self._run("show", "missing")
        self.assertEqual(code, 1)
        self.assertIn("Delivery not found: missing", stderr)

    def test_clear_requires_yes(self) -> None:
        code, _, stderr = self._run("clear", "260316LC_1")
        self.assertEqual(code, 2)
        self.assertIn("Refusing to delete without --yes", stderr)

        row = db.get_delivery(self.conn, "260316LC_1")
        self.assertIsNotNone(row)

    def test_clear_dry_run_keeps_row(self) -> None:
        code, stdout, _ = self._run("clear", "260316LC_1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Would delete delivery row: 260316LC_1", stdout)

        row = db.get_delivery(self.conn, "260316LC_1")
        self.assertIsNotNone(row)

    def test_clear_deletes_row(self) -> None:
        code, stdout, _ = self._run("clear", "260316LC_1", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Deleted delivery row: 260316LC_1", stdout)

        row = db.get_delivery(self.conn, "260316LC_1")
        self.assertIsNone(row)

    def test_diagnose_json_classifies_current_hash_conflict_without_mutating(self) -> None:
        source_txt = self._write_text(self.root / "03_correction" / "260316LC_9.txt", "source correction")
        source_json = self._write_text(self.root / "03_correction" / "260316LC_9.json", '{"text":"source"}')
        dest_txt = self._write_text(self.root / "GH" / "260316LC_9.txt", "destination correction")
        dest_json = self._write_text(self.root / "GH" / "260316LC_9.json", '{"text":"destination"}')
        db.upsert_delivery(
            self.conn,
            "260316LC_9",
            subject_abbr="LC",
            correction_status="CONFLICT",
            summary_status="BLOCKED",
            correction_txt_path=str(source_txt),
            correction_json_path=str(source_json),
            tuk_origin_txt_path=str(dest_txt),
            tuk_origin_json_path=str(dest_json),
            last_error_code="CONFLICT",
        )
        before_count = self.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]

        code, stdout, _ = self._run("diagnose", "--json", "--limit", "20")

        after_count = self.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        self.assertEqual(code, 0)
        self.assertEqual(after_count, before_count)
        payload = json.loads(stdout)
        row = next(item for item in payload["rows"] if item["logical_stem"] == "260316LC_9")
        self.assertEqual(row["classification"], "hash-conflict")
        self.assertIn("correction_txt", row["different_destinations"])
        self.assertIn("manual", row["recommended_action"])

    def test_diagnose_json_routes_invalid_and_unknown_subject_rows_to_manual_table(self) -> None:
        db.upsert_delivery(
            self.conn,
            "260407_LA",
            subject_abbr="UNKNOWN",
            correction_status="ERROR",
            summary_status="MISSING",
            last_error_code="INVALID_STEM",
        )
        db.upsert_delivery(
            self.conn,
            "260323DS_1__20260412_020950__3b5301",
            subject_abbr="DS",
            correction_status="ERROR",
            summary_status="MISSING",
            last_error_code="UNKNOWN_SUBJECT",
        )

        code, stdout, _ = self._run("diagnose", "--json", "--limit", "20")

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        invalid = next(item for item in payload["rows"] if item["logical_stem"] == "260407_LA")
        unknown = next(
            item for item in payload["rows"] if item["logical_stem"] == "260323DS_1__20260412_020950__3b5301"
        )
        self.assertEqual(invalid["classification"], "route/rename-needed")
        self.assertEqual(unknown["classification"], "route/rename-needed")
        self.assertEqual(unknown["proposed_stem"], "260323DS_1")
        self.assertIn("manual table", unknown["recommended_action"])

    def test_diagnose_json_protects_260422lc_source_missing_row(self) -> None:
        db.upsert_delivery(
            self.conn,
            "260422LC",
            subject_abbr="LC",
            correction_status="CONFLICT",
            summary_status="BLOCKED",
            last_error_code="CONFLICT",
        )

        code, stdout, _ = self._run("diagnose", "--json", "--limit", "20")

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        row = next(item for item in payload["rows"] if item["logical_stem"] == "260422LC")
        self.assertEqual(row["classification"], "source-missing")
        self.assertIn("document only", row["recommended_action"])

    def test_diagnose_report_path_writes_machine_readable_report_without_mutating(self) -> None:
        report_path = self.root / "state" / "reports" / "downstream-diagnose.json"
        before_count = self.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]

        code, stdout, _ = self._run("diagnose", "--json", "--limit", "20", "--report-path", str(report_path))

        after_count = self.conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        self.assertEqual(code, 0)
        self.assertEqual(after_count, before_count)
        stdout_payload = json.loads(stdout)
        file_payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(stdout_payload["dry_run"])
        self.assertEqual(file_payload["row_count"], stdout_payload["row_count"])
        self.assertIn("rows", file_payload)

    def test_clear_stale_dry_run_keeps_row_and_reports_backup_plan(self) -> None:
        db.upsert_delivery(
            self.conn,
            "260501LC",
            subject_abbr="LC",
            correction_status="CONFLICT",
            summary_status="BLOCKED",
            last_error_code="CONFLICT",
        )
        backup_path = self.root / "backups" / "jobs.before-clear.sqlite3"

        code, stdout, _ = self._run("clear-stale", "260501LC", "--dry-run", "--backup-path", str(backup_path))

        self.assertEqual(code, 0)
        self.assertIn("Would clear stale delivery row: 260501LC", stdout)
        self.assertIn("backup_path:", stdout)
        self.assertFalse(backup_path.exists())
        self.assertIsNotNone(db.get_delivery(self.conn, "260501LC"))

    def test_clear_stale_requires_yes_and_backup_path(self) -> None:
        db.upsert_delivery(
            self.conn,
            "260501LC",
            subject_abbr="LC",
            correction_status="CONFLICT",
            summary_status="BLOCKED",
            last_error_code="CONFLICT",
        )

        code, _, stderr = self._run("clear-stale", "260501LC", "--yes")

        self.assertEqual(code, 2)
        self.assertIn("--backup-path", stderr)
        self.assertIsNotNone(db.get_delivery(self.conn, "260501LC"))

    def test_clear_stale_with_yes_creates_backup_and_deletes_source_missing_row(self) -> None:
        db.upsert_delivery(
            self.conn,
            "260501LC",
            subject_abbr="LC",
            correction_status="CONFLICT",
            summary_status="BLOCKED",
            last_error_code="CONFLICT",
        )
        backup_path = self.root / "backups" / "jobs.before-clear.sqlite3"

        code, stdout, _ = self._run("clear-stale", "260501LC", "--yes", "--backup-path", str(backup_path))

        self.assertEqual(code, 0)
        self.assertIn("Cleared stale delivery row: 260501LC", stdout)
        self.assertTrue(backup_path.is_file())
        self.assertIsNone(db.get_delivery(self.conn, "260501LC"))
        with sqlite3.connect(backup_path) as backup_conn:
            row_count = backup_conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE logical_stem = ?",
                ("260501LC",),
            ).fetchone()[0]
        self.assertEqual(row_count, 1)

    def test_clear_stale_refuses_260422lc_even_with_yes(self) -> None:
        db.upsert_delivery(
            self.conn,
            "260422LC",
            subject_abbr="LC",
            correction_status="CONFLICT",
            summary_status="BLOCKED",
            last_error_code="CONFLICT",
        )
        backup_path = self.root / "backups" / "jobs.before-clear.sqlite3"

        code, _, stderr = self._run("clear-stale", "260422LC", "--yes", "--backup-path", str(backup_path))

        self.assertEqual(code, 2)
        self.assertIn("260422LC", stderr)
        self.assertIn("document-only", stderr)
        self.assertFalse(backup_path.exists())
        self.assertIsNotNone(db.get_delivery(self.conn, "260422LC"))


if __name__ == "__main__":
    unittest.main()
