from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.stt.controller_readiness import (  # noqa: E402
    MAX_LEDGER_BYTES,
    ControllerReadinessError,
    REPORT_SCHEMA_VERSION,
    SUMMARY_SCHEMA_VERSION,
    verify_controller_readiness,
)


class ControllerReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.ledger_path = self.root / "shadow.jsonl"
        self.base_time = datetime(2026, 7, 28, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _timestamp(self, value: datetime) -> str:
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _report(
        self,
        run_index: int,
        *,
        paths: list[str] | None = None,
        start_offset_seconds: int | None = None,
        duration_seconds: int = 1,
    ) -> dict:
        stable_paths = paths or [
            "alpha.m4a",
            "beta.m4a",
            "delta.m4a",
            "gamma.m4a",
            "한글.m4a",
        ]
        started_at = self.base_time + timedelta(
            seconds=(
                run_index * 2
                if start_offset_seconds is None
                else start_offset_seconds
            )
        )
        completed_at = started_at + timedelta(seconds=duration_seconds)
        checks = [
            {
                "plan_sha256": f"{run_index * 10 + offset + 1:064x}",
                "relative_path": name,
                "scan_index": 1,
                "status": "verified",
            }
            for offset, name in enumerate(stable_paths)
        ]
        return {
            "completed_at": self._timestamp(completed_at),
            "kill_switch_configured": True,
            "mode": "read_only",
            "ok": True,
            "plan_checks": checks,
            "polling_match": True,
            "run_id": f"{run_index + 1:032x}",
            "scan_comparisons": [
                {
                    "go_stable_relative_paths": stable_paths,
                    "match": True,
                    "python_stable_relative_paths": stable_paths,
                    "scan_index": 1,
                }
            ],
            "scan_count": 1,
            "schema_version": REPORT_SCHEMA_VERSION,
            "started_at": self._timestamp(started_at),
            "verified_count": len(checks),
        }

    def _write_reports(self, reports: list[dict]) -> bytes:
        raw = b"".join(
            (
                json.dumps(
                    report,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            for report in reports
        )
        self.ledger_path.write_bytes(raw)
        return raw

    def test_valid_twenty_run_hundred_observation_ledger(self) -> None:
        reports = [self._report(index) for index in range(20)]
        raw = self._write_reports(reports)

        summary = verify_controller_readiness(self.ledger_path)

        self.assertEqual(summary["schema_version"], SUMMARY_SCHEMA_VERSION)
        self.assertIs(summary["ready"], True)
        self.assertEqual(summary["run_count"], 20)
        self.assertEqual(summary["scan_count"], 20)
        self.assertEqual(summary["verified_count"], 100)
        self.assertEqual(summary["ledger_sha256"], hashlib.sha256(raw).hexdigest())
        encoded = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(str(self.ledger_path), encoded)
        self.assertNotIn("alpha.m4a", encoded)
        self.assertNotIn(reports[0]["run_id"], encoded)
        self.assertNotIn(reports[0]["started_at"], encoded)
        self.assertNotIn(reports[0]["plan_checks"][0]["plan_sha256"], encoded)

    def test_below_run_or_verified_threshold_is_rejected(self) -> None:
        self._write_reports([self._report(index) for index in range(19)])
        with self.assertRaisesRegex(ControllerReadinessError, "criteria"):
            verify_controller_readiness(self.ledger_path)

        reports = [
            self._report(index, paths=["alpha.m4a"])
            for index in range(20)
        ]
        self._write_reports(reports)
        with self.assertRaisesRegex(ControllerReadinessError, "criteria"):
            verify_controller_readiness(self.ledger_path)

    def test_duplicate_or_unknown_json_keys_are_rejected(self) -> None:
        report = self._report(0)
        encoded = json.dumps(report, separators=(",", ":"))
        duplicate = encoded[:-1] + ',"mode":"read_only"}\n'
        self.ledger_path.write_text(duplicate, encoding="utf-8")
        with self.assertRaisesRegex(ControllerReadinessError, "duplicate JSON key"):
            verify_controller_readiness(
                self.ledger_path,
                min_runs=1,
                min_verified=1,
            )

        report["unexpected"] = True
        self._write_reports([report])
        with self.assertRaisesRegex(ControllerReadinessError, "keys mismatch"):
            verify_controller_readiness(
                self.ledger_path,
                min_runs=1,
                min_verified=1,
            )

    def test_failed_or_non_read_only_report_is_rejected(self) -> None:
        for field, value in (
            ("ok", False),
            ("polling_match", False),
            ("kill_switch_configured", False),
            ("mode", "execute"),
            ("schema_version", "lecture-stt/controller-shadow-report@3"),
        ):
            with self.subTest(field=field):
                report = self._report(0)
                report[field] = value
                self._write_reports([report])
                with self.assertRaises(ControllerReadinessError):
                    verify_controller_readiness(
                        self.ledger_path,
                        min_runs=1,
                        min_verified=1,
                    )

    def test_scan_mismatch_order_and_unsafe_paths_are_rejected(self) -> None:
        mutations = []
        mismatch = self._report(0)
        mismatch["scan_comparisons"][0]["match"] = False
        mutations.append(mismatch)
        unequal = self._report(0)
        unequal["scan_comparisons"][0]["python_stable_relative_paths"] = []
        mutations.append(unequal)
        wrong_index = self._report(0)
        wrong_index["scan_comparisons"][0]["scan_index"] = 2
        mutations.append(wrong_index)
        unsorted = self._report(0)
        unsorted["scan_comparisons"][0]["go_stable_relative_paths"] = [
            "beta.m4a",
            "alpha.m4a",
        ]
        unsorted["scan_comparisons"][0]["python_stable_relative_paths"] = [
            "beta.m4a",
            "alpha.m4a",
        ]
        mutations.append(unsorted)
        temporary = self._report(0, paths=["unsafe.part"])
        mutations.append(temporary)

        for report in mutations:
            with self.subTest(report=report):
                self._write_reports([report])
                with self.assertRaises(ControllerReadinessError):
                    verify_controller_readiness(
                        self.ledger_path,
                        min_runs=1,
                        min_verified=1,
                    )

    def test_plan_check_digest_status_count_and_scan_link_are_rejected(self) -> None:
        mutations = []
        bad_digest = self._report(0)
        bad_digest["plan_checks"][0]["plan_sha256"] = "A" * 64
        mutations.append(bad_digest)
        bad_status = self._report(0)
        bad_status["plan_checks"][0]["status"] = "rejected"
        mutations.append(bad_status)
        wrong_count = self._report(0)
        wrong_count["verified_count"] = 4
        mutations.append(wrong_count)
        unlinked = self._report(0)
        unlinked["plan_checks"][0]["relative_path"] = "missing.m4a"
        mutations.append(unlinked)
        duplicate = self._report(0)
        duplicate["plan_checks"].append(deepcopy(duplicate["plan_checks"][0]))
        duplicate["verified_count"] += 1
        mutations.append(duplicate)

        for report in mutations:
            with self.subTest(report=report):
                self._write_reports([report])
                with self.assertRaises(ControllerReadinessError):
                    verify_controller_readiness(
                        self.ledger_path,
                        min_runs=1,
                        min_verified=1,
                    )

    def test_duplicate_reordered_or_overlapping_run_metadata_is_rejected(self) -> None:
        valid_first = self._report(0)
        duplicate_id = self._report(1)
        duplicate_id["run_id"] = valid_first["run_id"]
        self._write_reports([valid_first, duplicate_id])
        with self.assertRaisesRegex(ControllerReadinessError, "duplicate run id"):
            verify_controller_readiness(
                self.ledger_path,
                min_runs=2,
                min_verified=1,
            )

        self._write_reports([self._report(1), self._report(0)])
        with self.assertRaisesRegex(ControllerReadinessError, "strictly increasing"):
            verify_controller_readiness(
                self.ledger_path,
                min_runs=2,
                min_verified=1,
            )

        first = self._report(0, duration_seconds=3)
        overlapping = self._report(1, start_offset_seconds=2)
        self._write_reports([first, overlapping])
        with self.assertRaisesRegex(ControllerReadinessError, "overlapping"):
            verify_controller_readiness(
                self.ledger_path,
                min_runs=2,
                min_verified=1,
            )

    def test_invalid_run_id_formats_are_rejected(self) -> None:
        for invalid_run_id in (
            "A" * 32,
            "f" * 31,
            "g" * 32,
        ):
            with self.subTest(run_id=invalid_run_id):
                report = self._report(0)
                report["run_id"] = invalid_run_id
                self._write_reports([report])
                with self.assertRaisesRegex(
                    ControllerReadinessError,
                    "run_id is invalid",
                ):
                    verify_controller_readiness(
                        self.ledger_path,
                        min_runs=1,
                        min_verified=1,
                    )

    def test_invalid_or_reversed_timestamps_are_rejected(self) -> None:
        invalid = self._report(0)
        invalid["started_at"] = "2026-07-28T00:00:00+09:00"
        self._write_reports([invalid])
        with self.assertRaisesRegex(ControllerReadinessError, "UTC RFC3339"):
            verify_controller_readiness(
                self.ledger_path,
                min_runs=1,
                min_verified=1,
            )

        nanosecond_first = self._report(0)
        nanosecond_first["started_at"] = "2026-07-28T00:00:00.000000002Z"
        nanosecond_first["completed_at"] = "2026-07-28T00:00:00.000000003Z"
        nanosecond_second = self._report(1)
        nanosecond_second["started_at"] = "2026-07-28T00:00:00.000000001Z"
        nanosecond_second["completed_at"] = "2026-07-28T00:00:00.000000004Z"
        self._write_reports([nanosecond_first, nanosecond_second])
        with self.assertRaisesRegex(ControllerReadinessError, "strictly increasing"):
            verify_controller_readiness(
                self.ledger_path,
                min_runs=2,
                min_verified=1,
            )

        invalid_separator = self._report(0)
        invalid_separator["started_at"] = "2026-07-28 00:00:00Z"
        self._write_reports([invalid_separator])
        with self.assertRaisesRegex(ControllerReadinessError, "UTC RFC3339"):
            verify_controller_readiness(
                self.ledger_path,
                min_runs=1,
                min_verified=1,
            )

        reversed_report = self._report(0)
        reversed_report["completed_at"] = self._timestamp(
            self.base_time - timedelta(seconds=1)
        )
        self._write_reports([reversed_report])
        with self.assertRaisesRegex(ControllerReadinessError, "before"):
            verify_controller_readiness(
                self.ledger_path,
                min_runs=1,
                min_verified=1,
            )

    def test_symlink_hardlink_directory_and_oversized_input_are_rejected(self) -> None:
        self._write_reports([self._report(0)])
        symlink = self.root / "symlink.jsonl"
        symlink.symlink_to(self.ledger_path)
        with self.assertRaises(ControllerReadinessError):
            verify_controller_readiness(symlink, min_runs=1, min_verified=1)

        hardlink = self.root / "hardlink.jsonl"
        os.link(self.ledger_path, hardlink)
        with self.assertRaisesRegex(ControllerReadinessError, "one hard link"):
            verify_controller_readiness(
                self.ledger_path,
                min_runs=1,
                min_verified=1,
            )
        hardlink.unlink()

        with self.assertRaisesRegex(ControllerReadinessError, "regular file"):
            verify_controller_readiness(
                self.root,
                min_runs=1,
                min_verified=1,
            )

        oversized = self.root / "oversized.jsonl"
        oversized.write_bytes(b"x" * (MAX_LEDGER_BYTES + 1))
        with self.assertRaisesRegex(ControllerReadinessError, "size"):
            verify_controller_readiness(
                oversized,
                min_runs=1,
                min_verified=1,
            )

    def test_incomplete_blank_and_non_utf8_records_are_rejected(self) -> None:
        valid = json.dumps(self._report(0), separators=(",", ":")).encode()
        self.ledger_path.write_bytes(valid)
        with self.assertRaisesRegex(ControllerReadinessError, "complete JSONL"):
            verify_controller_readiness(
                self.ledger_path,
                min_runs=1,
                min_verified=1,
            )

        self.ledger_path.write_bytes(valid + b"\n\n")
        with self.assertRaisesRegex(ControllerReadinessError, "blank"):
            verify_controller_readiness(
                self.ledger_path,
                min_runs=1,
                min_verified=1,
            )

        self.ledger_path.write_bytes(b"\xff\n")
        with self.assertRaisesRegex(ControllerReadinessError, "UTF-8"):
            verify_controller_readiness(
                self.ledger_path,
                min_runs=1,
                min_verified=1,
            )

    def test_cli_success_and_rejection_are_sanitized(self) -> None:
        self._write_reports([self._report(0)])
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(SRC_ROOT)
        command = [
            sys.executable,
            "-m",
            "lecture_stt.stt.controller_readiness",
            "--input",
            str(self.ledger_path),
            "--min-runs",
            "1",
            "--min-verified",
            "1",
        ]
        success = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertIs(json.loads(success.stdout)["ready"], True)

        self.ledger_path.write_text("{}\n", encoding="utf-8")
        rejected = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(rejected.stdout, "")
        self.assertNotIn(str(self.ledger_path), rejected.stderr)


if __name__ == "__main__":
    unittest.main()
