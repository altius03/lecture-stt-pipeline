from __future__ import annotations

import errno
from datetime import datetime
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from lecture_stt.shared import utils


MANIFEST_SCHEMA_VERSION = "storage-v2/recording-manifest@1"
_SAFE_KEY_RE = re.compile(r"^[0-9A-Za-z_-]+$")
_RFC3339_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T"
    r"\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:\d{2})$"
)
_FORBIDDEN_ARTIFACT_FIELDS = {
    "body",
    "content",
    "raw_text",
    "segments",
    "transcript",
    "transcript_text",
}
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "storage_key",
    "generated_at",
    "recording",
    "source",
    "title",
    "context",
    "jobs",
    "artifacts",
    "legacy",
}
_RECORDING_FIELDS = {
    "original_name_raw",
    "original_name_nfc",
    "created_at",
}
_SOURCE_FIELDS = {
    "path",
    "availability",
    "sha256",
    "bytes",
    "mime_type",
    "marker_sha256",
}
_TITLE_FIELDS = {"value", "source"}
_CONTEXT_FIELDS = {
    "type",
    "label",
    "semester",
    "course_name",
    "course_code",
    "session_date",
    "period_label",
    "source",
}
_JOB_FIELDS = {
    "job_key",
    "status",
    "requested_profile",
    "requested_profile_version",
    "queued_at",
    "started_at",
    "finished_at",
    "engine",
    "artifact_paths",
}
_ENGINE_FIELDS = {
    "name",
    "version",
    "started_at",
    "finished_at",
    "stderr_relpath",
    "log_relpath",
}
_ARTIFACT_FIELDS = {"kind", "path", "sha256", "bytes", "mime_type"}
_LEGACY_FIELDS = {
    "kind",
    "job_id",
    "delivery_key",
    "canonical_base",
    "status",
    "source_fingerprint",
}
_TITLE_SOURCES = {
    "manual",
    "schedule",
    "filename_inference",
    "legacy_import",
    "system",
}
_CONTEXT_TYPES = {
    "general",
    "class_session",
    "daily_note",
    "meeting",
    "memo",
}
_CONTEXT_SOURCES = {
    "manual",
    "schedule_import",
    "filename_inference",
    "legacy_import",
    "system",
}
_JOB_STATUSES = {
    "queued",
    "processing",
    "done",
    "error",
    "needs_review",
    "canceled",
}
_ARTIFACT_KINDS = {
    "source_copy",
    "transcript_raw_text",
    "transcript_segments_json",
    "quality_scorecard",
    "correction_text",
    "correction_json",
    "summary_markdown",
    "summary_json",
    "metadata",
    "log",
    "other",
}
_MAX_JOBS = 64
_MAX_ARTIFACTS = 1024
_MAX_JOB_ARTIFACT_PATHS = 1024


class ManifestValidationError(ValueError):
    """Raised when a manifest could leak content or escape its recording root."""


def validate_storage_key(value: str, *, field: str = "storage_key") -> str:
    key = str(value)
    if not key or not _SAFE_KEY_RE.fullmatch(key):
        raise ManifestValidationError(f"{field} must be a non-empty ASCII path-safe key")
    return key


