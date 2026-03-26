from __future__ import annotations

import os
import tempfile
import threading
import unittest
from unittest import mock

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.ui import web_panel_state  # noqa: E402


class WebPanelStateSnapshotTests(unittest.TestCase):
    def _make_state(self) -> web_panel_state.ControlState:
        state = object.__new__(web_panel_state.ControlState)
        state.lock = threading.Lock()
        state.notice = "정상 동작 중"
        state.config_data = {"notification": {"provider": "telegram", "enabled": True, "dual_send_providers": []}}
        state._notification_restart_required = False
        state._notification_availability = mock.Mock(return_value={"telegram": True, "discord": False})
        state._is_managed_running = mock.Mock(return_value=True)
        state._find_worker_pids = mock.Mock(return_value=[4321])
        state._is_paused = mock.Mock(return_value=False)
        state._db_counts = mock.Mock(
            return_value={
                "PENDING": 1,
                "PROCESSING": 2,
                "DONE": 3,
                "ERROR": 4,
                "UNREGISTERED": 5,
            }
        )
        state._folder_counts = mock.Mock(
            return_value={
                "inbox": "11",
                "audio": "12",
                "transcripts": "13",
                "errors": "14",
            }
        )
        state._recent_jobs = mock.Mock(
            return_value=[
                (7, "PROCESSING", "lecture.m4a", "2026-03-22 21:00:00", "전사 시작/진행", 45, ""),
            ]
        )
        state._processing_jobs = mock.Mock(
            return_value=[
                (7, "lecture.m4a", "전사 시작/진행", 45, 120),
            ]
        )
        state._recent_log_text = mock.Mock(return_value="recent log line")
        state._poll_interval_sec = mock.Mock(return_value=1.0)
        return state

    def test_snapshot_includes_react_friendly_fields(self) -> None:
        state = self._make_state()

        snapshot = state.snapshot(include_log=False)

        self.assertEqual(snapshot["schema_version"], 2)
        self.assertEqual(snapshot["runtime_state"]["status"], "running")
        self.assertEqual(snapshot["runtime_state"]["source"], "web")
        self.assertEqual(snapshot["notification"]["selection"], "telegram")
        self.assertFalse(snapshot["notification"]["restart_required"])
        self.assertTrue(snapshot["notification"]["can_apply_now"])
        self.assertEqual(snapshot["actions"]["pause_action"], "pause")
        self.assertEqual(snapshot["actions"]["endpoints"]["state"], "/api/state")
        self.assertEqual(snapshot["actions"]["endpoints"]["notification"], "/api/notification")
        self.assertEqual(snapshot["jobs_v2"][0]["id"], 7)
        self.assertEqual(snapshot["jobs_v2"][0]["file_name"], "lecture.m4a")
        self.assertEqual(snapshot["processing_v2"][0]["eta_sec"], 120)
        self.assertEqual(snapshot["summary"]["current_job"]["id"], 7)
        self.assertEqual(snapshot["summary"]["notice"], "정상 동작 중")
        self.assertEqual(snapshot["summary"]["poll_interval_sec"], 1.0)
        self.assertEqual(snapshot["log_tail"], "")
        self.assertEqual(snapshot["runtime"], snapshot["runtime_state"]["label"])
        self.assertEqual(snapshot["runtime_desc"], snapshot["runtime_state"]["description"])
        self.assertEqual(snapshot["jobs"][0][0], snapshot["jobs_v2"][0]["id"])
        self.assertEqual(snapshot["processing"][0][0], snapshot["processing_v2"][0]["id"])
        self.assertEqual(
            set(snapshot["actions"]["endpoints"].keys()),
            {
                "state",
                "logs",
                "notification",
                "start",
                "pause",
                "resume",
                "stop",
                "refresh",
                "exit",
                "clear_history",
            },
        )

    def test_update_notification_selection_writes_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.yaml").write_text(
                "paths:\n"
                f"  db_path: {root / 'state' / 'jobs.sqlite3'}\n"
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            state = web_panel_state.ControlState(repo_root=root)
            state._is_managed_running = mock.Mock(return_value=False)
            state._find_worker_pids = mock.Mock(return_value=[])
            state._set_poll_boost = mock.Mock()

            with mock.patch.object(
                state,
                "_notification_availability",
                return_value={"telegram": True, "discord": True},
            ):
                state.update_notification_selection("both")

            saved = state._load_config()
            self.assertEqual(saved["notification"]["provider"], "telegram")
            self.assertEqual(saved["notification"]["dual_send_providers"], ["discord"])
            self.assertTrue(saved["notification"]["enabled"])
            self.assertIn("다음 시작부터 적용", state.notice)

    def test_control_state_resolves_env_backed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_root = root / "runtime-state"
            recordings_root = root / "recordings"
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (root / ".env").write_text(
                (
                    f"STATE_ROOT={state_root}\n"
                    f"LECTURE_RECORDINGS_ROOT={recordings_root}\n"
                ),
                encoding="utf-8",
            )
            (config_dir / "config.yaml").write_text(
                (
                    "paths:\n"
                    "  db_path: ${STATE_ROOT}/jobs.sqlite3\n"
                    "  watch_folder: ${LECTURE_RECORDINGS_ROOT}/00_inbox\n"
                    "logging:\n"
                    "  file: logs/app.log\n"
                ),
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {
                    "STATE_ROOT": str(state_root),
                    "LECTURE_RECORDINGS_ROOT": str(recordings_root),
                },
                clear=False,
            ):
                state = web_panel_state.ControlState(repo_root=root)

            self.assertEqual(state.db_path, state_root / "jobs.sqlite3")
            self.assertEqual(state.watch_folder, recordings_root / "00_inbox")

    def test_restart_worker_for_notification_restarts_and_clears_flag(self) -> None:
        state = object.__new__(web_panel_state.ControlState)
        state._notification_restart_required = True
        state.notice = "대기중"
        state.start = mock.Mock(side_effect=lambda: setattr(state, "_notification_restart_required", False))
        state.stop = mock.Mock()
        state._is_managed_running = mock.Mock(return_value=True)
        state._find_worker_pids = mock.Mock(return_value=[1234])
        state._wait_for_worker_shutdown = mock.Mock(return_value=True)
        state._set_poll_boost = mock.Mock()

        state.restart_worker_for_notification()

        state.stop.assert_called_once_with()
        state.start.assert_called_once_with()
        state._wait_for_worker_shutdown.assert_called_once_with()
        self.assertFalse(state._notification_restart_required)
        self.assertIn("즉시 적용", state.notice)

    def test_log_stream_delta_marks_reset_after_truncate(self) -> None:
        state = object.__new__(web_panel_state.ControlState)
        state.LOG_READ_BYTES = 65536

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "app.log"
            log_path.write_text("line-1\nline-2\n", encoding="utf-8")
            state.log_path = log_path

            offset, text, reset = state._log_stream_delta(None)

            self.assertTrue(reset)
            self.assertEqual(text, "line-1\nline-2\n")
            self.assertEqual(offset, log_path.stat().st_size)

            log_path.write_text("", encoding="utf-8")

            next_offset, next_text, next_reset = state._log_stream_delta(offset)

            self.assertTrue(next_reset)
            self.assertEqual(next_offset, 0)
            self.assertEqual(next_text, "")
