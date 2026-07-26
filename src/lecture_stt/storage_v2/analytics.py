from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lecture_stt.storage_v2.manifest import (
    ManifestValidationError,
    validate_relative_path,
    validate_storage_key,
)
from lecture_stt.storage_v2.repository import connect_v2, require_v2_schema


SCHEMA_VERSION = "storage-v2/transcription-analytics@1"
PERIODS = ("day", "week", "month")
STATUSES = (
    "queued",
    "processing",
    "done",
    "needs_review",
    "error",
    "canceled",
)
QUALITY_BANDS = (
    "90-100",
    "80-89",
    "70-79",
    "60-69",
    "0-59",
    "unscored",
)
QUALITY_BAND_RANGES: tuple[tuple[str, int | None, int | None], ...] = (
    ("90-100", 90, 100),
    ("80-89", 80, 89),
    ("70-79", 70, 79),
    ("60-69", 60, 69),
    ("0-59", 0, 59),
    ("unscored", None, None),
)
QUALITY_BAND_LABELS = {
    "90-100": "90–100점",
    "80-89": "80–89점",
    "70-79": "70–79점",
    "60-69": "60–69점",
    "0-59": "0–59점",
    "unscored": "점수 없음",
}
CONTEXT_LABELS = {
    "general": "일반",
    "class_session": "수업",
    "daily_note": "일상 기록",
    "meeting": "회의·대화",
    "memo": "개인 메모",
}
_MAX_RECENT_ATTENTION = 20
_MAX_SCORECARD_BYTES = 1024 * 1024
_MAX_DURATION_SEC = 365 * 24 * 60 * 60
_UTC = timezone.utc
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=_UTC)


@dataclass(frozen=True)
class _WindowSpec:
    period: str
    start_at: datetime
    end_at: datetime
    bucket: str
    label: str
    bucket_starts: tuple[datetime, ...]
    bucket_labels: tuple[str, ...]


@dataclass(frozen=True)
class _ScorecardSummary:
    score: int
    health: str
    audio_duration_sec: float | None
    total_processing_sec: float | None


@dataclass(frozen=True)
class _ScorecardReadResult:
    state: str
    summary: _ScorecardSummary | None


@dataclass(frozen=True)
class _RowSummary:
    storage_key: str
    display_name: str
    status: str
    event_at: datetime
    event_time_basis: str
    quality_score: int | None
    health: str | None
    audio_duration_sec: float | None
    total_processing_sec: float | None
    context_type: str | None
    classification_source: str | None
    scorecard_state: str


def disabled_transcription_analytics(
    period: str,
    *,
    row_limit: int = 5000,
    scorecard_max_bytes: int = 262144,
    now: datetime | None = None,
    timezone_name: str = "Asia/Seoul",
) -> dict[str, Any]:
    normalized_period = _validate_period(period)
    normalized_row_limit = _validate_row_limit(row_limit)
    normalized_scorecard_max_bytes = _validate_scorecard_max_bytes(
        scorecard_max_bytes
    )
    tz = _load_timezone(timezone_name)
    current = _coerce_now(now, tz)
    window = _build_window(normalized_period, current)
    return {
        "schema_version": SCHEMA_VERSION,
        "available": False,
        "disabled_reason": "transcription_analytics_disabled",
        "period": normalized_period,
        "timezone": timezone_name,
        "window": _window_payload(window),
        "freshness": {
            "generated_at": current.isoformat(),
            "latest_event_at": None,
        },
        "limits": {
            "row_limit": normalized_row_limit,
            "scorecard_max_bytes": normalized_scorecard_max_bytes,
            "truncated": False,
        },
        "coverage": _empty_coverage(),
        "totals": _empty_totals(),
        "quality_distribution": _quality_distribution_payload(
            {label: 0 for label in QUALITY_BANDS}
        ),
        "status_distribution": _status_distribution_payload(
            {status: 0 for status in STATUSES}
        ),
        "classification_distribution": _classification_distribution_payload({}),
        "timeline": _finalize_timeline(_empty_timeline(window)),
        "recent_attention": [],
    }


