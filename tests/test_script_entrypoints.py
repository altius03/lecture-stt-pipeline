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
                ),
                encoding="utf-8",
            )

            env = dict(os.environ)
            env["PYTHONPATH"] = ""
            result = subprocess.run(
                [
                    str(REPO_ROOT / ".venv" / "bin" / "python"),
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
