from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from lecture_stt.storage_v2 import cli
from lecture_stt.storage_v2.manifest import build_manifest, write_manifest
from lecture_stt.storage_v2.repository import apply_migration
from lecture_stt.storage_v2.transcript_recovery import (
    DEFAULT_MAX_ARTIFACT_BYTES,
    HistoricalTranscriptRecoveryConflictError,
    HistoricalTranscriptRecoveryNotFoundError,
    HistoricalTranscriptRecoveryRequiredError,
    HistoricalTranscriptRecoveryWriteDisabledError,
    apply_historical_transcript_recovery,
    plan_historical_transcript_recovery,
    historical_transcript_recovery_integrity_issues,
)
from lecture_stt.storage_v2.verifier import verify_library


class StorageV2TranscriptRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "storage-v2.sqlite3"
        self.records_root = self.root / "records"
        self.records_root.mkdir()
        self.historical_root = self.root / "historical"
        self.historical_root.mkdir()
        self.storage_key = "rec_recovery"
        self.canonical_base = "REC-260723-1"
        self._seed_recovery_case(
            self.root,
            storage_key=self.storage_key,
            canonical_base=self.canonical_base,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_recovery_case(
        self,
        root: Path,
        *,
        storage_key: str,
        canonical_base: str,
    ) -> dict[str, object]:
        db_path = root / "storage-v2.sqlite3"
        records_root = root / "records"
        historical_root = root / "historical"
        records_root.mkdir(exist_ok=True)
        historical_root.mkdir(exist_ok=True)

        record_root = records_root / storage_key
        source_path = record_root / "source" / "original.m4a"
        source_path.parent.mkdir(parents=True)
        source_payload = b"audio-for-transcript-recovery"
        source_path.write_bytes(source_payload)
        source_sha = hashlib.sha256(source_payload).hexdigest()
        source_bytes = source_path.stat().st_size
        conn = apply_migration(db_path)
        conn.row_factory = sqlite3.Row
        try:
            recording_id = int(
                conn.execute(
                    """
                    INSERT INTO recordings(
                        storage_key,
                        original_name_raw,
                        original_name_nfc,
                        source_relpath,
                        manifest_relpath,
                        ingest_sha256,
                        ingest_bytes,
                        source_mime,
                        source_state,
                        created_at
                    ) VALUES (?, ?, ?, 'source/original.m4a', 'manifest.json', ?, ?, 'audio/mp4', 'available', '2026-07-23T00:00:00+09:00')
                    """,
                    (
                        storage_key,
                        f"{storage_key}.m4a",
                        f"{storage_key}.m4a",
                        source_sha,
                        source_bytes,
                    ),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO recording_titles(
                    recording_id,
                    title,
                    title_source,
                    locale,
                    is_current
                ) VALUES (?, '원본 제목', 'legacy_import', 'ko-KR', 1)
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
                ) VALUES (?, 'general', 'legacy_import', 1)
                """,
                (recording_id,),
            )
            job_key = f"job_{storage_key}"
            (record_root / "jobs" / job_key).mkdir(parents=True, exist_ok=True)
            job_id = int(
                conn.execute(
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
                    ) VALUES (
                        ?,
                        ?,
                        ?,
                        'lecture',
                        'v1',
                        'needs_review',
                        100,
                        1,
                        '2026-07-23T00:01:00+09:00',
                        '2026-07-23T00:02:00+09:00',
                        '2026-07-23T01:00:00+09:00'
                    )
                    """,
                    (recording_id, job_key, f"jobs/{job_key}"),
                ).lastrowid
            )
            engine_run_id = int(
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
                    ) VALUES (?, ?, 'faster-whisper', 'large-v3', 'legacy_import', 'succeeded', 1, '2026-07-23T00:02:00+09:00', '2026-07-23T01:00:00+09:00')
                    """,
                    (job_id, recording_id),
                ).lastrowid
            )
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
                ) VALUES (?, ?, ?, 'source_copy', 1, 'source/original.m4a', ?, ?, 'audio/mp4', 1)
                """,
                (recording_id, job_id, None, source_sha, source_bytes),
            )
            review_id = int(
                conn.execute(
                    """
                    INSERT INTO review_items(
                        recording_id,
                        job_id,
                        status,
                        severity,
                        reason_code,
                        detail_json
                    ) VALUES (?, ?, 'open', 'medium', 'legacy_import_review', ?)
                    """,
                    (
                        recording_id,
                        job_id,
                        json.dumps(
                            {
                                "schema_version": (
                                    "storage-v2/legacy-import-review@1"
                                ),
                                "issues": [
                                    {"code": "transcript_pair_incomplete"},
                                    {"code": "transcript_txt_missing"},
                                    {"code": "transcript_json_missing"},
                                ],
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO legacy_import_map(
                    legacy_kind,
                    legacy_key,
                    recording_id,
                    source_fingerprint,
                    legacy_snapshot_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "job",
                    str(job_id),
                    recording_id,
                    "a" * 64,
                    json.dumps(
                        {
                            "canonical_base": canonical_base,
                            "status": "DONE",
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        manifest = build_manifest(
            storage_key=storage_key,
            generated_at="2026-07-23T01:00:00+09:00",
            original_name_raw=f"{storage_key}.m4a",
            original_name_nfc=f"{storage_key}.m4a",
            recording_created_at="2026-07-23T00:00:00+09:00",
            source={
                "path": "source/original.m4a",
                "availability": "available",
                "sha256": source_sha,
                "bytes": source_bytes,
                "mime_type": "audio/mp4",
            },
            title={"value": "원본 제목", "source": "legacy_import"},
            context={"type": "general", "source": "legacy_import"},
            jobs=[
                {
                    "job_key": f"job_{storage_key}",
                    "status": "needs_review",
                    "requested_profile": "lecture",
                    "requested_profile_version": "v1",
                    "queued_at": "2026-07-23T00:01:00+09:00",
                    "started_at": "2026-07-23T00:02:00+09:00",
                    "finished_at": "2026-07-23T01:00:00+09:00",
                    "engine": {
                        "name": "faster-whisper",
                        "version": "large-v3",
                        "started_at": "2026-07-23T00:02:00+09:00",
                        "finished_at": "2026-07-23T01:00:00+09:00",
                        "stderr_relpath": None,
                        "log_relpath": None,
                    },
                    "artifact_paths": [],
                }
            ],
            artifacts=[
                {
                    "kind": "source_copy",
                    "path": "source/original.m4a",
                    "sha256": source_sha,
                    "bytes": source_bytes,
                    "mime_type": "audio/mp4",
                }
            ],
            legacy={
                "kind": "job",
                "job_id": 1,
                "canonical_base": canonical_base,
                "status": "DONE",
                "source_fingerprint": "a" * 64,
            },
        )
        manifest_path = record_root / "manifest.json"
        write_manifest(manifest_path, manifest)

        transcript_path = historical_root / f"{canonical_base}.txt"
        transcript_json_path = historical_root / f"{canonical_base}.json"
        transcript_path.write_text(
            "복구 전사 텍스트",
            encoding="utf-8",
        )
        transcript_json_path.write_text(
            json.dumps(
                {
                    "segments": [
                        {"id": 0, "start": 0.0, "end": 0.5, "text": "복구"}
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        return {
            "recording_id": recording_id,
            "job_id": job_id,
            "engine_run_id": engine_run_id,
            "review_id": review_id,
            "manifest_path": manifest_path,
            "transcript_path": transcript_path,
            "transcript_json_path": transcript_json_path,
        }

    def _plan(self) -> dict[str, object]:
        return plan_historical_transcript_recovery(
            self.db_path,
            self.records_root,
            self.historical_root,
            self.storage_key,
        )

    def _apply(
        self,
        plan: dict[str, object],
        *,
        recovery_enabled: bool = True,
        allow_write: bool = True,
        expected_count: int = 1,
    ) -> dict[str, object]:
        return apply_historical_transcript_recovery(
            self.db_path,
            self.records_root,
            self.historical_root,
            self.storage_key,
            expected_count=expected_count,
            expected_plan_sha256=str(plan["plan_sha256"]),
            recovery_enabled=recovery_enabled,
            allow_write=allow_write,
        )

    def _journal_state(self) -> list[tuple[str, int]]:
        with sqlite3.connect(self.db_path) as conn:
            job_id = int(
                conn.execute(
                    """
                    SELECT j.id
                    FROM transcription_jobs AS j
                    JOIN recordings AS r ON r.id = j.recording_id
                    WHERE r.storage_key = ? AND j.is_current = 1
                    """,
                    (self.storage_key,),
                ).fetchone()[0]
            )
            rows = conn.execute(
                """
                SELECT event_type, event_seq
                FROM job_events
                WHERE job_id = ?
                ORDER BY event_seq ASC
                """,
                (job_id,),
            ).fetchall()
        return [(str(row[0]), int(row[1])) for row in rows]

    def _manifest_artifact_kinds(self) -> set[str]:
        manifest_path = self.records_root / self.storage_key / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        return {item["kind"] for item in payload["artifacts"]}

    def _manifest_job_paths(self) -> list[str]:
        manifest_path = self.records_root / self.storage_key / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        return list(payload["jobs"][0]["artifact_paths"])

    def test_plan_is_read_only_and_metadata_only(self) -> None:
        plan = self._plan()
        payload = json.dumps(plan, ensure_ascii=False)

        self.assertEqual(plan["schema_version"], "storage-v2/historical-transcript-recovery-plan@1")
        self.assertEqual(plan["expected_count"], 1)
        self.assertEqual(plan["max_artifact_bytes"], DEFAULT_MAX_ARTIFACT_BYTES)
        self.assertEqual(plan["recovery_state"], "planned")
        self.assertEqual(plan["canonical_base"], self.canonical_base)
        self.assertEqual(len(plan["source_artifacts"]), 2)
        self.assertEqual(len(plan["target_artifacts"]), 2)
        self.assertNotIn("복구 전사 텍스트", payload)
        self.assertNotIn("복구", payload)

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT COUNT(*)
                FROM job_events
                """
            ).fetchone()
            self.assertEqual(int(rows[0]), 0)

    def test_default_guards_and_count_or_digest_mismatch_are_rejected(self) -> None:
        plan = self._plan()

        with self.assertRaises(HistoricalTranscriptRecoveryWriteDisabledError):
            self._apply(plan, recovery_enabled=False, allow_write=False)
        with self.assertRaises(HistoricalTranscriptRecoveryWriteDisabledError):
            self._apply(plan, recovery_enabled=True, allow_write=False)
        with self.assertRaises(HistoricalTranscriptRecoveryConflictError):
            self._apply(plan, expected_count=2)
        with self.assertRaises(HistoricalTranscriptRecoveryConflictError):
            apply_historical_transcript_recovery(
                self.db_path,
                self.records_root,
                self.historical_root,
                self.storage_key,
                expected_count=1,
                expected_plan_sha256="0" * 64,
                recovery_enabled=True,
                allow_write=True,
            )

    def test_cli_plan_and_apply_are_guarded_and_apply_succeeds_when_unlocked(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = cli.main(
                [
                    "plan-historical-transcript-recovery",
                    "--v2-db",
                    str(self.db_path),
                    "--records-root",
                    str(self.records_root),
                    "--historical-transcript-root",
                    str(self.historical_root),
                    "--storage-key",
                    self.storage_key,
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        plan = json.loads(stdout.getvalue())

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = cli.main(
                [
                    "apply-historical-transcript-recovery",
                    "--v2-db",
                    str(self.db_path),
                    "--records-root",
                    str(self.records_root),
                    "--historical-transcript-root",
                    str(self.historical_root),
                    "--storage-key",
                    self.storage_key,
                    "--expected-count",
                    "1",
                    "--expected-plan-sha256",
                    plan["plan_sha256"],
                    "--json",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("disabled", stderr.getvalue())

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = cli.main(
                [
                    "apply-historical-transcript-recovery",
                    "--v2-db",
                    str(self.db_path),
                    "--records-root",
                    str(self.records_root),
                    "--historical-transcript-root",
                    str(self.historical_root),
                    "--storage-key",
                    self.storage_key,
                    "--expected-count",
                    "2",
                    "--expected-plan-sha256",
                    plan["plan_sha256"],
                    "--enable-recovery",
                    "--allow-write",
                    "--json",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("must equal 1", stderr.getvalue())

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = cli.main(
                [
                    "apply-historical-transcript-recovery",
                    "--v2-db",
                    str(self.db_path),
                    "--records-root",
                    str(self.records_root),
                    "--historical-transcript-root",
                    str(self.historical_root),
                    "--storage-key",
                    self.storage_key,
                    "--expected-count",
                    "1",
                    "--expected-plan-sha256",
                    plan["plan_sha256"],
                    "--enable-recovery",
                    "--allow-write",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["action"], "applied")

    def test_apply_success_materializes_exactly_two_transcript_artifacts_and_keeps_identity(self) -> None:
        plan = self._plan()
        title_before = "원본 제목"

        with sqlite3.connect(self.db_path) as conn:
            result_before = conn.execute(
                """
                SELECT title, title_source
                FROM recording_titles
                WHERE recording_id = (SELECT id FROM recordings WHERE storage_key = ?)
                  AND is_current = 1
                """,
                (self.storage_key,),
            ).fetchone()
            context_before = conn.execute(
                """
                SELECT context_type, source
                FROM recording_contexts
                WHERE recording_id = (SELECT id FROM recordings WHERE storage_key = ?)
                  AND is_selected = 1
                """,
                (self.storage_key,),
            ).fetchone()
            self.assertEqual(title_before, str(result_before[0]))
            self.assertEqual("legacy_import", str(result_before[1]))
            self.assertEqual("general", str(context_before[0]))
            self.assertEqual("legacy_import", str(context_before[1]))

        applied = self._apply(plan)
        self.assertEqual(applied["action"], "applied")
        self.assertEqual(self._manifest_artifact_kinds(), {
            "source_copy",
            "transcript_raw_text",
            "transcript_segments_json",
        })

        expected_transcript_paths = {
            f"jobs/job_{self.storage_key}/transcript.txt",
            f"jobs/job_{self.storage_key}/transcript.segments.json",
        }
        self.assertEqual(set(self._manifest_job_paths()), expected_transcript_paths)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            artifacts = conn.execute(
                """
                SELECT artifact_kind, revision, path_rel, bytes, content_sha256
                FROM artifacts
                WHERE recording_id = (SELECT id FROM recordings WHERE storage_key = ?)
                  AND artifact_kind IN ('transcript_raw_text', 'transcript_segments_json')
                  AND archived_at IS NULL
                ORDER BY artifact_kind
                """,
                (self.storage_key,),
            ).fetchall()
            self.assertEqual(len(artifacts), 2)
            for artifact in artifacts:
                self.assertEqual(int(artifact["revision"]), 1)
                self.assertIn(artifact["path_rel"], expected_transcript_paths)
            self.assertEqual(
                conn.execute(
                    """
                    SELECT title
                    FROM recording_titles
                    WHERE recording_id = (SELECT id FROM recordings WHERE storage_key = ?)
                      AND is_current = 1
                    """,
                    (self.storage_key,),
                ).fetchone()[0],
                title_before,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT context_type
                    FROM recording_contexts
                    WHERE recording_id = (SELECT id FROM recordings WHERE storage_key = ?)
                      AND is_selected = 1
                    """,
                    (self.storage_key,),
                ).fetchone()[0],
                str(context_before[0]),
            )

    def test_apply_is_idempotent_when_already_applied(self) -> None:
        plan = self._plan()
        first = self._apply(plan)
        self.assertEqual(first["action"], "applied")

        manifest_after_first = (self.records_root / self.storage_key / "manifest.json").read_bytes()
        second = self._apply(plan)
        self.assertEqual(second["action"], "already_applied")

        manifest_after_second = (self.records_root / self.storage_key / "manifest.json").read_bytes()
        self.assertEqual(manifest_after_first, manifest_after_second)
        self.assertEqual(self._journal_state(), [
            ("historical_transcript_recovery_prepared", 1),
            ("historical_transcript_recovery_applied", 2),
        ])
        self.assertTrue(verify_library(self.db_path, self.records_root)["ok"])

    def test_apply_recovery_from_prepared_state(self) -> None:
        plan = self._plan()
        with mock.patch(
            "lecture_stt.storage_v2.transcript_recovery._write_or_verify_target",
            side_effect=RuntimeError("simulated write target crash"),
        ):
            with self.assertRaises(HistoricalTranscriptRecoveryRequiredError):
                self._apply(plan)

        self.assertEqual(self._journal_state(), [("historical_transcript_recovery_prepared", 1)])

        recovered = self._apply(plan)
        self.assertEqual(recovered["action"], "recovered")
        self.assertTrue(verify_library(self.db_path, self.records_root)["ok"])

        issues = verify_library(self.db_path, self.records_root)["issues"]
        codes = {issue["code"] for issue in issues}
        self.assertNotIn("historical_transcript_recovery_prepared", codes)
        self.assertEqual(self._journal_state(), [
            ("historical_transcript_recovery_prepared", 1),
            ("historical_transcript_recovery_applied", 2),
        ])

    def test_apply_preserves_unproven_partial_temporary_file(self) -> None:
        plan = self._plan()
        with mock.patch(
            "lecture_stt.storage_v2.transcript_recovery._write_or_verify_target",
            side_effect=RuntimeError("simulated crash before copy"),
        ):
            with self.assertRaises(HistoricalTranscriptRecoveryRequiredError):
                self._apply(plan)

        job_root = (
            self.records_root
            / self.storage_key
            / "jobs"
            / f"job_{self.storage_key}"
        )
        temporary = (
            job_root
            / (
                ".transcript.txt.historical-transcript-recovery-"
                f"{plan['source_artifacts'][0]['sha256'][:16]}.tmp"
            )
        )
        temporary.write_bytes(b"partial")

        with self.assertRaises(HistoricalTranscriptRecoveryRequiredError):
            self._apply(plan)
        self.assertEqual(temporary.read_bytes(), b"partial")
        self.assertFalse(
            (job_root / "transcript.txt").exists()
        )

    def test_apply_recovers_publish_link_before_temporary_cleanup(self) -> None:
        plan = self._plan()
        with mock.patch(
            "lecture_stt.storage_v2.transcript_recovery.os.unlink",
            side_effect=RuntimeError("simulated crash after publish link"),
        ):
            with self.assertRaises(HistoricalTranscriptRecoveryRequiredError):
                self._apply(plan)

        job_root = (
            self.records_root
            / self.storage_key
            / "jobs"
            / f"job_{self.storage_key}"
        )
        temporary = (
            job_root
            / (
                ".transcript.txt.historical-transcript-recovery-"
                f"{plan['source_artifacts'][0]['sha256'][:16]}.tmp"
            )
        )
        published = job_root / "transcript.txt"
        self.assertTrue(temporary.exists())
        self.assertTrue(published.exists())
        self.assertEqual(temporary.stat().st_ino, published.stat().st_ino)
        self.assertEqual(published.stat().st_nlink, 2)

        recovered = self._apply(plan)
        self.assertEqual(recovered["action"], "recovered")
        self.assertFalse(temporary.exists())
        self.assertEqual(published.stat().st_nlink, 1)
        self.assertTrue(verify_library(self.db_path, self.records_root)["ok"])

    def test_plan_rejects_overlapping_roots(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            plan_historical_transcript_recovery(
                self.db_path,
                self.records_root,
                self.records_root,
                self.storage_key,
            )

    def test_apply_rejects_missing_db_path_without_creating_sqlite(self) -> None:
        plan = self._plan()
        missing_db_path = self.root / "missing-storage-v2.sqlite3"
        self.assertFalse(missing_db_path.exists())

        with self.assertRaises(HistoricalTranscriptRecoveryNotFoundError):
            apply_historical_transcript_recovery(
                missing_db_path,
                self.records_root,
                self.historical_root,
                self.storage_key,
                expected_count=1,
                expected_plan_sha256=str(plan["plan_sha256"]),
                recovery_enabled=True,
                allow_write=True,
            )

        self.assertFalse(missing_db_path.exists())

    def test_apply_rejects_stale_title_changes(self) -> None:
        plan = self._plan()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE recording_titles
                SET title = '변조된 제목'
                WHERE id = (
                    SELECT id FROM recording_titles
                    WHERE recording_id = (
                        SELECT id FROM recordings WHERE storage_key = ?
                    ) AND is_current = 1
                )
                """,
                (self.storage_key,),
            )
            conn.commit()

        with self.assertRaises(HistoricalTranscriptRecoveryConflictError):
            self._apply(plan)

    def test_apply_rejects_stale_transcript_source_in_historical_root(self) -> None:
        plan = self._plan()
        (self.historical_root / f"{self.canonical_base}.txt").write_text(
            "변조 전사 텍스트",
            encoding="utf-8",
        )

        with self.assertRaises(HistoricalTranscriptRecoveryConflictError):
            self._apply(plan)

    def test_apply_does_not_claim_or_delete_a_preexisting_reserved_temp(self) -> None:
        plan = self._plan()
        job_root = (
            self.records_root
            / self.storage_key
            / "jobs"
            / f"job_{self.storage_key}"
        )
        temporary = (
            job_root
            / (
                ".transcript.txt.historical-transcript-recovery-"
                f"{plan['source_artifacts'][0]['sha256'][:16]}.tmp"
            )
        )
        temporary.write_bytes(b"not-owned-by-recovery")

        with self.assertRaises(HistoricalTranscriptRecoveryConflictError):
            self._apply(plan)
        self.assertEqual(temporary.read_bytes(), b"not-owned-by-recovery")
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0],
                0,
            )

    def test_apply_rejects_tampered_review_item(self) -> None:
        plan = self._plan()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE review_items
                SET status = 'resolved', resolved_at = '2026-07-23T02:00:00+09:00'
                WHERE recording_id = (
                    SELECT id FROM recordings WHERE storage_key = ?
                )
                  AND reason_code = 'legacy_import_review'
                """,
                (self.storage_key,),
            )
            conn.commit()

        with self.assertRaises(HistoricalTranscriptRecoveryConflictError):
            self._apply(plan)

    def test_apply_rejects_manifest_tamper_before_apply(self) -> None:
        plan = self._plan()
        manifest_path = self.records_root / self.storage_key / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["title"]["value"] = "변조 제목"
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(HistoricalTranscriptRecoveryConflictError):
            self._apply(plan)

    def test_symlinked_or_hardlinked_or_invalid_historical_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as outer_tmp:
            root = Path(outer_tmp).resolve()
            info = self._seed_recovery_case(
                root,
                storage_key="rec_payload_guard",
                canonical_base="LINKED-BASE-1",
            )
            db_path = root / "storage-v2.sqlite3"
            records_root = root / "records"
            historical_root = root / "historical"

            for mode in ("symlink", "hardlink", "invalid_utf8", "invalid_json"):
                with self.subTest(mode=mode):
                    case = self._seed_recovery_case(
                        root,
                        storage_key=f"rec_payload_guard_{mode}",
                        canonical_base=f"LINKED-{mode.upper()}-1",
                    )
                    db_path = root / "storage-v2.sqlite3"
                    records_root = root / "records"
                    historical_root = root / "historical"
                    txt_path = historical_root / f"LINKED-{mode.upper()}-1.txt"
                    json_path = historical_root / f"LINKED-{mode.upper()}-1.json"
                    txt_path.write_text("good txt", encoding="utf-8")
                    json_path.write_text(json.dumps({"segments": []}), encoding="utf-8")

                    if mode == "symlink":
                        outside = root / "outside.txt"
                        outside.write_text("outside", encoding="utf-8")
                        txt_path.unlink()
                        txt_path.symlink_to(outside)
                    elif mode == "hardlink":
                        try:
                            outside = root / "outside-payload.txt"
                            outside.write_text("outside", encoding="utf-8")
                            txt_path.unlink()
                            os.link(outside, txt_path)
                        except OSError as exc:
                            self.skipTest(f"hardlink unavailable: {exc}")
                    elif mode == "invalid_utf8":
                        txt_path.write_bytes(b"\xff\xfe")
                    elif mode == "invalid_json":
                        json_path.write_text("{bad-json", encoding="utf-8")

                    with self.assertRaises(
                        (
                            HistoricalTranscriptRecoveryConflictError,
                            HistoricalTranscriptRecoveryNotFoundError,
                        )
                    ):
                        plan_historical_transcript_recovery(
                            db_path,
                            records_root,
                            historical_root,
                            f"rec_payload_guard_{mode}",
                        )

                    txt_path.unlink(missing_ok=True)
                    json_path.unlink(missing_ok=True)

    def test_verifier_reports_prepared_and_invalid_recovery_journals(self) -> None:
        plan = self._plan()
        with mock.patch(
            "lecture_stt.storage_v2.transcript_recovery._write_or_verify_target",
            side_effect=RuntimeError("simulated recovery write crash"),
        ):
            with self.assertRaises(HistoricalTranscriptRecoveryRequiredError):
                self._apply(plan)

        prepared_result = verify_library(self.db_path, self.records_root)
        self.assertFalse(prepared_result["ok"])
        self.assertIn(
            "historical_transcript_recovery_prepared",
            {issue["code"] for issue in prepared_result["issues"]},
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE job_events
                SET event_json = '{}'
                WHERE event_type = 'historical_transcript_recovery_prepared'
                """
            )
            conn.commit()

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            root_lock_fd = os.open(self.records_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                issues = historical_transcript_recovery_integrity_issues(conn, root_lock_fd)
            finally:
                os.close(root_lock_fd)
        self.assertIn("historical_transcript_recovery_journal_invalid", {
            issue["code"] for issue in issues
        })

    def test_verifier_detects_deleted_applied_journal(self) -> None:
        plan = self._plan()
        self._apply(plan)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                DELETE FROM job_events
                WHERE event_type IN (?, ?)
                """,
                (
                    "historical_transcript_recovery_prepared",
                    "historical_transcript_recovery_applied",
                ),
            )
            conn.commit()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE review_items
                SET detail_json = '{"issues":[]}',
                    status = 'resolved',
                    resolved_at = '2026-07-27T00:00:00+09:00'
                WHERE reason_code = 'legacy_import_review'
                """
            )
            conn.commit()

        verification = verify_library(self.db_path, self.records_root)
        self.assertFalse(verification["ok"])
        self.assertIn(
            "historical_transcript_recovery_journal_missing",
            {issue["code"] for issue in verification["issues"]},
        )


if __name__ == "__main__":
    unittest.main()
