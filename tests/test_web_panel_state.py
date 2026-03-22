from __future__ import annotations

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
        self.assertEqual(snapshot["actions"]["pause_action"], "pause")
        self.assertEqual(snapshot["actions"]["endpoints"]["state"], "/api/state")
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
            {"state", "logs", "start", "pause", "resume", "stop", "refresh", "exit", "clear_history"},
        )

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
