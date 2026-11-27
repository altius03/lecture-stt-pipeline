from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from lecture_stt.shared import utils
from lecture_stt.shared.paths import RECOVERY_PAIR_PREFIX, RECOVERY_STAGING_PREFIX
from lecture_stt.stt import quality_gate


STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_DONE = "DONE"
STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
STATUS_ERROR = "ERROR"
QUALITY_REPAIR_WAIT = "품질 산출물 복구 대기"


class TranscriptSchemaCompatibilityError(sqlite3.DatabaseError):
    """Raised when an existing transcript table cannot be migrated additively."""


DELIVERY_COLUMN_DEFS = {
    "source_job_id": "INTEGER",
    "subject_abbr": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "correction_txt_path": "TEXT",
    "correction_json_path": "TEXT",
    "summary_md_path": "TEXT",
    "correction_txt_sha256": "TEXT",
    "correction_json_sha256": "TEXT",
    "summary_md_sha256": "TEXT",
    "tuk_origin_txt_path": "TEXT",
    "tuk_origin_json_path": "TEXT",
    "tuk_summary_path": "TEXT",
    "obsidian_summary_path": "TEXT",
    "correction_status": "TEXT NOT NULL DEFAULT 'MISSING'",
    "summary_status": "TEXT NOT NULL DEFAULT 'MISSING'",
    "tuk_origin_done": "INTEGER NOT NULL DEFAULT 0",
    "tuk_summary_done": "INTEGER NOT NULL DEFAULT 0",
    "obsidian_done": "INTEGER NOT NULL DEFAULT 0",
    "last_error_code": "TEXT",
    "last_error": "TEXT",
    "last_attempted_at": "TEXT",
    "completed_at": "TEXT",
    "updated_at": "TEXT NOT NULL",
}

TRANSCRIPT_DELIVERY_COLUMN_DEFS = {
    "source_job_id": "INTEGER PRIMARY KEY",
    "source_recording_id": "INTEGER",
    "storage_key": "TEXT",
    "logical_stem": "TEXT NOT NULL",
    "semester": "TEXT",
    "course_code": "TEXT",
    "course_name": "TEXT",
    "course_dir": "TEXT",
    "vault_root_path": "TEXT",
    "semester_root_path": "TEXT",
    "course_root_path": "TEXT",
    "origin_dir_path": "TEXT",
    "source_txt_path": "TEXT NOT NULL",
    "source_txt_sha256": "TEXT NOT NULL",
    "source_json_path": "TEXT NOT NULL",
    "source_json_sha256": "TEXT NOT NULL",
    "destination_txt_path": "TEXT",
    "destination_txt_sha256": "TEXT",
    "destination_json_path": "TEXT",
    "destination_json_sha256": "TEXT",
    "destination_md_path": "TEXT",
    "destination_md_sha256": "TEXT",
    "route_method": "TEXT NOT NULL",
    "active_semester_path": "TEXT",
    "active_semester_sha256": "TEXT",
    "manifest_path": "TEXT",
    "manifest_sha256": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'PENDING'",
    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "error_count": "INTEGER NOT NULL DEFAULT 0",
    "last_error_code": "TEXT",
    "last_error": "TEXT",
    "created_at": "TEXT NOT NULL",
    "last_attempted_at": "TEXT",
    "completed_at": "TEXT",
    "updated_at": "TEXT NOT NULL",
}

TRANSCRIPT_POSTPROCESS_JOB_COLUMN_DEFS = {
    "source_job_id": "INTEGER PRIMARY KEY",
    "storage_key": "TEXT",
    "logical_stem": "TEXT NOT NULL",
    "semester": "TEXT",
    "course_code": "TEXT",
    "course_name": "TEXT",
    "course_dir": "TEXT",
    "vault_root_path": "TEXT",
    "semester_root_path": "TEXT",
    "course_root_path": "TEXT",
    "origin_dir_path": "TEXT",
    "summary_dir_path": "TEXT",
    "active_semester_path": "TEXT",
    "active_semester_sha256": "TEXT",
    "manifest_source_path": "TEXT",
    "manifest_source_sha256": "TEXT",
    "activation_cutoff": "TEXT NOT NULL",
    "route_method": "TEXT NOT NULL",
    "status": "TEXT NOT NULL DEFAULT 'PENDING'",
    "source_txt_path": "TEXT NOT NULL",
    "source_txt_sha256": "TEXT NOT NULL",
    "source_json_path": "TEXT NOT NULL",
    "source_json_sha256": "TEXT NOT NULL",
    "correction_stage_txt_path": "TEXT",
    "correction_stage_txt_sha256": "TEXT",
    "correction_stage_json_path": "TEXT",
    "correction_stage_json_sha256": "TEXT",
    "summary_stage_md_path": "TEXT",
    "summary_stage_md_sha256": "TEXT",
    "destination_txt_path": "TEXT",
    "destination_txt_sha256": "TEXT",
    "destination_json_path": "TEXT",
    "destination_json_sha256": "TEXT",
    "destination_summary_md_path": "TEXT",
    "destination_summary_md_sha256": "TEXT",
    "generator_backend": "TEXT NOT NULL",
    "generator_program": "TEXT",
    "generator_model": "TEXT",
    "generator_reasoning_effort": "TEXT",
    "generator_prompt_version": "TEXT",
    "correction_status": "TEXT NOT NULL DEFAULT 'PENDING'",
    "summary_status": "TEXT NOT NULL DEFAULT 'BLOCKED'",
    "delivery_status": "TEXT NOT NULL DEFAULT 'BLOCKED'",
    "correction_attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "summary_attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "delivery_attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "error_count": "INTEGER NOT NULL DEFAULT 0",
    "last_error_stage": "TEXT",
    "last_error_code": "TEXT",
    "last_error": "TEXT",
    "created_at": "TEXT NOT NULL",
    "last_attempted_at": "TEXT",
    "completed_at": "TEXT",
    "updated_at": "TEXT NOT NULL",
}


