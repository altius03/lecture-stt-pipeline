from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import stat
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from lecture_stt.downstream.course_storage import (
    ROUTE_KIND_ROUTED,
    ROUTE_KIND_SKIPPED,
    CourseRouteDecision,
    CourseRoutingSource,
    CourseStorageError,
    ensure_course_storage_parent,
    resolve_course_route,
)
from lecture_stt.downstream.postprocess import (
    CORRECTION_PROMPT_VERSION,
    GENERATOR_BACKEND_CODEX,
    GENERATOR_BACKEND_FAKE,
    SUMMARY_PROMPT_VERSION,
    CorrectionArtifacts,
    GeneratorSettings,
    PostprocessExecutionError,
    PostprocessValidationError,
    SummaryArtifacts,
    correction_from_staged_json,
    generate_correction,
    generate_summary,
    summary_from_staged_markdown,
    validate_generator_settings,
)
from lecture_stt.downstream.semester import (
    ActiveSemester,
    DEFAULT_ACTIVE_SEMESTER_PATH,
    load_active_semester,
)
from lecture_stt.shared import db as shared_db
from lecture_stt.shared import utils
from lecture_stt.shared.paths import default_db_path, resolve_config_path, resolve_executable


TRANSCRIPT_DELIVERY_PENDING = "PENDING"
TRANSCRIPT_DELIVERY_DELIVERED = "DELIVERED"
TRANSCRIPT_DELIVERY_UNROUTED = "UNROUTED"
TRANSCRIPT_DELIVERY_CONFLICT = "CONFLICT"
TRANSCRIPT_DELIVERY_ERROR = "ERROR"
TRANSCRIPT_DELIVERY_SKIPPED = "SKIPPED"
TRANSCRIPT_DELIVERY_NEEDS_REVIEW = "NEEDS_REVIEW"
STAGE_BLOCKED = "BLOCKED"
STAGE_PENDING = "PENDING"
STAGE_READY = "READY"
_SEOUL = ZoneInfo("Asia/Seoul")


class TranscriptDeliveryError(RuntimeError):
    """Base error for transcript routing and delivery."""


class TranscriptDeliveryConflictError(TranscriptDeliveryError):
    """The queue already contains a different pinned row for the same source job."""


class TranscriptDeliveryUnsafePathError(TranscriptDeliveryError):
    """A source or destination path is unsafe to read or write."""


@dataclass(frozen=True)
class LegacyJobSource:
    source_job_id: int
    canonical_base: str
    orig_name: str
    transcript_txt_path: Path
    transcript_json_path: Path
    source_txt_sha256: str
    source_json_sha256: str
    profile_key: str | None
    recorded_at: str | None
    created_at: str
    completed_at: str


@dataclass(frozen=True)
class TranscriptPostprocessSettings:
    active_semester_path: Path
    correction_staging_dir: Path
    summary_staging_dir: Path
    generator: GeneratorSettings


def _normalize_text(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value).strip())


def _normalize_token(value: Any) -> str:
    return " ".join(_normalize_text(value).split()).upper()


