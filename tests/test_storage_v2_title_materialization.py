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
from lecture_stt.storage_v2.title_materialization import (
    TitleMaterializationConflictError,
    TitleMaterializationRecoveryRequiredError,
    TitleMaterializationWriteDisabledError,
    apply_title_materialization,
    plan_title_materialization,
)
from lecture_stt.storage_v2.title_suggestions import (
    apply_title_suggestion_confirmation,
    apply_title_suggestions,
    list_title_suggestions,
    plan_title_suggestion_confirmation,
    plan_title_suggestions,
    TitleSuggestionConflictError,
)
from lecture_stt.storage_v2.verifier import verify_library


class StorageV2TitleMaterializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "storage-v2.sqlite3"
        self.records_root = self.root / "records"
        self.records_root.mkdir()
        conn = apply_migration(self.db_path)
        conn.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_recording(self, storage_key: str) -> dict[str, object]:
        job_key = f"job_{storage_key}"
        record_root = self.records_root / storage_key
        source_path = record_root / "source" / "original.m4a"
        transcript_path = record_root / "jobs" / job_key / "transcript.txt"
        source_path.parent.mkdir(parents=True)
        transcript_path.parent.mkdir(parents=True)
        source_payload = b"seed-audio-bytes"
        transcript_payload = (
            "보안 대시보드에서는 보안 지표를 기준으로 "
            "보안 등급을 분류한다."
        )
        source_path.write_bytes(source_payload)
        transcript_path.write_text(transcript_payload, encoding="utf-8")
        source_sha = hashlib.sha256(source_payload).hexdigest()
        source_bytes = source_path.stat().st_size
        transcript_sha = hashlib.sha256(
            transcript_payload.encode("utf-8")
        ).hexdigest()
        transcript_bytes = len(transcript_payload.encode("utf-8"))

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            recording_id = int(
                conn.execute(
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
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'available', '2026-07-26T00:00:00+09:00')
                    """,
                    (
                        storage_key,
                        f"{storage_key}.m4a",
                        f"{storage_key}.m4a",
                        "source/original.m4a",
                        source_sha,
                        source_bytes,
                        "audio/mp4",
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
                )
                VALUES (?, '원본 제목', 'filename_inference', 'ko-KR', 1)
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
                VALUES (?, 'general', 'manual', 1)
                """,
                (recording_id,),
            )
            job_id = int(
                conn.execute(
                    """
                    INSERT INTO transcription_jobs(
                        recording_id,
                        job_key,
                        job_relpath,
                        requested_profile,
                        requested_profile_version,
                        queued_at,
                        started_at,
                        finished_at,
                        status,
                        progress,
                        is_current
                    ) VALUES (
                        ?, ?, ?, 'lecture', 'v1',
                        '2026-07-26T00:01:00+09:00',
                        '2026-07-26T00:01:10+09:00',
                        '2026-07-26T00:01:30+09:00',
                        'done', 100, 1
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
                    ) VALUES (
                        ?, ?, 'faster-whisper', 'large-v3',
                        'legacy_import', 'succeeded', 1,
                        '2026-07-26T00:01:10+09:00',
                        '2026-07-26T00:01:20+09:00'
                    )
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
                ) VALUES (?, ?, ?, 'source_copy', 1, ?, ?, ?, 'audio/mp4', 1)
                """,
                (
                    recording_id,
                    job_id,
                    None,
                    "source/original.m4a",
                    source_sha,
                    source_bytes,
                ),
            )
            artifact_id = int(
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
                    ) VALUES (?, ?, ?, 'transcript_raw_text', 1, ?, ?, ?, 'text/plain', 1)
                    """,
                    (
                        recording_id,
                        job_id,
                        engine_run_id,
                        f"jobs/{job_key}/transcript.txt",
                        transcript_sha,
                        transcript_bytes,
                    ),
                ).lastrowid
            )
            conn.executemany(
                """
                INSERT INTO legacy_import_map(
                    legacy_kind,
                    legacy_key,
                    recording_id,
                    source_fingerprint,
                    legacy_snapshot_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        "job",
                        "41",
                        recording_id,
                        "a" * 64,
                        json.dumps(
                            {
                                "canonical_base": "TM-VER-001",
                                "status": "DONE",
                            },
                            sort_keys=True,
                        ),
                    ),
                    (
                        "delivery",
                        "TM-VER-001",
                        recording_id,
                        "a" * 64,
                        json.dumps(
                            {"logical_stem": "TM-VER-001"},
                            sort_keys=True,
                        ),
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        manifest = build_manifest(
            storage_key=storage_key,
            generated_at="2026-07-26T00:01:00+09:00",
            original_name_raw=f"{storage_key}.m4a",
            original_name_nfc=f"{storage_key}.m4a",
            recording_created_at="2026-07-26T00:00:00+09:00",
            source={
                "path": "source/original.m4a",
                "availability": "available",
                "sha256": source_sha,
                "bytes": source_bytes,
                "mime_type": "audio/mp4",
            },
            title={"value": "원본 제목", "source": "filename_inference"},
            context={"type": "general", "source": "manual"},
            jobs=[
                {
                    "job_key": job_key,
                    "status": "done",
                    "requested_profile": "lecture",
                    "requested_profile_version": "v1",
                    "queued_at": "2026-07-26T00:01:00+09:00",
                    "started_at": "2026-07-26T00:01:10+09:00",
                    "finished_at": "2026-07-26T00:01:30+09:00",
                    "engine": {
                        "name": "faster-whisper",
                        "version": "large-v3",
                        "started_at": "2026-07-26T00:01:10+09:00",
                        "finished_at": "2026-07-26T00:01:20+09:00",
                        "stderr_relpath": None,
                        "log_relpath": None,
                    },
                    "artifact_paths": ["jobs/job_%s/transcript.txt" % storage_key],
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
                    "path": f"jobs/{job_key}/transcript.txt",
                    "sha256": transcript_sha,
                    "bytes": transcript_bytes,
                    "mime_type": "text/plain",
                },
            ],
            legacy={
                "kind": "job",
                "job_id": 41,
                "delivery_key": "TM-VER-001",
                "canonical_base": "TM-VER-001",
                "status": "DONE",
                "source_fingerprint": "a" * 64,
            },
        )
        manifest_path = record_root / "manifest.json"
        write_manifest(manifest_path, manifest)
        return {
            "recording_id": recording_id,
            "job_id": job_id,
            "artifact_id": artifact_id,
            "transcript_path": transcript_path,
        }

    def _confirmed_title_proposal(self, storage_key: str) -> int:
        plan = plan_title_suggestions(
            self.db_path,
            self.records_root,
        )
        apply_title_suggestions(
            self.db_path,
            self.records_root,
            expected_count=int(plan["expected_count"]),
            expected_plan_sha256=str(plan["plan_sha256"]),
            suggestions_enabled=True,
            allow_write=True,
        )
        proposal = list_title_suggestions(
            self.db_path,
            status="suggested",
        )["proposals"][0]
        confirmation_plan = plan_title_suggestion_confirmation(
            self.db_path,
            self.records_root,
            int(proposal["id"]),
        )
        apply_title_suggestion_confirmation(
            self.db_path,
            self.records_root,
            int(proposal["id"]),
            expected_count=1,
            expected_plan_sha256=confirmation_plan["plan_sha256"],
            confirmations_enabled=True,
            allow_write=True,
        )
        return int(proposal["id"])

    def _run_apply(self, proposal_id: int) -> dict[str, object]:
        plan = plan_title_materialization(
            self.db_path,
            self.records_root,
            proposal_id,
        )
        return apply_title_materialization(
            self.db_path,
            self.records_root,
            proposal_id,
            expected_count=1,
            expected_plan_sha256=str(plan["plan_sha256"]),
            materializations_enabled=True,
            allow_write=True,
        )

    def test_plan_refuses_non_confirmed_content_title_proposal(self) -> None:
        self._seed_recording("rec_not_confirmed")
        seed_plan = plan_title_suggestions(
            self.db_path,
            self.records_root,
        )
        apply_title_suggestions(
            self.db_path,
            self.records_root,
            expected_count=int(seed_plan["expected_count"]),
            expected_plan_sha256=str(seed_plan["plan_sha256"]),
            suggestions_enabled=True,
            allow_write=True,
        )
        proposal = list_title_suggestions(self.db_path, status="suggested")[
            "proposals"
        ][0]

        with self.assertRaises(TitleMaterializationConflictError):
            plan_title_materialization(
                self.db_path,
                self.records_root,
                int(proposal["id"]),
            )

    def test_apply_rejects_without_required_guards(self) -> None:
        self._seed_recording("rec_guard")
        proposal_id = self._confirmed_title_proposal("rec_guard")
        plan = plan_title_materialization(
            self.db_path,
            self.records_root,
            proposal_id,
        )

        with self.assertRaisesRegex(
            ValueError,
            "expected_plan_sha256 must be a canonical SHA-256",
        ):
            apply_title_materialization(
                self.db_path,
                self.records_root,
                proposal_id,
                expected_count=1,
                expected_plan_sha256="bad-sha",
                materializations_enabled=True,
                allow_write=True,
            )

        with self.assertRaisesRegex(
            TitleMaterializationConflictError,
            "expected_count must equal 1",
        ):
            apply_title_materialization(
                self.db_path,
                self.records_root,
                proposal_id,
                expected_count=2,
                expected_plan_sha256=str(plan["plan_sha256"]),
                materializations_enabled=True,
                allow_write=True,
            )

        with self.assertRaises(TitleMaterializationWriteDisabledError):
            apply_title_materialization(
                self.db_path,
                self.records_root,
                proposal_id,
                expected_count=1,
                expected_plan_sha256=str(plan["plan_sha256"]),
                materializations_enabled=False,
                allow_write=True,
            )

        with self.assertRaises(TitleMaterializationWriteDisabledError):
            apply_title_materialization(
                self.db_path,
                self.records_root,
                proposal_id,
                expected_count=1,
                expected_plan_sha256=str(plan["plan_sha256"]),
                materializations_enabled=True,
                allow_write=False,
            )

    def test_plan_matches_expected_contract_for_confirmed_proposal(self) -> None:
        self._seed_recording("rec_plan_contract")
        proposal_id = self._confirmed_title_proposal("rec_plan_contract")

        plan = plan_title_materialization(
            self.db_path,
            self.records_root,
            proposal_id,
        )

        self.assertEqual(
            plan["schema_version"],
            "storage-v2/title-materialization-plan@1",
        )
        self.assertEqual(
            set(plan),
            {
                "schema_version",
                "proposal_id",
                "storage_key",
                "suggestion_reason",
                "previous_title",
                "target_title",
                "max_transcript_bytes",
                "expected_count",
                "materialization",
                "mode",
                "plan_sha256",
                "materialization_state",
            },
        )
        self.assertEqual(plan["materialization"], "canonical_content_title_manifest")
        self.assertEqual(plan["expected_count"], 1)
        self.assertEqual(plan["mode"], "read_only")
        self.assertEqual(plan["materialization_state"], "not_prepared")
        self.assertEqual(
            plan["previous_title"],
            {"value": "원본 제목", "source": "filename_inference"},
        )
        self.assertEqual(
            plan["target_title"]["source"],
            "system",
        )
        self.assertGreater(plan["max_transcript_bytes"], 0)
        public_json = json.dumps(plan, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            '"transcript_path',
            '"transcript_sha256"',
            '"artifact_id"',
            '"recording_id"',
            '"manifest":',
            '"path_rel"',
            '"review_item_id"',
        ):
            self.assertNotIn(forbidden, public_json)

    def test_apply_rejects_wrong_valid_plan_sha256(self) -> None:
        self._seed_recording("rec_wrong_plan_sha")
        proposal_id = self._confirmed_title_proposal("rec_wrong_plan_sha")

        with self.assertRaises(TitleMaterializationConflictError):
            apply_title_materialization(
                self.db_path,
                self.records_root,
                proposal_id,
                expected_count=1,
                expected_plan_sha256="f" * 64,
                materializations_enabled=True,
                allow_write=True,
            )

    def test_apply_rejects_mismatched_max_transcript_bytes(self) -> None:
        self._seed_recording("rec_max_bytes")
        proposal_id = self._confirmed_title_proposal("rec_max_bytes")
        plan = plan_title_materialization(
            self.db_path,
            self.records_root,
            proposal_id,
        )

        _ = self._run_apply(
            proposal_id,
        )

        with self.assertRaisesRegex(
            TitleMaterializationConflictError,
            "replay must use the planned",
        ):
            apply_title_materialization(
                self.db_path,
                self.records_root,
                proposal_id,
                expected_count=int(plan["expected_count"]),
                expected_plan_sha256=str(plan["plan_sha256"]),
                materializations_enabled=True,
                allow_write=True,
                max_transcript_bytes=1024,
            )

    def test_plan_refuses_hard_linked_manifest(self) -> None:
        self._seed_recording("rec_hardlink")
        proposal_id = self._confirmed_title_proposal("rec_hardlink")
        manifest_path = self.records_root / "rec_hardlink" / "manifest.json"
        hardlink_path = self.root / "manifest-hard-link-title.json"
        os.link(manifest_path, hardlink_path)

        with self.assertRaisesRegex(
            TitleMaterializationConflictError,
            "manifest_unreadable",
        ):
            plan_title_materialization(
                self.db_path,
                self.records_root,
                proposal_id,
            )

    def test_confirmed_and_materialized_replays_are_distinct_states(self) -> None:
        storage_key = "rec_replay_distinct"
        seeded = self._seed_recording(storage_key)
        proposal_id = self._confirmed_title_proposal("rec_replay_distinct")
        plan = plan_title_materialization(
            self.db_path,
            self.records_root,
            proposal_id,
        )
        manifest_path = self.records_root / storage_key / "manifest.json"
        manifest_before = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_before = (
            self.records_root / storage_key / "source" / "original.m4a"
        ).read_bytes()
        transcript_before = Path(seeded["transcript_path"]).read_bytes()

        conn = sqlite3.connect(self.db_path)
        try:
            recording_before = conn.execute(
                """
                SELECT storage_key, source_relpath, original_name_raw,
                       original_name_nfc
                FROM recordings
                WHERE id = ?
                """,
                (seeded["recording_id"],),
            ).fetchone()
            contexts_before = conn.execute(
                """
                SELECT id, context_type, label, source, is_selected
                FROM recording_contexts
                WHERE recording_id = ?
                ORDER BY id
                """,
                (seeded["recording_id"],),
            ).fetchall()
            artifact_paths_before = conn.execute(
                """
                SELECT id, path_rel
                FROM artifacts
                WHERE recording_id = ?
                ORDER BY id
                """,
                (seeded["recording_id"],),
            ).fetchall()
            title_before = conn.execute(
                """
                SELECT id, title, title_source, locale, confidence, is_current
                FROM recording_titles
                WHERE recording_id = ?
                """,
                (seeded["recording_id"],),
            ).fetchone()
            confirmation_digest = conn.execute(
                """
                SELECT confirmation_plan_sha256
                FROM recording_title_proposals
                WHERE id = ?
                """,
                (proposal_id,),
            ).fetchone()[0]
        finally:
            conn.close()

        first = apply_title_materialization(
            self.db_path,
            self.records_root,
            proposal_id,
            expected_count=1,
            expected_plan_sha256=str(plan["plan_sha256"]),
            materializations_enabled=True,
            allow_write=True,
        )
        second = self._run_apply(proposal_id)

        self.assertEqual(first["action"], "materialized")
        self.assertTrue(first["canonical_metadata_changed"])
        self.assertEqual(second["action"], "skipped")
        self.assertFalse(second["canonical_metadata_changed"])

        with self.assertRaises(TitleSuggestionConflictError):
            apply_title_suggestion_confirmation(
                self.db_path,
                self.records_root,
                proposal_id,
                expected_count=1,
                expected_plan_sha256=str(confirmation_digest),
                confirmations_enabled=True,
                allow_write=True,
            )

        manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {key: value for key, value in manifest_after.items() if key != "title"},
            {key: value for key, value in manifest_before.items() if key != "title"},
        )
        self.assertEqual(
            manifest_after["title"],
            {
                "value": plan["target_title"]["value"],
                "source": plan["target_title"]["source"],
            },
        )
        self.assertEqual(
            (
                self.records_root / storage_key / "source" / "original.m4a"
            ).read_bytes(),
            source_before,
        )
        self.assertEqual(
            Path(seeded["transcript_path"]).read_bytes(),
            transcript_before,
        )

        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT state
                FROM recording_title_materializations
                WHERE proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
            self.assertEqual(str(row["state"]), "applied")
            recording_after = conn.execute(
                """
                SELECT storage_key, source_relpath, original_name_raw,
                       original_name_nfc
                FROM recordings
                WHERE id = ?
                """,
                (seeded["recording_id"],),
            ).fetchone()
            self.assertEqual(tuple(recording_after), tuple(recording_before))
            contexts_after = conn.execute(
                """
                SELECT id, context_type, label, source, is_selected
                FROM recording_contexts
                WHERE recording_id = ?
                ORDER BY id
                """,
                (seeded["recording_id"],),
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in contexts_after],
                [tuple(row) for row in contexts_before],
            )
            artifact_paths_after = conn.execute(
                """
                SELECT id, path_rel
                FROM artifacts
                WHERE recording_id = ?
                ORDER BY id
                """,
                (seeded["recording_id"],),
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in artifact_paths_after],
                [tuple(row) for row in artifact_paths_before],
            )
            titles = conn.execute(
                """
                SELECT id, title, title_source, locale, confidence, is_current
                FROM recording_titles
                WHERE recording_id = ?
                ORDER BY id
                """,
                (seeded["recording_id"],),
            ).fetchall()
            self.assertEqual(len(titles), 2)
            self.assertEqual(tuple(titles[0]), (*tuple(title_before)[:-1], 0))
            self.assertEqual(
                (
                    titles[1]["title"],
                    titles[1]["title_source"],
                    titles[1]["is_current"],
                ),
                (
                    plan["target_title"]["value"],
                    "system",
                    1,
                ),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    DELETE FROM recording_title_materializations
                    WHERE proposal_id = ?
                    """,
                    (proposal_id,),
                )
            conn.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    UPDATE recording_title_materializations
                    SET materialization_plan_sha256 = ?
                    WHERE proposal_id = ?
                    """,
                    ("0" * 64, proposal_id),
                )
            conn.rollback()
        finally:
            conn.close()
        self.assertTrue(verify_library(self.db_path, self.records_root)["ok"])

    def test_replay_rejects_transcript_change(self) -> None:
        self._seed_recording("rec_stale_transcript")
        proposal_id = self._confirmed_title_proposal("rec_stale_transcript")

        payload = self._seeded_transcript_path("rec_stale_transcript")
        Path(payload).write_text(
            "변조된 전사 내용",
            encoding="utf-8",
        )

        with self.assertRaises(Exception) as ctx:
            self._run_apply(proposal_id)
        self.assertIsInstance(
            ctx.exception,
            (
                TitleMaterializationConflictError,
                TitleSuggestionConflictError,
            ),
        )

    def _seeded_transcript_path(self, storage_key: str) -> Path:
        return self.records_root / storage_key / "jobs" / f"job_{storage_key}" / "transcript.txt"

    def test_replay_rejects_current_title_change(self) -> None:
        storage_key = "rec_title_changed"
        self._seed_recording(storage_key)
        proposal_id = self._confirmed_title_proposal(storage_key)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE recording_titles
                SET title = '변경된 사용자 표시'
                WHERE is_current = 1 AND recording_id = (
                    SELECT recording_id
                    FROM recording_title_proposals
                    WHERE id = ?
                )
                """,
                (proposal_id,),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises((
            TitleMaterializationConflictError,
            TitleSuggestionConflictError,
        )):
            self._run_apply(proposal_id)

    def test_replay_rejects_tampered_review_evidence(self) -> None:
        storage_key = "rec_review_tamper"
        self._seed_recording(storage_key)
        proposal_id = self._confirmed_title_proposal(storage_key)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE review_items
                SET detail_json = '{}'
                WHERE id = (
                    SELECT review_item_id
                    FROM recording_title_proposals
                    WHERE id = ?
                )
                """,
                (proposal_id,),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises((
            TitleMaterializationConflictError,
            TitleSuggestionConflictError,
        )):
            self._run_apply(proposal_id)

    def test_replay_rejects_tampered_review_resolved_time(self) -> None:
        storage_key = "rec_review_tamper_time"
        self._seed_recording(storage_key)
        proposal_id = self._confirmed_title_proposal(storage_key)
        plan = plan_title_materialization(
            self.db_path,
            self.records_root,
            proposal_id,
        )

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE review_items
                SET resolved_at = '2099-01-01T00:00:00+00:00'
                WHERE id = (
                    SELECT review_item_id
                    FROM recording_title_proposals
                    WHERE id = ?
                )
                """,
                (proposal_id,),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(TitleMaterializationConflictError):
            apply_title_materialization(
                self.db_path,
                self.records_root,
                proposal_id,
                expected_count=1,
                expected_plan_sha256=str(plan["plan_sha256"]),
                materializations_enabled=True,
                allow_write=True,
            )
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM recording_title_materializations"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_replay_rejects_materialized_state_tamper(self) -> None:
        storage_key = "rec_materialized_replay_tamper"
        self._seed_recording(storage_key)
        proposal_id = self._confirmed_title_proposal(storage_key)

        first = self._run_apply(proposal_id)
        self.assertEqual(first["action"], "materialized")

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE review_items
                SET detail_json = '{}',
                    resolved_at = NULL,
                    status = 'open'
                WHERE id = (
                    SELECT review_item_id
                    FROM recording_title_proposals
                    WHERE id = ?
                )
                """,
                (proposal_id,),
            )
            conn.commit()
        finally:
            conn.close()

        verification = verify_library(self.db_path, self.records_root)
        self.assertFalse(verification["ok"])
        self.assertIn(
            "title_materialization_journal_invalid",
            {issue["code"] for issue in verification["issues"]},
        )
        with self.assertRaises((
            TitleMaterializationConflictError,
            TitleSuggestionConflictError,
        )):
            self._run_apply(proposal_id)

    def test_applied_current_title_tamper_fails_verifier_and_replay(self) -> None:
        storage_key = "rec_applied_title_tamper"
        self._seed_recording(storage_key)
        proposal_id = self._confirmed_title_proposal(storage_key)
        self.assertEqual(self._run_apply(proposal_id)["action"], "materialized")

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE recording_titles
                SET title = '변조된 applied 제목'
                WHERE id = (
                    SELECT materialized_title_id
                    FROM recording_title_materializations
                    WHERE proposal_id = ?
                )
                """,
                (proposal_id,),
            )
            conn.commit()
        finally:
            conn.close()

        verification = verify_library(self.db_path, self.records_root)
        self.assertFalse(verification["ok"])
        self.assertIn(
            "title_materialization_journal_invalid",
            {issue["code"] for issue in verification["issues"]},
        )
        with self.assertRaises(TitleMaterializationConflictError):
            self._run_apply(proposal_id)

    def test_recoverable_prepared_state_returns_recovered(self) -> None:
        storage_key = "rec_recoverable"
        self._seed_recording(storage_key)
        proposal_id = self._confirmed_title_proposal(storage_key)

        with mock.patch(
            "lecture_stt.storage_v2.title_materialization._replace_record_manifest",
            side_effect=RuntimeError("simulated manifest crash"),
        ):
            with self.assertRaises(TitleMaterializationRecoveryRequiredError):
                self._run_apply(proposal_id)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT state, COUNT(*) AS total
                FROM recording_title_materializations
                WHERE proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
            self.assertEqual(str(row["state"]), "prepared")
            self.assertEqual(int(row["total"]), 1)
        finally:
            conn.close()

        recovered = self._run_apply(proposal_id)
        self.assertEqual(recovered["action"], "recovered")
        self.assertTrue(recovered["canonical_metadata_changed"])
        verify = verify_library(self.db_path, self.records_root)
        self.assertTrue(verify["ok"])

    def test_replay_after_manifest_write_failure_is_recoverable(self) -> None:
        storage_key = "rec_recoverable_manifest_write"
        self._seed_recording(storage_key)
        proposal_id = self._confirmed_title_proposal(storage_key)

        with mock.patch(
            "lecture_stt.storage_v2.title_materialization._finalize_materialization",
            side_effect=RuntimeError("simulated finalize crash"),
        ):
            with self.assertRaises(TitleMaterializationRecoveryRequiredError):
                self._run_apply(proposal_id)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT state, COUNT(*) AS total
                FROM recording_title_materializations
                WHERE proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
            self.assertEqual(str(row["state"]), "prepared")
            self.assertEqual(int(row["total"]), 1)
        finally:
            conn.close()

        recovered = self._run_apply(proposal_id)
        self.assertEqual(recovered["action"], "recovered")
        self.assertTrue(
            verify_library(self.db_path, self.records_root)["ok"]
        )

    def test_cli_plan_and_apply_guarded(self) -> None:
        storage_key = "rec_cli_materialization"
        self._seed_recording(storage_key)
        proposal_id = self._confirmed_title_proposal(storage_key)

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            plan_code = cli.main(
                [
                    "plan-title-materialization",
                    "--v2-db",
                    str(self.db_path),
                    "--records-root",
                    str(self.records_root),
                    "--proposal-id",
                    str(proposal_id),
                    "--json",
                ]
            )
        self.assertEqual(plan_code, 0)
        plan = json.loads(stdout.getvalue())

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            denied = cli.main(
                [
                    "apply-title-materialization",
                    "--v2-db",
                    str(self.db_path),
                    "--records-root",
                    str(self.records_root),
                    "--proposal-id",
                    str(proposal_id),
                    "--expected-count",
                    "1",
                    "--expected-plan-sha256",
                    plan["plan_sha256"],
                    "--json",
                ]
            )
        self.assertEqual(denied, 2)
        self.assertIn("disabled", stderr.getvalue())

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            applied = cli.main(
                [
                    "apply-title-materialization",
                    "--v2-db",
                    str(self.db_path),
                    "--records-root",
                    str(self.records_root),
                    "--proposal-id",
                    str(proposal_id),
                    "--expected-count",
                    "1",
                    "--expected-plan-sha256",
                    plan["plan_sha256"],
                    "--enable-materialization",
                    "--allow-write",
                    "--json",
                ]
            )
        self.assertEqual(applied, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["action"], "materialized")


if __name__ == "__main__":
    unittest.main()
