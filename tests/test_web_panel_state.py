from __future__ import annotations

import hashlib
import fcntl
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
from lecture_stt.shared import db  # noqa: E402


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

        state._notification_availability.assert_called_once_with()
        self.assertEqual(snapshot["schema_version"], 2)
        self.assertEqual(snapshot["runtime_state"]["status"], "running")
        self.assertEqual(snapshot["runtime_state"]["source"], "web")
        self.assertEqual(snapshot["notification"]["selection"], "telegram")
        self.assertFalse(snapshot["notification"]["restart_required"])
        self.assertFalse(snapshot["notification"]["can_apply_now"])
        self.assertEqual(snapshot["actions"]["pause_action"], "pause")
        self.assertEqual(snapshot["actions"]["endpoints"]["state"], "/api/state")
        self.assertEqual(snapshot["actions"]["endpoints"]["notification"], "/api/notification")
        self.assertEqual(snapshot["jobs_v2"][0]["id"], 7)
        self.assertEqual(snapshot["jobs_v2"][0]["file_name"], "lecture.m4a")
        self.assertEqual(snapshot["jobs_v2"][0]["stages"]["audio"], "done")
        self.assertEqual(snapshot["jobs_v2"][0]["stages"]["transcript"], "processing")
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

    def test_legacy_discord_config_is_not_exposed_as_a_selection(self) -> None:
        state = self._make_state()
        state.config_data = {
            "notification": {
                "provider": "discord",
                "enabled": True,
                "dual_send_providers": ["telegram"],
            }
        }
        state._notification_availability = mock.Mock(return_value={"telegram": True})
        notification = state.snapshot(include_log=False)["notification"]
        self.assertEqual(notification["selection"], "disabled")
        self.assertEqual([option["id"] for option in notification["options"]], ["telegram", "disabled"])

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
                state.update_notification_selection("telegram")

            saved = state._load_config()
            self.assertEqual(saved["notification"]["provider"], "telegram")
            self.assertNotIn("dual_send_providers", saved["notification"])
            self.assertTrue(saved["notification"]["enabled"])
            self.assertTrue(saved["notification"]["send_review"])
            self.assertIn("다음 작업부터 적용", state.notice)
            self.assertEqual([option["id"] for option in state._notification_options({"telegram": True})], ["telegram", "disabled"])
            with self.assertRaises(RuntimeError):
                state.update_notification_selection("both")
            with self.assertRaises(RuntimeError):
                state.update_notification_selection("discord")

    def test_save_config_keeps_original_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text("notification:\n  provider: telegram\n", encoding="utf-8")
            config_path.chmod(0o600)
            state = object.__new__(web_panel_state.ControlState)
            state.config_path = config_path
            state.config_data = {"notification": {"provider": "telegram"}}

            with mock.patch.object(web_panel_state.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    state._save_config({"notification": {"provider": "discord"}})

            self.assertEqual(config_path.read_text(encoding="utf-8"), "notification:\n  provider: telegram\n")
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            self.assertEqual(state.config_data["notification"]["provider"], "telegram")
            self.assertEqual(list(config_path.parent.iterdir()), [config_path])

    def test_save_config_reports_directory_sync_failure_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text("notification:\n  provider: telegram\n", encoding="utf-8")
            state = object.__new__(web_panel_state.ControlState)
            state.config_path = config_path
            state.config_data = {"notification": {"provider": "telegram"}}

            with mock.patch.object(
                web_panel_state.os, "fsync", side_effect=[None, OSError("directory sync failed")]
            ):
                synced = state._save_config({"notification": {"provider": "discord"}})

            self.assertFalse(synced)
            self.assertIn("discord", config_path.read_text(encoding="utf-8"))
            self.assertEqual(state.config_data["notification"]["provider"], "discord")

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
            self.assertEqual(
                payload["capabilities"],
                {"transcript_preview": False},
            )
            self.assertFalse((root / "state" / "storage-v2.sqlite3").exists())
            with self.assertRaises(
                web_panel_state.RecordingLibraryDisabledError
            ):
                state.recording_library_detail("recording_a")
            with self.assertRaises(
                web_panel_state.RecordingLibraryDisabledError
            ):
                state.recording_library_transcript_preview("recording_a")

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
                "  records_root: private/records\n"
                "  library:\n"
                "    enabled: true\n"
                "    transcript_preview_enabled: true\n"
                "    transcript_preview_max_bytes: 4096\n"
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
            with mock.patch.object(
                web_panel_state,
                "read_recording_transcript_preview",
                return_value={"recording": {"storage_key": "recording_a"}},
            ) as read_preview:
                preview_payload = state.recording_library_transcript_preview(
                    "recording_a"
                )

            self.assertTrue(list_payload["available"])
            self.assertEqual(
                list_payload["capabilities"],
                {"transcript_preview": True},
            )
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
            self.assertEqual(
                preview_payload["recording"]["storage_key"],
                "recording_a",
            )
            read_preview.assert_called_once_with(
                root / "state" / "storage-v2.sqlite3",
                root / "private" / "records",
                "recording_a",
                max_bytes=4096,
            )

    def test_enabled_recording_library_list_defaults_preview_capability_to_false(
        self,
    ) -> None:
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
            ):
                payload = state.recording_library_list()

            self.assertEqual(
                payload["capabilities"],
                {"transcript_preview": False},
            )

    def test_recording_library_preview_requires_records_root_and_strict_int_max_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            config_path = config_dir / "config.yaml"
            config_path.write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  db_path: state/storage-v2.sqlite3\n"
                "  library:\n"
                "    enabled: true\n"
                "    transcript_preview_enabled: true\n"
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            missing_root_state = web_panel_state.ControlState(repo_root=root)

            with self.assertRaisesRegex(RuntimeError, "records_root"):
                missing_root_state.recording_library_transcript_preview(
                    "recording_a"
                )

            config_path.write_text(
                "paths:\n"
                "  db_path: state/jobs.sqlite3\n"
                "storage_v2:\n"
                "  db_path: state/storage-v2.sqlite3\n"
                "  records_root: private/records\n"
                "  library:\n"
                "    enabled: true\n"
                "    transcript_preview_enabled: true\n"
                '    transcript_preview_max_bytes: "4096"\n'
                "logging:\n"
                "  file: logs/app.log\n",
                encoding="utf-8",
            )
            invalid_max_state = web_panel_state.ControlState(repo_root=root)

            with self.assertRaisesRegex(
                RuntimeError,
                "transcript_preview_max_bytes must be an integer",
            ):
                invalid_max_state.recording_library_transcript_preview(
                    "recording_a"
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
            self.assertEqual(run_mock.call_args_list[1].args[0], ["launchctl", "kickstart", state._controller_service_target()])

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

    def _supervision_fixture(self, root):
        _, state_dir, marker, _, _ = self._console_fixture(root)
        cfg = root / "config/config.yaml"
        cfg.write_text(cfg.read_text().replace("app:\n", "app:\n  console_controller_autostart: true\n", 1))
        state = web_panel_state.ControlState(root)
        state._run_control_cmd = mock.Mock(return_value=self._completed_process(
            ["launchctl", "print"], returncode=113, stderr=f'Could not find service "{state._controller_label()}"'
        ))
        state._find_worker_pids = mock.Mock(return_value=[])
        return state, state_dir, marker

    def test_console_supervision_requires_explicit_darwin_console_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, _, _ = self._supervision_fixture(Path(tmp))
            self.assertTrue(state.console_supervision_enabled())
            app = state.config_data["app"]
            for value in (None, False, 1, "true", "on"):
                with self.subTest(value=value), mock.patch.dict(app, console_controller_autostart=value):
                    self.assertFalse(state.console_supervision_enabled())
            for change in ({"controller_runtime": "launchd"}, {"execution_owner": "python"}):
                with mock.patch.dict(app, change):
                    self.assertFalse(state.console_supervision_enabled())
            with mock.patch.object(state, "_console_runtime_supported", return_value=False):
                self.assertFalse(state.console_supervision_enabled())

    def test_automatic_start_preserves_pause_and_stop_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, _, marker = self._supervision_fixture(Path(tmp))
            event = threading.Event()
            with mock.patch.object(web_panel_state.subprocess, "Popen") as spawn:
                marker.write_text("operator hold")
                for mode in (0o600, 0o644):
                    marker.chmod(mode)
                    state._start_console_controller(stopping=event)
                    self.assertEqual(marker.read_text(), "operator hold")
                marker.unlink()
                state.pause_flag.symlink_to(Path(tmp) / "absent")
                state._start_console_controller(stopping=event)
                self.assertTrue(state.pause_flag.is_symlink())
                state.pause_flag.unlink()
                state._set_paused(True)
                state._start_console_controller(stopping=event)
                self.assertTrue(state._is_paused())
                spawn.assert_not_called()
                state._run_control_cmd.assert_not_called()

    def test_automatic_start_rejects_competing_or_unknown_owners(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, state_dir, marker = self._supervision_fixture(Path(tmp))
            event = threading.Event()
            with mock.patch.object(web_panel_state.subprocess, "Popen") as spawn:
                absent = state._run_control_cmd.return_value
                state._run_control_cmd.return_value = self._completed_process([], returncode=0)
                state._start_console_controller(stopping=event)
                state._run_control_cmd.return_value = self._completed_process([], returncode=1)
                with self.assertRaises(RuntimeError):
                    state._start_console_controller(stopping=event)
                state._run_control_cmd.return_value = absent
                state._find_worker_pids.return_value = [42]
                state._start_console_controller(stopping=event)
                state._find_worker_pids.return_value = []
                lock = state_dir / "controller.lock"
                lock.touch(mode=0o600)
                with lock.open() as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    state._start_console_controller(stopping=event)
                pid = Path(tmp) / "controller.pid"
                pid.write_text("incomplete")
                pid.chmod(0o600)
                with self.assertRaises(RuntimeError):
                    state._start_console_controller(stopping=event)
                self.assertEqual(pid.read_text(), "incomplete")
                self.assertFalse(marker.exists())
                spawn.assert_not_called()

    def test_automatic_adoption_and_restart_after_a_verified_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, _, marker = self._supervision_fixture(Path(tmp))
            event = threading.Event()
            pid = Path(tmp) / "controller.pid"
            pid.write_text(self._console_pid_payload(4242))
            pid.chmod(0o600)
            proc = mock.Mock(pid=4243)
            proc.poll.return_value = None
            birth = (1753684496, 123456)
            with mock.patch.object(web_panel_state.subprocess, "Popen", return_value=proc) as spawn, \
                 mock.patch.object(state, "_console_controller_process_matches", return_value=True) as matches, \
                 mock.patch.object(state, "_console_controller_process_identity", return_value=(*birth, 4242)) as kernel, \
                 mock.patch.object(state, "_set_paused", side_effect=AssertionError("must preserve manual pause")):
                state._start_console_controller(stopping=event)
                state._start_console_controller(stopping=event)
                spawn.assert_not_called()
                self.assertEqual(matches.call_count, 1)
                kernel.side_effect = [None, (*birth, 4243)]
                matches.side_effect = [False, False, True]
                with mock.patch.object(web_panel_state.os, "kill", side_effect=ProcessLookupError):
                    state._start_console_controller(stopping=event)
                state._start_console_controller(stopping=event)
                spawn.assert_called_once()
                self.assertEqual(json.loads(pid.read_text())["pid"], 4243)
                self.assertFalse(marker.exists())

    def test_start_race_leaves_replaced_stop_marker_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, _, marker = self._supervision_fixture(Path(tmp))
            def changed(*_args, **_kwargs):
                replacement = marker.with_suffix(".new")
                replacement.write_text("manual")
                replacement.chmod(0o600)
                replacement.replace(marker)
                raise OSError("injected launch failure")
            with mock.patch.object(web_panel_state.subprocess, "Popen", side_effect=changed) as spawn:
                with self.assertRaises(OSError):
                    state._start_console_controller(stopping=threading.Event())
                state._start_console_controller(stopping=threading.Event())
                spawn.assert_called_once()
                self.assertEqual(marker.read_text(), "manual")

    def test_transient_spawn_failure_waits_before_retry_and_removes_only_own_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, _, marker = self._supervision_fixture(Path(tmp))
            stop = mock.Mock()
            stop.is_set.return_value = False
            stop.wait.side_effect = [False, True]
            with mock.patch.object(web_panel_state.subprocess, "Popen", side_effect=OSError("offline")) as spawn:
                state.supervise_console_controller(stop)
                self.assertEqual(spawn.call_count, 2)
                self.assertEqual(stop.wait.call_args_list, [mock.call(30.0), mock.call(30.0)])
                self.assertFalse(marker.exists())
                self.assertFalse((Path(tmp) / "controller.pid").exists())
                self.assertIn("보류", state.notice)

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

    def test_external_worker_identity_filters_other_config_and_reused_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_config(root)
            state = web_panel_state.ControlState(root)
            def worker(pid, config):
                proc = mock.Mock(pid=pid, info={"name": "Python"})
                proc.uids.return_value.effective = os.geteuid()
                proc.cwd.return_value = str(root)
                proc.cmdline.return_value = ["python", "-m", state.worker_module, "--config", str(config)]
                proc.is_running.return_value = True
                return proc
            own = worker(21, state.config_path)
            foreign = worker(22, root / "foreign.yaml")
            reused = worker(23, state.config_path)
            reused.is_running.side_effect = [True, False]
            other_checkout = worker(24, state.config_path)
            other_checkout.cwd.return_value = str(root / "other")
            ambiguous = worker(25, state.config_path)
            ambiguous.cmdline.return_value[3] = "--conf"
            with mock.patch.object(web_panel_state.psutil, "process_iter",
                                   return_value=[own, foreign, reused, other_checkout, ambiguous]):
                status, _ = state.stop()
            self.assertEqual(status, 200)
            own.terminate.assert_called_once()
            foreign.terminate.assert_not_called()
            reused.terminate.assert_not_called()
            other_checkout.terminate.assert_not_called()
            ambiguous.terminate.assert_not_called()

    def test_folder_counts_share_cached_walk_between_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_config(root)
            state = web_panel_state.ControlState(root)
            with mock.patch.object(state, "_count_transcript_sets", return_value="2") as walk:
                self.assertEqual(state._folder_counts(), state._folder_counts())
            walk.assert_called_once()

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
                conn.execute(
                    "CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT NOT NULL, "
                    "deduped_from_job_id INTEGER)"
                )
                conn.execute("CREATE TABLE transcript_postprocess_jobs (source_job_id INTEGER PRIMARY KEY)")
                conn.executemany(
                    "INSERT INTO jobs (id, status) VALUES (?, ?)",
                    [
                        (1, "DONE"),
                        (2, "ERROR"),
                        (3, "NEEDS_REVIEW"),
                        (4, "PROCESSING"),
                        (5, "DONE"),
                        (6, "DONE"),
                        (7, "DONE"),
                    ],
                )
                conn.execute("INSERT INTO transcript_postprocess_jobs VALUES (1)")
                conn.execute("UPDATE jobs SET deduped_from_job_id = 5 WHERE id = 3")
                conn.execute("UPDATE jobs SET deduped_from_job_id = 7 WHERE id = 5")
                conn.execute("UPDATE jobs SET deduped_from_job_id = 2 WHERE id = 6")

            state = object.__new__(web_panel_state.ControlState)
            state.db_path = db_path
            state.log_path = log_path
            state.notice = ""
            state.repo_root = root
            state.config_path = root / "config.yaml"
            state.config_path.write_text(f"paths:\n  db_path: {db_path}\n")
            state.config_data = {"transcript_delivery": {"enabled": False}}
            state._set_poll_boost = mock.Mock()

            self.assertEqual(state.history_plan()["count"], 1)
            self.assertEqual(state.clear_history("0" * 64)[0], 409)
            self.assertEqual(state.clear_history(state.history_plan()["digest"])[0], 200)

            with sqlite3.connect(db_path) as conn:
                jobs = conn.execute("SELECT id, status FROM jobs ORDER BY id").fetchall()
                queued = conn.execute("SELECT source_job_id FROM transcript_postprocess_jobs").fetchall()
            self.assertEqual(jobs, [(1, "DONE"), (2, "ERROR"), (3, "NEEDS_REVIEW"), (4, "PROCESSING"), (5, "DONE"), (7, "DONE")])
            self.assertEqual(queued, [(1,)])
            self.assertEqual(log_path.read_text(encoding="utf-8"), "old log")
            self.assertIn("미참조 완료 이력 1건", state.notice)

    def test_clear_history_preserves_unqueued_done_when_delivery_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "jobs.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
                conn.executemany(
                    "INSERT INTO jobs (id, status) VALUES (?, ?)",
                    [(1, "DONE"), (2, "ERROR")],
                )

            state = object.__new__(web_panel_state.ControlState)
            state.db_path = db_path
            state.log_path = Path(temp_dir) / "missing.log"
            state.repo_root = Path(temp_dir)
            state.config_path = state.repo_root / "config.yaml"
            state.config_path.write_text(f"paths:\n  db_path: {db_path}\ntranscript_delivery:\n  enabled: true\n")
            state.config_data = {"transcript_delivery": {"enabled": False}}
            state.notice = ""
            state._set_poll_boost = mock.Mock()

            self.assertEqual(state.clear_history(state.history_plan()["digest"])[0], 200)

            with sqlite3.connect(db_path) as conn:
                self.assertEqual(
                    conn.execute("SELECT id, status FROM jobs ORDER BY id").fetchall(),
                    [(1, "DONE"), (2, "ERROR")],
                )
            self.assertIn("0건", state.notice)

    def test_clear_history_reports_db_failure_without_truncating_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "app.log"
            log_path.write_text("keep log", encoding="utf-8")
            state = object.__new__(web_panel_state.ControlState)
            state.db_path = root / "jobs.sqlite3"
            state.log_path = log_path
            state.repo_root = root
            state.config_path = root / "config.yaml"
            state.config_data = {}
            state.notice = ""

            self.assertEqual(state.clear_history("0" * 64)[0], 500)
            self.assertIn("이력 정리 실패", state.notice)
            self.assertEqual(log_path.read_text(encoding="utf-8"), "keep log")

    def test_history_plan_and_apply_use_the_default_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config/config.yaml"
            config.parent.mkdir()
            config.write_text("paths: {watch_folder: inbox}\n", encoding="utf-8")
            (root / "state").mkdir()
            state = web_panel_state.ControlState(root)
            self.assertEqual(state.db_path, root / "state/jobs.sqlite3")
            with sqlite3.connect(state.db_path) as conn:
                conn.execute(
                    "INSERT INTO jobs (status, created_at, updated_at, orig_inbox_path, orig_name,"
                    " canonical_base, canonical_audio_path) VALUES "
                    "('DONE','2026-01-01','2026-01-01','inbox/a.wav','a.wav','a','audio/a.wav')"
                )
            plan = state.history_plan()
            self.assertEqual(plan["count"], 1)

            for changed in ("paths: {db_path: state/other.sqlite3}", "paths: {db_path: null}", ""):
                with self.subTest(changed=changed):
                    config.write_text(changed, encoding="utf-8")
                    self.assertEqual(state.clear_history(plan["digest"])[0], 500)
            config.write_text("paths: {}\n", encoding="utf-8")
            self.assertEqual(state.clear_history(plan["digest"])[0], 200)
            self.assertEqual(state.history_plan()["count"], 0)

    def test_history_apply_rereads_current_delivery_policy_and_refuses_unreadable_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(root)
            state = web_panel_state.ControlState(root)
            db_path = root / "history.sqlite3"
            state.db_path = db_path
            config = f"paths:\n  db_path: {db_path}\ntranscript_delivery:\n  enabled: "
            state.config_path.write_text(config + "'off'\n")
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY,status TEXT)")
                conn.execute("INSERT INTO jobs VALUES(7,'DONE')")
            plan = state.history_plan()
            self.assertEqual(plan["count"], 1)
            for enabled in ("'true'", "1", "'ON'", "'y'"):
                state.config_path.write_text(config + enabled)
                self.assertEqual(state.history_plan()["count"], 0)
                self.assertEqual(state.clear_history(plan["digest"])[0], 409)
            for invalid in (config + "maybe", config + "[", "", "paths: []"):
                state.config_path.write_text(invalid)
                with self.assertRaises(ValueError):
                    state.history_plan()
                self.assertEqual(state.clear_history(plan["digest"])[0], 500)
            state.config_path.unlink()
            self.assertEqual(state.clear_history(plan["digest"])[0], 500)
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("SELECT id FROM jobs").fetchone()[0], 7)

    def test_transcript_count_ignores_quality_scorecard_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript_dir = Path(temp_dir)
            (transcript_dir / "260519OOP_2.txt").write_text("transcript", encoding="utf-8")
            (transcript_dir / "260519OOP_2.json").write_text("{}", encoding="utf-8")
            (transcript_dir / "260519OOP_2.quality.json").write_text("{}", encoding="utf-8")
            (transcript_dir / "orphan.quality.json").write_text("{}", encoding="utf-8")

            self.assertEqual(web_panel_state.ControlState._count_transcript_sets(transcript_dir), "1")

    def test_transcript_count_includes_nested_course_sets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript_dir = Path(temp_dir)
            for course in ("01_computer_architecture", "02_computer_network"):
                course_dir = transcript_dir / "2026-2" / course
                course_dir.mkdir(parents=True)
                (course_dir / "same_stem.txt").write_text("transcript", encoding="utf-8")
                (course_dir / "same_stem.json").write_text("{}", encoding="utf-8")
                (course_dir / "same_stem.quality.json").write_text("{}", encoding="utf-8")

            for name in (".recovered-job-3", ".quality-recovery-temp/pair"):
                recovery = course_dir / name
                recovery.mkdir(parents=True)
                (recovery / "same_stem.txt").write_text("review copy")
                (recovery / "same_stem.json").write_text("{}")

            self.assertEqual(
                web_panel_state.ControlState._count_transcript_sets(transcript_dir),
                "2",
            )


class WebPanelJobApiStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.transcripts = root / "transcripts"
        self.transcripts.mkdir()
        self.db_path = root / "jobs.sqlite3"
        db.init_db(str(self.db_path)).close()
        self.state = object.__new__(web_panel_state.ControlState)
        self.state.db_path = self.db_path
        self.state.transcript_folder = self.transcripts
        self.state.config_data = {
            "storage_v2": {"library": {"transcript_preview_enabled": True}},
            "transcript_delivery": {"enabled": False},
        }
        self.state.repo_root = root
        self.state.config_path = root / "config.yaml"
        self.state.config_data["paths"] = {"db_path": str(self.db_path)}
        self.state.config_path.write_text(json.dumps(self.state.config_data), encoding="utf-8")
        self.state.notice = ""
        self.state._set_poll_boost = mock.Mock()

    def _job(self, *, status: str = "DONE", quality: dict | None = None, path: Path | None = None) -> int:
        path = path or self.transcripts / "lecture.txt"
        with db.connect_db(str(self.db_path)) as conn:
            return db.create_job(
                conn,
                status=status,
                orig_inbox_path=str(path),
                orig_name="lecture.m4a",
                canonical_base="lecture",
                canonical_audio_path=str(self.transcripts / "lecture.m4a"),
                transcript_txt_path=str(path),
                transcript_json_path=str(path.with_suffix(".json")),
                engine_params={"quality": quality or {}},
            )

    @staticmethod
    def _soft_bad_quality() -> dict:
        return {
            "health": "bad",
            "summary": "낮은 음량 구간",
            "nonspace_chars": 100,
            "nonempty_segments": 2,
            "audio_duration_sec": 100,
            "coverage_ratio": 0.8,
        }

    def test_preview_requires_opt_in_done_and_safe_bounded_text(self) -> None:
        transcript = self.transcripts / "lecture.txt"
        transcript.write_text("전사 본문\n", encoding="utf-8")
        job_id = self._job(path=transcript)
        self.assertEqual(
            self.state.transcript_preview(job_id),
            {"job_id": job_id, "text": "전사 본문\n", "truncated": False},
        )

        self.state.config_data["storage_v2"]["library"]["transcript_preview_enabled"] = False
        with self.assertRaises(web_panel_state.JobPreviewDisabledError):
            self.state.transcript_preview(job_id)
        self.state.config_data["storage_v2"]["library"]["transcript_preview_enabled"] = True
        with self.assertRaises(web_panel_state.JobPreviewNotFoundError):
            self.state.transcript_preview(self._job(status="PROCESSING", path=transcript))

        unsafe_paths = []
        outside = Path(self.temp_dir.name) / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        unsafe_paths.append(outside)
        link = self.transcripts / "linked.txt"
        link.symlink_to(outside)
        unsafe_paths.append(link)
        hardlink = self.transcripts / "hardlinked.txt"
        os.link(transcript, hardlink)
        unsafe_paths.append(hardlink)
        too_large = self.transcripts / "large.txt"
        too_large.write_bytes(b"a" * (2 * 1024 * 1024 + 1))
        unsafe_paths.append(too_large)
        invalid = self.transcripts / "invalid.txt"
        invalid.write_bytes(b"\xff")
        unsafe_paths.append(invalid)
        nul = self.transcripts / "nul.txt"
        nul.write_bytes(b"hello\x00world")
        unsafe_paths.append(nul)
        for path in unsafe_paths:
            with self.subTest(path=path.name):
                with db.connect_db(str(self.db_path)) as conn:
                    db.update_job(conn, job_id, transcript_txt_path=str(path))
                with self.assertRaises(web_panel_state.JobPreviewConflictError):
                    self.state.transcript_preview(job_id)

    def test_preview_respects_configured_max_bytes(self) -> None:
        transcript = self.transcripts / "lecture.txt"
        transcript.write_text("hello", encoding="utf-8")
        job_id = self._job(path=transcript)
        library = self.state.config_data["storage_v2"]["library"]
        library["transcript_preview_max_bytes"] = 4
        with self.assertRaises(web_panel_state.JobPreviewConflictError):
            self.state.transcript_preview(job_id)
        library["transcript_preview_max_bytes"] = 5
        self.assertEqual(self.state.transcript_preview(job_id)["text"], "hello")
        for invalid in (True, 0, web_panel_state.MAX_TRANSCRIPT_PREVIEW_MAX_BYTES + 1):
            with self.subTest(invalid=invalid):
                library["transcript_preview_max_bytes"] = invalid
                with self.assertRaises(ValueError):
                    self.state.transcript_preview(job_id)

    def test_quality_review_is_open_after_completion_and_decision_is_idempotent(self) -> None:
        job_id = self._job(quality=self._soft_bad_quality())
        open_jobs = self.state.quality_review_jobs(limit=10)
        self.assertEqual(open_jobs["total"], 1)
        self.assertEqual(open_jobs["items"][0]["id"], job_id)
        self.assertEqual(open_jobs["items"][0]["stages"]["correction"], "skipped")
        self.assertTrue(open_jobs["items"][0]["quality_review"]["required"])

        first = self.state.record_quality_review_decision(job_id, "no_issue")
        self.assertEqual(first, self.state.record_quality_review_decision(job_id, "no_issue"))
        self.assertEqual(self.state.quality_review_jobs()["total"], 0)
        with self.assertRaises(web_panel_state.QualityReviewConflictError):
            self.state.record_quality_review_decision(job_id, "needs_correction")
        with self.assertRaises(ValueError):
            self.state.record_quality_review_decision(job_id, "invalid")

        hard_quality = {**self._soft_bad_quality(), "nonspace_chars": 0}
        hard_id = self._job(quality=hard_quality)
        with self.assertRaises(web_panel_state.QualityReviewConflictError):
            self.state.record_quality_review_decision(hard_id, "no_issue")
        self.assertEqual(self.state.quality_review_jobs()["total"], 0)

    def test_needs_correction_stays_open_until_no_issue_transition(self) -> None:
        self.state.log_path = Path(self.temp_dir.name) / "missing.log"
        job_id = self._job(quality=self._soft_bad_quality())
        for index in range(25):
            self._job(
                quality={"health": "good"},
                path=self.transcripts / f"ordinary-{index}.txt",
            )

        first = self.state.record_quality_review_decision(job_id, "needs_correction")
        self.assertTrue(first["required"])
        self.assertEqual(first, self.state.record_quality_review_decision(job_id, "needs_correction"))
        followups = self.state.quality_review_jobs(limit=10)
        self.assertEqual(followups["total"], 1)
        self.assertEqual(followups["items"][0]["id"], job_id)
        self.assertEqual(followups["items"][0]["quality_review"]["decision"], "needs_correction")
        self.assertTrue(followups["items"][0]["quality_review"]["required"])

        self.assertEqual(self.state.clear_history(self.state.history_plan()["digest"])[0], 200)
        with db.connect_db(str(self.db_path)) as conn:
            self.assertEqual([row[0] for row in conn.execute("SELECT id FROM jobs")], [job_id])
        closed = self.state.record_quality_review_decision(job_id, "no_issue")
        self.assertFalse(closed["required"])
        self.assertEqual(self.state.record_quality_review_decision(job_id, "no_issue"), closed)
        self.assertEqual(self.state.quality_review_jobs()["total"], 0)
        with self.assertRaises(web_panel_state.QualityReviewConflictError):
            self.state.record_quality_review_decision(job_id, "needs_correction")
        self.assertEqual(self.state.clear_history(self.state.history_plan()["digest"])[0], 200)
        with db.connect_db(str(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_quality_review_list_includes_old_blocking_job_without_soft_count(self) -> None:
        soft_id = self._job(quality=self._soft_bad_quality())
        hard_id = self._job(
            status="NEEDS_REVIEW",
            quality={**self._soft_bad_quality(), "nonspace_chars": 0},
            path=self.transcripts / "blocked.txt",
        )
        for index in range(25):
            self._job(
                quality={"health": "good"},
                path=self.transcripts / f"ordinary-{index}.txt",
            )

        self.assertEqual(len(self.state._open_quality_review_jobs()), 1)
        first_page = self.state.quality_review_jobs(limit=1)
        second_page = self.state.quality_review_jobs(limit=1, offset=1)
        self.assertEqual(first_page["total"], 2)
        self.assertEqual(first_page["items"][0]["id"], hard_id)
        self.assertEqual(first_page["items"][0]["status"], "NEEDS_REVIEW")
        self.assertTrue(first_page["items"][0]["quality_review"]["blocking"])
        self.assertFalse(first_page["items"][0]["quality_review"]["required"])
        self.assertEqual(second_page["items"][0]["id"], soft_id)
        with self.assertRaises(web_panel_state.QualityReviewConflictError):
            self.state.record_quality_review_decision(hard_id, "no_issue")

    def test_quality_review_waits_for_enabled_postprocess_terminal_state(self) -> None:
        self.state.config_data["transcript_delivery"]["enabled"] = "true"
        self.state.config_path.write_text(json.dumps(self.state.config_data))
        job_id = self._job(quality=self._soft_bad_quality())
        self.assertEqual(self.state.quality_review_jobs()["total"], 0)
        with self.assertRaises(web_panel_state.QualityReviewConflictError):
            self.state.record_quality_review_decision(job_id, "no_issue")
        with db.connect_db(str(self.db_path)) as conn:
            db.insert_transcript_postprocess_job_if_absent(
                conn, job_id,
                logical_stem="lecture", activation_cutoff="2026-09-28T00:00:00+09:00",
                route_method="test", status="PENDING",
                source_txt_path="/tmp/source.txt", source_txt_sha256="0" * 64,
                source_json_path="/tmp/source.json", source_json_sha256="0" * 64,
                generator_backend="test",
            )
        self.assertEqual(self.state.quality_review_jobs()["total"], 0)
        self.state.config_data["transcript_delivery"]["enabled"] = False
        self.state.config_path.write_text(json.dumps(self.state.config_data))
        self.assertEqual(self.state.quality_review_jobs()["total"], 0)
        with self.assertRaises(web_panel_state.QualityReviewConflictError):
            self.state.record_quality_review_decision(job_id, "no_issue")
        self.state.config_data["transcript_delivery"]["enabled"] = "true"
        self.state.config_path.write_text(json.dumps(self.state.config_data))
        with db.connect_db(str(self.db_path)) as conn:
            db.update_transcript_postprocess_job(
                conn, job_id, status="DELIVERED", correction_status="READY", summary_status="READY"
            )
        open_jobs = self.state.quality_review_jobs()
        self.assertEqual(open_jobs["total"], 1)
        self.assertEqual(open_jobs["items"][0]["stages"]["summary"], "done")
        self.assertEqual(self.state.record_quality_review_decision(job_id, "needs_correction")["decision"], "needs_correction")

    def test_quality_decision_rechecks_policy_changed_after_preview(self) -> None:
        job_id = self._job(quality=self._soft_bad_quality())
        self.assertEqual(self.state.quality_review_jobs()["total"], 1)
        saved = json.loads(self.state.config_path.read_text())
        saved["transcript_delivery"] = {"enabled": "on"}
        self.state.config_path.write_text(json.dumps(saved))
        with self.assertRaises(web_panel_state.QualityReviewConflictError):
            self.state.record_quality_review_decision(job_id, "no_issue")
        with db.connect_db(str(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quality_review_decisions").fetchone()[0], 0)

    def test_clear_history_preserves_open_quality_review_until_decision(self) -> None:
        self.state.log_path = Path(self.temp_dir.name) / "missing.log"
        open_id = self._job(quality=self._soft_bad_quality())
        ordinary_id = self._job(quality={"health": "good"}, path=self.transcripts / "ordinary.txt")
        hard_id = self._job(
            quality={**self._soft_bad_quality(), "nonspace_chars": 0},
            path=self.transcripts / "hard.txt",
        )

        self.assertEqual(self.state.clear_history(self.state.history_plan()["digest"])[0], 200)
        with db.connect_db(str(self.db_path)) as conn:
            kept = {row[0] for row in conn.execute("SELECT id FROM jobs")}
        self.assertEqual(kept, {open_id})
        self.assertNotIn(ordinary_id, kept)
        self.assertNotIn(hard_id, kept)

        self.state.record_quality_review_decision(open_id, "no_issue")
        self.assertEqual(self.state.clear_history(self.state.history_plan()["digest"])[0], 200)
        with db.connect_db(str(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM quality_review_decisions").fetchone()[0], 0
            )
