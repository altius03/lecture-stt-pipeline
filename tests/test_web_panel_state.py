from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
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
                "NEEDS_REVIEW": 3,
                "DONE": 4,
                "ERROR": 5,
                "UNREGISTERED": 6,
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
        state.transcription_analytics = mock.Mock(
            side_effect=AssertionError("snapshot must not query analytics")
        )
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
        self.assertEqual(snapshot["counts"]["NEEDS_REVIEW"], 3)
        self.assertNotIn("analytics", snapshot)
        state.transcription_analytics.assert_not_called()
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

    def test_snapshot_controller_idle_with_kill_switch_is_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "controller.disabled"
            state = self._make_state()
            state.config_data = {
                "app": {
                    "execution_owner": "controller",
                    "controller_label": "com.geonha.lecture-stt-controller",
                    "controller_kill_switch": str(marker),
                },
                "notification": {
                    "provider": "telegram",
                    "enabled": True,
                    "dual_send_providers": [],
                },
            }
            state._is_managed_running = mock.Mock(return_value=False)
            state._find_worker_pids = mock.Mock(return_value=[])
            state._is_paused = mock.Mock(return_value=False)
            state._controller_service_loaded = mock.Mock(return_value=True)
            state._controller_kill_switch_active = mock.Mock(return_value=True)

            snapshot = state.snapshot(include_log=False)

            self.assertEqual(snapshot["runtime_state"]["status"], "stopped")
            self.assertEqual(snapshot["runtime_state"]["source"], "controller")
            self.assertEqual(snapshot["runtime"], "중지")
            self.assertIn("kill-switch", snapshot["runtime_desc"])

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
            self.assertTrue(saved["notification"]["send_review"])
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

    def test_transcription_analytics_gate_is_boolean_only_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            config_path = config_dir / "config.yaml"
            config_path.write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                web_panel_state,
                "read_transcription_analytics",
            ) as read_analytics:
                default_state = web_panel_state.ControlState(repo_root=root)
                default_payload = default_state.transcription_analytics(
                    period="month"
                )

                config_path.write_text(
                    "paths:\n"
                    "  db_path: state/jobs.sqlite3\n"
                    "storage_v2:\n"
                    "  analytics:\n"
                    '    enabled: "true"\n'
                    "logging:\n"
                    "  file: logs/app.log\n",
                    encoding="utf-8",
                )
                string_state = web_panel_state.ControlState(repo_root=root)
                string_payload = string_state.transcription_analytics(
                    period="day"
                )

            self.assertFalse(default_payload["available"])
            self.assertEqual(len(default_payload["timeline"]), 30)
            self.assertFalse(string_payload["available"])
            self.assertEqual(len(string_payload["timeline"]), 24)
            read_analytics.assert_not_called()
            self.assertFalse((root / "state" / "storage-v2.sqlite3").exists())

    def test_enabled_transcription_analytics_requires_and_resolves_records_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            config_path = config_dir / "config.yaml"
            config_path.write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  db_path: state/analytics.sqlite3\n"
                "  analytics:\n"
                "    enabled: true\n"
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            missing_root_state = web_panel_state.ControlState(repo_root=root)

            with self.assertRaisesRegex(RuntimeError, "records_root"):
                missing_root_state.transcription_analytics(period="week")

            config_path.write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  db_path: state/analytics.sqlite3\n"
                "  records_root: private/records\n"
                "  analytics:\n"
                "    enabled: true\n"
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            enabled_state = web_panel_state.ControlState(repo_root=root)
            expected_payload = {"available": True, "period": "day"}

            with mock.patch.object(
                web_panel_state,
                "read_transcription_analytics",
                return_value=expected_payload,
            ) as read_analytics:
                payload = enabled_state.transcription_analytics(period="day")

            self.assertEqual(payload, expected_payload)
            read_analytics.assert_called_once_with(
                root / "state" / "analytics.sqlite3",
                root / "private" / "records",
                period="day",
            )

    def test_transcription_analytics_prometheus_renders_series(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.yaml").write_text(
                "paths:\n  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  db_path: state/storage-v2.sqlite3\n"
                "  records_root: private/records\n"
                "  analytics:\n"
                "    enabled: true\n"
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            state = web_panel_state.ControlState(repo_root=root)
            payload = {
                "period": "day",
                "available": True,
                "coverage": {
                    "quality_missing": 0,
                    "quality_invalid": 0,
                    "quality_scored": 3,
                },
                "totals": {
                    "jobs": 3,
                    "average_quality_score": 88.3,
                    "audio_duration_sec": 120.5,
                    "total_processing_sec": 20.2,
                },
                "status_distribution": [
                    {"status": "done", "count": 2},
                    {"status": "needs_review", "count": 1},
                ],
                "quality_distribution": [
                    {"band": "90-100", "count": 1},
                    {"band": "80-89", "count": 2},
                    {"band": "70-79", "count": 0},
                    {"band": "60-69", "count": 0},
                    {"band": "0-59", "count": 0},
                    {"band": "unscored", "count": 0},
                ],
                "classification_distribution": [
                    {
                        "context_type": "class_session",
                        "source": "manual",
                        "count": 2,
                    },
                    {
                        "context_type": None,
                        "source": None,
                        "count": 1,
                    },
                ],
                "freshness": {
                    "generated_at": "2026-07-25T00:00:00+09:00",
                    "latest_event_at": "2026-07-25T00:01:00+09:00",
                },
                "window": {
                    "start_at": "2026-07-24T00:00:00+09:00",
                    "end_at": "2026-07-25T00:00:00+09:00",
                },
            }

            with mock.patch.object(
                web_panel_state,
                "read_transcription_analytics",
                return_value=payload,
            ):
                metrics = state.transcription_analytics_prometheus(period="day")

            self.assertIn(
                'lecture_stt_transcriptions_jobs_in_window{period="day",available="true",status="done"} 2',
                metrics,
            )
            self.assertIn(
                'lecture_stt_transcriptions_quality_band_jobs{period="day",available="true",band="80-89"} 2',
                metrics,
            )
            self.assertIn(
                'lecture_stt_transcriptions_classification_jobs{period="day",available="true",context_type="class_session",source="manual"} 2',
                metrics,
            )
            self.assertIn(
                'lecture_stt_transcriptions_average_quality_score{period="day",available="true"} 88.3',
                metrics,
            )
            self.assertIn(
                'lecture_stt_transcriptions_jobs_in_window_overall{period="day",available="true"} 3',
                metrics,
            )
            self.assertNotIn(
                "lecture_stt_transcriptions_jobs_in_window_total",
                metrics,
            )
            self.assertEqual(
                metrics.count(
                    "# HELP lecture_stt_transcriptions_jobs_in_window "
                ),
                1,
            )
            self.assertIn(
                'lecture_stt_transcriptions_quality_band_jobs{period="day",available="true",band="70-79"} 0',
                metrics,
            )

    def test_recording_library_is_disabled_by_default_without_touching_v2_db(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.yaml").write_text(
                "paths:\n  db_path: state/jobs.sqlite3\n"
                "logging:\n  file: logs/app.log\n",
                encoding="utf-8",
            )
            state = web_panel_state.ControlState(repo_root=root)

            payload = state.recording_library_list(limit=50, offset=0)

            self.assertFalse(payload["available"])
            self.assertEqual(
                payload["disabled_reason"],
                "recording_library_disabled",
            )
            self.assertFalse((root / "state" / "storage-v2.sqlite3").exists())
            with self.assertRaises(
                web_panel_state.RecordingLibraryDisabledError
            ):
                state.recording_library_detail("recording_a")

    def test_enabled_recording_library_delegates_with_resolved_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.yaml").write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  db_path: state/storage-v2.sqlite3\n"
                "  library:\n"
                "    enabled: true\n"
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            state = web_panel_state.ControlState(repo_root=root)

            with mock.patch.object(
                web_panel_state,
                "list_recordings",
                return_value={"available": True, "summaries": []},
            ) as list_recordings:
                list_payload = state.recording_library_list(limit=25, offset=10)
            with mock.patch.object(
                web_panel_state,
                "read_recording_detail",
                return_value={"available": True, "recording": {"storage_key": "recording_a"}},
            ) as read_detail:
                detail_payload = state.recording_library_detail("recording_a")

            self.assertTrue(list_payload["available"])
            list_recordings.assert_called_once_with(
                root / "state" / "storage-v2.sqlite3",
                limit=25,
                offset=10,
            )
            self.assertEqual(detail_payload["recording"]["storage_key"], "recording_a")
            read_detail.assert_called_once_with(
                root / "state" / "storage-v2.sqlite3",
                "recording_a",
            )

    def test_unified_review_feed_uses_same_db_path_and_source_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.yaml").write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  db_path: state/storage-v2.sqlite3\n"
                "  archive_review:\n"
                "    enabled: true\n"
                "  timetable:\n"
                "    enabled: false\n"
                "  title_review:\n"
                "    enabled: true\n"
                "  library:\n"
                "    enabled: true\n"
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            state = web_panel_state.ControlState(repo_root=root)

            with mock.patch.object(
                web_panel_state,
                "read_unified_review_feed",
                return_value={"available": True, "items": []},
            ) as read_feed:
                payload = state.unified_review_feed(limit=25, offset=10)

            self.assertTrue(payload["available"])
            read_feed.assert_called_once_with(
                root / "state" / "storage-v2.sqlite3",
                archive_enabled=True,
                timetable_enabled=False,
                title_enabled=True,
                recording_enabled=True,
                limit=25,
                offset=10,
            )

    def test_title_review_is_disabled_by_default_without_touching_v2_db(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.yaml").write_text(
                "paths:\n  db_path: state/jobs.sqlite3\n"
                "logging:\n  file: logs/app.log\n",
                encoding="utf-8",
            )
            state = web_panel_state.ControlState(repo_root=root)

            payload = state.title_review_list()

            self.assertFalse(payload["available"])
            self.assertEqual(
                payload["disabled_reason"],
                "title_review_disabled",
            )
            self.assertFalse((root / "state" / "storage-v2.sqlite3").exists())
            with self.assertRaises(
                web_panel_state.TitleSuggestionWriteDisabledError
            ):
                state.title_review_detail(1)
            with self.assertRaises(
                web_panel_state.TitleSuggestionWriteDisabledError
            ):
                state.title_review_status_update(
                    1,
                    status="rejected",
                    allow_write=True,
                )
            with self.assertRaises(
                web_panel_state.TitleSuggestionWriteDisabledError
            ):
                state.title_review_confirmation_plan(1)
            with self.assertRaises(
                web_panel_state.TitleSuggestionWriteDisabledError
            ):
                state.title_review_confirmation_apply(
                    1,
                    expected_count=1,
                    expected_plan_sha256="a" * 64,
                    allow_write=True,
                )

    def test_title_review_config_is_boolean_only_and_guards_confirmations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.yaml").write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  db_path: state/storage-v2.sqlite3\n"
                "  records_root: state/storage-v2-records\n"
                "  title_review:\n"
                "    enabled: true\n"
                "    status_writes_enabled: false\n"
                "    confirmations_enabled: false\n"
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            state = web_panel_state.ControlState(repo_root=root)

            with mock.patch.object(
                web_panel_state,
                "list_title_suggestions",
                return_value={"available": True, "proposals": []},
            ) as list_title:
                payload = state.title_review_list(status="suggested")
            self.assertFalse(payload["capabilities"]["confirmations_enabled"])
            self.assertFalse(payload["capabilities"]["status_writes_enabled"])
            list_title.assert_called_once_with(
                root / "state" / "storage-v2.sqlite3",
                status="suggested",
                limit=50,
                offset=0,
            )

            with mock.patch.object(
                web_panel_state,
                "read_title_suggestion",
                return_value={"proposal": {"id": 7}},
            ) as read_detail:
                detail_payload = state.title_review_detail(7)
            self.assertEqual(detail_payload["proposal"]["id"], 7)
            read_detail.assert_called_once_with(
                root / "state" / "storage-v2.sqlite3",
                7,
            )

            with mock.patch.object(
                web_panel_state,
                "apply_title_suggestion_confirmation",
                return_value={"ok": True},
            ) as apply_confirmation:
                state.title_review_confirmation_apply(
                    7,
                    expected_count=1,
                    expected_plan_sha256="a" * 64,
                    allow_write=True,
                )
            apply_confirmation.assert_called_once_with(
                root / "state" / "storage-v2.sqlite3",
                root / "state" / "storage-v2-records",
                7,
                expected_count=1,
                expected_plan_sha256="a" * 64,
                confirmations_enabled=False,
                allow_write=True,
            )

            with mock.patch.object(
                web_panel_state,
                "reject_title_suggestion",
                return_value={"ok": True},
            ) as reject_title:
                state.title_review_status_update(
                    7,
                    status="rejected",
                    allow_write=True,
                )
            reject_title.assert_called_once_with(
                root / "state" / "storage-v2.sqlite3",
                7,
                status_writes_enabled=False,
                allow_write=True,
            )

            (config_dir / "config.yaml").write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  title_review:\n"
                '    enabled: "true"\n'
                '    status_writes_enabled: "true"\n'
                '    confirmations_enabled: "true"\n'
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            string_state = web_panel_state.ControlState(repo_root=root)
            self.assertFalse(string_state.title_review_list()["available"])
            with self.assertRaises(
                web_panel_state.TitleSuggestionWriteDisabledError
            ):
                string_state.title_review_confirmation_plan(7)

    def test_title_review_records_root_is_resolved_lazily(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.yaml").write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  db_path: state/storage-v2.sqlite3\n"
                "  records_root: ${MISSING_STORAGE_ROOT}\n"
                "  title_review:\n"
                "    enabled: false\n"
                "  archive_review:\n"
                "    enabled: true\n"
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            state = web_panel_state.ControlState(repo_root=root)

            with mock.patch.object(
                web_panel_state,
                "read_unified_review_feed",
                return_value={"available": False, "items": []},
            ) as read_feed:
                payload = state.unified_review_feed()

            self.assertFalse(payload["available"])
            read_feed.assert_called_once_with(
                root / "state" / "storage-v2.sqlite3",
                archive_enabled=True,
                timetable_enabled=False,
                title_enabled=False,
                recording_enabled=False,
                limit=50,
                offset=0,
            )

            (config_dir / "config.yaml").write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  db_path: state/storage-v2.sqlite3\n"
                "  title_review:\n"
                "    enabled: true\n"
                "    confirmations_enabled: false\n"
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            enabled_state = web_panel_state.ControlState(repo_root=root)
            with mock.patch.object(
                web_panel_state,
                "list_title_suggestions",
                return_value={"available": True, "proposals": []},
            ) as list_title:
                enabled_state.title_review_list()
            list_title.assert_called_once_with(
                root / "state" / "storage-v2.sqlite3",
                status=None,
                limit=50,
                offset=0,
            )

    def test_title_review_enabled_missing_db_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.yaml").write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  db_path: state/missing-storage-v2.sqlite3\n"
                "  title_review:\n"
                "    enabled: true\n"
                "    status_writes_enabled: true\n"
                "    confirmations_enabled: true\n"
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            state = web_panel_state.ControlState(repo_root=root)

            payload = state.title_review_list()

            self.assertFalse(payload["available"])
            self.assertEqual(
                payload["disabled_reason"],
                "storage_v2_db_unavailable",
            )
            self.assertEqual(
                payload["capabilities"],
                {
                    "confirmations_enabled": False,
                    "status_writes_enabled": False,
                },
            )

    def test_archive_review_is_disabled_by_default_without_touching_v2_db(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.yaml").write_text(
                "paths:\n  db_path: state/jobs.sqlite3\n"
                "logging:\n  file: logs/app.log\n",
                encoding="utf-8",
            )
            state = web_panel_state.ControlState(repo_root=root)

            payload = state.archive_review_list()

            self.assertFalse(payload["available"])
            self.assertEqual(
                payload["disabled_reason"],
                "archive_review_disabled",
            )
            self.assertFalse((root / "state" / "storage-v2.sqlite3").exists())
            with self.assertRaises(
                web_panel_state.ArchiveReviewWriteDisabledError
            ):
                state.archive_review_detail("case_a")
            with self.assertRaises(
                web_panel_state.ArchiveReviewWriteDisabledError
            ):
                state.archive_review_update_status(
                    "case_a",
                    "triaged",
                    allow_write=True,
                )
            with self.assertRaises(
                web_panel_state.ArchiveReviewWriteDisabledError
            ):
                state.archive_review_plan_promotion(
                    "case_a",
                    target_storage_key="recording_a",
                    selected_revisions={"summary_markdown": 1},
                )
            with self.assertRaises(
                web_panel_state.ArchiveReviewWriteDisabledError
            ):
                state.archive_review_apply_promotion(
                    "case_a",
                    target_storage_key="recording_a",
                    selected_revisions={"summary_markdown": 1},
                    expected_count=1,
                    expected_plan_sha256="a" * 64,
                    allow_write=True,
                )

    def test_archive_review_config_guards_status_and_promotion_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.yaml").write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  db_path: state/storage-v2.sqlite3\n"
                "  archive_review:\n"
                "    enabled: true\n"
                "    status_writes_enabled: true\n"
                "    promotions_enabled: false\n"
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            state = web_panel_state.ControlState(repo_root=root)

            with mock.patch.object(
                web_panel_state,
                "update_archive_review_status",
                return_value={"ok": True},
            ) as update_status:
                state.archive_review_update_status(
                    "case_a",
                    "triaged",
                    allow_write=True,
                )
            update_status.assert_called_once_with(
                root / "state" / "storage-v2.sqlite3",
                "case_a",
                "triaged",
                allow_write=True,
            )

            with mock.patch.object(
                web_panel_state,
                "apply_archive_review_promotion",
                return_value={"ok": True},
            ) as apply_promotion:
                state.archive_review_apply_promotion(
                    "case_a",
                    target_storage_key="recording_a",
                    selected_revisions={"summary_markdown": 7},
                    expected_count=1,
                    expected_plan_sha256="a" * 64,
                    allow_write=True,
                )
            apply_promotion.assert_called_once_with(
                root / "state" / "storage-v2.sqlite3",
                "case_a",
                target_storage_key="recording_a",
                selected_revisions={"summary_markdown": 7},
                expected_count=1,
                expected_plan_sha256="a" * 64,
                promotions_enabled=False,
                allow_write=True,
            )

    def test_archive_review_string_flags_remain_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.yaml").write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  db_path: state/storage-v2.sqlite3\n"
                "  archive_review:\n"
                '    enabled: "true"\n'
                '    status_writes_enabled: "true"\n'
                '    promotions_enabled: "true"\n'
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            state = web_panel_state.ControlState(repo_root=root)

            payload = state.archive_review_list()

            self.assertFalse(payload["available"])
            self.assertEqual(
                payload["disabled_reason"],
                "archive_review_disabled",
            )

    def test_timetable_api_is_disabled_by_default_without_touching_v2_db(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.yaml").write_text(
                "paths:\n  db_path: state/jobs.sqlite3\n"
                "logging:\n  file: logs/app.log\n",
                encoding="utf-8",
            )
            state = web_panel_state.ControlState(repo_root=root)

            timetable = state.timetable_list()
            classifications = state.timetable_classification_list()

            self.assertFalse(timetable["available"])
            self.assertFalse(classifications["available"])
            self.assertFalse((root / "state" / "storage-v2.sqlite3").exists())
            with self.assertRaises(
                web_panel_state.TimetableWriteDisabledError
            ):
                state.timetable_classification_detail(1)
            with self.assertRaises(
                web_panel_state.TimetableWriteDisabledError
            ):
                state.timetable_confirmation_plan(1)
            with self.assertRaises(
                web_panel_state.TimetableWriteDisabledError
            ):
                state.timetable_classification_status_update(
                    1,
                    status="rejected",
                    allow_write=True,
                )

    def test_timetable_config_is_boolean_only_and_guards_confirmations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "config.yaml").write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  db_path: state/storage-v2.sqlite3\n"
                "  timetable:\n"
                "    enabled: true\n"
                "    status_writes_enabled: false\n"
                "    confirmations_enabled: false\n"
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            state = web_panel_state.ControlState(repo_root=root)

            with mock.patch.object(
                web_panel_state,
                "list_timetable_entries",
                return_value={"available": True, "entries": []},
            ) as list_entries:
                payload = state.timetable_list(semester="2026-1")
            self.assertFalse(payload["capabilities"]["confirmations_enabled"])
            self.assertFalse(payload["capabilities"]["status_writes_enabled"])
            list_entries.assert_called_once_with(
                root / "state" / "storage-v2.sqlite3",
                semester="2026-1",
                limit=100,
                offset=0,
            )

            with mock.patch.object(
                web_panel_state,
                "apply_classification_confirmation",
                return_value={"ok": True},
            ) as apply_confirmation:
                state.timetable_confirmation_apply(
                    7,
                    expected_count=1,
                    expected_plan_sha256="a" * 64,
                    allow_write=True,
                )
            apply_confirmation.assert_called_once_with(
                root / "state" / "storage-v2.sqlite3",
                7,
                expected_count=1,
                expected_plan_sha256="a" * 64,
                confirmations_enabled=False,
                allow_write=True,
            )

            with mock.patch.object(
                web_panel_state,
                "update_classification_status",
                return_value={"ok": True},
            ) as update_status:
                state.timetable_classification_status_update(
                    7,
                    status="rejected",
                    allow_write=True,
                )
            update_status.assert_called_once_with(
                root / "state" / "storage-v2.sqlite3",
                7,
                status="rejected",
                status_writes_enabled=False,
                allow_write=True,
            )

            (config_dir / "config.yaml").write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  timetable:\n"
                '    enabled: "true"\n'
                '    status_writes_enabled: "true"\n'
                '    confirmations_enabled: "true"\n'
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            string_state = web_panel_state.ControlState(repo_root=root)
            self.assertFalse(string_state.timetable_list()["available"])
            with self.assertRaises(
                web_panel_state.TimetableWriteDisabledError
            ):
                string_state.timetable_classification_status_update(
                    7,
                    status="rejected",
                    allow_write=True,
                )


class WebPanelControllerExecutionOwnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._immutable_path_check = (
            web_panel_state.ControlState._console_binary_path_is_immutable
        )
        self._immutable_path_patcher = mock.patch.object(
            web_panel_state.ControlState,
            "_console_binary_path_is_immutable",
            return_value=True,
        )
        self._immutable_path_patcher.start()
        self.addCleanup(self._immutable_path_patcher.stop)
        self._console_runtime_patcher = mock.patch.object(
            web_panel_state.ControlState,
            "_console_runtime_supported",
            return_value=True,
        )
        self._console_runtime_patcher.start()
        self.addCleanup(self._console_runtime_patcher.stop)

    def _write_config(
        self,
        root: Path,
        *,
        execution_owner: str = "python",
        controller_label: str = "com.geonha.lecture-stt-controller",
        controller_kill_switch: Path | None = None,
        controller_runtime: str = "launchd",
        controller_binary: Path | None = None,
        controller_binary_sha256: str | None = None,
        controller_state_dir: Path | None = None,
        controller_pid_path: Path | None = None,
        controller_stdout_path: Path | None = None,
        controller_stderr_path: Path | None = None,
    ) -> None:
        config_dir = root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        app_lines: list[str] = []
        if execution_owner == "controller":
            marker = controller_kill_switch or (root / "state" / "controller.disabled")
            app_lines = [
                "app:\n",
                f"  execution_owner: {execution_owner}\n",
                f"  controller_label: {controller_label}\n",
                f"  controller_kill_switch: {marker}\n",
                f"  controller_runtime: {controller_runtime}\n",
            ]
            if controller_binary is not None:
                app_lines.append(f"  controller_binary: {controller_binary}\n")
            if controller_binary_sha256 is not None:
                app_lines.append(
                    f"  controller_binary_sha256: {controller_binary_sha256}\n"
                )
            if controller_state_dir is not None:
                app_lines.append(f"  controller_state_dir: {controller_state_dir}\n")
            if controller_pid_path is not None:
                app_lines.append(f"  controller_pid_path: {controller_pid_path}\n")
            if controller_stdout_path is not None:
                app_lines.append(f"  controller_stdout_path: {controller_stdout_path}\n")
            if controller_stderr_path is not None:
                app_lines.append(f"  controller_stderr_path: {controller_stderr_path}\n")
        (config_dir / "config.yaml").write_text(
            "".join(
                [
                    "paths:\n",
                    "  db_path: state/jobs.sqlite3\n",
                    "logging:\n",
                    "  file: logs/app.log\n",
                    *app_lines,
                ]
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _completed_process(
        cmd: list[str],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    def test_controller_start_uses_launchctl_and_never_spawns_python(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / "state" / "controller.disabled"
            self._write_config(
                root,
                execution_owner="controller",
                controller_kill_switch=marker,
            )
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("", encoding="utf-8")
            marker.chmod(0o600)
            paused = root / "state" / "paused"
            paused.write_text("", encoding="utf-8")
            state = web_panel_state.ControlState(repo_root=root)
            state._set_poll_boost = mock.Mock()

            def fake_run(cmd, **kwargs):
                if cmd[:2] == ["launchctl", "print"]:
                    return self._completed_process(cmd, stdout="loaded")
                if cmd[:2] == ["launchctl", "kickstart"]:
                    return self._completed_process(cmd)
                raise AssertionError(cmd)

            with mock.patch.object(web_panel_state.subprocess, "run", side_effect=fake_run) as run_mock:
                with mock.patch.object(web_panel_state.subprocess, "Popen") as popen_mock:
                    state.start()

            popen_mock.assert_not_called()
            self.assertFalse(marker.exists())
            self.assertFalse(paused.exists())
            self.assertIn("재개", state.notice)
            self.assertEqual(run_mock.call_args_list[0].args[0][:2], ["launchctl", "print"])
            self.assertEqual(run_mock.call_args_list[1].args[0][:3], ["launchctl", "kickstart", "-k"])

    def test_controller_start_fails_closed_when_service_not_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / "state" / "controller.disabled"
            self._write_config(
                root,
                execution_owner="controller",
                controller_kill_switch=marker,
            )
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("", encoding="utf-8")
            state = web_panel_state.ControlState(repo_root=root)

            with mock.patch.object(
                web_panel_state.subprocess,
                "run",
                return_value=self._completed_process(
                    ["launchctl", "print"],
                    returncode=113,
                    stderr="not loaded",
                ),
            ) as run_mock:
                with mock.patch.object(web_panel_state.subprocess, "Popen") as popen_mock:
                    state.start()

            popen_mock.assert_not_called()
            self.assertTrue(marker.exists())
            self.assertIn("load되지 않아", state.notice)
            run_mock.assert_called_once()

    def test_controller_start_rejects_invalid_kill_switch_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / "state" / "controller.disabled"
            self._write_config(
                root,
                execution_owner="controller",
                controller_kill_switch=marker,
            )
            marker.parent.mkdir(parents=True, exist_ok=True)
            target = marker.parent / "marker-target"
            target.write_text("x", encoding="utf-8")
            marker.symlink_to(target)
            state = web_panel_state.ControlState(repo_root=root)

            def fake_run(cmd, **kwargs):
                if cmd[:2] == ["launchctl", "print"]:
                    return self._completed_process(cmd, stdout="loaded")
                if cmd[:2] == ["launchctl", "kickstart"]:
                    return self._completed_process(cmd)
                raise AssertionError(cmd)

            with mock.patch.object(
                web_panel_state.subprocess,
                "run",
                side_effect=fake_run,
            ) as run_mock:
                state.start()

            self.assertIn("상태가 올바르지 않습니다", state.notice)
            self.assertEqual(len(run_mock.call_args_list), 1)
            self.assertTrue(marker.is_symlink())

    def test_controller_stop_creates_kill_switch_and_uses_launchctl_kill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / "state" / "controller.disabled"
            self._write_config(
                root,
                execution_owner="controller",
                controller_kill_switch=marker,
            )
            state = web_panel_state.ControlState(repo_root=root)
            marker.parent.chmod(0o700)
            state._set_poll_boost = mock.Mock()

            with mock.patch.object(
                web_panel_state.subprocess,
                "run",
                return_value=self._completed_process(["launchctl", "kill"]),
            ) as run_mock:
                state.stop()

            run_mock.assert_called_once()
            self.assertTrue(marker.exists())
            self.assertTrue(marker.is_file())
            self.assertIn("중지 요청", state.notice)
            self.assertEqual(run_mock.call_args.args[0][:3], ["launchctl", "kill", "SIGTERM"])

    def test_controller_stop_reports_launchctl_failure_and_keeps_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / "state" / "controller.disabled"
            self._write_config(
                root,
                execution_owner="controller",
                controller_kill_switch=marker,
            )
            state = web_panel_state.ControlState(repo_root=root)
            marker.parent.chmod(0o700)
            state._set_poll_boost = mock.Mock()

            with mock.patch.object(
                web_panel_state.subprocess,
                "run",
                return_value=self._completed_process(
                    ["launchctl", "kill"],
                    returncode=1,
                    stderr="permission denied",
                ),
            ):
                state.stop()

            self.assertTrue(marker.exists())
            self.assertNotIn("permission denied", state.notice)
            self.assertIn("서비스 제어 명령이 실패", state.notice)
            self.assertIn("중지 실패", state.notice)

    def _console_fixture(self, root: Path) -> tuple[Path, Path, Path, Path, Path]:
        state_dir = root / "controller-state"
        state_dir.mkdir(mode=0o700)
        binary_payload = b"controller"
        binary_sha256 = hashlib.sha256(binary_payload).hexdigest()
        versions = root / "versions"
        versions.mkdir(mode=0o700)
        version_dir = versions / binary_sha256
        version_dir.mkdir(mode=0o700)
        binary = version_dir / "lecture-stt-controller"
        binary.write_bytes(binary_payload)
        binary.chmod(0o500)
        version_dir.chmod(0o500)
        versions.chmod(0o500)
        logs = root / "logs"
        logs.mkdir(mode=0o700)
        marker = state_dir / "controller.disabled"
        pid_path = root / "controller.pid"
        stdout_path = logs / "controller.out.jsonl"
        stderr_path = logs / "controller.err.log"
        self._write_config(
            root,
            execution_owner="controller",
            controller_kill_switch=marker,
            controller_runtime="console",
            controller_binary=binary,
            controller_binary_sha256=binary_sha256,
            controller_state_dir=state_dir,
            controller_pid_path=pid_path,
            controller_stdout_path=stdout_path,
            controller_stderr_path=stderr_path,
        )
        return binary, state_dir, marker, stdout_path, stderr_path

    @staticmethod
    def _console_pid_payload(
        pid: int,
        *,
        tv_sec: int = 1_753_684_496,
        tv_usec: int = 123_456,
    ) -> str:
        return (
            json.dumps(
                {
                    "schema_version": web_panel_state.CONTROLLER_PID_SCHEMA,
                    "pid": pid,
                    "process_birth": {
                        "tv_sec": tv_sec,
                        "tv_usec": tv_usec,
                    },
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )

    def test_console_controller_start_spawns_exact_guarded_command_without_launchctl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            binary, state_dir, marker, _, _ = self._console_fixture(root)
            marker.write_text("", encoding="utf-8")
            marker.chmod(0o600)
            state = web_panel_state.ControlState(repo_root=root)
            state._set_poll_boost = mock.Mock()
            proc = mock.Mock()
            proc.pid = 4242
            proc.poll.return_value = None

            with mock.patch.object(
                web_panel_state.subprocess,
                "Popen",
                return_value=proc,
            ) as popen_mock:
                with mock.patch.object(
                    state,
                    "_console_controller_process_identity",
                    return_value=(1_753_684_496, 123_456, 4242),
                ):
                    with mock.patch.object(
                        state,
                        "_console_controller_process_matches",
                        return_value=True,
                    ):
                        with mock.patch.object(
                            web_panel_state.subprocess,
                            "run",
                            side_effect=AssertionError("launchctl/ps must not run"),
                        ):
                            state.start()

            command = popen_mock.call_args.args[0]
            self.assertEqual(command[0], str(binary))
            self.assertIn("--enable-execution", command)
            self.assertIn("--allow-write", command)
            self.assertEqual(command[command.index("--max-fresh-jobs") + 1], "1")
            self.assertTrue(popen_mock.call_args.kwargs["start_new_session"])
            self.assertFalse(marker.exists())
            self.assertEqual(
                (root / "controller.pid").read_text(),
                self._console_pid_payload(4242),
            )
            self.assertEqual((root / "controller.pid").stat().st_mode & 0o777, 0o600)
            self.assertIn("시작", state.notice)

    def test_console_controller_start_rejects_exact_sha256_mismatch_without_spawning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            binary, _, _, _, _ = self._console_fixture(root)
            expected_sha256 = "a" * 64
            versions = binary.parent.parent
            versions.chmod(0o700)
            mismatch_dir = versions / expected_sha256
            mismatch_dir.mkdir(mode=0o700)
            binary = mismatch_dir / "lecture-stt-controller"
            binary.write_bytes(b"controller")
            binary.chmod(0o500)
            mismatch_dir.chmod(0o500)
            versions.chmod(0o500)

            mismatch_yaml = (
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "logging:\n"
                "  file: logs/app.log\n"
                "app:\n"
                "  execution_owner: controller\n"
                "  controller_label: com.geonha.lecture-stt-controller\n"
                f"  controller_binary: {binary}\n"
                f"  controller_binary_sha256: {expected_sha256}\n"
                f"  controller_kill_switch: {root / 'controller-state' / 'controller.disabled'}\n"
                "  controller_runtime: console\n"
                f"  controller_state_dir: {root / 'controller-state'}\n"
                f"  controller_pid_path: {root / 'controller.pid'}\n"
                f"  controller_stdout_path: {root / 'logs' / 'controller.out.jsonl'}\n"
                f"  controller_stderr_path: {root / 'logs' / 'controller.err.log'}\n"
            )
            (root / "config" / "config.yaml").write_text(mismatch_yaml, encoding="utf-8")
            state = web_panel_state.ControlState(repo_root=root)
            state._set_poll_boost = mock.Mock()

            with mock.patch.object(web_panel_state.subprocess, "Popen") as popen_mock:
                state.start()

            popen_mock.assert_not_called()
            self.assertIn("시작 실패", state.notice)
            self.assertIn("SHA-256", state.notice)
            self.assertFalse((root / "controller.pid").exists())

    def test_console_controller_start_rejects_invalid_pid_file_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            binary, _, marker, _, _ = self._console_fixture(root)
            pid_path = root / "controller.pid"
            pid_path.write_text("invalid-pid", encoding="ascii")
            pid_path.chmod(0o600)
            state = web_panel_state.ControlState(repo_root=root)
            state._set_poll_boost = mock.Mock()

            with mock.patch.object(web_panel_state.subprocess, "Popen") as popen_mock:
                state.start()

            popen_mock.assert_not_called()
            self.assertIn("시작 실패", state.notice)
            self.assertIn("PID file", state.notice)
            self.assertEqual(pid_path.read_text(), "invalid-pid")

    def test_console_controller_rejects_pid_payload_schema_tamper_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._console_fixture(root)
            pid_path = root / "controller.pid"
            tampered = json.loads(self._console_pid_payload(4242))
            tampered["unexpected"] = "field"
            pid_path.write_text(
                json.dumps(tampered, separators=(",", ":"), sort_keys=True) + "\n",
                encoding="ascii",
            )
            pid_path.chmod(0o600)
            state = web_panel_state.ControlState(repo_root=root)
            state._set_poll_boost = mock.Mock()

            with mock.patch.object(web_panel_state.subprocess, "Popen") as popen_mock:
                state.start()

            popen_mock.assert_not_called()
            self.assertIn("시작 실패", state.notice)
            self.assertIn("PID file", state.notice)
            self.assertTrue(pid_path.exists())

    def test_console_controller_stale_pid_file_is_removed_before_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            binary, _, marker, _, _ = self._console_fixture(root)
            marker.write_text("", encoding="utf-8")
            marker.chmod(0o600)
            pid_path = root / "controller.pid"
            pid_path.write_text(self._console_pid_payload(9999), encoding="ascii")
            pid_path.chmod(0o600)
            state = web_panel_state.ControlState(repo_root=root)
            proc = mock.Mock()
            proc.pid = 4242
            proc.poll.return_value = None
            state._set_poll_boost = mock.Mock()

            with mock.patch.object(
                web_panel_state.subprocess,
                "Popen",
                return_value=proc,
            ) as popen_mock:
                with mock.patch.object(
                    state,
                    "_console_controller_process_matches",
                    side_effect=[False, False, True],
                ):
                    with mock.patch.object(
                        state,
                        "_console_controller_process_identity",
                        return_value=(1_753_684_496, 654_321, 4242),
                    ):
                        state.start()

            popen_mock.assert_called_once()
            self.assertEqual(
                pid_path.read_text(),
                self._console_pid_payload(4242, tv_usec=654_321),
            )
            self.assertEqual(pid_path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(marker.exists())
            self.assertIn("시작", state.notice)
            self.assertEqual(popen_mock.call_args.args[0][0], str(binary))

    def test_console_controller_stop_fails_if_process_group_breaks_fencing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, state_dir, marker, _, _ = self._console_fixture(root)
            pid_path = root / "controller.pid"
            pid_path.write_text(self._console_pid_payload(4242), encoding="ascii")
            pid_path.chmod(0o600)
            state = web_panel_state.ControlState(repo_root=root)
            state._set_poll_boost = mock.Mock()

            with mock.patch.object(
                web_panel_state.os,
                "getpgid",
                return_value=9999,
            ):
                with mock.patch.object(
                    state,
                    "_console_controller_process_matches",
                    return_value=True,
                ):
                    with mock.patch.object(web_panel_state.os, "killpg") as killpg_mock:
                        with mock.patch.object(
                            web_panel_state.subprocess,
                            "run",
                            side_effect=AssertionError("launchctl must not run"),
                        ):
                            state.stop()

            self.assertIn("중지 실패", state.notice)
            self.assertIn("process group", state.notice)
            self.assertTrue(marker.exists())
            self.assertTrue(pid_path.exists())
            killpg_mock.assert_not_called()

    def test_console_controller_stop_fences_identity_and_signals_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, state_dir, marker, _, _ = self._console_fixture(root)
            pid_path = root / "controller.pid"
            pid_path.write_text(self._console_pid_payload(4242), encoding="ascii")
            pid_path.chmod(0o600)
            state = web_panel_state.ControlState(repo_root=root)
            state._set_poll_boost = mock.Mock()

            with mock.patch.object(
                state,
                "_console_controller_process_matches",
                return_value=True,
            ):
                with mock.patch.object(
                    state,
                    "_console_process_stopped",
                    return_value=True,
                ):
                    with mock.patch.object(web_panel_state.os, "getpgid", return_value=4242):
                        with mock.patch.object(web_panel_state.os, "killpg") as killpg_mock:
                            with mock.patch.object(
                                web_panel_state.subprocess,
                                "run",
                                side_effect=AssertionError("launchctl must not run"),
                            ):
                                state.stop()

            killpg_mock.assert_called_once_with(4242, web_panel_state.signal.SIGTERM)
            self.assertTrue(marker.exists())
            self.assertFalse(pid_path.exists())
            self.assertIn("완료", state.notice)

    def test_console_controller_rejects_symlink_binary_and_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            binary, state_dir, marker, _, _ = self._console_fixture(root)
            target = root / "real-controller"
            target.write_bytes(b"controller")
            target.chmod(0o500)
            binary.parent.parent.chmod(0o700)
            binary.parent.chmod(0o700)
            binary.unlink()
            binary.symlink_to(target)
            binary.parent.chmod(0o500)
            binary.parent.parent.chmod(0o500)
            state = web_panel_state.ControlState(repo_root=root)
            with mock.patch.object(web_panel_state.subprocess, "Popen") as popen_mock:
                state.start()
            popen_mock.assert_not_called()
            self.assertIn("binary 상태", state.notice)

            binary.parent.parent.chmod(0o700)
            binary.parent.chmod(0o700)
            binary.unlink()
            binary.write_bytes(b"controller")
            binary.chmod(0o500)
            binary.parent.chmod(0o500)
            binary.parent.parent.chmod(0o500)
            pid_target = state_dir / "pid-target"
            pid_target.write_text(self._console_pid_payload(4242), encoding="ascii")
            pid_target.chmod(0o600)
            (root / "controller.pid").symlink_to(pid_target)
            state.stop()
            self.assertTrue(marker.exists())
            self.assertIn("중지 실패", state.notice)

    def test_console_controller_rejects_pid_reuse_with_different_birth_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._console_fixture(root)
            pid_path = root / "controller.pid"
            pid_path.write_text(self._console_pid_payload(4242), encoding="ascii")
            pid_path.chmod(0o600)
            state = web_panel_state.ControlState(repo_root=root)
            settings = state._console_controller_settings()

            with mock.patch.object(web_panel_state.os, "kill", return_value=None):
                with mock.patch.object(
                    state,
                    "_console_controller_process_identity",
                    return_value=(1_753_684_496, 654_321, 4242),
                ):
                    self.assertIsNone(state._console_controller_pid(settings))

    def test_console_controller_rejects_adoption_when_process_is_not_group_leader(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._console_fixture(root)
            pid_path = root / "controller.pid"
            pid_path.write_text(self._console_pid_payload(4242), encoding="ascii")
            pid_path.chmod(0o600)
            state = web_panel_state.ControlState(repo_root=root)
            settings = state._console_controller_settings()

            with mock.patch.object(web_panel_state.os, "kill", return_value=None):
                with mock.patch.object(
                    state,
                    "_console_controller_process_identity",
                    return_value=(1_753_684_496, 123_456, 9999),
                ):
                    self.assertIsNone(state._console_controller_pid(settings))

    @unittest.skipUnless(sys.platform == "darwin", "Darwin proc_pidinfo contract")
    def test_console_controller_reads_kernel_birth_and_process_group_identity(self) -> None:
        identity = self._immutable_process_identity_for_current_test()
        self.assertIsNotNone(identity)
        assert identity is not None
        tv_sec, tv_usec, pgid = identity
        self.assertGreater(tv_sec, 0)
        self.assertGreaterEqual(tv_usec, 0)
        self.assertLess(tv_usec, 1_000_000)
        self.assertEqual(pgid, os.getpgid(os.getpid()))

    @staticmethod
    def _immutable_process_identity_for_current_test() -> tuple[int, int, int] | None:
        return web_panel_state.ControlState._console_controller_process_identity(
            os.getpid()
        )

    def test_console_controller_rejects_writable_binary_version_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            binary, _, _, _, _ = self._console_fixture(root)
            binary.parent.chmod(0o700)
            state = web_panel_state.ControlState(repo_root=root)
            state._set_poll_boost = mock.Mock()

            with mock.patch.object(web_panel_state.subprocess, "Popen") as popen_mock:
                state.start()

            popen_mock.assert_not_called()
            self.assertIn("시작 실패", state.notice)
            self.assertIn("version directory", state.notice)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin immutable flag contract")
    def test_console_controller_darwin_immutable_flag_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "controller"
            path.write_bytes(b"controller")
            path.chmod(0o500)
            self.assertFalse(self._immutable_path_check(path.lstat()))
            os.chflags(path, stat.UF_IMMUTABLE)
            try:
                self.assertTrue(self._immutable_path_check(path.lstat()))
                with self.assertRaises(PermissionError):
                    path.write_bytes(b"replacement")
            finally:
                os.chflags(path, 0)

    def test_legacy_start_still_uses_python_worker_popen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_config(root)
            state = web_panel_state.ControlState(repo_root=root)
            state._find_worker_pids = mock.Mock(return_value=[])
            state._set_poll_boost = mock.Mock()
            proc = mock.Mock()
            proc.poll.return_value = None

            with mock.patch.object(web_panel_state, "package_env", return_value={"TEST_ENV": "1"}):
                with mock.patch.object(
                    web_panel_state.subprocess,
                    "Popen",
                    return_value=proc,
                ) as popen_mock:
                    state.start()

            popen_mock.assert_called_once()
            self.assertIn("lecture_stt.stt.main", " ".join(popen_mock.call_args.args[0]))
            self.assertIn("실행중", state.notice)

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

    def test_db_counts_counts_known_statuses_and_ignores_unknown_ones(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            state_path = root / "state" / "jobs.sqlite3"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_dir = root / "00_inbox"
            state_dir.mkdir()
            (root / "01_audio").mkdir()
            (root / "02_transcripts").mkdir()
            (root / "99_errors").mkdir()
            (config_dir / "config.yaml").write_text(
                f"paths:\n  db_path: {state_path}\n  watch_folder: {state_dir}\n  stable_audio_folder: {root / '01_audio'}\n  transcript_folder: {root / '02_transcripts'}\n  error_folder: {root / '99_errors'}\nlogging:\n  file: logs/app.log\n",
                encoding="utf-8",
            )
            with sqlite3.connect(state_path) as conn:
                conn.execute(
                    """CREATE TABLE jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        status TEXT NOT NULL,
                        orig_name TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )"""
                )
                conn.executemany(
                    "INSERT INTO jobs (status, orig_name, created_at, updated_at) VALUES (?, ?, '2026-01-01', '2026-01-01')",
                    [
                        ("PENDING", "inbox-a.m4a"),
                        ("PROCESSING", "inbox-b.m4a"),
                        ("NEEDS_REVIEW", "inbox-c.m4a"),
                        ("DONE", "inbox-d.m4a"),
                        ("ERROR", "inbox-e.m4a"),
                        ("UNUSED", "inbox-f.m4a"),
                    ],
                )
            state = web_panel_state.ControlState(repo_root=root)

            (state.watch_folder / "unregistered_1.m4a").write_text("new", encoding="utf-8")
            (state.watch_folder / "inbox-b.m4a").write_text("old", encoding="utf-8")

            counts = state._db_counts()

            self.assertEqual(counts["PENDING"], 1)
            self.assertEqual(counts["PROCESSING"], 1)
            self.assertEqual(counts["NEEDS_REVIEW"], 1)
            self.assertEqual(counts["DONE"], 1)
            self.assertEqual(counts["ERROR"], 1)
            self.assertEqual(counts["UNREGISTERED"], 1)

    def test_clear_history_preserves_needs_review_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "jobs.sqlite3"
            log_path = root / "app.log"
            log_path.write_text("old log", encoding="utf-8")
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
                conn.executemany(
                    "INSERT INTO jobs (id, status) VALUES (?, ?)",
                    [
                        (1, "DONE"),
                        (2, "ERROR"),
                        (3, "NEEDS_REVIEW"),
                        (4, "PROCESSING"),
                    ],
                )

            state = object.__new__(web_panel_state.ControlState)
            state.db_path = db_path
            state.log_path = log_path
            state.notice = ""
            state._set_poll_boost = mock.Mock()

            state.clear_history()

            with sqlite3.connect(db_path) as conn:
                statuses = [
                    row[0]
                    for row in conn.execute("SELECT status FROM jobs ORDER BY id").fetchall()
                ]
            self.assertEqual(statuses, ["NEEDS_REVIEW", "PROCESSING"])
            self.assertEqual(log_path.read_text(encoding="utf-8"), "")
            self.assertIn("확인 필요 항목은 보존", state.notice)

    def test_transcript_count_ignores_quality_scorecard_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript_dir = Path(temp_dir)
            (transcript_dir / "260519OOP_2.txt").write_text("transcript", encoding="utf-8")
            (transcript_dir / "260519OOP_2.json").write_text("{}", encoding="utf-8")
            (transcript_dir / "260519OOP_2.quality.json").write_text("{}", encoding="utf-8")
            (transcript_dir / "orphan.quality.json").write_text("{}", encoding="utf-8")

            self.assertEqual(web_panel_state.ControlState._count_transcript_sets(transcript_dir), "1")
