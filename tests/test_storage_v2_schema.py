from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from lecture_stt.storage_v2 import repository as storage_repository
from lecture_stt.storage_v2.repository import (
    apply_migration,
    connect_v2,
    read_library_snapshot,
    require_v2_schema,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPO_ROOT / "migrations" / "v2" / "0001_recording_store.sql"


class StorageV2SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")

    def _connect(self, db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _apply(self, conn: sqlite3.Connection) -> None:
        conn.executescript(self.migration_sql)

    def test_schema_creates_expected_objects_and_migration_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            with self._connect(db_path) as conn:
                self._apply(conn)

                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                self.assertTrue(
                    {
                        "schema_migrations",
                        "recordings",
                        "recording_titles",
                        "recording_contexts",
                        "transcription_jobs",
                        "engine_runs",
                        "job_events",
                        "artifacts",
                        "review_items",
                        "schedule_imports",
                        "schedule_entries",
                        "schedule_semester_selections",
                        "recording_classification_proposals",
                        "recording_title_proposals",
                        "outbox_events",
                        "legacy_import_map",
                    }.issubset(tables)
                )

                trigger_names = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                    ).fetchall()
                }
                self.assertIn("recordings_storage_key_immutable", trigger_names)

                migration_rows = conn.execute(
                    "SELECT version, checksum_sha256 FROM schema_migrations WHERE version = ?",
                    ("storage_v2/0001_recording_store",),
                ).fetchall()
                self.assertEqual([row["version"] for row in migration_rows], ["storage_v2/0001_recording_store"])
                self.assertEqual(migration_rows[0]["checksum_sha256"], None)

    def test_schema_apply_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            with self._connect(db_path) as conn:
                self._apply(conn)
                self._apply(conn)

                count = conn.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                    ("storage_v2/0001_recording_store",),
                ).fetchone()[0]
                self.assertEqual(count, 1)

    def test_title_proposal_schema_separates_suggestion_from_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            with self._connect(db_path) as conn:
                self._apply(conn)
                recording_id = int(
                    conn.execute(
                        """
                        INSERT INTO recordings(
                            storage_key,
                            original_name_raw,
                            original_name_nfc,
                            source_relpath,
                            source_state
                        )
                        VALUES (
                            'rec_title_schema',
                            '메모.m4a',
                            '메모.m4a',
                            'source/unavailable.json',
                            'missing'
                        )
                        """
                    ).lastrowid
                )
                job_id = int(
                    conn.execute(
                        """
                        INSERT INTO transcription_jobs(
                            recording_id,
                            job_key,
                            job_relpath,
                            status,
                            progress
                        )
                        VALUES (?, 'job_title_schema', 'jobs/job_title_schema', 'done', 100)
                        """,
                        (recording_id,),
                    ).lastrowid
                )
                artifact_id = int(
                    conn.execute(
                        """
                        INSERT INTO artifacts(
                            recording_id,
                            job_id,
                            artifact_kind,
                            path_rel,
                            content_sha256,
                            bytes
                        )
                        VALUES (
                            ?,
                            ?,
                            'transcript_raw_text',
                            'jobs/job_title_schema/transcript.txt',
                            ?,
                            4
                        )
                        """,
                        (recording_id, job_id, "a" * 64),
                    ).lastrowid
                )
                review_id = int(
                    conn.execute(
                        """
                        INSERT INTO review_items(
                            recording_id,
                            job_id,
                            artifact_id,
                            status,
                            reason_code
                        )
                        VALUES (
                            ?,
                            ?,
                            ?,
                            'open',
                            'title_suggestion_content_topic'
                        )
                        """,
                        (recording_id, job_id, artifact_id),
                    ).lastrowid
                )
                proposal_id = int(
                    conn.execute(
                        """
                        INSERT INTO recording_title_proposals(
                            recording_id,
                            transcript_artifact_id,
                            review_item_id,
                            status,
                            suggestion_reason,
                            proposed_title,
                            confidence,
                            generator_version,
                            detail_json
                        )
                        VALUES (
                            ?,
                            ?,
                            ?,
                            'suggested',
                            'content_topic',
                            '2026-07-25 회의 기록',
                            0.55,
                            'deterministic-keywords-v1',
                            '{}'
                        )
                        """,
                        (recording_id, artifact_id, review_id),
                    ).lastrowid
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO recording_title_proposals(
                            recording_id,
                            transcript_artifact_id,
                            review_item_id,
                            status,
                            suggestion_reason,
                            proposed_title,
                            confidence,
                            generator_version,
                            detail_json
                        )
                        VALUES (
                            ?,
                            ?,
                            ?,
                            'suggested',
                            'content_topic',
                            '중복 제안',
                            0.55,
                            'deterministic-keywords-v1',
                            '{}'
                        )
                        """,
                        (recording_id, artifact_id, review_id),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        UPDATE recording_title_proposals
                        SET status = 'confirmed'
                        WHERE id = ?
                        """,
                        (proposal_id,),
                    )

                conn.execute(
                    """
                    UPDATE recording_title_proposals
                    SET status = 'confirmed',
                        confirmation_plan_sha256 = ?,
                        confirmed_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    ("b" * 64, proposal_id),
                )
                row = conn.execute(
                    """
                    SELECT status, confirmation_plan_sha256, confirmed_at
                    FROM recording_title_proposals
                    WHERE id = ?
                    """,
                    (proposal_id,),
                ).fetchone()
                self.assertEqual(row["status"], "confirmed")
                self.assertEqual(row["confirmation_plan_sha256"], "b" * 64)
                self.assertIsNotNone(row["confirmed_at"])

    def test_apply_migration_stamps_and_verifies_current_sql_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            expected_checksum = hashlib.sha256(
                MIGRATION_PATH.read_bytes()
            ).hexdigest()

            conn = apply_migration(db_path)
            try:
                stored_checksum = conn.execute(
                    """
                    SELECT checksum_sha256
                    FROM schema_migrations
                    WHERE version = ?
                    """,
                    ("storage_v2/0001_recording_store",),
                ).fetchone()[0]
                self.assertEqual(stored_checksum, expected_checksum)
            finally:
                conn.close()

            conn = apply_migration(db_path)
            conn.close()

            with self._connect(db_path) as conn:
                conn.execute(
                    """
                    UPDATE schema_migrations
                    SET checksum_sha256 = ?
                    WHERE version = ?
                    """,
                    ("0" * 64, "storage_v2/0001_recording_store"),
                )
                conn.commit()

            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                apply_migration(db_path)

    def test_apply_migration_stamps_an_idempotently_raw_initialized_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            with self._connect(db_path) as conn:
                self._apply(conn)
                self._apply(conn)
                self.assertIsNone(
                    conn.execute(
                        """
                        SELECT checksum_sha256
                        FROM schema_migrations
                        WHERE version = ?
                        """,
                        ("storage_v2/0001_recording_store",),
                    ).fetchone()[0]
                )

            conn = apply_migration(db_path)
            try:
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT checksum_sha256
                        FROM schema_migrations
                        WHERE version = ?
                        """,
                        ("storage_v2/0001_recording_store",),
                    ).fetchone()[0],
                    hashlib.sha256(MIGRATION_PATH.read_bytes()).hexdigest(),
                )
            finally:
                conn.close()

    def test_apply_migration_rejects_null_checksum_after_storage_has_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            conn = apply_migration(db_path)
            try:
                conn.execute(
                    """
                    INSERT INTO recordings(
                        storage_key,
                        original_name_raw,
                        original_name_nfc,
                        source_relpath,
                        manifest_relpath
                    )
                    VALUES (
                        'rec_checksum_tamper',
                        'source.m4a',
                        'source.m4a',
                        'source/original.m4a',
                        'manifest.json'
                    )
                    """
                )
                conn.execute(
                    """
                    UPDATE schema_migrations
                    SET checksum_sha256 = NULL
                    WHERE version = ?
                    """,
                    ("storage_v2/0001_recording_store",),
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                apply_migration(db_path)

            with self._connect(db_path) as conn:
                self.assertIsNone(
                    conn.execute(
                        """
                        SELECT checksum_sha256
                        FROM schema_migrations
                        WHERE version = ?
                        """,
                        ("storage_v2/0001_recording_store",),
                    ).fetchone()[0]
                )

    def test_apply_migration_rejects_hardlinked_writable_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            original = root / "legacy.sqlite3"
            original.write_bytes(b"legacy snapshot must remain untouched")
            linked_target = root / "storage_v2.sqlite3"
            try:
                linked_target.hardlink_to(original)
            except OSError as exc:
                self.skipTest(f"hard-link creation is unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "hard-linked"):
                apply_migration(linked_target)

            self.assertEqual(
                original.read_bytes(),
                b"legacy snapshot must remain untouched",
            )

    def test_apply_migration_rejects_weak_preexisting_table_without_expanding_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            with self._connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE recordings (
                        id INTEGER PRIMARY KEY,
                        storage_key TEXT,
                        original_name_raw TEXT,
                        original_name_nfc TEXT,
                        source_relpath TEXT,
                        manifest_relpath TEXT
                    )
                    """
                )
                conn.commit()

            with self.assertRaisesRegex(RuntimeError, "canonical migration"):
                apply_migration(db_path)

            with self._connect(db_path) as conn:
                objects = {
                    (row["type"], row["name"])
                    for row in conn.execute(
                        """
                        SELECT type, name
                        FROM sqlite_master
                        WHERE type IN ('table', 'index', 'trigger')
                          AND name NOT LIKE 'sqlite_%'
                        """
                    ).fetchall()
                }
                self.assertEqual(objects, {("table", "recordings")})

    def test_unrelated_delete_mode_database_rejection_is_non_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "storage_v2.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    str(
                        conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
                    ).lower(),
                    "delete",
                )
                conn.execute("CREATE TABLE unrelated(secret TEXT)")
                conn.execute("INSERT INTO unrelated VALUES ('preserve-me')")
                conn.commit()
            finally:
                conn.close()
            before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
            sidecars = tuple(
                db_path.with_name(f"{db_path.name}{suffix}")
                for suffix in ("-wal", "-shm", "-journal")
            )
            before_sidecars = {
                path.name: (
                    path.read_bytes() if path.exists() else None
                )
                for path in sidecars
            }

            with self.assertRaisesRegex(RuntimeError, "canonical migration"):
                apply_migration(db_path)

            self.assertEqual(
                hashlib.sha256(db_path.read_bytes()).hexdigest(),
                before_hash,
            )
            self.assertEqual(
                {
                    path.name: (
                        path.read_bytes() if path.exists() else None
                    )
                    for path in sidecars
                },
                before_sidecars,
            )
            conn = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                    "delete",
                )
                self.assertEqual(
                    conn.execute("SELECT secret FROM unrelated").fetchone()[0],
                    "preserve-me",
                )
            finally:
                conn.close()

    def test_writable_connect_rejects_target_inode_swap_before_pragmas(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "storage_v2.sqlite3"
            conn = apply_migration(target)
            conn.close()

            victim = root / "victim.sqlite3"
            victim_conn = sqlite3.connect(victim)
            try:
                victim_conn.execute("PRAGMA journal_mode = DELETE")
                victim_conn.execute("CREATE TABLE private_data(value TEXT)")
                victim_conn.execute(
                    "INSERT INTO private_data VALUES ('must-survive')"
                )
                victim_conn.commit()
            finally:
                victim_conn.close()
            victim_hash = hashlib.sha256(victim.read_bytes()).hexdigest()
            real_connect = sqlite3.connect
            swapped = False

            def swap_before_writable_open(
                database: object,
                *args: object,
                **kwargs: object,
            ) -> sqlite3.Connection:
                nonlocal swapped
                if not kwargs.get("uri") and Path(database) == target:
                    target.unlink()
                    os.link(victim, target)
                    swapped = True
                return real_connect(database, *args, **kwargs)

            with (
                patch.object(
                    storage_repository.sqlite3,
                    "connect",
                    side_effect=swap_before_writable_open,
                ),
                self.assertRaisesRegex(
                    (ValueError, RuntimeError),
                    "hard-linked|binding|changed|validated writable v2 DB inode",
                ),
            ):
                apply_migration(target)

            self.assertTrue(swapped)
            self.assertEqual(
                hashlib.sha256(victim.read_bytes()).hexdigest(),
                victim_hash,
            )
            victim_conn = sqlite3.connect(victim)
            try:
                self.assertEqual(
                    str(
                        victim_conn.execute("PRAGMA journal_mode").fetchone()[0]
                    ).lower(),
                    "delete",
                )
                self.assertEqual(
                    victim_conn.execute(
                        "SELECT value FROM private_data"
                    ).fetchone()[0],
                    "must-survive",
                )
            finally:
                victim_conn.close()

    def test_unsafe_writable_sidecars_are_rejected_without_touching_victim(
        self,
    ) -> None:
        cases = ("hardlink", "symlink", "special")
        for case in cases:
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                root = Path(tmpdir)
                target = root / "storage_v2.sqlite3"
                sidecar = target.with_name(f"{target.name}-shm")
                victim = root / "victim.bin"
                victim.write_bytes(b"victim-sidecar-bytes")
                victim_hash = hashlib.sha256(victim.read_bytes()).hexdigest()
                try:
                    if case == "hardlink":
                        os.link(victim, sidecar)
                    elif case == "symlink":
                        sidecar.symlink_to(victim)
                    else:
                        os.mkfifo(sidecar)
                except (OSError, NotImplementedError) as exc:
                    continue

                with self.assertRaisesRegex(
                    ValueError,
                    "Unsafe writable v2 DB sidecar",
                ):
                    apply_migration(target)

                self.assertFalse(target.exists())
                self.assertEqual(
                    hashlib.sha256(victim.read_bytes()).hexdigest(),
                    victim_hash,
                )

    def test_sidecar_inserted_during_connect_is_rejected_before_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "storage_v2.sqlite3"
            sidecar = target.with_name(f"{target.name}-shm")
            victim = root / "victim.bin"
            victim.write_bytes(b"post-connect-victim")
            victim_hash = hashlib.sha256(victim.read_bytes()).hexdigest()
            real_connect = sqlite3.connect
            injected = False

            def inject_sidecar(
                database: object,
                *args: object,
                **kwargs: object,
            ) -> sqlite3.Connection:
                nonlocal injected
                if not kwargs.get("uri") and Path(database) == target:
                    os.link(victim, sidecar)
                    injected = True
                return real_connect(database, *args, **kwargs)

            with (
                patch.object(
                    storage_repository.sqlite3,
                    "connect",
                    side_effect=inject_sidecar,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "Unsafe writable v2 DB sidecar",
                ),
            ):
                apply_migration(target)

            self.assertTrue(injected)
            self.assertEqual(
                hashlib.sha256(victim.read_bytes()).hexdigest(),
                victim_hash,
            )

    def test_initial_database_commit_fsyncs_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "storage_v2.sqlite3"
            with patch.object(
                storage_repository,
                "_fsync_directory",
                wraps=storage_repository._fsync_directory,
            ) as fsync_directory:
                conn = apply_migration(target)
                conn.close()

            fsync_directory.assert_called_once_with(root)

    def test_schema_signature_rejects_changed_index_and_trigger_sql(self) -> None:
        mutations = {
            "index": (
                "DROP INDEX recording_titles_one_current_per_recording",
                """
                CREATE INDEX recording_titles_one_current_per_recording
                ON recording_titles(recording_id)
                """,
            ),
            "trigger": (
                "DROP TRIGGER recordings_storage_key_immutable",
                """
                CREATE TRIGGER recordings_storage_key_immutable
                BEFORE UPDATE OF storage_key ON recordings
                FOR EACH ROW
                BEGIN
                    SELECT 1;
                END
                """,
            ),
        }
        for object_type, statements in mutations.items():
            with self.subTest(object_type=object_type), tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "storage_v2.sqlite3"
                conn = apply_migration(db_path)
                try:
                    for statement in statements:
                        conn.execute(statement)
                    conn.commit()
                    with self.assertRaisesRegex(RuntimeError, "changed"):
                        require_v2_schema(conn)
                finally:
                    conn.close()

    def test_readonly_snapshot_rejects_missing_canonical_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            conn = apply_migration(db_path)
            try:
                conn.execute("DROP TRIGGER transcription_jobs_job_key_immutable")
                conn.commit()
            finally:
                conn.close()

            readonly = connect_v2(db_path, readonly=True)
            try:
                with self.assertRaisesRegex(RuntimeError, "missing"):
                    require_v2_schema(readonly)
            finally:
                readonly.close()

            with self.assertRaisesRegex(RuntimeError, "missing"):
                read_library_snapshot(db_path)

    def test_readonly_connection_rejects_symlink_and_hardlink_targets(self) -> None:
        for mode in ("symlink", "hardlink"):
            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                root = Path(tmpdir)
                db_path = root / "storage_v2.sqlite3"
                conn = apply_migration(db_path)
                conn.close()
                alias = root / "alias.sqlite3"
                try:
                    if mode == "symlink":
                        alias.symlink_to(db_path)
                    else:
                        os.link(db_path, alias)
                except OSError as exc:
                    self.skipTest(f"{mode} creation is unavailable: {exc}")

                expected_message = (
                    "symlink" if mode == "symlink" else "hard-linked"
                )
                with self.assertRaisesRegex(ValueError, expected_message):
                    connect_v2(alias, readonly=True)

    def test_library_snapshot_excludes_archived_recording_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            conn = apply_migration(db_path)
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
                            archived_at
                        )
                        VALUES (
                            'rec_archived',
                            'archived.m4a',
                            'archived.m4a',
                            'source/original.m4a',
                            'manifest.json',
                            '2026-07-23T00:00:00+00:00'
                        )
                        """
                    ).lastrowid
                )
                job_id = int(
                    conn.execute(
                        """
                        INSERT INTO transcription_jobs(
                            recording_id,
                            job_key,
                            job_relpath,
                            status,
                            progress,
                            is_current
                        )
                        VALUES (?, 'job_archived', 'jobs/job_archived', 'done', 100, 1)
                        """,
                        (recording_id,),
                    ).lastrowid
                )
                conn.execute(
                    """
                    INSERT INTO review_items(
                        recording_id,
                        job_id,
                        status,
                        severity,
                        reason_code
                    )
                    VALUES (?, ?, 'open', 'medium', 'archived_review')
                    """,
                    (recording_id, job_id),
                )
                conn.commit()
            finally:
                conn.close()

            snapshot = read_library_snapshot(db_path)

            self.assertEqual(snapshot["counts"]["recordings"], 0)
            self.assertEqual(snapshot["counts"]["done"], 0)
            self.assertEqual(snapshot["counts"]["open_reviews"], 0)
            self.assertEqual(snapshot["recordings"], [])

    def test_recording_storage_key_is_unique_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            with self._connect(db_path) as conn:
                self._apply(conn)
                conn.execute(
                    """
                    INSERT INTO recordings(
                        storage_key, original_name_raw, original_name_nfc, source_relpath, manifest_relpath
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "rec_2026_07_23_0001",
                        "원본 녹음.m4a",
                        "원본 녹음.m4a",
                        "source/original.m4a",
                        "manifest.json",
                    ),
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("SAVEPOINT invalid_capture_hash")
                    conn.execute(
                        """
                        INSERT INTO recordings(
                            storage_key, original_name_raw, original_name_nfc, source_relpath, manifest_relpath
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            "rec_2026_07_23_0001",
                            "duplicate.m4a",
                            "duplicate.m4a",
                            "source/original-2.m4a",
                            "manifest.json",
                        ),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("SAVEPOINT duplicate_capture_key_per_case")
                    conn.execute(
                        """
                        INSERT INTO recordings(
                            storage_key, original_name_raw, original_name_nfc, source_relpath, manifest_relpath
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        ("../escape", "escape.m4a", "escape.m4a", "source/original.m4a", "manifest.json"),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE recordings SET storage_key = ? WHERE storage_key = ?",
                        ("renamed_key", "rec_2026_07_23_0001"),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE recordings SET source_state = 'unknown' WHERE storage_key = ?",
                        ("rec_2026_07_23_0001",),
                    )

    def test_relative_path_json_progress_and_partial_unique_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            with self._connect(db_path) as conn:
                self._apply(conn)

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO recordings(
                            storage_key, original_name_raw, original_name_nfc, source_relpath, manifest_relpath
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        ("bad_abs", "bad.m4a", "bad.m4a", "/absolute/original.m4a", "manifest.json"),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO recordings(
                            storage_key, original_name_raw, original_name_nfc, source_relpath, manifest_relpath
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        ("bad_parent", "bad.m4a", "bad.m4a", "source/../original.m4a", "manifest.json"),
                    )

                conn.execute(
                    """
                    INSERT INTO recordings(
                        storage_key, original_name_raw, original_name_nfc, source_relpath, manifest_relpath
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "rec_2026_07_23_0002",
                        "자료구조 녹음.m4a",
                        "자료구조 녹음.m4a",
                        "source/original.m4a",
                        "manifest.json",
                    ),
                )
                recording_id = conn.execute(
                    "SELECT id FROM recordings WHERE storage_key = ?",
                    ("rec_2026_07_23_0002",),
                ).fetchone()[0]

                conn.execute(
                    """
                    INSERT INTO recording_titles(recording_id, title, title_source, is_current)
                    VALUES (?, ?, ?, 1)
                    """,
                    (recording_id, "자료구조 3교시", "schedule"),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO recording_titles(recording_id, title, title_source, is_current)
                        VALUES (?, ?, ?, 1)
                        """,
                        (recording_id, "새 제목", "manual"),
                    )

                conn.execute(
                    """
                    INSERT INTO recording_contexts(
                        recording_id, context_type, label, context_json, source, is_selected
                    )
                    VALUES (?, ?, ?, ?, ?, 1)
                    """,
                    (
                        recording_id,
                        "class_session",
                        "2026-2 자료구조 3교시",
                        '{"semester":"2026-2","period":3}',
                        "schedule_import",
                    ),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO recording_contexts(
                            recording_id, context_type, label, context_json, source, is_selected
                        )
                        VALUES (?, ?, ?, ?, ?, 1)
                        """,
                        (
                            recording_id,
                            "memo",
                            "다른 컨텍스트",
                            '{"note":"secondary"}',
                            "manual",
                        ),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO recording_contexts(
                            recording_id, context_type, label, context_json, source, is_selected
                        )
                        VALUES (?, ?, ?, ?, ?, 0)
                        """,
                        (
                            recording_id,
                            "meeting",
                            "잘못된 json",
                            "{bad-json}",
                            "manual",
                        ),
                    )

                conn.execute(
                    """
                    INSERT INTO transcription_jobs(
                        recording_id, job_key, job_relpath, status, progress, config_json, manifest_json, is_current
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        recording_id,
                        "job-1",
                        "jobs/job-1",
                        "queued",
                        0,
                        '{"profile":"lecture"}',
                        '{"storage_key":"rec_2026_07_23_0002"}',
                    ),
                )
                job_id = conn.execute(
                    "SELECT id FROM transcription_jobs WHERE job_key = ?",
                    ("job-1",),
                ).fetchone()[0]

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE transcription_jobs SET job_key = ? WHERE id = ?",
                        ("renamed-job", job_id),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO transcription_jobs(
                            recording_id, job_key, job_relpath, status, progress, config_json, manifest_json, is_current
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            recording_id,
                            "job-2",
                            "jobs/job-2",
                            "queued",
                            0,
                            '{"profile":"general"}',
                            '{"storage_key":"rec_2026_07_23_0002"}',
                        ),
                    )

                conn.execute(
                    """
                    INSERT INTO job_events(job_id, recording_id, event_seq, event_type, event_json)
                    VALUES (?, ?, 1, 'queued', '{"status":"queued"}')
                    """,
                    (job_id, recording_id),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO job_events(job_id, recording_id, event_seq, event_type, event_json)
                        VALUES (?, ?, 1, 'duplicate-sequence', '{"status":"processing"}')
                        """,
                        (job_id, recording_id),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO transcription_jobs(
                            recording_id, job_key, job_relpath, status, progress, config_json, manifest_json, is_current
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                        """,
                        (
                            recording_id,
                            "job-3",
                            "jobs\\job-3",
                            "queued",
                            101,
                            '{"profile":"general"}',
                            '{"storage_key":"rec_2026_07_23_0002"}',
                        ),
                    )

                conn.execute(
                    """
                    INSERT INTO engine_runs(
                        job_id, recording_id, engine_name, engine_version, provider, status, is_selected, params_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (job_id, recording_id, "whisper-large-v3", "1", "faster-whisper", "running", '{"beam_size":5}'),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO engine_runs(
                            job_id, recording_id, engine_name, engine_version, provider, status, is_selected, params_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                        """,
                        (job_id, recording_id, "mlx-whisper", "2", "mlx", "planned", '{"temperature":0.0}'),
                    )

    def test_artifacts_use_composite_job_recording_fk_and_uniqueness(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            with self._connect(db_path) as conn:
                self._apply(conn)

                conn.execute(
                    "INSERT INTO recordings(storage_key, original_name_raw, original_name_nfc, source_relpath, manifest_relpath) VALUES (?, ?, ?, ?, ?)",
                    ("rec_a", "a.m4a", "a.m4a", "source/a.m4a", "manifest.json"),
                )
                conn.execute(
                    "INSERT INTO recordings(storage_key, original_name_raw, original_name_nfc, source_relpath, manifest_relpath) VALUES (?, ?, ?, ?, ?)",
                    ("rec_b", "b.m4a", "b.m4a", "source/b.m4a", "manifest.json"),
                )
                rec_a = conn.execute("SELECT id FROM recordings WHERE storage_key = 'rec_a'").fetchone()[0]
                rec_b = conn.execute("SELECT id FROM recordings WHERE storage_key = 'rec_b'").fetchone()[0]

                conn.execute(
                    """
                    INSERT INTO transcription_jobs(
                        recording_id, job_key, job_relpath, status, progress, config_json, manifest_json, is_current
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (rec_a, "job-a", "jobs/job-a", "processing", 10, '{"profile":"lecture"}', '{"job":"a"}'),
                )
                job_a = conn.execute("SELECT id FROM transcription_jobs WHERE job_key = 'job-a'").fetchone()[0]

                conn.execute(
                    """
                    INSERT INTO artifacts(
                        recording_id, job_id, artifact_kind, revision, path_rel, content_sha256, bytes, mime_type
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rec_a,
                        job_a,
                        "transcript_raw_text",
                        1,
                        "jobs/job-a/transcript.txt",
                        "abc123",
                        100,
                        "text/plain",
                    ),
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO artifacts(
                            recording_id, job_id, artifact_kind, revision, path_rel, content_sha256, bytes, mime_type
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            rec_a,
                            job_a,
                            "transcript_raw_text",
                            2,
                            "jobs/job-a/transcript-v2-latest.txt",
                            "latest-conflict",
                            110,
                            "text/plain",
                        ),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO artifacts(
                            recording_id, job_id, artifact_kind, revision, path_rel, content_sha256, bytes, mime_type
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            rec_b,
                            job_a,
                            "transcript_segments_json",
                            1,
                            "jobs/job-a/transcript.segments.json",
                            "def456",
                            200,
                            "application/json",
                        ),
                    )

                artifact_id = conn.execute(
                    "SELECT id FROM artifacts WHERE recording_id = ? AND job_id = ?",
                    (rec_a, job_a),
                ).fetchone()[0]
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO review_items(
                            recording_id, job_id, artifact_id, status, severity, reason_code
                        )
                        VALUES (?, ?, ?, 'open', 'high', 'cross_recording')
                        """,
                        (rec_b, job_a, artifact_id),
                    )

                conn.execute(
                    """
                    UPDATE transcription_jobs
                    SET is_current = 0
                    WHERE id = ?
                    """,
                    (job_a,),
                )
                conn.execute(
                    """
                    INSERT INTO transcription_jobs(
                        recording_id, job_key, job_relpath, status, progress, config_json, manifest_json, is_current
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (rec_a, "job-a-2", "jobs/job-a-2", "done", 100, '{"profile":"general"}', '{"job":"a-2"}'),
                )
                job_a_2 = conn.execute(
                    "SELECT id FROM transcription_jobs WHERE job_key = 'job-a-2'"
                ).fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO artifacts(
                        recording_id, job_id, artifact_kind, revision, path_rel, content_sha256, bytes, mime_type
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rec_a,
                        job_a_2,
                        "transcript_raw_text",
                        1,
                        "jobs/job-a-2/transcript.txt",
                        "job-a-2-sha",
                        90,
                        "text/plain",
                    ),
                )
                artifact_job_a_2 = conn.execute(
                    "SELECT id FROM artifacts WHERE job_id = ?",
                    (job_a_2,),
                ).fetchone()[0]
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO review_items(
                            recording_id, job_id, artifact_id, status, severity, reason_code
                        )
                        VALUES (?, ?, ?, 'open', 'high', 'cross_job')
                        """,
                        (rec_a, job_a, artifact_job_a_2),
                    )

                conn.execute(
                    """
                    INSERT INTO legacy_import_map(
                        legacy_kind, legacy_key, recording_id, source_fingerprint, legacy_snapshot_json
                    )
                    VALUES ('job', '42', ?, 'sha256:abc', '{"status":"DONE"}')
                    """,
                    (rec_a,),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO legacy_import_map(
                            legacy_kind, legacy_key, recording_id, source_fingerprint
                        )
                        VALUES ('job', '42', ?, 'sha256:different')
                        """,
                        (rec_b,),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO artifacts(
                            recording_id, job_id, artifact_kind, revision, path_rel, content_sha256, bytes, mime_type
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            rec_a,
                            job_a,
                            "transcript_raw_text",
                            1,
                            "jobs/job-a/transcript-v2.txt",
                            "ghi789",
                            110,
                            "text/plain",
                        ),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO artifacts(
                            recording_id, job_id, artifact_kind, revision, path_rel, content_sha256, bytes, mime_type
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            rec_a,
                            job_a,
                            "quality_scorecard",
                            1,
                            "jobs/job-a/transcript.txt",
                            "jkl012",
                            120,
                            "application/json",
                        ),
                    )

    def test_migration_refuses_legacy_database_markers_before_persistent_ddl(self) -> None:
        for legacy_table in ("jobs", "deliveries"):
            with self.subTest(legacy_table=legacy_table), tempfile.TemporaryDirectory() as tmpdir:
                db_path = Path(tmpdir) / "legacy.sqlite3"
                with self._connect(db_path) as conn:
                    conn.execute(
                        f"""
                        CREATE TABLE {legacy_table} (
                            id INTEGER PRIMARY KEY,
                            status TEXT NOT NULL
                        )
                        """
                    )
                    conn.commit()

                    with self.assertRaises(sqlite3.IntegrityError):
                        self._apply(conn)
                    conn.rollback()

                    persistent_tables = {
                        row["name"]
                        for row in conn.execute(
                            """
                            SELECT name
                            FROM sqlite_master
                            WHERE type = 'table'
                            """
                        ).fetchall()
                    }
                    self.assertEqual(persistent_tables, {legacy_table})

    def test_archive_evidence_schema_has_expected_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            with self._connect(db_path) as conn:
                self._apply(conn)
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                self.assertTrue(
                    {
                        "archive_evidence_cases",
                        "archive_evidence_captures",
                        "archive_evidence_revisions",
                        "archive_evidence_observations",
                        "archive_evidence_canonical_selections",
                    }.issubset(tables)
                )

                triggers = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                    ).fetchall()
                }
                self.assertIn(
                    "archive_evidence_cases_case_key_immutable",
                    triggers,
                )
                self.assertIn(
                    "archive_evidence_captures_capture_key_immutable",
                    triggers,
                )
                self.assertIn(
                    "archive_evidence_canonical_selections_immutable",
                    triggers,
                )

                conn.execute(
                    "INSERT INTO archive_evidence_cases("
                    "case_key, legacy_delivery_key, logical_stem"
                    ") VALUES (?, ?, ?)",
                    ("case_for_schema", "delivery_schema", "stem_schema"),
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        UPDATE archive_evidence_cases
                        SET case_key = ?
                        WHERE case_key = ?
                        """,
                        ("renamed_case", "case_for_schema"),
                    )

    def test_archive_evidence_canonical_selection_enforces_case_and_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            with self._connect(db_path) as conn:
                self._apply(conn)
                conn.executemany(
                    """
                    INSERT INTO archive_evidence_cases(
                        case_key, legacy_delivery_key, logical_stem
                    )
                    VALUES (?, ?, ?)
                    """,
                    [
                        ("selection_case_a", "selection_delivery_a", "stem_a"),
                        ("selection_case_b", "selection_delivery_b", "stem_b"),
                    ],
                )
                case_a = int(
                    conn.execute(
                        """
                        SELECT id FROM archive_evidence_cases
                        WHERE case_key = 'selection_case_a'
                        """
                    ).fetchone()[0]
                )
                case_b = int(
                    conn.execute(
                        """
                        SELECT id FROM archive_evidence_cases
                        WHERE case_key = 'selection_case_b'
                        """
                    ).fetchone()[0]
                )
                conn.execute(
                    """
                    INSERT INTO archive_evidence_revisions(
                        case_id,
                        artifact_kind,
                        content_sha256,
                        bytes,
                        path_rel
                    )
                    VALUES (?, 'summary_markdown', ?, 10, ?)
                    """,
                    (
                        case_a,
                        "a" * 64,
                        f"cases/selection_case_a/revisions/"
                        f"summary_markdown/{'a' * 64}.md",
                    ),
                )
                revision_id = int(
                    conn.execute(
                        """
                        SELECT id FROM archive_evidence_revisions
                        WHERE case_id = ?
                        """,
                        (case_a,),
                    ).fetchone()[0]
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO archive_evidence_canonical_selections(
                            case_id,
                            revision_id,
                            artifact_kind,
                            promotion_plan_sha256
                        )
                        VALUES (?, ?, 'summary_markdown', ?)
                        """,
                        (case_b, revision_id, "b" * 64),
                    )

                conn.execute(
                    """
                    INSERT INTO archive_evidence_canonical_selections(
                        case_id,
                        revision_id,
                        artifact_kind,
                        promotion_plan_sha256
                    )
                    VALUES (?, ?, 'summary_markdown', ?)
                    """,
                    (case_a, revision_id, "c" * 64),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        UPDATE archive_evidence_canonical_selections
                        SET promotion_plan_sha256 = ?
                        WHERE case_id = ?
                        """,
                        ("d" * 64, case_a),
                    )

    def test_archive_evidence_canonical_selection_fk_prevents_referenced_revision_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            with self._connect(db_path) as conn:
                self._apply(conn)

                conn.execute(
                    """
                    INSERT INTO archive_evidence_cases(
                        case_key, legacy_delivery_key, logical_stem
                    )
                    VALUES ('fk_case', 'fk_delivery', 'fk_stem')
                    """
                )
                case_id = int(
                    conn.execute(
                        "SELECT id FROM archive_evidence_cases WHERE case_key = 'fk_case'"
                    ).fetchone()[0]
                )
                conn.execute(
                    """
                    INSERT INTO archive_evidence_revisions(
                        case_id,
                        artifact_kind,
                        content_sha256,
                        bytes,
                        path_rel
                    )
                    VALUES (?, 'summary_markdown', ?, 10,
                            'cases/fk_case/revisions/summary_markdown/sha.md')
                    """,
                    (case_id, "a" * 64),
                )
                revision_id = int(
                    conn.execute(
                        "SELECT id FROM archive_evidence_revisions WHERE case_id = ?",
                        (case_id,),
                    ).fetchone()[0]
                )
                conn.execute(
                    """
                    INSERT INTO archive_evidence_canonical_selections(
                        case_id,
                        revision_id,
                        artifact_kind,
                        promotion_plan_sha256
                    )
                    VALUES (?, ?, 'summary_markdown', ?)
                    """,
                    (case_id, revision_id, "b" * 64),
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "DELETE FROM archive_evidence_revisions WHERE id = ?",
                        (revision_id,),
                    )

    def test_archive_evidence_case_constraints_status_and_hash_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            with self._connect(db_path) as conn:
                self._apply(conn)
                conn.execute(
                    """
                    INSERT INTO archive_evidence_cases(
                        case_key, legacy_delivery_key, logical_stem
                    )
                    VALUES ('cap_case', 'delivery_cap', 'stem')
                    """
                )
                case_id = conn.execute(
                    "SELECT id FROM archive_evidence_cases WHERE case_key = 'cap_case'"
                ).fetchone()["id"]
                valid_hash = "a" * 64

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO archive_evidence_captures(
                            case_id, capture_key, reconciliation_classification,
                            legacy_database_sha256, source_fingerprint, plan_sha256,
                            manifest_relpath, snapshot_json
                        )
                        VALUES (?, 'capture', 'verified_delivered', 'x', 'x', 'x',
                        'cases/cap_case/captures/capture.json', '{}')
                        """,
                        (case_id,),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO archive_evidence_captures(
                            case_id, capture_key, reconciliation_classification,
                            legacy_database_sha256, source_fingerprint,
                            plan_sha256, manifest_relpath, snapshot_json
                        )
                        VALUES (?, 'capture2', 'verified_delivered',
                                'g' || substr(printf('%064x', 0), 1, 62),
                                'a' || substr(printf('%064x', 0), 1, 62),
                                'b' || substr(printf('%064x', 0), 1, 62),
                                'cases/cap_case/captures/capture-2.json', '{}')
                        """,
                        (case_id,),
                    )

                conn.execute(
                    """
                    INSERT INTO archive_evidence_captures(
                        case_id, capture_key, reconciliation_classification,
                        legacy_database_sha256, source_fingerprint, plan_sha256,
                        manifest_relpath, snapshot_json
                    )
                    VALUES (?, 'capture-valid', 'verified_delivered', ?, ?, ?,
                            'cases/cap_case/captures/capture-valid.json', '{}')
                    """,
                    (
                        case_id,
                        valid_hash,
                        valid_hash,
                        valid_hash,
                    ),
                )
                capture_id = conn.execute(
                    "SELECT id FROM archive_evidence_captures WHERE capture_key = 'capture-valid'"
                ).fetchone()["id"]

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO archive_evidence_revisions(
                            case_id, artifact_kind, content_sha256, bytes, mime_type, path_rel
                        )
                        VALUES (?, 'correction_text', 'deadbeef', 10, 'text/plain',
                        'cases/cap_case/revisions/correction_text/deadbeef.txt')
                        """,
                        (case_id,),
                    )

                conn.execute(
                    """
                    INSERT INTO archive_evidence_revisions(
                        case_id, artifact_kind, content_sha256, bytes, mime_type, path_rel
                    )
                    VALUES (?, 'correction_text', ?, 10, 'text/plain',
                            'cases/cap_case/revisions/correction_text/correct.txt')
                    """,
                    (case_id, valid_hash),
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO archive_evidence_revisions(
                            case_id, artifact_kind, content_sha256, bytes, mime_type, path_rel
                        )
                        VALUES (?, 'correction_text', ?, 10, 'text/plain',
                                '../bad/path.txt')
                        """,
                        (case_id, valid_hash),
                    )

                conn.execute(
                    """
                    INSERT INTO archive_evidence_observations(
                        capture_id, case_id, revision_id,
                        source_role, source_root_label, source_relpath,
                        observed_sha256, relationship
                    )
                    VALUES (?, ?, 1, 'historical', 'hist', 'some/path.txt',
                            ?, 'unclaimed')
                    """,
                    (capture_id, case_id, valid_hash),
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO archive_evidence_observations(
                            capture_id, case_id, revision_id,
                            source_role, source_root_label, source_relpath,
                            observed_sha256, relationship
                        )
                        VALUES (?, ?, 1, 'historical', 'hist', 'some/path.txt',
                                ?, 'unclaimed')
                        """,
                        (capture_id, case_id, valid_hash),
                    )

    def test_archive_evidence_case_key_and_fk_constraints_are_isolated_per_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "storage_v2.sqlite3"
            with self._connect(db_path) as conn:
                self._apply(conn)

                conn.execute(
                    """
                    INSERT INTO archive_evidence_cases(
                        case_key, legacy_delivery_key, logical_stem
                    )
                    VALUES ('case_a', 'delivery_a', 'stem_a')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO archive_evidence_cases(
                        case_key, legacy_delivery_key, logical_stem
                    )
                    VALUES ('case_b', 'delivery_b', 'stem_b')
                    """
                )
                case_a = conn.execute(
                    "SELECT id FROM archive_evidence_cases WHERE case_key = 'case_a'"
                ).fetchone()["id"]
                case_b = conn.execute(
                    "SELECT id FROM archive_evidence_cases WHERE case_key = 'case_b'"
                ).fetchone()["id"]

                conn.execute(
                    """
                    INSERT INTO archive_evidence_revisions(case_id, artifact_kind, content_sha256, bytes, mime_type, path_rel)
                    VALUES (?, 'correction_text', 'a' || substr(printf('%064x', 0), 1, 63), 10, 'text/plain', 'cases/case_a/revisions/correction_text/aa.txt')
                    """,
                    (case_a,),
                )
                revision_a = conn.execute(
                    "SELECT id FROM archive_evidence_revisions WHERE case_id = ?",
                    (case_a,),
                ).fetchone()["id"]

                conn.execute(
                    """
                    INSERT INTO archive_evidence_captures(
                        case_id, capture_key, reconciliation_classification,
                        legacy_database_sha256, source_fingerprint, plan_sha256,
                        manifest_relpath, snapshot_json
                    )
                    VALUES (
                        ?, 'capture_a', 'verified_delivered',
                        'a' || substr(printf('%064x', 0), 1, 63),
                        'b' || substr(printf('%064x', 0), 1, 63),
                        'c' || substr(printf('%064x', 0), 1, 63),
                        'cases/case_a/captures/capture_a.json',
                        '{}'
                    )
                    """,
                    (case_a,),
                )
                capture_a = conn.execute(
                    "SELECT id FROM archive_evidence_captures WHERE capture_key = 'capture_a'"
                ).fetchone()["id"]

                conn.execute(
                    """
                    INSERT INTO archive_evidence_observations(
                        capture_id, case_id, revision_id,
                        source_role, source_root_label, source_relpath,
                        observed_sha256, relationship
                    )
                    VALUES (?, ?, ?, 'current_gh', 'current_gh', 'correction.txt',
                            'a' || substr(printf('%064x', 0), 1, 63), 'matches_ledger')
                    """,
                    (capture_a, case_a, revision_a),
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO archive_evidence_captures(
                            case_id, capture_key, reconciliation_classification,
                            legacy_database_sha256, source_fingerprint, plan_sha256,
                            manifest_relpath, snapshot_json
                        )
                        VALUES (
                            ?, 'capture_a', 'verified_delivered',
                            'a' || substr(printf('%064x', 0), 1, 63),
                            'b' || substr(printf('%064x', 0), 1, 63),
                            'c' || substr(printf('%064x', 0), 1, 63),
                            'cases/case_b/captures/capture_a.json',
                            '{}'
                        )
                        """,
                        (case_b,),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO archive_evidence_observations(
                            capture_id, case_id, revision_id,
                            source_role, source_root_label, source_relpath,
                            observed_sha256, relationship
                        )
                        VALUES (?, ?, ?, 'current_gh', 'current_gh', 'correction.txt',
                                'a' || substr(printf('%064x', 0), 1, 63), 'matches_ledger')
                        """,
                        (capture_a, case_b, revision_a),
                    )



if __name__ == "__main__":
    unittest.main()
