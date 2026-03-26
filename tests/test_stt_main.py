from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.stt import main as stt_main  # noqa: E402


class SttMainControlCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.db_path = self.root / "custom-state" / "jobs.sqlite3"
        self.pause_path = self.db_path.parent / "paused"
        self.fallback_pause = self.root / "default-state" / "paused"
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(
            (
                "paths:\n"
                f"  db_path: {self.db_path}\n"
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _run_main(self, *args: str) -> str:
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["lecture-stt", *args]),
            mock.patch.object(stt_main.utils, "pause_flag_path", return_value=self.fallback_pause),
            redirect_stdout(stdout),
        ):
            stt_main.main()
        return stdout.getvalue()

    def test_status_uses_configured_pause_path(self) -> None:
        output = self._run_main("--status", "--config", str(self.config_path))
        self.assertIn(f"Pause flag: {self.pause_path}", output)
        self.assertNotIn(str(self.fallback_pause), output)

    def test_pause_and_resume_use_configured_pause_path(self) -> None:
        pause_output = self._run_main("--pause", "--config", str(self.config_path))
        self.assertTrue(self.pause_path.exists())
        self.assertFalse(self.fallback_pause.exists())
        self.assertIn(f"Paused: pause flag created at {self.pause_path}", pause_output)

        resume_output = self._run_main("--resume", "--config", str(self.config_path))
        self.assertFalse(self.pause_path.exists())
        self.assertFalse(self.fallback_pause.exists())
        self.assertIn(f"Resumed: pause flag removed at {self.pause_path}", resume_output)

    def test_status_resolves_env_backed_db_path(self) -> None:
        env_db_path = self.root / "env-state" / "jobs.sqlite3"
        self.config_path.write_text(
            (
                "paths:\n"
                "  db_path: ${STATE_ROOT}/jobs.sqlite3\n"
            ),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"STATE_ROOT": str(env_db_path.parent)}, clear=False):
            output = self._run_main("--status", "--config", str(self.config_path))

        self.assertIn(f"Pause flag: {env_db_path.parent / 'paused'}", output)
