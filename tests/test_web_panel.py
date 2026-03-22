from __future__ import annotations

import unittest
from unittest import mock

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.ui import web_panel  # noqa: E402


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

        handler._action_exit(api_mode=True)

        state.request_shutdown.assert_called_once_with()
        state.stop.assert_not_called()
        handler._write_json.assert_called_once_with(
            200,
            {"ok": True, "notice": "웹 제어판 종료 요청됨"},
        )

    def test_app_route_returns_placeholder_when_react_build_is_missing(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/app"
        handler._write = mock.Mock()

        with mock.patch.object(web_panel, "_resolve_react_asset", return_value=None):
            handler.do_GET()

        handler._write.assert_called_once()
        args = handler._write.call_args[0]
        self.assertEqual(args[0], 200)
        self.assertIn("React 패널 빌드가 아직 없습니다", args[1])

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

                getattr(handler, handler_name).assert_called_once_with(api_mode=True)
                handler._api_error.assert_not_called()

    def test_api_events_route_dispatches_stream_handler(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/events"
        handler._serve_event_stream = mock.Mock()

        handler.do_GET()

        handler._serve_event_stream.assert_called_once_with()

    def test_unknown_api_path_returns_json_404(self) -> None:
        handler = object.__new__(web_panel.RequestHandler)
        handler.path = "/api/does-not-exist"
        handler._api_error = mock.Mock()

        handler.do_POST()

        handler._api_error.assert_called_once_with(404, "Not Found")