def _safe_engine_params(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _iso_or_none(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_SEOUL)
    return parsed.isoformat()


def _parse_iso_datetime(value: str | None) -> datetime | None:
    normalized = _iso_or_none(value)
    if normalized is None:
        return None
    return datetime.fromisoformat(normalized)


def _profile_key_from_engine_params(payload: Mapping[str, Any]) -> str | None:
    profile = payload.get("profile")
    if isinstance(profile, Mapping):
        key = profile.get("key")
        if isinstance(key, str) and key.strip():
            return _normalize_text(key)
    if isinstance(profile, str) and profile.strip():
        return _normalize_text(profile)
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        nested_profile = metadata.get("profile")
        if isinstance(nested_profile, Mapping):
            key = nested_profile.get("key")
            if isinstance(key, str) and key.strip():
                return _normalize_text(key)
    return None


def _recorded_at_from_job(row: sqlite3.Row, payload: Mapping[str, Any]) -> str | None:
    metadata = payload.get("metadata")
    timings = payload.get("timings")
    for candidate in (
        payload.get("recorded_at"),
        metadata.get("recorded_at") if isinstance(metadata, Mapping) else None,
        timings.get("started_at") if isinstance(timings, Mapping) else None,
        row["started_at"],
        row["created_at"],
    ):
        normalized = _iso_or_none(candidate)
        if normalized is not None:
            return normalized
    return None


def _completed_at_from_job(row: sqlite3.Row, payload: Mapping[str, Any]) -> str:
    timings = payload.get("timings")
    for candidate in (
        timings.get("ended_at") if isinstance(timings, Mapping) else None,
        row["ended_at"],
        row["updated_at"],
        row["created_at"],
    ):
        normalized = _iso_or_none(candidate)
        if normalized is not None:
            return normalized
    raise TranscriptDeliveryError(f"Job {row['id']} has no usable completion timestamp")


def _assert_no_symlink_components(path: Path, *, field: str) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path.cwd()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current = current / part
        if os.path.lexists(current) and current.is_symlink():
            raise TranscriptDeliveryUnsafePathError(
                f"{field} must not contain symlink components: {current}"
            )


def _validate_existing_directory(path: Path, *, field: str) -> Path:
    _assert_no_symlink_components(path, field=field)
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise TranscriptDeliveryUnsafePathError(f"{field} does not exist: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise TranscriptDeliveryUnsafePathError(f"{field} must be a directory: {path}")
    return path.resolve(strict=True)


def _read_regular_file_bytes(path: Path, *, field: str) -> bytes:
    _assert_no_symlink_components(path, field=field)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise TranscriptDeliveryUnsafePathError(f"{field} does not exist: {path}") from exc
    except OSError as exc:
        raise TranscriptDeliveryUnsafePathError(
            f"{field} must be a regular non-symlink file: {path}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise TranscriptDeliveryUnsafePathError(f"{field} must be a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _active_snapshot_sha256(path: Path) -> str:
    return utils.compute_sha256(path)


def _load_legacy_job_source(conn: sqlite3.Connection, source_job_id: int) -> LegacyJobSource:
    row = conn.execute(
        """
        SELECT
            id,
            status,
            created_at,
            updated_at,
            started_at,
            ended_at,
            orig_name,
            canonical_base,
            transcript_txt_path,
            transcript_json_path,
            engine_params
        FROM jobs
        WHERE id = ?
        """,
        (source_job_id,),
    ).fetchone()
    if row is None:
        raise TranscriptDeliveryError(f"Legacy job not found: {source_job_id}")
    if str(row["status"]) != "DONE":
        raise TranscriptDeliveryError(f"Legacy job is not DONE: {source_job_id}")
    txt_path = Path(str(row["transcript_txt_path"] or "").strip())
    json_path = Path(str(row["transcript_json_path"] or "").strip())
    if not txt_path or not json_path:
        raise TranscriptDeliveryError(f"Legacy job transcript paths are incomplete: {source_job_id}")
    txt_bytes = _read_regular_file_bytes(txt_path, field="source_txt_path")
    json_bytes = _read_regular_file_bytes(json_path, field="source_json_path")
    engine_params = _safe_engine_params(row["engine_params"])
    return LegacyJobSource(
        source_job_id=int(row["id"]),
        canonical_base=_normalize_text(row["canonical_base"]),
        orig_name=_normalize_text(row["orig_name"]),
        transcript_txt_path=txt_path,
        transcript_json_path=json_path,
        source_txt_sha256=_sha256_bytes(txt_bytes),
        source_json_sha256=_sha256_bytes(json_bytes),
        profile_key=_profile_key_from_engine_params(engine_params),
        recorded_at=_recorded_at_from_job(row, engine_params),
        created_at=_normalize_text(row["created_at"]),
        completed_at=_completed_at_from_job(row, engine_params),
    )


def _parse_positive_int(value: Any, *, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TranscriptDeliveryError(f"{field} must be an integer") from exc
    if parsed <= 0:
        raise TranscriptDeliveryError(f"{field} must be greater than 0")
    return parsed


def _parse_bool(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    raise TranscriptDeliveryError(f"{field} must be a boolean")


def load_postprocess_settings(
    delivery_cfg: Mapping[str, Any],
    *,
    base_dir: Path,
    env: Mapping[str, str],
    default_tmp_root: Path,
    allow_fake_backend: bool = False,
) -> TranscriptPostprocessSettings:
    active_raw = delivery_cfg.get("active_semester_manifest")
    if not isinstance(active_raw, str) or not active_raw.strip():
        raise TranscriptDeliveryError(
            "transcript_delivery.active_semester_manifest must be a non-empty path"
        )
    correction_raw = delivery_cfg.get("correction_staging_dir")
    summary_raw = delivery_cfg.get("summary_staging_dir")
    if not isinstance(correction_raw, str) or not correction_raw.strip():
        raise TranscriptDeliveryError(
            "transcript_delivery.correction_staging_dir must be a non-empty path"
        )
    if not isinstance(summary_raw, str) or not summary_raw.strip():
        raise TranscriptDeliveryError(
            "transcript_delivery.summary_staging_dir must be a non-empty path"
        )
    generator_cfg = delivery_cfg.get("generator") or {}
    if not isinstance(generator_cfg, Mapping):
        raise TranscriptDeliveryError("transcript_delivery.generator must be a mapping")
    backend = _normalize_text(generator_cfg.get("backend") or GENERATOR_BACKEND_CODEX).lower()
    if backend == GENERATOR_BACKEND_FAKE and not allow_fake_backend:
        raise TranscriptDeliveryError(
            "transcript_delivery.generator.backend=fake is reserved for internal tests"
        )
    codex_binary: Path | None = None
    if backend == GENERATOR_BACKEND_CODEX:
        binary_raw = generator_cfg.get("codex_binary", "codex")
        if not isinstance(binary_raw, str) or not binary_raw.strip():
            raise TranscriptDeliveryError("transcript_delivery.generator.codex_binary is required")
        codex_binary = Path(
            resolve_executable(binary_raw, base_dir=base_dir, env=env)
        ).expanduser()
    model = _normalize_text(generator_cfg.get("model") or "gpt-5.6-terra")
    reasoning_effort = _normalize_text(generator_cfg.get("reasoning_effort") or "low").lower()
    timeout_sec = _parse_positive_int(
        generator_cfg.get("timeout_sec", 1200),
        field="transcript_delivery.generator.timeout_sec",
    )
    max_attempts = _parse_positive_int(
        generator_cfg.get("max_attempts", 3),
        field="transcript_delivery.generator.max_attempts",
    )
    max_correction_chars = _parse_positive_int(
        generator_cfg.get("max_correction_chars", 100000),
        field="transcript_delivery.generator.max_correction_chars",
    )
    max_summary_chars = _parse_positive_int(
        generator_cfg.get("max_summary_chars", 100000),
        field="transcript_delivery.generator.max_summary_chars",
    )
    temp_root_raw = generator_cfg.get("temp_root")
    temp_root = (
        Path(resolve_config_path(str(temp_root_raw), base_dir=base_dir, env=env))
        if isinstance(temp_root_raw, str) and temp_root_raw.strip()
        else default_tmp_root
    )
    settings = TranscriptPostprocessSettings(
        active_semester_path=Path(
            resolve_config_path(str(active_raw), base_dir=base_dir, env=env)
        ),
        correction_staging_dir=Path(
            resolve_config_path(str(correction_raw), base_dir=base_dir, env=env)
        ),
        summary_staging_dir=Path(
            resolve_config_path(str(summary_raw), base_dir=base_dir, env=env)
        ),
        generator=validate_generator_settings(
            GeneratorSettings(
                backend=backend,
                codex_binary=codex_binary,
                model=model,
                reasoning_effort=reasoning_effort,
                timeout_sec=timeout_sec,
                max_attempts=max_attempts,
                temp_root=temp_root,
                max_correction_chars=max_correction_chars,
                max_summary_chars=max_summary_chars,
            )
        ),
    )
    _validate_existing_directory(settings.correction_staging_dir, field="correction_staging_dir")
    _validate_existing_directory(settings.summary_staging_dir, field="summary_staging_dir")
    load_active_semester(settings.active_semester_path)
    return settings


def _build_queue_fields(
    active: ActiveSemester,
    *,
    active_semester_path: Path,
    active_semester_sha256: str,
    source: LegacyJobSource,
    route: CourseRouteDecision,
    settings: TranscriptPostprocessSettings,
) -> dict[str, Any]:
    status = _delivery_status_for_route(route)
    fields: dict[str, Any] = {
        "storage_key": None,
        "logical_stem": route.logical_stem,
        "semester": active.semester,
        "source_txt_path": source.transcript_txt_path,
        "source_txt_sha256": source.source_txt_sha256,
        "source_json_path": source.transcript_json_path,
        "source_json_sha256": source.source_json_sha256,
        "route_method": route.route_method,
        "active_semester_path": active_semester_path,
        "active_semester_sha256": active_semester_sha256,
        "manifest_source_path": active.manifest_source_path,
        "manifest_source_sha256": active.manifest_source_sha256,
        "activation_cutoff": active.activated_at,
        "status": status,
        "generator_backend": settings.generator.backend,
        "generator_program": str(settings.generator.codex_binary) if settings.generator.codex_binary else None,
        "generator_model": settings.generator.model,
        "generator_reasoning_effort": settings.generator.reasoning_effort,
        "generator_prompt_version": f"{CORRECTION_PROMPT_VERSION}+{SUMMARY_PROMPT_VERSION}",
        "correction_status": STAGE_BLOCKED,
        "summary_status": STAGE_BLOCKED,
        "delivery_status": STAGE_BLOCKED,
        "last_error_code": route.error_code,
        "last_error": route.error_message,
        "last_error_stage": None,
    }
    if route.course is None:
        return fields

    correction_parent = ensure_course_storage_parent(
        settings.correction_staging_dir,
        semester=active.semester,
        course_dir=route.course.course_dir,
        field="correction_staging_dir",
        create=False,
    )
    summary_parent = ensure_course_storage_parent(
        settings.summary_staging_dir,
        semester=active.semester,
        course_dir=route.course.course_dir,
        field="summary_staging_dir",
        create=False,
    )
    fields.update(
        {
            "course_code": route.course.course_code,
            "course_name": route.course.course_name,
            "course_dir": route.course.course_dir,
            "vault_root_path": active.vault_root,
            "semester_root_path": active.semester_root,
            "course_root_path": route.course.course_root,
            "origin_dir_path": route.course.origin_dir,
            "summary_dir_path": route.course.summary_dir,
            "correction_stage_txt_path": correction_parent / f"{route.logical_stem}.txt",
            "correction_stage_json_path": correction_parent / f"{route.logical_stem}.json",
            "summary_stage_md_path": summary_parent / f"{route.logical_stem}.md",
            "destination_txt_path": route.course.origin_dir / f"{route.logical_stem}.txt",
            "destination_json_path": route.course.origin_dir / f"{route.logical_stem}.json",
            "destination_summary_md_path": route.course.summary_dir / f"{route.logical_stem}.md",
            "correction_status": STAGE_PENDING,
            "summary_status": STAGE_BLOCKED,
            "delivery_status": STAGE_BLOCKED,
        }
    )
    return fields


def _delivery_status_for_route(route: CourseRouteDecision) -> str:
    if route.kind == ROUTE_KIND_ROUTED:
        return TRANSCRIPT_DELIVERY_PENDING
    if route.kind == ROUTE_KIND_SKIPPED:
        return TRANSCRIPT_DELIVERY_SKIPPED
    return TRANSCRIPT_DELIVERY_UNROUTED


def _legacy_stage_path(
    settings: TranscriptPostprocessSettings,
    *,
    column: str,
    logical_stem: str,
) -> Path | None:
    if column == "correction_stage_txt_path":
        return settings.correction_staging_dir / f"{logical_stem}.txt"
    if column == "correction_stage_json_path":
        return settings.correction_staging_dir / f"{logical_stem}.json"
    if column == "summary_stage_md_path":
        return settings.summary_staging_dir / f"{logical_stem}.md"
    return None


def _pinned_field_mismatch(
    existing: sqlite3.Row,
    fields: Mapping[str, Any],
    *,
    settings: TranscriptPostprocessSettings,
) -> str | None:
    pinned_columns = [
        "logical_stem",
        "semester",
        "course_code",
        "course_name",
        "course_dir",
        "vault_root_path",
        "semester_root_path",
        "course_root_path",
        "origin_dir_path",
        "summary_dir_path",
        "source_txt_path",
        "source_txt_sha256",
        "source_json_path",
        "source_json_sha256",
        "correction_stage_txt_path",
        "correction_stage_json_path",
        "summary_stage_md_path",
        "destination_txt_path",
        "destination_json_path",
        "destination_summary_md_path",
        "route_method",
        "active_semester_path",
        "active_semester_sha256",
        "manifest_source_path",
        "manifest_source_sha256",
        "activation_cutoff",
        "generator_backend",
        "generator_program",
        "generator_model",
        "generator_reasoning_effort",
        "generator_prompt_version",
    ]
    for column in pinned_columns:
        new_value = fields.get(column)
        current_value = existing[column]
        if new_value is None and current_value is None:
            continue
        if str(new_value) != str(current_value):
            legacy_path = _legacy_stage_path(
                settings,
                column=column,
                logical_stem=str(fields["logical_stem"]),
            )
            if legacy_path is not None and Path(str(current_value)) == legacy_path:
                # Rows pinned before course-scoped staging keep their exact
                # flat paths.  They are processed in place and never migrated.
                continue
            return column
    return None


def enqueue_completed_job(
    conn: sqlite3.Connection,
    *,
    source_job_id: int,
    settings: TranscriptPostprocessSettings,
) -> str | None:
    shared_db.init_transcript_postprocess_jobs_table(conn)
    active = load_active_semester(settings.active_semester_path)
    active_sha256 = _active_snapshot_sha256(settings.active_semester_path)
    source = _load_legacy_job_source(conn, source_job_id)
    completed_at = _parse_iso_datetime(source.completed_at)
    cutoff = _parse_iso_datetime(active.activated_at)
    if completed_at is None or cutoff is None:
        raise TranscriptDeliveryError("active semester activated_at or completed_at is invalid")
    if completed_at < cutoff:
        return None
    route = resolve_course_route(
        active,
        CourseRoutingSource(
            canonical_base=source.canonical_base,
            orig_name=source.orig_name,
            profile_key=source.profile_key,
            recorded_at=source.recorded_at,
        ),
    )
    fields = _build_queue_fields(
        active,
        active_semester_path=settings.active_semester_path,
        active_semester_sha256=active_sha256,
        source=source,
        route=route,
        settings=settings,
    )
    inserted = shared_db.insert_transcript_postprocess_job_if_absent(
        conn,
        source_job_id,
        **fields,
    )
    existing = shared_db.get_transcript_postprocess_job(conn, source_job_id)
    if existing is None:
        raise TranscriptDeliveryError(f"Failed to load queued postprocess row: {source_job_id}")
    mismatch = _pinned_field_mismatch(existing, fields, settings=settings)
    if mismatch is not None:
        raise TranscriptDeliveryConflictError(
            f"Transcript postprocess row already exists with a different pinned {mismatch}: {source_job_id}"
        )
    if inserted:
        return _delivery_status_for_route(route)
    return str(existing["status"])


def _queued_source_job_ids(conn: sqlite3.Connection) -> set[int]:
    try:
        rows = conn.execute("SELECT source_job_id FROM transcript_postprocess_jobs").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {int(row["source_job_id"]) for row in rows}


def _find_done_jobs_missing_queue(
    conn: sqlite3.Connection,
    *,
    active: ActiveSemester,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    queued_ids = _queued_source_job_ids(conn)
    cutoff = _parse_iso_datetime(active.activated_at)
    if cutoff is None:
        raise TranscriptDeliveryError("active semester activated_at is invalid")
    rows = conn.execute(
        """
        SELECT id, created_at, updated_at, started_at, ended_at, engine_params
        FROM jobs
        WHERE status = 'DONE'
        ORDER BY id ASC
        """
    ).fetchall()
    missing: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for row in rows:
        source_job_id = int(row["id"])
        if source_job_id in queued_ids:
            continue
        payload = _safe_engine_params(row["engine_params"])
        try:
            completed_at = _completed_at_from_job(row, payload)
        except TranscriptDeliveryError:
            errors.append(
                {
                    "source_job_id": source_job_id,
                    "status": "error",
                    "error_code": "INVALID_COMPLETION_TIMESTAMP",
                    "error": "TranscriptDeliveryError",
                }
            )
            continue
        completed_dt = _parse_iso_datetime(completed_at)
        if completed_dt is None or completed_dt < cutoff:
            continue
        missing.append({"source_job_id": source_job_id, "completed_at": completed_at})
    return missing, errors


def _bounded_error(exc: BaseException) -> tuple[str, str]:
    return type(exc).__name__.upper(), type(exc).__name__


def reconcile_completed_jobs(
    conn: sqlite3.Connection,
    *,
    settings: TranscriptPostprocessSettings,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    active = load_active_semester(settings.active_semester_path)
    active_sha256 = _active_snapshot_sha256(settings.active_semester_path)
    missing, discovery_errors = _find_done_jobs_missing_queue(conn, active=active)
    success_limit = max(limit, 0) if limit is not None else None
    successful = 0
    results: list[dict[str, Any]] = list(discovery_errors)
    for item in missing:
        # Broken rows are reported but do not consume the success budget.  If
        # the first N legacy rows are permanently broken, a fixed pre-slice
        # would otherwise starve every valid row behind them on every run.
        if success_limit is not None and successful >= success_limit:
            break
        source_job_id = int(item["source_job_id"])
        try:
            if dry_run:
                source = _load_legacy_job_source(conn, source_job_id)
                route = resolve_course_route(
                    active,
                    CourseRoutingSource(
                        canonical_base=source.canonical_base,
                        orig_name=source.orig_name,
                        profile_key=source.profile_key,
                        recorded_at=source.recorded_at,
                    ),
                )
                fields = _build_queue_fields(
                    active,
                    active_semester_path=settings.active_semester_path,
                    active_semester_sha256=active_sha256,
                    source=source,
                    route=route,
                    settings=settings,
                )
                results.append(
                    {
                        "source_job_id": source_job_id,
                        "status": _delivery_status_for_route(route),
                        "logical_stem": fields["logical_stem"],
                        "course_code": fields.get("course_code"),
                        "route_method": fields["route_method"],
                        "dry_run": True,
                    }
                )
                successful += 1
            else:
                status = enqueue_completed_job(conn, source_job_id=source_job_id, settings=settings)
                if status is not None:
                    results.append({"source_job_id": source_job_id, "status": status})
                    successful += 1
        except Exception as exc:
            code, message = _bounded_error(exc)
            results.append(
                {
                    "source_job_id": source_job_id,
                    "status": "error",
                    "error_code": code,
                    "error": message,
                }
            )
    return {
        "queued": sum(1 for item in results if item.get("status") != "error"),
        "errors": sum(1 for item in results if item.get("status") == "error"),
        "dry_run": dry_run,
        "results": results,
    }


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_bytes_no_overwrite(payload: bytes, dst: Path) -> None:
    temp_path = dst.parent / f".{dst.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temp_path, flags, 0o600)
    try:
        try:
            view = memoryview(payload)
            written = 0
            while written < len(view):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise OSError("short write while creating temporary output")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temp_path, dst)
        except FileExistsError as exc:
            raise FileExistsError(str(dst)) from exc
        except OSError as exc:
            if exc.errno not in {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV}:
                raise
            descriptor = os.open(dst, flags, 0o600)
            try:
                view = memoryview(payload)
                written = 0
                while written < len(view):
                    count = os.write(descriptor, view[written:])
                    if count <= 0:
                        raise OSError("short write while creating destination")
                    written += count
                os.fsync(descriptor)
            except BaseException:
                os.close(descriptor)
                dst.unlink(missing_ok=True)
                raise
            else:
                os.close(descriptor)
    finally:
        temp_path.unlink(missing_ok=True)
    _fsync_directory(dst.parent)


def _destination_state(path: Path, *, expected_sha256: str, field: str) -> str:
    if not path.exists():
        return "missing"
    payload = _read_regular_file_bytes(path, field=field)
    digest = _sha256_bytes(payload)
    if digest != expected_sha256:
        return "conflict"
    return "same"


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _load_source_json_payload(payload: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TranscriptDeliveryError("source transcript JSON is invalid") from exc
    if not isinstance(parsed, dict):
        raise TranscriptDeliveryError("source transcript JSON must be an object")
    return parsed


def _ensure_pinned_stage_parent(
    root: Path,
    *,
    semester: str,
    course_dir: str,
    paths: tuple[Path, ...],
    field: str,
) -> None:
    legacy_parent = _validate_existing_directory(root, field=field)
    parents = {path.parent for path in paths}
    if parents == {legacy_parent}:
        # Compatibility boundary for rows pinned before nested staging. Keep
        # writing to their exact flat paths and never silently migrate them.
        return
    course_parent = ensure_course_storage_parent(
        root,
        semester=semester,
        course_dir=course_dir,
        field=field,
        create=True,
    )
    if parents != {course_parent}:
        raise TranscriptDeliveryConflictError(
            f"{field} paths do not match the pinned flat or course-scoped directory"
        )


class TranscriptDeliveryDistributor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: TranscriptPostprocessSettings,
        *,
        dry_run: bool = False,
    ):
        self.conn = conn
        self.settings = settings
        self.dry_run = dry_run
        if not self.dry_run:
            shared_db.init_transcript_postprocess_jobs_table(self.conn)

    def process_pending(self, *, limit: int | None = None) -> dict[str, Any]:
        rows = shared_db.list_transcript_postprocess_jobs(
            self.conn,
            statuses=(TRANSCRIPT_DELIVERY_PENDING,),
            limit=limit,
        )
        results = [self._process_row(row) for row in rows]
        return {
            "processed": len(results),
            "delivered": sum(1 for item in results if item["status"] == TRANSCRIPT_DELIVERY_DELIVERED),
            "conflict": sum(1 for item in results if item["status"] == TRANSCRIPT_DELIVERY_CONFLICT),
            "needs_review": sum(
                1 for item in results if item["status"] == TRANSCRIPT_DELIVERY_NEEDS_REVIEW
            ),
            "error": sum(1 for item in results if item["status"] == TRANSCRIPT_DELIVERY_ERROR),
            "dry_run": self.dry_run,
            "results": results,
        }

    def _fresh_row(self, row: sqlite3.Row) -> sqlite3.Row:
        current = shared_db.get_transcript_postprocess_job(
            self.conn,
            int(row["source_job_id"]),
        )
        return current if current is not None else row

    def _finalize_error(
        self,
        row: sqlite3.Row,
        *,
        status: str,
        error_stage: str,
        error_code: str,
        error_message: str,
        correction_attempt_delta: int = 0,
        summary_attempt_delta: int = 0,
        delivery_attempt_delta: int = 0,
        stage_status: str | None = None,
    ) -> dict[str, Any]:
        row = self._fresh_row(row)
        if not self.dry_run:
            updates: dict[str, Any] = {
                "status": status,
                "correction_attempt_count": int(row["correction_attempt_count"] or 0)
                + correction_attempt_delta,
                "summary_attempt_count": int(row["summary_attempt_count"] or 0)
                + summary_attempt_delta,
                "delivery_attempt_count": int(row["delivery_attempt_count"] or 0)
                + delivery_attempt_delta,
                "error_count": int(row["error_count"] or 0) + 1,
                "last_error_stage": error_stage,
                "last_error_code": error_code,
                "last_error": error_message,
                "last_attempted_at": utils.now_iso(),
                "completed_at": None,
            }
            if stage_status is not None and error_stage in {
                "correction",
                "summary",
                "delivery",
            }:
                updates[f"{error_stage}_status"] = stage_status
            shared_db.update_transcript_postprocess_job(
                self.conn,
                int(row["source_job_id"]),
                **updates,
            )
        return {
            "source_job_id": int(row["source_job_id"]),
            "status": status,
            "error_code": error_code,
            "error": error_message,
        }

    @staticmethod
    def _active_stage(row: sqlite3.Row) -> str:
        if str(row["correction_status"]) != STAGE_READY:
            return "correction"
        if str(row["summary_status"]) != STAGE_READY:
            return "summary"
        return "delivery"

    def _finalize_retryable_error(
        self,
        row: sqlite3.Row,
        *,
        error_code: str,
    ) -> dict[str, Any]:
        row = self._fresh_row(row)
        stage = self._active_stage(row)
        count_column = f"{stage}_attempt_count"
        next_attempt = int(row[count_column] or 0) + 1
        terminal = next_attempt >= self.settings.generator.max_attempts
        deltas = {
            "correction_attempt_delta": 1 if stage == "correction" else 0,
            "summary_attempt_delta": 1 if stage == "summary" else 0,
            "delivery_attempt_delta": 1 if stage == "delivery" else 0,
        }
        return self._finalize_error(
            row,
            status=TRANSCRIPT_DELIVERY_ERROR if terminal else TRANSCRIPT_DELIVERY_PENDING,
            error_stage=stage,
            error_code=error_code,
            error_message=error_code,
            stage_status="ERROR" if terminal else STAGE_PENDING,
            **deltas,
        )

    def _assert_generator_provenance(self, row: sqlite3.Row) -> None:
        expected = {
            "generator_backend": self.settings.generator.backend,
            "generator_program": (
                str(self.settings.generator.codex_binary)
                if self.settings.generator.codex_binary is not None
                else None
            ),
            "generator_model": self.settings.generator.model,
            "generator_reasoning_effort": self.settings.generator.reasoning_effort,
            "generator_prompt_version": (
                f"{CORRECTION_PROMPT_VERSION}+{SUMMARY_PROMPT_VERSION}"
            ),
        }
        for column, value in expected.items():
            current = row[column]
            if current is None and value is None:
                continue
            if str(current) != str(value):
                raise TranscriptDeliveryConflictError(
                    f"queued generator provenance changed: {column}"
                )

    def _process_row(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            self._assert_generator_provenance(row)
            txt_bytes = _read_regular_file_bytes(Path(str(row["source_txt_path"])), field="source_txt_path")
            json_bytes = _read_regular_file_bytes(Path(str(row["source_json_path"])), field="source_json_path")
            if _sha256_bytes(txt_bytes) != str(row["source_txt_sha256"]):
                return self._finalize_error(
                    row,
                    status=TRANSCRIPT_DELIVERY_ERROR,
                    error_stage="correction",
                    error_code="SOURCE_TXT_DRIFT",
                    error_message="source transcript text hash changed after queueing",
                )
            if _sha256_bytes(json_bytes) != str(row["source_json_sha256"]):
                return self._finalize_error(
                    row,
                    status=TRANSCRIPT_DELIVERY_ERROR,
                    error_stage="correction",
                    error_code="SOURCE_JSON_DRIFT",
                    error_message="source transcript JSON hash changed after queueing",
                )
            source_json = _load_source_json_payload(json_bytes)
            correction = self._ensure_correction(row, source_json)
            if correction is None:
                return {
                    "source_job_id": int(row["source_job_id"]),
                    "status": TRANSCRIPT_DELIVERY_PENDING,
                    "dry_run": True,
                }
            summary = self._ensure_summary(row, correction)
            if summary is None:
                return {
                    "source_job_id": int(row["source_job_id"]),
                    "status": TRANSCRIPT_DELIVERY_PENDING,
                    "dry_run": True,
                }
            return self._deliver(row, correction, summary)
        except TranscriptDeliveryConflictError:
            current = self._fresh_row(row)
            return self._finalize_error(
                current,
                status=TRANSCRIPT_DELIVERY_CONFLICT,
                error_stage=self._active_stage(current),
                error_code="POSTPROCESS_CONFLICT",
                error_message="POSTPROCESS_CONFLICT",
                stage_status=TRANSCRIPT_DELIVERY_CONFLICT,
            )
        except PostprocessValidationError:
            current = self._fresh_row(row)
            return self._finalize_error(
                current,
                status=TRANSCRIPT_DELIVERY_NEEDS_REVIEW,
                error_stage=self._active_stage(current),
                error_code="POSTPROCESS_VALIDATION_FAILED",
                error_message="POSTPROCESS_VALIDATION_FAILED",
                stage_status=TRANSCRIPT_DELIVERY_NEEDS_REVIEW,
            )
        except PostprocessExecutionError:
            return self._finalize_retryable_error(
                row,
                error_code="CODEX_EXEC_FAILED",
            )
        except (TranscriptDeliveryError, CourseStorageError) as exc:
            return self._finalize_error(
                row,
                status=TRANSCRIPT_DELIVERY_ERROR,
                error_stage="runtime",
                error_code=type(exc).__name__.upper(),
                error_message=type(exc).__name__,
            )
        except FileExistsError:
            current = self._fresh_row(row)
            stage = self._active_stage(current)
            return self._finalize_error(
                current,
                status=TRANSCRIPT_DELIVERY_CONFLICT,
                error_stage=stage,
                error_code="DESTINATION_CONFLICT",
                error_message="destination file appeared during write",
                stage_status=TRANSCRIPT_DELIVERY_CONFLICT,
            )
        except OSError:
            return self._finalize_retryable_error(
                row,
                error_code="FILESYSTEM_ERROR",
            )

    def _ensure_correction(
        self,
        row: sqlite3.Row,
        source_json: Mapping[str, Any],
    ) -> CorrectionArtifacts | None:
        if str(row["correction_status"]) == STAGE_READY:
            txt_bytes = _read_regular_file_bytes(
                Path(str(row["correction_stage_txt_path"])),
                field="correction_stage_txt_path",
            )
            json_bytes = _read_regular_file_bytes(
                Path(str(row["correction_stage_json_path"])),
                field="correction_stage_json_path",
            )
            if _sha256_bytes(txt_bytes) != str(row["correction_stage_txt_sha256"]):
                raise TranscriptDeliveryError("correction stage txt hash drifted")
            if _sha256_bytes(json_bytes) != str(row["correction_stage_json_sha256"]):
                raise TranscriptDeliveryError("correction stage json hash drifted")
            correction = correction_from_staged_json(source_json, json_bytes)
            if txt_bytes != correction.transcript_txt_bytes:
                raise PostprocessValidationError(
                    "Correction stage TXT does not match its canonical JSON"
                )
            return correction
        if self.dry_run:
            return None

        txt_path = Path(str(row["correction_stage_txt_path"]))
        json_path = Path(str(row["correction_stage_json_path"]))
        if row["course_dir"] is not None:
            _ensure_pinned_stage_parent(
                self.settings.correction_staging_dir,
                semester=str(row["semester"]),
                course_dir=str(row["course_dir"]),
                paths=(txt_path, json_path),
                field="correction_staging_dir",
            )
        pinned_txt_sha = str(row["correction_stage_txt_sha256"] or "")
        pinned_json_sha = str(row["correction_stage_json_sha256"] or "")
        txt_present = _path_entry_exists(txt_path)
        json_present = _path_entry_exists(json_path)
        if bool(pinned_txt_sha) != bool(pinned_json_sha):
            raise TranscriptDeliveryConflictError(
                "correction staging intent is incomplete"
            )
        if pinned_txt_sha and pinned_json_sha:
            if json_present:
                json_bytes = _read_regular_file_bytes(
                    json_path,
                    field="correction_stage_json_path",
                )
                if _sha256_bytes(json_bytes) != pinned_json_sha:
                    raise TranscriptDeliveryConflictError(
                        "correction staging JSON differs from its pinned intent"
                    )
                correction = correction_from_staged_json(source_json, json_bytes)
                if _sha256_bytes(correction.transcript_txt_bytes) != pinned_txt_sha:
                    raise PostprocessValidationError(
                        "Correction staging intent does not match canonical TXT"
                    )
                if txt_present:
                    txt_bytes = _read_regular_file_bytes(
                        txt_path,
                        field="correction_stage_txt_path",
                    )
                    if _sha256_bytes(txt_bytes) != pinned_txt_sha:
                        raise TranscriptDeliveryConflictError(
                            "correction staging TXT differs from its pinned intent"
                        )
                    if txt_bytes != correction.transcript_txt_bytes:
                        raise PostprocessValidationError(
                            "Correction staging TXT does not match canonical JSON"
                        )
                else:
                    _write_bytes_no_overwrite(
                        correction.transcript_txt_bytes,
                        txt_path,
                    )
                self._mark_correction_ready(row, attempt_delta=0)
                return correction
            if txt_present:
                raise TranscriptDeliveryConflictError(
                    "correction staging JSON is missing for an existing TXT"
                )
        elif txt_present or json_present:
            raise TranscriptDeliveryConflictError(
                "correction staging files exist without a pinned intent"
            )

        correction = generate_correction(
            self.settings.generator,
            logical_stem=str(row["logical_stem"]),
            course_name=str(row["course_name"] or ""),
            source_json_payload=source_json,
        )
        txt_sha = _sha256_bytes(correction.transcript_txt_bytes)
        json_sha = _sha256_bytes(correction.transcript_json_bytes)
        shared_db.update_transcript_postprocess_job(
            self.conn,
            int(row["source_job_id"]),
            correction_stage_txt_sha256=txt_sha,
            correction_stage_json_sha256=json_sha,
            last_attempted_at=utils.now_iso(),
        )
        txt_state = _destination_state(
            txt_path,
            expected_sha256=txt_sha,
            field="correction_stage_txt_path",
        )
        json_state = _destination_state(
            json_path,
            expected_sha256=json_sha,
            field="correction_stage_json_path",
        )
        if "conflict" in {txt_state, json_state}:
            raise TranscriptDeliveryConflictError("correction staging conflict")
        created_paths: list[Path] = []
        try:
            if json_state == "missing":
                _write_bytes_no_overwrite(correction.transcript_json_bytes, json_path)
                created_paths.append(json_path)
            if txt_state == "missing":
                _write_bytes_no_overwrite(correction.transcript_txt_bytes, txt_path)
                created_paths.append(txt_path)
        except BaseException:
            for created in reversed(created_paths):
                created.unlink(missing_ok=True)
            raise
        self._mark_correction_ready(row, attempt_delta=1)
        return correction

    def _mark_correction_ready(
        self,
        row: sqlite3.Row,
        *,
        attempt_delta: int,
    ) -> None:
        current = self._fresh_row(row)
        shared_db.update_transcript_postprocess_job(
            self.conn,
            int(current["source_job_id"]),
            correction_status=STAGE_READY,
            summary_status=STAGE_PENDING,
            delivery_status=STAGE_BLOCKED,
            correction_attempt_count=int(current["correction_attempt_count"] or 0)
            + attempt_delta,
            last_error_stage=None,
            last_error_code=None,
            last_error=None,
            last_attempted_at=utils.now_iso(),
        )

    def _ensure_summary(
        self,
        row: sqlite3.Row,
        correction: CorrectionArtifacts,
    ) -> SummaryArtifacts | None:
        if str(row["summary_status"]) == STAGE_READY:
            md_bytes = _read_regular_file_bytes(
                Path(str(row["summary_stage_md_path"])),
                field="summary_stage_md_path",
            )
            if _sha256_bytes(md_bytes) != str(row["summary_stage_md_sha256"]):
                raise TranscriptDeliveryError("summary stage markdown hash drifted")
            return summary_from_staged_markdown(md_bytes)
        if self.dry_run:
            return None

        md_path = Path(str(row["summary_stage_md_path"]))
        if row["course_dir"] is not None:
            _ensure_pinned_stage_parent(
                self.settings.summary_staging_dir,
                semester=str(row["semester"]),
                course_dir=str(row["course_dir"]),
                paths=(md_path,),
                field="summary_staging_dir",
            )
        pinned_md_sha = str(row["summary_stage_md_sha256"] or "")
        md_present = _path_entry_exists(md_path)
        if pinned_md_sha and md_present:
            md_bytes = _read_regular_file_bytes(
                md_path,
                field="summary_stage_md_path",
            )
            if _sha256_bytes(md_bytes) != pinned_md_sha:
                raise TranscriptDeliveryConflictError(
                    "summary staging Markdown differs from its pinned intent"
                )
            summary = summary_from_staged_markdown(md_bytes)
            self._mark_summary_ready(row, attempt_delta=0)
            return summary
        if not pinned_md_sha and md_present:
            raise TranscriptDeliveryConflictError(
                "summary staging Markdown exists without a pinned intent"
            )

        summary = generate_summary(
            self.settings.generator,
            logical_stem=str(row["logical_stem"]),
            course_name=str(row["course_name"] or ""),
            corrected_text=correction.transcript_text,
        )
        md_sha = _sha256_bytes(summary.markdown_bytes)
        shared_db.update_transcript_postprocess_job(
            self.conn,
            int(row["source_job_id"]),
            summary_stage_md_sha256=md_sha,
            last_attempted_at=utils.now_iso(),
        )
        md_state = _destination_state(
            md_path,
            expected_sha256=md_sha,
            field="summary_stage_md_path",
        )
        if md_state == "conflict":
            raise TranscriptDeliveryConflictError("summary staging conflict")
        if md_state == "missing":
            _write_bytes_no_overwrite(summary.markdown_bytes, md_path)
        self._mark_summary_ready(row, attempt_delta=1)
        return summary

    def _mark_summary_ready(
        self,
        row: sqlite3.Row,
        *,
        attempt_delta: int,
    ) -> None:
        current = self._fresh_row(row)
        shared_db.update_transcript_postprocess_job(
            self.conn,
            int(current["source_job_id"]),
            summary_status=STAGE_READY,
            delivery_status=STAGE_PENDING,
            summary_attempt_count=int(current["summary_attempt_count"] or 0)
            + attempt_delta,
            last_error_stage=None,
            last_error_code=None,
            last_error=None,
            last_attempted_at=utils.now_iso(),
        )

    def _deliver(
        self,
        row: sqlite3.Row,
        correction: CorrectionArtifacts,
        summary: SummaryArtifacts,
    ) -> dict[str, Any]:
        origin_dir = _validate_existing_directory(Path(str(row["origin_dir_path"])), field="origin_dir_path")
        summary_dir = _validate_existing_directory(Path(str(row["summary_dir_path"])), field="summary_dir_path")
        destination_txt = Path(str(row["destination_txt_path"]))
        destination_json = Path(str(row["destination_json_path"]))
        destination_summary = Path(str(row["destination_summary_md_path"]))
        states = {
            "txt": _destination_state(
                destination_txt,
                expected_sha256=_sha256_bytes(correction.transcript_txt_bytes),
                field="destination_txt_path",
            ),
            "json": _destination_state(
                destination_json,
                expected_sha256=_sha256_bytes(correction.transcript_json_bytes),
                field="destination_json_path",
            ),
            "summary": _destination_state(
                destination_summary,
                expected_sha256=_sha256_bytes(summary.markdown_bytes),
                field="destination_summary_md_path",
            ),
        }
        if any(state == "conflict" for state in states.values()):
            return self._finalize_error(
                row,
                status=TRANSCRIPT_DELIVERY_CONFLICT,
                error_stage="delivery",
                error_code="DESTINATION_CONFLICT",
                error_message="one or more destination files already exist with different content",
                delivery_attempt_delta=1,
            )
        if self.dry_run:
            return {
                "source_job_id": int(row["source_job_id"]),
                "status": TRANSCRIPT_DELIVERY_PENDING,
                "dry_run": True,
                "states": states,
            }
        created_paths: list[Path] = []
        try:
            if states["txt"] == "missing":
                if destination_txt.parent != origin_dir:
                    raise TranscriptDeliveryError("destination txt parent mismatch")
                _write_bytes_no_overwrite(correction.transcript_txt_bytes, destination_txt)
                created_paths.append(destination_txt)
            if states["json"] == "missing":
                if destination_json.parent != origin_dir:
                    raise TranscriptDeliveryError("destination json parent mismatch")
                _write_bytes_no_overwrite(correction.transcript_json_bytes, destination_json)
                created_paths.append(destination_json)
            if states["summary"] == "missing":
                if destination_summary.parent != summary_dir:
                    raise TranscriptDeliveryError("destination summary parent mismatch")
                _write_bytes_no_overwrite(summary.markdown_bytes, destination_summary)
                created_paths.append(destination_summary)
        except BaseException:
            for created in reversed(created_paths):
                created.unlink(missing_ok=True)
            raise
        shared_db.update_transcript_postprocess_job(
            self.conn,
            int(row["source_job_id"]),
            status=TRANSCRIPT_DELIVERY_DELIVERED,
            correction_status=STAGE_READY,
            summary_status=STAGE_READY,
            delivery_status=TRANSCRIPT_DELIVERY_DELIVERED,
            delivery_attempt_count=int(row["delivery_attempt_count"] or 0) + 1,
            destination_txt_sha256=_sha256_bytes(correction.transcript_txt_bytes),
            destination_json_sha256=_sha256_bytes(correction.transcript_json_bytes),
            destination_summary_md_sha256=_sha256_bytes(summary.markdown_bytes),
            last_error_stage=None,
            last_error_code=None,
            last_error=None,
            last_attempted_at=utils.now_iso(),
            completed_at=utils.now_iso(),
        )
        return {
            "source_job_id": int(row["source_job_id"]),
            "status": TRANSCRIPT_DELIVERY_DELIVERED,
        }


def open_delivery_db(
    path: Path | str = default_db_path(),
    *,
    readonly: bool = False,
) -> sqlite3.Connection:
    db_path = Path(path).expanduser()
    if not readonly:
        conn = shared_db.connect_db(str(db_path))
        shared_db.init_transcript_postprocess_jobs_table(conn)
        return conn

    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    _assert_no_symlink_components(db_path, field="db_path")
    try:
        metadata = db_path.lstat()
    except FileNotFoundError as exc:
        raise TranscriptDeliveryUnsafePathError(
            f"db_path does not exist for read-only dry-run: {db_path}"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise TranscriptDeliveryUnsafePathError(
            f"db_path must be a regular non-symlink file: {db_path}"
        )
    db_path = db_path.resolve(strict=True)
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA query_only = ON")
    return conn
