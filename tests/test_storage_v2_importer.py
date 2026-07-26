from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unicodedata
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from unittest.mock import patch

from lecture_stt.shared import db
from lecture_stt.storage_v2 import importer as storage_importer
from lecture_stt.storage_v2.importer import (
    discover_candidates,
    import_candidate,
)
from lecture_stt.storage_v2.cli import (
    compute_plan_digest,
    main as storage_cli_main,
)
from lecture_stt.storage_v2.manifest import (
    ManifestValidationError,
    build_manifest,
)
from lecture_stt.storage_v2.repository import read_library_snapshot
from lecture_stt.storage_v2.verifier import verify_library


class StorageV2ImporterTests(unittest.TestCase):
    def _fixture(self, root: Path) -> dict[str, Path | str | int]:
        legacy_root = root / "legacy"
        audio_dir = legacy_root / "01_audio"
        transcript_dir = legacy_root / "02_transcripts"
        correction_dir = legacy_root / "03_correction"
        summary_dir = legacy_root / "04_summarize"
        for directory in (audio_dir, transcript_dir, correction_dir, summary_dir):
            directory.mkdir(parents=True)

        canonical_base = "260723DS_2"
        audio_path = audio_dir / f"{canonical_base}.m4a"
        transcript_txt = transcript_dir / f"{canonical_base}.txt"
        transcript_json = transcript_dir / f"{canonical_base}.json"
        quality_json = transcript_dir / f"{canonical_base}.quality.json"
        correction_txt = correction_dir / f"{canonical_base}.txt"
        correction_json = correction_dir / f"{canonical_base}.json"
        summary_md = summary_dir / f"{canonical_base}.md"

        audio_path.write_bytes(b"fixture-audio")
        transcript_txt.write_text("PRIVATE TRANSCRIPT BODY", encoding="utf-8")
        transcript_json.write_text(
            json.dumps(
                {
                    "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "PRIVATE"}],
                    "metadata": {"profile": {"key": "lecture", "version": "1"}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        quality_json.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "lecture_stt_quality_scorecard",
                    "canonical_base": canonical_base,
                    "health": "good",
                }
            ),
            encoding="utf-8",
        )
        correction_txt.write_text("PRIVATE CORRECTION BODY", encoding="utf-8")
        correction_json.write_text(
            json.dumps({"segments": [{"id": 0, "text": "PRIVATE CORRECTION"}]}),
            encoding="utf-8",
        )
        summary_md.write_text("# PRIVATE SUMMARY BODY", encoding="utf-8")
        correction_txt_sha = hashlib.sha256(correction_txt.read_bytes()).hexdigest()
        correction_json_sha = hashlib.sha256(correction_json.read_bytes()).hexdigest()
        summary_md_sha = hashlib.sha256(summary_md.read_bytes()).hexdigest()

        legacy_db = root / "legacy.sqlite3"
        conn = db.init_db(str(legacy_db))
        try:
            original_name = unicodedata.normalize("NFD", "마유로.m4a")
            source_sha = hashlib.sha256(audio_path.read_bytes()).hexdigest()
            cursor = conn.execute(
                """
                INSERT INTO jobs(
                    status,
                    created_at,
                    updated_at,
                    orig_inbox_path,
                    orig_name,
                    canonical_base,
                    canonical_audio_path,
                    transcript_txt_path,
                    transcript_json_path,
                    sha256,
                    started_at,
                    ended_at,
                    preprocess_sec,
                    transcribe_sec,
                    total_sec,
                    engine_params,
                    is_deduped,
                    current_step,
                    progress_pct
                )
                VALUES (
                    'DONE',
                    '2026-07-23T09:00:00+09:00',
                    '2026-07-23T10:00:00+09:00',
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    '2026-07-23T09:01:00+09:00',
                    '2026-07-23T10:00:00+09:00',
                    1.0,
                    2.0,
                    3.0,
                    ?,
                    0,
                    '완료',
                    100
                )
                """,
                (
                    str(legacy_root / "00_inbox" / original_name),
                    original_name,
                    canonical_base,
                    str(audio_path),
                    str(transcript_txt),
                    str(transcript_json),
                    source_sha,
                    json.dumps(
                        {
                            "engine": "faster-whisper",
                            "model_size": "large-v3",
                            "profile": {
                                "key": "lecture",
                                "version": "2026-07-23.1",
                            },
                        }
                    ),
                ),
            )
            job_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO deliveries(
                    logical_stem,
                    source_job_id,
                    subject_abbr,
                    correction_txt_path,
                    correction_json_path,
                    summary_md_path,
                    correction_txt_sha256,
                    correction_json_sha256,
                    summary_md_sha256,
                    correction_status,
                    summary_status,
                    updated_at
                )
                VALUES (?, ?, 'DS', ?, ?, ?, ?, ?, ?, 'DELIVERED', 'DELIVERED', ?)
                """,
                (
                    canonical_base,
                    job_id,
                    str(correction_txt),
                    str(correction_json),
                    str(summary_md),
                    correction_txt_sha,
                    correction_json_sha,
                    summary_md_sha,
                    "2026-07-23T10:00:00+09:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        return {
            "legacy_root": legacy_root,
            "legacy_db": legacy_db,
            "job_id": job_id,
            "canonical_base": canonical_base,
            "audio_path": audio_path,
            "transcript_txt": transcript_txt,
            "transcript_json": transcript_json,
            "quality_json": quality_json,
            "correction_txt": correction_txt,
            "correction_json": correction_json,
            "summary_md": summary_md,
        }

    def _clone_job(
        self,
        conn: sqlite3.Connection,
        *,
        source_job_id: int,
        canonical_base: str,
        transcript_txt_path: Path | str,
        transcript_json_path: Path | str,
        status: str = "DONE",
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO jobs(
                status,
                created_at,
                updated_at,
                orig_inbox_path,
                orig_name,
                canonical_base,
                canonical_audio_path,
                transcript_txt_path,
                transcript_json_path,
                sha256,
                started_at,
                ended_at,
                preprocess_sec,
                transcribe_sec,
                total_sec,
                engine_params,
                is_deduped,
                current_step,
                progress_pct
            )
            SELECT
                ?,
                created_at,
                updated_at,
                orig_inbox_path,
                orig_name,
                ?,
                canonical_audio_path,
                ?,
                ?,
                sha256,
                started_at,
                ended_at,
                preprocess_sec,
                transcribe_sec,
                total_sec,
                engine_params,
                is_deduped,
                current_step,
                progress_pct
            FROM jobs
            WHERE id = ?
            """,
            (
                status,
                canonical_base,
                str(transcript_txt_path),
                str(transcript_json_path),
                source_job_id,
            ),
        )
        return int(cursor.lastrowid)

    def test_discover_and_apply_preserves_source_and_builds_record_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
                job_ids=[int(fixture["job_id"])],
            )[0]

            self.assertTrue(plan.can_apply)
            self.assertTrue(plan.candidate.source_available)
            self.assertEqual(plan.candidate.original_name_nfc, "마유로.m4a")
            self.assertEqual(plan.candidate.v2_status, "done")
            self.assertEqual(
                {
                    artifact.kind for artifact in plan.candidate.artifacts
                },
                {
                    "source_copy",
                    "transcript_raw_text",
                    "transcript_segments_json",
                    "quality_scorecard",
                    "correction_text",
                    "correction_json",
                    "summary_markdown",
                },
            )
            serialized_plan = json.dumps(plan.as_dict(), ensure_ascii=False)
            self.assertNotIn("PRIVATE TRANSCRIPT BODY", serialized_plan)
            self.assertNotIn("PRIVATE CORRECTION BODY", serialized_plan)
            self.assertNotIn("PRIVATE SUMMARY BODY", serialized_plan)

            source_before = Path(fixture["audio_path"]).read_bytes()
            target_db = root / "v2.sqlite3"
            records_root = root / "records"
            result = import_candidate(
                plan,
                target_db_path=target_db,
                records_root=records_root,
            )

            self.assertEqual(result.action, "imported")
            self.assertEqual(Path(fixture["audio_path"]).read_bytes(), source_before)
            record_root = records_root / result.storage_key
            manifest = json.loads((record_root / "manifest.json").read_text(encoding="utf-8"))
            serialized_manifest = json.dumps(manifest, ensure_ascii=False)
            self.assertEqual(manifest["source"]["availability"], "available")
            self.assertNotIn("PRIVATE TRANSCRIPT BODY", serialized_manifest)
            self.assertNotIn("PRIVATE CORRECTION BODY", serialized_manifest)
            self.assertNotIn("PRIVATE SUMMARY BODY", serialized_manifest)
            for artifact in plan.candidate.artifacts:
                self.assertTrue((record_root / artifact.target_relpath).is_file())

            with sqlite3.connect(target_db) as conn:
                source_state = conn.execute(
                    "SELECT source_state FROM recordings"
                ).fetchone()[0]
                self.assertEqual(source_state, "available")
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
                    len(plan.candidate.artifacts),
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM legacy_import_map").fetchone()[0],
                    2,
                )

            second = import_candidate(
                plan,
                target_db_path=target_db,
                records_root=records_root,
            )
            self.assertEqual(second.action, "skipped")
            snapshot = read_library_snapshot(target_db)
            self.assertTrue(snapshot["available"])
            self.assertEqual(snapshot["counts"]["recordings"], 1)
            self.assertEqual(snapshot["counts"]["done"], 1)
            self.assertEqual(snapshot["recordings"][0]["source_state"], "available")
            verification = verify_library(target_db, records_root)
            self.assertTrue(verification["ok"])
            self.assertEqual(
                verification["checked_artifacts"],
                len(plan.candidate.artifacts),
            )

            copied_transcript = (
                record_root
                / next(
                    artifact.target_relpath
                    for artifact in plan.candidate.artifacts
                    if artifact.kind == "transcript_raw_text"
                )
            )
            copied_transcript.write_text("tampered", encoding="utf-8")
            tampered = verify_library(target_db, records_root)
            self.assertFalse(tampered["ok"])
            self.assertIn(
                "artifact_size_mismatch",
                {issue["code"] for issue in tampered["issues"]},
            )
            with self.assertRaisesRegex(RuntimeError, "size mismatch"):
                import_candidate(
                    plan,
                    target_db_path=target_db,
                    records_root=records_root,
                )

    def test_missing_source_uses_explicit_marker_and_review_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            Path(fixture["audio_path"]).unlink()
            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]

            self.assertTrue(plan.can_apply)
            self.assertFalse(plan.candidate.source_available)
            self.assertIn("source_missing", {issue.code for issue in plan.candidate.issues})
            marker = next(
                artifact
                for artifact in plan.candidate.artifacts
                if artifact.target_relpath == "source/unavailable.json"
            )
            self.assertIsNone(marker.source_path)
            self.assertIsNotNone(marker.generated_bytes)

            target_db = root / "v2.sqlite3"
            records_root = root / "records"
            result = import_candidate(
                plan,
                target_db_path=target_db,
                records_root=records_root,
            )
            marker_path = records_root / result.storage_key / "source" / "unavailable.json"
            marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker_payload["availability"], "missing")
            self.assertEqual(marker_payload["reason"], "legacy_source_missing")
            with sqlite3.connect(target_db) as conn:
                row = conn.execute(
                    "SELECT source_state, source_error_code FROM recordings"
                ).fetchone()
                self.assertEqual(row, ("missing", "LEGACY_SOURCE_MISSING"))
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM review_items").fetchone()[0],
                    1,
                )

    def test_missing_source_without_legacy_hash_keeps_manifest_and_db_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                conn.execute("UPDATE jobs SET sha256 = NULL")
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            Path(fixture["audio_path"]).unlink()

            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            target_db = root / "v2.sqlite3"
            records_root = root / "records"
            result = import_candidate(
                plan,
                target_db_path=target_db,
                records_root=records_root,
            )

            manifest = json.loads(
                (records_root / result.storage_key / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIsNone(manifest["source"]["sha256"])
            with sqlite3.connect(target_db) as conn:
                self.assertIsNone(
                    conn.execute("SELECT ingest_sha256 FROM recordings").fetchone()[0]
                )
            self.assertTrue(verify_library(target_db, records_root)["ok"])

    def test_outside_root_and_symlink_sources_are_blocked_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            outside = root / "outside.m4a"
            outside.write_bytes(b"outside")
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                conn.execute(
                    "UPDATE jobs SET canonical_audio_path = ?",
                    (str(outside),),
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            self.assertFalse(plan.can_apply)
            self.assertIn(
                "source_outside_root",
                {issue.code for issue in plan.candidate.issues},
            )
            target_db = root / "v2.sqlite3"
            with self.assertRaises(RuntimeError):
                import_candidate(
                    plan,
                    target_db_path=target_db,
                    records_root=root / "records",
                )
            self.assertFalse(target_db.exists())

            link_path = Path(fixture["legacy_root"]) / "01_audio" / "linked.m4a"
            try:
                os.symlink(Path(fixture["audio_path"]), link_path)
            except (OSError, NotImplementedError):
                return
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                conn.execute(
                    "UPDATE jobs SET canonical_audio_path = ?",
                    (str(link_path),),
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            symlink_plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            self.assertFalse(symlink_plan.can_apply)
            self.assertIn(
                "source_symlink",
                {issue.code for issue in symlink_plan.candidate.issues},
            )

    def test_incomplete_done_transcript_is_imported_as_needs_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            Path(fixture["transcript_txt"]).unlink()
            Path(fixture["transcript_json"]).unlink()
            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]

            self.assertTrue(plan.can_apply)
            self.assertEqual(plan.candidate.v2_status, "needs_review")
            self.assertIn(
                "transcript_pair_incomplete",
                {issue.code for issue in plan.candidate.issues},
            )
            result = import_candidate(
                plan,
                target_db_path=root / "v2.sqlite3",
                records_root=root / "records",
            )
            snapshot = read_library_snapshot(root / "v2.sqlite3")
            self.assertEqual(result.action, "imported")
            self.assertEqual(snapshot["counts"]["needs_review"], 1)
            self.assertEqual(snapshot["counts"]["open_reviews"], 1)

    def test_changed_legacy_artifact_fingerprint_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            first_plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            target_db = root / "v2.sqlite3"
            records_root = root / "records"
            import_candidate(
                first_plan,
                target_db_path=target_db,
                records_root=records_root,
            )

            Path(fixture["transcript_txt"]).write_text(
                "CHANGED PRIVATE TRANSCRIPT",
                encoding="utf-8",
            )
            changed_plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            self.assertNotEqual(
                first_plan.candidate.source_fingerprint,
                changed_plan.candidate.source_fingerprint,
            )
            with self.assertRaisesRegex(RuntimeError, "different source fingerprint"):
                import_candidate(
                    changed_plan,
                    target_db_path=target_db,
                    records_root=records_root,
                )

    def test_same_length_source_mutation_is_blocked_by_legacy_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            audio_path = Path(fixture["audio_path"])
            original = audio_path.read_bytes()
            mutated = bytes([original[0] ^ 1]) + original[1:]
            self.assertEqual(len(mutated), len(original))
            audio_path.write_bytes(mutated)

            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            self.assertFalse(plan.can_apply)
            self.assertIn(
                "source_hash_mismatch",
                {issue.code for issue in plan.candidate.issues},
            )

    def test_malformed_nonempty_source_hash_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                conn.execute("UPDATE jobs SET sha256 = 'malformed'")
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]

            self.assertTrue(plan.can_apply)
            self.assertIn(
                "source_hash_invalid",
                {issue.code for issue in plan.candidate.issues},
            )
            source_artifact = next(
                artifact
                for artifact in plan.candidate.artifacts
                if artifact.kind == "source_copy"
            )
            self.assertEqual(
                plan.candidate.source_sha256,
                source_artifact.sha256,
            )

    def test_strict_readonly_plan_refuses_uncheckpointed_wal_without_shm_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            legacy_db = Path(fixture["legacy_db"])
            wal_path = legacy_db.with_name(f"{legacy_db.name}-wal")
            shm_path = legacy_db.with_name(f"{legacy_db.name}-shm")
            shm_path.unlink(missing_ok=True)
            wal_path.write_bytes(b"uncheckpointed")

            with self.assertRaisesRegex(RuntimeError, "standalone SQLite backup"):
                discover_candidates(legacy_db, fixture["legacy_root"])
            self.assertFalse(shm_path.exists())

    def test_strict_snapshot_detects_post_connect_and_post_query_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            legacy_db = Path(fixture["legacy_db"])
            wal_path = legacy_db.with_name(f"{legacy_db.name}-wal")
            real_connect = sqlite3.connect

            def connect_then_create_wal(*args: object, **kwargs: object) -> sqlite3.Connection:
                connection = real_connect(*args, **kwargs)
                wal_path.write_bytes(b"raced-wal")
                return connection

            with patch.object(
                storage_importer.sqlite3,
                "connect",
                side_effect=connect_then_create_wal,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "standalone SQLite backup",
                ):
                    discover_candidates(
                        legacy_db,
                        fixture["legacy_root"],
                    )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            legacy_db = Path(fixture["legacy_db"])
            original_row_dict = storage_importer._row_dict
            mutated = False

            def mutate_after_query(row: sqlite3.Row | None) -> dict[str, object]:
                nonlocal mutated
                result = original_row_dict(row)
                if not mutated:
                    mutated = True
                    with legacy_db.open("ab") as handle:
                        handle.write(b"snapshot-race")
                return result

            with patch.object(
                storage_importer,
                "_row_dict",
                side_effect=mutate_after_query,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed during"):
                    discover_candidates(
                        legacy_db,
                        fixture["legacy_root"],
                    )

    def test_strict_snapshot_rejects_journal_hardlink_and_corrupt_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            legacy_db = Path(fixture["legacy_db"])
            journal_path = legacy_db.with_name(f"{legacy_db.name}-journal")
            journal_path.write_bytes(b"rollback journal")
            with self.assertRaisesRegex(RuntimeError, "standalone SQLite backup"):
                discover_candidates(legacy_db, fixture["legacy_root"])

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            legacy_db = Path(fixture["legacy_db"])
            alias = root / "legacy-hardlink.sqlite3"
            try:
                os.link(legacy_db, alias)
            except OSError as exc:
                self.skipTest(f"hard links are unavailable: {exc}")
            with self.assertRaisesRegex(RuntimeError, "hard-linked"):
                discover_candidates(alias, fixture["legacy_root"])

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            legacy_root = root / "legacy"
            legacy_root.mkdir()
            corrupt_db = root / "corrupt.sqlite3"
            corrupt_db.write_bytes(b"not a sqlite database")
            with self.assertRaisesRegex(RuntimeError, "valid, intact SQLite"):
                discover_candidates(corrupt_db, legacy_root)

    def test_duplicate_legacy_base_uses_source_job_delivery_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            old_job_id = int(fixture["job_id"])
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'ERROR' WHERE id = ?",
                    (old_job_id,),
                )
                cursor = conn.execute(
                    """
                    INSERT INTO jobs(
                        status,
                        created_at,
                        updated_at,
                        orig_inbox_path,
                        orig_name,
                        canonical_base,
                        canonical_audio_path,
                        transcript_txt_path,
                        transcript_json_path,
                        sha256,
                        started_at,
                        ended_at,
                        preprocess_sec,
                        transcribe_sec,
                        total_sec,
                        engine_params,
                        is_deduped,
                        current_step,
                        progress_pct
                    )
                    SELECT
                        'DONE',
                        created_at,
                        updated_at,
                        orig_inbox_path,
                        orig_name,
                        canonical_base,
                        canonical_audio_path,
                        transcript_txt_path,
                        transcript_json_path,
                        sha256,
                        started_at,
                        ended_at,
                        preprocess_sec,
                        transcribe_sec,
                        total_sec,
                        engine_params,
                        is_deduped,
                        current_step,
                        progress_pct
                    FROM jobs
                    WHERE id = ?
                    """,
                    (old_job_id,),
                )
                owner_job_id = int(cursor.lastrowid)
                conn.execute(
                    "UPDATE deliveries SET source_job_id = ? WHERE logical_stem = ?",
                    (owner_job_id, fixture["canonical_base"]),
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            plans = {
                plan.candidate.legacy_job_id: plan
                for plan in discover_candidates(
                    fixture["legacy_db"],
                    fixture["legacy_root"],
                )
            }
            old_plan = plans[old_job_id]
            owner_plan = plans[owner_job_id]
            self.assertTrue(old_plan.can_apply)
            self.assertTrue(owner_plan.can_apply)
            self.assertIsNone(old_plan.candidate.legacy_delivery_key)
            self.assertEqual(
                owner_plan.candidate.legacy_delivery_key,
                fixture["canonical_base"],
            )
            self.assertEqual(
                {artifact.kind for artifact in old_plan.candidate.artifacts},
                {"source_copy"},
            )
            self.assertIn(
                "shared_artifacts_omitted",
                {issue.code for issue in old_plan.candidate.issues},
            )
            self.assertIn(
                "summary_markdown",
                {artifact.kind for artifact in owner_plan.candidate.artifacts},
            )

            target_db = root / "v2.sqlite3"
            records_root = root / "records"
            self.assertEqual(
                import_candidate(
                    old_plan,
                    target_db_path=target_db,
                    records_root=records_root,
                ).action,
                "imported",
            )
            self.assertEqual(
                import_candidate(
                    owner_plan,
                    target_db_path=target_db,
                    records_root=records_root,
                ).action,
                "imported",
            )
            with sqlite3.connect(target_db) as conn:
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM legacy_import_map
                        WHERE legacy_kind = 'delivery'
                        """
                    ).fetchone()[0],
                    1,
                )

    def test_duplicate_base_nonowner_keeps_distinct_transcripts_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            distinct_txt = (
                Path(fixture["legacy_root"])
                / "02_transcripts"
                / "distinct.txt"
            )
            distinct_json = distinct_txt.with_suffix(".json")
            distinct_txt.write_text("distinct transcript", encoding="utf-8")
            distinct_json.write_text(
                json.dumps({"segments": [{"text": "distinct"}]}),
                encoding="utf-8",
            )
            owner_correction_txt = (
                Path(fixture["legacy_root"])
                / "03_correction"
                / "owner-specific.txt"
            )
            owner_correction_json = owner_correction_txt.with_suffix(".json")
            owner_summary = (
                Path(fixture["legacy_root"])
                / "04_summarize"
                / "owner-specific.md"
            )
            owner_correction_txt.write_text("owner correction", encoding="utf-8")
            owner_correction_json.write_text('{"owner":true}', encoding="utf-8")
            owner_summary.write_text("# owner summary", encoding="utf-8")
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                conn.execute(
                    """
                    UPDATE deliveries
                    SET correction_txt_path = ?,
                        correction_json_path = ?,
                        summary_md_path = ?,
                        correction_txt_sha256 = ?,
                        correction_json_sha256 = ?,
                        summary_md_sha256 = ?
                    """,
                    (
                        str(owner_correction_txt),
                        str(owner_correction_json),
                        str(owner_summary),
                        hashlib.sha256(
                            owner_correction_txt.read_bytes()
                        ).hexdigest(),
                        hashlib.sha256(
                            owner_correction_json.read_bytes()
                        ).hexdigest(),
                        hashlib.sha256(
                            owner_summary.read_bytes()
                        ).hexdigest(),
                    ),
                )
                nonowner_id = self._clone_job(
                    conn,
                    source_job_id=int(fixture["job_id"]),
                    canonical_base=str(fixture["canonical_base"]),
                    transcript_txt_path=distinct_txt,
                    transcript_json_path=distinct_json,
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            plans = {
                plan.candidate.legacy_job_id: plan
                for plan in discover_candidates(
                    fixture["legacy_db"],
                    fixture["legacy_root"],
                )
            }
            nonowner = plans[nonowner_id]
            kinds = {
                artifact.kind for artifact in nonowner.candidate.artifacts
            }

            self.assertTrue(nonowner.can_apply)
            self.assertIn("transcript_raw_text", kinds)
            self.assertIn("transcript_segments_json", kinds)
            self.assertNotIn("correction_text", kinds)
            self.assertNotIn("summary_markdown", kinds)
            self.assertIn(
                "shared_artifacts_omitted",
                {issue.code for issue in nonowner.candidate.issues},
            )
            self.assertEqual(
                nonowner.candidate.job_key.rsplit("_", 1)[-1],
                nonowner.candidate.source_fingerprint[:12],
            )
            for artifact in nonowner.candidate.artifacts:
                if artifact.target_relpath.startswith("source/"):
                    continue
                self.assertTrue(
                    artifact.target_relpath.startswith(
                        f"jobs/{nonowner.candidate.job_key}/"
                    )
                )

    def test_cross_base_path_and_inode_sharing_block_even_after_filtering(self) -> None:
        for share_mode in ("same_path", "hard_link"):
            with self.subTest(share_mode=share_mode), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                fixture = self._fixture(root)
                transcript_txt = Path(fixture["transcript_txt"])
                transcript_json = Path(fixture["transcript_json"])
                if share_mode == "hard_link":
                    linked_txt = transcript_txt.with_name("hard-linked.txt")
                    linked_json = transcript_json.with_name("hard-linked.json")
                    try:
                        os.link(transcript_txt, linked_txt)
                        os.link(transcript_json, linked_json)
                    except OSError as exc:
                        self.skipTest(f"hard links are unavailable: {exc}")
                    transcript_txt = linked_txt
                    transcript_json = linked_json

                with sqlite3.connect(fixture["legacy_db"]) as conn:
                    second_job_id = self._clone_job(
                        conn,
                        source_job_id=int(fixture["job_id"]),
                        canonical_base="260724OS_1",
                        transcript_txt_path=transcript_txt,
                        transcript_json_path=transcript_json,
                    )
                    conn.commit()
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

                selected = discover_candidates(
                    fixture["legacy_db"],
                    fixture["legacy_root"],
                    job_ids=[second_job_id],
                    limit=1,
                )

                self.assertEqual(len(selected), 1)
                self.assertFalse(selected[0].can_apply)
                self.assertIn(
                    "cross_base_artifact_share",
                    {
                        issue.code
                        for issue in selected[0].candidate.issues
                    },
                )

    def test_delivery_lineage_dangling_and_mismatch_fail_closed(self) -> None:
        cases = ("dangling", "mismatch")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                fixture = self._fixture(root)
                with sqlite3.connect(fixture["legacy_db"]) as conn:
                    if case == "dangling":
                        conn.execute(
                            "UPDATE deliveries SET source_job_id = 999999"
                        )
                    else:
                        conn.execute(
                            "UPDATE jobs SET canonical_base = '260724OS_1' "
                            "WHERE id = ?",
                            (int(fixture["job_id"]),),
                        )
                    conn.commit()
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

                with self.assertRaisesRegex(
                    RuntimeError,
                    "dangling|does not match",
                ):
                    discover_candidates(
                        fixture["legacy_db"],
                        fixture["legacy_root"],
                    )

    def test_ownerless_duplicate_delivery_lineage_blocks_all_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            second_txt = (
                Path(fixture["legacy_root"])
                / "02_transcripts"
                / "ownerless-second.txt"
            )
            second_json = second_txt.with_suffix(".json")
            second_txt.write_text("second", encoding="utf-8")
            second_json.write_text('{"segments":[]}', encoding="utf-8")
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                self._clone_job(
                    conn,
                    source_job_id=int(fixture["job_id"]),
                    canonical_base=str(fixture["canonical_base"]),
                    transcript_txt_path=second_txt,
                    transcript_json_path=second_json,
                )
                conn.execute("UPDATE deliveries SET source_job_id = NULL")
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            plans = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )

            self.assertEqual(len(plans), 2)
            self.assertTrue(all(not plan.can_apply for plan in plans))
            for plan in plans:
                self.assertIn(
                    "ambiguous_delivery_lineage",
                    {issue.code for issue in plan.candidate.issues},
                )
                self.assertIn(
                    "transcript_raw_text",
                    {
                        artifact.kind
                        for artifact in plan.candidate.artifacts
                    },
                )

    def test_unmatched_ownerless_delivery_with_existing_artifact_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            historical_correction = (
                Path(fixture["legacy_root"])
                / "03_correction"
                / "manual-historical-name.txt"
            )
            historical_correction.write_text(
                "orphaned correction",
                encoding="utf-8",
            )
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                conn.execute(
                    """
                    UPDATE deliveries
                    SET logical_stem = 'historical-ledger-only',
                        source_job_id = NULL,
                        correction_txt_path = ?,
                        correction_json_path = ?,
                        summary_md_path = ?
                    """,
                    (
                        str(historical_correction),
                        str(
                            Path(fixture["legacy_root"])
                            / "03_correction"
                            / "missing-historical.json"
                        ),
                        str(
                            Path(fixture["legacy_root"])
                            / "04_summarize"
                            / "missing-historical.md"
                        ),
                    ),
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            with self.assertRaisesRegex(
                RuntimeError,
                "Unmatched ownerless delivery.*existing artifact",
            ):
                discover_candidates(
                    fixture["legacy_db"],
                    fixture["legacy_root"],
                )

    def test_unmatched_ownerless_delivery_with_only_missing_artifacts_is_ignored(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                conn.execute(
                    """
                    UPDATE deliveries
                    SET logical_stem = 'historical-ledger-only',
                        source_job_id = NULL,
                        correction_txt_path = ?,
                        correction_json_path = ?,
                        summary_md_path = ?
                    """,
                    (
                        str(
                            Path(fixture["legacy_root"])
                            / "03_correction"
                            / "missing-historical.txt"
                        ),
                        str(
                            Path(fixture["legacy_root"])
                            / "03_correction"
                            / "missing-historical.json"
                        ),
                        str(
                            Path(fixture["legacy_root"])
                            / "04_summarize"
                            / "missing-historical.md"
                        ),
                    ),
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            plans = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )

            self.assertEqual(len(plans), 1)
            self.assertTrue(plans[0].can_apply)
            self.assertIsNone(plans[0].candidate.legacy_delivery_key)

    def test_existing_fallback_artifact_is_preserved_despite_error_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                conn.execute(
                    """
                    UPDATE deliveries
                    SET summary_status = 'ERROR', summary_md_path = NULL
                    WHERE logical_stem = ?
                    """,
                    (fixture["canonical_base"],),
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]

            self.assertTrue(plan.can_apply)
            self.assertIn(
                "summary_markdown",
                {artifact.kind for artifact in plan.candidate.artifacts},
            )

    def test_non_success_delivery_stale_hash_is_preserved_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                conn.execute(
                    """
                    UPDATE deliveries
                    SET summary_status = 'BLOCKED',
                        summary_md_sha256 = ?
                    WHERE logical_stem = ?
                    """,
                    ("0" * 64, fixture["canonical_base"]),
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]

            self.assertTrue(plan.can_apply)
            self.assertEqual(plan.candidate.v2_status, "needs_review")
            self.assertIn(
                "summary_hash_stale_non_success",
                {issue.code for issue in plan.candidate.issues},
            )
            summary_artifact = next(
                artifact
                for artifact in plan.candidate.artifacts
                if artifact.kind == "summary_markdown"
            )
            self.assertEqual(
                summary_artifact.sha256,
                hashlib.sha256(
                    Path(fixture["summary_md"]).read_bytes()
                ).hexdigest(),
            )

            target_db = root / "v2.sqlite3"
            import_candidate(
                plan,
                target_db_path=target_db,
                records_root=root / "records",
            )
            with sqlite3.connect(target_db) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT status FROM transcription_jobs"
                    ).fetchone()[0],
                    "needs_review",
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM review_items "
                        "WHERE reason_code = 'legacy_import_review'"
                    ).fetchone()[0],
                    1,
                )

    def test_non_success_delivery_malformed_hash_remains_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                conn.execute(
                    """
                    UPDATE deliveries
                    SET summary_status = 'BLOCKED',
                        summary_md_sha256 = 'not-a-digest'
                    """
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]

            self.assertFalse(plan.can_apply)
            self.assertIn(
                "summary_hash_invalid",
                {issue.code for issue in plan.candidate.issues},
            )

    def test_delivery_artifact_hash_mismatch_and_invalid_digest_are_blocking(self) -> None:
        cases = (
            (
                "correction_txt_sha256",
                "correction_txt",
                "correction_txt_hash_mismatch",
            ),
            (
                "correction_json_sha256",
                "correction_json",
                "correction_json_hash_mismatch",
            ),
            (
                "summary_md_sha256",
                "summary_md",
                "summary_hash_mismatch",
            ),
        )
        for _column, fixture_key, expected_code in cases:
            with self.subTest(expected_code=expected_code), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                fixture = self._fixture(root)
                artifact_path = Path(fixture[fixture_key])
                original = artifact_path.read_bytes()
                artifact_path.write_bytes(
                    bytes([original[0] ^ 1]) + original[1:]
                )

                plan = discover_candidates(
                    fixture["legacy_db"],
                    fixture["legacy_root"],
                )[0]

                self.assertFalse(plan.can_apply)
                self.assertIn(
                    expected_code,
                    {issue.code for issue in plan.candidate.issues},
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                conn.execute(
                    "UPDATE deliveries SET summary_md_sha256 = 'not-a-digest'"
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]

            self.assertFalse(plan.can_apply)
            self.assertIn(
                "summary_hash_invalid",
                {issue.code for issue in plan.candidate.issues},
            )

    def test_missing_artifact_reappearance_requires_plan_rediscovery(self) -> None:
        for artifact_kind in ("quality", "source"):
            with self.subTest(artifact_kind=artifact_kind), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                fixture = self._fixture(root)
                path = Path(
                    fixture[
                        "quality_json"
                        if artifact_kind == "quality"
                        else "audio_path"
                    ]
                )
                original = path.read_bytes()
                path.unlink()
                plan = discover_candidates(
                    fixture["legacy_db"],
                    fixture["legacy_root"],
                )[0]
                self.assertTrue(plan.can_apply)
                self.assertTrue(plan.candidate.absent_artifacts)
                path.write_bytes(original)

                with self.assertRaisesRegex(RuntimeError, "reappeared"):
                    import_candidate(
                        plan,
                        target_db_path=root / "v2.sqlite3",
                        records_root=root / "records",
                    )
                self.assertFalse((root / "records").exists())

    def test_plan_digest_binds_legacy_database_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            first = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            first_digest = compute_plan_digest([first])

            legacy_db = Path(fixture["legacy_db"])
            current_stat = legacy_db.stat()
            os.utime(
                legacy_db,
                ns=(
                    current_stat.st_atime_ns,
                    current_stat.st_mtime_ns + 1_000_000,
                ),
            )
            second = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]

            self.assertEqual(
                first.candidate.source_fingerprint,
                second.candidate.source_fingerprint,
            )
            self.assertNotEqual(
                first.legacy_database_snapshot,
                second.legacy_database_snapshot,
            )
            self.assertNotEqual(
                first_digest,
                compute_plan_digest([second]),
            )

    def test_legacy_database_change_after_discovery_blocks_before_target_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'ERROR' WHERE id = ?",
                    (int(fixture["job_id"]),),
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            target_db = root / "v2.sqlite3"
            records_root = root / "records"
            with self.assertRaisesRegex(
                RuntimeError,
                "Legacy DB or its SQLite sidecars changed",
            ):
                import_candidate(
                    plan,
                    target_db_path=target_db,
                    records_root=records_root,
                )

            self.assertFalse(target_db.exists())
            self.assertFalse(records_root.exists())

    def test_missing_source_reappearance_during_insert_rolls_back_cleanly(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            audio_path = Path(fixture["audio_path"])
            audio_bytes = audio_path.read_bytes()
            audio_path.unlink()
            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            old_storage_key = plan.candidate.storage_key
            target_db = root / "v2.sqlite3"
            records_root = root / "records"
            real_insert = storage_importer._insert_candidate

            def insert_then_reappear(
                conn: sqlite3.Connection,
                candidate: object,
                *,
                manifest: object,
            ) -> tuple[int, int]:
                result = real_insert(
                    conn,
                    candidate,
                    manifest=manifest,
                )
                audio_path.write_bytes(audio_bytes)
                return result

            with patch.object(
                storage_importer,
                "_insert_candidate",
                side_effect=insert_then_reappear,
            ):
                with self.assertRaisesRegex(RuntimeError, "reappeared"):
                    import_candidate(
                        plan,
                        target_db_path=target_db,
                        records_root=records_root,
                    )

            with sqlite3.connect(target_db) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0],
                    0,
                )
            self.assertFalse((records_root / old_storage_key).exists())
            self.assertEqual(list((records_root / ".staging").iterdir()), [])
            self.assertEqual(list((records_root / ".locks").iterdir()), [])

            refreshed = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            self.assertNotEqual(
                refreshed.candidate.source_fingerprint,
                plan.candidate.source_fingerprint,
            )
            result = import_candidate(
                refreshed,
                target_db_path=target_db,
                records_root=records_root,
            )
            self.assertEqual(result.action, "imported")

    def test_present_artifact_inode_replacement_requires_rediscovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            transcript_path = Path(fixture["transcript_txt"])
            original_inode = transcript_path.stat().st_ino
            replacement = transcript_path.with_name("replacement.tmp")
            replacement.write_bytes(transcript_path.read_bytes())
            os.replace(replacement, transcript_path)
            if transcript_path.stat().st_ino == original_inode:
                self.skipTest("filesystem immediately reused the original inode")

            with self.assertRaisesRegex(RuntimeError, "changed after discovery"):
                import_candidate(
                    plan,
                    target_db_path=root / "v2.sqlite3",
                    records_root=root / "records",
                )
            self.assertFalse((root / "records").exists())

    def test_same_job_artifact_role_collision_is_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            correction_path = Path(fixture["correction_txt"])
            correction_sha = hashlib.sha256(
                correction_path.read_bytes()
            ).hexdigest()
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                conn.execute(
                    """
                    UPDATE deliveries
                    SET summary_md_path = ?,
                        summary_md_sha256 = ?
                    """,
                    (str(correction_path), correction_sha),
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]

            self.assertFalse(plan.can_apply)
            self.assertIn(
                "ambiguous_artifact_role",
                {issue.code for issue in plan.candidate.issues},
            )

    def test_source_and_transcript_path_collision_is_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                conn.execute(
                    "UPDATE jobs SET transcript_txt_path = ? WHERE id = ?",
                    (
                        str(fixture["audio_path"]),
                        int(fixture["job_id"]),
                    ),
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]

            self.assertFalse(plan.can_apply)
            self.assertIn(
                "ambiguous_artifact_role",
                {issue.code for issue in plan.candidate.issues},
            )

    def test_source_share_across_canonical_bases_is_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            second_txt = (
                Path(fixture["legacy_root"])
                / "02_transcripts"
                / "cross-base-source.txt"
            )
            second_json = second_txt.with_suffix(".json")
            second_txt.write_text("second transcript", encoding="utf-8")
            second_json.write_text('{"segments":[]}', encoding="utf-8")
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                self._clone_job(
                    conn,
                    source_job_id=int(fixture["job_id"]),
                    canonical_base="260724OS_1",
                    transcript_txt_path=second_txt,
                    transcript_json_path=second_json,
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            plans = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )

            self.assertEqual(len(plans), 2)
            self.assertTrue(all(not plan.can_apply for plan in plans))
            for plan in plans:
                self.assertIn(
                    "cross_base_artifact_share",
                    {issue.code for issue in plan.candidate.issues},
                )

    def test_cross_job_different_artifact_roles_are_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            transcript_txt = Path(fixture["transcript_txt"])
            second_txt = transcript_txt.with_name("role-second.txt")
            second_json = transcript_txt.with_name("role-second.json")
            second_txt.write_text("second transcript", encoding="utf-8")
            second_json.write_text('{"segments":[]}', encoding="utf-8")
            transcript_sha = hashlib.sha256(
                transcript_txt.read_bytes()
            ).hexdigest()
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                second_job_id = self._clone_job(
                    conn,
                    source_job_id=int(fixture["job_id"]),
                    canonical_base="260724OS_1",
                    transcript_txt_path=second_txt,
                    transcript_json_path=second_json,
                )
                conn.execute(
                    """
                    INSERT INTO deliveries(
                        logical_stem,
                        source_job_id,
                        subject_abbr,
                        correction_txt_path,
                        correction_txt_sha256,
                        correction_status,
                        summary_status,
                        updated_at
                    )
                    VALUES (
                        '260724OS_1',
                        ?,
                        'OS',
                        ?,
                        ?,
                        'DELIVERED',
                        'MISSING',
                        '2026-07-24T10:00:00+09:00'
                    )
                    """,
                    (
                        second_job_id,
                        str(transcript_txt),
                        transcript_sha,
                    ),
                )
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            plans = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )

            self.assertEqual(len(plans), 2)
            self.assertTrue(all(not plan.can_apply for plan in plans))
            for plan in plans:
                self.assertIn(
                    "ambiguous_artifact_role",
                    {issue.code for issue in plan.candidate.issues},
                )

    def test_target_paths_must_be_outside_legacy_and_not_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]

            with self.assertRaisesRegex(ValueError, "separate from the legacy root"):
                import_candidate(
                    plan,
                    target_db_path=root / "v2.sqlite3",
                    records_root=Path(fixture["legacy_root"]) / "records-v2",
                )
            with self.assertRaisesRegex(ValueError, "separate from the legacy root"):
                import_candidate(
                    plan,
                    target_db_path=root / "v2.sqlite3",
                    records_root=root,
                )
            with self.assertRaisesRegex(ValueError, "outside the legacy root"):
                import_candidate(
                    plan,
                    target_db_path=Path(fixture["legacy_root"]) / "v2.sqlite3",
                    records_root=root / "records",
                )
            with self.assertRaisesRegex(ValueError, "outside the records root"):
                import_candidate(
                    plan,
                    target_db_path=root / "records" / "v2.sqlite3",
                    records_root=root / "records",
                )

            real_db = root / "real-v2.sqlite3"
            sqlite3.connect(real_db).close()
            linked_db = root / "linked-v2.sqlite3"
            real_records = root / "real-records"
            real_records.mkdir()
            linked_records = root / "linked-records"
            try:
                os.symlink(real_db, linked_db)
                os.symlink(real_records, linked_records)
            except (OSError, NotImplementedError):
                return
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                import_candidate(
                    plan,
                    target_db_path=linked_db,
                    records_root=root / "records",
                )
                with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                    import_candidate(
                        plan,
                        target_db_path=root / "v2.sqlite3",
                        records_root=linked_records,
                    )

    def test_copy_deadlock_retry_is_bounded_and_root_lock_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            artifact = next(
                item
                for item in plan.candidate.artifacts
                if item.source_path is not None
            )
            destination = root / "copy-target" / "artifact.bin"
            deadlock = OSError(errno.EDEADLK, "simulated deadlock")
            with (
                patch.object(
                    storage_importer.os,
                    "read",
                    side_effect=deadlock,
                ) as mocked_read,
                patch.object(storage_importer.time, "sleep") as mocked_sleep,
            ):
                with self.assertRaisesRegex(RuntimeError, "bounded retries"):
                    storage_importer._copy_no_overwrite(
                        destination,
                        expected=artifact,
                    )
            self.assertEqual(mocked_read.call_count, 5)
            self.assertEqual(mocked_sleep.call_count, 4)

            records_root = root / "lock-root"
            records_root.mkdir()
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            competing_fd = os.open(records_root, flags)
            try:
                with storage_importer._exclusive_records_root_lock(records_root):
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(
                            competing_fd,
                            fcntl.LOCK_SH | fcntl.LOCK_NB,
                        )
            finally:
                os.close(competing_fd)

    def test_hard_linked_v2_target_cannot_mutate_legacy_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            legacy_db = Path(fixture["legacy_db"])
            with sqlite3.connect(legacy_db) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                journal_mode = conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
            self.assertEqual(str(journal_mode).lower(), "delete")

            plan = discover_candidates(
                legacy_db,
                fixture["legacy_root"],
            )[0]
            hard_link = root / "hard-linked-v2.sqlite3"
            try:
                os.link(legacy_db, hard_link)
            except OSError as exc:
                self.skipTest(f"hard links are unavailable: {exc}")
            legacy_sha_before = hashlib.sha256(legacy_db.read_bytes()).hexdigest()

            with self.assertRaisesRegex(
                (ValueError, RuntimeError),
                "hard-linked",
            ):
                import_candidate(
                    plan,
                    target_db_path=hard_link,
                    records_root=root / "records",
                )

            self.assertEqual(
                hashlib.sha256(legacy_db.read_bytes()).hexdigest(),
                legacy_sha_before,
            )
            with sqlite3.connect(legacy_db) as conn:
                self.assertEqual(
                    str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                    "delete",
                )
            self.assertFalse((root / "records").exists())

    def test_promoted_record_without_db_commit_is_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            target_db = root / "v2.sqlite3"
            records_root = root / "records"
            first = import_candidate(
                plan,
                target_db_path=target_db,
                records_root=records_root,
            )
            self.assertEqual(first.action, "imported")

            # Simulate the only cross-resource crash window: the promoted,
            # fully verified record exists but the DB transaction did not.
            target_db.unlink()
            target_db.with_name(f"{target_db.name}-wal").unlink(missing_ok=True)
            target_db.with_name(f"{target_db.name}-shm").unlink(missing_ok=True)

            resolved_records_root = records_root.resolve()
            record_root = resolved_records_root / plan.candidate.storage_key
            with (
                patch.object(
                    storage_importer,
                    "_fsync_tree_directories",
                    wraps=storage_importer._fsync_tree_directories,
                ) as fsync_tree,
                patch.object(
                    storage_importer,
                    "_fsync_directory",
                    wraps=storage_importer._fsync_directory,
                ) as fsync_directory,
            ):
                recovered = import_candidate(
                    plan,
                    target_db_path=target_db,
                    records_root=records_root,
                )
            fsync_tree.assert_called_once_with(record_root)
            fsync_directory.assert_any_call(resolved_records_root / ".staging")
            fsync_directory.assert_any_call(resolved_records_root)
            self.assertEqual(recovered.action, "recovered")
            snapshot = read_library_snapshot(target_db)
            self.assertEqual(snapshot["counts"]["recordings"], 1)
            self.assertEqual(snapshot["counts"]["done"], 1)

    def test_existing_record_rejects_valid_but_tampered_manifest_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            target_db = root / "v2.sqlite3"
            records_root = root / "records"
            result = import_candidate(
                plan,
                target_db_path=target_db,
                records_root=records_root,
            )
            manifest_path = records_root / result.storage_key / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"][0]["sha256"] = "f" * 64
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "metadata mismatch"):
                import_candidate(
                    plan,
                    target_db_path=target_db,
                    records_root=records_root,
                )

    def test_idempotent_skip_requires_complete_database_provenance(self) -> None:
        for damage in ("job", "job_map", "delivery_map"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                fixture = self._fixture(root)
                plan = discover_candidates(
                    fixture["legacy_db"],
                    fixture["legacy_root"],
                )[0]
                target_db = root / "v2.sqlite3"
                records_root = root / "records"
                import_candidate(
                    plan,
                    target_db_path=target_db,
                    records_root=records_root,
                )
                with sqlite3.connect(target_db) as conn:
                    conn.execute("PRAGMA foreign_keys = ON")
                    if damage == "job":
                        conn.execute("DELETE FROM transcription_jobs")
                    elif damage == "job_map":
                        conn.execute(
                            "DELETE FROM legacy_import_map "
                            "WHERE legacy_kind = 'job'"
                        )
                    else:
                        conn.execute(
                            "DELETE FROM legacy_import_map "
                            "WHERE legacy_kind = 'delivery'"
                        )
                    conn.commit()

                expected = (
                    "current job count"
                    if damage == "job"
                    else "import-map provenance"
                )
                with self.assertRaisesRegex(RuntimeError, expected):
                    import_candidate(
                        plan,
                        target_db_path=target_db,
                        records_root=records_root,
                    )

    def test_idempotent_skip_requires_exact_deterministic_timestamps_and_logs(
        self,
    ) -> None:
        cases = (
            ("recordings", "created_at", "2099-01-01T00:00:00+00:00", "recording"),
            (
                "transcription_jobs",
                "queued_at",
                "2099-01-01T00:00:00+00:00",
                "current job",
            ),
            (
                "transcription_jobs",
                "started_at",
                "2099-01-01T00:00:00+00:00",
                "current job",
            ),
            (
                "transcription_jobs",
                "finished_at",
                "2099-01-01T00:00:00+00:00",
                "current job",
            ),
            (
                "engine_runs",
                "started_at",
                "2099-01-01T00:00:00+00:00",
                "selected engine run",
            ),
            (
                "engine_runs",
                "finished_at",
                "2099-01-01T00:00:00+00:00",
                "selected engine run",
            ),
            (
                "engine_runs",
                "stderr_relpath",
                "logs/changed.stderr",
                "selected engine run",
            ),
            (
                "engine_runs",
                "log_relpath",
                "logs/changed.log",
                "selected engine run",
            ),
        )
        for table, field, value, expected in cases:
            with (
                self.subTest(table=table, field=field),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                root = Path(tmpdir)
                fixture = self._fixture(root)
                plan = discover_candidates(
                    fixture["legacy_db"],
                    fixture["legacy_root"],
                )[0]
                target_db = root / "v2.sqlite3"
                records_root = root / "records"
                import_candidate(
                    plan,
                    target_db_path=target_db,
                    records_root=records_root,
                )
                with sqlite3.connect(target_db) as conn:
                    conn.execute(
                        f"UPDATE {table} SET {field} = ?",
                        (value,),
                    )
                    conn.commit()

                with self.assertRaisesRegex(
                    RuntimeError,
                    f"{expected} metadata mismatch",
                ):
                    import_candidate(
                        plan,
                        target_db_path=target_db,
                        records_root=records_root,
                    )

    def test_skip_and_recovery_reject_hardlinked_record_files(self) -> None:
        cases = (
            ("skip", "artifact"),
            ("recovery", "manifest"),
        )
        for mode, target_kind in cases:
            with (
                self.subTest(mode=mode, target_kind=target_kind),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                root = Path(tmpdir)
                fixture = self._fixture(root)
                plan = discover_candidates(
                    fixture["legacy_db"],
                    fixture["legacy_root"],
                )[0]
                target_db = root / "v2.sqlite3"
                records_root = root / "records"
                result = import_candidate(
                    plan,
                    target_db_path=target_db,
                    records_root=records_root,
                )
                record_root = records_root / result.storage_key
                if target_kind == "manifest":
                    target = record_root / "manifest.json"
                else:
                    target = record_root / next(
                        artifact.target_relpath
                        for artifact in plan.candidate.artifacts
                        if artifact.kind == "transcript_raw_text"
                    )
                alias = root / f"{mode}-{target_kind}-alias"
                try:
                    os.link(target, alias)
                except OSError as exc:
                    self.skipTest(f"hard links are unavailable: {exc}")
                if mode == "recovery":
                    target_db.unlink()
                    target_db.with_name(
                        f"{target_db.name}-wal"
                    ).unlink(missing_ok=True)
                    target_db.with_name(
                        f"{target_db.name}-shm"
                    ).unlink(missing_ok=True)

                with self.assertRaisesRegex(RuntimeError, "hard-linked"):
                    import_candidate(
                        plan,
                        target_db_path=target_db,
                        records_root=records_root,
                    )

    def test_one_database_cannot_split_across_records_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            target_db = root / "v2.sqlite3"
            first_root = root / "records-a"
            second_root = root / "records-b"
            import_candidate(
                plan,
                target_db_path=target_db,
                records_root=first_root,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "bound to a different",
            ):
                import_candidate(
                    plan,
                    target_db_path=target_db,
                    records_root=second_root,
                )

            self.assertTrue(
                (first_root / plan.candidate.storage_key / "manifest.json").is_file()
            )
            self.assertFalse(
                (second_root / plan.candidate.storage_key).exists()
            )

    def test_import_preflight_rejects_unbound_root_and_stale_internal_entries(self) -> None:
        cases = ("orphan", "staging", "lock")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                fixture = self._fixture(root)
                plan = discover_candidates(
                    fixture["legacy_db"],
                    fixture["legacy_root"],
                )[0]
                records_root = root / "records"
                records_root.mkdir()
                if case == "orphan":
                    (records_root / "unrelated-record").mkdir()
                    expected = "Unexpected records root entry"
                elif case == "staging":
                    staging = records_root / ".staging"
                    staging.mkdir()
                    (staging / "stale").mkdir()
                    expected = "staging directory is not empty"
                else:
                    locks = records_root / ".locks"
                    locks.mkdir()
                    (locks / "unrelated.lock").touch()
                    expected = "unsafe storage v2 lock"

                with self.assertRaisesRegex(RuntimeError, expected):
                    import_candidate(
                        plan,
                        target_db_path=root / "v2.sqlite3",
                        records_root=records_root,
                    )
                self.assertFalse(
                    (records_root / plan.candidate.storage_key).exists()
                )

    def test_idempotent_skip_rejects_unknown_tree_and_manifest_payload(self) -> None:
        for damage in ("unindexed_file", "manifest_extra"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                fixture = self._fixture(root)
                plan = discover_candidates(
                    fixture["legacy_db"],
                    fixture["legacy_root"],
                )[0]
                target_db = root / "v2.sqlite3"
                records_root = root / "records"
                result = import_candidate(
                    plan,
                    target_db_path=target_db,
                    records_root=records_root,
                )
                record_root = records_root / result.storage_key
                if damage == "unindexed_file":
                    (record_root / "utterances.txt").write_text(
                        "private extra payload",
                        encoding="utf-8",
                    )
                    expected = "unindexed files"
                else:
                    manifest_path = record_root / "manifest.json"
                    manifest = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                    manifest["utterances"] = ["private extra payload"]
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
                    expected = "manifest"

                with self.assertRaisesRegex(RuntimeError, expected):
                    import_candidate(
                        plan,
                        target_db_path=target_db,
                        records_root=records_root,
                    )

    def test_idempotent_skip_requires_legacy_review_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            Path(fixture["audio_path"]).unlink()
            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            target_db = root / "v2.sqlite3"
            records_root = root / "records"
            import_candidate(
                plan,
                target_db_path=target_db,
                records_root=records_root,
            )
            with sqlite3.connect(target_db) as conn:
                conn.execute("DELETE FROM review_items")
                conn.commit()

            with self.assertRaisesRegex(
                RuntimeError,
                "legacy import review count",
            ):
                import_candidate(
                    plan,
                    target_db_path=target_db,
                    records_root=records_root,
                )

    def test_manifest_rejects_content_fields_and_parent_paths(self) -> None:
        base = {
            "storage_key": "rec_test",
            "generated_at": "2026-07-23T00:00:00Z",
            "original_name_raw": "테스트.m4a",
            "original_name_nfc": "테스트.m4a",
            "source": {
                "path": "source/original.m4a",
                "availability": "available",
                "sha256": "2" * 64,
                "bytes": 1,
            },
            "title": {"value": "테스트"},
            "context": {"type": "general"},
            "jobs": [
                {
                    "job_key": "job_test",
                    "artifact_paths": ["jobs/job_test/transcript.txt"],
                }
            ],
            "legacy": {"source_fingerprint": "0" * 64},
        }
        with self.assertRaises(ManifestValidationError):
            build_manifest(
                **base,
                artifacts=[
                    {
                        "kind": "source_copy",
                        "path": "source/original.m4a",
                        "sha256": "2" * 64,
                        "bytes": 1,
                    },
                    {
                        "kind": "transcript_raw_text",
                        "path": "jobs/job_test/transcript.txt",
                        "sha256": "1" * 64,
                        "bytes": 1,
                        "content": "must not leak",
                    }
                ],
            )
        recursive_base = dict(base)
        recursive_base["context"] = {"type": "general", "segments": []}
        with self.assertRaises(ManifestValidationError):
            build_manifest(
                **recursive_base,
                artifacts=[
                    {
                        "kind": "source_copy",
                        "path": "source/original.m4a",
                        "sha256": "2" * 64,
                        "bytes": 1,
                    },
                    {
                        "kind": "transcript_raw_text",
                        "path": "jobs/job_test/transcript.txt",
                        "sha256": "1" * 64,
                        "bytes": 1,
                    },
                ],
            )
        with self.assertRaises(ManifestValidationError):
            build_manifest(
                **base,
                artifacts=[
                    {
                        "kind": "source_copy",
                        "path": "source/original.m4a",
                        "sha256": "2" * 64,
                        "bytes": 1,
                    },
                    {
                        "kind": "metadata",
                        "path": "../escape.json",
                        "sha256": "1" * 64,
                        "bytes": 1,
                    }
                ],
            )

    def test_cli_plan_is_read_only_and_apply_requires_explicit_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            target_db = root / "v2.sqlite3"
            records_root = root / "records"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = storage_cli_main(
                    [
                        "plan",
                        "--legacy-db",
                        str(fixture["legacy_db"]),
                        "--legacy-root",
                        str(fixture["legacy_root"]),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertFalse(target_db.exists())
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["mode"], "read_only")
            self.assertEqual(payload["summary"]["total"], 1)
            plan_sha256 = payload["plan_sha256"]

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = storage_cli_main(
                    [
                        "apply",
                        "--legacy-db",
                        str(fixture["legacy_db"]),
                        "--legacy-root",
                        str(fixture["legacy_root"]),
                        "--v2-db",
                        str(target_db),
                        "--records-root",
                        str(records_root),
                        "--expected-count",
                        "1",
                        "--expected-plan-sha256",
                        plan_sha256,
                    ]
                )
            self.assertEqual(exit_code, 2)
            self.assertIn("--allow-write", stderr.getvalue())
            self.assertFalse(target_db.exists())

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = storage_cli_main(
                    [
                        "apply",
                        "--legacy-db",
                        str(fixture["legacy_db"]),
                        "--legacy-root",
                        str(fixture["legacy_root"]),
                        "--v2-db",
                        str(target_db),
                        "--records-root",
                        str(records_root),
                        "--expected-count",
                        "2",
                        "--expected-plan-sha256",
                        plan_sha256,
                        "--allow-write",
                    ]
                )
            self.assertEqual(exit_code, 2)
            self.assertIn("current plan contains 1", stderr.getvalue())
            self.assertFalse(target_db.exists())

    def test_cli_requires_acknowledgement_for_missing_source_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            Path(fixture["audio_path"]).unlink()
            target_db = root / "v2.sqlite3"
            plans = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = storage_cli_main(
                    [
                        "apply",
                        "--legacy-db",
                        str(fixture["legacy_db"]),
                        "--legacy-root",
                        str(fixture["legacy_root"]),
                        "--v2-db",
                        str(target_db),
                        "--records-root",
                        str(root / "records"),
                        "--expected-count",
                        "1",
                        "--expected-plan-sha256",
                        compute_plan_digest(plans),
                        "--allow-write",
                    ]
                )
            self.assertEqual(exit_code, 2)
            self.assertIn("--allow-missing-source", stderr.getvalue())
            self.assertFalse(target_db.exists())

    def test_core_refuses_legacy_database_as_v2_target_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture = self._fixture(root)
            plan = discover_candidates(
                fixture["legacy_db"],
                fixture["legacy_root"],
            )[0]
            records_root = root / "records"
            with self.assertRaisesRegex(ValueError, "must be separate"):
                import_candidate(
                    plan,
                    target_db_path=fixture["legacy_db"],
                    records_root=records_root,
                )
            self.assertFalse(records_root.exists())
            with sqlite3.connect(fixture["legacy_db"]) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
            self.assertIn("jobs", tables)
            self.assertNotIn("recordings", tables)


if __name__ == "__main__":
    unittest.main()