def validate_relative_path(value: str, *, field: str = "path") -> str:
    raw = str(value)
    if not raw or "\x00" in raw or "\\" in raw:
        raise ManifestValidationError(f"{field} must be a non-empty POSIX relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ManifestValidationError(f"{field} must stay inside the recording root")
    normalized = path.as_posix()
    if normalized != raw:
        raise ManifestValidationError(f"{field} must already be normalized")
    return normalized


def _validate_artifact(entry: Mapping[str, Any], *, index: int) -> None:
    forbidden = _FORBIDDEN_ARTIFACT_FIELDS.intersection(entry)
    if forbidden:
        names = ", ".join(sorted(forbidden))
        raise ManifestValidationError(
            f"artifacts[{index}] contains transcript/content fields: {names}"
        )
    artifact_path = f"artifacts[{index}]"
    _required_string(
        entry,
        "kind",
        path=artifact_path,
        max_length=64,
        allowed=_ARTIFACT_KINDS,
    )
    raw_path = _required_string(
        entry,
        "path",
        path=artifact_path,
        max_length=4096,
    )
    validate_relative_path(raw_path, field=f"{artifact_path}.path")
    sha256 = entry.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ManifestValidationError(f"artifacts[{index}].sha256 must be lowercase SHA-256")
    byte_count = entry.get("bytes")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
        raise ManifestValidationError(f"artifacts[{index}].bytes must be non-negative")
    _optional_string(
        entry,
        "mime_type",
        path=artifact_path,
        max_length=256,
    )


def _reject_forbidden_content_fields(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        forbidden = _FORBIDDEN_ARTIFACT_FIELDS.intersection(
            str(key) for key in value
        )
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise ManifestValidationError(
                f"{path} contains transcript/content fields: {names}"
            )
        for key, child in value.items():
            _reject_forbidden_content_fields(child, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for index, child in enumerate(value):
            _reject_forbidden_content_fields(child, path=f"{path}[{index}]")


def _validate_object_fields(
    value: Any,
    *,
    allowed: set[str],
    path: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestValidationError(f"{path} must be an object")
    unknown = {str(key) for key in value}.difference(allowed)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ManifestValidationError(f"{path} contains unknown fields: {names}")
    return value


def _required_string(
    mapping: Mapping[str, Any],
    key: str,
    *,
    path: str,
    max_length: int,
    allowed: set[str] | None = None,
) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestValidationError(f"{path}.{key} must be a non-empty string")
    if len(value) > max_length:
        raise ManifestValidationError(
            f"{path}.{key} exceeds the {max_length} character limit"
        )
    if allowed is not None and value not in allowed:
        raise ManifestValidationError(f"{path}.{key} has an unsupported value")
    return value


def _optional_string(
    mapping: Mapping[str, Any],
    key: str,
    *,
    path: str,
    max_length: int,
) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestValidationError(f"{path}.{key} must be a string or null")
    if len(value) > max_length:
        raise ManifestValidationError(
            f"{path}.{key} exceeds the {max_length} character limit"
        )
    return value


def _required_nullable_string(
    mapping: Mapping[str, Any],
    key: str,
    *,
    path: str,
    max_length: int,
) -> str | None:
    if key not in mapping:
        raise ManifestValidationError(
            f"{path}.{key} must be present as a string or null"
        )
    return _optional_string(
        mapping,
        key,
        path=path,
        max_length=max_length,
    )


def _validate_array_count(
    value: Sequence[Any],
    *,
    path: str,
    max_items: int,
) -> None:
    if len(value) > max_items:
        raise ManifestValidationError(
            f"{path} exceeds the {max_items} item limit"
        )


def _validate_rfc3339_timestamp(value: str, *, field: str) -> None:
    if not _RFC3339_TIMESTAMP_RE.fullmatch(value):
        raise ManifestValidationError(
            f"{field} must be a timezone-aware RFC3339 timestamp"
        )
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00"
            if value.endswith("Z")
            else value
        )
    except ValueError as exc:
        raise ManifestValidationError(
            f"{field} must be a valid timezone-aware RFC3339 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManifestValidationError(
            f"{field} must include a UTC offset"
        )


def validate_manifest(payload: Mapping[str, Any]) -> None:
    _validate_object_fields(payload, allowed=_TOP_LEVEL_FIELDS, path="$")
    _reject_forbidden_content_fields(payload)
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ManifestValidationError("Unsupported recording manifest schema_version")
    storage_key = _required_string(
        payload,
        "storage_key",
        path="$",
        max_length=255,
    )
    validate_storage_key(storage_key)
    generated_at = _required_string(
        payload,
        "generated_at",
        path="$",
        max_length=64,
    )
    _validate_rfc3339_timestamp(generated_at, field="$.generated_at")

    recording = _validate_object_fields(
        payload.get("recording"),
        allowed=_RECORDING_FIELDS,
        path="recording",
    )
    _required_string(
        recording,
        "original_name_raw",
        path="recording",
        max_length=1024,
    )
    _required_string(
        recording,
        "original_name_nfc",
        path="recording",
        max_length=1024,
    )
    recording_created_at = _required_string(
        recording,
        "created_at",
        path="recording",
        max_length=64,
    )
    _validate_rfc3339_timestamp(
        recording_created_at,
        field="recording.created_at",
    )
    source = _validate_object_fields(
        payload.get("source"),
        allowed=_SOURCE_FIELDS,
        path="source",
    )
    title = _validate_object_fields(
        payload.get("title"),
        allowed=_TITLE_FIELDS,
        path="title",
    )
    _required_string(title, "value", path="title", max_length=512)
    _required_string(
        title,
        "source",
        path="title",
        max_length=64,
        allowed=_TITLE_SOURCES,
    )
    context = _validate_object_fields(
        payload.get("context"),
        allowed=_CONTEXT_FIELDS,
        path="context",
    )
    _required_string(
        context,
        "type",
        path="context",
        max_length=64,
        allowed=_CONTEXT_TYPES,
    )
    _required_string(
        context,
        "source",
        path="context",
        max_length=64,
        allowed=_CONTEXT_SOURCES,
    )
    for key, max_length in (
        ("label", 512),
        ("semester", 128),
        ("course_name", 512),
        ("course_code", 128),
        ("session_date", 64),
        ("period_label", 128),
    ):
        _optional_string(
            context,
            key,
            path="context",
            max_length=max_length,
        )

    source_path_raw = _required_string(
        source,
        "path",
        path="source",
        max_length=4096,
    )
    source_path = validate_relative_path(source_path_raw, field="source.path")
    availability = _required_string(
        source,
        "availability",
        path="source",
        max_length=32,
        allowed={"available", "missing"},
    )
    _optional_string(
        source,
        "mime_type",
        path="source",
        max_length=256,
    )
    if availability == "available":
        source_sha = source.get("sha256")
        if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha):
            raise ManifestValidationError("Available source must have a lowercase SHA-256")
        source_bytes = source.get("bytes")
        if (
            not isinstance(source_bytes, int)
            or isinstance(source_bytes, bool)
            or source_bytes < 0
        ):
            raise ManifestValidationError("Available source must have a non-negative byte count")
        if source.get("marker_sha256") is not None:
            raise ManifestValidationError(
                "Available source marker_sha256 must be null or absent"
            )
    else:
        source_sha = source.get("sha256")
        if source_sha is not None and (
            not isinstance(source_sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", source_sha)
        ):
            raise ManifestValidationError(
                "Missing source sha256 must be null or lowercase SHA-256"
            )
        source_bytes = source.get("bytes")
        if source_bytes is not None and (
            not isinstance(source_bytes, int)
            or isinstance(source_bytes, bool)
            or source_bytes < 0
        ):
            raise ManifestValidationError(
                "Missing source bytes must be null or non-negative"
            )
        marker_sha = source.get("marker_sha256")
        if not isinstance(marker_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", marker_sha):
            raise ManifestValidationError("Missing source must have marker_sha256")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes, bytearray)):
        raise ManifestValidationError("artifacts must be an array")
    _validate_array_count(
        artifacts,
        path="artifacts",
        max_items=_MAX_ARTIFACTS,
    )
    seen_paths: set[str] = set()
    for index, raw_entry in enumerate(artifacts):
        raw_entry = _validate_object_fields(
            raw_entry,
            allowed=_ARTIFACT_FIELDS,
            path=f"artifacts[{index}]",
        )
        _validate_artifact(raw_entry, index=index)
        path = str(raw_entry["path"])
        if path in seen_paths:
            raise ManifestValidationError(f"Duplicate artifact path: {path}")
        seen_paths.add(path)
    if source_path not in seen_paths:
        raise ManifestValidationError("source.path must also be indexed as an artifact")

    jobs = payload.get("jobs")
    if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes, bytearray)):
        raise ManifestValidationError("jobs must be an array")
    _validate_array_count(jobs, path="jobs", max_items=_MAX_JOBS)
    seen_job_keys: set[str] = set()
    for index, raw_job in enumerate(jobs):
        raw_job = _validate_object_fields(
            raw_job,
            allowed=_JOB_FIELDS,
            path=f"jobs[{index}]",
        )
        job_path = f"jobs[{index}]"
        raw_job_key = _required_string(
            raw_job,
            "job_key",
            path=job_path,
            max_length=255,
        )
        job_key = validate_storage_key(
            raw_job_key,
            field=f"{job_path}.job_key",
        )
        if job_key in seen_job_keys:
            raise ManifestValidationError(f"Duplicate job_key: {job_key}")
        seen_job_keys.add(job_key)
        artifact_paths = raw_job.get("artifact_paths")
        if not isinstance(artifact_paths, Sequence) or isinstance(
            artifact_paths,
            (str, bytes, bytearray),
        ):
            raise ManifestValidationError(f"jobs[{index}].artifact_paths must be an array")
        _validate_array_count(
            artifact_paths,
            path=f"{job_path}.artifact_paths",
            max_items=_MAX_JOB_ARTIFACT_PATHS,
        )
        _required_string(
            raw_job,
            "status",
            path=job_path,
            max_length=32,
            allowed=_JOB_STATUSES,
        )
        _optional_string(
            raw_job,
            "requested_profile",
            path=job_path,
            max_length=256,
        )
        _optional_string(
            raw_job,
            "requested_profile_version",
            path=job_path,
            max_length=256,
        )
        queued_at = _required_string(
            raw_job,
            "queued_at",
            path=job_path,
            max_length=64,
        )
        _validate_rfc3339_timestamp(
            queued_at,
            field=f"{job_path}.queued_at",
        )
        for timestamp_field in ("started_at", "finished_at"):
            timestamp_value = _required_nullable_string(
                raw_job,
                timestamp_field,
                path=job_path,
                max_length=64,
            )
            if timestamp_value is not None:
                _validate_rfc3339_timestamp(
                    timestamp_value,
                    field=f"{job_path}.{timestamp_field}",
                )
        engine = _validate_object_fields(
            raw_job.get("engine"),
            allowed=_ENGINE_FIELDS,
            path=f"{job_path}.engine",
        )
        _required_string(
            engine,
            "name",
            path=f"{job_path}.engine",
            max_length=256,
        )
        _optional_string(
            engine,
            "version",
            path=f"{job_path}.engine",
            max_length=256,
        )
        engine_started_at = _required_string(
            engine,
            "started_at",
            path=f"{job_path}.engine",
            max_length=64,
        )
        _validate_rfc3339_timestamp(
            engine_started_at,
            field=f"{job_path}.engine.started_at",
        )
        engine_finished_at = _required_nullable_string(
            engine,
            "finished_at",
            path=f"{job_path}.engine",
            max_length=64,
        )
        if engine_finished_at is not None:
            _validate_rfc3339_timestamp(
                engine_finished_at,
                field=f"{job_path}.engine.finished_at",
            )
        for path_field in ("stderr_relpath", "log_relpath"):
            path_value = _required_nullable_string(
                engine,
                path_field,
                path=f"{job_path}.engine",
                max_length=4096,
            )
            if path_value is not None:
                validate_relative_path(
                    path_value,
                    field=f"{job_path}.engine.{path_field}",
                )
        seen_job_artifact_paths: set[str] = set()
        for path_index, raw_path in enumerate(artifact_paths):
            if not isinstance(raw_path, str) or not raw_path:
                raise ManifestValidationError(
                    f"{job_path}.artifact_paths[{path_index}] "
                    "must be a non-empty string"
                )
            if len(raw_path) > 4096:
                raise ManifestValidationError(
                    f"{job_path}.artifact_paths[{path_index}] "
                    "exceeds the 4096 character limit"
                )
            path = validate_relative_path(
                raw_path,
                field=f"{job_path}.artifact_paths[{path_index}]",
            )
            if path in seen_job_artifact_paths:
                raise ManifestValidationError(
                    f"Duplicate artifact path in jobs[{index}]: {path}"
                )
            seen_job_artifact_paths.add(path)
            if path == source_path:
                raise ManifestValidationError(
                    f"jobs[{index}].artifact_paths must not include source.path"
                )
            expected_job_prefix = f"jobs/{job_key}/"
            if not path.startswith(expected_job_prefix):
                raise ManifestValidationError(
                    f"jobs[{index}].artifact_paths must stay under "
                    f"{expected_job_prefix}"
                )
            if path not in seen_paths:
                raise ManifestValidationError(
                    f"jobs[{index}] references an artifact path not in the index: {path}"
                )

    legacy = _validate_object_fields(
        payload.get("legacy"),
        allowed=_LEGACY_FIELDS,
        path="legacy",
    )
    _required_string(
        legacy,
        "kind",
        path="legacy",
        max_length=32,
        allowed={"job"},
    )
    legacy_job_id = legacy.get("job_id")
    if (
        not isinstance(legacy_job_id, int)
        or isinstance(legacy_job_id, bool)
        or legacy_job_id < 1
    ):
        raise ManifestValidationError("legacy.job_id must be a positive integer")
    delivery_key = _optional_string(
        legacy,
        "delivery_key",
        path="legacy",
        max_length=1024,
    )
    if delivery_key == "":
        raise ManifestValidationError(
            "legacy.delivery_key must be null or a non-empty string"
        )
    _required_string(
        legacy,
        "canonical_base",
        path="legacy",
        max_length=1024,
    )
    _required_string(
        legacy,
        "status",
        path="legacy",
        max_length=64,
    )
    fingerprint = legacy.get("source_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ManifestValidationError("legacy.source_fingerprint must be lowercase SHA-256")


def build_manifest(
    *,
    storage_key: str,
    generated_at: str,
    original_name_raw: str,
    original_name_nfc: str,
    source: Mapping[str, Any],
    title: Mapping[str, Any],
    context: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    artifacts: Sequence[Mapping[str, Any]],
    legacy: Mapping[str, Any],
    recording_created_at: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "storage_key": validate_storage_key(storage_key),
        "generated_at": str(generated_at),
        "recording": {
            "original_name_raw": str(original_name_raw),
            "original_name_nfc": str(original_name_nfc),
            "created_at": str(
                recording_created_at
                if recording_created_at is not None
                else generated_at
            ),
        },
        "source": dict(source),
        "title": dict(title),
        "context": dict(context),
        "jobs": [dict(item) for item in jobs],
        "artifacts": [dict(item) for item in artifacts],
        "legacy": dict(legacy),
    }
    validate_manifest(payload)
    return payload


def write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    validate_manifest(payload)
    target = Path(path)
    if target.exists():
        raise FileExistsError(f"Manifest already exists: {target}")
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(content.encode("utf-8")) > 2 * 1024 * 1024:
        raise ManifestValidationError("Recording manifest exceeds the 2 MiB metadata limit")
    utils.atomic_write(
        target,
        content,
    )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_descriptor = os.open(target.parent, directory_flags)
    try:
        try:
            os.fsync(directory_descriptor)
        except OSError as exc:
            unsupported = {
                errno.EINVAL,
                getattr(errno, "ENOTSUP", errno.EINVAL),
                getattr(errno, "EOPNOTSUPP", errno.EINVAL),
            }
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(directory_descriptor)
