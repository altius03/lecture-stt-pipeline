from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "rotate_logs.py"


def _load_rotate_logs_module():
    spec = importlib.util.spec_from_file_location("rotate_logs_script_under_test", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RotateLogsScriptTests(unittest.TestCase):
    def test_default_launchd_log_paths_use_macos_user_log_dir(self) -> None:
        module = _load_rotate_logs_module()

        with mock.patch("lecture_stt.shared.paths.Path.home", return_value=Path("/Users/example")):
            paths = module._default_launchd_log_paths()

        log_root = Path("/Users/example/Library/Logs/lecture_stt")
        self.assertEqual(
            paths,
            [
                log_root / "launchd.out.log",
                log_root / "launchd.err.log",
                log_root / "downstream.out.log",
                log_root / "downstream.err.log",
                log_root / "cleanup.out.log",
                log_root / "cleanup.err.log",
                log_root / "webpanel.out.log",
                log_root / "webpanel.err.log",
            ],
        )


if __name__ == "__main__":
    unittest.main()
