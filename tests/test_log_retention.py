from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.shared.log_retention import (  # noqa: E402
    DEFAULT_ARCHIVE_RETENTION_DAYS,
    DEFAULT_LAUNCHD_BACKUP_COUNT,
    DEFAULT_LOG_MAX_BYTES,
    LogRotationPlan,
    prune_compressed_archives,
    rotate_plain_log_if_needed,
)


class LogRetentionTests(unittest.TestCase):
    def test_rotate_plain_log_if_needed_keeps_three_launchd_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "launchd.out.log"
            log_path.write_text("first-generation", encoding="utf-8")

            first_plan = rotate_plain_log_if_needed(
                log_path,
                max_bytes=1,
                backup_count=DEFAULT_LAUNCHD_BACKUP_COUNT,
                dry_run=False,
            )
            log_path.write_text("second-generation", encoding="utf-8")
            second_plan = rotate_plain_log_if_needed(log_path, max_bytes=1, backup_count=3, dry_run=False)
            log_path.write_text("third-generation", encoding="utf-8")
            third_plan = rotate_plain_log_if_needed(log_path, max_bytes=1, backup_count=3, dry_run=False)
            log_path.write_text("fourth-generation", encoding="utf-8")
            fourth_plan = rotate_plain_log_if_needed(log_path, max_bytes=1, backup_count=3, dry_run=False)

            self.assertIsInstance(first_plan, LogRotationPlan)
            self.assertTrue(second_plan.rotated)
            self.assertTrue(third_plan.rotated)
            self.assertTrue(fourth_plan.rotated)
            self.assertEqual((Path(tmp) / "launchd.out.log.1").read_text(encoding="utf-8"), "fourth-generation")
            self.assertEqual((Path(tmp) / "launchd.out.log.2").read_text(encoding="utf-8"), "third-generation")
            self.assertEqual((Path(tmp) / "launchd.out.log.3").read_text(encoding="utf-8"), "second-generation")
            self.assertFalse((Path(tmp) / "launchd.out.log.4").exists())
            self.assertEqual(log_path.read_text(encoding="utf-8"), "")

    def test_rotate_plain_log_dry_run_reports_without_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "app.out.log"
            log_path.write_text("x" * (DEFAULT_LOG_MAX_BYTES + 1), encoding="utf-8")

            plan = rotate_plain_log_if_needed(log_path, max_bytes=DEFAULT_LOG_MAX_BYTES, backup_count=3, dry_run=True)

            self.assertTrue(plan.should_rotate)
            self.assertFalse(plan.rotated)
            self.assertEqual(log_path.read_text(encoding="utf-8"), "x" * (DEFAULT_LOG_MAX_BYTES + 1))
            self.assertFalse((Path(tmp) / "app.out.log.1").exists())

    def test_rotate_plain_log_uses_copytruncate_for_active_launchd_fd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "launchd.err.log"
            with log_path.open("ab", buffering=0) as active_fd:
                active_fd.write(b"before-rotation")

                plan = rotate_plain_log_if_needed(log_path, max_bytes=1, backup_count=3, dry_run=False)
                active_fd.write(b"after-rotation")

            self.assertTrue(plan.rotated)
            self.assertEqual((Path(tmp) / "launchd.err.log.1").read_bytes(), b"before-rotation")
            self.assertEqual(log_path.read_bytes(), b"after-rotation")

    def test_prune_compressed_archives_honors_30_day_policy_and_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_archive = root / "old.log.gz"
            recent_archive = root / "recent.log.gz"
            plain_log = root / "plain.log"
            old_archive.write_text("old", encoding="utf-8")
            recent_archive.write_text("recent", encoding="utf-8")
            plain_log.write_text("plain", encoding="utf-8")

            now = time.time()
            os.utime(old_archive, (now - (DEFAULT_ARCHIVE_RETENTION_DAYS + 1) * 86400, now - (DEFAULT_ARCHIVE_RETENTION_DAYS + 1) * 86400))
            os.utime(recent_archive, (now, now))

            planned = prune_compressed_archives(
                root,
                max_age_days=DEFAULT_ARCHIVE_RETENTION_DAYS,
                dry_run=True,
                now=now,
            )
            self.assertEqual(planned, [old_archive])
            self.assertTrue(old_archive.exists())

            deleted = prune_compressed_archives(
                root,
                max_age_days=DEFAULT_ARCHIVE_RETENTION_DAYS,
                dry_run=False,
                now=now,
            )
            self.assertEqual(deleted, [old_archive])
            self.assertFalse(old_archive.exists())
            self.assertTrue(recent_archive.exists())
            self.assertTrue(plain_log.exists())


if __name__ == "__main__":
    unittest.main()
