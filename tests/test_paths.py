from __future__ import annotations

from pathlib import Path
import unittest
from unittest import mock

import yaml

from lecture_stt.downstream import worker as downstream_worker
from lecture_stt.shared import paths
from lecture_stt.stt import main as stt_main


class RuntimePathDefaultsTests(unittest.TestCase):
    def test_macos_standard_runtime_dirs_are_under_user_library(self) -> None:
        with mock.patch("lecture_stt.shared.paths.Path.home", return_value=Path("/Users/example")):
            self.assertEqual(paths.default_log_dir(), Path("/Users/example/Library/Logs/lecture_stt"))
            self.assertEqual(paths.default_cache_dir(), Path("/Users/example/Library/Caches/lecture_stt"))
            self.assertEqual(paths.default_tmp_dir(), Path("/Users/example/Library/Caches/lecture_stt/tmp"))
            self.assertEqual(paths.default_db_path(), paths.repo_root() / "state" / "jobs.sqlite3")

    def test_stt_defaults_keep_db_in_repo_but_move_tmp_and_logs_to_user_library(self) -> None:
        with mock.patch("lecture_stt.shared.paths.Path.home", return_value=Path("/Users/example")):
            config = stt_main._ensure_config_defaults({})

        self.assertEqual(config["paths"]["db_path"], str(paths.repo_root() / "state" / "jobs.sqlite3"))
        self.assertEqual(config["paths"]["tmp_dir"], "/Users/example/Library/Caches/lecture_stt/tmp")
        self.assertEqual(config["logging"]["file"], "/Users/example/Library/Logs/lecture_stt/app.log")

    def test_downstream_default_jsonl_uses_user_log_dir_but_db_stays_in_repo(self) -> None:
        with mock.patch("lecture_stt.shared.paths.Path.home", return_value=Path("/Users/example")):
            config = downstream_worker._default_config()

        self.assertEqual(config["paths"]["db_path"], str(paths.repo_root() / "state" / "jobs.sqlite3"))
        self.assertEqual(
            config["downstream"]["log_jsonl_path"],
            "/Users/example/Library/Logs/lecture_stt/downstream.jsonl",
        )
        self.assertEqual(config["downstream"]["lock_path"], str(paths.repo_root() / "state" / "downstream.lock"))

    def test_config_example_documents_macos_runtime_defaults(self) -> None:
        example_path = paths.repo_root() / "config" / "config.example.yaml"
        config = yaml.safe_load(example_path.read_text(encoding="utf-8"))

        self.assertEqual(config["paths"]["db_path"], "state/jobs.sqlite3")
        self.assertEqual(config["paths"]["tmp_dir"], "~/Library/Caches/lecture_stt/tmp")
        self.assertEqual(config["logging"]["file"], "~/Library/Logs/lecture_stt/app.log")
        self.assertEqual(
            config["downstream"]["log_jsonl_path"],
            "~/Library/Logs/lecture_stt/downstream.jsonl",
        )


if __name__ == "__main__":
    unittest.main()
