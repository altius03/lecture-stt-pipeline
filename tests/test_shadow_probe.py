from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.stt import shadow_probe  # noqa: E402


class ShadowProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.watch_dir = self.root / "inbox"
        self.watch_dir.mkdir()
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(
            (
                "app:\n"
                "  polling_interval_sec: 3\n"
                "  stable_for_sec: 0\n"
                "paths:\n"
                "  watch_folder: ${WATCH_ROOT}\n"
            ),
            encoding="utf-8",
        )
        self.env = {"WATCH_ROOT": str(self.watch_dir)}

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run_main(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        old = dict(os.environ)
        os.environ.update(self.env)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = shadow_probe.main(args)
        finally:
            os.environ.clear()
            os.environ.update(old)
        return code, stdout.getvalue(), stderr.getvalue()

    def _run_lockstep_main(self, stdin_bytes: bytes, *args: str) -> tuple[int, bytes, str]:
        stdout = io.BytesIO()
        stderr = io.StringIO()
        old = dict(os.environ)
        os.environ.update(self.env)
        try:
            code = shadow_probe.main(
                args,
                stdin=io.BytesIO(stdin_bytes),
                stdout=stdout,
                stderr=stderr,
            )
        finally:
            os.environ.clear()
            os.environ.update(old)
        return code, stdout.getvalue(), stderr.getvalue()

    def _lockstep_request(self, scan_index: int) -> bytes:
        return (
            json.dumps(
                {
                    "schema_version": shadow_probe.LOCKSTEP_SCAN_REQUEST_SCHEMA_VERSION,
                    "scan_index": scan_index,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    def test_config_returns_normalized_shadow_values_without_full_worker_sections(self) -> None:
        code, stdout, stderr = self._run_main(
            "config",
            "--config",
            str(self.config_path),
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["schema_version"], shadow_probe.CONFIG_SCHEMA_VERSION)
        self.assertEqual(payload["watch_folder"], str(self.watch_dir))
        self.assertEqual(payload["stable_for_sec"], 0)
        self.assertEqual(payload["polling_interval_sec"], 3)

    def test_scan_emits_relative_stable_paths_only(self) -> None:
        older = self.watch_dir / "older.m4a"
        newer = self.watch_dir / "newer.m4a"
        temp = self.watch_dir / ".upload.part"
        older.write_bytes(b"older")
        newer.write_bytes(b"newer")
        temp.write_bytes(b"partial")
        older_stat = older.stat()
        newer_stat = newer.stat()
        os.utime(older, ns=(older_stat.st_atime_ns, older_stat.st_mtime_ns - 2_000_000_000))
        os.utime(newer, ns=(newer_stat.st_atime_ns, newer_stat.st_mtime_ns - 1_000_000_000))

        code, stdout, stderr = self._run_main(
            "scan",
            "--config",
            str(self.config_path),
            "--scan-count",
            "2",
            "--sleep-sec",
            "0",
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["schema_version"], shadow_probe.SCAN_SCHEMA_VERSION)
        self.assertEqual(payload["scan_count"], 2)
        self.assertEqual(payload["sleep_sec"], 0.0)
        self.assertEqual(payload["scans"][0]["stable_relative_paths"], [])
        self.assertEqual(payload["scans"][1]["stable_relative_paths"], ["older.m4a", "newer.m4a"])
        self.assertNotIn("watch_folder", payload)
        self.assertNotIn(str(self.watch_dir / "older.m4a"), stdout)
        self.assertNotIn(".upload.part", json.dumps(payload["scans"][1]["stable_relative_paths"]))

    def test_scan_rejects_missing_watch_folder(self) -> None:
        missing = self.root / "missing"
        code, stdout, stderr = self._run_main(
            "scan",
            "--config",
            str(self.config_path),
            "--watch-folder",
            str(missing),
            "--scan-count",
            "1",
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("watch_folder does not exist", stderr)

    def test_stat_scan_emits_direct_regular_non_temporary_metadata_only(self) -> None:
        older = self.watch_dir / "alpha.m4a"
        newer = self.watch_dir / "beta.wav"
        temp = self.watch_dir / ".upload.part"
        nested = self.watch_dir / "nested"
        target = self.watch_dir / "target.m4a"
        symlink = self.watch_dir / "link.m4a"
        older.write_bytes(b"older")
        newer.write_bytes(b"newer-audio")
        temp.write_bytes(b"partial")
        nested.mkdir()
        (nested / "inside.m4a").write_bytes(b"inside")
        target.write_bytes(b"target")
        symlink.symlink_to(target)

        code, stdout, stderr = self._run_main(
            "stat-scan",
            "--config",
            str(self.config_path),
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["schema_version"], shadow_probe.STAT_SCAN_SCHEMA_VERSION)
        self.assertEqual(
            [list(entry.keys()) for entry in payload["entries"]],
            [list(shadow_probe.STAT_SCAN_ENTRY_KEYS)] * 3,
        )
        self.assertEqual(
            [entry["name"] for entry in payload["entries"]],
            ["alpha.m4a", "beta.wav", "target.m4a"],
        )
        self.assertEqual(payload["entries"][0]["size_bytes"], 5)
        self.assertIsInstance(payload["entries"][0]["mtime"], float)
        self.assertNotIn(str(self.watch_dir), stdout)
        self.assertNotIn(".upload.part", stdout)
        self.assertNotIn("nested", stdout)
        self.assertNotIn("link.m4a", stdout)

    def test_stat_scan_returns_empty_entries_when_no_eligible_files_exist(self) -> None:
        (self.watch_dir / ".upload.part").write_bytes(b"partial")
        (self.watch_dir / "subdir").mkdir()

        code, stdout, stderr = self._run_main(
            "stat-scan",
            "--config",
            str(self.config_path),
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload, {"schema_version": shadow_probe.STAT_SCAN_SCHEMA_VERSION, "entries": []})

    def test_stat_scan_honors_watch_folder_override_without_absolute_path_output(self) -> None:
        override_dir = self.root / "override"
        override_dir.mkdir()
        override_file = override_dir / "override.m4a"
        override_file.write_bytes(b"override")

        code, stdout, stderr = self._run_main(
            "stat-scan",
            "--config",
            str(self.config_path),
            "--watch-folder",
            str(override_dir),
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["entries"], [{"name": "override.m4a", "size_bytes": 8, "mtime": payload["entries"][0]["mtime"]}])
        self.assertNotIn(str(override_dir), stdout)

    def test_scan_count_bounds_are_strict(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            shadow_probe.parse_args(
                [
                    "scan",
                    "--config",
                    str(self.config_path),
                    "--scan-count",
                    "0",
                ]
            )
        self.assertEqual(exc.exception.code, 2)

    def test_sleep_sec_rejects_non_finite_values(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with (
                self.subTest(value=value),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as exc,
            ):
                shadow_probe.parse_args(
                    [
                        "scan",
                        "--config",
                        str(self.config_path),
                        "--scan-count",
                        "1",
                        "--sleep-sec",
                        value,
                    ]
                )
            self.assertEqual(exc.exception.code, 2)

    def test_config_rejects_empty_watch_folder(self) -> None:
        self.config_path.write_text(
            (
                "app:\n"
                "  polling_interval_sec: 3\n"
                "  stable_for_sec: 0\n"
                "paths:\n"
                "  watch_folder: \"\"\n"
            ),
            encoding="utf-8",
        )

        code, stdout, stderr = self._run_main(
            "config",
            "--config",
            str(self.config_path),
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("must be a non-empty string", stderr)

    def test_config_rejects_non_positive_polling_interval(self) -> None:
        self.config_path.write_text(
            (
                "app:\n"
                "  polling_interval_sec: 0\n"
                "  stable_for_sec: 0\n"
                "paths:\n"
                "  watch_folder: ${WATCH_ROOT}\n"
            ),
            encoding="utf-8",
        )

        code, stdout, stderr = self._run_main(
            "config",
            "--config",
            str(self.config_path),
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("must be greater than 0", stderr)

    def test_lockstep_scan_uses_persistent_watcher_state_and_emits_one_line_per_scan(self) -> None:
        self.config_path.write_text(
            (
                "app:\n"
                "  polling_interval_sec: 3\n"
                "  stable_for_sec: 1\n"
                "paths:\n"
                "  watch_folder: ${WATCH_ROOT}\n"
            ),
            encoding="utf-8",
        )
        stable = self.watch_dir / "stable.m4a"
        stable.write_bytes(b"stable-audio")
        requests = b"".join(self._lockstep_request(index) for index in range(1, 5))

        now_values = iter((100.0, 100.5, 101.1, 101.2))
        with mock.patch("lecture_stt.stt.watcher.utils.now", side_effect=lambda: next(now_values)):
            code, stdout, stderr = self._run_lockstep_main(
                requests,
                "lockstep-scan",
                "--config",
                str(self.config_path),
                "--scan-count",
                "4",
            )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout.count(b"\n"), 4)
        decoded_lines = stdout.decode("utf-8").splitlines()
        payloads = [json.loads(line) for line in decoded_lines]
        self.assertEqual(
            [list(payload.keys()) for payload in payloads],
            [list(shadow_probe.LOCKSTEP_RESULT_KEYS)] * 4,
        )
        self.assertEqual(
            [payload["stable_relative_paths"] for payload in payloads],
            [[], [], ["stable.m4a"], []],
        )
        self.assertEqual(
            [payload["scan_index"] for payload in payloads],
            [1, 2, 3, 4],
        )
        self.assertTrue(
            all(payload["schema_version"] == shadow_probe.LOCKSTEP_SCAN_RESULT_SCHEMA_VERSION for payload in payloads)
        )
        self.assertNotIn(str(self.watch_dir), stdout.decode("utf-8"))
        self.assertNotIn("watch_folder", stdout.decode("utf-8"))
        self.assertNotIn("sha256", stdout.decode("utf-8"))

    def test_lockstep_scan_rejects_invalid_protocol_shapes_without_leaking_input(self) -> None:
        secret = "/tmp/private-shadow-input"
        cases = {
            "duplicate_key": (
                b'{"schema_version":"lecture-stt/shadow-scan-request@1","schema_version":"'
                + secret.encode("utf-8")
                + b'","scan_index":1}\n'
            ),
            "unknown_field": (
                b'{"schema_version":"lecture-stt/shadow-scan-request@1","scan_index":1,"secret":"'
                + secret.encode("utf-8")
                + b'"}\n'
            ),
            "out_of_order_index": self._lockstep_request(2),
            "invalid_utf8": b'{"schema_version":"lecture-stt/shadow-scan-request@1","scan_index":"\xff"}\n',
        }

        for label, request in cases.items():
            with self.subTest(label=label):
                code, stdout, stderr = self._run_lockstep_main(
                    request,
                    "lockstep-scan",
                    "--config",
                    str(self.config_path),
                    "--scan-count",
                    "1",
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout, b"")
                self.assertEqual(stderr.strip(), "Shadow probe rejected: invalid lockstep protocol")
                self.assertNotIn(secret, stderr)

    def test_lockstep_scan_rejects_oversized_request_line(self) -> None:
        secret = "very-secret-shadow-payload"
        oversized = (
            b'{"schema_version":"lecture-stt/shadow-scan-request@1","scan_index":1,"padding":"'
            + (secret.encode("utf-8") * 5000)
            + b'"}\n'
        )
        self.assertGreater(len(oversized), shadow_probe.LOCKSTEP_MAX_LINE_BYTES)

        code, stdout, stderr = self._run_lockstep_main(
            oversized,
            "lockstep-scan",
            "--config",
            str(self.config_path),
            "--scan-count",
            "1",
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, b"")
        self.assertEqual(stderr.strip(), "Shadow probe rejected: invalid lockstep protocol")
        self.assertNotIn(secret, stderr)

    def test_lockstep_scan_rejects_eof_without_a_complete_request_line(self) -> None:
        truncated = (
            json.dumps(
                {
                    "schema_version": shadow_probe.LOCKSTEP_SCAN_REQUEST_SCHEMA_VERSION,
                    "scan_index": 1,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )

        code, stdout, stderr = self._run_lockstep_main(
            truncated,
            "lockstep-scan",
            "--config",
            str(self.config_path),
            "--scan-count",
            "1",
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, b"")
        self.assertEqual(stderr.strip(), "Shadow probe rejected: invalid lockstep protocol")


if __name__ == "__main__":
    unittest.main()
