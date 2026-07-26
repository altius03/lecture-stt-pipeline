from __future__ import annotations

import os
import sqlite3
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
