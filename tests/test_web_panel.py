from __future__ import annotations

import io
import json
import unittest
from unittest import mock

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.ui import web_panel  # noqa: E402
from lecture_stt.storage_v2.archive_review import (  # noqa: E402
    ArchiveReviewConflictError,
    ArchiveReviewNotFoundError,
    ArchiveReviewWriteDisabledError,
)
from lecture_stt.storage_v2.library import (  # noqa: E402
    RecordingLibraryDisabledError,
    RecordingLibraryNotFoundError,
)
from lecture_stt.storage_v2.title_suggestions import (  # noqa: E402
    TitleSuggestionConflictError,
    TitleSuggestionNotFoundError,
    TitleSuggestionWriteDisabledError,
)
from lecture_stt.storage_v2.timetable import (  # noqa: E402
    TimetableConflictError,
    TimetableNotFoundError,
    TimetableWriteDisabledError,
)


class WebPanelRequestHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prev_state = web_panel.STATE

    def tearDown(self) -> None:
        web_panel.STATE = self.prev_state

    def test_exit_only_requests_server_shutdown(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler._write_json = mock.Mock()

        state = mock.Mock()
        web_panel.STATE = state

        handler._action_exit()

        state.request_shutdown.assert_called_once_with()
        state.stop.assert_not_called()
        handler._write_json.assert_called_once_with(
            200,
            {"ok": True, "notice": "웹 제어판 종료 요청됨"},
        )

    def test_root_route_returns_placeholder_when_react_build_is_missing(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/"
        handler._write = mock.Mock()

        with mock.patch.object(web_panel, "_resolve_react_asset", return_value=None):
            handler.do_GET()

        handler._write.assert_called_once()
        args = handler._write.call_args[0]
        self.assertEqual(args[0], 200)
        self.assertIn("웹 패널 빌드가 필요합니다", args[1])

    def test_root_route_serves_react_index_when_build_exists(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/"
        handler._write_file = mock.Mock()
        index_asset = Path("/tmp/index.html")

        with mock.patch.object(web_panel, "_resolve_react_asset", return_value=index_asset):
            handler.do_GET()

        handler._write_file.assert_called_once_with(index_asset)

    def test_app_route_redirects_to_main_panel(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/app"
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()

        handler.do_GET()

        handler.send_response.assert_called_once_with(303)
        handler.send_header.assert_any_call("Location", "/")
        handler.send_header.assert_any_call("Content-Length", "0")
        handler.end_headers.assert_called_once_with()

    def test_placeholder_uses_runtime_repo_root_in_instructions(self) -> None:
        rendered = web_panel.render_react_placeholder(Path("/tmp/lecture-stt-alt"))
        self.assertIn("/tmp/lecture-stt-alt/frontend/web-panel", rendered)

    def test_api_post_routes_dispatch_to_expected_handlers(self) -> None:
        routes = {
            "/api/start": "_action_start",
            "/api/pause": "_action_pause",
            "/api/resume": "_action_resume",
            "/api/stop": "_action_stop",
            "/api/refresh": "_action_refresh",
            "/api/exit": "_action_exit",
            "/api/clear_history": "_action_clear_history",
        }

        for path, handler_name in routes.items():
            with self.subTest(path=path):
                handler = object.__new__(web_panel.RequestHandler)
                handler.path = path
                for name in routes.values():
                    setattr(handler, name, mock.Mock())
                handler._api_error = mock.Mock()

                handler.do_POST()

                getattr(handler, handler_name).assert_called_once_with()
                handler._api_error.assert_not_called()

    def test_api_events_route_dispatches_stream_handler(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/events"
        handler._serve_event_stream = mock.Mock()

        handler.do_GET()

        handler._serve_event_stream.assert_called_once_with(
            include_state=True,
            include_logs=True,
        )

    def test_api_events_route_accepts_explicit_stream_selection(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/events?streams=state"
        handler._serve_event_stream = mock.Mock()
        handler._api_error = mock.Mock()

        handler.do_GET()

        handler._serve_event_stream.assert_called_once_with(
            include_state=True,
            include_logs=False,
        )
        handler._api_error.assert_not_called()

    def test_api_events_route_rejects_invalid_stream_selection(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/events?streams=state,unknown"
        handler._serve_event_stream = mock.Mock()
        handler._api_error = mock.Mock()

        handler.do_GET()

        handler._serve_event_stream.assert_not_called()
        handler._api_error.assert_called_once_with(
            400,
            "streams must be a comma-separated list of state and/or logs",
        )

    def test_api_events_route_rejects_blank_stream_selection(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/events?streams="
        handler._serve_event_stream = mock.Mock()
        handler._api_error = mock.Mock()

        handler.do_GET()

        handler._serve_event_stream.assert_not_called()
        handler._api_error.assert_called_once_with(
            400,
            "streams must be a comma-separated list of state and/or logs",
        )

    def test_api_events_route_rejects_repeated_stream_parameters(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/events?streams=state&streams=logs"
        handler._serve_event_stream = mock.Mock()
        handler._api_error = mock.Mock()

        handler.do_GET()

        handler._serve_event_stream.assert_not_called()
        handler._api_error.assert_called_once_with(
            400,
            "streams must be a comma-separated list of state and/or logs",
        )

    def test_api_events_route_rejects_duplicate_stream_tokens(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/events?streams=state,state"
        handler._serve_event_stream = mock.Mock()
        handler._api_error = mock.Mock()

        handler.do_GET()

        handler._serve_event_stream.assert_not_called()
        handler._api_error.assert_called_once_with(
            400,
            "streams must be a comma-separated list of state and/or logs",
        )

    def test_transcription_analytics_route_defaults_to_week_and_accepts_periods(self) -> None:
        state = mock.Mock()
        state.transcription_analytics.side_effect = (
            lambda *, period: {"period": period}
        )
        web_panel.STATE = state

        cases = (
            ("/api/storage-v2/analytics/transcriptions", "week"),
            ("/api/storage-v2/analytics/transcriptions?period=day", "day"),
            ("/api/storage-v2/analytics/transcriptions?period=week", "week"),
            ("/api/storage-v2/analytics/transcriptions?period=month", "month"),
        )
        for path, expected_period in cases:
            with self.subTest(path=path):
                handler = object.__new__(web_panel.RequestHandler)
                handler.path = path
                handler._write_json = mock.Mock()
                handler._api_error = mock.Mock()

                handler.do_GET()

                state.transcription_analytics.assert_called_once_with(
                    period=expected_period
                )
                handler._write_json.assert_called_once_with(
                    200,
                    {"period": expected_period},
                )
                handler._api_error.assert_not_called()
                state.transcription_analytics.reset_mock()

    def test_transcription_analytics_route_rejects_bad_period_queries(self) -> None:
        state = mock.Mock()

        def analytics(*, period: str) -> dict:
            if period not in {"day", "week", "month"}:
                raise ValueError("period must be one of: day, week, month")
            return {"period": period}

        state.transcription_analytics.side_effect = analytics
        web_panel.STATE = state
        cases = (
            "/api/storage-v2/analytics/transcriptions?period=",
            "/api/storage-v2/analytics/transcriptions?period=year",
            "/api/storage-v2/analytics/transcriptions?period=day&period=week",
        )

        for path in cases:
            with self.subTest(path=path):
                handler = object.__new__(web_panel.RequestHandler)
                handler.path = path
                handler._write_json = mock.Mock()
                handler._api_error = mock.Mock()

                handler.do_GET()

                handler._api_error.assert_called_once_with(
                    400,
                    "period must be one of: day, week, month",
                )
                handler._write_json.assert_not_called()
                state.transcription_analytics.reset_mock()

    def test_transcription_analytics_route_maps_runtime_failure_to_503(self) -> None:
        state = mock.Mock()
        state.transcription_analytics.side_effect = RuntimeError(
            "isolated analytics unavailable"
        )
        web_panel.STATE = state
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/storage-v2/analytics/transcriptions?period=week"
        handler._write_json = mock.Mock()
        handler._api_error = mock.Mock()

        handler.do_GET()

        state.transcription_analytics.assert_called_once_with(period="week")
        handler._api_error.assert_called_once_with(
            503,
            "isolated analytics unavailable",
        )
        handler._write_json.assert_not_called()

    def test_transcription_analytics_prometheus_route_defaults_to_week(self) -> None:
        state = mock.Mock()
        state.transcription_analytics_prometheus.side_effect = (
            lambda *, period: (
                "# HELP test\n"
                "# TYPE test gauge\n"
                f'lecture_stt_transcriptions_jobs_in_window{{period=\"{period}\"}} 1\n'
            )
        )
        web_panel.STATE = state

        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/storage-v2/analytics/transcriptions/metrics"
        handler._write = mock.Mock()
        handler._api_error = mock.Mock()

        handler.do_GET()

        state.transcription_analytics_prometheus.assert_called_once_with(period="week")
        handler._write.assert_called_once()
        self.assertIn(
            "text/plain; version=0.0.4; charset=utf-8",
            handler._write.call_args[0],
        )
        args = handler._write.call_args[0]
        self.assertEqual(args[0], 200)
        self.assertIn(
            'lecture_stt_transcriptions_jobs_in_window{period="week"} 1',
            args[1],
        )
        handler._api_error.assert_not_called()

    def test_transcription_analytics_prometheus_route_rejects_bad_period_queries(self) -> None:
        state = mock.Mock()
        def analytics_prometheus(*, period: str) -> str:
            if period not in {"day", "week", "month"}:
                raise ValueError("period must be one of: day, week, month")
            return f"period={period}\n"

        state.transcription_analytics_prometheus.side_effect = analytics_prometheus
        web_panel.STATE = state

        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/storage-v2/analytics/transcriptions/metrics?period=year"
        handler._write = mock.Mock()
        handler._api_error = mock.Mock()

        handler.do_GET()

        handler._api_error.assert_called_once_with(
            400,
            "period must be one of: day, week, month",
        )
        handler._write.assert_not_called()
        state.transcription_analytics_prometheus.assert_called_once_with(period="year")

    def test_transcription_analytics_prometheus_route_rejects_unknown_query(self) -> None:
        state = mock.Mock()
        web_panel.STATE = state
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = (
            "/api/storage-v2/analytics/transcriptions/metrics?period=week&extra=1"
        )
        handler._write = mock.Mock()
        handler._api_error = mock.Mock()

        handler.do_GET()

        handler._api_error.assert_called_once_with(
            400,
            "only the following query parameters are allowed: period",
        )
        handler._write.assert_not_called()
        state.transcription_analytics_prometheus.assert_not_called()

    def test_recording_library_routes_dispatch_and_default_pagination(self) -> None:
        state = mock.Mock()
        state.recording_library_list.return_value = {
            "available": True,
            "summaries": [],
        }
        state.recording_library_detail.return_value = {
            "available": True,
            "recording": {"storage_key": "recording_a"},
        }
        web_panel.STATE = state

        list_handler = object.__new__(web_panel.RequestHandler)
        list_handler.path = "/api/storage-v2/library/recordings"
        list_handler._write_json = mock.Mock()
        list_handler._api_error = mock.Mock()
        list_handler.do_GET()

        state.recording_library_list.assert_called_once_with(
            limit=50,
            offset=0,
        )
        list_handler._write_json.assert_called_once_with(
            200,
            {"available": True, "summaries": []},
        )

        detail_handler = object.__new__(web_panel.RequestHandler)
        detail_handler.path = "/api/storage-v2/library/recordings/recording_a"
        detail_handler._write_json = mock.Mock()
        detail_handler._api_error = mock.Mock()
        detail_handler.do_GET()

        state.recording_library_detail.assert_called_once_with("recording_a")
        detail_handler._write_json.assert_called_once_with(
            200,
            {"available": True, "recording": {"storage_key": "recording_a"}},
        )

    def test_recording_library_list_route_is_exact_query_fail_closed(self) -> None:
        state = mock.Mock()
        state.recording_library_list.return_value = {"available": True}
        web_panel.STATE = state

        cases = (
            (
                "/api/storage-v2/library/recordings?limit=25&offset=5",
                None,
                {"limit": 25, "offset": 5},
            ),
            (
                "/api/storage-v2/library/recordings?limit=+1",
                "limit must be between 0 and 200",
                None,
            ),
            (
                "/api/storage-v2/library/recordings?offset= 1",
                "offset must be between 0 and 100000",
                None,
            ),
            (
                "/api/storage-v2/library/recordings?limit=01",
                "limit must be between 0 and 200",
                None,
            ),
            (
                "/api/storage-v2/library/recordings?limit=10&limit=11",
                "limit must be between 0 and 200",
                None,
            ),
            (
                "/api/storage-v2/library/recordings?detail=true",
                "only the following query parameters are allowed: limit, offset",
                None,
            ),
            (
                "/api/storage-v2/library/recordings?offset=",
                "offset must be between 0 and 100000",
                None,
            ),
        )

        for path, expected_error, expected_call in cases:
            with self.subTest(path=path):
                handler = object.__new__(web_panel.RequestHandler)
                handler.path = path
                handler._write_json = mock.Mock()
                handler._api_error = mock.Mock()

                handler.do_GET()

                if expected_error is None:
                    state.recording_library_list.assert_called_once_with(
                        **expected_call
                    )
                    handler._write_json.assert_called_once_with(
                        200,
                        {"available": True},
                    )
                    handler._api_error.assert_not_called()
                else:
                    handler._api_error.assert_called_once_with(
                        400,
                        expected_error,
                    )
                    handler._write_json.assert_not_called()
                state.recording_library_list.reset_mock()

    def test_recording_library_detail_route_rejects_query_and_maps_errors(self) -> None:
        state = mock.Mock()
        web_panel.STATE = state

        query_handler = object.__new__(web_panel.RequestHandler)
        query_handler.path = "/api/storage-v2/library/recordings/recording_a?offset=1"
        query_handler._write_json = mock.Mock()
        query_handler._api_error = mock.Mock()
        query_handler.do_GET()
        query_handler._api_error.assert_called_once_with(
            400,
            "recording detail does not accept query parameters",
        )

        encoded_key_handler = object.__new__(web_panel.RequestHandler)
        web_panel.STATE = state
        encoded_key_handler.path = "/api/storage-v2/library/recordings/rec%2Fbad%2Fkey"
        encoded_key_handler._write_json = mock.Mock()
        encoded_key_handler._api_error = mock.Mock()
        state.recording_library_detail.side_effect = ValueError(
            "storage_key must be a non-empty ASCII path-safe key"
        )
        encoded_key_handler.do_GET()

        state.recording_library_detail.assert_called_once_with("rec%2Fbad%2Fkey")
        encoded_key_handler._api_error.assert_called_once_with(
            400,
            "storage_key must be a non-empty ASCII path-safe key",
        )
        encoded_key_handler._write_json.assert_not_called()

        blank_query_handler = object.__new__(web_panel.RequestHandler)
        blank_query_handler.path = (
            "/api/storage-v2/library/recordings/recording_a?offset="
        )
        blank_query_handler._write_json = mock.Mock()
        blank_query_handler._api_error = mock.Mock()
        blank_query_handler.do_GET()
        blank_query_handler._api_error.assert_called_once_with(
            400,
            "recording detail does not accept query parameters",
        )

        trailing_handler = object.__new__(web_panel.RequestHandler)
        trailing_handler.path = "/api/storage-v2/library/recordings/recording_a/"
        trailing_handler._write_json = mock.Mock()
        trailing_handler._api_error = mock.Mock()
        trailing_handler.do_GET()
        trailing_handler._api_error.assert_called_once_with(404, "Not Found")

        state.recording_library_detail.side_effect = RecordingLibraryDisabledError(
            "Recording library API is disabled"
        )
        disabled_handler = object.__new__(web_panel.RequestHandler)
        disabled_handler.path = "/api/storage-v2/library/recordings/recording_a"
        disabled_handler._write_json = mock.Mock()
        disabled_handler._api_error = mock.Mock()
        disabled_handler.do_GET()
        disabled_handler._api_error.assert_called_once_with(
            403,
            "Recording library API is disabled",
        )

        state.recording_library_detail.side_effect = RecordingLibraryNotFoundError(
            "Recording not found: recording_a"
        )
        not_found_handler = object.__new__(web_panel.RequestHandler)
        not_found_handler.path = "/api/storage-v2/library/recordings/recording_a"
        not_found_handler._write_json = mock.Mock()
        not_found_handler._api_error = mock.Mock()
        not_found_handler.do_GET()
        not_found_handler._api_error.assert_called_once_with(
            404,
            "Recording not found: recording_a",
        )

        state.recording_library_detail.side_effect = RuntimeError(
            "/private/operating/storage-v2.sqlite3 failed"
        )
        failure_handler = object.__new__(web_panel.RequestHandler)
        failure_handler.path = "/api/storage-v2/library/recordings/recording_a"
        failure_handler._write_json = mock.Mock()
        failure_handler._api_error = mock.Mock()
        failure_handler.do_GET()
        failure_handler._api_error.assert_called_once_with(
            503,
            "Recording library is unavailable",
        )

    def test_unified_review_feed_route_dispatches_and_fails_closed(self) -> None:
        state = mock.Mock()
        state.unified_review_feed.return_value = {"available": True, "items": []}
        web_panel.STATE = state

        cases = (
            (
                "/api/storage-v2/review-feed?limit=25&offset=5",
                None,
                {"limit": 25, "offset": 5},
            ),
            (
                "/api/storage-v2/review-feed?limit=01",
                "limit must be between 0 and 200",
                None,
            ),
            (
                "/api/storage-v2/review-feed?offset=",
                "offset must be between 0 and 100000",
                None,
            ),
            (
                "/api/storage-v2/review-feed?offset=10&offset=11",
                "offset must be between 0 and 100000",
                None,
            ),
            (
                "/api/storage-v2/review-feed?detail=true",
                "only the following query parameters are allowed: limit, offset",
                None,
            ),
        )

        for path, expected_error, expected_call in cases:
            with self.subTest(path=path):
                handler = object.__new__(web_panel.RequestHandler)
                handler.path = path
                handler._write_json = mock.Mock()
                handler._api_error = mock.Mock()

                handler.do_GET()

                if expected_error is None:
                    state.unified_review_feed.assert_called_once_with(**expected_call)
                    handler._write_json.assert_called_once_with(
                        200,
                        {"available": True, "items": []},
                    )
                    handler._api_error.assert_not_called()
                else:
                    handler._api_error.assert_called_once_with(400, expected_error)
                    handler._write_json.assert_not_called()
                    state.unified_review_feed.assert_not_called()
                state.unified_review_feed.reset_mock()

    def test_unified_review_feed_route_hides_runtime_details(self) -> None:
        for internal_error in (
            RuntimeError("/private/runtime/storage-v2.sqlite3 failed"),
            ValueError("unified review timestamps must be valid ISO timestamps"),
        ):
            with self.subTest(error_type=type(internal_error).__name__):
                state = mock.Mock()
                state.unified_review_feed.side_effect = internal_error
                web_panel.STATE = state

                handler = object.__new__(web_panel.RequestHandler)
                handler.path = "/api/storage-v2/review-feed"
                handler._write_json = mock.Mock()
                handler._api_error = mock.Mock()

                handler.do_GET()

                state.unified_review_feed.assert_called_once_with(limit=50, offset=0)
                handler._write_json.assert_not_called()
                handler._api_error.assert_called_once_with(
                    503,
                    "Unified review feed is unavailable",
                )

    def test_title_suggestion_list_and_detail_routes_dispatch_and_fail_closed(
        self,
    ) -> None:
        state = mock.Mock()
        state.title_review_list.return_value = {"available": True, "proposals": []}
        state.title_review_detail.return_value = {"proposal": {"id": 7}}
        web_panel.STATE = state

        list_cases = (
            (
                "/api/storage-v2/title-suggestions?status=suggested&limit=25&offset=5",
                None,
                {"status": "suggested", "limit": 25, "offset": 5},
            ),
            (
                "/api/storage-v2/title-suggestions?status=draft",
                "status must be suggested, confirmed, or rejected",
                None,
            ),
            (
                "/api/storage-v2/title-suggestions?offset=10&offset=11",
                "offset must be between 0 and 100000",
                None,
            ),
            (
                "/api/storage-v2/title-suggestions?extra=1",
                "only the following query parameters are allowed: status, limit, offset",
                None,
            ),
        )
        for path, expected_error, expected_call in list_cases:
            with self.subTest(path=path):
                handler = object.__new__(web_panel.RequestHandler)
                handler.path = path
                handler._write_json = mock.Mock()
                handler._api_error = mock.Mock()

                handler.do_GET()

                if expected_error is None:
                    state.title_review_list.assert_called_once_with(
                        **expected_call
                    )
                    handler._write_json.assert_called_once_with(
                        200,
                        {"available": True, "proposals": []},
                    )
                    handler._api_error.assert_not_called()
                else:
                    handler._api_error.assert_called_once_with(
                        400,
                        expected_error,
                    )
                    state.title_review_list.assert_not_called()
                state.title_review_list.reset_mock()

        detail_handler = object.__new__(web_panel.RequestHandler)
        detail_handler.path = "/api/storage-v2/title-suggestions/7"
        detail_handler._write_json = mock.Mock()
        detail_handler._api_error = mock.Mock()
        detail_handler.do_GET()

        state.title_review_detail.assert_called_once_with(7)
        detail_handler._write_json.assert_called_once_with(
            200,
            {"proposal": {"id": 7}},
        )

    def test_title_suggestion_detail_route_rejects_noncanonical_ids_and_query(
        self,
    ) -> None:
        state = mock.Mock()
        state.title_review_detail.return_value = {"proposal": {"id": 7}}
        web_panel.STATE = state

        invalid_paths = (
            "/api/storage-v2/title-suggestions/01",
            "/api/storage-v2/title-suggestions/+1",
            "/api/storage-v2/title-suggestions/1/",
            "/api/storage-v2/title-suggestions//1",
        )
        for path in invalid_paths:
            with self.subTest(path=path):
                handler = object.__new__(web_panel.RequestHandler)
                handler.path = path
                handler._write_json = mock.Mock()
                handler._api_error = mock.Mock()
                handler.do_GET()
                handler._api_error.assert_called_once()
                self.assertEqual(handler._api_error.call_args.args[0], 400)
                state.title_review_detail.assert_not_called()

        query_handler = object.__new__(web_panel.RequestHandler)
        query_handler.path = "/api/storage-v2/title-suggestions/7?extra=1"
        query_handler._write_json = mock.Mock()
        query_handler._api_error = mock.Mock()
        query_handler.do_GET()
        query_handler._api_error.assert_called_once_with(
            400,
            "title suggestion detail does not accept query parameters",
        )
        state.title_review_detail.assert_not_called()

    def test_title_suggestion_post_routes_require_exact_json_fields_and_no_query(
        self,
    ) -> None:
        state = mock.Mock()
        state.title_review_status_update.return_value = {"ok": True}
        state.title_review_confirmation_plan.return_value = {
            "plan_sha256": "a" * 64
        }
        state.title_review_confirmation_apply.return_value = {"ok": True}
        web_panel.STATE = state

        status_handler = object.__new__(web_panel.RequestHandler)
        status_handler.path = "/api/storage-v2/title-suggestions/7/status"
        status_body = json.dumps(
            {"status": "rejected", "allow_write": True}
        ).encode("utf-8")
        status_handler.headers = {
            "Content-Length": str(len(status_body)),
            "Content-Type": "application/json",
        }
        status_handler.rfile = io.BytesIO(status_body)
        status_handler._write_json = mock.Mock()
        status_handler._api_error = mock.Mock()
        status_handler.do_POST()
        state.title_review_status_update.assert_called_once_with(
            7,
            status="rejected",
            allow_write=True,
        )

        extra_status = object.__new__(web_panel.RequestHandler)
        extra_status.path = "/api/storage-v2/title-suggestions/7/status"
        extra_status_body = json.dumps(
            {"status": "rejected", "allow_write": True, "extra": 1}
        ).encode("utf-8")
        extra_status.headers = {
            "Content-Length": str(len(extra_status_body)),
            "Content-Type": "application/json",
        }
        extra_status.rfile = io.BytesIO(extra_status_body)
        extra_status._write_json = mock.Mock()
        extra_status._api_error = mock.Mock()
        extra_status.do_POST()
        extra_status._api_error.assert_called_once_with(
            400,
            "only the following JSON fields are allowed: status, allow_write",
        )

        invalid_status = object.__new__(web_panel.RequestHandler)
        invalid_status.path = "/api/storage-v2/title-suggestions/7/status"
        invalid_status_body = json.dumps(
            {"status": "confirmed", "allow_write": True}
        ).encode("utf-8")
        invalid_status.headers = {
            "Content-Length": str(len(invalid_status_body)),
            "Content-Type": "application/json",
        }
        invalid_status.rfile = io.BytesIO(invalid_status_body)
        invalid_status._write_json = mock.Mock()
        invalid_status._api_error = mock.Mock()
        invalid_status.do_POST()
        invalid_status._api_error.assert_called_once_with(
            400,
            "status must be rejected",
        )

        plan_handler = object.__new__(web_panel.RequestHandler)
        plan_handler.path = (
            "/api/storage-v2/title-suggestions/7/confirmation/plan"
        )
        plan_handler.headers = {"Content-Length": "0"}
        plan_handler.rfile = io.BytesIO(b"")
        plan_handler._write_json = mock.Mock()
        plan_handler._api_error = mock.Mock()
        plan_handler.do_POST()
        state.title_review_confirmation_plan.assert_called_once_with(7)

        extra_plan = object.__new__(web_panel.RequestHandler)
        extra_plan.path = (
            "/api/storage-v2/title-suggestions/7/confirmation/plan"
        )
        extra_plan_body = json.dumps({"extra": True}).encode("utf-8")
        extra_plan.headers = {
            "Content-Length": str(len(extra_plan_body)),
            "Content-Type": "application/json",
        }
        extra_plan.rfile = io.BytesIO(extra_plan_body)
        extra_plan._write_json = mock.Mock()
        extra_plan._api_error = mock.Mock()
        extra_plan.do_POST()
        extra_plan._api_error.assert_called_once_with(
            400,
            "title suggestion confirmation plan does not accept request fields",
        )

        apply_handler = object.__new__(web_panel.RequestHandler)
        apply_handler.path = (
            "/api/storage-v2/title-suggestions/7/confirmation/apply"
        )
        apply_body = json.dumps(
            {
                "expected_count": 1,
                "expected_plan_sha256": "a" * 64,
                "allow_write": True,
            }
        ).encode("utf-8")
        apply_handler.headers = {
            "Content-Length": str(len(apply_body)),
            "Content-Type": "application/json",
        }
        apply_handler.rfile = io.BytesIO(apply_body)
        apply_handler._write_json = mock.Mock()
        apply_handler._api_error = mock.Mock()
        apply_handler.do_POST()
        state.title_review_confirmation_apply.assert_called_once_with(
            7,
            expected_count=1,
            expected_plan_sha256="a" * 64,
            allow_write=True,
        )

        extra_apply = object.__new__(web_panel.RequestHandler)
        extra_apply.path = (
            "/api/storage-v2/title-suggestions/7/confirmation/apply"
        )
        extra_apply_body = json.dumps(
            {
                "expected_count": 1,
                "expected_plan_sha256": "a" * 64,
                "allow_write": True,
                "extra": "x",
            }
        ).encode("utf-8")
        extra_apply.headers = {
            "Content-Length": str(len(extra_apply_body)),
            "Content-Type": "application/json",
        }
        extra_apply.rfile = io.BytesIO(extra_apply_body)
        extra_apply._write_json = mock.Mock()
        extra_apply._api_error = mock.Mock()
        extra_apply.do_POST()
        extra_apply._api_error.assert_called_once_with(
            400,
            "only the following JSON fields are allowed: expected_count, expected_plan_sha256, allow_write",
        )

        bad_digest = object.__new__(web_panel.RequestHandler)
        bad_digest.path = (
            "/api/storage-v2/title-suggestions/7/confirmation/apply"
        )
        bad_digest_body = json.dumps(
            {
                "expected_count": 1,
                "expected_plan_sha256": "ABC",
                "allow_write": True,
            }
        ).encode("utf-8")
        bad_digest.headers = {
            "Content-Length": str(len(bad_digest_body)),
            "Content-Type": "application/json",
        }
        bad_digest.rfile = io.BytesIO(bad_digest_body)
        bad_digest._write_json = mock.Mock()
        bad_digest._api_error = mock.Mock()
        bad_digest.do_POST()
        bad_digest._api_error.assert_called_once_with(
            400,
            "expected_plan_sha256 must be a canonical SHA-256",
        )

        query_handler = object.__new__(web_panel.RequestHandler)
        query_handler.path = "/api/storage-v2/title-suggestions/7/status?extra=1"
        query_handler._write_json = mock.Mock()
        query_handler._api_error = mock.Mock()
        query_handler.do_POST()
        query_handler._api_error.assert_called_once_with(
            400,
            "title suggestion actions do not accept query parameters",
        )

    def test_title_suggestion_errors_map_to_specific_http_statuses(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler._api_error = mock.Mock()

        handler._title_review_api_error(TitleSuggestionNotFoundError("missing"))
        handler._title_review_api_error(
            TitleSuggestionWriteDisabledError("disabled")
        )
        handler._title_review_api_error(
            TitleSuggestionConflictError("conflict")
        )
        handler._title_review_api_error(ValueError("/private/config/path"))

        self.assertEqual(
            handler._api_error.call_args_list,
            [
                mock.call(404, "missing"),
                mock.call(403, "disabled"),
                mock.call(409, "conflict"),
                mock.call(503, "Title review is unavailable"),
            ],
        )

    def test_title_suggestion_route_sanitizes_state_value_error(self) -> None:
        state = mock.Mock()
        state.title_review_list.side_effect = ValueError(
            "/private/runtime/storage-v2.sqlite3"
        )
        web_panel.STATE = state

        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/storage-v2/title-suggestions"
        handler._write_json = mock.Mock()
        handler._api_error = mock.Mock()
        handler.do_GET()

        handler._api_error.assert_called_once_with(
            503,
            "Title review is unavailable",
        )

    def test_unknown_api_path_returns_json_404(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/does-not-exist"
        handler._api_error = mock.Mock()

        handler.do_POST()

        handler._api_error.assert_called_once_with(404, "Not Found")

    def test_unknown_api_get_path_returns_json_404(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/does-not-exist"
        handler._api_error = mock.Mock()

        handler.do_GET()

        handler._api_error.assert_called_once_with(404, "Not Found")

    def test_api_notification_route_defaults_to_deferred_apply(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/notification"
        handler.headers = {"Content-Length": "14"}
        handler.rfile = io.BytesIO(b"selection=both")
        handler._action_notification_update = mock.Mock()
        handler._api_error = mock.Mock()

        handler.do_POST()

        handler._action_notification_update.assert_called_once_with("both", apply_now=False)
        handler._api_error.assert_not_called()

    def test_api_notification_route_passes_apply_now_flag(self) -> None:
        body = b"selection=discord&apply_now=1"
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/notification"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._action_notification_update = mock.Mock()
        handler._api_error = mock.Mock()

        handler.do_POST()

        handler._action_notification_update.assert_called_once_with("discord", apply_now=True)
        handler._api_error.assert_not_called()

    def test_json_body_parser_rejects_malformed_or_oversized_payloads(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)

        malformed_cases = {
            "malformed_json": (b"{bad-json", "Invalid JSON request body"),
            "array_payload": (b"[]", "JSON request body must be an object"),
            "empty_payload": (b"", "JSON request body is required"),
        }
        for _label, (body, message) in malformed_cases.items():
            with self.subTest(label=_label):
                handler.headers = {"Content-Length": str(len(body))}
                handler.headers["Content-Type"] = "application/json"
                handler.rfile = io.BytesIO(body)

                with self.assertRaisesRegex(ValueError, message):
                    handler._read_json_object()

        oversized_body = json.dumps(
            {"payload": "x" * (web_panel.RequestHandler.JSON_BODY_LIMIT_BYTES + 1)}
        ).encode("utf-8")
        handler.headers = {
            "Content-Length": str(len(oversized_body)),
            "Content-Type": "application/json",
        }
        handler.rfile = io.BytesIO(oversized_body)
        with self.assertRaisesRegex(ValueError, "too large"):
            handler._read_json_object()

        handler.headers = {
            "Content-Length": "2",
            "Content-Type": "text/plain",
        }
        handler.rfile = io.BytesIO(b"{}")
        with self.assertRaisesRegex(ValueError, "Content-Type"):
            handler._read_json_object()

        handler.headers = {"Content-Length": "0"}
        self.assertEqual({}, handler._read_optional_json_object())

    def test_archive_review_list_and_detail_routes_dispatch_to_state(self) -> None:
        state = mock.Mock()
        state.archive_review_list.return_value = {"available": True, "cases": []}
        state.archive_review_detail.return_value = {"case": {"case_key": "case_a"}}
        web_panel.STATE = state

        list_handler = object.__new__(web_panel.RequestHandler)
        list_handler.path = (
            "/api/storage-v2/archive-evidence/cases"
            "?status=triaged&limit=10&offset=20"
        )
        list_handler._write_json = mock.Mock()
        list_handler._api_error = mock.Mock()
        list_handler.do_GET()

        state.archive_review_list.assert_called_once_with(
            review_status="triaged",
            limit=10,
            offset=20,
        )
        list_handler._write_json.assert_called_once_with(
            200,
            {"available": True, "cases": []},
        )

        detail_handler = object.__new__(web_panel.RequestHandler)
        detail_handler.path = "/api/storage-v2/archive-evidence/cases/case_a"
        detail_handler._write_json = mock.Mock()
        detail_handler._api_error = mock.Mock()
        detail_handler.do_GET()

        state.archive_review_detail.assert_called_once_with("case_a")
        detail_handler._write_json.assert_called_once_with(
            200,
            {"case": {"case_key": "case_a"}},
        )

    def test_archive_review_list_rejects_unknown_and_repeated_queries(
        self,
    ) -> None:
        state = mock.Mock()
        web_panel.STATE = state
        cases = (
            "/api/storage-v2/archive-evidence/cases?extra=1",
            "/api/storage-v2/archive-evidence/cases?status=new&status=triaged",
            "/api/storage-v2/archive-evidence/cases?limit=10&limit=20",
        )

        for path in cases:
            with self.subTest(path=path):
                handler = object.__new__(web_panel.RequestHandler)
                handler.path = path
                handler._write_json = mock.Mock()
                handler._api_error = mock.Mock()

                handler.do_GET()

                handler._api_error.assert_called_once()
                self.assertEqual(handler._api_error.call_args.args[0], 400)
                handler._write_json.assert_not_called()
                state.archive_review_list.assert_not_called()

    def test_archive_review_status_route_requires_json_guard(self) -> None:
        body = json.dumps(
            {
                "review_status": "triaged",
                "allow_write": True,
            }
        ).encode("utf-8")
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/storage-v2/archive-evidence/cases/case_a/status"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
        }
        handler.rfile = io.BytesIO(body)
        handler._write_json = mock.Mock()
        handler._api_error = mock.Mock()
        state = mock.Mock()
        state.archive_review_update_status.return_value = {
            "ok": True,
            "review_status": "triaged",
        }
        web_panel.STATE = state

        handler.do_POST()

        state.archive_review_update_status.assert_called_once_with(
            "case_a",
            "triaged",
            allow_write=True,
        )
        handler._write_json.assert_called_once_with(
            200,
            {"ok": True, "review_status": "triaged"},
        )

    def test_archive_review_promotion_plan_and_apply_routes_dispatch_guards(self) -> None:
        base_payload = {
            "target_storage_key": "recording_a",
            "selected_revisions": {"summary_markdown": 7},
        }
        state = mock.Mock()
        state.archive_review_plan_promotion.return_value = {
            "mode": "read_only",
            "expected_count": 1,
        }
        state.archive_review_apply_promotion.return_value = {
            "ok": True,
            "status": "promoted",
        }
        web_panel.STATE = state

        plan_body = json.dumps(base_payload).encode("utf-8")
        plan_handler = object.__new__(web_panel.RequestHandler)
        plan_handler.path = (
            "/api/storage-v2/archive-evidence/cases/case_a/promotion/plan"
        )
        plan_handler.headers = {
            "Content-Length": str(len(plan_body)),
            "Content-Type": "application/json",
        }
        plan_handler.rfile = io.BytesIO(plan_body)
        plan_handler._write_json = mock.Mock()
        plan_handler._api_error = mock.Mock()
        plan_handler.do_POST()

        state.archive_review_plan_promotion.assert_called_once_with(
            "case_a",
            target_storage_key="recording_a",
            selected_revisions={"summary_markdown": 7},
        )

        apply_payload = {
            **base_payload,
            "expected_count": 1,
            "expected_plan_sha256": "a" * 64,
            "allow_write": True,
        }
        apply_body = json.dumps(apply_payload).encode("utf-8")
        apply_handler = object.__new__(web_panel.RequestHandler)
        apply_handler.path = (
            "/api/storage-v2/archive-evidence/cases/case_a/promotion/apply"
        )
        apply_handler.headers = {
            "Content-Length": str(len(apply_body)),
            "Content-Type": "application/json",
        }
        apply_handler.rfile = io.BytesIO(apply_body)
        apply_handler._write_json = mock.Mock()
        apply_handler._api_error = mock.Mock()
        apply_handler.do_POST()

        state.archive_review_apply_promotion.assert_called_once_with(
            "case_a",
            target_storage_key="recording_a",
            selected_revisions={"summary_markdown": 7},
            expected_count=1,
            expected_plan_sha256="a" * 64,
            allow_write=True,
        )

    def test_archive_review_errors_map_to_specific_http_statuses(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler._api_error = mock.Mock()

        handler._archive_review_api_error(
            ArchiveReviewNotFoundError("missing")
        )
        handler._archive_review_api_error(
            ArchiveReviewWriteDisabledError("disabled")
        )
        handler._archive_review_api_error(
            ArchiveReviewConflictError("conflict")
        )
        handler._archive_review_api_error(ValueError("invalid"))

        self.assertEqual(
            handler._api_error.call_args_list,
            [
                mock.call(404, "missing"),
                mock.call(403, "disabled"),
                mock.call(409, "conflict"),
                mock.call(400, "invalid"),
            ],
        )

    def test_disabled_archive_review_detail_returns_403(self) -> None:
        state = mock.Mock()
        state.archive_review_detail.side_effect = (
            ArchiveReviewWriteDisabledError(
                "Archive evidence review API is disabled"
            )
        )
        web_panel.STATE = state
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/storage-v2/archive-evidence/cases/case_a"
        handler._write_json = mock.Mock()

        handler.do_GET()

        handler._write_json.assert_called_once_with(
            403,
            {
                "ok": False,
                "error": "Archive evidence review API is disabled",
            },
        )

    def test_state_only_event_stream_skips_log_reads(self) -> None:
        snapshot = {
            "runtime_state": "idle",
            "notification": {},
            "counts": {},
            "folders": {},
            "actions": {},
            "jobs_v2": {},
            "processing_v2": {},
            "summary": {"poll_interval_sec": 1.0},
        }
        state = mock.Mock()
        state.snapshot.return_value = snapshot
        state._log_stream_delta = mock.Mock()
        state.transcription_analytics = mock.Mock()
        web_panel.STATE = state

        handler = object.__new__(web_panel.RequestHandler)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler._write_sse_event = mock.Mock()
        handler._write_sse_comment = mock.Mock()

        with (
            mock.patch.object(web_panel.time, "monotonic", side_effect=[0.0, 0.0]),
            mock.patch.object(
                web_panel.time,
                "sleep",
                side_effect=BrokenPipeError,
            ),
        ):
            handler._serve_event_stream(include_state=True, include_logs=False)

        state.snapshot.assert_called_once_with(include_log=False)
        state._log_stream_delta.assert_not_called()
        state.transcription_analytics.assert_not_called()
        self.assertNotIn("analytics", snapshot)
        handler._write_sse_event.assert_called_once_with("state", snapshot)
        handler._write_sse_comment.assert_not_called()

    def test_timetable_list_detail_and_confirmation_routes(self) -> None:
        state = mock.Mock()
        state.timetable_list.return_value = {"available": True, "entries": []}
        state.timetable_classification_list.return_value = {
            "available": True,
            "proposals": [],
        }
        state.timetable_classification_detail.return_value = {
            "proposal": {"id": 7}
        }
        state.timetable_confirmation_plan.return_value = {
            "expected_count": 1,
            "mode": "read_only",
        }
        state.timetable_confirmation_apply.return_value = {
            "ok": True,
            "status": "confirmed",
        }
        state.timetable_classification_status_update.return_value = {
            "ok": True,
            "status": "rejected",
        }
        web_panel.STATE = state

        entries = object.__new__(web_panel.RequestHandler)
        entries.path = (
            "/api/storage-v2/timetable/entries"
            "?semester=2026-1&limit=10&offset=2"
        )
        entries._write_json = mock.Mock()
        entries.do_GET()
        state.timetable_list.assert_called_once_with(
            semester="2026-1",
            limit=10,
            offset=2,
        )

        queue = object.__new__(web_panel.RequestHandler)
        queue.path = (
            "/api/storage-v2/timetable/classifications"
            "?status=suggested&limit=20&offset=3"
        )
        queue._write_json = mock.Mock()
        queue.do_GET()
        state.timetable_classification_list.assert_called_once_with(
            status="suggested",
            limit=20,
            offset=3,
        )

        detail = object.__new__(web_panel.RequestHandler)
        detail.path = "/api/storage-v2/timetable/classifications/7"
        detail._write_json = mock.Mock()
        detail.do_GET()
        state.timetable_classification_detail.assert_called_once_with(7)

        status_body = json.dumps(
            {"status": "rejected", "allow_write": True}
        ).encode("utf-8")
        status_handler = object.__new__(web_panel.RequestHandler)
        status_handler.path = (
            "/api/storage-v2/timetable/classifications/7/status"
        )
        status_handler.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(status_body)),
        }
        status_handler.rfile = io.BytesIO(status_body)
        status_handler._write_json = mock.Mock()
        status_handler.do_POST()
        state.timetable_classification_status_update.assert_called_once_with(
            7,
            status="rejected",
            allow_write=True,
        )

        plan_body = b"{}"
        plan = object.__new__(web_panel.RequestHandler)
        plan.path = (
            "/api/storage-v2/timetable/classifications/7/confirmation/plan"
        )
        plan.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(plan_body)),
        }
        plan.rfile = io.BytesIO(plan_body)
        plan._write_json = mock.Mock()
        plan.do_POST()
        state.timetable_confirmation_plan.assert_called_once_with(7)

        apply_body = json.dumps(
            {
                "expected_count": 1,
                "expected_plan_sha256": "a" * 64,
                "allow_write": True,
            }
        ).encode("utf-8")
        apply_handler = object.__new__(web_panel.RequestHandler)
        apply_handler.path = (
            "/api/storage-v2/timetable/classifications/7/confirmation/apply"
        )
        apply_handler.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(apply_body)),
        }
        apply_handler.rfile = io.BytesIO(apply_body)
        apply_handler._write_json = mock.Mock()
        apply_handler.do_POST()
        state.timetable_confirmation_apply.assert_called_once_with(
            7,
            expected_count=1,
            expected_plan_sha256="a" * 64,
            allow_write=True,
        )

    def test_timetable_lists_reject_unknown_and_repeated_queries(self) -> None:
        state = mock.Mock()
        web_panel.STATE = state
        cases = (
            (
                "/api/storage-v2/timetable/entries?extra=1",
                state.timetable_list,
            ),
            (
                "/api/storage-v2/timetable/entries"
                "?semester=2026-1&semester=2026-2",
                state.timetable_list,
            ),
            (
                "/api/storage-v2/timetable/classifications?extra=1",
                state.timetable_classification_list,
            ),
            (
                "/api/storage-v2/timetable/classifications"
                "?status=suggested&status=confirmed",
                state.timetable_classification_list,
            ),
        )

        for path, state_method in cases:
            with self.subTest(path=path):
                handler = object.__new__(web_panel.RequestHandler)
                handler.path = path
                handler._write_json = mock.Mock()
                handler._api_error = mock.Mock()

                handler.do_GET()

                handler._api_error.assert_called_once()
                self.assertEqual(handler._api_error.call_args.args[0], 400)
                handler._write_json.assert_not_called()
                state_method.assert_not_called()

    def test_timetable_errors_map_to_specific_http_statuses(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler._api_error = mock.Mock()

        handler._timetable_api_error(TimetableNotFoundError("missing"))
        handler._timetable_api_error(TimetableWriteDisabledError("disabled"))
        handler._timetable_api_error(TimetableConflictError("conflict"))
        handler._timetable_api_error(ValueError("invalid"))

        self.assertEqual(
            handler._api_error.call_args_list,
            [
                mock.call(404, "missing"),
                mock.call(403, "disabled"),
                mock.call(409, "conflict"),
                mock.call(400, "invalid"),
            ],
        )

    def test_timetable_confirmation_routes_require_json_body(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler._api_error = mock.Mock()
        handler._write_json = mock.Mock()
        state = mock.Mock()
        state.timetable_confirmation_apply.side_effect = TimetableWriteDisabledError(
            "disabled"
        )
        web_panel.STATE = state
        handler.path = (
            "/api/storage-v2/timetable/classifications/7/confirmation/apply"
        )
        payload = json.dumps(
            {
                "expected_count": 1,
                "expected_plan_sha256": "a" * 64,
                "allow_write": True,
            }
        ).encode("utf-8")
        handler.headers = {
            "Content-Type": "text/plain",
            "Content-Length": str(len(payload)),
        }
        handler.rfile = io.BytesIO(payload)

        handler.do_POST()

        handler._api_error.assert_called_once_with(
            400,
            "Content-Type must be application/json",
        )
        handler._write_json.assert_not_called()

    def test_timetable_status_route_requires_json_body(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        state = mock.Mock()
        state.timetable_classification_status_update = mock.Mock()
        web_panel.STATE = state
        handler._api_error = mock.Mock()
        handler._write_json = mock.Mock()
        payload = json.dumps({"status": "rejected", "allow_write": True}).encode(
            "utf-8"
        )
        handler.path = "/api/storage-v2/timetable/classifications/7/status"
        handler.headers = {
            "Content-Type": "text/plain",
            "Content-Length": str(len(payload)),
        }
        handler.rfile = io.BytesIO(payload)

        handler.do_POST()

        handler._api_error.assert_called_once_with(
            400,
            "Content-Type must be application/json",
        )

    def test_timetable_status_route_rejects_invalid_allow_write_type(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        state = mock.Mock()
        handler._api_error = mock.Mock()
        handler._write_json = mock.Mock()
        state.timetable_classification_status_update = mock.Mock()
        web_panel.STATE = state
        payload = json.dumps({"status": "rejected", "allow_write": "true"}).encode(
            "utf-8"
        )
        handler.path = "/api/storage-v2/timetable/classifications/7/status"
        handler.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
        }
        handler.rfile = io.BytesIO(payload)

        handler.do_POST()

        handler._api_error.assert_called_once_with(
            400,
            "allow_write must be a boolean",
        )

    def test_timetable_confirmation_plan_allows_empty_body(self) -> None:
        state = mock.Mock()
        state.timetable_confirmation_plan.return_value = {
            "expected_count": 0,
            "mode": "read_only",
        }
        web_panel.STATE = state

        handler = object.__new__(web_panel.RequestHandler)
        handler.path = (
            "/api/storage-v2/timetable/classifications/7/confirmation/plan"
        )
        handler.headers = {"Content-Length": "0"}
        handler.rfile = io.BytesIO(b"")
        handler._write_json = mock.Mock()
        handler._api_error = mock.Mock()

        handler.do_POST()

        state.timetable_confirmation_plan.assert_called_once_with(7)
        handler._write_json.assert_called_once_with(
            200,
            {"expected_count": 0, "mode": "read_only"},
        )
        handler._api_error.assert_not_called()
