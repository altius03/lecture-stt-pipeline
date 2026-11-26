import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cleanup.py"


class RetentionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "jobs.db"
        self.config = self.root / "config.yaml"
        self.paths = {key: str(self.root / name) for key, name in (
            ("stable_audio_folder", "audio"), ("transcript_folder", "text"), ("tmp_dir", "tmp")
        )}
        for value in self.paths.values():
            Path(value).mkdir()
        self.config.write_text(yaml.safe_dump({
            "paths": {**self.paths, "db_path": str(self.db)},
            "cleanup": {"retain_days": 7, "retain_min_transcripts": 0},
            "transcript_delivery": {"enabled": True},
        }))
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, status, canonical_base, canonical_audio_path, transcript_txt_path, transcript_json_path, sha256)")
            conn.execute("CREATE TABLE transcript_postprocess_jobs(source_job_id INTEGER, status TEXT)")

    def file(self, relative, old=True):
        path = self.root / relative
        path.write_bytes(b"sample")
        if old:
            os.utime(path, (1, 1))
        return path

    def cleanup(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "--config", str(self.config), *args],
                              text=True, capture_output=True)

    def test_missing_db_abort_apply_and_keep_original(self):
        self.db.unlink()
        audio = self.file("audio/unclaimed.m4a")
        self.assertNotEqual(self.cleanup("--apply").returncode, 0)
        self.assertTrue(audio.exists())
        self.assertFalse(self.db.exists())

    def test_same_lock_as_worker_blocks_apply(self):
        audio = self.file("audio/old.m4a")
        with (self.root / "stt.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.assertNotEqual(self.cleanup("--apply").returncode, 0)
        self.assertTrue(audio.exists())

    def test_unverified_or_shared_sources_keep_last_wav(self):
        for name, issue in (("missing", "missing"), ("changed", "changed"), ("shared", "shared")):
            wav = self.file(f"tmp/{name}.wav")
            audio = self.file(f"audio/{name}.m4a")
            values = (1, "DONE", name, str(audio), None, None, hashlib.sha256(audio.read_bytes()).hexdigest())
            with sqlite3.connect(self.db) as conn:
                conn.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)", values)
                conn.execute("INSERT OR REPLACE INTO transcript_postprocess_jobs VALUES (1,'DELIVERED')")
                if issue == "shared":
                    conn.execute("INSERT INTO jobs VALUES (2,'PENDING',?,?,NULL,NULL,NULL)", (name, str(audio)))
            if issue == "missing":
                audio.unlink()
            if issue == "changed":
                audio.write_bytes(b"replacement")
            result = self.cleanup("--apply")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(wav.exists())

    def test_historical_transcripts_and_staging_survive_apply(self):
        for relative in ("text/a/class", "text/b/class", "tmp/inbox_staging"):
            directory = self.root / relative
            directory.mkdir(parents=True)
            for name in ("lecture.txt", "lecture.json", "lecture.quality.json", "orphan.quality.json"):
                self.file(f"{relative}/{name}")
        files = set(p for p in self.root.rglob("*") if p.is_file())
        result = self.cleanup("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(p.is_file() for p in files))

    def test_only_retired_paths_and_stale_worker_temp_can_be_deleted(self):
        rows = [("PENDING", None), ("PROCESSING", None), ("NEEDS_REVIEW", None),
                ("ERROR", None), ("DONE", None), ("DONE", "SKIPPED"),
                ("DONE", "CONFLICT"), ("DONE", "PENDING"), ("DONE", "DELIVERED")]
        preserved = [self.file("audio/unclaimed.m4a"), self.file("text/manual.md"),
                     self.file("tmp/recent.wav", old=False)]
        eligible = []
        with sqlite3.connect(self.db) as conn:
            for idx, (status, delivery) in enumerate(rows, 1):
                name = f"lecture{idx}"
                audio = self.file(f"audio/{name}.m4a")
                txt = self.file(f"text/{name}.txt")
                js = self.file(f"text/{name}.json")
                sidecar = self.file(f"text/{name}.quality.json")
                wav = self.file(f"tmp/{name}.wav")
                conn.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                             (idx, status, name, str(audio), str(txt), str(js), hashlib.sha256(audio.read_bytes()).hexdigest()))
                if delivery:
                    conn.execute("INSERT INTO transcript_postprocess_jobs VALUES(?,?)", (idx, delivery))
                target = eligible if delivery == "DELIVERED" else preserved
                target.append(wav)
                preserved.extend((audio, txt, js, sidecar))
        preview = self.cleanup("--dry-run")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertTrue(all(p.exists() for p in eligible + preserved))
        result = self.cleanup("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p.name for p in preserved if not p.exists()], [])
        self.assertEqual([p.name for p in eligible if p.exists()], [])
