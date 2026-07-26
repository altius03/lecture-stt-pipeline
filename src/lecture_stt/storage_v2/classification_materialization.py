from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import date, datetime
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import stat
from typing import Any, Iterator, Mapping

from lecture_stt.shared.paths import repo_root
from lecture_stt.storage_v2.manifest import (
    ManifestValidationError,
    validate_manifest,
    validate_relative_path,
    validate_storage_key,
)
from lecture_stt.storage_v2.repository import connect_v2, require_v2_schema
from lecture_stt.storage_v2.verifier import verify_library


PLAN_SCHEMA_VERSION = "storage-v2/classification-materialization-plan@1"
RESULT_SCHEMA_VERSION = "storage-v2/classification-materialization-result@1"
CONTEXT_PROVENANCE_SCHEMA_VERSION = (
    "storage-v2/timetable-materialized-context@1"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MATERIALIZATION_PLAN_JSON_MAX_BYTES = 64 * 1024
MATERIALIZED_CONTEXT_JSON_MAX_BYTES = 16 * 1024
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
_WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_PLAN_FIELDS = {
    "schema_version",
    "proposal_id",
    "recording_id",
    "storage_key",
    "proposal_updated_at",
    "confirmation_plan_sha256",
    "confirmed_at",
    "previous",
    "target",
    "manifest",
    "expected_count",
    "materialization",
}
_TITLE_FIELDS = {
    "id",
    "value",
    "source",
    "locale",
    "confidence",
}
_CONTEXT_FIELDS = {
    "id",
    "type",
    "label",
    "semester",
    "course_name",
    "course_code",
    "session_date",
    "period_label",
    "period_index",
    "source",
    "context_json",
}
_PROVENANCE_FIELDS = {
    "schema_version",
    "proposal_id",
    "schedule_entry_id",
    "confirmation_plan_sha256",
    "weekday",
    "start_time",
    "end_time",
    "classroom",
}


class ClassificationMaterializationError(RuntimeError):
    """Base error for canonical timetable metadata materialization."""


class ClassificationMaterializationNotFoundError(
    ClassificationMaterializationError
):
    """The requested proposal, database, record, or manifest does not exist."""


class ClassificationMaterializationConflictError(
    ClassificationMaterializationError
):
    """Persisted DB or filesystem state does not match the guarded plan."""


class ClassificationMaterializationWriteDisabledError(
    ClassificationMaterializationError
):
    """A canonical materialization write was attempted without every guard."""


class ClassificationMaterializationRecoveryRequiredError(
    ClassificationMaterializationError
):
    """A prepared or applied write exists and needs replay or investigation."""


class ClassificationMaterializationPostCommitVerificationError(
    ClassificationMaterializationRecoveryRequiredError
):
    """Canonical writes committed, but their final verification did not pass."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _manifest_bytes(payload: Mapping[str, Any]) -> bytes:
    validate_manifest(payload)
    content = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(content) > _MAX_MANIFEST_BYTES:
        raise ManifestValidationError(
            "Recording manifest exceeds the 2 MiB metadata limit"
        )
    return content


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _require_positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ClassificationMaterializationConflictError(
            f"{field} must be a positive integer"
        )
    return value


def _validate_digest(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ClassificationMaterializationConflictError(
            f"{field} must be a canonical SHA-256"
        )
    return value


def _validate_optional_text(
    value: Any,
    *,
    field: str,
    max_length: int,
    required: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise ClassificationMaterializationConflictError(
                f"{field} must be a non-empty string"
            )
        return None
    if (
        not isinstance(value, str)
        or len(value) > max_length
        or (required and not value)
    ):
        raise ClassificationMaterializationConflictError(
            f"{field} has an invalid text value"
        )
    return value


def _validate_confidence(value: Any, *, field: str) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ClassificationMaterializationConflictError(
            f"{field} must be null or a finite number between 0 and 1"
        )


def _validate_title_payload(
    value: Any,
    *,
    field: str,
    require_id: bool,
) -> None:
    expected_fields = _TITLE_FIELDS if require_id else _TITLE_FIELDS - {"id"}
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ClassificationMaterializationConflictError(
            f"{field} has an invalid schema"
        )
    if require_id:
        _require_positive_int(value.get("id"), field=f"{field}.id")
    _validate_optional_text(
        value.get("value"),
        field=f"{field}.value",
        max_length=512,
        required=True,
    )
    if value.get("source") not in _TITLE_SOURCES:
        raise ClassificationMaterializationConflictError(
            f"{field}.source is invalid"
        )
    _validate_optional_text(
        value.get("locale"),
        field=f"{field}.locale",
        max_length=64,
    )
    _validate_confidence(value.get("confidence"), field=f"{field}.confidence")


def _validate_context_payload(
    value: Any,
    *,
    field: str,
    require_id: bool,
    require_provenance: bool,
) -> None:
    expected_fields = (
        _CONTEXT_FIELDS - {"context_json"}
        if require_id
        else _CONTEXT_FIELDS - {"id"}
    )
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ClassificationMaterializationConflictError(
            f"{field} has an invalid schema"
        )
    if require_id:
        _require_positive_int(value.get("id"), field=f"{field}.id")
    if value.get("type") not in _CONTEXT_TYPES:
        raise ClassificationMaterializationConflictError(
            f"{field}.type is invalid"
        )
    if value.get("source") not in _CONTEXT_SOURCES:
        raise ClassificationMaterializationConflictError(
            f"{field}.source is invalid"
        )
    for name, limit in (
        ("label", 256),
        ("semester", 128),
        ("course_name", 256),
        ("course_code", 128),
        ("period_label", 128),
    ):
        _validate_optional_text(
            value.get(name),
            field=f"{field}.{name}",
            max_length=limit,
        )
    session_date = _validate_optional_text(
        value.get("session_date"),
        field=f"{field}.session_date",
        max_length=10,
    )
    if session_date is not None:
        try:
            date.fromisoformat(session_date)
        except ValueError as exc:
            raise ClassificationMaterializationConflictError(
                f"{field}.session_date must be an ISO date"
            ) from exc
    period_index = value.get("period_index")
    if period_index is not None and (
        isinstance(period_index, bool)
        or not isinstance(period_index, int)
        or period_index <= 0
    ):
        raise ClassificationMaterializationConflictError(
            f"{field}.period_index must be null or a positive integer"
        )
    if require_provenance and value.get("type") != "class_session":
        raise ClassificationMaterializationConflictError(
            f"{field}.type must be class_session"
        )


def _validate_plan_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _PLAN_FIELDS:
        raise ClassificationMaterializationConflictError(
            "Stored materialization plan has an invalid top-level schema"
        )
    if payload.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ClassificationMaterializationConflictError(
            "Stored materialization plan schema_version is unsupported"
        )
    _require_positive_int(payload.get("proposal_id"), field="proposal_id")
    _require_positive_int(payload.get("recording_id"), field="recording_id")
    if not isinstance(payload.get("storage_key"), str):
        raise ClassificationMaterializationConflictError(
            "Stored materialization plan has an invalid storage_key"
        )
    try:
        validate_storage_key(payload["storage_key"])
    except ValueError as exc:
        raise ClassificationMaterializationConflictError(
            "Stored materialization plan has an invalid storage_key"
        ) from exc
    for field in ("proposal_updated_at", "confirmed_at"):
        if (
            not isinstance(payload.get(field), str)
            or not payload[field]
            or len(payload[field]) > 128
        ):
            raise ClassificationMaterializationConflictError(
                f"Stored materialization plan {field} is invalid"
            )
        try:
            datetime.fromisoformat(payload[field].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ClassificationMaterializationConflictError(
                f"Stored materialization plan {field} is invalid"
            ) from exc
    _validate_digest(
        payload["confirmation_plan_sha256"],
        field="confirmation_plan_sha256",
    )
    if payload.get("expected_count") != 1:
        raise ClassificationMaterializationConflictError(
            "Stored materialization plan expected_count must equal 1"
        )
    if payload.get("materialization") != "canonical_title_context_manifest":
        raise ClassificationMaterializationConflictError(
            "Stored materialization plan mode is invalid"
        )

    previous = payload.get("previous")
    target = payload.get("target")
    manifest = payload.get("manifest")
    if (
        not isinstance(previous, dict)
        or set(previous) != {"title", "context"}
        or not isinstance(target, dict)
        or set(target) != {"title", "context"}
        or not isinstance(manifest, dict)
        or set(manifest)
        != {"relpath", "previous_sha256", "materialized_sha256"}
    ):
        raise ClassificationMaterializationConflictError(
            "Stored materialization plan metadata shape is invalid"
        )
    previous_title = previous["title"]
    target_title = target["title"]
    previous_context = previous["context"]
    target_context = target["context"]
    _validate_title_payload(
        previous_title,
        field="previous.title",
        require_id=True,
    )
    _validate_title_payload(
        target_title,
        field="target.title",
        require_id=False,
    )
    _validate_context_payload(
        previous_context,
        field="previous.context",
        require_id=True,
        require_provenance=False,
    )
    _validate_context_payload(
        target_context,
        field="target.context",
        require_id=False,
        require_provenance=True,
    )
    if (
        target_title.get("source") != "schedule"
        or target_context.get("source") != "schedule_import"
    ):
        raise ClassificationMaterializationConflictError(
            "Stored materialization target sources are invalid"
        )
    provenance = target_context.get("context_json")
    if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_FIELDS:
        raise ClassificationMaterializationConflictError(
            "Stored materialization context provenance is invalid"
        )
    if (
        provenance.get("schema_version")
        != CONTEXT_PROVENANCE_SCHEMA_VERSION
        or provenance.get("proposal_id") != payload["proposal_id"]
        or provenance.get("confirmation_plan_sha256")
        != payload["confirmation_plan_sha256"]
    ):
        raise ClassificationMaterializationConflictError(
            "Stored materialization context provenance does not match its plan"
        )
    _require_positive_int(
        provenance.get("schedule_entry_id"),
        field="target.context.context_json.schedule_entry_id",
    )
    if provenance.get("weekday") not in _WEEKDAYS:
        raise ClassificationMaterializationConflictError(
            "Stored materialization context provenance weekday is invalid"
        )
    start_time = provenance.get("start_time")
    end_time = provenance.get("end_time")
    if (
        not isinstance(start_time, str)
        or not _TIME_RE.fullmatch(start_time)
        or not isinstance(end_time, str)
        or not _TIME_RE.fullmatch(end_time)
        or start_time >= end_time
    ):
        raise ClassificationMaterializationConflictError(
            "Stored materialization context provenance time range is invalid"
        )
    _validate_optional_text(
        provenance.get("classroom"),
        field="target.context.context_json.classroom",
        max_length=256,
        required=True,
    )
    if (
        target_context.get("label") != target_context.get("course_name")
        or target_context.get("semester") is None
        or target_context.get("course_name") is None
        or target_context.get("session_date") is None
        or target_context.get("period_label") is None
    ):
        raise ClassificationMaterializationConflictError(
            "Stored materialization target class metadata is incomplete"
        )
    if not isinstance(manifest.get("relpath"), str):
        raise ClassificationMaterializationConflictError(
            "Stored materialization manifest path is invalid"
        )
    try:
        validate_relative_path(
            manifest["relpath"],
            field="manifest.relpath",
        )
    except ValueError as exc:
        raise ClassificationMaterializationConflictError(
            "Stored materialization manifest path is invalid"
        ) from exc
    _validate_digest(
        manifest.get("previous_sha256"),
        field="manifest.previous_sha256",
    )
    _validate_digest(
        manifest.get("materialized_sha256"),
        field="manifest.materialized_sha256",
    )
    return payload


def validate_materialization_plan_payload(payload: Any) -> dict[str, Any]:
    """Validate the closed metadata-only journal plan contract."""

    return _validate_plan_payload(payload)


def materialization_plan_sha256(payload: Any) -> str:
    """Return the digest of one validated journal plan payload."""

    return _sha256_json(validate_materialization_plan_payload(payload))


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _regular_read_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _file_snapshot(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _fsync_directory_fd(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        unsupported = {
            errno.EINVAL,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        if exc.errno not in unsupported:
            raise


@contextmanager
def _locked_records_root(
    records_root: Path | str,
) -> Iterator[tuple[Path, int]]:
    raw = Path(records_root).expanduser()
    if raw.is_symlink():
        raise ValueError(f"Records root must not be a symlink: {raw}")
    try:
        resolved = raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ClassificationMaterializationNotFoundError(
            "Storage v2 records root is not available"
        ) from exc
    unsafe = {
        Path("/").resolve(),
        Path.home().resolve(),
        repo_root().resolve(),
    }
    if resolved in unsafe or not resolved.is_dir():
        raise ValueError(f"Refusing unsafe records root: {resolved}")
    root_fd = os.open(resolved, _directory_flags())
    baseline = _file_snapshot(os.fstat(root_fd))
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX)
        current = _file_snapshot(os.stat(resolved, follow_symlinks=False))
        if current[:2] != baseline[:2] or not stat.S_ISDIR(current[2]):
            raise ClassificationMaterializationConflictError(
                "Records root identity changed while locking"
            )
        yield resolved, root_fd
        after = _file_snapshot(os.stat(resolved, follow_symlinks=False))
        if after[:2] != baseline[:2] or not stat.S_ISDIR(after[2]):
            raise ClassificationMaterializationConflictError(
                "Records root identity changed during materialization"
            )
    finally:
        try:
            fcntl.flock(root_fd, fcntl.LOCK_UN)
        finally:
            os.close(root_fd)


def _open_manifest_parent(
    root_fd: int,
    storage_key: str,
    manifest_relpath: str,
) -> tuple[int, str]:
    validate_storage_key(storage_key)
    validate_relative_path(manifest_relpath, field="manifest_relpath")
    record_fd = os.open(storage_key, _directory_flags(), dir_fd=root_fd)
    current_fd = record_fd
    try:
        parts = PurePosixPath(manifest_relpath).parts
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                _directory_flags(),
                dir_fd=current_fd,
            )
            if current_fd != record_fd:
                os.close(current_fd)
            current_fd = next_fd
        if current_fd == record_fd:
            return os.dup(record_fd), parts[-1]
        result = os.dup(current_fd)
        return result, parts[-1]
    finally:
        if current_fd != record_fd:
            os.close(current_fd)
        os.close(record_fd)


def _read_manifest_from_parent(
    parent_fd: int,
    filename: str,
    *,
    storage_key: str,
) -> tuple[dict[str, Any], bytes, str, tuple[int, ...]]:
    try:
        manifest_fd = os.open(
            filename,
            _regular_read_flags(),
            dir_fd=parent_fd,
        )
    except FileNotFoundError as exc:
        raise ClassificationMaterializationNotFoundError(
            f"Recording manifest is missing for {storage_key}"
        ) from exc
    try:
        before = os.fstat(manifest_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > _MAX_MANIFEST_BYTES
        ):
            raise ClassificationMaterializationConflictError(
                "Recording manifest must be a bounded single-link regular file"
            )
        chunks: list[bytes] = []
        total = 0
        while total <= _MAX_MANIFEST_BYTES:
            chunk = os.read(
                manifest_fd,
                min(64 * 1024, _MAX_MANIFEST_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(manifest_fd)
        path_stat = os.stat(
            filename,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            total > _MAX_MANIFEST_BYTES
            or _file_snapshot(before) != _file_snapshot(after)
            or _file_snapshot(after) != _file_snapshot(path_stat)
        ):
            raise ClassificationMaterializationConflictError(
                "Recording manifest changed while reading"
            )
        raw = b"".join(chunks)
        try:
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ManifestValidationError(
                    "Recording manifest root must be an object"
                )
            validate_manifest(parsed)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ClassificationMaterializationConflictError(
                "Recording manifest is invalid"
            ) from exc
        if parsed.get("storage_key") != storage_key:
            raise ClassificationMaterializationConflictError(
                "Recording manifest storage_key does not match its DB row"
            )
        return parsed, raw, _digest_bytes(raw), _file_snapshot(after)
    finally:
        os.close(manifest_fd)


def _read_record_manifest(
    root_fd: int,
    *,
    storage_key: str,
    manifest_relpath: str,
) -> tuple[dict[str, Any], bytes, str, tuple[int, ...]]:
    parent_fd, filename = _open_manifest_parent(
        root_fd,
        storage_key,
        manifest_relpath,
    )
    try:
        return _read_manifest_from_parent(
            parent_fd,
            filename,
            storage_key=storage_key,
        )
    finally:
        os.close(parent_fd)


def _replace_record_manifest(
    root_fd: int,
    *,
    storage_key: str,
    manifest_relpath: str,
    expected_sha256: str,
    payload: Mapping[str, Any],
) -> str:
    target_bytes = _manifest_bytes(payload)
    target_sha256 = _digest_bytes(target_bytes)
    parent_fd, filename = _open_manifest_parent(
        root_fd,
        storage_key,
        manifest_relpath,
    )
    temp_name = (
        f".{filename}.materialize-{os.getpid()}-{secrets.token_hex(8)}.tmp"
    )
    temp_created = False
    try:
        _payload, _raw, current_sha256, _snapshot = _read_manifest_from_parent(
            parent_fd,
            filename,
            storage_key=storage_key,
        )
        if current_sha256 == target_sha256:
            return target_sha256
        if current_sha256 != expected_sha256:
            raise ClassificationMaterializationConflictError(
                "Recording manifest changed after the materialization plan"
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        temp_fd = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
        temp_created = True
        try:
            view = memoryview(target_bytes)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short manifest write")
                view = view[written:]
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)

        _current, _raw, still_current_sha256, _snapshot = (
            _read_manifest_from_parent(
                parent_fd,
                filename,
                storage_key=storage_key,
            )
        )
        if still_current_sha256 != expected_sha256:
            raise ClassificationMaterializationConflictError(
                "Recording manifest changed immediately before replacement"
            )
        os.replace(
            temp_name,
            filename,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temp_created = False
        _fsync_directory_fd(parent_fd)
        _written, _raw, written_sha256, _snapshot = (
            _read_manifest_from_parent(
                parent_fd,
                filename,
                storage_key=storage_key,
            )
        )
        if written_sha256 != target_sha256:
            raise ClassificationMaterializationConflictError(
                "Materialized manifest digest does not match its plan"
            )
        return written_sha256
    finally:
        if temp_created:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
                _fsync_directory_fd(parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _require_database_integrity(conn: sqlite3.Connection) -> None:
    quick_check = [
        str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()
    ]
    if quick_check != ["ok"]:
        raise ClassificationMaterializationConflictError(
            "Storage v2 database failed quick_check"
        )
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ClassificationMaterializationConflictError(
            "Storage v2 database has foreign key violations"
        )


def _proposal_row(
    conn: sqlite3.Connection,
    proposal_id: int,
    *,
    require_active_schedule: bool,
) -> sqlite3.Row:
    if (
        isinstance(proposal_id, bool)
        or not isinstance(proposal_id, int)
        or proposal_id <= 0
    ):
        raise ValueError("proposal_id must be a positive integer")
    row = conn.execute(
        """
        SELECT
            proposal.*,
            recording.storage_key,
            recording.manifest_relpath,
            recording.archived_at AS recording_archived_at,
            review.status AS review_status,
            entry.schedule_import_id AS proposal_schedule_import_id,
            selection.schedule_import_id AS active_schedule_import_id,
            entry.course_name AS entry_course_name,
            entry.course_code AS entry_course_code,
            entry.weekday AS entry_weekday,
            entry.start_time AS entry_start_time,
            entry.end_time AS entry_end_time,
            entry.period_label AS entry_period_label,
            entry.period_index AS entry_period_index,
            entry.classroom AS entry_classroom,
            entry.semester AS entry_semester
        FROM recording_classification_proposals AS proposal
        JOIN recordings AS recording
          ON recording.id = proposal.recording_id
        JOIN review_items AS review
          ON review.id = proposal.review_item_id
         AND review.recording_id = proposal.recording_id
        LEFT JOIN schedule_entries AS entry
          ON entry.id = proposal.schedule_entry_id
         AND entry.semester = proposal.semester
        LEFT JOIN schedule_semester_selections AS selection
          ON selection.semester = proposal.semester
        WHERE proposal.id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise ClassificationMaterializationNotFoundError(
            f"Classification proposal not found: {proposal_id}"
        )
    if row["recording_archived_at"] is not None:
        raise ClassificationMaterializationConflictError(
            "Archived recordings cannot receive canonical classification metadata"
        )
    if (
        str(row["status"]) != "confirmed"
        or str(row["review_status"]) != "resolved"
        or str(row["classification_reason"]) != "unique_time_match"
        or row["schedule_entry_id"] is None
        or row["confirmation_plan_sha256"] is None
        or row["confirmed_at"] is None
    ):
        raise ClassificationMaterializationConflictError(
            "Only resolved, confirmed unique timetable matches can be materialized"
        )
    confirmation_digest = _validate_digest(
        row["confirmation_plan_sha256"],
        field="confirmation_plan_sha256",
    )
    proposal_to_entry_fields = (
        ("semester", "entry_semester"),
        ("course_name", "entry_course_name"),
        ("course_code", "entry_course_code"),
        ("weekday", "entry_weekday"),
        ("start_time", "entry_start_time"),
        ("end_time", "entry_end_time"),
        ("period_label", "entry_period_label"),
        ("period_index", "entry_period_index"),
        ("classroom", "entry_classroom"),
    )
    mismatched = [
        proposal_field
        for proposal_field, entry_field in proposal_to_entry_fields
        if row[proposal_field] != row[entry_field]
    ]
    if mismatched:
        raise ClassificationMaterializationConflictError(
            "Confirmed proposal no longer matches its timetable entry: "
            + ", ".join(sorted(mismatched))
        )
    if require_active_schedule and (
        row["proposal_schedule_import_id"] is None
        or row["active_schedule_import_id"] is None
        or int(row["proposal_schedule_import_id"])
        != int(row["active_schedule_import_id"])
    ):
        raise ClassificationMaterializationConflictError(
            "Timetable changed after this classification was confirmed"
        )
    if confirmation_digest != str(row["confirmation_plan_sha256"]):
        raise ClassificationMaterializationConflictError(
            "Classification confirmation digest is not canonical"
        )
    return row


def _exact_selected_row(
    conn: sqlite3.Connection,
    *,
    table: str,
    recording_id: int,
    flag: str,
    label: str,
) -> sqlite3.Row:
    rows = conn.execute(
        f"""
        SELECT *
        FROM {table}
        WHERE recording_id = ? AND {flag} = 1
        ORDER BY id
        LIMIT 2
        """,
        (recording_id,),
    ).fetchall()
    if len(rows) != 1:
        raise ClassificationMaterializationConflictError(
            f"Recording must have exactly one {label}"
        )
    return rows[0]


def _title_payload(
    row: Mapping[str, Any],
    *,
    include_id: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "value": str(row["title"]),
        "source": str(row["title_source"]),
        "locale": _optional_text(row["locale"]),
        "confidence": (
            None if row["confidence"] is None else float(row["confidence"])
        ),
    }
    if include_id:
        payload = {"id": int(row["id"]), **payload}
    return payload


def _context_payload(
    row: Mapping[str, Any],
    *,
    include_id: bool,
    context_json: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": str(row["context_type"]),
        "label": _optional_text(row["label"]),
        "semester": _optional_text(row["semester"]),
        "course_name": _optional_text(row["course_name"]),
        "course_code": _optional_text(row["course_code"]),
        "session_date": _optional_text(row["session_date"]),
        "period_label": _optional_text(row["period_label"]),
        "period_index": (
            None
            if row["period_index"] is None
            else int(row["period_index"])
        ),
        "source": str(row["source"]),
    }
    if context_json is not None:
        payload["context_json"] = dict(context_json)
    if include_id:
        payload = {"id": int(row["id"]), **payload}
    return payload


def _manifest_title(title: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "value": str(title["value"]),
        "source": str(title["source"]),
    }


def _manifest_context(context: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "type": str(context["type"]),
        "source": str(context["source"]),
    }
    for field in (
        "label",
        "semester",
        "course_name",
        "course_code",
        "session_date",
        "period_label",
    ):
        if context.get(field) is not None:
            payload[field] = context[field]
    return payload


def _require_manifest_matches_previous(
    manifest: Mapping[str, Any],
    *,
    previous_title: Mapping[str, Any],
    previous_context: Mapping[str, Any],
) -> None:
    if manifest.get("title") != _manifest_title(previous_title):
        raise ClassificationMaterializationConflictError(
            "Recording manifest title does not match the current DB title"
        )
    manifest_context = manifest.get("context")
    if not isinstance(manifest_context, Mapping):
        raise ClassificationMaterializationConflictError(
            "Recording manifest context is invalid"
        )
    expected_context = _manifest_context(previous_context)
    context_fields = (
        "type",
        "label",
        "semester",
        "course_name",
        "course_code",
        "session_date",
        "period_label",
        "source",
    )
    if any(
        manifest_context.get(field) != expected_context.get(field)
        for field in context_fields
    ):
        raise ClassificationMaterializationConflictError(
            "Recording manifest context does not match the selected DB context"
        )


def _target_metadata(
    proposal: sqlite3.Row,
    *,
    locale: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    confidence = (
        None
        if proposal["confidence"] is None
        else float(proposal["confidence"])
    )
    if confidence is not None and (
        not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0
    ):
        raise ClassificationMaterializationConflictError(
            "Classification proposal confidence is invalid"
        )
    target_title = {
        "value": str(proposal["proposed_title"]),
        "source": "schedule",
        "locale": locale,
        "confidence": confidence,
    }
    provenance = {
        "schema_version": CONTEXT_PROVENANCE_SCHEMA_VERSION,
        "proposal_id": int(proposal["id"]),
        "schedule_entry_id": int(proposal["schedule_entry_id"]),
        "confirmation_plan_sha256": str(
            proposal["confirmation_plan_sha256"]
        ),
        "weekday": str(proposal["weekday"]),
        "start_time": str(proposal["start_time"]),
        "end_time": str(proposal["end_time"]),
        "classroom": str(proposal["classroom"]),
    }
    target_context = {
        "type": str(proposal["context_type"]),
        "label": _optional_text(proposal["label"]),
        "semester": str(proposal["semester"]),
        "course_name": _optional_text(proposal["course_name"]),
        "course_code": _optional_text(proposal["course_code"]),
        "session_date": _optional_text(proposal["session_date"]),
        "period_label": _optional_text(proposal["period_label"]),
        "period_index": (
            None
            if proposal["period_index"] is None
            else int(proposal["period_index"])
        ),
        "source": "schedule_import",
        "context_json": provenance,
    }
    return target_title, target_context


def _journal_row(
    conn: sqlite3.Connection,
    proposal_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM recording_classification_materializations
        WHERE proposal_id = ?
        """,
        (proposal_id,),
    ).fetchone()


def _prepared_for_recording(
    conn: sqlite3.Connection,
    recording_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT proposal_id, materialization_plan_sha256
        FROM recording_classification_materializations
        WHERE recording_id = ? AND state = 'prepared'
        """,
        (recording_id,),
    ).fetchone()


def _reject_title_materialization_history(
    conn: sqlite3.Connection,
    recording_id: int,
) -> None:
    if conn.execute(
        """
        SELECT 1
        FROM recording_title_materializations
        WHERE recording_id = ?
        LIMIT 1
        """,
        (recording_id,),
    ).fetchone() is not None:
        raise ClassificationMaterializationConflictError(
            "Timetable and content-title materialization histories cannot be "
            "mixed in this CLI-first revision"
        )


def _plan_result(
    payload: Mapping[str, Any],
    *,
    state: str,
) -> dict[str, Any]:
    validated = _validate_plan_payload(dict(payload))
    return {
        **validated,
        "mode": "read_only",
        "plan_sha256": _sha256_json(validated),
        "materialization_state": state,
    }


def _new_plan_from_connection(
    conn: sqlite3.Connection,
    root_fd: int,
    proposal_id: int,
) -> dict[str, Any]:
    _require_database_integrity(conn)
    proposal = _proposal_row(
        conn,
        proposal_id,
        require_active_schedule=True,
    )
    _reject_title_materialization_history(
        conn,
        int(proposal["recording_id"]),
    )
    existing = _journal_row(conn, proposal_id)
    if existing is not None:
        return _stored_plan_from_connection(
            conn,
            root_fd,
            proposal_id,
            journal=existing,
        )
    prepared = _prepared_for_recording(conn, int(proposal["recording_id"]))
    if prepared is not None:
        raise ClassificationMaterializationConflictError(
            "Recording already has another prepared materialization"
        )
    recording_id = int(proposal["recording_id"])
    previous_title_row = _exact_selected_row(
        conn,
        table="recording_titles",
        recording_id=recording_id,
        flag="is_current",
        label="current title",
    )
    previous_context_row = _exact_selected_row(
        conn,
        table="recording_contexts",
        recording_id=recording_id,
        flag="is_selected",
        label="selected context",
    )
    previous_title = _title_payload(previous_title_row, include_id=True)
    previous_context = _context_payload(
        previous_context_row,
        include_id=True,
    )
    storage_key = str(proposal["storage_key"])
    manifest_relpath = str(proposal["manifest_relpath"])
    manifest, _raw, previous_manifest_sha256, _snapshot = (
        _read_record_manifest(
            root_fd,
            storage_key=storage_key,
            manifest_relpath=manifest_relpath,
        )
    )
    _require_manifest_matches_previous(
        manifest,
        previous_title=previous_title,
        previous_context=previous_context,
    )
    target_title, target_context = _target_metadata(
        proposal,
        locale=_optional_text(previous_title_row["locale"]),
    )
    target_manifest = copy.deepcopy(manifest)
    target_manifest["title"] = _manifest_title(target_title)
    target_manifest["context"] = _manifest_context(target_context)
    materialized_manifest_sha256 = _digest_bytes(
        _manifest_bytes(target_manifest)
    )
    digest_payload = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "proposal_id": int(proposal["id"]),
        "recording_id": recording_id,
        "storage_key": storage_key,
        "proposal_updated_at": str(proposal["updated_at"]),
        "confirmation_plan_sha256": str(
            proposal["confirmation_plan_sha256"]
        ),
        "confirmed_at": str(proposal["confirmed_at"]),
        "previous": {
            "title": previous_title,
            "context": previous_context,
        },
        "target": {
            "title": target_title,
            "context": target_context,
        },
        "manifest": {
            "relpath": manifest_relpath,
            "previous_sha256": previous_manifest_sha256,
            "materialized_sha256": materialized_manifest_sha256,
        },
        "expected_count": 1,
        "materialization": "canonical_title_context_manifest",
    }
    return _plan_result(digest_payload, state="not_prepared")


def _row_by_owned_id(
    conn: sqlite3.Connection,
    *,
    table: str,
    row_id: int,
    recording_id: int,
    label: str,
) -> sqlite3.Row:
    row = conn.execute(
        f"SELECT * FROM {table} WHERE id = ? AND recording_id = ?",
        (row_id, recording_id),
    ).fetchone()
    if row is None:
        raise ClassificationMaterializationConflictError(
            f"Materialization {label} row is missing or belongs to another recording"
        )
    return row


def _stored_plan_from_connection(
    conn: sqlite3.Connection,
    root_fd: int,
    proposal_id: int,
    *,
    journal: sqlite3.Row | None = None,
) -> dict[str, Any]:
    journal_row = journal or _journal_row(conn, proposal_id)
    if journal_row is None:
        raise ClassificationMaterializationNotFoundError(
            f"Materialization journal not found for proposal {proposal_id}"
        )
    raw_plan_json = str(journal_row["plan_json"])
    if (
        len(raw_plan_json.encode("utf-8"))
        > MATERIALIZATION_PLAN_JSON_MAX_BYTES
    ):
        raise ClassificationMaterializationConflictError(
            "Stored materialization plan JSON exceeds its metadata limit"
        )
    try:
        raw_plan = json.loads(raw_plan_json)
    except (TypeError, ValueError) as exc:
        raise ClassificationMaterializationConflictError(
            "Stored materialization plan JSON is invalid"
        ) from exc
    plan = _validate_plan_payload(raw_plan)
    plan_sha256 = _sha256_json(plan)
    if (
        plan_sha256 != str(journal_row["materialization_plan_sha256"])
        or int(journal_row["proposal_id"]) != int(plan["proposal_id"])
        or int(journal_row["recording_id"]) != int(plan["recording_id"])
        or int(journal_row["previous_title_id"])
        != int(plan["previous"]["title"]["id"])
        or int(journal_row["previous_context_id"])
        != int(plan["previous"]["context"]["id"])
        or str(journal_row["confirmation_plan_sha256"])
        != str(plan["confirmation_plan_sha256"])
        or str(journal_row["previous_manifest_sha256"])
        != str(plan["manifest"]["previous_sha256"])
        or str(journal_row["materialized_manifest_sha256"])
        != str(plan["manifest"]["materialized_sha256"])
    ):
        raise ClassificationMaterializationConflictError(
            "Materialization journal does not match its stored plan"
        )
    proposal = _proposal_row(
        conn,
        proposal_id,
        require_active_schedule=False,
    )
    _reject_title_materialization_history(conn, int(plan["recording_id"]))
    if (
        int(proposal["recording_id"]) != int(plan["recording_id"])
        or str(proposal["storage_key"]) != str(plan["storage_key"])
        or str(proposal["manifest_relpath"])
        != str(plan["manifest"]["relpath"])
        or str(proposal["updated_at"]) != str(plan["proposal_updated_at"])
        or str(proposal["confirmation_plan_sha256"])
        != str(plan["confirmation_plan_sha256"])
        or str(proposal["confirmed_at"]) != str(plan["confirmed_at"])
    ):
        raise ClassificationMaterializationConflictError(
            "Confirmed proposal changed after materialization was prepared"
        )

    recording_id = int(plan["recording_id"])
    previous_title = _row_by_owned_id(
        conn,
        table="recording_titles",
        row_id=int(journal_row["previous_title_id"]),
        recording_id=recording_id,
        label="previous title",
    )
    previous_context = _row_by_owned_id(
        conn,
        table="recording_contexts",
        row_id=int(journal_row["previous_context_id"]),
        recording_id=recording_id,
        label="previous context",
    )
    materialized_title = _row_by_owned_id(
        conn,
        table="recording_titles",
        row_id=int(journal_row["materialized_title_id"]),
        recording_id=recording_id,
        label="target title",
    )
    materialized_context = _row_by_owned_id(
        conn,
        table="recording_contexts",
        row_id=int(journal_row["materialized_context_id"]),
        recording_id=recording_id,
        label="target context",
    )
    expected_target_title, expected_target_context = _target_metadata(
        proposal,
        locale=_optional_text(previous_title["locale"]),
    )
    if (
        plan["target"]["title"] != expected_target_title
        or plan["target"]["context"] != expected_target_context
    ):
        raise ClassificationMaterializationConflictError(
            "Confirmed proposal metadata changed after materialization was prepared"
        )
    if _title_payload(previous_title, include_id=True) != plan["previous"]["title"]:
        raise ClassificationMaterializationConflictError(
            "Previous title row no longer matches the materialization plan"
        )
    if (
        _context_payload(previous_context, include_id=True)
        != plan["previous"]["context"]
    ):
        raise ClassificationMaterializationConflictError(
            "Previous context row no longer matches the materialization plan"
        )
    if (
        _title_payload(materialized_title, include_id=False)
        != plan["target"]["title"]
    ):
        raise ClassificationMaterializationConflictError(
            "Target title row no longer matches the materialization plan"
        )
    raw_context_json = str(materialized_context["context_json"])
    if (
        len(raw_context_json.encode("utf-8"))
        > MATERIALIZED_CONTEXT_JSON_MAX_BYTES
    ):
        raise ClassificationMaterializationConflictError(
            "Target context provenance JSON exceeds its metadata limit"
        )
    try:
        materialized_context_json = json.loads(raw_context_json)
    except (TypeError, ValueError) as exc:
        raise ClassificationMaterializationConflictError(
            "Target context provenance JSON is invalid"
        ) from exc
    if (
        _context_payload(
            materialized_context,
            include_id=False,
            context_json=materialized_context_json,
        )
        != plan["target"]["context"]
    ):
        raise ClassificationMaterializationConflictError(
            "Target context row no longer matches the materialization plan"
        )

    state = str(journal_row["state"])
    if state == "prepared":
        expected_flags = (1, 1, 0, 0)
    elif state == "applied":
        expected_flags = (0, 0, 1, 1)
    else:
        raise ClassificationMaterializationConflictError(
            "Materialization journal state is invalid"
        )
    actual_flags = (
        int(previous_title["is_current"]),
        int(previous_context["is_selected"]),
        int(materialized_title["is_current"]),
        int(materialized_context["is_selected"]),
    )
    successors = conn.execute(
        """
        SELECT *
        FROM recording_classification_materializations
        WHERE recording_id = ?
          AND previous_title_id = ?
          AND previous_context_id = ?
          AND id > ?
        ORDER BY id
        LIMIT 2
        """,
        (
            recording_id,
            int(journal_row["materialized_title_id"]),
            int(journal_row["materialized_context_id"]),
            int(journal_row["id"]),
        ),
    ).fetchall()
    if successors:
        if state != "applied" or len(successors) != 1:
            raise ClassificationMaterializationConflictError(
                "Materialization revision successor chain is invalid"
            )
        successor = successors[0]
        successor_expected_flags = (
            (0, 0, 1, 1)
            if str(successor["state"]) == "prepared"
            else (0, 0, 0, 0)
        )
        if actual_flags != successor_expected_flags:
            raise ClassificationMaterializationConflictError(
                "Superseded materialization selection state is inconsistent"
            )
        _stored_plan_from_connection(
            conn,
            root_fd,
            int(successor["proposal_id"]),
            journal=successor,
        )
        return _plan_result(plan, state=state)
    if actual_flags != expected_flags:
        raise ClassificationMaterializationConflictError(
            "Materialization title/context selection state is inconsistent"
        )

    manifest, _raw, manifest_sha256, _snapshot = _read_record_manifest(
        root_fd,
        storage_key=str(plan["storage_key"]),
        manifest_relpath=str(plan["manifest"]["relpath"]),
    )
    previous_sha256 = str(plan["manifest"]["previous_sha256"])
    materialized_sha256 = str(plan["manifest"]["materialized_sha256"])
    if state == "prepared":
        if manifest_sha256 not in {previous_sha256, materialized_sha256}:
            raise ClassificationMaterializationConflictError(
                "Prepared materialization manifest has an unknown digest"
            )
        expected_metadata = (
            plan["previous"]
            if manifest_sha256 == previous_sha256
            else plan["target"]
        )
    else:
        if manifest_sha256 != materialized_sha256:
            raise ClassificationMaterializationConflictError(
                "Applied materialization manifest does not match its plan"
            )
        expected_metadata = plan["target"]
    _require_manifest_matches_previous(
        manifest,
        previous_title=expected_metadata["title"],
        previous_context=expected_metadata["context"],
    )
    return _plan_result(plan, state=state)


def _readonly_connection(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    if not path.exists():
        raise ClassificationMaterializationNotFoundError(
            "Storage v2 database is not available"
        )
    conn = connect_v2(path, readonly=True)
    try:
        require_v2_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _plan_payload_only(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {field: plan[field] for field in _PLAN_FIELDS}


def _verification_issue_codes(result: Mapping[str, Any]) -> str:
    codes = sorted(
        {
            str(issue.get("code") or "unknown")
            for issue in result.get("issues", [])
            if isinstance(issue, Mapping)
        }
    )
    return ", ".join(codes[:20]) or "unknown"


def plan_classification_materialization(
    db_path: Path | str,
    records_root: Path | str,
    proposal_id: int,
) -> dict[str, Any]:
    """Build a root-bound, metadata-only canonical materialization plan."""

    path = Path(db_path).expanduser()
    if not path.exists():
        raise ClassificationMaterializationNotFoundError(
            "Storage v2 database is not available"
        )
    with _locked_records_root(records_root) as (_root, root_fd):
        conn = _readonly_connection(path)
        try:
            conn.execute("BEGIN")
            existing = _journal_row(conn, proposal_id)
            if existing is not None:
                return _stored_plan_from_connection(
                    conn,
                    root_fd,
                    proposal_id,
                    journal=existing,
                )
        finally:
            conn.close()

    verification = verify_library(path, records_root)
    if not verification.get("ok"):
        raise ClassificationMaterializationConflictError(
            "Storage v2 library must verify cleanly before a new "
            "materialization plan: "
            + _verification_issue_codes(verification)
        )

    with _locked_records_root(records_root) as (_root, root_fd):
        conn = _readonly_connection(path)
        try:
            conn.execute("BEGIN")
            return _new_plan_from_connection(
                conn,
                root_fd,
                proposal_id,
            )
        finally:
            conn.close()


def _validate_apply_guards(
    plan: Mapping[str, Any],
    *,
    expected_count: int,
    expected_plan_sha256: str,
    materializations_enabled: bool,
    allow_write: bool,
) -> str:
    if not materializations_enabled:
        raise ClassificationMaterializationWriteDisabledError(
            "Classification materializations are disabled"
        )
    if not allow_write:
        raise ClassificationMaterializationWriteDisabledError(
            "Classification materialization requires allow_write=True"
        )
    if isinstance(expected_count, bool) or expected_count != 1:
        raise ClassificationMaterializationConflictError(
            "Classification materialization expected_count must equal 1"
        )
    normalized_digest = str(expected_plan_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized_digest):
        raise ValueError(
            "expected_plan_sha256 must be a canonical SHA-256"
        )
    if (
        plan.get("expected_count") != expected_count
        or plan.get("plan_sha256") != normalized_digest
    ):
        raise ClassificationMaterializationConflictError(
            "Classification materialization plan changed; create a new plan"
        )
    return normalized_digest


def _prepare_materialization(
    conn: sqlite3.Connection,
    root_fd: int,
    *,
    proposal_id: int,
    expected_plan_sha256: str,
) -> tuple[dict[str, Any], sqlite3.Row, bool]:
    existing = _journal_row(conn, proposal_id)
    if existing is not None:
        stored = _stored_plan_from_connection(
            conn,
            root_fd,
            proposal_id,
            journal=existing,
        )
        if stored["plan_sha256"] != expected_plan_sha256:
            raise ClassificationMaterializationConflictError(
                "Existing materialization journal belongs to another plan"
            )
        return stored, existing, False

    current = _new_plan_from_connection(
        conn,
        root_fd,
        proposal_id,
    )
    if current["plan_sha256"] != expected_plan_sha256:
        raise ClassificationMaterializationConflictError(
            "Classification materialization plan changed before prepare"
        )
    payload = _plan_payload_only(current)
    recording_id = int(payload["recording_id"])
    target_title = payload["target"]["title"]
    target_context = payload["target"]["context"]
    title_cursor = conn.execute(
        """
        INSERT INTO recording_titles(
            recording_id,
            title,
            title_source,
            locale,
            confidence,
            is_current
        )
        VALUES (?, ?, 'schedule', ?, ?, 0)
        """,
        (
            recording_id,
            str(target_title["value"]),
            target_title["locale"],
            target_title["confidence"],
        ),
    )
    materialized_title_id = int(title_cursor.lastrowid)
    context_cursor = conn.execute(
        """
        INSERT INTO recording_contexts(
            recording_id,
            context_type,
            label,
            semester,
            course_name,
            course_code,
            session_date,
            period_label,
            period_index,
            context_json,
            source,
            is_selected
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'schedule_import', 0)
        """,
        (
            recording_id,
            str(target_context["type"]),
            target_context["label"],
            target_context["semester"],
            target_context["course_name"],
            target_context["course_code"],
            target_context["session_date"],
            target_context["period_label"],
            target_context["period_index"],
            _canonical_json(target_context["context_json"]),
        ),
    )
    materialized_context_id = int(context_cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO recording_classification_materializations(
            proposal_id,
            recording_id,
            previous_title_id,
            previous_context_id,
            materialized_title_id,
            materialized_context_id,
            confirmation_plan_sha256,
            materialization_plan_sha256,
            previous_manifest_sha256,
            materialized_manifest_sha256,
            plan_json,
            state
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared')
        """,
        (
            proposal_id,
            recording_id,
            int(payload["previous"]["title"]["id"]),
            int(payload["previous"]["context"]["id"]),
            materialized_title_id,
            materialized_context_id,
            str(payload["confirmation_plan_sha256"]),
            expected_plan_sha256,
            str(payload["manifest"]["previous_sha256"]),
            str(payload["manifest"]["materialized_sha256"]),
            _canonical_json(payload),
        ),
    )
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ClassificationMaterializationConflictError(
            "Classification materialization prepare would violate foreign keys"
        )
    prepared = _journal_row(conn, proposal_id)
    assert prepared is not None
    return current, prepared, True


def _target_manifest_from_plan(
    root_fd: int,
    plan: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    manifest, _raw, current_sha256, _snapshot = _read_record_manifest(
        root_fd,
        storage_key=str(plan["storage_key"]),
        manifest_relpath=str(plan["manifest"]["relpath"]),
    )
    previous_sha256 = str(plan["manifest"]["previous_sha256"])
    materialized_sha256 = str(plan["manifest"]["materialized_sha256"])
    if current_sha256 == materialized_sha256:
        _require_manifest_matches_previous(
            manifest,
            previous_title=plan["target"]["title"],
            previous_context=plan["target"]["context"],
        )
        return manifest, current_sha256
    if current_sha256 != previous_sha256:
        raise ClassificationMaterializationConflictError(
            "Recording manifest is neither the planned previous nor target revision"
        )
    _require_manifest_matches_previous(
        manifest,
        previous_title=plan["previous"]["title"],
        previous_context=plan["previous"]["context"],
    )
    target_manifest = copy.deepcopy(manifest)
    target_manifest["title"] = _manifest_title(plan["target"]["title"])
    target_manifest["context"] = _manifest_context(plan["target"]["context"])
    target_sha256 = _digest_bytes(_manifest_bytes(target_manifest))
    if target_sha256 != materialized_sha256:
        raise ClassificationMaterializationConflictError(
            "Rebuilt target manifest does not match the guarded plan"
        )
    return target_manifest, current_sha256


def _finalize_materialization(
    conn: sqlite3.Connection,
    root_fd: int,
    *,
    proposal_id: int,
    expected_plan_sha256: str,
) -> str:
    journal = _journal_row(conn, proposal_id)
    if journal is None:
        raise ClassificationMaterializationConflictError(
            "Materialization journal disappeared before finalize"
        )
    stored = _stored_plan_from_connection(
        conn,
        root_fd,
        proposal_id,
        journal=journal,
    )
    if stored["plan_sha256"] != expected_plan_sha256:
        raise ClassificationMaterializationConflictError(
            "Materialization journal digest changed before finalize"
        )
    if str(journal["state"]) == "applied":
        return "skipped"

    recording_id = int(journal["recording_id"])
    demoted_title = conn.execute(
        """
        UPDATE recording_titles
        SET is_current = 0
        WHERE id = ? AND recording_id = ? AND is_current = 1
        """,
        (int(journal["previous_title_id"]), recording_id),
    )
    demoted_context = conn.execute(
        """
        UPDATE recording_contexts
        SET is_selected = 0
        WHERE id = ? AND recording_id = ? AND is_selected = 1
        """,
        (int(journal["previous_context_id"]), recording_id),
    )
    promoted_title = conn.execute(
        """
        UPDATE recording_titles
        SET is_current = 1
        WHERE id = ? AND recording_id = ? AND is_current = 0
        """,
        (int(journal["materialized_title_id"]), recording_id),
    )
    promoted_context = conn.execute(
        """
        UPDATE recording_contexts
        SET is_selected = 1
        WHERE id = ? AND recording_id = ? AND is_selected = 0
        """,
        (int(journal["materialized_context_id"]), recording_id),
    )
    if (
        demoted_title.rowcount,
        demoted_context.rowcount,
        promoted_title.rowcount,
        promoted_context.rowcount,
    ) != (1, 1, 1, 1):
        raise ClassificationMaterializationConflictError(
            "Materialization title/context selections changed concurrently"
        )
    journal_update = conn.execute(
        """
        UPDATE recording_classification_materializations
        SET state = 'applied',
            applied_at = CURRENT_TIMESTAMP
        WHERE id = ? AND state = 'prepared' AND applied_at IS NULL
        """,
        (int(journal["id"]),),
    )
    if journal_update.rowcount != 1:
        raise ClassificationMaterializationConflictError(
            "Materialization journal changed concurrently"
        )
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ClassificationMaterializationConflictError(
            "Classification materialization would violate foreign keys"
        )
    return "materialized"


def apply_classification_materialization(
    db_path: Path | str,
    records_root: Path | str,
    proposal_id: int,
    *,
    expected_count: int,
    expected_plan_sha256: str,
    materializations_enabled: bool = False,
    allow_write: bool = False,
) -> dict[str, Any]:
    """Apply or forward-recover one guarded canonical materialization."""

    if not materializations_enabled:
        raise ClassificationMaterializationWriteDisabledError(
            "Classification materializations are disabled"
        )
    if not allow_write:
        raise ClassificationMaterializationWriteDisabledError(
            "Classification materialization requires allow_write=True"
        )
    if isinstance(expected_count, bool) or expected_count != 1:
        raise ClassificationMaterializationConflictError(
            "Classification materialization expected_count must equal 1"
        )
    normalized_digest = str(expected_plan_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized_digest):
        raise ValueError(
            "expected_plan_sha256 must be a canonical SHA-256"
        )
    initial_plan = plan_classification_materialization(
        db_path,
        records_root,
        proposal_id,
    )
    _validate_apply_guards(
        initial_plan,
        expected_count=expected_count,
        expected_plan_sha256=normalized_digest,
        materializations_enabled=materializations_enabled,
        allow_write=allow_write,
    )

    path = Path(db_path).expanduser()
    prepared_now = False
    recovered = initial_plan.get("materialization_state") == "prepared"
    recovery_state_exists = recovered
    action = "skipped"
    with _locked_records_root(records_root) as (_root, root_fd):
        conn = connect_v2(path)
        try:
            require_v2_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            plan, journal, prepared_now = _prepare_materialization(
                conn,
                root_fd,
                proposal_id=proposal_id,
                expected_plan_sha256=normalized_digest,
            )
            if plan["plan_sha256"] != normalized_digest:
                raise ClassificationMaterializationConflictError(
                    "Classification materialization plan changed while locking"
                )
            if str(journal["state"]) == "applied":
                conn.rollback()
                action = "skipped"
            else:
                conn.commit()
                recovery_state_exists = True
                target_manifest, current_manifest_sha256 = (
                    _target_manifest_from_plan(root_fd, plan)
                )
                if current_manifest_sha256 != str(
                    plan["manifest"]["materialized_sha256"]
                ):
                    written_sha256 = _replace_record_manifest(
                        root_fd,
                        storage_key=str(plan["storage_key"]),
                        manifest_relpath=str(plan["manifest"]["relpath"]),
                        expected_sha256=str(
                            plan["manifest"]["previous_sha256"]
                        ),
                        payload=target_manifest,
                    )
                    if written_sha256 != str(
                        plan["manifest"]["materialized_sha256"]
                    ):
                        raise ClassificationMaterializationConflictError(
                            "Materialized manifest digest changed after replacement"
                        )

                conn.execute("BEGIN IMMEDIATE")
                finalized = _finalize_materialization(
                    conn,
                    root_fd,
                    proposal_id=proposal_id,
                    expected_plan_sha256=normalized_digest,
                )
                conn.commit()
                action = (
                    "recovered"
                    if recovered and finalized == "materialized"
                    else finalized
                )
        except BaseException as exc:
            if conn.in_transaction:
                conn.rollback()
            if recovery_state_exists and not isinstance(
                exc,
                ClassificationMaterializationRecoveryRequiredError,
            ):
                raise ClassificationMaterializationRecoveryRequiredError(
                    "Classification materialization has a recoverable prepared "
                    f"state; replay the same guarded plan: {exc}"
                ) from exc
            raise
        finally:
            conn.close()

    try:
        verification = verify_library(path, records_root)
    except BaseException as exc:
        raise ClassificationMaterializationPostCommitVerificationError(
            "Classification metadata committed, but post-materialization "
            f"library verification could not complete: {exc}"
        ) from exc
    if not verification.get("ok"):
        raise ClassificationMaterializationPostCommitVerificationError(
            "Classification metadata committed, but post-materialization "
            "library verification failed; investigate before another write: "
            + _verification_issue_codes(verification)
        )
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "ok": True,
        "action": action,
        "proposal_id": proposal_id,
        "storage_key": str(initial_plan["storage_key"]),
        "expected_count": 1,
        "plan_sha256": normalized_digest,
        "canonical_metadata_changed": action != "skipped",
        "prepared_during_apply": prepared_now,
        "verification": {
            "ok": True,
            "checked_recordings": int(
                verification.get("checked_recordings", 0)
            ),
            "checked_artifacts": int(
                verification.get("checked_artifacts", 0)
            ),
        },
    }
