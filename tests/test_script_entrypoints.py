from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScriptEntrypointTests(unittest.TestCase):
    def test_setup_launchd_refuses_controller_execution_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "scripts").mkdir()
            (root / "config").mkdir()
            (root / ".venv" / "bin").mkdir(parents=True)
            shutil.copy2(REPO_ROOT / "scripts" / "setup_launchd.sh", root / "scripts" / "setup_launchd.sh")
            (root / "config" / "config.yaml").write_text(
                "app:\n  execution_owner: controller\n", encoding="utf-8"
            )
            python_wrapper = root / ".venv" / "bin" / "python"
            python_wrapper.write_text(
                f'#!/bin/sh\nif [ "$1" = "-m" ]; then exit 0; fi\nexec "{sys.executable}" "$@"\n',
                encoding="utf-8",
            )
            python_wrapper.chmod(0o700)
            override_config = root / "other-config.yaml"
            override_config.write_text("app:\n  execution_owner: python\n", encoding="utf-8")
            env = dict(os.environ)
            env["HOME"] = str(root / "home")
            env["LECTURE_STT_CONFIG"] = str(override_config)
            result = subprocess.run(
                ["bash", str(root / "scripts" / "setup_launchd.sh")],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("execution_owner=controller", result.stdout)
            self.assertFalse((root / "home" / "Library" / "LaunchAgents").exists())

    def test_cleanup_script_runs_without_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio_dir = root / "audio"
            transcript_dir = root / "transcripts"
            tmp_dir = root / "tmp"
            audio_dir.mkdir(parents=True, exist_ok=True)
            transcript_dir.mkdir(parents=True, exist_ok=True)
            tmp_dir.mkdir(parents=True, exist_ok=True)

            config_path = root / "config.yaml"
            config_path.write_text(
                (
                    "paths:\n"
                    f"  stable_audio_folder: {audio_dir}\n"
                    f"  transcript_folder: {transcript_dir}\n"
                    f"  tmp_dir: {tmp_dir}\n"
                    f"  db_path: {root / 'jobs.sqlite3'}\n"
                ),
                encoding="utf-8",
            )

            env = dict(os.environ)
            env["PYTHONPATH"] = ""
            python_bin = REPO_ROOT / ".venv" / "bin" / "python"
            if not python_bin.exists():
                python_bin = Path(sys.executable)

            result = subprocess.run(
                [
                    str(python_bin),
                    str(REPO_ROOT / "scripts" / "cleanup.py"),
                    "--dry-run",
                    "--config",
                    str(config_path),
                ],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("dry_run=True", result.stdout)

    def test_ab_test_resolves_ffmpeg_from_path(self) -> None:
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            self.skipTest("ffmpeg not available on PATH")

        module = _load_module("ab_test_script", REPO_ROOT / "scripts" / "ab_test.py")
        self.assertEqual(module.resolve_ffmpeg_path("ffmpeg"), ffmpeg_path)

    def test_rotate_logs_script_is_dry_run_by_default(self) -> None:
        module = _load_module("rotate_logs_script", REPO_ROOT / "scripts" / "rotate_logs.py")
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "launchd.out.log"
            log_path.write_text("x" * 128, encoding="utf-8")

            code = module.main(["--path", str(log_path), "--max-bytes", "1", "--backup-count", "3"])

            self.assertEqual(code, 0)
            self.assertEqual(log_path.read_text(encoding="utf-8"), "x" * 128)
            self.assertFalse((Path(temp_dir) / "launchd.out.log.1").exists())

    def test_rotate_logs_script_apply_rotates_explicit_path(self) -> None:
        module = _load_module("rotate_logs_script", REPO_ROOT / "scripts" / "rotate_logs.py")
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "launchd.err.log"
            log_path.write_text("x" * 128, encoding="utf-8")

            code = module.main(
                ["--path", str(log_path), "--max-bytes", "1", "--backup-count", "3", "--apply"]
            )

            self.assertEqual(code, 0)
            self.assertEqual(log_path.read_text(encoding="utf-8"), "")
            self.assertEqual((Path(temp_dir) / "launchd.err.log.1").read_text(encoding="utf-8"), "x" * 128)
