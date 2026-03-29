from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from lecture_stt.shared import utils


STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_DONE = "DONE"
STATUS_ERROR = "ERROR"

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
    init_deliveries_table(conn)
    conn.commit()
    return conn


def _existing_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


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


def set_status(conn: sqlite3.Connection, job_id: int, status: str, **extra: Any) -> None:
    update_job(conn, job_id, status=status, **extra)


# 동일 sha256 완료 작업을 최신순으로 찾아 중복 전송/재처리에 활용한다.
def find_done_job_by_sha(conn: sqlite3.Connection, sha256: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE sha256 = ? AND status = ? ORDER BY created_at DESC LIMIT 1",
        (sha256, STATUS_DONE),
    ).fetchone()


# 현재 처리 중인 작업 목록을 조회한다.
def list_processing_jobs(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM jobs WHERE status = ? ORDER BY id ASC", (STATUS_PROCESSING,)).fetchall()


def get_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {"PENDING": 0, "PROCESSING": 0, "DONE": 0, "ERROR": 0}
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
    # 업데이트 시각 기준 stale 여부를 판정해 복구 정책에 반영한다.
    if not updated_at_raw:
        return True
    try:
        updated_at = datetime.fromisoformat(updated_at_raw)
        return (now - updated_at).total_seconds() >= stale_seconds
    except Exception:
        return True


def recover_processing_jobs(conn: sqlite3.Connection, stale_processing_hours: int = 6) -> Dict[str, int]:
    # 시작 시 끊긴 PROCESSING 작업을 정상 종료/재시도/오류로 복구한다.
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

        # 시작 시 PROCESSING 작업을 복구해 중단된 실행이 계속 걸리지 않게 한다.
        # 오래된 상태인지는 메타 데이터로만 기록해 추적한다.
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
