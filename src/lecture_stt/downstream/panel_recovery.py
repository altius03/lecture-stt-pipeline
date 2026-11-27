"""Read the missing-course queue and confirm one operator-selected recording."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any

from lecture_stt.downstream import transcript_delivery as delivery
from lecture_stt.downstream.semester import load_active_semester_snapshot
from lecture_stt.downstream.worker import _load_config_mapping, _parse_bool, load_transcript_worker_config
from lecture_stt.shared.paths import resolve_config_path


class RecoveryDisabledError(RuntimeError):
    pass


class RecoveryNotFoundError(RuntimeError):
    pass


class RecoveryConflictError(RuntimeError):
    pass


_MISSING = "q.status IN ('SKIPPED', 'UNROUTED')"
_CONFIRMED = "q.route_method = 'operator_confirmed'"
_VIEWS = {"missing": _MISSING, "confirmed": _CONFIRMED}
_JOIN = "FROM transcript_postprocess_jobs AS q JOIN jobs AS j ON j.id=q.source_job_id"
_COLUMNS = """
    q.source_job_id, j.orig_name, j.engine_params,
    q.status, q.course_code, q.last_error,
    q.correction_status, q.summary_status, q.delivery_status
"""
_ROUTING_REASONS = {
    "no_time_match": "녹음 시각과 일치하는 수업이 없습니다.",
    "ambiguous_time_match": "녹음 시각에 여러 과목이 겹칩니다.",
    "recorded_at_missing": "녹음 시각을 확인할 수 없습니다.",
}


def _read_settings(config_path: Path, db_path: Path) -> Path | None:
    # Reads only routing settings: polling completed jobs does not need a generator or output folders.
    config, env = _load_config_mapping(str(config_path))
    section = config["transcript_delivery"]
    if not _parse_bool(section["enabled"], name="transcript_delivery.enabled"):
        return None
    if resolve_config_path(config["paths"]["db_path"], env=env).resolve() != db_path.resolve():
        raise RuntimeError("작업 DB 설정이 변경되었습니다.")
    return resolve_config_path(section["active_semester_manifest"], env=env)


def _connect(path: Path, *, writable: bool = False) -> sqlite3.Connection:
    mode = "rw" if writable else "ro"
    conn = sqlite3.connect(f"{path.absolute().as_uri()}?mode={mode}", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


def _job(row: sqlite3.Row) -> dict[str, Any]:
    engine = delivery._safe_engine_params(row["engine_params"])
    recorded = delivery._iso_or_none(engine.get("recorded_at"))
    if recorded is None and isinstance(engine.get("metadata"), dict):
        recorded = delivery._iso_or_none(engine["metadata"].get("recorded_at"))
    if row["status"] == "SKIPPED":
        reason = "강의로 분류되지 않았습니다."
    elif row["status"] == "UNROUTED":
        reason = _ROUTING_REASONS.get(row["last_error"], "과목을 결정할 수 없습니다.")
    else:
        reason = "운영자가 과목을 확정했습니다."
    return {
        "id": row["source_job_id"],
        "title": Path(str(row["orig_name"]).replace("\\", "/")).name,
        "recorded_at": recorded,
        "reason": reason,
        "status": row["status"],
        "course_code": row["course_code"],
        "correction_status": row["correction_status"],
        "summary_status": row["summary_status"],
        "delivery_status": row["delivery_status"],
    }


def list_recovery(config_path: Path, db_path: Path, *, view: str, offset: int, limit: int) -> dict:
    if view not in _VIEWS or not 1 <= limit <= 100 or not 0 <= offset <= 100_000:
        raise ValueError("잘못된 복구 목록 범위입니다.")
    if _read_settings(config_path, db_path) is None:
        return {"enabled": False, "total": 0, "items": []}
    where = _VIEWS[view]
    with closing(_connect(db_path)) as conn:
        conn.execute("BEGIN")
        total = conn.execute(f"SELECT COUNT(*) {_JOIN} WHERE {where}").fetchone()[0]
        rows = conn.execute(
            f"SELECT {_COLUMNS} {_JOIN} WHERE {where} "
            "ORDER BY q.updated_at DESC, q.source_job_id DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
    return {"enabled": True, "total": total, "items": [_job(row) for row in rows]}


def get_recovery(config_path: Path, db_path: Path, *, job_id: int) -> dict:
    active_path = _read_settings(config_path, db_path)
    if active_path is None:
        raise RecoveryDisabledError("후처리가 꺼져 있습니다.")
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT q.*, j.orig_name, j.engine_params, j.status AS source_status "
            f"{_JOIN} WHERE q.source_job_id=? AND ({_MISSING} OR {_CONFIRMED})", (job_id,)
        ).fetchone()
    if row is None:
        raise RecoveryNotFoundError("복구 대상 작업을 찾을 수 없습니다.")
    courses = []
    if (
        row["source_status"] == "DONE"
        and row["status"] in {"SKIPPED", "UNROUTED"}
        and not delivery.has_routing_artifact_reservation(row)
    ):
        active, digest = load_active_semester_snapshot(active_path)
        if digest == row["active_semester_sha256"]:
            courses = [{"code": c.course_code, "name": c.course_name} for c in active.courses]
    return {
        "job": _job(row),
        "courses": courses,
        "can_requeue": bool(courses),
        "source_txt_sha256": row["source_txt_sha256"],
        "source_json_sha256": row["source_json_sha256"],
    }


def apply_recovery(config_path: Path, db_path: Path, *, job_id: int, course_code: str,
                   source_txt_sha256: str, source_json_sha256: str) -> dict:
    config = load_transcript_worker_config(str(config_path))
    if not config.enabled or config.postprocess_settings is None:
        raise RecoveryDisabledError("후처리가 꺼져 있습니다.")
    if config.db_path.resolve() != db_path.resolve():
        raise RuntimeError("작업 DB 설정이 변경되었습니다.")
    with closing(_connect(db_path, writable=True)) as conn:
        try:
            status = delivery.requeue_confirmed_job(
                conn, source_job_id=job_id, course_code=course_code,
                source_txt_sha256=source_txt_sha256, source_json_sha256=source_json_sha256,
                settings=config.postprocess_settings, missing_course_only=True,
            )
        except delivery.TranscriptDeliveryJobNotFoundError as exc:
            raise RecoveryNotFoundError("복구 대상 작업을 찾을 수 없습니다.") from exc
        except (FileNotFoundError, delivery.TranscriptDeliveryError) as exc:
            raise RecoveryConflictError(
                "재등록할 수 없습니다. 과목과 작업 상태를 다시 조회하세요. "
                "원전사가 바뀌었거나 누락됐다면 등록 당시 파일을 복원한 뒤 다시 시도하세요."
            ) from exc
    return {"id": job_id, "status": status}
