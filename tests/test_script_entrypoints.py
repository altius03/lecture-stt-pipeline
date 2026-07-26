from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
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

    def test_cleanup_tmp_preserves_inbox_staging_directory(self) -> None:
        module = _load_module("cleanup_script", REPO_ROOT / "scripts" / "cleanup.py")
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_root = Path(temp_dir)
            staging_dir = tmp_root / "inbox_staging"
            staging_dir.mkdir(parents=True, exist_ok=True)
            (staging_dir / "claimed.m4a").write_bytes(b"audio")
            (tmp_root / "old.tmp").write_text("x", encoding="utf-8")

            deleted = module._cleanup_tmp(tmp_root, dry_run=False)

            self.assertEqual(deleted, 1)
            self.assertTrue(staging_dir.exists())
            self.assertTrue((staging_dir / "claimed.m4a").exists())

    def test_cleanup_transcripts_keeps_scorecard_with_latest_transcript_set(self) -> None:
        module = _load_module("cleanup_script", REPO_ROOT / "scripts" / "cleanup.py")
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript_dir = Path(temp_dir)
            now = time.time()

            for index, name in enumerate(
                [
                    "old_lecture.txt",
                    "old_lecture.json",
                    "old_lecture.quality.json",
                    "new_lecture.txt",
                    "new_lecture.json",
                    "new_lecture.quality.json",
                ]
            ):
                path = transcript_dir / name
                path.write_text("{}", encoding="utf-8")
                # Make the new lecture set newer than the old set, while still
                # older than the cutoff so min_keep is the only thing preserving it.
                mtime = now - 600 + index
                if name.startswith("new_lecture"):
                    mtime = now - 60 + index
                os.utime(path, (mtime, mtime))

            deleted, kept = module._cleanup_transcripts(
                transcript_dir,
                cutoff_ts=now + 1,
                dry_run=False,
                min_keep=1,
            )

            self.assertEqual(deleted, 3)
            self.assertEqual(kept, 3)
            self.assertFalse((transcript_dir / "old_lecture.txt").exists())
            self.assertFalse((transcript_dir / "old_lecture.json").exists())
            self.assertFalse((transcript_dir / "old_lecture.quality.json").exists())
            self.assertTrue((transcript_dir / "new_lecture.txt").exists())
            self.assertTrue((transcript_dir / "new_lecture.json").exists())
            self.assertTrue((transcript_dir / "new_lecture.quality.json").exists())

    def test_cleanup_transcripts_orphan_scorecard_does_not_consume_min_keep(self) -> None:
        module = _load_module("cleanup_script", REPO_ROOT / "scripts" / "cleanup.py")
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript_dir = Path(temp_dir)
            now = time.time()

            for index, name in enumerate(
                [
                    "real_lecture.txt",
                    "real_lecture.json",
                    "real_lecture.quality.json",
                    "orphan.quality.json",
                ]
            ):
                path = transcript_dir / name
                path.write_text("{}", encoding="utf-8")
                mtime = now - 600 + index
                if name == "orphan.quality.json":
                    mtime = now - 60
                os.utime(path, (mtime, mtime))

            deleted, kept = module._cleanup_transcripts(
                transcript_dir,
                cutoff_ts=now + 1,
                dry_run=False,
                min_keep=1,
            )

            self.assertEqual(deleted, 1)
            self.assertEqual(kept, 3)
            self.assertTrue((transcript_dir / "real_lecture.txt").exists())
            self.assertTrue((transcript_dir / "real_lecture.json").exists())
            self.assertTrue((transcript_dir / "real_lecture.quality.json").exists())
            self.assertFalse((transcript_dir / "orphan.quality.json").exists())

    def test_cleanup_preserves_unresolved_needs_review_artifacts(self) -> None:
        module = _load_module("cleanup_script", REPO_ROOT / "scripts" / "cleanup.py")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio_dir = root / "audio"
            transcript_dir = root / "transcripts"
            audio_dir.mkdir()
            transcript_dir.mkdir()
            db_path = root / "jobs.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE jobs (canonical_base TEXT, status TEXT NOT NULL)"
                )
                conn.executemany(
                    "INSERT INTO jobs (canonical_base, status) VALUES (?, ?)",
                    [
                        ("protected", "NEEDS_REVIEW"),
                        ("expired", "DONE"),
                    ],
                )

            now = time.time()
            for directory, names in (
                (audio_dir, ["protected.m4a", "expired.m4a"]),
                (
                    transcript_dir,
                    [
                        "protected.txt",
                        "protected.json",
                        "protected.quality.json",
                        "expired.txt",
                        "expired.json",
                        "expired.quality.json",
                    ],
                ),
            ):
                for name in names:
                    path = directory / name
                    path.write_text("{}", encoding="utf-8")
                    os.utime(path, (now - 600, now - 600))

            protected = module._needs_review_stems(db_path)
            removed_audio, _ = module._cleanup_audio(
                audio_dir,
                cutoff_ts=now,
                dry_run=False,
                protected_stems=protected,
            )
            removed_transcripts, _ = module._cleanup_transcripts(
                transcript_dir,
                cutoff_ts=now,
                dry_run=False,
                min_keep=0,
                protected_stems=protected,
            )

            self.assertEqual(protected, {"protected"})
            self.assertEqual(removed_audio, 1)
            self.assertEqual(removed_transcripts, 3)
            self.assertTrue((audio_dir / "protected.m4a").exists())
            self.assertTrue((transcript_dir / "protected.txt").exists())
            self.assertTrue((transcript_dir / "protected.json").exists())
            self.assertTrue((transcript_dir / "protected.quality.json").exists())
            self.assertFalse((audio_dir / "expired.m4a").exists())
            self.assertFalse((transcript_dir / "expired.txt").exists())

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
