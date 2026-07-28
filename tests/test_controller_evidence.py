from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.stt import controller_evidence as ce  # noqa: E402


class ControllerEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.python_binary = Path(sys.executable)
        self._workspace_counter = 0

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _new_workspace(self) -> Path:
        self._workspace_counter += 1
        workspace = self.root / f"workspace-{self._workspace_counter:03d}"
        workspace.mkdir(parents=True)
        return workspace

    def _timestamp(self, value: datetime) -> str:
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _base_workspace(self) -> dict[str, Path]:
        workspace = self._new_workspace()
        journal_root = workspace / "journal"
        repo_root = workspace / "repo"
        config = workspace / "config.json"
        kill_switch = workspace / "kill-switch"
        journal_root.mkdir()
        journal_root.chmod(0o700)
        repo_root.mkdir()
        config.write_text("{}", encoding="utf-8")
        return {
            "journal_root": journal_root,
            "repo_root": repo_root,
            "config": config,
            "kill_switch": kill_switch,
        }

    def _run_observation(
        self,
        workspace: dict[str, Path],
        controller: Path,
        *,
        expected_count: int = 1,
        timeout_sec: int = 3,
        enable_observation: bool = True,
        allow_write: bool = True,
    ) -> dict[str, object]:
        return ce.run_controller_observation(
            journal_root=workspace["journal_root"],
            controller_binary=controller,
            python_binary=self.python_binary,
            repo_root=workspace["repo_root"],
            config=workspace["config"],
            kill_switch=workspace["kill_switch"],
            timeout_sec=timeout_sec,
            enable_observation=enable_observation,
            allow_write=allow_write,
            expected_count=expected_count,
        )

    def _write_controller_script(
        self,
        workspace: dict[str, Path],
        report: dict[str, object],
        *,
        touch_kill_switch: bool = False,
    ) -> Path:
        path = workspace["repo_root"] / "controller"
        payload = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        path.write_text(
            f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

def _arg(name: str) -> str | None:
    args = sys.argv[1:]
    for index, value in enumerate(args):
        if value == name and index + 1 < len(args):
            return args[index + 1]
    return None

kill_switch = _arg("--kill-switch")
if {touch_kill_switch!r} and kill_switch:
    Path(kill_switch).write_text("", encoding="utf-8")

payload = {payload!r}
print(payload)
""",
            encoding="utf-8",
        )
        path.chmod(0o700)
        return path

    def _write_interrupted_controller_script(
        self,
        workspace: dict[str, Path],
        *,
        pid_log: Path,
    ) -> Path:
        path = workspace["repo_root"] / "controller-interrupt"
        path.write_text(
            f"""#!/usr/bin/env python3
import os
import signal
import subprocess
import sys

signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen(
    [sys.executable, "-c", "import time,signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"]
)
with open({str(pid_log)!r}, "w", encoding="utf-8") as handle:
    handle.write("%d,%d" % (os.getpid(), child.pid))

import time
time.sleep(120)
""",
            encoding="utf-8",
        )
        path.chmod(0o700)
        return path

    def _write_trapped_success_controller_script(
        self,
        workspace: dict[str, Path],
        report: dict[str, object],
        *,
        pid_log: Path,
    ) -> Path:
        path = workspace["repo_root"] / "controller-success-on-signal"
        payload = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        path.write_text(
            f"""#!/usr/bin/env python3
import json
import os
import signal
import sys
import time

state = {{}}

def _arg(name: str) -> str | None:
    args = sys.argv[1:]
    for index, value in enumerate(args):
        if value == name and index + 1 < len(args):
            return args[index + 1]
    return None

def _on_signal(signum: int, _frame: object) -> None:
    state["tripped"] = True

signal.signal(signal.SIGTERM, _on_signal)
payload = {payload!r}
with open({str(pid_log)!r}, "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))

while not state.get("tripped"):
    time.sleep(0.05)

print(payload, flush=True)
time.sleep(120)
""",
            encoding="utf-8",
        )
        path.chmod(0o700)
        return path

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _open_journal_root_fd(journal_root: Path) -> int:
        return os.open(
            journal_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )

    def _success_report(
        self,
        *,
        run_id: str,
        started_at: datetime,
        duration_seconds: int = 1,
    ) -> dict[str, object]:
        completed_at = started_at + timedelta(seconds=duration_seconds)
        paths = ["alpha.m4a", "beta.m4a"]
        return {
            "completed_at": self._timestamp(completed_at),
            "kill_switch_configured": True,
            "mode": "read_only",
            "ok": True,
            "plan_checks": [
                {
                    "plan_sha256": f"{len(paths):064x}",
                    "relative_path": paths[0],
                    "scan_index": 1,
                    "status": "verified",
                },
                {
                    "plan_sha256": f"{len(paths) + 1:064x}",
                    "relative_path": paths[1],
                    "scan_index": 2,
                    "status": "verified",
                },
            ],
            "polling_match": True,
            "run_id": run_id,
            "scan_comparisons": [
                {
                    "go_stable_relative_paths": paths,
                    "match": True,
                    "python_stable_relative_paths": paths,
                    "scan_index": 1,
                },
                {
                    "go_stable_relative_paths": paths,
                    "match": True,
                    "python_stable_relative_paths": paths,
                    "scan_index": 2,
                },
            ],
            "scan_count": 2,
            "schema_version": ce.REPORT_SCHEMA_VERSION,
            "started_at": self._timestamp(started_at),
            "verified_count": 2,
        }

    def _failure_report(self, *, run_id: str, error_kind: str, started_at: datetime) -> dict[str, object]:
        completed_at = started_at + timedelta(seconds=1)
        paths = ["alpha.m4a"]
        return {
            "completed_at": self._timestamp(completed_at),
            "kill_switch_configured": True,
            "mode": "read_only",
            "ok": False,
            "plan_checks": [
                {
                    "error_kind": "python_plan_rejected",
                    "relative_path": paths[0],
                    "scan_index": 1,
                    "status": "rejected",
                }
            ],
            "polling_match": True,
            "run_id": run_id,
            "scan_comparisons": [
                {
                    "go_stable_relative_paths": paths,
                    "match": False,
                    "python_stable_relative_paths": paths,
                    "scan_index": 1,
                }
            ],
            "scan_count": 1,
            "schema_version": ce.REPORT_SCHEMA_VERSION,
            "started_at": self._timestamp(started_at),
            "verified_count": 0,
            "error_kind": error_kind,
        }

    def _write_records_for_incomplete_suffix(self, workspace: dict[str, Path]) -> None:
        state = ce._load_journal(
            os.open(
                workspace["journal_root"],
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        )
        start = ce._record_payload(
            {
                "schema_version": ce.START_SCHEMA_VERSION,
                "sequence": len(state.starts) + 1,
                "attempt_id": "4" * 32,
                "started_at": self._timestamp(datetime.now(timezone.utc)),
                "previous_record_sha256": state.head_sha256,
                "binding_sha256": ce._requested_binding_sha256(
                    controller_binary=workspace["repo_root"] / "controller-not-used",
                    python_binary=self.python_binary,
                    repo_root=workspace["repo_root"],
                    config=workspace["config"],
                    kill_switch=workspace["kill_switch"],
                    timeout_sec=3,
                ),
            }
        )
        path = workspace["journal_root"] / f"{len(state.starts) + 1:08d}.start.json"
        path.write_bytes(ce._json_bytes(start))
        path.chmod(0o400)

    def _serialized_report(self, report: dict[str, object]) -> bytes:
        return (json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    def test_write_guards_require_observation_and_allow_write_and_expected_count_one(self) -> None:
        workspace = self._base_workspace()
        controller = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="0" * 32,
                started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        )
        with self.assertRaises(ce.ControllerEvidenceWriteDisabledError):
            self._run_observation(workspace, controller, enable_observation=False)
        with self.assertRaises(ce.ControllerEvidenceWriteDisabledError):
            self._run_observation(workspace, controller, allow_write=False)
        with self.assertRaisesRegex(ce.ControllerEvidenceError, "requires --expected-count 1"):
            self._run_observation(workspace, controller, expected_count=2)

    def test_successful_observation_writes_immutable_records_and_verifies(self) -> None:
        workspace = self._base_workspace()
        controller = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="1" * 32,
                started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        )
        result = self._run_observation(workspace, controller)

        self.assertEqual(result["outcome"], "success")
        start_record = workspace["journal_root"] / "00000001.start.json"
        finish_record = workspace["journal_root"] / "00000001.finish.json"
        lock_file = workspace["journal_root"] / "journal.lock"
        self.assertEqual(start_record.stat().st_mode & 0o777, 0o400)
        self.assertEqual(finish_record.stat().st_mode & 0o777, 0o400)
        self.assertEqual(lock_file.stat().st_mode & 0o777, 0o600)

        summary = ce.verify_controller_evidence(
            workspace["journal_root"],
            min_runs=1,
            min_verified=2,
        )
        self.assertEqual(summary["consecutive_success_count"], 1)
        self.assertEqual(summary["incomplete_count"], 0)

    def test_observation_outcomes_cover_success_and_errors(self) -> None:
        workspace = self._base_workspace()
        controller = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="a" * 32,
                started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        )
        base_time = datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc)
        reports = {
            "success": self._success_report(run_id="b" * 32, started_at=base_time),
            "failed": self._failure_report(
                run_id="c" * 32,
                error_kind="shadow_failed",
                started_at=base_time + timedelta(seconds=3),
            ),
            "missing_report": None,
            "invalid_report": b"not-json\n",
            "spawn_failed": None,
            "timeout": None,
            "interrupted": self._failure_report(
                run_id="d" * 32,
                error_kind="interrupted",
                started_at=base_time + timedelta(seconds=6),
            ),
        }
        with patch(
            "lecture_stt.stt.controller_evidence._run_controller",
            side_effect=[
                (0, self._serialized_report(reports["success"]), "completed"),
                (1, self._serialized_report(reports["failed"]), "completed"),
                (0, b"", "completed"),
                (0, reports["invalid_report"], "completed"),
                (None, b"", "spawn_failed"),
                (124, b"", "timeout"),
                (1, self._serialized_report(reports["interrupted"]), "interrupted"),
            ],
        ):
            outcomes = ["success", "failed", "missing_report", "invalid_report", "spawn_failed", "timeout", "interrupted"]
            for expected_outcome in outcomes:
                with self.subTest(expected_outcome=expected_outcome):
                    result = self._run_observation(workspace, controller)
                    self.assertEqual(result["outcome"], expected_outcome)

    def test_input_drift_detects_kill_switch_transition(self) -> None:
        workspace = self._base_workspace()
        before = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="1" * 32,
                started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        )
        first = self._run_observation(workspace, before)
        self.assertEqual(first["outcome"], "success")

        controller = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="2" * 32,
                started_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            ),
            touch_kill_switch=True,
        )
        second = self._run_observation(workspace, controller)
        self.assertEqual(second["outcome"], "input_changed")

    def test_verify_detects_hash_mismatch_after_record_tamper(self) -> None:
        workspace = self._base_workspace()
        controller = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="a" * 32,
                started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        )
        result = self._run_observation(workspace, controller)
        self.assertEqual(result["outcome"], "success")

        start_record = workspace["journal_root"] / "00000001.start.json"
        start_data = json.loads(start_record.read_text(encoding="utf-8"))
        start_data["previous_record_sha256"] = "1" * 64
        start_data = ce._record_payload(start_data)
        start_record.chmod(0o600)
        try:
            start_record.write_bytes(ce._json_bytes(start_data))
        finally:
            start_record.chmod(0o400)

        with self.assertRaisesRegex(
            ce.ControllerEvidenceError, "start record SHA-256 does not match"
        ):
            ce.verify_controller_evidence(workspace["journal_root"])

    def test_rejects_symlink_hardlink_and_unexpected_journal_file(self) -> None:
        workspace = self._base_workspace()
        controller = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="1" * 32,
                started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        )
        hardlink = workspace["journal_root"] / "hardlink-controller"
        os.link(controller, hardlink)
        workspace["config"].write_text("{}", encoding="utf-8")
        symlink = workspace["journal_root"] / "symlink-controller"
        symlink.symlink_to(controller)

        with self.assertRaises(ce.ControllerEvidenceError):
            self._run_observation(workspace, symlink)
        with self.assertRaises(ce.ControllerEvidenceError):
            self._run_observation(workspace, hardlink)
        (workspace["journal_root"] / "notes.txt").write_text("unexpected", encoding="utf-8")
        with self.assertRaises(ce.ControllerEvidenceError):
            self._run_observation(workspace, controller)

    def test_lock_mode_handling(self) -> None:
        workspace = self._base_workspace()
        workspace["journal_root"].joinpath("journal.lock").write_text("pinned-lock", encoding="utf-8")
        os.chmod(workspace["journal_root"] / "journal.lock", 0o644)
        controller = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="1" * 32,
                started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        )
        with self.assertRaises(ce.ControllerEvidenceError):
            self._run_observation(workspace, controller)

    def test_run_controller_cleans_unresponsive_process_groups_on_signal(self) -> None:
        workspace = self._base_workspace()
        pid_log = workspace["repo_root"] / "signal-pids.txt"
        controller = self._write_interrupted_controller_script(
            workspace,
            pid_log=pid_log,
        )
        stop = threading.Event()

        def send_sigterm() -> None:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if pid_log.exists():
                    break
                time.sleep(0.05)
            if pid_log.exists():
                os.kill(os.getpid(), signal.SIGTERM)

        sender = threading.Thread(target=send_sigterm, daemon=True)
        start = time.perf_counter()
        sender.start()
        try:
            exit_code, raw_report, runner_status = ce._run_controller(
                [str(controller)],
                timeout_sec=30,
            )
        finally:
            stop.set()
            sender.join(timeout=1.0)
        elapsed = time.perf_counter() - start

        self.assertEqual(runner_status, "interrupted")
        self.assertIsNotNone(exit_code)
        self.assertLess(elapsed, 6.0)
        self.assertEqual(raw_report, b"")
        for _ in range(50):
            if pid_log.exists():
                break
            time.sleep(0.05)
        self.assertTrue(pid_log.exists())
        values = [int(item) for item in pid_log.read_text(encoding="utf-8").split(",") if item]
        parent_pid, descendant_pid = values
        self.assertFalse(self._is_pid_alive(parent_pid))
        self.assertFalse(self._is_pid_alive(descendant_pid))

    def test_run_controller_observation_marks_interrupted_even_with_valid_report_on_sigterm(self) -> None:
        workspace = self._base_workspace()
        pid_log = workspace["repo_root"] / "trap-success.pid"
        controller = self._write_trapped_success_controller_script(
            workspace,
            self._success_report(
                run_id="2" * 32,
                started_at=datetime(2026, 7, 28, 0, 5, tzinfo=timezone.utc),
            ),
            pid_log=pid_log,
        )
        stop = threading.Event()

        def send_sigterm() -> None:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if pid_log.exists():
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                time.sleep(0.05)

        sender = threading.Thread(target=send_sigterm, daemon=True)
        sender.start()
        try:
            result = self._run_observation(workspace, controller)
        finally:
            stop.set()
            sender.join(timeout=1.0)
        self.assertEqual(result["outcome"], "interrupted")

        root_fd = self._open_journal_root_fd(workspace["journal_root"])
        try:
            start_record, _ = ce._read_record(root_fd, "00000001.start.json")
            finish_record, _ = ce._read_record(root_fd, "00000001.finish.json")
        finally:
            os.close(root_fd)
        self.assertEqual(start_record["sequence"], 1)
        self.assertEqual(finish_record["sequence"], 1)
        self.assertEqual(finish_record["outcome"], "interrupted")
        self.assertEqual(finish_record["runner_status"], "interrupted")
        self.assertIsNotNone(finish_record["raw_report_bytes"])
        self.assertGreater(finish_record["raw_report_bytes"], 0)
        self.assertIsNone(finish_record["report"])

        with self.assertRaisesRegex(
            ce.ControllerEvidenceError, "does not satisfy the consecutive readiness criteria"
        ):
            ce.verify_controller_evidence(
                workspace["journal_root"],
                min_runs=1,
                min_verified=2,
            )

    def test_observation_is_rejected_when_journal_is_busy(self) -> None:
        workspace = self._base_workspace()
        lock_script = workspace["repo_root"] / "hold_journal_lock.py"
        lock_script.write_text(
            """#!/usr/bin/env python3
import os
import fcntl
import sys
import time

root = sys.argv[1]
lock_path = os.path.join(root, "journal.lock")
ready_path = os.path.join(root, "journal.lock.ready")

fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
os.chmod(lock_path, 0o600)
with os.fdopen(fd, "a+") as handle:
    fcntl.flock(fd, fcntl.LOCK_EX)
    with open(ready_path, "w", encoding="utf-8") as ready:
        ready.write("locked")
    time.sleep(120)
""",
            encoding="utf-8",
        )
        lock_script.chmod(0o700)
        locker = subprocess.Popen(
            [str(self.python_binary), str(lock_script), str(workspace["journal_root"])],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ready_marker = workspace["journal_root"] / "journal.lock.ready"
        for _ in range(50):
            if ready_marker.exists():
                break
            time.sleep(0.05)
        self.assertTrue(ready_marker.exists())
        try:
            controller = self._write_controller_script(
                workspace,
                self._success_report(
                    run_id="1" * 32,
                    started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
                ),
            )
            with self.assertRaises(ce.ControllerEvidenceBusyError):
                self._run_observation(workspace, controller)
        finally:
            locker.terminate()
            locker.wait(timeout=2.0)

    def test_journal_root_must_be_stable_owned_0700(self) -> None:
        workspace = self._base_workspace()
        workspace["journal_root"].chmod(0o755)
        controller = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="1" * 32,
                started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        )
        with self.assertRaisesRegex(
            ce.ControllerEvidenceError,
            "journal root must be an owned stable 0700 directory",
        ):
            self._run_observation(workspace, controller)

    def test_verify_rejects_duplicate_run_ids_and_overlapping_runs(self) -> None:
        workspace = self._base_workspace()
        repo = workspace["repo_root"] / "controller"
        workspace["repo_root"].mkdir(exist_ok=True)
        default_report = repo.with_name("controller")
        controller = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="1" * 32,
                started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        )
        base = datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc)
        first_report = self._success_report(
            run_id="a" * 32,
            started_at=base,
        )
        duplicate_run = self._success_report(
            run_id="a" * 32,
            started_at=base + timedelta(seconds=5),
        )
        overlapping = self._success_report(
            run_id="b" * 32,
            started_at=base + timedelta(seconds=1),
        )
        with patch(
            "lecture_stt.stt.controller_evidence._run_controller",
            side_effect=[
                (0, self._serialized_report(first_report), "completed"),
                (0, self._serialized_report(duplicate_run), "completed"),
            ],
        ):
            self._run_observation(workspace, controller)
            self._run_observation(workspace, controller)
        with self.assertRaisesRegex(ce.ControllerEvidenceError, "duplicate controller run id"):
            ce.verify_controller_evidence(workspace["journal_root"], min_runs=2, min_verified=2)

        workspace = self._base_workspace()
        controller = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="3" * 32,
                started_at=base,
            ),
        )
        first = self._success_report(
            run_id="c" * 32,
            started_at=base,
            duration_seconds=10,
        )
        second = overlapping
        with patch(
            "lecture_stt.stt.controller_evidence._run_controller",
            side_effect=[
                (0, self._serialized_report(first), "completed"),
                (0, self._serialized_report(second), "completed"),
            ],
        ):
            self._run_observation(workspace, controller)
            self._run_observation(workspace, controller)
        with self.assertRaisesRegex(ce.ControllerEvidenceError, "overlap"):
            ce.verify_controller_evidence(workspace["journal_root"], min_runs=2, min_verified=2)

    def test_verify_rejects_incomplete_journal_suffix_for_readiness_criteria(self) -> None:
        workspace = self._base_workspace()
        controller = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="a" * 32,
                started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        )
        self._run_observation(workspace, controller)
        self._write_records_for_incomplete_suffix(workspace)

        with self.assertRaisesRegex(
            ce.ControllerEvidenceError, "does not satisfy the consecutive readiness criteria"
        ):
            ce.verify_controller_evidence(
                workspace["journal_root"],
                min_runs=1,
                min_verified=2,
            )

    def test_input_invalid_when_controller_inputs_change_after_start_is_logged(self) -> None:
        workspace = self._base_workspace()
        controller = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="1" * 32,
                started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        )
        workspace["config"].write_text("", encoding="utf-8")
        result = self._run_observation(workspace, controller)
        self.assertEqual(result["outcome"], "input_invalid")

        root_fd = self._open_journal_root_fd(workspace["journal_root"])
        try:
            start_record, _ = ce._read_record(root_fd, "00000001.start.json")
            finish_record, _ = ce._read_record(root_fd, "00000001.finish.json")
        finally:
            os.close(root_fd)

        self.assertEqual(finish_record["outcome"], "input_invalid")
        self.assertIsNone(finish_record["exit_code"])
        self.assertEqual(finish_record["runner_status"], "not_started")
        self.assertEqual(finish_record["execution_sha256"], ce.ZERO_SHA256)
        self.assertEqual(finish_record["raw_report_bytes"], 0)
        self.assertEqual(finish_record["raw_report_sha256"], ce.ZERO_SHA256)
        self.assertIsNone(finish_record["report"])
        self.assertEqual(finish_record["report_sha256"], ce.ZERO_SHA256)
        self.assertEqual(finish_record["previous_record_sha256"], start_record["record_sha256"])

    def test_incomplete_start_followed_by_success_preserves_incomplete(self) -> None:
        workspace = self._base_workspace()
        first = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="a" * 32,
                started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        )
        second = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="b" * 32,
                started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        )
        self._run_observation(workspace, first)
        self._write_records_for_incomplete_suffix(workspace)
        self._run_observation(workspace, second)

        summary = ce.verify_controller_evidence(
            workspace["journal_root"],
            min_runs=1,
            min_verified=2,
        )
        self.assertEqual(summary["consecutive_success_count"], 1)
        self.assertEqual(summary["incomplete_count"], 1)
        self.assertEqual(summary["attempt_count"], 3)
        self.assertEqual(summary["completed_count"], 2)

    def test_success_records_include_exact_hash_and_link_metadata(self) -> None:
        workspace = self._base_workspace()
        expected_report = self._success_report(
            run_id="1" * 32,
            started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
        )
        controller = self._write_controller_script(
            workspace,
            expected_report,
        )
        result = self._run_observation(workspace, controller)
        self.assertEqual(result["outcome"], "success")

        execution, _ = ce._execution_evidence(
            controller_binary=controller,
            python_binary=self.python_binary,
            repo_root=workspace["repo_root"],
            config=workspace["config"],
            kill_switch=workspace["kill_switch"],
        )
        raw_report = self._serialized_report(expected_report)
        root_fd = self._open_journal_root_fd(workspace["journal_root"])
        try:
            start_record, _ = ce._read_record(root_fd, "00000001.start.json")
            finish_record, _ = ce._read_record(root_fd, "00000001.finish.json")
        finally:
            os.close(root_fd)

        self.assertEqual(start_record["sequence"], 1)
        self.assertEqual(finish_record["sequence"], 1)
        self.assertEqual(finish_record["outcome"], "success")
        self.assertEqual(finish_record["exit_code"], 0)
        self.assertEqual(finish_record["runner_status"], "completed")
        self.assertEqual(finish_record["previous_record_sha256"], start_record["record_sha256"])
        self.assertEqual(finish_record["execution_sha256"], ce._sha256_json(execution))
        self.assertEqual(start_record["previous_record_sha256"], ce.ZERO_SHA256)
        self.assertEqual(finish_record["raw_report_bytes"], len(raw_report))
        self.assertEqual(finish_record["raw_report_sha256"], ce._sha256_bytes(raw_report))
        self.assertEqual(finish_record["report_sha256"], ce._sha256_json(expected_report))
        self.assertEqual(finish_record["report"], expected_report)
        self.assertEqual(start_record["attempt_id"], finish_record["attempt_id"])

    def test_record_tamper_cases_fail_closed(self) -> None:
        workspace = self._base_workspace()
        controller = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="1" * 32,
                started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        )
        self._run_observation(workspace, controller)
        canonical_start = workspace["journal_root"] / "00000001.start.json"
        tamper = workspace["journal_root"] / "00000002.start.json"
        with self.subTest("symlink_record"):
            tamper.symlink_to(canonical_start)
            with self.assertRaisesRegex(
                ce.ControllerEvidenceError, "journal record is not safely openable"
            ):
                ce.verify_controller_evidence(workspace["journal_root"])
            tamper.unlink()
        with self.subTest("hardlink_record"):
            os.link(canonical_start, tamper)
            with self.assertRaisesRegex(
                ce.ControllerEvidenceError, "journal record file contract is invalid"
            ):
                ce.verify_controller_evidence(workspace["journal_root"])
            tamper.unlink()
        with self.subTest("partial_json_record"):
            tamper.write_text("{", encoding="utf-8")
            tamper.chmod(0o400)
            with self.assertRaisesRegex(
                ce.ControllerEvidenceError, "one complete JSON line"
            ):
                ce.verify_controller_evidence(workspace["journal_root"])
            tamper.unlink()
        with self.subTest("mode_drift_record"):
            os.chmod(canonical_start, 0o644)
            with self.assertRaisesRegex(
                ce.ControllerEvidenceError, "journal record file contract is invalid"
            ):
                ce.verify_controller_evidence(workspace["journal_root"])
            os.chmod(canonical_start, 0o400)

    def test_cli_output_is_metadata_only_and_redacts_paths(self) -> None:
        workspace = self._base_workspace()
        run_script = self._write_controller_script(
            workspace,
            self._success_report(
                run_id="a" * 32,
                started_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            ),
        )

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = ce.main(
                [
                    "run",
                    "--journal-root",
                    str(workspace["journal_root"]),
                    "--controller-bin",
                    str(run_script),
                    "--python-bin",
                    str(self.python_binary),
                    "--repo-root",
                    str(workspace["repo_root"]),
                    "--config",
                    str(workspace["config"]),
                    "--kill-switch",
                    str(workspace["kill_switch"]),
                    "--expected-count",
                    "1",
                    "--enable-observation",
                    "--allow-write",
                ]
            )
        self.assertEqual(exit_code, 0)
        parsed = json.loads(output.getvalue())
        encoded = output.getvalue()
        self.assertNotIn(str(workspace["journal_root"]), encoded)
        self.assertNotIn(str(run_script), encoded)
        self.assertNotIn(str(workspace["repo_root"]), encoded)
        self.assertNotIn("alpha.m4a", encoded)

        verify_output = io.StringIO()
        with redirect_stdout(verify_output):
            verify_exit = ce.main(
                [
                    "verify",
                    "--journal-root",
                    str(workspace["journal_root"]),
                    "--min-runs",
                    "1",
                    "--min-verified",
                    "2",
                ]
            )
        self.assertEqual(verify_exit, 0)
        verify_payload = json.loads(verify_output.getvalue())
        self.assertTrue(verify_payload["ready"])
        verify_text = verify_output.getvalue()
        self.assertNotIn(str(workspace["journal_root"]), verify_text)
        self.assertNotIn(str(run_script), verify_text)
