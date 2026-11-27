"""Exercise the recovery routes through the same HTTP and permission gates as the panel."""
import json
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch

from lecture_stt.ui import web_panel


BASE = "/api/postprocess/recovery"


class PanelRecoveryHttpTests(unittest.TestCase):
    def setUp(self):
        self.state = SimpleNamespace(config_path=Path("config.yaml"), db_path=Path("jobs.sqlite3"))
        self.enterContext(patch.object(web_panel, "STATE", self.state))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_panel.RequestHandler)
        port = self.server.server_port
        self.server.allowed_hosts = {f"127.0.0.1:{port}"}
        self.server.loopback_listener = True
        self.server.shared_mode = False
        self.server.control_token = "test-operator"
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.confirmation = {"course_code": "CS201", "source_txt_sha256": "a"*64, "source_json_sha256": "b"*64}

    def request(self, method, path, payload=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request(method, path, None if payload is None else json.dumps(payload),
                         {"Content-Type": "application/json", **(headers or {})})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_list_and_page_bounds(self):
        with patch.object(web_panel.panel_recovery, "list_recovery", return_value={"enabled": False, "total": 0, "items": []}) as read:
            status, result = self.request("GET", BASE)
            self.assertEqual((status, result["enabled"]), (200, False))
            read.assert_called_once_with(self.state.config_path, self.state.db_path, view="missing", limit=20, offset=0)
            self.request("GET", BASE + "?view=confirmed&limit=10&offset=20")
            self.assertEqual(read.call_args.kwargs, {"view": "confirmed", "limit": 10, "offset": 20})
            for query in ("view=all", "view=missing&view=confirmed", "limit=0", "limit=101", "offset=100001", "batch=true"):
                with self.subTest(query=query):
                    self.assertEqual(self.request("GET", BASE + "?" + query)[0], 400)
            self.assertEqual(read.call_count, 2)

    def test_read_only_can_track_without_confirmation_fields_and_cannot_write(self):
        self.server.shared_mode = True
        detail = {"job": {"id": 7}, "courses": [{"code": "CS201", "name": "자료구조"}], "can_requeue": True,
                  "source_txt_sha256": "a"*64, "source_json_sha256": "b"*64}
        target = BASE + "/7"
        with (
            patch.object(web_panel.panel_recovery, "get_recovery", return_value=detail),
            patch.object(web_panel.panel_recovery, "apply_recovery", return_value={"id": 7, "status": "PENDING"}) as apply,
        ):
            status, public = self.request("GET", target)
            self.assertEqual((status, public["job"]["id"], public["can_requeue"]), (200, 7, False))
            self.assertIsNone(public["source_txt_sha256"])
            self.assertIsNone(public["source_json_sha256"])
            self.assertEqual(public["courses"], [])
            self.assertEqual(self.request("POST", target + "/requeue", self.confirmation)[0], 403)
            operator = {"Authorization": "Bearer test-operator"}
            self.assertEqual(self.request("GET", target, headers=operator), (200, detail))
            self.assertEqual(self.request("POST", target + "/requeue", self.confirmation,
                                          {**operator, "Origin": "https://example.invalid"})[0], 403)
            apply.assert_not_called()
            self.assertEqual(self.request("POST", target + "/requeue", self.confirmation, operator),
                             (200, {"id": 7, "status": "PENDING"}))
            apply.assert_called_once_with(self.state.config_path, self.state.db_path, job_id=7, **self.confirmation)

    def test_only_exact_single_job_confirmations_reach_the_service(self):
        with patch.object(web_panel.panel_recovery, "apply_recovery") as apply:
            for suffix in ("/0/requeue", "/01/requeue", "/7/requeue/extra", "/7", "/7/requeue?batch=1", "/9007199254740992/requeue"):
                with self.subTest(suffix=suffix):
                    self.assertIn(self.request("POST", BASE + suffix, self.confirmation)[0], (400, 404))
            for payload in ({}, {**self.confirmation, "course_code": " "},
                            {**self.confirmation, "source_txt_sha256": "bad"},
                            {**self.confirmation, "source_json_sha256": None}, {**self.confirmation, "allow_all": True}):
                with self.subTest(payload=payload):
                    self.assertEqual(self.request("POST", BASE + "/7/requeue", payload)[0], 400)
            apply.assert_not_called()

    def test_expected_failures_are_distinguishable_and_configuration_paths_are_private(self):
        service = web_panel.panel_recovery
        for exc, status in ((service.RecoveryDisabledError(), 403), (service.RecoveryNotFoundError(), 404),
                            (service.RecoveryConflictError("원전사가 변경되었습니다."), 409),
                            (OSError("/private/test-fixture"), 503)):
            with self.subTest(exc=type(exc).__name__), patch.object(service, "apply_recovery", side_effect=exc):
                actual, body = self.request("POST", BASE + "/7/requeue", self.confirmation)
                self.assertEqual(actual, status)
                self.assertFalse(body["ok"])
                self.assertNotIn("/private/", body["error"])
