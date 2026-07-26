from __future__ import annotations

import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

from lecture_stt.storage_v2.manifest import validate_storage_key
from lecture_stt.storage_v2.repository import connect_v2, require_v2_schema


LIST_SCHEMA_VERSION = "storage-v2/recording-library-list@1"
DETAIL_SCHEMA_VERSION = "storage-v2/recording-detail@1"
_JS_SAFE_INTEGER_MAX = 9_007_199_254_740_991
_STORAGE_KEY_MAX = 255
_JOB_KEY_MAX = 255
_NAME_MAX = 1024
_TITLE_MAX = 512
_TEXT_256_MAX = 256
_TEXT_128_MAX = 128
_TIMESTAMP_MAX = 64
JOB_STATUSES = (
    "queued",
    "processing",
    "done",
    "needs_review",
    "error",
    "canceled",
)
TITLE_SOURCES = (
    "manual",
    "schedule",
    "filename_inference",
    "legacy_import",
    "system",
)
CONTEXT_TYPES = (
    "general",
    "class_session",
    "daily_note",
    "meeting",
    "memo",
)
CONTEXT_SOURCES = (
    "manual",
    "schedule_import",
    "filename_inference",
    "legacy_import",
    "system",
)
SOURCE_STATES = ("available", "missing")
REVIEW_STATUSES = ("open", "triaged", "resolved", "dismissed")
REVIEW_SEVERITIES = ("low", "medium", "high")
_LIST_LIMIT_MAX = 200
_LIST_OFFSET_MAX = 100_000
_DETAIL_JOB_LIMIT = 50
_DETAIL_ARTIFACT_LIMIT = 500
_DETAIL_REVIEW_LIMIT = 100
_ARTIFACT_STAGE_BY_KIND = {
    "source_copy": "source",
    "transcript_raw_text": "transcript",
    "transcript_segments_json": "transcript",
    "correction_text": "correction",
    "correction_json": "correction",
    "summary_markdown": "summary",
    "summary_json": "summary",
    "quality_scorecard": "supporting",
    "metadata": "supporting",
    "log": "supporting",
    "other": "supporting",
}


class RecordingLibraryError(RuntimeError):
    """Base error for read-only recording library access."""


class RecordingLibraryDisabledError(RecordingLibraryError):
    """The recording library API is not enabled."""


class RecordingLibraryNotFoundError(RecordingLibraryError):
    """The requested recording does not exist."""


def _status_counts_zero() -> dict[str, int]:
    return {
        "recordings": 0,
        "queued": 0,
        "processing": 0,
        "done": 0,
        "needs_review": 0,
        "error": 0,
        "canceled": 0,
        "open_reviews": 0,
    }


def _validate_limit_offset(limit: int, offset: int) -> tuple[int, int]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 0 <= limit <= _LIST_LIMIT_MAX
    ):
        raise ValueError(f"limit must be between 0 and {_LIST_LIMIT_MAX}")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= _LIST_OFFSET_MAX
    ):
        raise ValueError(f"offset must be between 0 and {_LIST_OFFSET_MAX}")
    return limit, offset


def _validate_recording_key(storage_key: str) -> str:
    if not isinstance(storage_key, str):
        raise ValueError("storage_key must be a string")
    normalized = validate_storage_key(storage_key, field="storage_key")
    if len(normalized) > _STORAGE_KEY_MAX:
        raise ValueError(
            f"storage_key exceeds the {_STORAGE_KEY_MAX}-character limit"
        )
    return normalized


