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

from lecture_stt.stt.transcribe import EngineParams, STTWorker


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

    def worker(self):
        return STTWorker(EngineParams("tiny", "cpu", "int8", "ko", "transcribe", 1, False, False),
                         "/usr/bin/true", self.paths["tmp_dir"])

    def generated_wav(self, source, name):
        worker = self.worker()
        wav = worker.preprocess(source, name)
        # Simulate process exit before the normal cleanup hook.
        worker._temporary_wavs.pop(wav).handle.close()
        os.utime(wav, (1, 1))
        return wav

    def register_retired_job(self, audio, name="lecture"):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO jobs VALUES(1,'DONE',?,?,NULL,NULL,?)",
                         (name, str(audio), hashlib.sha256(audio.read_bytes()).hexdigest()))
            conn.execute("INSERT INTO transcript_postprocess_jobs VALUES(1,'DELIVERED')")

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

    def test_retain_unowned_collision_and_release_exact_worker_wav(self):
        audio = self.file("audio/lecture.m4a")
        self.register_retired_job(audio)
        unrelated = self.file("tmp/lecture.wav")
        unrelated.write_bytes(b"unclaimed recording")
        os.utime(unrelated, (1, 1))
        wav = self.generated_wav(audio, "lecture")
        self.assertNotEqual(wav, unrelated)
        preview = self.cleanup("--dry-run")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn(str(wav), preview.stdout)
        self.assertNotIn(str(unrelated), preview.stdout)
        self.assertTrue(wav.exists())
        apply = self.cleanup("--apply")
        self.assertEqual(apply.returncode, 0, apply.stderr)
        self.assertFalse(wav.exists())
        self.assertEqual(unrelated.read_bytes(), b"unclaimed recording")
        self.assertEqual(audio.stat().st_nlink, 1)

    def test_cleanup_skips_an_active_wav_even_when_old(self):
        audio = self.file("audio/lecture.m4a")
        self.register_retired_job(audio)
        worker = self.worker()
        wav = worker.preprocess(audio, "lecture")
        self.addCleanup(worker.cleanup_tmp, wav)
        os.utime(wav, (1, 1))
        result = self.cleanup("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(wav.exists())

    def test_unpaired_proof_and_extra_link_are_preserved(self):
        audio = self.file("audio/lecture.m4a")
        self.register_retired_job(audio)
        missing_wav = self.generated_wav(audio, "lecture")
        lone_witness, = missing_wav.parent.glob("owner-*")
        missing_wav.unlink()
        missing_witness = self.generated_wav(audio, "lecture")
        witness, = missing_witness.parent.glob("owner-*")
        witness.unlink()
        shared = self.generated_wav(audio, "lecture")
        os.link(shared, self.root / "other-copy.wav")
        result = self.cleanup("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        for retained in (lone_witness, missing_witness, shared):
            self.assertTrue(retained.exists())

    def test_other_source_and_legacy_untracked_files_are_preserved(self):
        audio = self.file("audio/lecture.m4a")
        self.register_retired_job(audio)
        wav = self.generated_wav(self.file("other.m4a"), "lecture")
        legacy = self.file("tmp/lecture.wav")
        result = self.cleanup("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(wav.exists())
        self.assertTrue(legacy.exists())

    def test_replaced_worker_output_is_preserved(self):
        audio = self.file("audio/lecture.m4a")
        self.register_retired_job(audio)
        wav = self.generated_wav(audio, "lecture")
        replacement = self.file("original.wav")
        replacement.replace(wav)
        apply = self.cleanup("--apply")
        self.assertEqual(apply.returncode, 0, apply.stderr)
        self.assertEqual(wav.read_bytes(), b"sample")

    def test_refuse_overlap_with_inbox_or_errors(self):
        for key in ("watch_folder", "error_folder"):
            for protected in (self.root / "tmp", self.root / "tmp" / "child", self.root):
                with self.subTest(key=key, protected=protected):
                    config = {"paths": {**self.paths, key: str(protected), "db_path": str(self.db)}}
                    self.config.write_text(yaml.safe_dump(config))
                    self.assertNotEqual(self.cleanup("--apply").returncode, 0)

    def test_unverified_or_shared_sources_keep_last_wav(self):
        for name, issue in (("missing", "missing"), ("changed", "changed"), ("shared", "shared")):
            audio = self.file(f"audio/{name}.m4a")
            wav = self.generated_wav(audio, name)
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
                wav = self.generated_wav(audio, name)
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