def read_transcription_analytics(
    db_path: Path | str,
    records_root: Path | str,
    *,
    period: str = "week",
    now: datetime | None = None,
    timezone_name: str = "Asia/Seoul",
    row_limit: int = 5000,
    scorecard_max_bytes: int = 262144,
) -> dict[str, Any]:
    normalized_period = _validate_period(period)
    normalized_row_limit = _validate_row_limit(row_limit)
    normalized_scorecard_max_bytes = _validate_scorecard_max_bytes(
        scorecard_max_bytes
    )
    tz = _load_timezone(timezone_name)
    current = _coerce_now(now, tz)
    window = _build_window(normalized_period, current)
    records_root_path = _validate_records_root(records_root)
    db_file = Path(db_path).expanduser()
    if not db_file.exists():
        raise RuntimeError("Storage v2 database is not available")

    conn = connect_v2(db_file, readonly=True)
    root_fd: int | None = None
    candidate_limit = normalized_row_limit + 1
    try:
        require_v2_schema(conn)
        root_fd = _open_records_root_fd(records_root_path)
        conn.create_function(
            "_lecture_stt_event_time_us",
            3,
            lambda recorded_at, received_at, queued_at: _event_time_unix_us(
                recorded_at=recorded_at,
                received_at=received_at,
                queued_at=queued_at,
                tz=tz,
            ),
            deterministic=True,
        )
        rows = conn.execute(
            """
            WITH analytics_candidates AS (
                SELECT
                    r.storage_key,
                    r.original_name_nfc,
                    r.recorded_at,
                    r.received_at,
                    job.id AS job_id,
                    job.status,
                    job.queued_at,
                    title.title AS current_title,
                    context.context_type,
                    context.source AS classification_source,
                    artifact.path_rel AS quality_scorecard_path,
                    artifact.content_sha256 AS quality_scorecard_sha256,
                    artifact.bytes AS quality_scorecard_bytes,
                    _lecture_stt_event_time_us(
                        r.recorded_at,
                        r.received_at,
                        job.queued_at
                    ) AS event_time_us
                FROM recordings AS r
                JOIN transcription_jobs AS job
                  ON job.recording_id = r.id
                 AND job.is_current = 1
                 AND job.archived_at IS NULL
                LEFT JOIN recording_titles AS title
                  ON title.recording_id = r.id
                 AND title.is_current = 1
                LEFT JOIN recording_contexts AS context
                  ON context.recording_id = r.id
                 AND context.is_selected = 1
                LEFT JOIN artifacts AS artifact
                  ON artifact.recording_id = r.id
                 AND artifact.job_id = job.id
                 AND artifact.artifact_kind = 'quality_scorecard'
                 AND artifact.is_latest = 1
                 AND artifact.archived_at IS NULL
                WHERE r.archived_at IS NULL
            )
            SELECT *
            FROM analytics_candidates
            WHERE event_time_us BETWEEN ? AND ?
            ORDER BY event_time_us DESC, job_id DESC
            LIMIT ?
            """,
            (
                _datetime_unix_us(window.start_at),
                _datetime_unix_us(window.end_at),
                candidate_limit,
            ),
        ).fetchall()

        parsed_rows: list[_RowSummary] = []
        for row in rows:
            event = _select_event_at(
                recorded_at=row["recorded_at"],
                received_at=row["received_at"],
                queued_at=row["queued_at"],
                tz=tz,
            )
            if event is None:
                continue
            event_at, event_basis = event
            if event_at < window.start_at or event_at > window.end_at:
                continue
            scorecard_result = _read_scorecard_summary(
                root_fd,
                storage_key=row["storage_key"],
                path_rel=row["quality_scorecard_path"],
                expected_sha256=row["quality_scorecard_sha256"],
                expected_bytes=row["quality_scorecard_bytes"],
                max_bytes=normalized_scorecard_max_bytes,
            )
            scorecard = scorecard_result.summary
            parsed_rows.append(
                _RowSummary(
                    storage_key=str(row["storage_key"]),
                    display_name=_display_name(
                        current_title=row["current_title"],
                        original_name_nfc=row["original_name_nfc"],
                    ),
                    status=str(row["status"]),
                    event_at=event_at,
                    event_time_basis=event_basis,
                    quality_score=None if scorecard is None else scorecard.score,
                    health=None if scorecard is None else scorecard.health,
                    audio_duration_sec=(
                        None if scorecard is None else scorecard.audio_duration_sec
                    ),
                    total_processing_sec=(
                        None
                        if scorecard is None
                        else scorecard.total_processing_sec
                    ),
                    context_type=_normalize_optional_text(row["context_type"]),
                    classification_source=_normalize_optional_text(
                        row["classification_source"]
                    ),
                    scorecard_state=scorecard_result.state,
                )
            )

        parsed_rows.sort(
            key=lambda item: (item.event_at, item.storage_key),
            reverse=True,
        )
        truncated = len(parsed_rows) > normalized_row_limit
        bounded_rows = parsed_rows[:normalized_row_limit]
    finally:
        if root_fd is not None:
            os.close(root_fd)
        conn.close()

    coverage = _empty_coverage()
    totals = _empty_totals()
    timeline = _empty_timeline(window)
    latest_event_at: str | None = None

    total_quality_score = 0
    total_quality_count = 0
    total_audio_duration = 0.0
    total_audio_duration_count = 0
    total_processing_sec = 0.0
    total_processing_sec_count = 0
    overall_quality_counts = {label: 0 for label in QUALITY_BANDS}
    overall_classification_counts: dict[tuple[str | None, str | None], int] = {}
    attention_rows: list[dict[str, Any]] = []

    for row in bounded_rows:
        coverage["jobs_total"] += 1
        coverage[f"event_time_{row.event_time_basis}"] += 1
        totals["jobs"] += 1
        totals[row.status] += 1

        if row.scorecard_state == "invalid":
            coverage["quality_invalid"] += 1
        elif row.scorecard_state == "missing":
            coverage["quality_missing"] += 1
        else:
            coverage["quality_scored"] += 1
            total_quality_score += row.quality_score
            total_quality_count += 1
            overall_quality_counts[_quality_band(row.quality_score)] += 1

        if row.audio_duration_sec is not None:
            total_audio_duration += row.audio_duration_sec
            total_audio_duration_count += 1
            coverage["audio_duration_known"] += 1
        if row.total_processing_sec is not None:
            total_processing_sec += row.total_processing_sec
            total_processing_sec_count += 1
            coverage["processing_duration_known"] += 1

        if row.context_type is None or row.classification_source is None:
            coverage["classification_unclassified"] += 1
        else:
            coverage["classification_known"] += 1

        latest_event_at = (
            row.event_at.isoformat()
            if latest_event_at is None
            else latest_event_at
        )

        bucket_index = _bucket_index(window, row.event_at)
        bucket = timeline[bucket_index]
        bucket["jobs"] += 1
        if row.status == "done":
            bucket["done"] += 1
        if row.status == "needs_review":
            bucket["needs_review"] += 1
        if row.status == "error":
            bucket["error"] += 1
        bucket["_status_counts"][row.status] += 1

        if row.quality_score is None:
            bucket["_quality_counts"]["unscored"] += 1
        else:
            bucket["quality_scored"] += 1
            bucket["_quality_score_total"] += row.quality_score
            bucket["_quality_counts"][_quality_band(row.quality_score)] += 1

        classification_key = _classification_distribution_key(
            row.context_type,
            row.classification_source,
        )
        overall_classification_counts[classification_key] = (
            overall_classification_counts.get(classification_key, 0) + 1
        )
        bucket["_classification_counts"][classification_key] = (
            bucket["_classification_counts"].get(classification_key, 0) + 1
        )

        if _needs_attention(row):
            attention_rows.append(
                {
                    "storage_key": row.storage_key,
                    "display_name": row.display_name,
                    "status": row.status,
                    "event_at": row.event_at.isoformat(),
                    "quality_score": row.quality_score,
                    "health": row.health,
                    "context_type": row.context_type,
                    "classification_source": row.classification_source,
                }
            )
        if row.quality_score is None:
            overall_quality_counts["unscored"] += 1

    if total_quality_count > 0:
        totals["average_quality_score"] = round(
            total_quality_score / total_quality_count,
            2,
        )
    if total_audio_duration_count > 0:
        totals["audio_duration_sec"] = round(total_audio_duration, 3)
    if total_processing_sec_count > 0:
        totals["total_processing_sec"] = round(total_processing_sec, 3)

    timeline_payload = _finalize_timeline(timeline)

    return {
        "schema_version": SCHEMA_VERSION,
        "available": True,
        "period": normalized_period,
        "timezone": timezone_name,
        "window": _window_payload(window),
        "freshness": {
            "generated_at": current.isoformat(),
            "latest_event_at": latest_event_at,
        },
        "limits": {
            "row_limit": normalized_row_limit,
            "scorecard_max_bytes": normalized_scorecard_max_bytes,
            "truncated": truncated,
        },
        "coverage": coverage,
        "totals": totals,
        "quality_distribution": _quality_distribution_payload(
            overall_quality_counts
        ),
        "status_distribution": _status_distribution_payload(
            {status: totals[status] for status in STATUSES}
        ),
        "classification_distribution": _classification_distribution_payload(
            overall_classification_counts
        ),
        "timeline": timeline_payload,
        "recent_attention": attention_rows[:_MAX_RECENT_ATTENTION],
    }


