from __future__ import annotations

import errno
import hashlib
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from lecture_stt.shared.paths import repo_root


DEFAULT_MIGRATION_PATH = repo_root() / "migrations" / "v2" / "0001_recording_store.sql"
MIGRATION_VERSION = "storage_v2/0001_recording_store"
_SchemaSignature = tuple[tuple[str, str, str, str | None], ...]
_FileState = tuple[int, int, int, int, int, int, int]
_WRITABLE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


@dataclass(frozen=True)
class _WritableTargetSnapshot:
    main: _FileState
    main_sha256: str
    sidecars: tuple[tuple[str, _FileState | None], ...]


def _configure(conn: sqlite3.Connection, *, writable: bool) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if writable:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    return conn


def _file_state(metadata: os.stat_result) -> _FileState:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _validate_writable_target(path: Path) -> _FileState | None:
    if path.is_symlink():
        raise ValueError(f"Refusing writable v2 DB symlink: {path}")
    if not os.path.lexists(path):
        return None
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Writable v2 DB target must be a regular file: {path}")
    if metadata.st_nlink != 1:
        raise ValueError(f"Refusing hard-linked writable v2 DB target: {path}")
    return _file_state(metadata)


def _validate_readonly_target(path: Path) -> _FileState:
    if path.is_symlink():
        raise ValueError(f"Refusing read-only v2 DB symlink: {path}")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Read-only v2 DB target must be a regular file: {path}")
    if metadata.st_nlink != 1:
        raise ValueError(f"Refusing hard-linked read-only v2 DB target: {path}")
    return _file_state(metadata)


def _validate_writable_sidecars(
    path: Path,
    *,
    reject_active: bool = False,
) -> tuple[tuple[str, _FileState | None], ...]:
    states: list[tuple[str, _FileState | None]] = []
    for suffix in _WRITABLE_SIDECAR_SUFFIXES:
        sidecar = path.with_name(f"{path.name}{suffix}")
        try:
            metadata = sidecar.lstat()
        except FileNotFoundError:
            states.append((suffix, None))
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(
                f"Unsafe writable v2 DB sidecar {suffix}: {sidecar}"
            )
        if (
            reject_active
            and suffix in {"-wal", "-journal"}
            and metadata.st_size > 0
        ):
            raise RuntimeError(
                "Writable v2 DB has active WAL/journal state; close all "
                f"writers and checkpoint it before migration: {sidecar}"
            )
        states.append((suffix, _file_state(metadata)))
    return tuple(states)


