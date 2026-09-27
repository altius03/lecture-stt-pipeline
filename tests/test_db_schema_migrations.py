from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.shared.db import (  # noqa: E402
    TRANSCRIPT_DELIVERY_COLUMN_DEFS,
    TRANSCRIPT_POSTPROCESS_JOB_COLUMN_DEFS,
    TranscriptSchemaCompatibilityError,
    init_transcript_deliveries_table,
    init_transcript_postprocess_jobs_table,
    insert_transcript_postprocess_job_if_absent,
    upsert_transcript_delivery,
)


TABLE_CASES = (
    (
        "transcript_deliveries",
        init_transcript_deliveries_table,
        TRANSCRIPT_DELIVERY_COLUMN_DEFS,
    ),
    (
        "transcript_postprocess_jobs",
        init_transcript_postprocess_jobs_table,
        TRANSCRIPT_POSTPROCESS_JOB_COLUMN_DEFS,
    ),
)


class TranscriptSchemaMigrationTests(unittest.TestCase):
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        return conn

    def _column_info(
        self,
        conn: sqlite3.Connection,
        table_name: str,
    ) -> dict[str, sqlite3.Row]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row["name"]): row for row in rows}

    def _create_table_without(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_defs: dict[str, str],
        missing: set[str],
    ) -> None:
        definitions = ", ".join(
            f"{name} {column_def}"
            for name, column_def in column_defs.items()
            if name not in missing
        )
        conn.execute(f"CREATE TABLE {table_name} ({definitions})")

    def _create_table_without_source_job_key(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_defs: dict[str, str],
    ) -> None:
        definitions = ", ".join(
            f"{name} {'INTEGER' if name == 'source_job_id' else column_def}"
            for name, column_def in column_defs.items()
        )
        conn.execute(f"CREATE TABLE {table_name} ({definitions})")

    def _insert_required_row(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_defs: dict[str, str],
        *,
        missing: set[str] | None = None,
    ) -> None:
        missing = missing or set()
        payload: dict[str, object] = {}
        for name, column_def in column_defs.items():
            if name in missing:
                continue
            normalized = " ".join(column_def.upper().split())
            if name == "source_job_id":
                payload[name] = 7
            elif "NOT NULL" in normalized and "DEFAULT" not in normalized:
                payload[name] = f"{name}-value"

        columns = ", ".join(payload)
        placeholders = ", ".join("?" for _ in payload)
        conn.execute(
            f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})",
            tuple(payload.values()),
        )
        conn.commit()

    def _api_required_fields(
        self,
        column_defs: dict[str, str],
    ) -> dict[str, object]:
        payload: dict[str, object] = {}
        for name, column_def in column_defs.items():
            if name in {"source_job_id", "created_at", "updated_at"}:
                continue
            normalized = " ".join(column_def.upper().split())
            if "NOT NULL" in normalized and "DEFAULT" not in normalized:
                payload[name] = f"{name}-api-value"
        return payload

    def _assert_conflict_target_is_writable(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_defs: dict[str, str],
    ) -> None:
        fields = self._api_required_fields(column_defs)
        if table_name == "transcript_deliveries":
            upsert_transcript_delivery(
                conn,
                23,
                **fields,
                storage_key="first",
            )
            upsert_transcript_delivery(
                conn,
                23,
                **fields,
                storage_key="updated",
            )
            row = conn.execute(
                "SELECT COUNT(*) AS total, storage_key "
                "FROM transcript_deliveries WHERE source_job_id = 23"
            ).fetchone()
            self.assertEqual(row["total"], 1)
            self.assertEqual(row["storage_key"], "updated")
            return

        inserted = insert_transcript_postprocess_job_if_absent(
            conn,
            23,
            **fields,
        )
        inserted_again = insert_transcript_postprocess_job_if_absent(
            conn,
            23,
            **fields,
        )
        self.assertTrue(inserted)
        self.assertFalse(inserted_again)
        row_count = conn.execute(
            "SELECT COUNT(*) AS total FROM transcript_postprocess_jobs "
            "WHERE source_job_id = 23"
        ).fetchone()["total"]
        self.assertEqual(row_count, 1)

    def test_non_empty_incompatible_partial_schema_fails_before_mutation(self) -> None:
        for table_name, initializer, _column_defs in TABLE_CASES:
            with self.subTest(table=table_name):
                conn = self._connect()
                try:
                    conn.execute(f"CREATE TABLE {table_name} (legacy_marker TEXT)")
                    conn.execute(
                        f"INSERT INTO {table_name} (legacy_marker) VALUES (?)",
                        ("keep-me",),
                    )
                    conn.commit()

                    with self.assertRaises(
                        TranscriptSchemaCompatibilityError
                    ) as raised:
                        initializer(conn)

                    message = str(raised.exception)
                    self.assertIsInstance(raised.exception, sqlite3.DatabaseError)
                    self.assertIn(
                        f"incompatible non-empty schema for {table_name}",
                        message,
                    )
                    self.assertIn("source_job_id (INTEGER PRIMARY KEY)", message)
                    self.assertIn("Back up the database", message)
                    self.assertEqual(
                        set(self._column_info(conn, table_name)),
                        {"legacy_marker"},
                    )
                    row = conn.execute(
                        f"SELECT legacy_marker FROM {table_name}"
                    ).fetchone()
                    self.assertEqual(row["legacy_marker"], "keep-me")
                finally:
                    conn.close()

    def test_empty_incompatible_partial_schema_is_rebuilt_canonically(self) -> None:
        for table_name, initializer, column_defs in TABLE_CASES:
            with self.subTest(table=table_name):
                conn = self._connect()
                try:
                    conn.execute(f"CREATE TABLE {table_name} (legacy_marker TEXT)")

                    initializer(conn)

                    info = self._column_info(conn, table_name)
                    self.assertEqual(set(info), set(column_defs))
                    self.assertNotIn("legacy_marker", info)
                    self.assertEqual(info["source_job_id"]["pk"], 1)
                    row_count = conn.execute(
                        f"SELECT COUNT(*) AS total FROM {table_name}"
                    ).fetchone()["total"]
                    self.assertEqual(row_count, 0)
                finally:
                    conn.close()

    def test_non_empty_complete_columns_without_key_fail_before_mutation(
        self,
    ) -> None:
        for table_name, initializer, column_defs in TABLE_CASES:
            with self.subTest(table=table_name):
                conn = self._connect()
                try:
                    self._create_table_without_source_job_key(
                        conn,
                        table_name,
                        column_defs,
                    )
                    self._insert_required_row(conn, table_name, column_defs)
                    schema_before = conn.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = ?",
                        (table_name,),
                    ).fetchone()["sql"]
                    changes_before = conn.total_changes
                    statements: list[str] = []
                    conn.set_trace_callback(statements.append)

                    with self.assertRaises(
                        TranscriptSchemaCompatibilityError
                    ) as raised:
                        initializer(conn)

                    conn.set_trace_callback(None)
                    self.assertIn(
                        "source_job_id lacks a required single-column PRIMARY KEY "
                        "or non-partial UNIQUE constraint",
                        str(raised.exception),
                    )
                    schema_after = conn.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = ?",
                        (table_name,),
                    ).fetchone()["sql"]
                    self.assertEqual(schema_after, schema_before)
                    self.assertEqual(conn.total_changes, changes_before)
                    self.assertFalse(
                        any(
                            statement.lstrip().upper().startswith(
                                ("ALTER TABLE", "DROP TABLE", "CREATE INDEX")
                            )
                            for statement in statements
                        )
                    )
                    row = conn.execute(
                        f"SELECT source_job_id FROM {table_name}"
                    ).fetchone()
                    self.assertEqual(row["source_job_id"], 7)
                finally:
                    conn.set_trace_callback(None)
                    conn.close()

    def test_empty_complete_columns_without_key_are_rebuilt_and_writable(
        self,
    ) -> None:
        for table_name, initializer, column_defs in TABLE_CASES:
            with self.subTest(table=table_name):
                conn = self._connect()
                try:
                    self._create_table_without_source_job_key(
                        conn,
                        table_name,
                        column_defs,
                    )

                    initializer(conn)

                    info = self._column_info(conn, table_name)
                    self.assertEqual(set(info), set(column_defs))
                    self.assertEqual(info["source_job_id"]["pk"], 1)
                    self._assert_conflict_target_is_writable(
                        conn,
                        table_name,
                        column_defs,
                    )
                finally:
                    conn.close()

    def test_single_column_unique_source_job_key_is_accepted_and_writable(
        self,
    ) -> None:
        for table_name, initializer, column_defs in TABLE_CASES:
            with self.subTest(table=table_name):
                conn = self._connect()
                try:
                    self._create_table_without_source_job_key(
                        conn,
                        table_name,
                        column_defs,
                    )
                    index_name = f"uq_{table_name}_source_job_id"
                    conn.execute(
                        f"CREATE UNIQUE INDEX {index_name} "
                        f"ON {table_name} (source_job_id)"
                    )
                    self._insert_required_row(conn, table_name, column_defs)

                    initializer(conn)

                    info = self._column_info(conn, table_name)
                    self.assertEqual(info["source_job_id"]["pk"], 0)
                    index_names = {
                        str(row["name"])
                        for row in conn.execute(
                            f"PRAGMA index_list({table_name})"
                        ).fetchall()
                    }
                    self.assertIn(index_name, index_names)
                    self._assert_conflict_target_is_writable(
                        conn,
                        table_name,
                        column_defs,
                    )
                finally:
                    conn.close()

    def test_nullable_and_defaulted_columns_are_added_without_losing_rows(
        self,
    ) -> None:
        missing = {"storage_key", "status"}
        for table_name, initializer, column_defs in TABLE_CASES:
            with self.subTest(table=table_name):
                conn = self._connect()
                try:
                    self._create_table_without(
                        conn,
                        table_name,
                        column_defs,
                        missing,
                    )
                    self._insert_required_row(
                        conn,
                        table_name,
                        column_defs,
                        missing=missing,
                    )

                    initializer(conn)

                    self.assertEqual(
                        set(self._column_info(conn, table_name)),
                        set(column_defs),
                    )
                    row = conn.execute(
                        f"SELECT source_job_id, storage_key, status FROM {table_name}"
                    ).fetchone()
                    self.assertEqual(row["source_job_id"], 7)
                    self.assertIsNone(row["storage_key"])
                    self.assertEqual(row["status"], "PENDING")
                finally:
                    conn.close()

    def test_complete_schema_is_left_unchanged(self) -> None:
        for table_name, initializer, column_defs in TABLE_CASES:
            with self.subTest(table=table_name):
                conn = self._connect()
                try:
                    initializer(conn)
                    self._insert_required_row(conn, table_name, column_defs)
                    schema_before = conn.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = ?",
                        (table_name,),
                    ).fetchone()["sql"]
                    statements: list[str] = []
                    conn.set_trace_callback(statements.append)

                    initializer(conn)

                    conn.set_trace_callback(None)
                    schema_after = conn.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = ?",
                        (table_name,),
                    ).fetchone()["sql"]
                    self.assertEqual(schema_after, schema_before)
                    self.assertFalse(
                        any(
                            statement.lstrip().upper().startswith(
                                ("ALTER TABLE", "DROP TABLE")
                            )
                            for statement in statements
                        )
                    )
                    row_count = conn.execute(
                        f"SELECT COUNT(*) AS total FROM {table_name}"
                    ).fetchone()["total"]
                    self.assertEqual(row_count, 1)
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
