from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import yaml

from scripts import download_model, verify_web_panel


class InstallationTests(unittest.TestCase):
    def test_custom_paths_and_model_match_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {key: str(root / key) for key in ("watch_folder", "transcript_folder",
                     "stable_audio_folder", "error_folder", "tmp_dir", "db_path")}
            config = root / "config.yaml"
            config.write_text(yaml.safe_dump({
                "paths": paths, "logging": {"file": str(root / "logs/app.log")},
                "ffmpeg": {"binary_path": "/bin/sh"},
                "transcribe": {"model_size": "tiny", "device": "cpu", "compute_type": "float32"},
                "transcript_delivery": {"correction_staging_dir": str(root / "custom-correction"),
                                        "summary_staging_dir": str(root / "custom-summary")},
            }))
            with patch.object(download_model, "WhisperModel") as model:
                download_model.prepare(str(config), create_folders=True)
            model.assert_called_once_with("tiny", device="cpu", compute_type="float32")
            for name in ("watch_folder", "stable_audio_folder", "transcript_folder", "error_folder",
                         "tmp_dir", "custom-correction", "custom-summary", "logs"):
                self.assertTrue((root / name).is_dir(), name)

    def test_health_requires_installed_assets_and_panel_state(self):
        pages = {}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = pages.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as tmp, HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                dist = Path(tmp)
                (dist / "assets").mkdir()
                (dist / "index.html").write_bytes(b'<script type="module" src="/assets/app.js"></script>')
                (dist / "assets/app.js").write_bytes(b"console.log(1)")
                pages.update({"/": b'<script type="module" src="/assets/app.js"></script>', "/assets/app.js": b"console.log(1)",
                              "/api/state": json.dumps({"schema_version": 2, "runtime_state": {},
                                    "actions": {"endpoints": {"state": "/api/state"}}}).encode()})
                verify_web_panel.verify(dist, server.server_port)
                for path, value in (("/", b"placeholder"), ("/api/state", b"{}"), ("/assets/app.js", None)):
                    with self.subTest(path=path), patch.dict(pages, {path: value}), self.assertRaises(ValueError):
                        verify_web_panel.verify(dist, server.server_port)
            finally:
                server.shutdown()
                thread.join()

    def test_setup_validates_build_before_registration_and_reports_health_failures(self):
        for failure in ("python", "build", "health", ""):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "checkout"
                (root / "scripts").mkdir(parents=True)
                (root / "config").mkdir()
                (root / "frontend/web-panel").mkdir(parents=True)
                (root / ".venv/bin").mkdir(parents=True)
                shutil.copytree(download_model.ROOT / "launchd", root / "launchd")
                shutil.copy2(download_model.ROOT / "scripts/setup_launchd.sh", root / "scripts")
                (root / "config/config.yaml").write_text("app: {execution_owner: python}\n")
                (root / "scripts/download_model.py").write_text(
                    "import sys\nassert '--create-folders' in sys.argv\n"
                )
                (root / "scripts/verify_web_panel.py").write_text(
                    "import os, sys\nsys.exit(int(os.environ['FAIL_STEP'] == 'health'))\n"
                )
                commands = Path(tmp) / "commands"
                commands.mkdir()
                wrappers = {
                    root / ".venv/bin/python": (
                        'if [ "$1" = "-c" ] && [ "$FAIL_STEP" = python ]; then exit 1; fi\n'
                        f'if [ "$1" = "-m" ]; then exit 0; fi\nexec "{sys.executable}" "$@"'
                    ),
                    commands / "npm": 'if [ "$1" = "run" ] && [ "$FAIL_STEP" = "build" ]; then exit 1; fi\nexit 0',
                    commands / "launchctl": 'echo "$1" >> "$CAPTURE"\nif [ "$1" = print ]; then echo "pid = 123"; fi',
                    commands / "ffmpeg": "exit 0",
                    commands / "sleep": "exit 0",
                    commands / "tailscale": "exit 0",
                }
                for script, command in wrappers.items():
                    script.write_text("#!/bin/sh\n" + command + "\n")
                    script.chmod(0o700)
                capture = Path(tmp) / "calls"
                env = {**os.environ, "HOME": str(Path(tmp) / "user"),
                       "PATH": str(commands) + os.pathsep + os.environ["PATH"],
                       "FAIL_STEP": failure, "CAPTURE": str(capture), "FFMPEG_BINARY": str(commands / "ffmpeg")}
                result = subprocess.run(["bash", str(root / "scripts/setup_launchd.sh")],
                                        env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, not failure, result.stdout + result.stderr)
                if failure in {"build", "python"}:
                    self.assertFalse(capture.exists())
                else:
                    self.assertEqual(capture.read_text().splitlines().count("bootstrap"), 4)
                    self.assertNotIn("kickstart", capture.read_text())
                self.assertEqual("설치 완료!" in result.stdout, not failure)