def _sha256_opened_regular_file(
    path: Path,
    *,
    expected_state: _FileState,
) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = _file_state(os.fstat(descriptor))
        if before != expected_state:
            raise RuntimeError(
                f"Writable v2 DB changed while opening: {path}"
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = _file_state(os.fstat(descriptor))
        current = _validate_writable_target(path)
        if after != before or current != before:
            raise RuntimeError(
                f"Writable v2 DB changed while hashing: {path}"
            )
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _writable_target_snapshot(
    path: Path,
    *,
    reject_active: bool = False,
) -> _WritableTargetSnapshot:
    main = _validate_writable_target(path)
    if main is None:
        raise FileNotFoundError(path)
    sidecars = _validate_writable_sidecars(
        path,
        reject_active=reject_active,
    )
    return _WritableTargetSnapshot(
        main=main,
        main_sha256=_sha256_opened_regular_file(
            path,
            expected_state=main,
        ),
        sidecars=sidecars,
    )


def _assert_writable_target_snapshot(
    path: Path,
    expected: _WritableTargetSnapshot,
    *,
    reject_active: bool = False,
    allow_shm_metadata_change: bool = False,
) -> None:
    current = _writable_target_snapshot(
        path,
        reject_active=reject_active,
    )
    snapshots_match = current == expected
    if allow_shm_metadata_change and not snapshots_match:
        expected_sidecars = dict(expected.sidecars)
        current_sidecars = dict(current.sidecars)
        expected_shm = expected_sidecars.pop("-shm")
        current_shm = current_sidecars.pop("-shm")
        shm_matches = (
            expected_shm is None
            and current_shm is None
        ) or (
            expected_shm is not None
            and current_shm is not None
            and expected_shm[:5] == current_shm[:5]
        )
        snapshots_match = (
            current.main == expected.main
            and current.main_sha256 == expected.main_sha256
            and current_sidecars == expected_sidecars
            and shm_matches
        )
    if not snapshots_match:
        raise RuntimeError(
            f"Writable v2 DB or its sidecars changed during validation: {path}"
        )


def _open_file_descriptor_states() -> dict[int, _FileState]:
    descriptor_root = next(
        (
            candidate
            for candidate in (Path("/dev/fd"), Path("/proc/self/fd"))
            if candidate.is_dir()
        ),
        None,
    )
    if descriptor_root is None:
        raise RuntimeError(
            "Cannot verify the SQLite opened-file binding on this platform"
        )
    states: dict[int, _FileState] = {}
    for raw_name in os.listdir(descriptor_root):
        if not raw_name.isdigit():
            continue
        descriptor = int(raw_name)
        try:
            states[descriptor] = _file_state(os.fstat(descriptor))
        except OSError:
            continue
    return states


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            unsupported = {
                errno.EINVAL,
                getattr(errno, "ENOTSUP", errno.EINVAL),
                getattr(errno, "EOPNOTSUPP", errno.EINVAL),
            }
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(descriptor)


def _connect_writable_bound(
    path: Path,
    *,
    expected_snapshot: _WritableTargetSnapshot | None,
) -> tuple[sqlite3.Connection, bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_writable_sidecars(path)
    created = expected_snapshot is None
    guard_flags = os.O_RDWR
    if created:
        guard_flags |= os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        guard_flags |= os.O_NOFOLLOW
    if not created:
        _assert_writable_target_snapshot(path, expected_snapshot)

    guard_fd = os.open(path, guard_flags, 0o600)
    conn: sqlite3.Connection | None = None
    try:
        guard_state = _file_state(os.fstat(guard_fd))
        current_state = _validate_writable_target(path)
        if (
            current_state != guard_state
            or not stat.S_ISREG(guard_state[2])
            or guard_state[3] != 1
        ):
            raise RuntimeError(
                f"Writable v2 DB pathname changed while opening: {path}"
            )
        if created:
            expected_before_write = _writable_target_snapshot(path)
        else:
            assert expected_snapshot is not None
            expected_before_write = expected_snapshot

        conn = sqlite3.connect(path, timeout=5.0)
        descriptors_after = _open_file_descriptor_states()
        sqlite_opened_matches = [
            descriptor
            for descriptor, state in descriptors_after.items()
            if descriptor != guard_fd
            and state[:2] == guard_state[:2]
            and stat.S_ISREG(state[2])
            and state[3] == 1
        ]
        if not sqlite_opened_matches:
            raise RuntimeError(
                "SQLite did not open the validated writable v2 DB inode"
            )

        database_rows = conn.execute("PRAGMA database_list").fetchall()
        main_rows = [
            row for row in database_rows if str(row[1]) == "main"
        ]
        if len(main_rows) != 1 or not str(main_rows[0][2]):
            raise RuntimeError(
                "SQLite writable v2 DB binding could not be identified"
            )
        opened_path = Path(str(main_rows[0][2]))
        opened_state = _validate_writable_target(opened_path)
        if (
            opened_state is None
            or opened_state[:2] != guard_state[:2]
            or _validate_writable_target(path) != guard_state
        ):
            raise RuntimeError(
                "SQLite writable v2 DB binding does not match the "
                "validated pathname"
            )
        _assert_writable_target_snapshot(
            path,
            expected_before_write,
            allow_shm_metadata_change=True,
        )

        configured = _configure(conn, writable=True)
        current_main = _validate_writable_target(path)
        if (
            current_main is None
            or current_main[:2] != guard_state[:2]
            or current_main[3] != 1
        ):
            raise RuntimeError(
                "Writable v2 DB pathname changed during configuration"
            )
        _validate_writable_sidecars(path)
        return configured, created
    except BaseException:
        if conn is not None:
            conn.close()
        raise
    finally:
        os.close(guard_fd)


def _connect_readonly_bound(path: Path) -> sqlite3.Connection:
    """Open a read-only database while binding SQLite to a validated inode."""

    absolute_path = path.absolute()
    baseline = _validate_readonly_target(absolute_path)
    _validate_writable_sidecars(absolute_path)
    guard_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        guard_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        guard_flags |= os.O_CLOEXEC
    guard_fd = os.open(absolute_path, guard_flags)
    conn: sqlite3.Connection | None = None
    try:
        guard_state = _file_state(os.fstat(guard_fd))
        if (
            guard_state[:4] != baseline[:4]
            or not stat.S_ISREG(guard_state[2])
            or guard_state[3] != 1
        ):
            raise RuntimeError(
                f"Read-only v2 DB pathname changed while opening: {absolute_path}"
            )
        conn = sqlite3.connect(
            f"{absolute_path.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        descriptors_after = _open_file_descriptor_states()
        sqlite_opened_matches = [
            descriptor
            for descriptor, state in descriptors_after.items()
            if descriptor != guard_fd
            and state[:2] == guard_state[:2]
            and stat.S_ISREG(state[2])
            and state[3] == 1
        ]
        if not sqlite_opened_matches:
            raise RuntimeError(
                "SQLite did not open the validated read-only v2 DB inode"
            )

        database_rows = conn.execute("PRAGMA database_list").fetchall()
        main_rows = [
            row for row in database_rows if str(row[1]) == "main"
        ]
        if len(main_rows) != 1 or not str(main_rows[0][2]):
            raise RuntimeError(
                "SQLite read-only v2 DB binding could not be identified"
            )
        opened_path = Path(str(main_rows[0][2]))
        opened_state = _validate_readonly_target(opened_path)
        current_state = _validate_readonly_target(absolute_path)
        if (
            opened_state[:4] != guard_state[:4]
            or current_state[:4] != guard_state[:4]
        ):
            raise RuntimeError(
                "SQLite read-only v2 DB binding does not match the "
                "validated pathname"
            )
        _validate_writable_sidecars(absolute_path)
        return _configure(conn, writable=False)
    except BaseException:
        if conn is not None:
            conn.close()
        raise
    finally:
        os.close(guard_fd)


def connect_v2(db_path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    if readonly:
        return _connect_readonly_bound(path)

    snapshot = (
        _writable_target_snapshot(path)
        if os.path.lexists(path)
        else None
    )
    conn, _created = _connect_writable_bound(
        path,
        expected_snapshot=snapshot,
    )
    return conn


def _migration_sql_and_checksum(
    migration_path: Path | str,
) -> tuple[Path, str, str]:
    migration = Path(migration_path).expanduser().resolve(strict=True)
    migration_bytes = migration.read_bytes()
    sql = migration_bytes.decode("utf-8")
    checksum = hashlib.sha256(migration_bytes).hexdigest()
    return migration, sql, checksum


def _normalize_schema_sql(sql: str | None) -> str | None:
    if sql is None:
        return None
    return re.sub(r"\s+", " ", sql).strip()


def _schema_signature(conn: sqlite3.Connection) -> _SchemaSignature:
    rows = conn.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE type IN ('table', 'index', 'trigger')
          AND (
              name NOT LIKE 'sqlite_%'
              OR name LIKE 'sqlite_autoindex_%'
          )
        ORDER BY type, name
        """
    ).fetchall()
    return tuple(
        (
            str(row["type"]),
            str(row["name"]),
            str(row["tbl_name"]),
            _normalize_schema_sql(row["sql"]),
        )
        for row in rows
    )


@lru_cache(maxsize=8)
def _canonical_schema_signature(sql: str) -> _SchemaSignature:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(sql)
        return _schema_signature(conn)
    finally:
        conn.close()


def _format_schema_objects(
    keys: set[tuple[str, str]],
) -> str:
    return ", ".join(f"{object_type}:{name}" for object_type, name in sorted(keys))


def _require_schema_signature(
    conn: sqlite3.Connection,
    *,
    migration_sql: str,
) -> None:
    expected = {
        (object_type, name): (table_name, normalized_sql)
        for object_type, name, table_name, normalized_sql in _canonical_schema_signature(
            migration_sql
        )
    }
    actual = {
        (object_type, name): (table_name, normalized_sql)
        for object_type, name, table_name, normalized_sql in _schema_signature(conn)
    }

    expected_keys = set(expected)
    actual_keys = set(actual)
    missing = expected_keys.difference(actual_keys)
    unexpected = actual_keys.difference(expected_keys)
    changed = {
        key
        for key in expected_keys.intersection(actual_keys)
        if expected[key] != actual[key]
    }
    if not (missing or unexpected or changed):
        return

    details: list[str] = []
    if missing:
        details.append(f"missing [{_format_schema_objects(missing)}]")
    if unexpected:
        details.append(f"unexpected [{_format_schema_objects(unexpected)}]")
    if changed:
        details.append(f"changed [{_format_schema_objects(changed)}]")
    raise RuntimeError(
        "Storage v2 schema does not match the canonical migration: "
        + "; ".join(details)
    )


def _migration_checksum(
    conn: sqlite3.Connection,
    *,
    expected_checksum: str,
    allow_unstamped: bool,
) -> str | None:
    try:
        row = conn.execute(
            """
            SELECT checksum_sha256
            FROM schema_migrations
            WHERE version = ?
            """,
            (MIGRATION_VERSION,),
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(
            "Storage v2 schema_migrations table does not match the canonical migration"
        ) from exc
    if row is None:
        raise RuntimeError(
            f"Storage v2 migration marker is missing: {MIGRATION_VERSION}"
        )

    stored = row["checksum_sha256"]
    if stored is None and allow_unstamped:
        return None
    if stored != expected_checksum:
        actual = "<unstamped>" if stored is None else str(stored)
        raise RuntimeError(
            "Storage v2 migration checksum mismatch: "
            f"expected {expected_checksum}, found {actual}"
        )
    return str(stored)


def _storage_schema_has_rows(conn: sqlite3.Connection) -> bool:
    for table in (
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
        "recording_classification_materializations",
        "recording_title_materializations",
        "outbox_events",
        "legacy_import_map",
        "archive_evidence_cases",
        "archive_evidence_captures",
        "archive_evidence_revisions",
        "archive_evidence_observations",
        "archive_evidence_canonical_selections",
    ):
        if conn.execute(f"SELECT EXISTS(SELECT 1 FROM {table} LIMIT 1)").fetchone()[0]:
            return True
    return False


def _preflight_existing_target(
    path: Path,
    *,
    migration_sql: str,
    expected_checksum: str,
) -> _WritableTargetSnapshot:
    baseline = _writable_target_snapshot(
        path,
        reject_active=False,
    )
    conn = sqlite3.connect(
        f"{path.resolve(strict=True).as_uri()}?mode=ro",
        uri=True,
        timeout=5.0,
    )
    try:
        configured = _configure(conn, writable=False)
        quick_check = [
            str(row[0])
            for row in configured.execute("PRAGMA quick_check").fetchall()
        ]
        if quick_check != ["ok"]:
            raise RuntimeError(
                "Writable v2 DB failed read-only PRAGMA quick_check: "
                + ", ".join(quick_check or ["no result"])
            )
        existing_signature = _schema_signature(configured)
        if existing_signature:
            _require_schema_signature(
                configured,
                migration_sql=migration_sql,
            )
            _migration_checksum(
                configured,
                expected_checksum=expected_checksum,
                allow_unstamped=not _storage_schema_has_rows(configured),
            )
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(
            f"Writable v2 DB failed non-mutating preflight: {path}"
        ) from exc
    finally:
        conn.close()
    _assert_writable_target_snapshot(
        path,
        baseline,
        reject_active=False,
        allow_shm_metadata_change=True,
    )
    return _writable_target_snapshot(path)


def require_v2_schema(
    conn: sqlite3.Connection,
    *,
    migration_path: Path | str = DEFAULT_MIGRATION_PATH,
) -> None:
    _, migration_sql, checksum = _migration_sql_and_checksum(migration_path)
    _require_schema_signature(conn, migration_sql=migration_sql)
    _migration_checksum(
        conn,
        expected_checksum=checksum,
        allow_unstamped=False,
    )


def apply_migration(
    db_path: Path | str,
    *,
    migration_path: Path | str = DEFAULT_MIGRATION_PATH,
    legacy_db_path: Path | str | None = None,
) -> sqlite3.Connection:
    target = Path(db_path).expanduser()
    if legacy_db_path is not None:
        legacy = Path(legacy_db_path).expanduser().resolve(strict=True)
        if target.resolve() == legacy or (
            os.path.lexists(target) and os.path.samefile(target, legacy)
        ):
            raise ValueError("Storage v2 DB must be separate from the legacy DB")
    migration, sql, checksum = _migration_sql_and_checksum(migration_path)
    existing_snapshot = (
        _preflight_existing_target(
            target,
            migration_sql=sql,
            expected_checksum=checksum,
        )
        if os.path.lexists(target)
        else None
    )
    conn, created = _connect_writable_bound(
        target,
        expected_snapshot=existing_snapshot,
    )
    try:
        existing_signature = _schema_signature(conn)
        if existing_signature:
            _require_schema_signature(conn, migration_sql=sql)
            stored_checksum = _migration_checksum(
                conn,
                expected_checksum=checksum,
                allow_unstamped=not _storage_schema_has_rows(conn),
            )
        else:
            conn.executescript(sql)
            _require_schema_signature(conn, migration_sql=sql)
            stored_checksum = _migration_checksum(
                conn,
                expected_checksum=checksum,
                allow_unstamped=True,
            )

        if stored_checksum is None:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE schema_migrations
                SET checksum_sha256 = ?
                WHERE version = ? AND checksum_sha256 IS NULL
                """,
                (checksum, MIGRATION_VERSION),
            )
            require_v2_schema(conn, migration_path=migration)
            conn.commit()
        else:
            require_v2_schema(conn, migration_path=migration)
        if created:
            _fsync_directory(target.parent)
        return conn
    except BaseException:
        conn.close()
        raise


def read_library_snapshot(
    db_path: Path | str,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    """Return a read-only, JSON-safe v2 library view for a future dashboard adapter."""

    path = Path(db_path).expanduser()
    if not path.exists():
        return {
            "schema_version": "storage-v2/library-snapshot@1",
            "available": False,
            "counts": {
                "recordings": 0,
                "queued": 0,
                "processing": 0,
                "done": 0,
                "needs_review": 0,
                "error": 0,
                "canceled": 0,
                "open_reviews": 0,
            },
            "recordings": [],
        }
    if limit < 0 or limit > 500:
        raise ValueError("limit must be between 0 and 500")

    conn = connect_v2(path, readonly=True)
    try:
        require_v2_schema(conn)
        status_counts = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                """
                SELECT j.status, COUNT(*) AS count
                FROM transcription_jobs AS j
                JOIN recordings AS r ON r.id = j.recording_id
                WHERE j.is_current = 1
                  AND j.archived_at IS NULL
                  AND r.archived_at IS NULL
                GROUP BY j.status
                """
            ).fetchall()
        }
        recording_count = int(
            conn.execute("SELECT COUNT(*) FROM recordings WHERE archived_at IS NULL").fetchone()[0]
        )
        open_reviews = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM review_items AS review
                JOIN recordings AS r ON r.id = review.recording_id
                WHERE review.status IN ('open', 'triaged')
                  AND r.archived_at IS NULL
                """
            ).fetchone()[0]
        )
        rows = conn.execute(
            """
            SELECT
                r.id,
                r.storage_key,
                r.original_name_nfc,
                r.source_state,
                r.recorded_at,
                r.received_at,
                t.title,
                c.context_type,
                c.semester,
                c.course_name,
                c.session_date,
                c.period_label,
                j.job_key,
                j.status,
                j.progress,
                j.requested_profile,
                j.requested_profile_version,
                j.finished_at
            FROM recordings AS r
            LEFT JOIN recording_titles AS t
              ON t.recording_id = r.id AND t.is_current = 1
            LEFT JOIN recording_contexts AS c
              ON c.recording_id = r.id AND c.is_selected = 1
            LEFT JOIN transcription_jobs AS j
              ON j.recording_id = r.id
             AND j.is_current = 1
             AND j.archived_at IS NULL
            WHERE r.archived_at IS NULL
            ORDER BY COALESCE(r.recorded_at, r.received_at) DESC, r.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    return {
        "schema_version": "storage-v2/library-snapshot@1",
        "available": True,
        "counts": {
            "recordings": recording_count,
            "queued": status_counts.get("queued", 0),
            "processing": status_counts.get("processing", 0),
            "done": status_counts.get("done", 0),
            "needs_review": status_counts.get("needs_review", 0),
            "error": status_counts.get("error", 0),
            "canceled": status_counts.get("canceled", 0),
            "open_reviews": open_reviews,
        },
        "recordings": [dict(row) for row in rows],
    }