def _validate_period(period: str) -> str:
    normalized = str(period or "").strip().lower()
    if normalized not in PERIODS:
        raise ValueError("period must be one of: day, week, month")
    return normalized


def _validate_row_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5000:
        raise ValueError("row_limit must be between 1 and 5000")
    return value


def _validate_scorecard_max_bytes(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_SCORECARD_BYTES
    ):
        raise ValueError("scorecard_max_bytes must be between 1 and 1048576")
    return value


def _load_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc


def _coerce_now(now: datetime | None, tz: ZoneInfo) -> datetime:
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None or now.utcoffset() is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _build_window(period: str, current: datetime) -> _WindowSpec:
    window_start = current.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    if period == "day":
        bucket_starts = tuple(
            window_start + timedelta(hours=offset)
            for offset in range(24)
        )
        bucket_labels = tuple(
            bucket.strftime("%H:00") for bucket in bucket_starts
        )
        return _WindowSpec(
            period=period,
            start_at=window_start,
            end_at=current,
            bucket="hour",
            label="오늘",
            bucket_starts=bucket_starts,
            bucket_labels=bucket_labels,
        )

    days = 6 if period == "week" else 29
    window_start = window_start - timedelta(days=days)
    bucket_starts = tuple(
        window_start + timedelta(days=offset)
        for offset in range(days + 1)
    )
    bucket_labels = tuple(
        bucket.strftime("%m-%d") for bucket in bucket_starts
    )
    return _WindowSpec(
        period=period,
        start_at=window_start,
        end_at=current,
        bucket="day",
        label="최근 7일" if period == "week" else "최근 30일",
        bucket_starts=bucket_starts,
        bucket_labels=bucket_labels,
    )


