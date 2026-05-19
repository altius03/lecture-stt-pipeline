from __future__ import annotations

from pathlib import Path
import plistlib
import unittest

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_ROOT_PLACEHOLDER = "__HOME__/Library/Logs/lecture_stt"


class RuntimeMigrationConfigTests(unittest.TestCase):
    def test_example_config_uses_macos_runtime_dirs_without_changing_db_or_model(self) -> None:
        config = yaml.safe_load((REPO_ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8"))

        self.assertEqual(config["paths"]["tmp_dir"], "~/Library/Caches/lecture_stt/tmp")
        self.assertEqual(config["paths"]["db_path"], "state/jobs.sqlite3")
        self.assertEqual(config["logging"]["file"], "~/Library/Logs/lecture_stt/app.log")
        self.assertEqual(config["downstream"]["log_jsonl_path"], "~/Library/Logs/lecture_stt/downstream.jsonl")
        self.assertEqual(config["downstream"]["lock_path"], "state/downstream.lock")
        self.assertEqual(config["transcribe"]["model_size"], "large-v3")

    def test_launchd_templates_write_stdout_and_stderr_to_user_log_dir(self) -> None:
        expected = {
            "com.geonha.lecture-stt.plist": ("launchd.out.log", "launchd.err.log"),
            "com.geonha.lecture-stt-cleanup.plist": ("cleanup.out.log", "cleanup.err.log"),
            "com.geonha.lecture-stt-distribute.plist": ("downstream.out.log", "downstream.err.log"),
            "com.geonha.lecture-stt-webpanel.plist": ("webpanel.out.log", "webpanel.err.log"),
        }

        for filename, (stdout_name, stderr_name) in expected.items():
            with self.subTest(filename=filename):
                with (REPO_ROOT / "launchd" / filename).open("rb") as handle:
                    payload = plistlib.load(handle)
                self.assertEqual(payload["StandardOutPath"], f"{LOG_ROOT_PLACEHOLDER}/{stdout_name}")
                self.assertEqual(payload["StandardErrorPath"], f"{LOG_ROOT_PLACEHOLDER}/{stderr_name}")

    def test_setup_launchd_renders_home_placeholder_for_absolute_launchd_log_paths(self) -> None:
        script = (REPO_ROOT / "scripts" / "setup_launchd.sh").read_text(encoding="utf-8")

        self.assertIn('home_placeholder = "__HOME__"', script)
        self.assertIn('value.replace(repo_placeholder, repo).replace(home_placeholder, home)', script)
        self.assertIn('mkdir -p "$HOME/Library/Logs/lecture_stt"', script)


if __name__ == "__main__":
    unittest.main()