# DB 파일 연결과 기본 PRAGMA 설정을 한 번에 수행한다.
def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def connect_db(db_path: str) -> sqlite3.Connection:
    return _connect(db_path)


# 테이블/인덱스를 준비하고 최초 커넥션을 반환한다.
def init_db(db_path: str) -> sqlite3.Connection:
    conn = _connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            orig_inbox_path TEXT NOT NULL,
            orig_name TEXT NOT NULL,
            canonical_base TEXT NOT NULL,
            canonical_audio_path TEXT NOT NULL,
            transcript_txt_path TEXT,
            transcript_json_path TEXT,
            sha256 TEXT,
            error_message TEXT,
            error_trace TEXT,
            started_at TEXT,
            ended_at TEXT,
            preprocess_sec REAL,
            transcribe_sec REAL,
            total_sec REAL,
            engine_params TEXT,
            is_deduped INTEGER DEFAULT 0,
            deduped_from_job_id INTEGER
        )
        """
    )
    _ensure_job_columns(conn, {"current_step", "progress_pct", "eta_sec"})
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_sha256 ON jobs (sha256)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs (created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs (updated_at)")
    init_quality_review_decisions_table(conn)
    init_deliveries_table(conn)
    init_transcript_deliveries_table(conn)
    init_transcript_postprocess_jobs_table(conn)
    conn.commit()
    return conn


def init_quality_review_decisions_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS quality_review_decisions ("
        "job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE, "
        "decision TEXT NOT NULL CHECK (decision IN ('no_issue', 'needs_correction')), "
        "decided_at TEXT NOT NULL)"
    )


def _existing_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def _is_safe_additive_column(column_def: str) -> bool:
    normalized = " ".join(column_def.upper().split())
    if "PRIMARY KEY" in normalized or "UNIQUE" in normalized:
        return False
    return "NOT NULL" not in normalized or "DEFAULT" in normalized


def _quote_pragma_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _has_source_job_conflict_key(
    conn: sqlite3.Connection,
    table_name: str,
) -> bool:
    table_info = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    primary_key_columns = sorted(
        (int(row["pk"]), str(row["name"]))
        for row in table_info
        if int(row["pk"]) > 0
    )
    if primary_key_columns == [(1, "source_job_id")]:
        return True

    indexes = conn.execute(f"PRAGMA index_list({table_name})").fetchall()
    for index in indexes:
        if int(index["unique"]) != 1 or int(index["partial"]) != 0:
            continue
        index_name = _quote_pragma_identifier(str(index["name"]))
        indexed_columns = conn.execute(
            f"PRAGMA index_info({index_name})"
        ).fetchall()
        column_names = [
            str(row["name"])
            for row in sorted(indexed_columns, key=lambda row: int(row["seqno"]))
        ]
        if column_names == ["source_job_id"]:
            return True
    return False


def _ensure_transcript_table_schema(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    create_sql: str,
    column_defs: Dict[str, str],
) -> None:
    """Apply safe additions, rebuilding only an incompatible empty table."""

    existing = _existing_columns(conn, table_name)
    missing = [name for name in column_defs if name not in existing]
    non_additive = [
        name for name in missing if not _is_safe_additive_column(column_defs[name])
    ]
    incompatibilities: list[str] = []
    if non_additive:
        details = ", ".join(
            f"{name} ({column_defs[name]})" for name in non_additive
        )
        incompatibilities.append(f"missing non-additive column(s): {details}")
    if "source_job_id" in existing and not _has_source_job_conflict_key(
        conn,
        table_name,
    ):
        incompatibilities.append(
            "source_job_id lacks a required single-column PRIMARY KEY or "
            "non-partial UNIQUE constraint"
        )

    if incompatibilities:
        has_rows = (
            conn.execute(f"SELECT 1 FROM {table_name} LIMIT 1").fetchone()
            is not None
        )
        if has_rows:
            raise TranscriptSchemaCompatibilityError(
                f"incompatible non-empty schema for {table_name}: "
                f"{'; '.join(incompatibilities)}. Automatic migration was not "
                "attempted because required key constraints and non-additive columns "
                "cannot be introduced safely for existing rows. Back up the database, "
                "then explicitly migrate the existing rows into the canonical schema "
                "or recreate the table after preserving its data."
            )

        # With no rows to preserve, rebuilding avoids SQLite's ADD COLUMN and
        # constraint restrictions while producing the exact canonical schema.
        conn.execute(f"DROP TABLE {table_name}")
        conn.execute(create_sql)
        return

    for column_name in missing:
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} "
            f"{column_defs[column_name]}"
        )


def _ensure_job_columns(conn: sqlite3.Connection, required: set[str]) -> None:
    existing = _existing_columns(conn, "jobs")
    if "current_step" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN current_step TEXT")
    if "progress_pct" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN progress_pct INTEGER DEFAULT 0")
    if "eta_sec" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN eta_sec INTEGER")
    conn.commit()


def init_deliveries_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS deliveries (
            logical_stem TEXT PRIMARY KEY,
            source_job_id INTEGER,
            subject_abbr TEXT NOT NULL DEFAULT 'UNKNOWN',
            correction_txt_path TEXT,
            correction_json_path TEXT,
            summary_md_path TEXT,
            correction_txt_sha256 TEXT,
            correction_json_sha256 TEXT,
            summary_md_sha256 TEXT,
            tuk_origin_txt_path TEXT,
            tuk_origin_json_path TEXT,
            tuk_summary_path TEXT,
            obsidian_summary_path TEXT,
            correction_status TEXT NOT NULL DEFAULT 'MISSING',
            summary_status TEXT NOT NULL DEFAULT 'MISSING',
            tuk_origin_done INTEGER NOT NULL DEFAULT 0,
            tuk_summary_done INTEGER NOT NULL DEFAULT 0,
            obsidian_done INTEGER NOT NULL DEFAULT 0,
            last_error_code TEXT,
            last_error TEXT,
            last_attempted_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    existing = _existing_columns(conn, "deliveries")
    for column_name, column_def in DELIVERY_COLUMN_DEFS.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE deliveries ADD COLUMN {column_name} {column_def}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_subject ON deliveries (subject_abbr)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_updated_at ON deliveries (updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_correction_status ON deliveries (correction_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_summary_status ON deliveries (summary_status)")
    conn.commit()


def init_transcript_deliveries_table(conn: sqlite3.Connection) -> None:
    create_sql = """
        CREATE TABLE IF NOT EXISTS transcript_deliveries (
            source_job_id INTEGER PRIMARY KEY,
            source_recording_id INTEGER,
            storage_key TEXT,
            logical_stem TEXT NOT NULL,
            semester TEXT,
            course_code TEXT,
            course_name TEXT,
            course_dir TEXT,
            vault_root_path TEXT,
            semester_root_path TEXT,
            course_root_path TEXT,
            origin_dir_path TEXT,
            source_txt_path TEXT NOT NULL,
            source_txt_sha256 TEXT NOT NULL,
            source_json_path TEXT NOT NULL,
            source_json_sha256 TEXT NOT NULL,
            destination_txt_path TEXT,
            destination_txt_sha256 TEXT,
            destination_json_path TEXT,
            destination_json_sha256 TEXT,
            destination_md_path TEXT,
            destination_md_sha256 TEXT,
            route_method TEXT NOT NULL,
            active_semester_path TEXT,
            active_semester_sha256 TEXT,
            manifest_path TEXT,
            manifest_sha256 TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            last_error_code TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            last_attempted_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    conn.execute(create_sql)
    _ensure_transcript_table_schema(
        conn,
        table_name="transcript_deliveries",
        create_sql=create_sql,
        column_defs=TRANSCRIPT_DELIVERY_COLUMN_DEFS,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_deliveries_status "
        "ON transcript_deliveries (status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_deliveries_storage_key "
        "ON transcript_deliveries (storage_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_deliveries_updated_at "
        "ON transcript_deliveries (updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_deliveries_completed_at "
        "ON transcript_deliveries (completed_at)"
    )
    conn.commit()


def init_transcript_postprocess_jobs_table(conn: sqlite3.Connection) -> None:
    create_sql = """
        CREATE TABLE IF NOT EXISTS transcript_postprocess_jobs (
            source_job_id INTEGER PRIMARY KEY,
            storage_key TEXT,
            logical_stem TEXT NOT NULL,
            semester TEXT,
            course_code TEXT,
            course_name TEXT,
            course_dir TEXT,
            vault_root_path TEXT,
            semester_root_path TEXT,
            course_root_path TEXT,
            origin_dir_path TEXT,
            summary_dir_path TEXT,
            active_semester_path TEXT,
            active_semester_sha256 TEXT,
            manifest_source_path TEXT,
            manifest_source_sha256 TEXT,
            activation_cutoff TEXT NOT NULL,
            route_method TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            source_txt_path TEXT NOT NULL,
            source_txt_sha256 TEXT NOT NULL,
            source_json_path TEXT NOT NULL,
            source_json_sha256 TEXT NOT NULL,
            correction_stage_txt_path TEXT,
            correction_stage_txt_sha256 TEXT,
            correction_stage_json_path TEXT,
            correction_stage_json_sha256 TEXT,
            summary_stage_md_path TEXT,
            summary_stage_md_sha256 TEXT,
            destination_txt_path TEXT,
            destination_txt_sha256 TEXT,
            destination_json_path TEXT,
            destination_json_sha256 TEXT,
            destination_summary_md_path TEXT,
            destination_summary_md_sha256 TEXT,
            generator_backend TEXT NOT NULL,
            generator_program TEXT,
            generator_model TEXT,
            generator_reasoning_effort TEXT,
            generator_prompt_version TEXT,
            correction_status TEXT NOT NULL DEFAULT 'PENDING',
            summary_status TEXT NOT NULL DEFAULT 'BLOCKED',
            delivery_status TEXT NOT NULL DEFAULT 'BLOCKED',
            correction_attempt_count INTEGER NOT NULL DEFAULT 0,
            summary_attempt_count INTEGER NOT NULL DEFAULT 0,
            delivery_attempt_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            last_error_stage TEXT,
            last_error_code TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            last_attempted_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    conn.execute(create_sql)
    _ensure_transcript_table_schema(
        conn,
        table_name="transcript_postprocess_jobs",
        create_sql=create_sql,
        column_defs=TRANSCRIPT_POSTPROCESS_JOB_COLUMN_DEFS,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_postprocess_jobs_status "
        "ON transcript_postprocess_jobs (status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_postprocess_jobs_stage_status "
        "ON transcript_postprocess_jobs (correction_status, summary_status, delivery_status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_postprocess_jobs_updated_at "
        "ON transcript_postprocess_jobs (updated_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_postprocess_jobs_completed_at "
        "ON transcript_postprocess_jobs (completed_at)"
    )
    conn.commit()


def _now() -> str:
    return utils.now_iso()


# Path 타입 값을 문자열로 바꿔 SQLite 바인딩 호환성을 확보한다.
def _clean_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = {}
    for key, value in payload.items():
        if isinstance(value, Path):
            value = str(value)
        result[key] = value
    return result


def get_delivery(conn: sqlite3.Connection, logical_stem: str) -> Optional[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT * FROM deliveries WHERE logical_stem = ?",
            (logical_stem,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def upsert_delivery(conn: sqlite3.Connection, logical_stem: str, **fields: Any) -> None:
    payload = _clean_payload(fields)
    payload["logical_stem"] = logical_stem
    payload["updated_at"] = _now()

    columns = ", ".join(payload.keys())
    placeholders = ", ".join([":" + key for key in payload.keys()])
    update_clause = ", ".join(
        [f"{key} = excluded.{key}" for key in payload.keys() if key != "logical_stem"]
    )
    conn.execute(
        f"INSERT INTO deliveries ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(logical_stem) DO UPDATE SET {update_clause}",
        payload,
    )
    conn.commit()


def get_transcript_delivery(
    conn: sqlite3.Connection,
    source_job_id: int,
) -> Optional[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT * FROM transcript_deliveries WHERE source_job_id = ?",
            (source_job_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def upsert_transcript_delivery(
    conn: sqlite3.Connection,
    source_job_id: int,
    **fields: Any,
) -> None:
    payload = _clean_payload(fields)
    payload["updated_at"] = _now()
    payload["source_job_id"] = source_job_id
    payload.setdefault("created_at", payload["updated_at"])
    columns = ", ".join(payload.keys())
    placeholders = ", ".join([":" + key for key in payload.keys()])
    update_clause = ", ".join(
        f"{key} = excluded.{key}"
        for key in payload
        if key not in {"source_job_id", "created_at"}
    )
    conn.execute(
        f"INSERT INTO transcript_deliveries ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(source_job_id) DO UPDATE SET {update_clause}",
        payload,
    )
    conn.commit()


def list_transcript_deliveries_by_status(
    conn: sqlite3.Connection,
    status: str,
) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT * FROM transcript_deliveries WHERE status = ? "
            "ORDER BY updated_at ASC, source_job_id ASC",
            (status,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def get_transcript_postprocess_job(
    conn: sqlite3.Connection,
    source_job_id: int,
) -> Optional[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT * FROM transcript_postprocess_jobs WHERE source_job_id = ?",
            (source_job_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def insert_transcript_postprocess_job_if_absent(
    conn: sqlite3.Connection,
    source_job_id: int,
    **fields: Any,
) -> bool:
    payload = _clean_payload(fields)
    payload["source_job_id"] = source_job_id
    payload.setdefault("created_at", _now())
    payload.setdefault("updated_at", payload["created_at"])
    columns = ", ".join(payload.keys())
    placeholders = ", ".join([":" + key for key in payload.keys()])
    cursor = conn.execute(
        f"INSERT INTO transcript_postprocess_jobs ({columns}) VALUES ({placeholders}) "
        "ON CONFLICT(source_job_id) DO NOTHING",
        payload,
    )
    conn.commit()
    return cursor.rowcount == 1


def update_transcript_postprocess_job(
    conn: sqlite3.Connection,
    source_job_id: int,
    **fields: Any,
) -> None:
    if not fields:
        return
    payload = _clean_payload(fields)
    payload["source_job_id"] = source_job_id
    payload["updated_at"] = _now()
    assignments = ", ".join(f"{key} = :{key}" for key in payload if key != "source_job_id")
    conn.execute(
        f"UPDATE transcript_postprocess_jobs SET {assignments} "
        "WHERE source_job_id = :source_job_id",
        payload,
    )
    conn.commit()


def list_transcript_postprocess_jobs(
    conn: sqlite3.Connection,
    *,
    statuses: tuple[str, ...] | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    try:
        params: list[Any] = []
        where = ""
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where = f"WHERE status IN ({placeholders})"
            params.extend(statuses)
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(max(0, int(limit)))
        return conn.execute(
            "SELECT * FROM transcript_postprocess_jobs "
            f"{where} ORDER BY updated_at ASC, source_job_id ASC{limit_clause}",
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def find_latest_job_by_canonical_base(conn: sqlite3.Connection, canonical_base: str) -> Optional[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT * FROM jobs WHERE canonical_base = ? ORDER BY id DESC LIMIT 1",
            (canonical_base,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def create_job(
    conn: sqlite3.Connection,
    *,
    status: str,
    orig_inbox_path: str,
    orig_name: str,
    canonical_base: str,
    canonical_audio_path: str,
    transcript_txt_path: str,
    transcript_json_path: str,
    engine_params: Optional[Dict[str, Any]] = None,
    current_step: str | None = None,
    progress_pct: int | None = None,
    eta_sec: int | None = None,
) -> int:
    payload = _clean_payload({
        "status": status,
        "created_at": _now(),
        "updated_at": _now(),
        "orig_inbox_path": orig_inbox_path,
        "orig_name": orig_name,
        "canonical_base": canonical_base,
        "canonical_audio_path": canonical_audio_path,
        "transcript_txt_path": transcript_txt_path,
        "transcript_json_path": transcript_json_path,
        "engine_params": json.dumps(engine_params or {}, ensure_ascii=False),
    })
    if current_step is not None:
        payload["current_step"] = current_step
    if progress_pct is not None:
        payload["progress_pct"] = progress_pct
    if eta_sec is not None:
        payload["eta_sec"] = eta_sec
    columns = ", ".join(payload.keys())
    placeholders = ", ".join([":" + key for key in payload.keys()])
    conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", payload)
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def get_job(conn: sqlite3.Connection, job_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


# 컬럼 부분 갱신(타임스탬프 자동 갱신 포함)용 공용 함수.
def update_job(conn: sqlite3.Connection, job_id: int, **fields: Any) -> None:
    if not fields:
        return

    update = ", ".join([f"{key} = :{key}" for key in fields.keys()])
    values = _clean_payload(fields)
    values["id"] = job_id
    values["updated_at"] = _now()
    conn.execute(f"UPDATE jobs SET {update}, updated_at = :updated_at WHERE id = :id", values)
    conn.commit()


def delete_job(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()


def claim_job_for_processing(conn: sqlite3.Connection, job_id: int) -> bool:
    now = _now()
    cur = conn.execute(
        "UPDATE jobs "
        "SET status = :processing, started_at = :started_at, updated_at = :updated_at "
        "WHERE id = :job_id AND status = :pending",
        {
            "job_id": job_id,
            "pending": STATUS_PENDING,
            "processing": STATUS_PROCESSING,
            "started_at": now,
            "updated_at": now,
        },
    )
    conn.commit()
    return cur.rowcount == 1


def claim_retry_job_for_processing(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    expected_updated_at: str,
    expected_current_step: str,
    expected_orig_name: str,
    expected_canonical_base: str,
    expected_canonical_audio_path: str,
    expected_transcript_txt_path: str,
    expected_transcript_json_path: str,
    expected_engine_params: str,
) -> bool:
    """Atomically fence an exact planned retry row before taking ownership."""
    now = _now()
    cur = conn.execute(
        "UPDATE jobs "
        "SET status = :processing, started_at = :started_at, updated_at = :updated_at "
        "WHERE id = :job_id "
        "AND status = :pending "
        "AND updated_at = :expected_updated_at "
        "AND current_step = :expected_current_step "
        "AND orig_name = :expected_orig_name "
        "AND canonical_base = :expected_canonical_base "
        "AND canonical_audio_path = :expected_canonical_audio_path "
        "AND transcript_txt_path = :expected_transcript_txt_path "
        "AND transcript_json_path = :expected_transcript_json_path "
        "AND engine_params = :expected_engine_params",
        {
            "job_id": job_id,
            "pending": STATUS_PENDING,
            "processing": STATUS_PROCESSING,
            "started_at": now,
            "updated_at": now,
            "expected_updated_at": expected_updated_at,
            "expected_current_step": expected_current_step,
            "expected_orig_name": expected_orig_name,
            "expected_canonical_base": expected_canonical_base,
            "expected_canonical_audio_path": expected_canonical_audio_path,
            "expected_transcript_txt_path": expected_transcript_txt_path,
            "expected_transcript_json_path": expected_transcript_json_path,
            "expected_engine_params": expected_engine_params,
        },
    )
    conn.commit()
    return cur.rowcount == 1


def set_status(conn: sqlite3.Connection, job_id: int, status: str, **extra: Any) -> None:
    update_job(conn, job_id, status=status, **extra)


# 동일 sha256의 재사용 가능한 완료 산출물을 최신순으로 찾아 중복 전송/재처리에 활용한다.
def find_replayable_job_by_sha(conn: sqlite3.Connection, sha256: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE sha256 = ? AND status IN (?, ?) ORDER BY created_at DESC LIMIT 1",
        (sha256, STATUS_DONE, STATUS_NEEDS_REVIEW),
    ).fetchone()


def find_done_job_by_sha(conn: sqlite3.Connection, sha256: str) -> Optional[sqlite3.Row]:
    """Backward-compatible alias for callers that still use the old name."""
    return find_replayable_job_by_sha(conn, sha256)


# 현재 처리 중인 작업 목록을 조회한다.
def list_processing_jobs(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM jobs WHERE status = ? ORDER BY id ASC", (STATUS_PROCESSING,)).fetchall()


def get_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {"PENDING": 0, "PROCESSING": 0, "DONE": 0, "NEEDS_REVIEW": 0, "ERROR": 0}
    rows = conn.execute("SELECT status, count(*) AS cnt FROM jobs GROUP BY status").fetchall()
    for row in rows:
        status = str(row["status"])
        if status in counts:
            counts[status] = int(row["cnt"])
    return counts


# txt/json 결과가 둘 다 존재하고 세그먼트 배열이 있으면 완료된 결과로 본다.
def _has_complete_transcripts(txt_path: str | None, json_path: str | None) -> bool:
    if not txt_path or not json_path:
        return False

    txt_file = Path(txt_path)
    json_file = Path(json_path)
    if not txt_file.is_file() or not json_file.is_file():
        return False

    try:
        # Pending claims only need a readability check, including trailing bytes.
        with txt_file.open(encoding="utf-8") as handle:
            while handle.read(64 * 1024):
                pass
        with json_file.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False

    if not isinstance(payload, dict):
        return False
    segments = payload.get("segments")
    return isinstance(segments, list)


def _is_stale_processing(updated_at_raw: str | None, now: datetime, stale_seconds: int) -> bool:
    # 업데이트 시각 기준 stale 여부를 판정해 복구 정책에 반영한다.
    if not updated_at_raw:
        return True
    try:
        updated_at = datetime.fromisoformat(updated_at_raw)
        return (now - updated_at).total_seconds() >= stale_seconds
    except Exception:
        return True


def _retry_step_from_engine_params(engine_params: str | None) -> str | None:
    if not engine_params:
        return None
    try:
        payload = json.loads(engine_params)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or "transcription_failures" not in payload:
        return None
    try:
        failures = int(payload.get("transcription_failures", 0) or 0)
        max_retries = int(payload.get("transcription_max_retries", failures) or failures)
    except (TypeError, ValueError):
        return None
    if failures <= 0 or max_retries < failures:
        return None
    return f"전사 재시도 대기 {failures}/{max_retries}"


def _validate_recovered_quality_scorecard(
    json_path: str,
    metadata: Dict[str, Any],
) -> str | None:
    quality = metadata.get("quality")
    if not isinstance(quality, dict):
        return None
    try:
        quality_gate.validate_quality_scorecard(json_path, metadata)
    except FileNotFoundError:
        # Transcript JSON is written before its metadata-only scorecard. A crash
        # in that narrow window is safe to repair deterministically from the
        # persisted metadata; rebuilding also keeps downstream quality gates
        # from mistaking this result for a pre-scorecard legacy transcript.
        try:
            quality_gate.write_quality_scorecard(json_path, metadata)
            quality_gate.validate_quality_scorecard(json_path, metadata)
        except (OSError, TypeError, ValueError) as exc:
            return f"복구된 전사 결과의 quality scorecard 재생성 실패: {exc}"
    except ValueError as exc:
        return f"복구된 전사 결과의 quality scorecard 검증 실패: {exc}"
    except OSError as exc:
        return f"복구된 전사 결과의 quality scorecard 접근 실패: {exc}"
    return None


def _has_pinned_transcript(conn: sqlite3.Connection, row: sqlite3.Row, digest: str) -> bool:
    """Check immutable delivery inputs and shared historical job paths before repair."""
    path = row["transcript_json_path"]
    return conn.execute(
        """SELECT 1 FROM transcript_postprocess_jobs WHERE source_json_path=? OR source_json_sha256=?
           UNION ALL SELECT 1 FROM transcript_deliveries WHERE source_json_path=? OR source_json_sha256=?
           UNION ALL SELECT 1 FROM deliveries WHERE correction_json_path=? OR correction_json_sha256=?
           UNION ALL SELECT 1 FROM jobs WHERE id!=? AND (transcript_json_path=? OR canonical_base=?)
           LIMIT 1""",
        (path, digest, path, digest, path, digest, row["id"], path, row["canonical_base"]),
    ).fetchone() is not None


def _publish_review_copy(
    job_id: int, txt: Path, source: Path, payload: dict, metadata: dict
) -> tuple[Path, Path]:
    """Publish a complete alternate pair before changing the job's DB paths."""
    directory = source.parent / f"{RECOVERY_PAIR_PREFIX}{job_id}"
    target_txt, target_json = directory / txt.name, directory / source.name
    metadata.update(transcript_txt_path=str(target_txt), transcript_json_path=str(target_json))
    card = quality_gate.build_quality_scorecard(metadata, transcript_json_path=target_json)
    files = {
        target_txt.name: txt.read_bytes(),
        target_json.name: json.dumps(payload, ensure_ascii=False, indent=2).encode(),
        quality_gate.quality_scorecard_path(target_json).name: json.dumps(card, ensure_ascii=False, indent=2).encode(),
    }
    if not os.path.lexists(directory):
        with tempfile.TemporaryDirectory(dir=source.parent, prefix=RECOVERY_STAGING_PREFIX) as temporary:
            staged = Path(temporary) / "pair"
            staged.mkdir(mode=0o700)
            for name, data in files.items():
                utils.atomic_write(staged / name, data)
            utils._rename_no_replace(staged, directory)
    # A restart can encounter the already published pair before DB commit.
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("복구 결과 경로 충돌")
    for name, data in files.items():
        file = directory / name
        if file.is_symlink() or not file.is_file() or file.read_bytes() != data:
            raise ValueError("기존 복구 결과가 현재 판정과 다릅니다")
    return target_txt, target_json


def _repair_empty_transcript(
    conn: sqlite3.Connection, row: sqlite3.Row, payload: dict, serialized: bytes, text: str
) -> dict:
    txt, path = Path(row["transcript_txt_path"]), Path(row["transcript_json_path"])
    previous_metadata = payload.get("metadata")
    metadata = dict(previous_metadata) if isinstance(previous_metadata, dict) else {}
    payload["metadata"] = metadata
    previous = metadata.get("quality")
    duration = previous.get("audio_duration_sec") if isinstance(previous, dict) else None
    segments = [
        {**item, "text": item.get("text") or ""}
        for item in payload["segments"] if isinstance(item, dict)
    ]
    quality = quality_gate.evaluate(segments, text, audio_duration_sec=duration).to_dict()
    engine = json.loads(row["engine_params"] or "{}")
    if not isinstance(engine, dict):
        raise ValueError("작업 메타데이터 형식 오류")
    metadata.setdefault("canonical_base", row["canonical_base"])
    metadata.setdefault("orig_name", row["orig_name"])
    metadata["quality"] = quality

    expected_card = quality_gate.build_quality_scorecard(metadata, transcript_json_path=path)
    try:
        card_matches = json.loads(quality_gate.quality_scorecard_path(path).read_bytes()) == expected_card
    except (FileNotFoundError, ValueError):
        card_matches = False
    if metadata != previous_metadata or not card_matches:
        if path.read_bytes() != serialized or txt.read_text(encoding="utf-8") != text:
            raise OSError("복구 중 원전사 파일이 변경되었습니다.")
        if _has_pinned_transcript(conn, row, hashlib.sha256(serialized).hexdigest()):
            txt, path = _publish_review_copy(row["id"], txt, path, payload, metadata)
        else:
            # Publish the derived sidecar first. If interrupted, the PROCESSING
            # row still points to the original JSON and the repair is repeatable.
            if not card_matches:
                quality_gate.write_quality_scorecard(path, metadata)
            if metadata != previous_metadata:
                utils.atomic_write(path, payload)
        quality_gate.validate_quality_scorecard(path, metadata)
    engine.update(quality=quality, transcript_txt_path=str(txt), transcript_json_path=str(path))
    return {
        "engine_params": json.dumps(engine, ensure_ascii=False),
        "transcript_txt_path": str(txt),
        "transcript_json_path": str(path),
    }


def _completed_transcript_quality(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> tuple[str | None, str | None, str | None, dict] | None:
    """Read the pair once. A new incomplete pair is eligible for audio recovery."""
    json_path, txt_path = row["transcript_json_path"], row["transcript_txt_path"]
    deferred = row["current_step"] == QUALITY_REPAIR_WAIT
    try:
        if not deferred and (
            not txt_path or not json_path or not Path(txt_path).is_file() or not Path(json_path).is_file()
        ):
            return None
        serialized = Path(json_path).read_bytes()
        # Initial admission requires UTF-8, including in the tail of the TXT.
        payload = json.loads(serialized if deferred else serialized.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("복구된 전사 JSON 형식 확인 필요")
        text = Path(txt_path).read_text(encoding="utf-8")
        if not deferred and not isinstance(payload.get("segments"), list):
            return None
    except OSError:
        if deferred:
            raise
        return None
    except (TypeError, ValueError) as exc:
        if deferred:
            return None, None, f"복구된 전사 산출물 검증 실패: {exc}", {}
        return None

    try:
        segments = payload["segments"]
        if not text.strip() or not any(
            isinstance(segment, dict) and str(segment.get("text") or "").strip()
            for segment in segments
        ):
            fields = _repair_empty_transcript(conn, row, payload, serialized, text)
            return "bad", payload["metadata"]["quality"]["summary"], None, fields
    except (KeyError, TypeError, ValueError) as exc:
        return None, None, f"복구된 전사 산출물 검증 실패: {exc}", {}
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None, None, None, {}
    scorecard_error = _validate_recovered_quality_scorecard(json_path, metadata)
    hold = quality_gate.inherited_review_reason(metadata)
    if hold:
        return "held", hold, scorecard_error, {}
    quality = metadata.get("quality")
    if not isinstance(quality, dict):
        return None, None, scorecard_error, {}
    summary = quality.get("summary")
    normalized_summary = summary.strip() if isinstance(summary, str) and summary.strip() else None
    try:
        prior = json.loads(row["engine_params"] or "{}")
    except (TypeError, ValueError):
        prior = {}
    # Recover the metadata snapshot written with the pair. The completed-job
    # review queue reads this exact quality evidence from the jobs ledger.
    if not isinstance(prior, dict):
        prior = {}
    fields = {"engine_params": json.dumps({**prior, **metadata}, ensure_ascii=False)}
    health = "bad" if quality_gate.requires_blocking_review(quality) else None
    return health, normalized_summary, scorecard_error, fields


def recover_processing_jobs(
    conn: sqlite3.Connection, stale_processing_hours: int = 6, *, deferred_only: bool = False
) -> Dict[str, int]:
    # 시작 시 끊긴 PROCESSING 작업을 정상 종료/재시도/오류로 복구한다.
    now = datetime.now().astimezone()
    stale_seconds = stale_processing_hours * 3600
    counts = {"done": 0, "needs_review": 0, "pending": 0, "error": 0}

    if deferred_only:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status=? AND current_step=? ORDER BY updated_at, id LIMIT 1",
            (STATUS_PROCESSING, QUALITY_REPAIR_WAIT),
        ).fetchall()
    else:
        rows = list_processing_jobs(conn)
    for row in rows:
        job_id = row["id"]
        audio = row["canonical_audio_path"]
        txt = row["transcript_txt_path"]
        json_path = row["transcript_json_path"]
        updated_at_raw = row["updated_at"]
        is_stale = _is_stale_processing(updated_at_raw, now, stale_seconds)

        # 시작 시 PROCESSING 작업을 복구해 중단된 실행이 계속 걸리지 않게 한다.
        # 오래된 상태인지는 메타 데이터로만 기록해 추적한다.
        # A deferred repair already saw a complete pair. An unavailable file
        # on its next read should wait; re-admitting its audio would lose that claim.
        try:
            verdict = _completed_transcript_quality(conn, row)
        except OSError as exc:
            # Keep the recovery claim retryable when iCloud or the disk is unavailable.
            update_job(conn, job_id, current_step=QUALITY_REPAIR_WAIT, error_message=str(exc))
            continue
        if verdict is not None:
            quality_health, quality_summary, scorecard_error, fields = verdict
            if scorecard_error:
                set_status(
                    conn,
                    job_id,
                    STATUS_NEEDS_REVIEW,
                    ended_at=utils.now_iso(),
                    error_message=scorecard_error,
                    error_trace=None,
                    current_step="품질 산출물 검증 필요",
                    progress_pct=100,
                    eta_sec=0,
                )
                counts["needs_review"] += 1
                continue
            if quality_health in {"bad", "held"}:
                set_status(
                    conn,
                    job_id,
                    STATUS_NEEDS_REVIEW,
                    ended_at=utils.now_iso(),
                    error_message=quality_summary or "복구된 전사 결과의 품질 검토가 필요합니다.",
                    error_trace=None,
                    current_step="품질 검토 필요",
                    progress_pct=100,
                    eta_sec=0,
                    **fields,
                )
                counts["needs_review"] += 1
                continue
            set_status(
                conn,
                job_id,
                STATUS_DONE,
                ended_at=utils.now_iso(),
                error_message=None,
                error_trace=None,
                **fields,
            )
            counts["done"] += 1
            continue

        if Path(audio).exists():
            update_fields: dict[str, Any] = {
                "status": STATUS_PENDING,
                "started_at": None,
                "ended_at": None,
                "preprocess_sec": None,
                "transcribe_sec": None,
                "total_sec": None,
                "is_deduped": 0,
                "deduped_from_job_id": None,
            }
            retry_step = _retry_step_from_engine_params(row["engine_params"])
            if retry_step:
                update_fields.update(
                    current_step=retry_step,
                    progress_pct=18,
                    eta_sec=None,
                )
            else:
                update_fields.update(
                    error_message=None,
                    error_trace=None,
                )
            update_job(conn, job_id, **update_fields)
            counts["pending"] += 1
            continue

        msg = "Recovered from interrupted PROCESSING job: missing canonical audio and transcripts"
        if is_stale:
            msg = f"{msg} (stale > {stale_processing_hours}h)"
        set_status(
            conn,
            job_id,
            STATUS_ERROR,
            ended_at=utils.now_iso(),
            error_message=msg,
            error_trace=f"Recovery check at startup, stale={is_stale}",
        )
        counts["error"] += 1

    conn.commit()
    return counts