def _window_payload(window: _WindowSpec) -> dict[str, Any]:
    return {
        "start_at": window.start_at.isoformat(),
        "end_at": window.end_at.isoformat(),
        "bucket": window.bucket,
        "label": window.label,
    }


def _empty_coverage() -> dict[str, int]:
    return {
        "jobs_total": 0,
        "quality_scored": 0,
        "quality_missing": 0,
        "quality_invalid": 0,
        "audio_duration_known": 0,
        "processing_duration_known": 0,
        "classification_known": 0,
        "classification_unclassified": 0,
        "event_time_recorded": 0,
        "event_time_received": 0,
        "event_time_queued": 0,
        "event_time_invalid": 0,
    }


def _empty_totals() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "jobs": 0,
        "average_quality_score": None,
        "audio_duration_sec": None,
        "total_processing_sec": None,
    }
    for status in STATUSES:
        payload[status] = 0
    return payload


def _empty_timeline(window: _WindowSpec) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    for bucket_start, label in zip(window.bucket_starts, window.bucket_labels):
        timeline.append(
            {
                "bucket_start": bucket_start.isoformat(),
                "label": label,
                "jobs": 0,
                "done": 0,
                "needs_review": 0,
                "error": 0,
                "quality_scored": 0,
                "average_quality_score": None,
                "_classification_counts": {},
                "_quality_counts": {
                    label: 0 for label in QUALITY_BANDS
                },
                "_status_counts": {
                    status: 0 for status in STATUSES
                },
                "_quality_score_total": 0,
            }
        )
    return timeline