def _bounded_text(
    value: Any,
    *,
    field: str,
    max_length: int,
    allowed: tuple[str, ...] | None = None,
    required: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if required and normalized.strip() == "":
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > max_length:
        raise ValueError(f"{field} exceeds the {max_length}-character limit")
    if allowed is not None and normalized not in allowed:
        raise ValueError(
            f"{field} must be one of: {', '.join(allowed)}"
        )
    return normalized


def _bounded_int(
    value: Any,
    *,
    field: str,
    minimum: int = 0,
    maximum: int = _JS_SAFE_INTEGER_MAX,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return int(value)


def _bounded_optional_int(
    value: Any,
    *,
    field: str,
    minimum: int = 0,
    maximum: int = _JS_SAFE_INTEGER_MAX,
) -> int | None:
    if value is None:
        return None
    return _bounded_int(
        value,
        field=field,
        minimum=minimum,
        maximum=maximum,
    )


def _open_readonly(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    if not path.exists():
        raise RuntimeError("Storage v2 database is not available")
    conn = connect_v2(path, readonly=True)
    try:
        require_v2_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _title_payload(title: str | None, source: str | None) -> dict[str, str] | None:
    if not title:
        return None
    return {
        "title": _bounded_text(
            title,
            field="current_title.title",
            max_length=_TITLE_MAX,
            required=True,
        ),
        "source": _bounded_text(
            source,
            field="current_title.source",
            max_length=_TEXT_128_MAX,
            allowed=TITLE_SOURCES,
            required=True,
        ),
    }


def _context_payload(row: sqlite3.Row) -> dict[str, Any] | None:
    if row["context_type"] is None:
        return None
    return {
        "context_type": _bounded_text(
            row["context_type"],
            field="selected_context.context_type",
            max_length=_TEXT_128_MAX,
            allowed=CONTEXT_TYPES,
            required=True,
        ),
        "label": _bounded_text(
            row["label"],
            field="selected_context.label",
            max_length=_TEXT_256_MAX,
        ),
        "semester": _bounded_text(
            row["semester"],
            field="selected_context.semester",
            max_length=_TEXT_128_MAX,
        ),
        "course_name": _bounded_text(
            row["course_name"],
            field="selected_context.course_name",
            max_length=_TEXT_256_MAX,
        ),
        "course_code": _bounded_text(
            row["course_code"],
            field="selected_context.course_code",
            max_length=_TEXT_128_MAX,
        ),
        "session_date": _bounded_text(
            row["session_date"],
            field="selected_context.session_date",
            max_length=_TIMESTAMP_MAX,
        ),
        "period_label": _bounded_text(
            row["period_label"],
            field="selected_context.period_label",
            max_length=_TEXT_128_MAX,
        ),
        "period_index": _bounded_optional_int(
            row["period_index"],
            field="selected_context.period_index",
            minimum=1,
        ),
        "source": _bounded_text(
            row["context_source"],
            field="selected_context.source",
            max_length=_TEXT_128_MAX,
            allowed=CONTEXT_SOURCES,
            required=True,
        ),
    }


def _current_job_payload(row: sqlite3.Row) -> dict[str, Any] | None:
    if row["job_key"] is None:
        return None
    return {
        "job_key": _bounded_text(
            row["job_key"],
            field="current_job.job_key",
            max_length=_JOB_KEY_MAX,
            required=True,
        ),
        "status": _bounded_text(
            row["job_status"],
            field="current_job.status",
            max_length=_TEXT_128_MAX,
            allowed=JOB_STATUSES,
            required=True,
        ),
        "progress": _bounded_int(
            row["job_progress"],
            field="current_job.progress",
            maximum=100,
        ),
        "requested_profile": _bounded_text(
            row["requested_profile"],
            field="current_job.requested_profile",
            max_length=_TEXT_128_MAX,
        ),
        "requested_profile_version": _bounded_text(
            row["requested_profile_version"],
            field="current_job.requested_profile_version",
            max_length=_TEXT_128_MAX,
        ),
        "queued_at": _bounded_text(
            row["queued_at"],
            field="current_job.queued_at",
            max_length=_TIMESTAMP_MAX,
            required=True,
        ),
        "finished_at": _bounded_text(
            row["finished_at"],
            field="current_job.finished_at",
            max_length=_TIMESTAMP_MAX,
        ),
    }


def _display_name(title: str | None, original_name_nfc: str) -> str:
    if isinstance(title, str) and title.strip():
        return _bounded_text(
            title,
            field="display_name",
            max_length=_NAME_MAX,
            required=True,
        )
    return _bounded_text(
        original_name_nfc,
        field="display_name",
        max_length=_NAME_MAX,
        required=True,
    )


def _summary_payload(row: sqlite3.Row) -> dict[str, Any]:
    original_name_nfc = _bounded_text(
        row["original_name_nfc"],
        field="original_name_nfc",
        max_length=_NAME_MAX,
        required=True,
    )
    assert original_name_nfc is not None
    return {
        "storage_key": _validate_recording_key(row["storage_key"]),
        "display_name": _display_name(row["title"], original_name_nfc),
        "original_name_nfc": original_name_nfc,
        "source_state": _bounded_text(
            row["source_state"],
            field="source_state",
            max_length=_TEXT_128_MAX,
            allowed=SOURCE_STATES,
            required=True,
        ),
        "recorded_at": _bounded_text(
            row["recorded_at"],
            field="recorded_at",
            max_length=_TIMESTAMP_MAX,
        ),
        "received_at": _bounded_text(
            row["received_at"],
            field="received_at",
            max_length=_TIMESTAMP_MAX,
            required=True,
        ),
        "current_title": _title_payload(row["title"], row["title_source"]),
        "selected_context": _context_payload(row),
        "current_job": _current_job_payload(row),
        "open_review_count": _bounded_int(
            row["open_review_count"],
            field="open_review_count",
        ),
        "artifact_count": _bounded_int(
            row["artifact_count"],
            field="artifact_count",
        ),
    }


def disabled_recording_library_list(
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    normalized_limit, normalized_offset = _validate_limit_offset(limit, offset)
    return {
        "schema_version": LIST_SCHEMA_VERSION,
        "available": False,
        "disabled_reason": "recording_library_disabled",
        "filters": {
            "limit": normalized_limit,
            "offset": normalized_offset,
        },
        "counts": _status_counts_zero(),
        "total": 0,
        "summaries": [],
    }


def list_recordings(
    db_path: Path | str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    normalized_limit, normalized_offset = _validate_limit_offset(limit, offset)
    conn = _open_readonly(db_path)
    try:
        counts = _status_counts_zero()
        for row in conn.execute(
            """
            SELECT job.status, COUNT(*) AS count
            FROM transcription_jobs AS job
            JOIN recordings AS recording
              ON recording.id = job.recording_id
            WHERE job.is_current = 1
              AND job.archived_at IS NULL
              AND recording.archived_at IS NULL
            GROUP BY job.status
            """
        ).fetchall():
            counts[str(row["status"])] = _bounded_int(
                row["count"],
                field=f"counts.{row['status']}",
            )

        counts["recordings"] = _bounded_int(
            int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM recordings
                    WHERE archived_at IS NULL
                    """
                ).fetchone()[0]
            ),
            field="counts.recordings",
        )
        counts["open_reviews"] = _bounded_int(
            int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM review_items AS review
                    JOIN recordings AS recording
                      ON recording.id = review.recording_id
                    WHERE recording.archived_at IS NULL
                      AND review.status IN ('open', 'triaged')
                    """
                ).fetchone()[0]
            ),
            field="counts.open_reviews",
        )
        rows = conn.execute(
            """
            WITH current_jobs AS (
                SELECT
                    job.recording_id,
                    job.job_key,
                    job.status,
                    job.progress,
                    job.requested_profile,
                    job.requested_profile_version,
                    job.queued_at,
                    job.finished_at
                FROM transcription_jobs AS job
                WHERE job.is_current = 1
                  AND job.archived_at IS NULL
            ),
            review_counts AS (
                SELECT
                    review.recording_id,
                    COUNT(*) AS open_review_count
                FROM review_items AS review
                WHERE review.status IN ('open', 'triaged')
                GROUP BY review.recording_id
            ),
            artifact_counts AS (
                SELECT
                    artifact.recording_id,
                    COUNT(*) AS artifact_count
                FROM artifacts AS artifact
                JOIN transcription_jobs AS job
                  ON job.id = artifact.job_id
                 AND job.recording_id = artifact.recording_id
                WHERE artifact.archived_at IS NULL
                  AND job.archived_at IS NULL
                GROUP BY artifact.recording_id
            )
            SELECT
                recording.storage_key,
                recording.original_name_nfc,
                recording.source_state,
                recording.recorded_at,
                recording.received_at,
                title.title,
                title.title_source,
                context.context_type,
                context.label,
                context.semester,
                context.course_name,
                context.course_code,
                context.session_date,
                context.period_label,
                context.period_index,
                context.source AS context_source,
                job.job_key,
                job.status AS job_status,
                job.progress AS job_progress,
                job.requested_profile,
                job.requested_profile_version,
                job.queued_at,
                job.finished_at,
                COALESCE(review_counts.open_review_count, 0) AS open_review_count,
                COALESCE(artifact_counts.artifact_count, 0) AS artifact_count
            FROM recordings AS recording
            LEFT JOIN recording_titles AS title
              ON title.recording_id = recording.id
             AND title.is_current = 1
            LEFT JOIN recording_contexts AS context
              ON context.recording_id = recording.id
             AND context.is_selected = 1
            LEFT JOIN current_jobs AS job
              ON job.recording_id = recording.id
            LEFT JOIN review_counts
              ON review_counts.recording_id = recording.id
            LEFT JOIN artifact_counts
              ON artifact_counts.recording_id = recording.id
            WHERE recording.archived_at IS NULL
            ORDER BY
                COALESCE(
                    recording.recorded_at,
                    recording.received_at,
                    job.queued_at,
                    recording.created_at
                ) DESC,
                recording.id DESC
            LIMIT ? OFFSET ?
            """,
            (normalized_limit, normalized_offset),
        ).fetchall()
    finally:
        conn.close()

    return {
        "schema_version": LIST_SCHEMA_VERSION,
        "available": True,
        "filters": {
            "limit": normalized_limit,
            "offset": normalized_offset,
        },
        "counts": counts,
        "total": counts["recordings"],
        "summaries": [_summary_payload(row) for row in rows],
    }


def _artifact_stage(artifact_kind: str) -> str:
    return _ARTIFACT_STAGE_BY_KIND.get(artifact_kind, "supporting")


def _detail_artifact_payload(row: sqlite3.Row) -> dict[str, Any]:
    artifact_kind = _bounded_text(
        row["artifact_kind"],
        field="jobs[].artifacts[].artifact_kind",
        max_length=_TEXT_128_MAX,
        required=True,
    )
    assert artifact_kind is not None
    return {
        "artifact_kind": artifact_kind,
        "stage": _bounded_text(
            _artifact_stage(artifact_kind),
            field="jobs[].artifacts[].stage",
            max_length=_TEXT_128_MAX,
            allowed=("source", "transcript", "correction", "summary", "supporting"),
            required=True,
        ),
        "revision": _bounded_int(
            row["revision"],
            field="jobs[].artifacts[].revision",
            minimum=1,
        ),
        "is_latest": bool(row["is_latest"]),
        "bytes": _bounded_optional_int(
            row["bytes"],
            field="jobs[].artifacts[].bytes",
        ),
        "mime_type": _bounded_text(
            row["mime_type"],
            field="jobs[].artifacts[].mime_type",
            max_length=_TEXT_256_MAX,
        ),
        "created_at": _bounded_text(
            row["created_at"],
            field="jobs[].artifacts[].created_at",
            max_length=_TIMESTAMP_MAX,
            required=True,
        ),
    }


def _detail_job_payload(
    row: sqlite3.Row,
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "job_key": _bounded_text(
            row["job_key"],
            field="jobs[].job_key",
            max_length=_JOB_KEY_MAX,
            required=True,
        ),
        "status": _bounded_text(
            row["status"],
            field="jobs[].status",
            max_length=_TEXT_128_MAX,
            allowed=JOB_STATUSES,
            required=True,
        ),
        "progress": _bounded_int(
            row["progress"],
            field="jobs[].progress",
            maximum=100,
        ),
        "is_current": bool(row["is_current"]),
        "requested_profile": _bounded_text(
            row["requested_profile"],
            field="jobs[].requested_profile",
            max_length=_TEXT_128_MAX,
        ),
        "requested_profile_version": _bounded_text(
            row["requested_profile_version"],
            field="jobs[].requested_profile_version",
            max_length=_TEXT_128_MAX,
        ),
        "queued_at": _bounded_text(
            row["queued_at"],
            field="jobs[].queued_at",
            max_length=_TIMESTAMP_MAX,
            required=True,
        ),
        "started_at": _bounded_text(
            row["started_at"],
            field="jobs[].started_at",
            max_length=_TIMESTAMP_MAX,
        ),
        "finished_at": _bounded_text(
            row["finished_at"],
            field="jobs[].finished_at",
            max_length=_TIMESTAMP_MAX,
        ),
        "artifacts": artifacts,
    }


def _detail_review_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "status": _bounded_text(
            row["status"],
            field="reviews[].status",
            max_length=_TEXT_128_MAX,
            allowed=REVIEW_STATUSES,
            required=True,
        ),
        "severity": _bounded_text(
            row["severity"],
            field="reviews[].severity",
            max_length=_TEXT_128_MAX,
            allowed=REVIEW_SEVERITIES,
        ),
        "reason_code": _bounded_text(
            row["reason_code"],
            field="reviews[].reason_code",
            max_length=_TEXT_128_MAX,
            required=True,
        ),
        "created_at": _bounded_text(
            row["created_at"],
            field="reviews[].created_at",
            max_length=_TIMESTAMP_MAX,
            required=True,
        ),
        "resolved_at": _bounded_text(
            row["resolved_at"],
            field="reviews[].resolved_at",
            max_length=_TIMESTAMP_MAX,
        ),
        "job_key": _bounded_text(
            row["job_key"],
            field="reviews[].job_key",
            max_length=_JOB_KEY_MAX,
        ),
        "artifact_kind": _bounded_text(
            row["artifact_kind"],
            field="reviews[].artifact_kind",
            max_length=_TEXT_128_MAX,
        ),
        "artifact_revision": _bounded_optional_int(
            row["artifact_revision"],
            field="reviews[].artifact_revision",
            minimum=1,
        ),
    }


def read_recording_detail(
    db_path: Path | str,
    storage_key: str,
) -> dict[str, Any]:
    normalized_storage_key = _validate_recording_key(storage_key)
    conn = _open_readonly(db_path)
    try:
        recording = conn.execute(
            """
            SELECT
                recording.id,
                recording.storage_key,
                recording.original_name_nfc,
                recording.source_state,
                recording.ingest_bytes,
                recording.source_mime,
                recording.recorded_at,
                recording.received_at,
                title.title,
                title.title_source,
                context.context_type,
                context.label,
                context.semester,
                context.course_name,
                context.course_code,
                context.session_date,
                context.period_label,
                context.period_index,
                context.source AS context_source
            FROM recordings AS recording
            LEFT JOIN recording_titles AS title
              ON title.recording_id = recording.id
             AND title.is_current = 1
            LEFT JOIN recording_contexts AS context
              ON context.recording_id = recording.id
             AND context.is_selected = 1
            WHERE recording.storage_key = ?
              AND recording.archived_at IS NULL
            """,
            (normalized_storage_key,),
        ).fetchone()
        if recording is None:
            raise RecordingLibraryNotFoundError(
                f"Recording not found: {normalized_storage_key}"
            )

        recording_id = int(recording["id"])
        counts = dict(
            conn.execute(
                """
                SELECT
                    (SELECT COUNT(*)
                     FROM transcription_jobs
                     WHERE recording_id = ?
                       AND archived_at IS NULL) AS jobs,
                    (SELECT COUNT(*)
                     FROM artifacts AS artifact
                     JOIN transcription_jobs AS job
                       ON job.id = artifact.job_id
                      AND job.recording_id = artifact.recording_id
                     WHERE artifact.recording_id = ?
                       AND artifact.archived_at IS NULL
                       AND job.archived_at IS NULL) AS artifacts,
                    (SELECT COUNT(*)
                     FROM review_items
                     WHERE recording_id = ?) AS reviews,
                    (SELECT COUNT(*)
                     FROM review_items
                     WHERE recording_id = ?
                       AND status IN ('open', 'triaged')) AS open_reviews
                """,
                (recording_id, recording_id, recording_id, recording_id),
            ).fetchone()
        )
        job_rows = conn.execute(
            """
            SELECT
                id,
                job_key,
                status,
                progress,
                is_current,
                requested_profile,
                requested_profile_version,
                queued_at,
                started_at,
                finished_at
            FROM transcription_jobs
            WHERE recording_id = ?
              AND archived_at IS NULL
            ORDER BY
                is_current DESC,
                COALESCE(finished_at, started_at, queued_at) DESC,
                id DESC
            LIMIT ?
            """,
            (recording_id, _DETAIL_JOB_LIMIT + 1),
        ).fetchall()
        jobs_truncated = len(job_rows) > _DETAIL_JOB_LIMIT
        returned_job_rows = job_rows[:_DETAIL_JOB_LIMIT]
        returned_job_ids = [int(row["id"]) for row in returned_job_rows]

        artifact_by_job_id: dict[int, list[dict[str, Any]]] = {
            job_id: [] for job_id in returned_job_ids
        }
        returned_artifact_count = 0
        if returned_job_ids:
            visible_job_values = ",".join(
                "(?, ?)" for _ in returned_job_ids
            )
            visible_job_parameters = tuple(
                value
                for job_rank, job_id in enumerate(returned_job_ids)
                for value in (job_id, job_rank)
            )
            artifact_rows = conn.execute(
                f"""
                WITH visible_jobs(job_id, job_rank) AS (
                    VALUES {visible_job_values}
                )
                SELECT
                    artifact.job_id,
                    artifact.artifact_kind,
                    artifact.revision,
                    artifact.is_latest,
                    artifact.bytes,
                    artifact.mime_type,
                    artifact.created_at
                FROM visible_jobs
                JOIN artifacts AS artifact
                  ON artifact.job_id = visible_jobs.job_id
                JOIN transcription_jobs AS job
                  ON job.id = artifact.job_id
                 AND job.recording_id = artifact.recording_id
                WHERE artifact.recording_id = ?
                  AND artifact.archived_at IS NULL
                  AND job.archived_at IS NULL
                ORDER BY
                    visible_jobs.job_rank,
                    CASE artifact.artifact_kind
                        WHEN 'source_copy' THEN 1
                        WHEN 'transcript_raw_text' THEN 2
                        WHEN 'transcript_segments_json' THEN 3
                        WHEN 'correction_text' THEN 4
                        WHEN 'correction_json' THEN 5
                        WHEN 'summary_markdown' THEN 6
                        WHEN 'summary_json' THEN 7
                        ELSE 8
                    END,
                    artifact.revision DESC,
                    artifact.id DESC
                LIMIT ?
                """,
                (
                    *visible_job_parameters,
                    recording_id,
                    _DETAIL_ARTIFACT_LIMIT + 1,
                ),
            ).fetchall()
            returned_artifact_rows = artifact_rows[:_DETAIL_ARTIFACT_LIMIT]
            returned_artifact_count = len(returned_artifact_rows)
            for row in returned_artifact_rows:
                artifact_by_job_id[int(row["job_id"])].append(
                    _detail_artifact_payload(row)
                )

        review_rows = conn.execute(
            """
            SELECT
                review.status,
                review.severity,
                review.reason_code,
                review.created_at,
                review.resolved_at,
                job.job_key,
                artifact.artifact_kind,
                artifact.revision AS artifact_revision
            FROM review_items AS review
            LEFT JOIN transcription_jobs AS job
              ON job.id = review.job_id
             AND job.recording_id = review.recording_id
             AND job.archived_at IS NULL
            LEFT JOIN artifacts AS artifact
              ON artifact.id = review.artifact_id
             AND artifact.recording_id = review.recording_id
             AND artifact.archived_at IS NULL
             AND job.id IS NOT NULL
            WHERE review.recording_id = ?
            ORDER BY review.created_at DESC, review.id DESC
            LIMIT ?
            """,
            (recording_id, _DETAIL_REVIEW_LIMIT + 1),
        ).fetchall()
        reviews_truncated = len(review_rows) > _DETAIL_REVIEW_LIMIT
        returned_review_rows = review_rows[:_DETAIL_REVIEW_LIMIT]
    finally:
        conn.close()

    original_name_nfc = _bounded_text(
        recording["original_name_nfc"],
        field="recording.original_name_nfc",
        max_length=_NAME_MAX,
        required=True,
    )
    assert original_name_nfc is not None
    return {
        "schema_version": DETAIL_SCHEMA_VERSION,
        "available": True,
        "recording": {
            "storage_key": _validate_recording_key(recording["storage_key"]),
            "display_name": _display_name(recording["title"], original_name_nfc),
            "original_name_nfc": original_name_nfc,
            "current_title": _title_payload(
                recording["title"],
                recording["title_source"],
            ),
            "selected_context": _context_payload(recording),
            "source": {
                "state": _bounded_text(
                    recording["source_state"],
                    field="recording.source.state",
                    max_length=_TEXT_128_MAX,
                    allowed=SOURCE_STATES,
                    required=True,
                ),
                "bytes": _bounded_optional_int(
                    recording["ingest_bytes"],
                    field="recording.source.bytes",
                ),
                "mime_type": _bounded_text(
                    recording["source_mime"],
                    field="recording.source.mime_type",
                    max_length=_TEXT_256_MAX,
                ),
                "recorded_at": _bounded_text(
                    recording["recorded_at"],
                    field="recording.source.recorded_at",
                    max_length=_TIMESTAMP_MAX,
                ),
                "received_at": _bounded_text(
                    recording["received_at"],
                    field="recording.source.received_at",
                    max_length=_TIMESTAMP_MAX,
                    required=True,
                ),
            },
        },
        "counts": {
            "jobs": _bounded_int(counts["jobs"], field="counts.jobs"),
            "artifacts": _bounded_int(
                counts["artifacts"],
                field="counts.artifacts",
            ),
            "reviews": _bounded_int(
                counts["reviews"],
                field="counts.reviews",
            ),
            "open_reviews": _bounded_int(
                counts["open_reviews"],
                field="counts.open_reviews",
            ),
        },
        "limits": {
            "jobs": _DETAIL_JOB_LIMIT,
            "artifacts": _DETAIL_ARTIFACT_LIMIT,
            "reviews": _DETAIL_REVIEW_LIMIT,
            "jobs_truncated": jobs_truncated,
            "artifacts_truncated": (
                counts["artifacts"] > returned_artifact_count
            ),
            "reviews_truncated": reviews_truncated,
        },
        "jobs": [
            _detail_job_payload(
                row,
                artifact_by_job_id.get(int(row["id"]), []),
            )
            for row in returned_job_rows
        ],
        "reviews": [
            _detail_review_payload(row)
            for row in returned_review_rows
        ],
    }
