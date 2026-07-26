from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from lecture_stt.storage_v2.manifest import (
    ManifestValidationError,
    build_manifest,
    validate_manifest,
    write_manifest,
)
from lecture_stt.storage_v2.repository import apply_migration
from lecture_stt.storage_v2.verifier import verify_library


class StorageV2VerifierTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        db_path = root / "storage-v2.sqlite3"
        records_root = root / "records"
        record_root = records_root / "rec_verifier"
        source_path = record_root / "source" / "original.m4a"
        transcript_path = record_root / "jobs" / "job_verifier" / "transcript.txt"
        source_path.parent.mkdir(parents=True)
        transcript_path.parent.mkdir(parents=True)
        source_path.write_bytes(b"fixture-audio")
        transcript_path.write_text("fixture transcript", encoding="utf-8")

        source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
        transcript_sha = hashlib.sha256(transcript_path.read_bytes()).hexdigest()
        source_bytes = source_path.stat().st_size
        transcript_bytes = transcript_path.stat().st_size

        conn = apply_migration(db_path)
        try:
            cursor = conn.execute(
                """
                INSERT INTO recordings(
                    storage_key,
                    original_name_raw,
                    original_name_nfc,
                    source_relpath,
                    ingest_sha256,
                    ingest_bytes,
                    source_mime,
                    source_state,
                    created_at
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    'available',
                    '2026-07-23T00:00:00+00:00'
                )
                """,
                (
                    "rec_verifier",
                    "검증.m4a",
                    "검증.m4a",
                    "source/original.m4a",
                    source_sha,
                    source_bytes,
                    "audio/mp4",
                ),
            )
            recording_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO recording_titles(
                    recording_id, title, title_source, locale, is_current
                )
                VALUES (?, '검증', 'legacy_import', 'ko-KR', 1)
                """,
                (recording_id,),
            )
            conn.execute(
                """
                INSERT INTO recording_contexts(
                    recording_id,
                    context_type,
                    source,
                    is_selected
                )
                VALUES (?, 'general', 'legacy_import', 1)
                """,
                (recording_id,),
            )
            cursor = conn.execute(
                """
                INSERT INTO transcription_jobs(
                    recording_id,
                    job_key,
                    job_relpath,
                    requested_profile,
                    requested_profile_version,
                    status,
                    progress,
                    is_current,
                    queued_at,
                    started_at,
                    finished_at
                )
                VALUES (
                    ?,
                    'job_verifier',
                    'jobs/job_verifier',
                    'lecture',
                    'v1',
                    'done',
                    100,
                    1,
                    '2026-07-23T00:01:00+00:00',
                    '2026-07-23T00:02:00+00:00',
                    '2026-07-23T01:00:00+00:00'
                )
                """,
                (recording_id,),
            )
            job_id = int(cursor.lastrowid)
            cursor = conn.execute(
                """
                INSERT INTO engine_runs(
                    job_id,
                    recording_id,
                    engine_name,
                    engine_version,
                    provider,
                    status,
                    is_selected,
                    started_at,
                    finished_at
                )
                VALUES (
                    ?,
                    ?,
                    'faster-whisper',
                    'large-v3',
                    'legacy_import',
                    'succeeded',
                    1,
                    '2026-07-23T00:02:00+00:00',
                    '2026-07-23T01:00:00+00:00'
                )
                """,
                (job_id, recording_id),
            )
            engine_run_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO artifacts(
                    recording_id,
                    job_id,
                    engine_run_id,
                    artifact_kind,
                    revision,
                    path_rel,
                    content_sha256,
                    bytes,
                    mime_type,
                    is_latest
                )
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, 1)
                """,
                [
                    (
                        recording_id,
                        job_id,
                        None,
                        "source_copy",
                        "source/original.m4a",
                        source_sha,
                        source_bytes,
                        "audio/mp4",
                    ),
                    (
                        recording_id,
                        job_id,
                        engine_run_id,
                        "transcript_raw_text",
                        "jobs/job_verifier/transcript.txt",
                        transcript_sha,
                        transcript_bytes,
                        "text/plain",
                    ),
                ],
            )
            conn.executemany(
                """
                INSERT INTO legacy_import_map(
                    legacy_kind,
                    legacy_key,
                    recording_id,
                    source_fingerprint,
                    legacy_snapshot_json
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        "job",
                        "41",
                        recording_id,
                        "a" * 64,
                        json.dumps(
                            {
                                "canonical_base": "260723VERIFY_1",
                                "status": "DONE",
                            },
                            sort_keys=True,
                        ),
                    ),
                    (
                        "delivery",
                        "260723VERIFY_1",
                        recording_id,
                        "a" * 64,
                        json.dumps(
                            {"logical_stem": "260723VERIFY_1"},
                            sort_keys=True,
                        ),
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        manifest = build_manifest(
            storage_key="rec_verifier",
            generated_at="2026-07-23T00:00:00+00:00",
            original_name_raw="검증.m4a",
            original_name_nfc="검증.m4a",
            recording_created_at="2026-07-23T00:00:00+00:00",
            source={
                "path": "source/original.m4a",
                "availability": "available",
                "sha256": source_sha,
                "bytes": source_bytes,
                "mime_type": "audio/mp4",
            },
            title={"value": "검증", "source": "legacy_import"},
            context={"type": "general", "source": "legacy_import"},
            jobs=[
                {
                    "job_key": "job_verifier",
                    "status": "done",
                    "requested_profile": "lecture",
                    "requested_profile_version": "v1",
                    "queued_at": "2026-07-23T00:01:00+00:00",
                    "started_at": "2026-07-23T00:02:00+00:00",
                    "finished_at": "2026-07-23T01:00:00+00:00",
                    "engine": {
                        "name": "faster-whisper",
                        "version": "large-v3",
                        "started_at": "2026-07-23T00:02:00+00:00",
                        "finished_at": "2026-07-23T01:00:00+00:00",
                        "stderr_relpath": None,
                        "log_relpath": None,
                    },
                    "artifact_paths": ["jobs/job_verifier/transcript.txt"],
                }
            ],
            artifacts=[
                {
                    "kind": "source_copy",
                    "path": "source/original.m4a",
                    "sha256": source_sha,
                    "bytes": source_bytes,
                    "mime_type": "audio/mp4",
                },
                {
                    "kind": "transcript_raw_text",
                    "path": "jobs/job_verifier/transcript.txt",
                    "sha256": transcript_sha,
                    "bytes": transcript_bytes,
                    "mime_type": "text/plain",
                },
            ],
            legacy={
                "kind": "job",
                "job_id": 41,
                "delivery_key": "260723VERIFY_1",
                "canonical_base": "260723VERIFY_1",
                "status": "DONE",
                "source_fingerprint": "a" * 64,
            },
        )
        manifest_path = record_root / "manifest.json"
        write_manifest(manifest_path, manifest)
        return db_path, records_root, manifest_path

    def _rewrite_manifest(
        self,
        manifest_path: Path,
        payload: dict[str, object],
    ) -> None:
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

    def test_accepts_manifest_bound_to_database_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, _ = self._fixture(Path(tmpdir))

            result = verify_library(db_path, records_root)

            self.assertTrue(result["ok"])
            self.assertEqual(result["checked_recordings"], 1)
            self.assertEqual(result["checked_artifacts"], 2)
            self.assertEqual(result["issues"], [])

    def test_detects_tampered_timetable_classification_review_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, _ = self._fixture(Path(tmpdir))
            with sqlite3.connect(db_path) as conn:
                review_item_id = int(
                    conn.execute(
                        """
                        INSERT INTO review_items(
                            recording_id,
                            status,
                            reason_code
                        ) VALUES (1, 'open', 'manual_review')
                        """
                    ).lastrowid
                )
                conn.execute(
                    """
                    INSERT INTO recording_classification_proposals(
                        recording_id,
                        schedule_entry_id,
                        review_item_id,
                        status,
                        classification_reason,
                        proposed_title,
                        context_type,
                        semester,
                        session_date,
                        confidence,
                        detail_json
                    ) VALUES (
                        1,
                        NULL,
                        ?,
                        'suggested',
                        'recorded_at_missing',
                        '수업 후보',
                        'general',
                        '2026-1',
                        '2026-07-23',
                        NULL,
                        '{"schema_version":"storage-v2/timetable-classification-review@1","semester":"2026-1"}'
                    )
                    """,
                    (review_item_id,),
                )
                conn.commit()

            result = verify_library(db_path, records_root)
            issue_codes = {issue["code"] for issue in result["issues"]}

            self.assertIn("classification_review_detail_invalid", issue_codes)

    def test_rejects_valid_shape_manifest_sha_that_disagrees_with_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, manifest_path = self._fixture(Path(tmpdir))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            transcript_entry = next(
                entry
                for entry in manifest["artifacts"]
                if entry["kind"] == "transcript_raw_text"
            )
            transcript_entry["sha256"] = "f" * 64
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            result = verify_library(db_path, records_root)
            codes = {issue["code"] for issue in result["issues"]}

            self.assertFalse(result["ok"])
            self.assertIn("manifest_artifact_index_mismatch", codes)
            self.assertNotIn("manifest_invalid", codes)
            self.assertNotIn("artifact_hash_mismatch", codes)

    def test_rejects_other_artifact_index_metadata_drift(self) -> None:
        mutations = {
            "kind": "correction_text",
            "path": "jobs/job_verifier/renamed.txt",
            "bytes": 999,
            "mime_type": "application/octet-stream",
        }
        for field, value in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmpdir:
                db_path, records_root, manifest_path = self._fixture(Path(tmpdir))
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                transcript_entry = next(
                    entry
                    for entry in manifest["artifacts"]
                    if entry["kind"] == "transcript_raw_text"
                )
                transcript_entry[field] = value
                if field == "path":
                    manifest["jobs"][0]["artifact_paths"] = [value]
                manifest_path.write_text(
                    json.dumps(
                        manifest,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )

                result = verify_library(db_path, records_root)
                codes = {issue["code"] for issue in result["issues"]}

                self.assertIn("manifest_artifact_index_mismatch", codes)
                self.assertNotIn("manifest_invalid", codes)

    def test_rejects_manifest_source_and_current_job_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, manifest_path = self._fixture(Path(tmpdir))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source"]["sha256"] = "e" * 64
            manifest["jobs"][0]["status"] = "needs_review"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            result = verify_library(db_path, records_root)
            codes = {issue["code"] for issue in result["issues"]}

            self.assertFalse(result["ok"])
            self.assertIn("manifest_source_mismatch", codes)
            self.assertIn("manifest_jobs_mismatch", codes)
            self.assertNotIn("manifest_artifact_index_mismatch", codes)

    def test_rejects_orphan_record_directory_not_indexed_by_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, _ = self._fixture(Path(tmpdir))
            (records_root / "rec_orphan").mkdir()

            result = verify_library(db_path, records_root)
            codes = {issue["code"] for issue in result["issues"]}

            self.assertFalse(result["ok"])
            self.assertIn("orphan_record_directory", codes)

    def test_manifest_rejects_duplicate_jobs_paths_and_source_job_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _, _, manifest_path = self._fixture(Path(tmpdir))
            base = json.loads(manifest_path.read_text(encoding="utf-8"))

            duplicate_job = copy.deepcopy(base)
            duplicate_job["jobs"].append(copy.deepcopy(duplicate_job["jobs"][0]))
            with self.assertRaisesRegex(
                ManifestValidationError,
                "Duplicate job_key",
            ):
                validate_manifest(duplicate_job)

            duplicate_path = copy.deepcopy(base)
            duplicate_path["jobs"][0]["artifact_paths"].append(
                "jobs/job_verifier/transcript.txt"
            )
            with self.assertRaisesRegex(
                ManifestValidationError,
                "Duplicate artifact path",
            ):
                validate_manifest(duplicate_path)

            source_in_job = copy.deepcopy(base)
            source_in_job["jobs"][0]["artifact_paths"].append(
                "source/original.m4a"
            )
            with self.assertRaisesRegex(
                ManifestValidationError,
                "must not include source.path",
            ):
                validate_manifest(source_in_job)

    def test_manifest_objects_reject_unknown_privacy_payload_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, manifest_path = self._fixture(Path(tmpdir))
            base = json.loads(manifest_path.read_text(encoding="utf-8"))

            target_paths = (
                "top",
                "recording",
                "source",
                "title",
                "context",
                "job",
                "engine",
                "artifact",
                "legacy",
            )
            for target_path in target_paths:
                with self.subTest(target_path=target_path):
                    payload = copy.deepcopy(base)
                    if target_path == "top":
                        target = payload
                        field = "payload"
                    elif target_path == "job":
                        target = payload["jobs"][0]
                        field = "private_payload"
                    elif target_path == "engine":
                        target = payload["jobs"][0]["engine"]
                        field = "private_payload"
                    elif target_path == "artifact":
                        target = payload["artifacts"][0]
                        field = "private_payload"
                    else:
                        target = payload[target_path]
                        field = "private_payload"
                    target[field] = {"utterances": ["PRIVATE TRANSCRIPT"]}
                    with self.assertRaisesRegex(
                        ManifestValidationError,
                        "unknown fields",
                    ):
                        validate_manifest(payload)

            leaked = copy.deepcopy(base)
            leaked["payload"] = {"utterances": ["PRIVATE TRANSCRIPT"]}
            self._rewrite_manifest(manifest_path, leaked)
            result = verify_library(db_path, records_root)
            codes = {issue["code"] for issue in result["issues"]}
            self.assertFalse(result["ok"])
            self.assertIn("manifest_invalid", codes)

    def test_manifest_rejects_oversized_metadata_scalar_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _, _, manifest_path = self._fixture(Path(tmpdir))
            base = json.loads(manifest_path.read_text(encoding="utf-8"))

            oversized_title = copy.deepcopy(base)
            oversized_title["title"]["value"] = "PRIVATE " * 1500
            with self.assertRaisesRegex(
                ManifestValidationError,
                "title.value exceeds the 512 character limit",
            ):
                validate_manifest(oversized_title)

            oversized_context = copy.deepcopy(base)
            oversized_context["context"]["label"] = "PRIVATE " * 1500
            with self.assertRaisesRegex(
                ManifestValidationError,
                "context.label exceeds the 512 character limit",
            ):
                validate_manifest(oversized_context)

            too_many_jobs = copy.deepcopy(base)
            too_many_jobs["jobs"] = [
                copy.deepcopy(base["jobs"][0])
                for _ in range(65)
            ]
            with self.assertRaisesRegex(
                ManifestValidationError,
                "jobs exceeds the 64 item limit",
            ):
                validate_manifest(too_many_jobs)

            too_many_artifacts = copy.deepcopy(base)
            too_many_artifacts["artifacts"] = [
                copy.deepcopy(base["artifacts"][0])
                for _ in range(1025)
            ]
            with self.assertRaisesRegex(
                ManifestValidationError,
                "artifacts exceeds the 1024 item limit",
            ):
                validate_manifest(too_many_artifacts)

    def test_manifest_generated_at_requires_timezone_aware_rfc3339(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _, _, manifest_path = self._fixture(Path(tmpdir))
            base = json.loads(manifest_path.read_text(encoding="utf-8"))

            valid = copy.deepcopy(base)
            valid["generated_at"] = "2026-07-23T09:30:00+00:00"
            validate_manifest(valid)

            for invalid_timestamp in (
                "PRIVATE TRANSCRIPT BODY",
                "2026-07-23T09:30:00",
            ):
                with self.subTest(generated_at=invalid_timestamp):
                    invalid = copy.deepcopy(base)
                    invalid["generated_at"] = invalid_timestamp
                    with self.assertRaisesRegex(
                        ManifestValidationError,
                        "timezone-aware RFC3339 timestamp",
                    ):
                        validate_manifest(invalid)

    def test_recursive_inventory_rejects_unindexed_and_unsafe_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path, records_root, _ = self._fixture(root)
            record_root = records_root / "rec_verifier"
            rogue_file = (
                record_root
                / "jobs"
                / "job_verifier"
                / "unindexed-private.txt"
            )
            rogue_file.write_text("PRIVATE", encoding="utf-8")
            (record_root / "unindexed-empty-directory").mkdir()

            outside_directory = root / "outside-canary"
            outside_directory.mkdir()
            (outside_directory / "must-not-be-scanned.txt").write_text(
                "outside",
                encoding="utf-8",
            )
            linked_directory = record_root / "linked-directory"
            try:
                linked_directory.symlink_to(
                    outside_directory,
                    target_is_directory=True,
                )
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            fifo_path = record_root / "unindexed.fifo"
            try:
                os.mkfifo(fifo_path)
            except OSError as exc:
                self.skipTest(f"FIFO creation is unavailable: {exc}")

            result = verify_library(db_path, records_root)
            issues = result["issues"]
            issue_pairs = {
                (issue["code"], issue.get("path"))
                for issue in issues
            }

            self.assertFalse(result["ok"])
            self.assertIn(
                ("unindexed_record_file", rogue_file.relative_to(record_root).as_posix()),
                issue_pairs,
            )
            self.assertIn(
                ("unindexed_record_directory", "unindexed-empty-directory"),
                issue_pairs,
            )
            self.assertIn(
                ("record_entry_symlink", "linked-directory"),
                issue_pairs,
            )
            self.assertIn(
                ("record_entry_special", "unindexed.fifo"),
                issue_pairs,
            )
            self.assertFalse(
                any(
                    str(issue.get("path", "")).startswith("linked-directory/")
                    for issue in issues
                )
            )

    def test_locks_directory_only_allows_empty_expected_lock_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, _ = self._fixture(Path(tmpdir))
            locks_root = records_root / ".locks"
            locks_root.mkdir()
            expected_lock = locks_root / "rec_verifier.lock"
            expected_lock.touch()

            self.assertTrue(verify_library(db_path, records_root)["ok"])

            expected_lock.write_text("unexpected content", encoding="utf-8")
            (locks_root / "rogue.lock").touch()
            (locks_root / "rogue-directory").mkdir()
            linked_lock = locks_root / "linked.lock"
            try:
                linked_lock.symlink_to(expected_lock)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            special_lock = locks_root / "special.fifo"
            try:
                os.mkfifo(special_lock)
            except OSError as exc:
                self.skipTest(f"FIFO creation is unavailable: {exc}")
            result = verify_library(db_path, records_root)
            issue_pairs = {
                (issue["code"], issue.get("path"))
                for issue in result["issues"]
            }

            self.assertFalse(result["ok"])
            self.assertIn(
                (
                    "internal_lock_file_not_empty",
                    ".locks/rec_verifier.lock",
                ),
                issue_pairs,
            )
            self.assertIn(
                ("internal_unexpected_entry", ".locks/rogue.lock"),
                issue_pairs,
            )
            self.assertIn(
                (
                    "internal_unexpected_entry",
                    ".locks/rogue-directory",
                ),
                issue_pairs,
            )
            self.assertIn(
                ("internal_unexpected_entry", ".locks/linked.lock"),
                issue_pairs,
            )
            self.assertIn(
                ("internal_unexpected_entry", ".locks/special.fifo"),
                issue_pairs,
            )

    def test_manifest_job_paths_must_match_current_database_job_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, _ = self._fixture(Path(tmpdir))
            with sqlite3.connect(db_path) as conn:
                recording_id = conn.execute(
                    "SELECT id FROM recordings WHERE storage_key = 'rec_verifier'"
                ).fetchone()[0]
                cursor = conn.execute(
                    """
                    INSERT INTO transcription_jobs(
                        recording_id,
                        job_key,
                        job_relpath,
                        status,
                        progress,
                        is_current
                    )
                    VALUES (?, 'job_other', 'jobs/job_other', 'done', 100, 0)
                    """,
                    (recording_id,),
                )
                other_job_id = int(cursor.lastrowid)
                conn.execute(
                    """
                    UPDATE artifacts
                    SET job_id = ?
                    WHERE artifact_kind = 'transcript_raw_text'
                    """,
                    (other_job_id,),
                )
                conn.commit()

            result = verify_library(db_path, records_root)
            issue = next(
                issue
                for issue in result["issues"]
                if issue["code"] == "manifest_job_artifact_paths_mismatch"
            )

            self.assertFalse(result["ok"])
            verifier_job_issue = next(
                job_issue
                for job_issue in issue["jobs"]
                if job_issue["job_key"] == "job_verifier"
            )
            self.assertEqual(
                verifier_job_issue["manifest_only_paths"],
                ["jobs/job_verifier/transcript.txt"],
            )
            self.assertEqual(verifier_job_issue["database_only_paths"], [])

    def test_manifest_job_snapshot_matches_relpath_profile_and_selected_engine(self) -> None:
        mutations = (
            (
                "job_relpath",
                "UPDATE transcription_jobs SET job_relpath = 'jobs/job_drift'",
            ),
            (
                "requested_profile",
                "UPDATE transcription_jobs SET requested_profile = 'general'",
            ),
            (
                "requested_profile_version",
                "UPDATE transcription_jobs SET requested_profile_version = 'v2'",
            ),
            (
                "queued_at",
                """
                UPDATE transcription_jobs
                SET queued_at = '2026-07-23T00:01:01+00:00'
                """,
            ),
            (
                "started_at",
                """
                UPDATE transcription_jobs
                SET started_at = '2026-07-23T00:02:01+00:00'
                """,
            ),
            (
                "finished_at",
                """
                UPDATE transcription_jobs
                SET finished_at = '2026-07-23T01:00:01+00:00'
                """,
            ),
            (
                "engine_name",
                "UPDATE engine_runs SET engine_name = 'mlx-whisper'",
            ),
            (
                "engine_version",
                "UPDATE engine_runs SET engine_version = 'large-v3-turbo'",
            ),
            (
                "engine_started_at",
                """
                UPDATE engine_runs
                SET started_at = '2026-07-23T00:02:01+00:00'
                """,
            ),
            (
                "engine_finished_at",
                """
                UPDATE engine_runs
                SET finished_at = '2026-07-23T01:00:01+00:00'
                """,
            ),
            (
                "engine_stderr_relpath",
                """
                UPDATE engine_runs
                SET stderr_relpath = 'jobs/job_verifier/stderr.log'
                """,
            ),
            (
                "engine_log_relpath",
                """
                UPDATE engine_runs
                SET log_relpath = 'jobs/job_verifier/engine.log'
                """,
            ),
        )
        for field, statement in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmpdir:
                db_path, records_root, _ = self._fixture(Path(tmpdir))
                with sqlite3.connect(db_path) as conn:
                    conn.execute(statement)
                    conn.commit()

                result = verify_library(db_path, records_root)
                codes = {issue["code"] for issue in result["issues"]}

                self.assertFalse(result["ok"])
                self.assertIn("manifest_jobs_mismatch", codes)

    def test_manifest_display_metadata_matches_database_snapshot(self) -> None:
        mutations = (
            (
                "recording",
                "UPDATE recordings SET original_name_nfc = '변조.m4a'",
                "manifest_recording_mismatch",
            ),
            (
                "recording_created_at",
                """
                UPDATE recordings
                SET created_at = '2026-07-23T00:00:01+00:00'
                """,
                "manifest_recording_mismatch",
            ),
            (
                "title",
                "UPDATE recording_titles SET title = '변조된 제목'",
                "manifest_title_mismatch",
            ),
            (
                "context",
                "UPDATE recording_contexts SET context_type = 'memo'",
                "manifest_context_mismatch",
            ),
        )
        for field, statement, expected_code in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmpdir:
                db_path, records_root, _ = self._fixture(Path(tmpdir))
                with sqlite3.connect(db_path) as conn:
                    conn.execute(statement)
                    conn.commit()

                result = verify_library(db_path, records_root)
                codes = {issue["code"] for issue in result["issues"]}

                self.assertFalse(result["ok"])
                self.assertIn(expected_code, codes)

    def test_missing_job_import_map_is_never_verified_as_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, _ = self._fixture(Path(tmpdir))
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "DELETE FROM legacy_import_map WHERE legacy_kind = 'job'"
                )
                conn.commit()

            result = verify_library(db_path, records_root)
            issue = next(
                issue
                for issue in result["issues"]
                if issue["code"] == "manifest_legacy_import_map_mismatch"
            )

            self.assertFalse(result["ok"])
            self.assertIn(
                {"kind": "job", "key": "41"},
                issue["manifest_only_keys"],
            )

    def test_delivery_import_map_requires_exact_manifest_provenance(self) -> None:
        mutations = (
            "missing",
            "unnecessary",
            "fingerprint_mismatch",
            "snapshot_mismatch",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmpdir:
                db_path, records_root, manifest_path = self._fixture(Path(tmpdir))
                if mutation == "missing":
                    with sqlite3.connect(db_path) as conn:
                        conn.execute(
                            """
                            DELETE FROM legacy_import_map
                            WHERE legacy_kind = 'delivery'
                            """
                        )
                        conn.commit()
                elif mutation == "unnecessary":
                    manifest = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                    manifest["legacy"]["delivery_key"] = None
                    self._rewrite_manifest(manifest_path, manifest)
                elif mutation == "fingerprint_mismatch":
                    with sqlite3.connect(db_path) as conn:
                        conn.execute(
                            """
                            UPDATE legacy_import_map
                            SET source_fingerprint = ?
                            WHERE legacy_kind = 'delivery'
                            """,
                            ("b" * 64,),
                        )
                        conn.commit()
                else:
                    with sqlite3.connect(db_path) as conn:
                        conn.execute(
                            """
                            UPDATE legacy_import_map
                            SET legacy_snapshot_json = ?
                            WHERE legacy_kind = 'delivery'
                            """,
                            (
                                json.dumps(
                                    {"logical_stem": "other-delivery"},
                                    sort_keys=True,
                                ),
                            ),
                        )
                        conn.commit()

                result = verify_library(db_path, records_root)
                issue = next(
                    issue
                    for issue in result["issues"]
                    if issue["code"] == "manifest_legacy_import_map_mismatch"
                )

                self.assertFalse(result["ok"])
                if mutation == "missing":
                    self.assertIn(
                        {
                            "kind": "delivery",
                            "key": "260723VERIFY_1",
                        },
                        issue["manifest_only_keys"],
                    )
                elif mutation == "unnecessary":
                    self.assertIn(
                        {
                            "kind": "delivery",
                            "key": "260723VERIFY_1",
                        },
                        issue["database_only_keys"],
                    )
                elif mutation == "fingerprint_mismatch":
                    self.assertIn(
                        {
                            "kind": "delivery",
                            "key": "260723VERIFY_1",
                        },
                        issue["fingerprint_mismatch_keys"],
                    )
                else:
                    self.assertIn(
                        {
                            "kind": "delivery",
                            "key": "260723VERIFY_1",
                        },
                        issue["snapshot_mismatch_keys"],
                    )

    def test_indexed_symlinks_are_not_followed_for_manifest_or_artifact(self) -> None:
        targets = ("manifest", "artifact")
        for target in targets:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                db_path, records_root, manifest_path = self._fixture(root)
                if target == "manifest":
                    indexed_path = manifest_path
                    outside_path = root / "outside-manifest.json"
                    outside_path.write_bytes(manifest_path.read_bytes())
                    expected_code = "manifest_unreadable"
                else:
                    indexed_path = (
                        records_root
                        / "rec_verifier"
                        / "jobs"
                        / "job_verifier"
                        / "transcript.txt"
                    )
                    outside_path = root / "outside-private.txt"
                    outside_path.write_text(
                        "PRIVATE TRANSCRIPT BODY",
                        encoding="utf-8",
                    )
                    expected_code = "artifact_unreadable"
                indexed_path.unlink()
                try:
                    indexed_path.symlink_to(outside_path)
                except OSError as exc:
                    self.skipTest(f"symlink creation is unavailable: {exc}")

                result = verify_library(db_path, records_root)
                codes = {issue["code"] for issue in result["issues"]}

                self.assertFalse(result["ok"])
                self.assertIn(expected_code, codes)
                self.assertIn("record_entry_symlink", codes)

    def test_artifact_hash_uses_the_already_opened_file_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path, records_root, _ = self._fixture(root)
            transcript_path = (
                records_root
                / "rec_verifier"
                / "jobs"
                / "job_verifier"
                / "transcript.txt"
            )
            opened_path = transcript_path.with_name("transcript-opened.txt")
            outside_path = root / "outside-private.txt"
            outside_path.write_text(
                "PRIVATE TRANSCRIPT BODY",
                encoding="utf-8",
            )
            from lecture_stt.storage_v2 import verifier as verifier_module

            real_open = verifier_module.os.open
            swapped = False

            def swap_after_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
                nonlocal swapped
                file_fd = real_open(path, flags, *args, **kwargs)
                if path == "transcript.txt" and not swapped:
                    transcript_path.rename(opened_path)
                    transcript_path.symlink_to(outside_path)
                    swapped = True
                return file_fd

            with mock.patch.object(
                verifier_module.os,
                "open",
                side_effect=swap_after_open,
            ):
                result = verifier_module.verify_library(db_path, records_root)

            codes = {issue["code"] for issue in result["issues"]}
            self.assertTrue(swapped)
            self.assertNotIn("artifact_hash_mismatch", codes)
            self.assertNotIn("artifact_unreadable", codes)
            self.assertIn("record_entry_symlink", codes)

    def test_manifest_and_artifact_io_errors_are_structured_issues(self) -> None:
        targets = ("manifest", "artifact")
        for target in targets:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                db_path, records_root, manifest_path = self._fixture(root)
                transcript_path = (
                    records_root
                    / "rec_verifier"
                    / "jobs"
                    / "job_verifier"
                    / "transcript.txt"
                )
                failing_inode = (
                    manifest_path.stat().st_ino
                    if target == "manifest"
                    else transcript_path.stat().st_ino
                )
                expected_code = (
                    "manifest_unreadable"
                    if target == "manifest"
                    else "artifact_unreadable"
                )
                from lecture_stt.storage_v2 import verifier as verifier_module

                real_read = verifier_module.os.read

                def fail_target_read(file_fd: int, byte_count: int) -> bytes:
                    if os.fstat(file_fd).st_ino == failing_inode:
                        raise OSError("simulated read failure")
                    return real_read(file_fd, byte_count)

                with mock.patch.object(
                    verifier_module.os,
                    "read",
                    side_effect=fail_target_read,
                ):
                    result = verifier_module.verify_library(
                        db_path,
                        records_root,
                    )

                codes = {issue["code"] for issue in result["issues"]}
                self.assertFalse(result["ok"])
                self.assertIn(expected_code, codes)

    def test_recursive_inventory_reports_depth_and_entry_caps(self) -> None:
        from lecture_stt.storage_v2 import verifier as verifier_module

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path, records_root, _ = self._fixture(root)
            nested = records_root / "rec_verifier" / "a" / "b" / "c"
            nested.mkdir(parents=True)
            with mock.patch.object(verifier_module, "_MAX_SCAN_DEPTH", 2):
                result = verifier_module.verify_library(db_path, records_root)
            codes = {issue["code"] for issue in result["issues"]}
            self.assertIn("record_scan_depth_limit_exceeded", codes)

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, _ = self._fixture(Path(tmpdir))
            with mock.patch.object(verifier_module, "_MAX_SCAN_ENTRIES", 2):
                result = verifier_module.verify_library(db_path, records_root)
            codes = {issue["code"] for issue in result["issues"]}
            self.assertIn("record_scan_entry_limit_exceeded", codes)

    def test_current_job_requires_exactly_one_selected_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, _ = self._fixture(Path(tmpdir))
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE engine_runs SET is_selected = 0")
                conn.commit()

            result = verify_library(db_path, records_root)
            issue = next(
                issue
                for issue in result["issues"]
                if issue["code"] == "current_job_selected_engine_invariant"
            )

            self.assertFalse(result["ok"])
            self.assertEqual(issue["job_key"], "job_verifier")
            self.assertEqual(issue["selected_engine_count"], 0)

    def test_engine_bound_artifact_requires_selected_engine_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, _ = self._fixture(Path(tmpdir))
            with sqlite3.connect(db_path) as conn:
                job_id, recording_id = conn.execute(
                    """
                    SELECT id, recording_id
                    FROM transcription_jobs
                    WHERE job_key = 'job_verifier'
                    """
                ).fetchone()
                cursor = conn.execute(
                    """
                    INSERT INTO engine_runs(
                        job_id,
                        recording_id,
                        engine_name,
                        engine_version,
                        provider,
                        status,
                        is_selected
                    )
                    VALUES (?, ?, 'other-engine', 'v2', 'test', 'succeeded', 0)
                    """,
                    (job_id, recording_id),
                )
                unselected_engine_id = int(cursor.lastrowid)
                conn.execute(
                    """
                    UPDATE artifacts
                    SET engine_run_id = ?
                    WHERE artifact_kind = 'transcript_raw_text'
                    """,
                    (unselected_engine_id,),
                )
                conn.commit()

            result = verify_library(db_path, records_root)
            issue = next(
                issue
                for issue in result["issues"]
                if issue["code"]
                == "artifact_engine_run_ownership_mismatch"
            )

            self.assertFalse(result["ok"])
            self.assertEqual(
                issue["actual_engine_run_id"],
                unselected_engine_id,
            )
            self.assertNotEqual(
                issue["expected_engine_run_id"],
                unselected_engine_id,
            )

    def test_optional_log_artifact_may_only_link_selected_engine(self) -> None:
        for linkage in ("selected", "unselected"):
            with self.subTest(linkage=linkage), tempfile.TemporaryDirectory() as tmpdir:
                db_path, records_root, manifest_path = self._fixture(
                    Path(tmpdir)
                )
                log_path = (
                    records_root
                    / "rec_verifier"
                    / "jobs"
                    / "job_verifier"
                    / "engine.log"
                )
                log_path.write_text("engine log", encoding="utf-8")
                log_sha = hashlib.sha256(log_path.read_bytes()).hexdigest()
                log_bytes = log_path.stat().st_size
                with sqlite3.connect(db_path) as conn:
                    (
                        job_id,
                        recording_id,
                        selected_engine_id,
                    ) = conn.execute(
                        """
                        SELECT j.id, j.recording_id, e.id
                        FROM transcription_jobs AS j
                        JOIN engine_runs AS e
                          ON e.job_id = j.id
                         AND e.is_selected = 1
                        WHERE j.job_key = 'job_verifier'
                        """
                    ).fetchone()
                    conn.execute(
                        """
                        UPDATE engine_runs
                        SET log_relpath = 'jobs/job_verifier/engine.log'
                        WHERE id = ?
                        """,
                        (selected_engine_id,),
                    )
                    linked_engine_id = selected_engine_id
                    if linkage == "unselected":
                        cursor = conn.execute(
                            """
                            INSERT INTO engine_runs(
                                job_id,
                                recording_id,
                                engine_name,
                                engine_version,
                                provider,
                                status,
                                is_selected,
                                log_relpath,
                                started_at,
                                finished_at
                            )
                            VALUES (
                                ?,
                                ?,
                                'other-engine',
                                'v2',
                                'test',
                                'succeeded',
                                0,
                                'jobs/job_verifier/engine.log',
                                '2026-07-23T00:02:00+00:00',
                                '2026-07-23T01:00:00+00:00'
                            )
                            """,
                            (job_id, recording_id),
                        )
                        linked_engine_id = int(cursor.lastrowid)
                    conn.execute(
                        """
                        INSERT INTO artifacts(
                            recording_id,
                            job_id,
                            engine_run_id,
                            artifact_kind,
                            revision,
                            path_rel,
                            content_sha256,
                            bytes,
                            mime_type,
                            is_latest
                        )
                        VALUES (
                            ?,
                            ?,
                            ?,
                            'log',
                            1,
                            'jobs/job_verifier/engine.log',
                            ?,
                            ?,
                            'text/plain',
                            1
                        )
                        """,
                        (
                            recording_id,
                            job_id,
                            linked_engine_id,
                            log_sha,
                            log_bytes,
                        ),
                    )
                    conn.commit()
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                manifest["jobs"][0]["engine"]["log_relpath"] = (
                    "jobs/job_verifier/engine.log"
                )
                manifest["jobs"][0]["artifact_paths"].append(
                    "jobs/job_verifier/engine.log"
                )
                manifest["artifacts"].append(
                    {
                        "kind": "log",
                        "path": "jobs/job_verifier/engine.log",
                        "sha256": log_sha,
                        "bytes": log_bytes,
                        "mime_type": "text/plain",
                    }
                )
                self._rewrite_manifest(manifest_path, manifest)

                result = verify_library(db_path, records_root)
                codes = {issue["code"] for issue in result["issues"]}

                if linkage == "selected":
                    self.assertTrue(result["ok"], result["issues"])
                else:
                    self.assertFalse(result["ok"])
                    self.assertIn(
                        "artifact_engine_run_ownership_mismatch",
                        codes,
                    )

    def test_active_artifact_cannot_hide_under_noncurrent_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, manifest_path = self._fixture(Path(tmpdir))
            with sqlite3.connect(db_path) as conn:
                recording_id = conn.execute(
                    """
                    SELECT id
                    FROM recordings
                    WHERE storage_key = 'rec_verifier'
                    """
                ).fetchone()[0]
                cursor = conn.execute(
                    """
                    INSERT INTO transcription_jobs(
                        recording_id,
                        job_key,
                        job_relpath,
                        status,
                        progress,
                        is_current
                    )
                    VALUES (?, 'job_old', 'jobs/job_old', 'done', 100, 0)
                    """,
                    (recording_id,),
                )
                old_job_id = int(cursor.lastrowid)
                cursor = conn.execute(
                    """
                    INSERT INTO engine_runs(
                        job_id,
                        recording_id,
                        engine_name,
                        engine_version,
                        provider,
                        status,
                        is_selected
                    )
                    VALUES (
                        ?,
                        ?,
                        'old-engine',
                        'v1',
                        'test',
                        'succeeded',
                        1
                    )
                    """,
                    (old_job_id, recording_id),
                )
                old_engine_id = int(cursor.lastrowid)
                conn.execute(
                    """
                    UPDATE artifacts
                    SET job_id = ?, engine_run_id = ?
                    WHERE artifact_kind = 'transcript_raw_text'
                    """,
                    (old_job_id, old_engine_id),
                )
                conn.commit()
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            manifest["jobs"][0]["artifact_paths"] = []
            self._rewrite_manifest(manifest_path, manifest)

            result = verify_library(db_path, records_root)
            codes = {issue["code"] for issue in result["issues"]}

            self.assertFalse(result["ok"])
            self.assertIn(
                "manifest_jobs_mismatch",
                codes,
            )
            self.assertIn(
                "artifact_manifest_job_ownership_mismatch",
                codes,
            )

    def test_job_rollover_keeps_recording_level_source_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, manifest_path = self._fixture(Path(tmpdir))
            with sqlite3.connect(db_path) as conn:
                recording_id = conn.execute(
                    """
                    SELECT id
                    FROM recordings
                    WHERE storage_key = 'rec_verifier'
                    """
                ).fetchone()[0]
                conn.execute(
                    """
                    UPDATE transcription_jobs
                    SET is_current = 0
                    WHERE job_key = 'job_verifier'
                    """
                )
                cursor = conn.execute(
                    """
                    INSERT INTO transcription_jobs(
                        recording_id,
                        job_key,
                        job_relpath,
                        requested_profile,
                        requested_profile_version,
                        status,
                        progress,
                        is_current,
                        queued_at,
                        started_at,
                        finished_at
                    )
                    VALUES (
                        ?,
                        'job_new',
                        'jobs/job_new',
                        'lecture',
                        'v1',
                        'done',
                        100,
                        1,
                        '2026-07-24T00:01:00+00:00',
                        '2026-07-24T00:02:00+00:00',
                        '2026-07-24T01:00:00+00:00'
                    )
                    """,
                    (recording_id,),
                )
                new_job_id = int(cursor.lastrowid)
                conn.execute(
                    """
                    INSERT INTO engine_runs(
                        job_id,
                        recording_id,
                        engine_name,
                        engine_version,
                        provider,
                        status,
                        is_selected,
                        started_at,
                        finished_at
                    )
                    VALUES (
                        ?,
                        ?,
                        'faster-whisper',
                        'large-v3',
                        'test',
                        'succeeded',
                        1,
                        '2026-07-24T00:02:00+00:00',
                        '2026-07-24T01:00:00+00:00'
                    )
                    """,
                    (new_job_id, recording_id),
                )
                conn.commit()
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            manifest["jobs"].append(
                {
                    "job_key": "job_new",
                    "status": "done",
                    "requested_profile": "lecture",
                    "requested_profile_version": "v1",
                    "queued_at": "2026-07-24T00:01:00+00:00",
                    "started_at": "2026-07-24T00:02:00+00:00",
                    "finished_at": "2026-07-24T01:00:00+00:00",
                    "engine": {
                        "name": "faster-whisper",
                        "version": "large-v3",
                        "started_at": "2026-07-24T00:02:00+00:00",
                        "finished_at": "2026-07-24T01:00:00+00:00",
                        "stderr_relpath": None,
                        "log_relpath": None,
                    },
                    "artifact_paths": [],
                }
            )
            self._rewrite_manifest(manifest_path, manifest)

            result = verify_library(db_path, records_root)

            self.assertTrue(result["ok"], result["issues"])

    def test_artifact_must_stay_under_owning_job_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, manifest_path = self._fixture(Path(tmpdir))
            record_root = records_root / "rec_verifier"
            old_path = (
                record_root
                / "jobs"
                / "job_verifier"
                / "transcript.txt"
            )
            new_path = (
                record_root
                / "jobs"
                / "wrong_job"
                / "transcript.txt"
            )
            new_path.parent.mkdir(parents=True)
            old_path.rename(new_path)
            old_path.parent.rmdir()
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    UPDATE artifacts
                    SET path_rel = 'jobs/wrong_job/transcript.txt'
                    WHERE artifact_kind = 'transcript_raw_text'
                    """
                )
                conn.commit()
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            transcript_entry = next(
                entry
                for entry in manifest["artifacts"]
                if entry["kind"] == "transcript_raw_text"
            )
            transcript_entry["path"] = "jobs/wrong_job/transcript.txt"
            manifest["jobs"][0]["artifact_paths"] = [
                "jobs/wrong_job/transcript.txt"
            ]
            self._rewrite_manifest(manifest_path, manifest)

            result = verify_library(db_path, records_root)
            codes = {issue["code"] for issue in result["issues"]}

            self.assertFalse(result["ok"])
            self.assertIn("artifact_job_namespace_mismatch", codes)
            self.assertIn("manifest_invalid", codes)

    def test_source_path_requires_source_copy_artifact_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, manifest_path = self._fixture(Path(tmpdir))
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    UPDATE artifacts
                    SET artifact_kind = 'other'
                    WHERE path_rel = 'source/original.m4a'
                    """
                )
                conn.commit()
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            source_entry = next(
                entry
                for entry in manifest["artifacts"]
                if entry["path"] == "source/original.m4a"
            )
            source_entry["kind"] = "other"
            self._rewrite_manifest(manifest_path, manifest)

            result = verify_library(db_path, records_root)
            codes = {issue["code"] for issue in result["issues"]}

            self.assertFalse(result["ok"])
            self.assertIn("source_artifact_kind_mismatch", codes)

    def test_noncanonical_source_copy_requires_manifest_job_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, manifest_path = self._fixture(Path(tmpdir))
            extra_path = (
                records_root
                / "rec_verifier"
                / "jobs"
                / "job_verifier"
                / "source-copy.m4a"
            )
            extra_path.write_bytes(b"extra-source-copy")
            extra_sha = hashlib.sha256(extra_path.read_bytes()).hexdigest()
            extra_bytes = extra_path.stat().st_size
            with sqlite3.connect(db_path) as conn:
                recording_id, job_id = conn.execute(
                    """
                    SELECT recording_id, id
                    FROM transcription_jobs
                    WHERE job_key = 'job_verifier'
                    """
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO artifacts(
                        recording_id,
                        job_id,
                        engine_run_id,
                        artifact_kind,
                        revision,
                        path_rel,
                        content_sha256,
                        bytes,
                        mime_type,
                        is_latest
                    )
                    VALUES (
                        ?,
                        ?,
                        NULL,
                        'source_copy',
                        2,
                        'jobs/job_verifier/source-copy.m4a',
                        ?,
                        ?,
                        'audio/mp4',
                        0
                    )
                    """,
                    (recording_id, job_id, extra_sha, extra_bytes),
                )
                conn.commit()
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            manifest["artifacts"].append(
                {
                    "kind": "source_copy",
                    "path": "jobs/job_verifier/source-copy.m4a",
                    "sha256": extra_sha,
                    "bytes": extra_bytes,
                    "mime_type": "audio/mp4",
                }
            )
            self._rewrite_manifest(manifest_path, manifest)

            result = verify_library(db_path, records_root)
            codes = {issue["code"] for issue in result["issues"]}

            self.assertFalse(result["ok"])
            self.assertIn(
                "artifact_manifest_job_ownership_mismatch",
                codes,
            )
            self.assertIn(
                "manifest_job_artifact_paths_mismatch",
                codes,
            )

    def test_available_source_artifact_metadata_matches_recording_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, manifest_path = self._fixture(Path(tmpdir))
            source_path = (
                records_root
                / "rec_verifier"
                / "source"
                / "original.m4a"
            )
            source_path.write_bytes(b"mutated-source-artifact")
            mutated_sha = hashlib.sha256(
                source_path.read_bytes()
            ).hexdigest()
            mutated_bytes = source_path.stat().st_size
            mutated_mime = "audio/x-mutated"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    UPDATE artifacts
                    SET content_sha256 = ?, bytes = ?, mime_type = ?
                    WHERE path_rel = 'source/original.m4a'
                    """,
                    (mutated_sha, mutated_bytes, mutated_mime),
                )
                conn.commit()
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            source_entry = next(
                entry
                for entry in manifest["artifacts"]
                if entry["path"] == "source/original.m4a"
            )
            source_entry["sha256"] = mutated_sha
            source_entry["bytes"] = mutated_bytes
            source_entry["mime_type"] = mutated_mime
            self._rewrite_manifest(manifest_path, manifest)

            result = verify_library(db_path, records_root)
            issue = next(
                issue
                for issue in result["issues"]
                if issue["code"] == "source_artifact_metadata_mismatch"
            )
            codes = {entry["code"] for entry in result["issues"]}

            self.assertFalse(result["ok"])
            self.assertEqual(
                set(issue["fields"]),
                {"sha256", "bytes", "mime_type"},
            )
            self.assertNotIn("manifest_source_mismatch", codes)
            self.assertNotIn("manifest_artifact_index_mismatch", codes)
            self.assertNotIn("artifact_hash_mismatch", codes)

    def test_selected_engine_status_must_cohere_with_job_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, _ = self._fixture(Path(tmpdir))
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE engine_runs SET status = 'planned'")
                conn.commit()

            result = verify_library(db_path, records_root)
            issue = next(
                issue
                for issue in result["issues"]
                if issue["code"] == "selected_engine_status_mismatch"
            )

            self.assertFalse(result["ok"])
            self.assertEqual(issue["job_status"], "done")
            self.assertEqual(issue["engine_status"], "planned")
            self.assertEqual(issue["allowed_engine_statuses"], ["succeeded"])

    def test_manifest_and_artifacts_must_not_have_hardlink_aliases(self) -> None:
        for target in ("manifest", "artifact"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                db_path, records_root, manifest_path = self._fixture(root)
                indexed_path = (
                    manifest_path
                    if target == "manifest"
                    else (
                        records_root
                        / "rec_verifier"
                        / "jobs"
                        / "job_verifier"
                        / "transcript.txt"
                    )
                )
                alias_path = root / f"{target}-hardlink-alias"
                try:
                    os.link(indexed_path, alias_path)
                except OSError as exc:
                    self.skipTest(f"hardlink creation is unavailable: {exc}")

                result = verify_library(db_path, records_root)
                codes = {issue["code"] for issue in result["issues"]}

                self.assertFalse(result["ok"])
                self.assertIn(
                    (
                        "manifest_unreadable"
                        if target == "manifest"
                        else "artifact_unreadable"
                    ),
                    codes,
                )

    def test_staging_inventory_is_bounded_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, _ = self._fixture(Path(tmpdir))
            staging_root = records_root / ".staging"
            staging_root.mkdir()
            for index in range(3):
                (staging_root / f"entry-{index}").touch()
            from lecture_stt.storage_v2 import verifier as verifier_module

            with mock.patch.object(
                verifier_module,
                "_MAX_SCAN_ENTRIES",
                2,
            ):
                result = verifier_module.verify_library(
                    db_path,
                    records_root,
                )

            codes = {issue["code"] for issue in result["issues"]}
            stale_issue = next(
                issue
                for issue in result["issues"]
                if issue["code"] == "stale_staging_entries"
            )
            self.assertFalse(result["ok"])
            self.assertIn(
                "internal_scan_entry_limit_exceeded",
                codes,
            )
            self.assertTrue(stale_issue["truncated"])
            self.assertLessEqual(len(stale_issue["entries"]), 2)

    def test_records_root_rename_swap_uses_open_fd_and_fails_identity_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path, records_root, _ = self._fixture(root)
            rogue_path = records_root / "rogue-private.txt"
            rogue_path.write_text(
                "PRIVATE TRANSCRIPT BODY",
                encoding="utf-8",
            )
            moved_records_root = root / "records-moved"
            from lecture_stt.storage_v2 import verifier as verifier_module

            real_flock = verifier_module.fcntl.flock
            swapped = False

            def swap_after_exclusive_lock(file_fd: int, operation: int) -> None:
                nonlocal swapped
                real_flock(file_fd, operation)
                if operation == verifier_module.fcntl.LOCK_EX and not swapped:
                    records_root.rename(moved_records_root)
                    records_root.mkdir()
                    swapped = True

            with mock.patch.object(
                verifier_module.fcntl,
                "flock",
                side_effect=swap_after_exclusive_lock,
            ):
                result = verifier_module.verify_library(db_path, records_root)

            issue_pairs = {
                (issue["code"], issue.get("path"))
                for issue in result["issues"]
            }
            self.assertTrue(swapped)
            self.assertFalse(result["ok"])
            self.assertIn(
                ("unexpected_records_root_entry", "rogue-private.txt"),
                issue_pairs,
            )
            self.assertIn(
                (
                    "records_root_identity_changed",
                    str(records_root.resolve()),
                ),
                issue_pairs,
            )
            self.assertNotIn(
                "record_root_missing_or_unsafe",
                {issue["code"] for issue in result["issues"]},
            )

    def test_verify_holds_exclusive_records_root_flock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, _ = self._fixture(Path(tmpdir))
            from lecture_stt.storage_v2 import verifier as verifier_module

            with mock.patch.object(
                verifier_module.fcntl,
                "flock",
                wraps=verifier_module.fcntl.flock,
            ) as flock_mock:
                result = verifier_module.verify_library(db_path, records_root)

            self.assertTrue(result["ok"])
            lock_modes = [call.args[1] for call in flock_mock.call_args_list]
            self.assertEqual(
                lock_modes,
                [
                    verifier_module.fcntl.LOCK_EX,
                    verifier_module.fcntl.LOCK_UN,
                ],
            )

    def test_foreign_key_check_rejects_dangling_database_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path, records_root, _ = self._fixture(Path(tmpdir))
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(
                    conn.execute("PRAGMA foreign_keys").fetchone()[0],
                    0,
                )
                conn.execute(
                    """
                    UPDATE artifacts
                    SET recording_id = 999999
                    WHERE artifact_kind = 'transcript_raw_text'
                    """
                )
                conn.commit()

            result = verify_library(db_path, records_root)
            issue = next(
                issue
                for issue in result["issues"]
                if issue["code"] == "database_foreign_key_violation"
            )

            self.assertFalse(result["ok"])
            self.assertGreaterEqual(issue["reported_violation_count"], 1)
            self.assertTrue(
                any(
                    violation["table"] == "artifacts"
                    for violation in issue["violations"]
                )
            )


if __name__ == "__main__":
    unittest.main()