def _finalize_timeline(
    timeline: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for raw_bucket in timeline:
        bucket = dict(raw_bucket)
        if bucket["quality_scored"] > 0:
            bucket["average_quality_score"] = round(
                bucket["_quality_score_total"] / bucket["quality_scored"],
                2,
            )
        else:
            bucket["average_quality_score"] = None
        bucket.pop("_classification_counts")
        bucket.pop("_quality_counts")
        bucket.pop("_status_counts")
        bucket.pop("_quality_score_total")
        payload.append(bucket)
    return payload


def _parse_timestamp(value: Any, *, tz: ZoneInfo) -> datetime | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=_UTC)
        return parsed.astimezone(tz)
    except (OverflowError, ValueError):
        return None


def _select_event_at(
    *,
    recorded_at: Any,
    received_at: Any,
    queued_at: Any,
    tz: ZoneInfo,
) -> tuple[datetime, str] | None:
    for basis, value in (
        ("recorded", recorded_at),
        ("received", received_at),
        ("queued", queued_at),
    ):
        parsed = _parse_timestamp(value, tz=tz)
        if parsed is not None:
            return parsed, basis
    return None


def _datetime_unix_us(value: datetime) -> int:
    delta = value.astimezone(_UTC) - _UNIX_EPOCH
    return (
        (delta.days * 24 * 60 * 60 + delta.seconds) * 1_000_000
        + delta.microseconds
    )


def _event_time_unix_us(
    *,
    recorded_at: Any,
    received_at: Any,
    queued_at: Any,
    tz: ZoneInfo,
) -> int | None:
    selected = _select_event_at(
        recorded_at=recorded_at,
        received_at=received_at,
        queued_at=queued_at,
        tz=tz,
    )
    if selected is None:
        return None
    event_at, _basis = selected
    return _datetime_unix_us(event_at)


def _display_name(*, current_title: Any, original_name_nfc: Any) -> str:
    title = _normalize_optional_text(current_title)
    if title:
        return title
    original = _normalize_optional_text(original_name_nfc)
    return original or ""


