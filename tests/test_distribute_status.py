from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import db  # noqa: E402
import distribute_status  # noqa: E402


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

    def test_summary_shows_aggregate_counts_and_problem_rows(self) -> None:
        code, stdout, _ = self._run("summary", "--limit", "5")
        self.assertEqual(code, 0)
        self.assertIn("Deliveries summary", stdout)
        self.assertIn("total_rows: 3", stdout)
        self.assertIn("problem_rows: 2", stdout)
        self.assertIn("- DELIVERED: 2", stdout)
        self.assertIn("- BLOCKED: 1", stdout)
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


if __name__ == "__main__":
    unittest.main()
