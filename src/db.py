from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import utils as utils


STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_DONE = "DONE"
STATUS_ERROR = "ERROR"


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_sha256 ON jobs (sha256)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs (created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs (updated_at)")
    conn.commit()
    return conn


def _now() -> str:
    return utils.now_iso()


def _clean_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = {}
    for key, value in payload.items():
        if isinstance(value, Path):
            value = str(value)
        result[key] = value
    return result


def create_job(conn: sqlite3.Connection, *, status: str, orig_inbox_path: str, orig_name: str, canonical_base: str,
               canonical_audio_path: str, transcript_txt_path: str, transcript_json_path: str,
               engine_params: Optional[Dict[str, Any]] = None) -> int:
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
    columns = ", ".join(payload.keys())
    placeholders = ", ".join([":" + key for key in payload.keys()])
    conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", payload)
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def get_job(conn: sqlite3.Connection, job_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def update_job(conn: sqlite3.Connection, job_id: int, **fields: Any) -> None:
    if not fields:
        return

    update = ", ".join([f"{key} = :{key}" for key in fields.keys()])
    values = _clean_payload(fields)
    values["id"] = job_id
    values["updated_at"] = _now()
    conn.execute(f"UPDATE jobs SET {update}, updated_at = :updated_at WHERE id = :id", values)
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


def set_status(conn: sqlite3.Connection, job_id: int, status: str, **extra: Any) -> None:
    update_job(conn, job_id, status=status, **extra)


def find_done_job_by_sha(conn: sqlite3.Connection, sha256: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE sha256 = ? AND status = ? ORDER BY created_at DESC LIMIT 1",
        (sha256, STATUS_DONE),
    ).fetchone()


def list_processing_jobs(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM jobs WHERE status = ? ORDER BY id ASC", (STATUS_PROCESSING,)).fetchall()


def _has_complete_transcripts(txt_path: str | None, json_path: str | None) -> bool:
    if not txt_path or not json_path:
        return False

    txt_file = Path(txt_path)
    json_file = Path(json_path)
    if not txt_file.exists() or not json_file.exists():
        return False

    try:
        with json_file.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return False

    if not isinstance(payload, dict):
        return False
    segments = payload.get("segments")
    return isinstance(segments, list)


def _is_stale_processing(updated_at_raw: str | None, now: datetime, stale_seconds: int) -> bool:
    if not updated_at_raw:
        return True
    try:
        updated_at = datetime.fromisoformat(updated_at_raw)
        return (now - updated_at).total_seconds() >= stale_seconds
    except Exception:
        return True


def recover_processing_jobs(conn: sqlite3.Connection, stale_processing_hours: int = 6) -> Dict[str, int]:
    now = datetime.now().astimezone()
    stale_seconds = stale_processing_hours * 3600
    counts = {"done": 0, "pending": 0, "error": 0}

    rows = list_processing_jobs(conn)
    for row in rows:
        job_id = row["id"]
        audio = row["canonical_audio_path"]
        txt = row["transcript_txt_path"]
        json_path = row["transcript_json_path"]
        updated_at_raw = row["updated_at"]
        is_stale = _is_stale_processing(updated_at_raw, now, stale_seconds)

        # Always recover processing jobs at startup so interrupted runs cannot be stuck.
        # Keep stale check only in logs/metadata for observability.
        transcript_ready = _has_complete_transcripts(txt, json_path)
        if transcript_ready:
            set_status(
                conn,
                job_id,
                STATUS_DONE,
                ended_at=utils.now_iso(),
                error_message=None,
                error_trace=None,
            )
            counts["done"] += 1
            continue

        if Path(audio).exists():
            update_job(
                conn,
                job_id,
                status=STATUS_PENDING,
                started_at=None,
                ended_at=None,
                preprocess_sec=None,
                transcribe_sec=None,
                total_sec=None,
                error_message=None,
                error_trace=None,
                is_deduped=0,
                deduped_from_job_id=None,
            )
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