def _normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _validate_records_root(path: Path | str) -> Path:
    root = Path(path).expanduser()
    try:
        metadata = root.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError("storage_v2.records_root is not available") from exc
    if root.is_symlink():
        raise RuntimeError("storage_v2.records_root must not be a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("storage_v2.records_root must be a directory")
    return root


def _open_records_root_fd(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(
            "storage_v2.records_root must be an existing non-symlink directory"
        ) from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise RuntimeError(
            "storage_v2.records_root must be an existing non-symlink directory"
        )
    return descriptor


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _regular_file_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _open_scorecard_fd(root_fd: int, *, storage_key: str, path_rel: str) -> int:
    components = (storage_key, *PurePosixPath(path_rel).parts)
    current_fd = os.dup(root_fd)
    try:
        for component in components[:-1]:
            next_fd = os.open(
                component,
                _directory_open_flags(),
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        file_fd = os.open(
            components[-1],
            _regular_file_open_flags(),
            dir_fd=current_fd,
        )
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(
                    errno.EINVAL,
                    "quality scorecard is not a regular file",
                    path_rel,
                )
            if metadata.st_nlink != 1:
                raise OSError(
                    errno.EMLINK,
                    "quality scorecard must not be hard-linked",
                    path_rel,
                )
        except BaseException:
            os.close(file_fd)
            raise
        return file_fd
    finally:
        os.close(current_fd)


def _read_scorecard_payload(
    root_fd: int,
    *,
    storage_key: str,
    path_rel: str,
    expected_sha256: str,
    expected_bytes: int,
    max_bytes: int,
) -> dict[str, Any]:
    descriptor = _open_scorecard_fd(
        root_fd,
        storage_key=storage_key,
        path_rel=path_rel,
    )
    try:
        before = os.fstat(descriptor)
        if before.st_size > max_bytes:
            raise ValueError(
                f"quality scorecard exceeds the {max_bytes}-byte limit"
            )
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise ValueError(
                    f"quality scorecard exceeds the {max_bytes}-byte limit"
                )
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("quality scorecard changed while reading")
        if len(payload) != before.st_size:
            raise ValueError("quality scorecard size changed while reading")
        observed_bytes = len(payload)
        observed_sha256 = hashlib.sha256(payload).hexdigest()
        if observed_bytes != expected_bytes or not hmac.compare_digest(
            observed_sha256,
            expected_sha256,
        ):
            raise ValueError("quality scorecard metadata does not match")
        parsed = json.loads(
            payload.decode("utf-8", errors="strict"),
            parse_constant=_reject_non_finite_json,
        )
    finally:
        os.close(descriptor)
    if not isinstance(parsed, dict):
        raise ValueError("quality scorecard payload must be a JSON object")
    return parsed


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"Non-finite JSON value is not allowed: {value}")


def _read_scorecard_summary(
    root_fd: int,
    *,
    storage_key: Any,
    path_rel: Any,
    expected_sha256: Any,
    expected_bytes: Any,
    max_bytes: int,
) -> _ScorecardReadResult:
    if path_rel is None:
        return _ScorecardReadResult(state="missing", summary=None)
    try:
        normalized_storage_key = validate_storage_key(
            str(storage_key),
            field="storage_key",
        )
        normalized_path = validate_relative_path(
            str(path_rel),
            field="path_rel",
        )
        normalized_sha256, normalized_bytes = _validate_scorecard_metadata(
            expected_sha256,
            expected_bytes,
        )
    except ManifestValidationError:
        return _ScorecardReadResult(state="invalid", summary=None)
    except ValueError:
        return _ScorecardReadResult(state="invalid", summary=None)

    try:
        payload = _read_scorecard_payload(
            root_fd,
            storage_key=normalized_storage_key,
            path_rel=normalized_path,
            expected_sha256=normalized_sha256,
            expected_bytes=normalized_bytes,
            max_bytes=max_bytes,
        )
    except FileNotFoundError:
        return _ScorecardReadResult(state="missing", summary=None)
    except OSError:
        return _ScorecardReadResult(state="invalid", summary=None)
    except (RecursionError, ValueError):
        return _ScorecardReadResult(state="invalid", summary=None)

    try:
        return _ScorecardReadResult(
            state="scored",
            summary=_parse_scorecard_summary(payload),
        )
    except ValueError:
        return _ScorecardReadResult(state="invalid", summary=None)


def _validate_scorecard_metadata(
    expected_sha256: Any,
    expected_bytes: Any,
) -> tuple[str, int]:
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise ValueError("quality scorecard content_sha256 is invalid")
    if (
        isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes < 0
    ):
        raise ValueError("quality scorecard bytes is invalid")
    return expected_sha256, expected_bytes


def _parse_scorecard_summary(payload: dict[str, Any]) -> _ScorecardSummary:
    raw_schema_version = payload.get("schema_version")
    if (
        isinstance(raw_schema_version, bool)
        or not isinstance(raw_schema_version, int)
        or raw_schema_version != 1
    ):
        raise ValueError("quality scorecard schema_version must be 1")
    if payload.get("kind") != "lecture_stt_quality_scorecard":
        raise ValueError("quality scorecard kind is invalid")
    raw_score = payload.get("quality_score")
    if isinstance(raw_score, bool) or not isinstance(raw_score, int) or not 0 <= raw_score <= 100:
        raise ValueError("quality scorecard quality_score must be an integer between 0 and 100")
    health = payload.get("health")
    if health not in {"good", "warn", "bad"}:
        raise ValueError("quality scorecard health is invalid")
    metrics = payload.get("metrics")
    if metrics is not None and not isinstance(metrics, dict):
        raise ValueError("quality scorecard metrics must be an object")
    timings = payload.get("timings")
    if timings is not None and not isinstance(timings, dict):
        raise ValueError("quality scorecard timings must be an object")
    audio_duration_sec = _optional_non_negative_float(
        None if metrics is None else metrics.get("audio_duration_sec"),
        field="metrics.audio_duration_sec",
    )
    total_processing_sec = _optional_non_negative_float(
        None if timings is None else timings.get("total_sec"),
        field="timings.total_sec",
    )
    return _ScorecardSummary(
        score=raw_score,
        health=str(health),
        audio_duration_sec=audio_duration_sec,
        total_processing_sec=total_processing_sec,
    )


def _optional_non_negative_float(value: Any, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    try:
        numeric = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field} must be within the supported range") from exc
    if (
        not math.isfinite(numeric)
        or numeric < 0
        or numeric > _MAX_DURATION_SEC
    ):
        raise ValueError(
            f"{field} must be between 0 and {_MAX_DURATION_SEC}"
        )
    return numeric


def _bucket_index(window: _WindowSpec, event_at: datetime) -> int:
    if window.bucket == "hour":
        delta = event_at - window.start_at
        return min(len(window.bucket_starts) - 1, max(0, int(delta.total_seconds() // 3600)))
    delta = event_at.date() - window.start_at.date()
    return min(len(window.bucket_starts) - 1, max(0, delta.days))


def _quality_band(score: int) -> str:
    if score >= 90:
        return "90-100"
    if score >= 80:
        return "80-89"
    if score >= 70:
        return "70-79"
    if score >= 60:
        return "60-69"
    return "0-59"


def _classification_distribution_key(
    context_type: str | None,
    source: str | None,
) -> tuple[str | None, str | None]:
    if context_type is None or source is None:
        return None, None
    return context_type, source


def _classification_distribution_payload(
    counts: dict[tuple[str | None, str | None], int],
) -> list[dict[str, Any]]:
    rows = [
        {
            "context_type": context_type,
            "source": source,
            "label": (
                "미분류"
                if context_type is None and source is None
                else CONTEXT_LABELS.get(str(context_type), str(context_type))
            ),
            "count": count,
        }
        for (context_type, source), count in counts.items()
    ]
    has_unclassified = any(
        row["context_type"] is None and row["source"] is None
        for row in rows
    )
    if not has_unclassified:
        rows.append(
            {
                "context_type": None,
                "source": None,
                "label": "미분류",
                "count": 0,
            }
        )
    rows.sort(
        key=lambda item: (
            item["context_type"] is None and item["source"] is None,
            -int(item["count"]),
            "" if item["context_type"] is None else str(item["context_type"]),
            "" if item["source"] is None else str(item["source"]),
        )
    )
    return rows


def _quality_distribution_payload(
    counts: dict[str, int],
) -> list[dict[str, Any]]:
    return [
        {
            "band": band,
            "label": QUALITY_BAND_LABELS[band],
            "min": min_score,
            "max": max_score,
            "count": int(counts.get(band, 0)),
        }
        for band, min_score, max_score in QUALITY_BAND_RANGES
    ]


def _status_distribution_payload(
    counts: dict[str, int],
) -> list[dict[str, Any]]:
    return [
        {
            "status": status,
            "count": int(counts.get(status, 0)),
        }
        for status in STATUSES
    ]


def _needs_attention(row: _RowSummary) -> bool:
    if row.status in {"queued", "processing", "needs_review", "error"}:
        return True
    if row.health in {"warn", "bad"}:
        return True
    return row.context_type is None or row.classification_source is None
