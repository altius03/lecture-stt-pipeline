from __future__ import annotations

from contextlib import contextmanager
import copy
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
from typing import Any, Iterator, Mapping

from lecture_stt.shared.paths import repo_root
from lecture_stt.storage_v2.classification_materialization import (
    _canonical_json,
    _digest_bytes,
    _locked_records_root,
    _manifest_bytes,
    _read_record_manifest,
    _replace_record_manifest,
    _require_database_integrity,
    _verification_issue_codes,
)
from lecture_stt.storage_v2.manifest import (
    ManifestValidationError,
    validate_manifest,
    validate_relative_path,
    validate_storage_key,
)
from lecture_stt.storage_v2.repository import connect_v2, require_v2_schema


PLAN_SCHEMA_VERSION = "storage-v2/historical-transcript-recovery-plan@1"
RESULT_SCHEMA_VERSION = "storage-v2/historical-transcript-recovery-result@1"
JOURNAL_SCHEMA_VERSION = "storage-v2/historical-transcript-recovery-journal@1"
PREPARED_EVENT = "historical_transcript_recovery_prepared"
APPLIED_EVENT = "historical_transcript_recovery_applied"
DEFAULT_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_JOURNAL_BYTES = 256 * 1024
_TRANSCRIPT_KINDS = {
    "transcript_raw_text",
    "transcript_segments_json",
}
_MISSING_TEXT_CODES = {
    "transcript_txt_missing",
    "transcript_txt_path_missing",
}
_MISSING_JSON_CODES = {
    "transcript_json_missing",
    "transcript_json_path_missing",
}
_RECOVERY_TEXT_MIME = (
    "text/plain; charset=utf-8; "
    "provenance=historical-transcript-recovery"
)
_RECOVERY_JSON_MIME = (
    "application/json; provenance=historical-transcript-recovery"
)


class HistoricalTranscriptRecoveryError(RuntimeError):
    """Base error for guarded historical transcript recovery."""


class HistoricalTranscriptRecoveryNotFoundError(
    HistoricalTranscriptRecoveryError
):
    """The requested database, recording, review, or evidence is unavailable."""


class HistoricalTranscriptRecoveryConflictError(
    HistoricalTranscriptRecoveryError
):
    """Current DB, manifest, or transcript evidence differs from the plan."""


class HistoricalTranscriptRecoveryWriteDisabledError(
    HistoricalTranscriptRecoveryError
):
    """A recovery write is missing an explicit capability guard."""


class HistoricalTranscriptRecoveryRequiredError(
    HistoricalTranscriptRecoveryError
):
    """A prepared recovery must be replayed with the same guarded plan."""


class HistoricalTranscriptRecoveryPostCommitVerificationError(
    HistoricalTranscriptRecoveryRequiredError
):
    """Recovery committed, but final library verification did not pass."""


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


def _validate_max_artifact_bytes(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_ARTIFACT_BYTES
    ):
        raise ValueError(
            "max_artifact_bytes must be between 1 and "
            f"{MAX_ARTIFACT_BYTES}"
        )
    return value


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _safe_canonical_base(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise HistoricalTranscriptRecoveryConflictError(
            "Manifest legacy canonical_base is invalid"
        )
    if (
        "\x00" in value
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
        or PurePosixPath(value).name != value
    ):
        raise HistoricalTranscriptRecoveryConflictError(
            "Manifest legacy canonical_base is not a single safe filename"
        )
    return value


@contextmanager
def _locked_historical_root(
    historical_root: Path | str,
) -> Iterator[tuple[Path, int]]:
    raw = Path(historical_root).expanduser()
    try:
        raw_stat = raw.lstat()
    except FileNotFoundError as exc:
        raise HistoricalTranscriptRecoveryNotFoundError(
            "Historical transcript root is not available"
        ) from exc
    if raw.is_symlink() or not stat.S_ISDIR(raw_stat.st_mode):
        raise ValueError(
            "Historical transcript root must be a non-symlink directory"
        )
    resolved = raw.resolve(strict=True)
    if resolved in {
        Path("/").resolve(),
        Path.home().resolve(),
        repo_root().resolve(),
    }:
        raise ValueError("Refusing unsafe historical transcript root")
    root_fd = os.open(resolved, _directory_flags())
    baseline = _file_snapshot(os.fstat(root_fd))
    try:
        fcntl.flock(root_fd, fcntl.LOCK_SH)
        current = _file_snapshot(os.stat(resolved, follow_symlinks=False))
        if current[:4] != baseline[:4] or not stat.S_ISDIR(current[2]):
            raise HistoricalTranscriptRecoveryConflictError(
                "Historical transcript root identity changed while locking"
            )
        yield resolved, root_fd
        after = _file_snapshot(os.stat(resolved, follow_symlinks=False))
        if after[:4] != baseline[:4] or not stat.S_ISDIR(after[2]):
            raise HistoricalTranscriptRecoveryConflictError(
                "Historical transcript root identity changed during recovery"
            )
    finally:
        try:
            fcntl.flock(root_fd, fcntl.LOCK_UN)
        finally:
            os.close(root_fd)


def _read_historical_file(
    root_fd: int,
    filename: str,
    *,
    max_artifact_bytes: int,
    require_json: bool,
) -> tuple[bytes, dict[str, Any]]:
    try:
        descriptor = os.open(filename, _regular_read_flags(), dir_fd=root_fd)
    except (FileNotFoundError, OSError) as exc:
        raise HistoricalTranscriptRecoveryNotFoundError(
            f"Historical transcript evidence is not safely readable: {filename}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > max_artifact_bytes
        ):
            raise HistoricalTranscriptRecoveryConflictError(
                "Historical transcript evidence must be a bounded "
                f"single-link regular file: {filename}"
            )
        payload = bytearray()
        while len(payload) <= max_artifact_bytes:
            chunk = os.read(
                descriptor,
                min(64 * 1024, max_artifact_bytes + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        path_stat = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
        if (
            len(payload) > max_artifact_bytes
            or _file_snapshot(before) != _file_snapshot(after)
            or _file_snapshot(after) != _file_snapshot(path_stat)
        ):
            raise HistoricalTranscriptRecoveryConflictError(
                f"Historical transcript evidence changed while reading: {filename}"
            )
        raw = bytes(payload)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HistoricalTranscriptRecoveryConflictError(
                f"Historical transcript evidence is not UTF-8: {filename}"
            ) from exc
        if require_json:
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                raise HistoricalTranscriptRecoveryConflictError(
                    f"Historical transcript JSON is invalid: {filename}"
                ) from exc
            if not isinstance(parsed, (dict, list)):
                raise HistoricalTranscriptRecoveryConflictError(
                    "Historical transcript JSON root must be an object or array"
                )
        return raw, {
            "filename": filename,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }
    finally:
        os.close(descriptor)


def _read_pair(
    root_fd: int,
    canonical_base: str,
    *,
    max_artifact_bytes: int,
) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    filenames = {
        "transcript_raw_text": f"{canonical_base}.txt",
        "transcript_segments_json": f"{canonical_base}.json",
    }
    payloads: dict[str, bytes] = {}
    evidence: list[dict[str, Any]] = []
    for kind in ("transcript_raw_text", "transcript_segments_json"):
        payload, metadata = _read_historical_file(
            root_fd,
            filenames[kind],
            max_artifact_bytes=max_artifact_bytes,
            require_json=kind == "transcript_segments_json",
        )
        payloads[kind] = payload
        evidence.append({"kind": kind, **metadata})
    return payloads, evidence


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_separated_roots(
    db_path: Path,
    records_root: Path,
    historical_root: Path,
) -> None:
    database = db_path.resolve(strict=True)
    if (
        records_root == historical_root
        or _is_within(records_root, historical_root)
        or _is_within(historical_root, records_root)
    ):
        raise ValueError(
            "Records root and historical transcript root must not overlap"
        )
    if _is_within(database, records_root) or _is_within(
        database, historical_root
    ):
        raise ValueError(
            "Storage v2 database must stay outside both transcript roots"
        )


def _preflight_root_separation(
    db_path: Path,
    records_root: Path | str,
    historical_root: Path | str,
) -> None:
    try:
        resolved_records = Path(records_root).expanduser().resolve(strict=True)
        resolved_historical = (
            Path(historical_root).expanduser().resolve(strict=True)
        )
    except FileNotFoundError as exc:
        raise HistoricalTranscriptRecoveryNotFoundError(
            "Transcript recovery root is not available"
        ) from exc
    _require_separated_roots(
        db_path,
        resolved_records,
        resolved_historical,
    )


def _row_dict(row: sqlite3.Row, fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: row[field] for field in fields}


def _current_evidence(
    conn: sqlite3.Connection,
    storage_key: str,
) -> tuple[dict[str, Any], sqlite3.Row, sqlite3.Row]:
    try:
        normalized_key = validate_storage_key(storage_key)
    except ManifestValidationError as exc:
        raise HistoricalTranscriptRecoveryConflictError(
            "storage_key is invalid"
        ) from exc
    recording_rows = conn.execute(
        """
        SELECT *
        FROM recordings
        WHERE storage_key = ? AND archived_at IS NULL
        """,
        (normalized_key,),
    ).fetchall()
    if len(recording_rows) != 1:
        raise HistoricalTranscriptRecoveryNotFoundError(
            "Exactly one active recording must match storage_key"
        )
    recording = recording_rows[0]
    recording_id = int(recording["id"])

    title_rows = conn.execute(
        """
        SELECT *
        FROM recording_titles
        WHERE recording_id = ? AND is_current = 1
        """,
        (recording_id,),
    ).fetchall()
    context_rows = conn.execute(
        """
        SELECT *
        FROM recording_contexts
        WHERE recording_id = ? AND is_selected = 1
        """,
        (recording_id,),
    ).fetchall()
    job_rows = conn.execute(
        """
        SELECT *
        FROM transcription_jobs
        WHERE recording_id = ?
          AND is_current = 1
          AND archived_at IS NULL
        """,
        (recording_id,),
    ).fetchall()
    if len(title_rows) != 1 or len(context_rows) != 1 or len(job_rows) != 1:
        raise HistoricalTranscriptRecoveryConflictError(
            "Recording must have exactly one current title, context, and job"
        )
    job = job_rows[0]
    if str(job["status"]) != "needs_review":
        raise HistoricalTranscriptRecoveryConflictError(
            "Historical transcript recovery requires a current needs_review job"
        )
    job_id = int(job["id"])

    engine_rows = conn.execute(
        """
        SELECT *
        FROM engine_runs
        WHERE job_id = ?
          AND is_selected = 1
          AND archived_at IS NULL
        """,
        (job_id,),
    ).fetchall()
    if len(engine_rows) != 1:
        raise HistoricalTranscriptRecoveryConflictError(
            "Recovery target must have exactly one selected engine run"
        )
    engine = engine_rows[0]
    if (
        str(engine["provider"]) != "legacy_import"
        or str(engine["status"]) != "succeeded"
    ):
        raise HistoricalTranscriptRecoveryConflictError(
            "Recovery target must be a succeeded legacy_import engine run"
        )

    review_rows = conn.execute(
        """
        SELECT *
        FROM review_items
        WHERE recording_id = ?
          AND job_id = ?
          AND status = 'open'
          AND reason_code = 'legacy_import_review'
        """,
        (recording_id, job_id),
    ).fetchall()
    if len(review_rows) != 1:
        raise HistoricalTranscriptRecoveryConflictError(
            "Recovery requires exactly one open legacy_import_review"
        )
    review = review_rows[0]
    try:
        detail = json.loads(str(review["detail_json"]))
        issues = detail["issues"]
        codes = {
            str(issue["code"])
            for issue in issues
            if isinstance(issue, dict) and isinstance(issue.get("code"), str)
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise HistoricalTranscriptRecoveryConflictError(
            "legacy_import_review detail is invalid"
        ) from exc
    if (
        "transcript_pair_incomplete" not in codes
        or not codes.intersection(_MISSING_TEXT_CODES)
        or not codes.intersection(_MISSING_JSON_CODES)
    ):
        raise HistoricalTranscriptRecoveryConflictError(
            "legacy_import_review does not prove both transcript files were missing"
        )

    map_rows = conn.execute(
        """
        SELECT *
        FROM legacy_import_map
        WHERE recording_id = ? AND legacy_kind = 'job'
        """,
        (recording_id,),
    ).fetchall()
    if len(map_rows) != 1:
        raise HistoricalTranscriptRecoveryConflictError(
            "Recovery target must have exactly one legacy job mapping"
        )

    recording_fields = (
        "id",
        "storage_key",
        "original_name_raw",
        "original_name_nfc",
        "source_relpath",
        "manifest_relpath",
        "ingest_sha256",
        "ingest_bytes",
        "source_mime",
        "source_state",
        "source_error_code",
        "recorded_at",
        "recorded_at_source",
        "received_at",
        "created_at",
    )
    title_fields = (
        "id",
        "title",
        "title_source",
        "locale",
        "confidence",
        "created_at",
    )
    context_fields = (
        "id",
        "context_type",
        "label",
        "semester",
        "course_name",
        "course_code",
        "session_date",
        "period_label",
        "period_index",
        "context_json",
        "source",
        "created_at",
    )
    job_fields = (
        "id",
        "job_key",
        "job_relpath",
        "requested_profile",
        "requested_profile_version",
        "status",
        "progress",
        "config_json",
        "manifest_json",
        "error_code",
        "error_message",
        "is_current",
        "queued_at",
        "started_at",
        "finished_at",
    )
    engine_fields = (
        "id",
        "engine_name",
        "engine_version",
        "provider",
        "status",
        "is_selected",
        "params_json",
        "metrics_json",
        "stderr_relpath",
        "log_relpath",
        "started_at",
        "finished_at",
    )
    review_fields = (
        "id",
        "job_id",
        "artifact_id",
        "status",
        "severity",
        "reason_code",
        "detail_json",
        "created_at",
        "resolved_at",
    )
    map_fields = (
        "id",
        "legacy_kind",
        "legacy_key",
        "source_fingerprint",
        "legacy_snapshot_json",
        "imported_at",
    )
    evidence = {
        "recording": _row_dict(recording, recording_fields),
        "title": _row_dict(title_rows[0], title_fields),
        "context": _row_dict(context_rows[0], context_fields),
        "job": _row_dict(job, job_fields),
        "engine": _row_dict(engine, engine_fields),
        "review": _row_dict(review, review_fields),
        "legacy_map": _row_dict(map_rows[0], map_fields),
    }
    return evidence, recording, job


def _journal_rows(
    conn: sqlite3.Connection,
    job_id: int,
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    rows = conn.execute(
        """
        SELECT *
        FROM job_events
        WHERE job_id = ?
          AND event_type IN (?, ?)
        ORDER BY event_seq ASC
        """,
        (job_id, PREPARED_EVENT, APPLIED_EVENT),
    ).fetchall()
    prepared = [row for row in rows if row["event_type"] == PREPARED_EVENT]
    applied = [row for row in rows if row["event_type"] == APPLIED_EVENT]
    if len(prepared) > 1 or len(applied) > 1:
        raise HistoricalTranscriptRecoveryConflictError(
            "Historical transcript recovery has duplicate journal events"
        )
    if applied and not prepared:
        raise HistoricalTranscriptRecoveryConflictError(
            "Historical transcript recovery applied event has no prepared event"
        )
    if prepared and applied and int(applied[0]["event_seq"]) <= int(
        prepared[0]["event_seq"]
    ):
        raise HistoricalTranscriptRecoveryConflictError(
            "Historical transcript recovery journal order is invalid"
        )
    return (
        prepared[0] if prepared else None,
        applied[0] if applied else None,
    )


def _recovery_job_for_storage_key(
    conn: sqlite3.Connection,
    storage_key: str,
) -> int | None:
    rows = conn.execute(
        """
        SELECT DISTINCT e.job_id
        FROM job_events e
        JOIN transcription_jobs j ON j.id = e.job_id
        JOIN recordings r ON r.id = j.recording_id
        WHERE r.storage_key = ?
          AND e.event_type IN (?, ?)
        """,
        (storage_key, PREPARED_EVENT, APPLIED_EVENT),
    ).fetchall()
    if len(rows) > 1:
        raise HistoricalTranscriptRecoveryConflictError(
            "Recording has recovery journals for multiple jobs"
        )
    return int(rows[0]["job_id"]) if rows else None


def _target_manifest(
    manifest: Mapping[str, Any],
    *,
    job_key: str,
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    target = copy.deepcopy(dict(manifest))
    target_artifacts = list(target["artifacts"])
    target_paths = {str(item["path"]) for item in target_artifacts}
    for item in targets:
        if item["path_rel"] in target_paths:
            raise HistoricalTranscriptRecoveryConflictError(
                "Target transcript path is already indexed in the manifest"
            )
        target_artifacts.append(
            {
                "kind": item["kind"],
                "path": item["path_rel"],
                "sha256": item["sha256"],
                "bytes": item["bytes"],
                "mime_type": item["mime_type"],
            }
        )
    target["artifacts"] = target_artifacts
    matched_jobs = [
        item for item in target["jobs"] if item.get("job_key") == job_key
    ]
    if len(matched_jobs) != 1:
        raise HistoricalTranscriptRecoveryConflictError(
            "Manifest does not contain exactly one matching recovery job"
        )
    paths = list(matched_jobs[0]["artifact_paths"])
    if any(item["path_rel"] in paths for item in targets):
        raise HistoricalTranscriptRecoveryConflictError(
            "Target transcript path is already owned by the manifest job"
        )
    paths.extend(item["path_rel"] for item in targets)
    matched_jobs[0]["artifact_paths"] = paths
    try:
        validate_manifest(target)
    except ManifestValidationError as exc:
        raise HistoricalTranscriptRecoveryConflictError(
            "Recovered manifest would violate the manifest contract"
        ) from exc
    return target


def _new_plan(
    conn: sqlite3.Connection,
    root_fd: int,
    historical_fd: int,
    storage_key: str,
    *,
    max_artifact_bytes: int,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    evidence, recording, job = _current_evidence(conn, storage_key)
    recording_id = int(recording["id"])
    job_id = int(job["id"])
    existing = conn.execute(
        """
        SELECT artifact_kind
        FROM artifacts
        WHERE recording_id = ?
          AND artifact_kind IN ('transcript_raw_text', 'transcript_segments_json')
          AND archived_at IS NULL
        """,
        (recording_id,),
    ).fetchall()
    if existing:
        raise HistoricalTranscriptRecoveryConflictError(
            "Recovery target already has transcript artifacts"
        )
    prepared, applied = _journal_rows(conn, job_id)
    if prepared is not None or applied is not None:
        raise HistoricalTranscriptRecoveryConflictError(
            "Recovery journal appeared while building a new plan"
        )
    manifest, _raw, previous_sha256, _snapshot = _read_record_manifest(
        root_fd,
        storage_key=storage_key,
        manifest_relpath=str(recording["manifest_relpath"]),
    )
    canonical_base = _safe_canonical_base(
        (manifest.get("legacy") or {}).get("canonical_base")
    )
    try:
        legacy_snapshot = json.loads(
            str(evidence["legacy_map"]["legacy_snapshot_json"])
        )
    except ValueError as exc:
        raise HistoricalTranscriptRecoveryConflictError(
            "Legacy import map snapshot is invalid"
        ) from exc
    if legacy_snapshot.get("canonical_base") != canonical_base:
        raise HistoricalTranscriptRecoveryConflictError(
            "Manifest and legacy map canonical_base differ"
        )
    payloads, sources = _read_pair(
        historical_fd,
        canonical_base,
        max_artifact_bytes=max_artifact_bytes,
    )
    job_relpath = validate_relative_path(
        str(job["job_relpath"]),
        field="job_relpath",
    )
    target_names = {
        "transcript_raw_text": "transcript.txt",
        "transcript_segments_json": "transcript.segments.json",
    }
    source_by_kind = {item["kind"]: item for item in sources}
    targets = [
        {
            "kind": kind,
            "revision": 1,
            "path_rel": f"{job_relpath}/{target_names[kind]}",
            "sha256": source_by_kind[kind]["sha256"],
            "bytes": source_by_kind[kind]["bytes"],
            "mime_type": (
                _RECOVERY_TEXT_MIME
                if kind == "transcript_raw_text"
                else _RECOVERY_JSON_MIME
            ),
            "temp_name": (
                f".{target_names[kind]}."
                "historical-transcript-recovery-"
                f"{source_by_kind[kind]['sha256'][:16]}.tmp"
            ),
        }
        for kind in ("transcript_raw_text", "transcript_segments_json")
    ]
    parent_fd = _open_record_parent(
        root_fd,
        storage_key,
        job_relpath,
    )
    try:
        for target in targets:
            for filename in (
                PurePosixPath(str(target["path_rel"])).name,
                str(target["temp_name"]),
            ):
                try:
                    os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise HistoricalTranscriptRecoveryConflictError(
                    "Recovery target or reserved temporary path already exists"
                )
    finally:
        os.close(parent_fd)
    target_manifest = _target_manifest(
        manifest,
        job_key=str(job["job_key"]),
        targets=targets,
    )
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "expected_count": 1,
        "storage_key": storage_key,
        "recording_id": recording_id,
        "job_id": job_id,
        "engine_run_id": int(evidence["engine"]["id"]),
        "canonical_base": canonical_base,
        "max_artifact_bytes": max_artifact_bytes,
        "evidence": evidence,
        "sources": sources,
        "targets": targets,
        "manifest": {
            "relpath": str(recording["manifest_relpath"]),
            "previous_sha256": previous_sha256,
            "materialized_sha256": _digest_bytes(
                _manifest_bytes(target_manifest)
            ),
        },
    }
    encoded = _canonical_json(plan).encode("utf-8")
    if len(encoded) > _MAX_JOURNAL_BYTES:
        raise HistoricalTranscriptRecoveryConflictError(
            "Historical transcript recovery plan is too large to journal"
        )
    return plan, payloads


def _journal_payload(row: sqlite3.Row, expected_state: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(row["event_json"]))
    except ValueError as exc:
        raise HistoricalTranscriptRecoveryConflictError(
            "Historical transcript recovery journal JSON is invalid"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != JOURNAL_SCHEMA_VERSION
        or payload.get("state") != expected_state
        or not isinstance(payload.get("plan"), dict)
        or not isinstance(payload.get("plan_sha256"), str)
    ):
        raise HistoricalTranscriptRecoveryConflictError(
            "Historical transcript recovery journal schema is invalid"
        )
    plan = payload["plan"]
    if (
        plan.get("schema_version") != PLAN_SCHEMA_VERSION
        or _sha256_json(plan) != payload["plan_sha256"]
    ):
        raise HistoricalTranscriptRecoveryConflictError(
            "Historical transcript recovery journal digest is invalid"
        )
    return payload


def _validate_stored_plan(
    conn: sqlite3.Connection,
    root_fd: int,
    historical_fd: int | None,
    plan: Mapping[str, Any],
    *,
    state: str,
    max_artifact_bytes: int,
) -> dict[str, bytes]:
    if (
        plan.get("schema_version") != PLAN_SCHEMA_VERSION
        or plan.get("expected_count") != 1
        or plan.get("max_artifact_bytes") != max_artifact_bytes
    ):
        raise HistoricalTranscriptRecoveryConflictError(
            "Stored transcript recovery plan settings are invalid"
        )
    storage_key = str(plan.get("storage_key") or "")
    evidence, recording, job = _current_evidence(conn, storage_key)
    if evidence != plan.get("evidence"):
        raise HistoricalTranscriptRecoveryConflictError(
            "Recording, title, context, job, engine, or review evidence changed"
        )
    if (
        int(recording["id"]) != plan.get("recording_id")
        or int(job["id"]) != plan.get("job_id")
        or int(evidence["engine"]["id"]) != plan.get("engine_run_id")
    ):
        raise HistoricalTranscriptRecoveryConflictError(
            "Stored transcript recovery identity changed"
        )
    target_rows = conn.execute(
        """
        SELECT
            artifact_kind AS kind,
            revision,
            path_rel,
            content_sha256 AS sha256,
            bytes,
            mime_type,
            engine_run_id
        FROM artifacts
        WHERE recording_id = ?
          AND job_id = ?
          AND artifact_kind IN ('transcript_raw_text', 'transcript_segments_json')
          AND archived_at IS NULL
        ORDER BY artifact_kind ASC
        """,
        (plan["recording_id"], plan["job_id"]),
    ).fetchall()
    expected_targets = {
        (
            str(item["kind"]),
            int(item["revision"]),
            str(item["path_rel"]),
            str(item["sha256"]),
            int(item["bytes"]),
            str(item["mime_type"]),
            int(plan["engine_run_id"]),
        )
        for item in plan["targets"]
    }
    actual_targets = {
        (
            str(row["kind"]),
            int(row["revision"]),
            str(row["path_rel"]),
            str(row["sha256"]),
            int(row["bytes"]),
            str(row["mime_type"]),
            int(row["engine_run_id"]),
        )
        for row in target_rows
    }
    if actual_targets != expected_targets:
        raise HistoricalTranscriptRecoveryConflictError(
            "Prepared transcript artifact rows differ from their journal plan"
        )

    manifest, _raw, current_sha256, _snapshot = _read_record_manifest(
        root_fd,
        storage_key=storage_key,
        manifest_relpath=str(plan["manifest"]["relpath"]),
    )
    allowed = (
        {
            str(plan["manifest"]["previous_sha256"]),
            str(plan["manifest"]["materialized_sha256"]),
        }
        if state == "prepared"
        else {str(plan["manifest"]["materialized_sha256"])}
    )
    if current_sha256 not in allowed:
        raise HistoricalTranscriptRecoveryConflictError(
            "Recording manifest digest differs from the recovery journal"
        )
    if current_sha256 == str(plan["manifest"]["materialized_sha256"]):
        rebuilt = _target_manifest_from_plan(manifest, plan)
        if _digest_bytes(_manifest_bytes(rebuilt)) != current_sha256:
            raise HistoricalTranscriptRecoveryConflictError(
                "Materialized manifest does not match recovery journal metadata"
            )

    if historical_fd is None:
        return {}
    payloads, sources = _read_pair(
        historical_fd,
        str(plan["canonical_base"]),
        max_artifact_bytes=max_artifact_bytes,
    )
    if sources != plan.get("sources"):
        raise HistoricalTranscriptRecoveryConflictError(
            "Historical transcript evidence changed after planning"
        )
    return payloads


def _validate_applied_plan(
    conn: sqlite3.Connection,
    root_fd: int,
    historical_fd: int | None,
    plan: Mapping[str, Any],
    *,
    max_artifact_bytes: int,
) -> dict[str, bytes]:
    if (
        plan.get("schema_version") != PLAN_SCHEMA_VERSION
        or plan.get("expected_count") != 1
        or plan.get("max_artifact_bytes") != max_artifact_bytes
    ):
        raise HistoricalTranscriptRecoveryConflictError(
            "Applied transcript recovery plan settings are invalid"
        )
    evidence = plan.get("evidence")
    if not isinstance(evidence, dict):
        raise HistoricalTranscriptRecoveryConflictError(
            "Applied transcript recovery evidence is invalid"
        )
    recording = conn.execute(
        "SELECT * FROM recordings WHERE id = ?",
        (plan["recording_id"],),
    ).fetchone()
    if (
        recording is None
        or str(recording["storage_key"]) != plan.get("storage_key")
        or recording["archived_at"] is not None
    ):
        raise HistoricalTranscriptRecoveryConflictError(
            "Applied transcript recovery recording identity changed"
        )
    for field, value in evidence["recording"].items():
        if recording[field] != value:
            raise HistoricalTranscriptRecoveryConflictError(
                f"Applied transcript recovery recording.{field} changed"
            )

    title = conn.execute(
        "SELECT * FROM recording_titles WHERE id = ? AND recording_id = ?",
        (evidence["title"]["id"], plan["recording_id"]),
    ).fetchone()
    context = conn.execute(
        "SELECT * FROM recording_contexts WHERE id = ? AND recording_id = ?",
        (evidence["context"]["id"], plan["recording_id"]),
    ).fetchone()
    job = conn.execute(
        "SELECT * FROM transcription_jobs WHERE id = ? AND recording_id = ?",
        (plan["job_id"], plan["recording_id"]),
    ).fetchone()
    engine = conn.execute(
        """
        SELECT *
        FROM engine_runs
        WHERE id = ? AND job_id = ? AND recording_id = ?
        """,
        (plan["engine_run_id"], plan["job_id"], plan["recording_id"]),
    ).fetchone()
    review = conn.execute(
        """
        SELECT *
        FROM review_items
        WHERE id = ? AND recording_id = ? AND job_id = ?
        """,
        (
            evidence["review"]["id"],
            plan["recording_id"],
            plan["job_id"],
        ),
    ).fetchone()
    legacy_map = conn.execute(
        """
        SELECT *
        FROM legacy_import_map
        WHERE id = ? AND recording_id = ?
        """,
        (evidence["legacy_map"]["id"], plan["recording_id"]),
    ).fetchone()
    if any(
        row is None
        for row in (title, context, job, engine, review, legacy_map)
    ):
        raise HistoricalTranscriptRecoveryConflictError(
            "Applied transcript recovery evidence row is missing"
        )
    assert title is not None
    assert context is not None
    assert job is not None
    assert engine is not None
    assert review is not None
    assert legacy_map is not None
    for label, row, expected, mutable in (
        ("title", title, evidence["title"], set()),
        ("context", context, evidence["context"], set()),
        ("job", job, evidence["job"], {"is_current"}),
        ("engine", engine, evidence["engine"], {"is_selected"}),
        (
            "review",
            review,
            evidence["review"],
            {"status", "resolved_at"},
        ),
        ("legacy_map", legacy_map, evidence["legacy_map"], set()),
    ):
        for field, value in expected.items():
            if field not in mutable and row[field] != value:
                raise HistoricalTranscriptRecoveryConflictError(
                    f"Applied transcript recovery {label}.{field} changed"
                )

    target_rows = conn.execute(
        """
        SELECT
            artifact_kind AS kind,
            revision,
            path_rel,
            content_sha256 AS sha256,
            bytes,
            mime_type,
            engine_run_id
        FROM artifacts
        WHERE recording_id = ?
          AND job_id = ?
          AND artifact_kind IN ('transcript_raw_text', 'transcript_segments_json')
          AND archived_at IS NULL
        """,
        (plan["recording_id"], plan["job_id"]),
    ).fetchall()
    expected_targets = {
        (
            str(item["kind"]),
            int(item["revision"]),
            str(item["path_rel"]),
            str(item["sha256"]),
            int(item["bytes"]),
            str(item["mime_type"]),
            int(plan["engine_run_id"]),
        )
        for item in plan["targets"]
    }
    actual_targets = {
        (
            str(row["kind"]),
            int(row["revision"]),
            str(row["path_rel"]),
            str(row["sha256"]),
            int(row["bytes"]),
            str(row["mime_type"]),
            int(row["engine_run_id"]),
        )
        for row in target_rows
    }
    if actual_targets != expected_targets:
        raise HistoricalTranscriptRecoveryConflictError(
            "Applied transcript artifacts differ from their recovery journal"
        )
    manifest, _raw, _sha256, _snapshot = _read_record_manifest(
        root_fd,
        storage_key=str(plan["storage_key"]),
        manifest_relpath=str(recording["manifest_relpath"]),
    )
    artifact_index = {
        (
            str(item["kind"]),
            str(item["path"]),
            str(item["sha256"]),
            int(item["bytes"]),
            item.get("mime_type"),
        )
        for item in manifest["artifacts"]
    }
    expected_index = {
        (
            str(item["kind"]),
            str(item["path_rel"]),
            str(item["sha256"]),
            int(item["bytes"]),
            str(item["mime_type"]),
        )
        for item in plan["targets"]
    }
    if not expected_index.issubset(artifact_index):
        raise HistoricalTranscriptRecoveryConflictError(
            "Applied transcript artifacts are missing from the current manifest"
        )
    manifest_jobs = [
        item
        for item in manifest["jobs"]
        if item.get("job_key") == evidence["job"]["job_key"]
    ]
    if len(manifest_jobs) != 1 or not {
        str(item["path_rel"]) for item in plan["targets"]
    }.issubset(set(manifest_jobs[0]["artifact_paths"])):
        raise HistoricalTranscriptRecoveryConflictError(
            "Applied transcript paths are missing from manifest job ownership"
        )

    if historical_fd is None:
        return {}
    payloads, sources = _read_pair(
        historical_fd,
        str(plan["canonical_base"]),
        max_artifact_bytes=max_artifact_bytes,
    )
    if sources != plan.get("sources"):
        raise HistoricalTranscriptRecoveryConflictError(
            "Historical transcript evidence changed after application"
        )
    return payloads


def _target_manifest_from_plan(
    current_manifest: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    target_sha256 = str(plan["manifest"]["materialized_sha256"])
    current_sha256 = _digest_bytes(_manifest_bytes(current_manifest))
    if current_sha256 == target_sha256:
        return copy.deepcopy(dict(current_manifest))
    if current_sha256 != str(plan["manifest"]["previous_sha256"]):
        raise HistoricalTranscriptRecoveryConflictError(
            "Manifest changed after the transcript recovery plan"
        )
    target = _target_manifest(
        current_manifest,
        job_key=str(plan["evidence"]["job"]["job_key"]),
        targets=list(plan["targets"]),
    )
    if _digest_bytes(_manifest_bytes(target)) != target_sha256:
        raise HistoricalTranscriptRecoveryConflictError(
            "Rebuilt recovery manifest digest differs from the journal"
        )
    return target


def _public_plan(plan: Mapping[str, Any], *, state: str) -> dict[str, Any]:
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "storage_key": plan["storage_key"],
        "recording_id": plan["recording_id"],
        "job_id": plan["job_id"],
        "review_item_id": plan["evidence"]["review"]["id"],
        "canonical_base": plan["canonical_base"],
        "expected_count": 1,
        "max_artifact_bytes": plan["max_artifact_bytes"],
        "source_artifacts": [
            {
                "kind": item["kind"],
                "bytes": item["bytes"],
                "sha256": item["sha256"],
            }
            for item in plan["sources"]
        ],
        "target_artifacts": [
            {
                "kind": item["kind"],
                "revision": item["revision"],
                "path_rel": item["path_rel"],
            }
            for item in plan["targets"]
        ],
        "recovery_state": state,
        "plan_sha256": _sha256_json(plan),
    }


def _plan_locked(
    conn: sqlite3.Connection,
    root_fd: int,
    historical_fd: int,
    storage_key: str,
    *,
    max_artifact_bytes: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    recovery_job_id = _recovery_job_for_storage_key(conn, storage_key)
    if recovery_job_id is None:
        plan, payloads = _new_plan(
            conn,
            root_fd,
            historical_fd,
            storage_key,
            max_artifact_bytes=max_artifact_bytes,
        )
        return _public_plan(plan, state="planned"), plan, payloads
    prepared, applied = _journal_rows(conn, recovery_job_id)
    if prepared is None:
        raise HistoricalTranscriptRecoveryConflictError(
            "Recovery journal is missing its prepared event"
        )
    prepared_payload = _journal_payload(prepared, "prepared")
    if prepared_payload["plan"].get("storage_key") != storage_key:
        raise HistoricalTranscriptRecoveryConflictError(
            "Recovery journal storage_key does not match the request"
        )
    plan = prepared_payload["plan"]
    state = "prepared"
    if applied is not None:
        applied_payload = _journal_payload(applied, "applied")
        if (
            applied_payload["plan_sha256"]
            != prepared_payload["plan_sha256"]
            or applied_payload["plan"] != plan
        ):
            raise HistoricalTranscriptRecoveryConflictError(
                "Prepared and applied recovery journals differ"
            )
        state = "applied"
    if state == "prepared":
        payloads = _validate_stored_plan(
            conn,
            root_fd,
            historical_fd,
            plan,
            state=state,
            max_artifact_bytes=max_artifact_bytes,
        )
    else:
        payloads = _validate_applied_plan(
            conn,
            root_fd,
            historical_fd,
            plan,
            max_artifact_bytes=max_artifact_bytes,
        )
    return _public_plan(plan, state=state), plan, payloads


def plan_historical_transcript_recovery(
    db_path: Path | str,
    records_root: Path | str,
    historical_transcript_root: Path | str,
    storage_key: str,
    *,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> dict[str, Any]:
    """Build a metadata-only recovery plan without changing DB or files."""

    max_bytes = _validate_max_artifact_bytes(max_artifact_bytes)
    path = Path(db_path).expanduser()
    if not path.exists():
        raise HistoricalTranscriptRecoveryNotFoundError(
            "Storage v2 database is not available"
        )
    _preflight_root_separation(
        path,
        records_root,
        historical_transcript_root,
    )
    with _locked_records_root(records_root) as (_root, root_fd):
        with _locked_historical_root(
            historical_transcript_root
        ) as (_historical_root, historical_fd):
            _require_separated_roots(path, _root, _historical_root)
            conn = connect_v2(path, readonly=True)
            conn.row_factory = sqlite3.Row
            try:
                require_v2_schema(conn)
                _require_database_integrity(conn)
                public, _private, _payloads = _plan_locked(
                    conn,
                    root_fd,
                    historical_fd,
                    storage_key,
                    max_artifact_bytes=max_bytes,
                )
                return public
            finally:
                conn.close()


def _next_event_seq(conn: sqlite3.Connection, job_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(event_seq), 0) + 1 FROM job_events WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _append_event(
    conn: sqlite3.Connection,
    plan: Mapping[str, Any],
    *,
    state: str,
) -> None:
    event_type = PREPARED_EVENT if state == "prepared" else APPLIED_EVENT
    payload = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "state": state,
        "plan_sha256": _sha256_json(plan),
        "plan": plan,
    }
    raw = _canonical_json(payload)
    if len(raw.encode("utf-8")) > _MAX_JOURNAL_BYTES:
        raise HistoricalTranscriptRecoveryConflictError(
            "Historical transcript recovery journal is too large"
        )
    conn.execute(
        """
        INSERT INTO job_events(
            job_id,
            recording_id,
            event_seq,
            event_type,
            event_json
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            plan["job_id"],
            plan["recording_id"],
            _next_event_seq(conn, int(plan["job_id"])),
            event_type,
            raw,
        ),
    )


def _prepare(conn: sqlite3.Connection, plan: Mapping[str, Any]) -> None:
    for target in plan["targets"]:
        conn.execute(
            """
            INSERT INTO artifacts(
                recording_id,
                job_id,
                engine_run_id,
                artifact_kind,
                revision,
                path_rel,
                content_sha256,
                bytes,
                mime_type,
                is_latest
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                plan["recording_id"],
                plan["job_id"],
                plan["engine_run_id"],
                target["kind"],
                target["revision"],
                target["path_rel"],
                target["sha256"],
                target["bytes"],
                target["mime_type"],
            ),
        )
    _append_event(conn, plan, state="prepared")


def _open_record_parent(
    root_fd: int,
    storage_key: str,
    parent_relpath: str,
) -> int:
    validate_storage_key(storage_key)
    validate_relative_path(parent_relpath, field="artifact parent")
    current_fd = os.dup(root_fd)
    try:
        for component in (
            storage_key,
            *PurePosixPath(parent_relpath).parts,
        ):
            next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _read_existing_target(
    parent_fd: int,
    filename: str,
    limit: int,
    *,
    allowed_nlinks: set[int] | None = None,
) -> tuple[bytes, tuple[int, ...]]:
    accepted_nlinks = allowed_nlinks or {1}
    descriptor = os.open(filename, _regular_read_flags(), dir_fd=parent_fd)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink not in accepted_nlinks
            or before.st_size > limit
        ):
            raise HistoricalTranscriptRecoveryConflictError(
                "Existing recovery target is not a bounded single-link file"
            )
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            chunk = os.read(
                descriptor,
                min(64 * 1024, limit + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        path_stat = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        if (
            total > limit
            or _file_snapshot(before) != _file_snapshot(after)
            or _file_snapshot(after) != _file_snapshot(path_stat)
        ):
            raise HistoricalTranscriptRecoveryConflictError(
                "Existing recovery target changed while reading"
            )
        return b"".join(chunks), _file_snapshot(after)
    finally:
        os.close(descriptor)


def _fsync_directory_fd(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        if exc.errno not in {
            errno.EINVAL,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }:
            raise


def _write_or_verify_target(
    root_fd: int,
    plan: Mapping[str, Any],
    target: Mapping[str, Any],
    payload: bytes,
) -> None:
    path = PurePosixPath(str(target["path_rel"]))
    parent_fd = _open_record_parent(
        root_fd,
        str(plan["storage_key"]),
        PurePosixPath(*path.parts[:-1]).as_posix(),
    )
    try:
        temp_name = str(target["temp_name"])
        expected_sha256 = str(target["sha256"])
        expected_bytes = int(target["bytes"])

        def exact(payload_value: bytes) -> bool:
            return (
                len(payload_value) == expected_bytes
                and hashlib.sha256(payload_value).hexdigest()
                == expected_sha256
                and payload_value == payload
            )

        try:
            existing, existing_snapshot = _read_existing_target(
                parent_fd,
                path.name,
                int(plan["max_artifact_bytes"]),
                allowed_nlinks={1, 2},
            )
        except FileNotFoundError:
            existing = None
            existing_snapshot = None
        try:
            temporary, temporary_snapshot = _read_existing_target(
                parent_fd,
                temp_name,
                int(plan["max_artifact_bytes"]),
                allowed_nlinks={1, 2},
            )
        except FileNotFoundError:
            temporary = None
            temporary_snapshot = None

        if existing is not None:
            if not exact(existing):
                raise HistoricalTranscriptRecoveryConflictError(
                    "Existing recovery target differs from plan: "
                    f"{target['path_rel']}"
                )
            if temporary is not None:
                assert existing_snapshot is not None
                assert temporary_snapshot is not None
                if (
                    not exact(temporary)
                    or existing_snapshot[:2] != temporary_snapshot[:2]
                    or existing_snapshot[3] != 2
                    or temporary_snapshot[3] != 2
                ):
                    raise HistoricalTranscriptRecoveryConflictError(
                        "Recovery target and journal-owned temporary file "
                        "do not form the expected publish pair"
                    )
                os.unlink(temp_name, dir_fd=parent_fd)
                _fsync_directory_fd(parent_fd)
            verified, verified_snapshot = _read_existing_target(
                parent_fd,
                path.name,
                int(plan["max_artifact_bytes"]),
            )
            if not exact(verified) or verified_snapshot[3] != 1:
                raise HistoricalTranscriptRecoveryConflictError(
                    f"Published recovery target is invalid: {target['path_rel']}"
                )
            return

        if temporary is not None and not exact(temporary):
            raise HistoricalTranscriptRecoveryConflictError(
                "Reserved recovery temporary file differs from its journal; "
                "ownership cannot be proven, so it was preserved"
            )

        if temporary is None:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            descriptor = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError(errno.EIO, "short transcript write")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_directory_fd(parent_fd)
            temporary, temporary_snapshot = _read_existing_target(
                parent_fd,
                temp_name,
                int(plan["max_artifact_bytes"]),
            )
        assert temporary is not None
        assert temporary_snapshot is not None
        if not exact(temporary) or temporary_snapshot[3] != 1:
            raise HistoricalTranscriptRecoveryConflictError(
                "Recovery temporary file differs from its journal plan"
            )

        try:
            os.link(
                temp_name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            competing, competing_snapshot = _read_existing_target(
                parent_fd,
                path.name,
                int(plan["max_artifact_bytes"]),
                allowed_nlinks={1, 2},
            )
            if not exact(competing):
                raise HistoricalTranscriptRecoveryConflictError(
                    "Recovery target appeared with different content"
                )
            linked_snapshot = _file_snapshot(
                os.stat(temp_name, dir_fd=parent_fd, follow_symlinks=False)
            )
            if (
                competing_snapshot[:2] != linked_snapshot[:2]
                or competing_snapshot[3] != 2
            ):
                raise HistoricalTranscriptRecoveryConflictError(
                    "Recovery target appeared outside the journal-owned publish"
                )
        _fsync_directory_fd(parent_fd)
        os.unlink(temp_name, dir_fd=parent_fd)
        _fsync_directory_fd(parent_fd)
        published, published_snapshot = _read_existing_target(
            parent_fd,
            path.name,
            int(plan["max_artifact_bytes"]),
        )
        if not exact(published) or published_snapshot[3] != 1:
            raise HistoricalTranscriptRecoveryConflictError(
                f"Published recovery target is invalid: {target['path_rel']}"
            )
    finally:
        os.close(parent_fd)


def apply_historical_transcript_recovery(
    db_path: Path | str,
    records_root: Path | str,
    historical_transcript_root: Path | str,
    storage_key: str,
    *,
    expected_count: int,
    expected_plan_sha256: str,
    recovery_enabled: bool = False,
    allow_write: bool = False,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> dict[str, Any]:
    """Apply or forward-recover one exact historical transcript pair."""

    if not recovery_enabled:
        raise HistoricalTranscriptRecoveryWriteDisabledError(
            "Historical transcript recovery is disabled"
        )
    if not allow_write:
        raise HistoricalTranscriptRecoveryWriteDisabledError(
            "Historical transcript recovery requires allow_write=True"
        )
    if isinstance(expected_count, bool) or expected_count != 1:
        raise HistoricalTranscriptRecoveryConflictError(
            "Historical transcript recovery expected_count must equal 1"
        )
    max_bytes = _validate_max_artifact_bytes(max_artifact_bytes)
    if (
        not isinstance(expected_plan_sha256, str)
        or len(expected_plan_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_plan_sha256)
    ):
        raise HistoricalTranscriptRecoveryConflictError(
            "Expected plan SHA-256 must be canonical lowercase hexadecimal"
        )
    path = Path(db_path).expanduser()
    if not path.exists():
        raise HistoricalTranscriptRecoveryNotFoundError(
            "Storage v2 database is not available"
        )
    _preflight_root_separation(
        path,
        records_root,
        historical_transcript_root,
    )
    prepared_exists = False
    action = "applied"
    public_plan: dict[str, Any] | None = None
    with _locked_records_root(records_root) as (_root, root_fd):
        with _locked_historical_root(
            historical_transcript_root
        ) as (_historical_root, historical_fd):
            _require_separated_roots(path, _root, _historical_root)
            conn = connect_v2(path)
            conn.row_factory = sqlite3.Row
            try:
                require_v2_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                _require_database_integrity(conn)
                public_plan, private_plan, payloads = _plan_locked(
                    conn,
                    root_fd,
                    historical_fd,
                    storage_key,
                    max_artifact_bytes=max_bytes,
                )
                if public_plan["plan_sha256"] != expected_plan_sha256:
                    raise HistoricalTranscriptRecoveryConflictError(
                        "Historical transcript recovery plan SHA-256 changed"
                    )
                state = str(public_plan["recovery_state"])
                if state == "applied":
                    conn.rollback()
                    action = "already_applied"
                else:
                    if state == "planned":
                        _prepare(conn, private_plan)
                        conn.commit()
                        prepared_exists = True
                        action = "applied"
                    else:
                        conn.rollback()
                        prepared_exists = True
                        action = "recovered"

                    readonly = connect_v2(path, readonly=True)
                    try:
                        require_v2_schema(readonly)
                        evidence, _recording, job = _current_evidence(
                            readonly,
                            storage_key,
                        )
                        prepared, applied = _journal_rows(
                            readonly, int(job["id"])
                        )
                        if prepared is None or applied is not None:
                            raise HistoricalTranscriptRecoveryConflictError(
                                "Committed prepared journal is unavailable"
                            )
                        journal = _journal_payload(prepared, "prepared")
                        if (
                            journal["plan_sha256"] != expected_plan_sha256
                            or journal["plan"] != private_plan
                            or evidence != private_plan["evidence"]
                        ):
                            raise HistoricalTranscriptRecoveryConflictError(
                                "Committed prepared journal differs from plan"
                            )
                        payloads = _validate_stored_plan(
                            readonly,
                            root_fd,
                            historical_fd,
                            private_plan,
                            state="prepared",
                            max_artifact_bytes=max_bytes,
                        )
                    finally:
                        readonly.close()

                    targets_by_kind = {
                        item["kind"]: item for item in private_plan["targets"]
                    }
                    for kind in (
                        "transcript_raw_text",
                        "transcript_segments_json",
                    ):
                        _write_or_verify_target(
                            root_fd,
                            private_plan,
                            targets_by_kind[kind],
                            payloads[kind],
                        )

                    current_manifest, _raw, current_sha256, _snapshot = (
                        _read_record_manifest(
                            root_fd,
                            storage_key=storage_key,
                            manifest_relpath=str(
                                private_plan["manifest"]["relpath"]
                            ),
                        )
                    )
                    target_manifest = _target_manifest_from_plan(
                        current_manifest,
                        private_plan,
                    )
                    if current_sha256 != str(
                        private_plan["manifest"]["materialized_sha256"]
                    ):
                        written_sha256 = _replace_record_manifest(
                            root_fd,
                            storage_key=storage_key,
                            manifest_relpath=str(
                                private_plan["manifest"]["relpath"]
                            ),
                            expected_sha256=str(
                                private_plan["manifest"]["previous_sha256"]
                            ),
                            payload=target_manifest,
                        )
                        if written_sha256 != str(
                            private_plan["manifest"]["materialized_sha256"]
                        ):
                            raise HistoricalTranscriptRecoveryConflictError(
                                "Recovered manifest digest differs from plan"
                            )

                    conn.execute("BEGIN IMMEDIATE")
                    _require_database_integrity(conn)
                    evidence, _recording, job = _current_evidence(
                        conn,
                        storage_key,
                    )
                    prepared, applied = _journal_rows(conn, int(job["id"]))
                    if prepared is None or applied is not None:
                        raise HistoricalTranscriptRecoveryConflictError(
                            "Recovery journal changed before finalization"
                        )
                    journal = _journal_payload(prepared, "prepared")
                    if (
                        journal["plan_sha256"] != expected_plan_sha256
                        or journal["plan"] != private_plan
                        or evidence != private_plan["evidence"]
                    ):
                        raise HistoricalTranscriptRecoveryConflictError(
                            "Recovery evidence changed before finalization"
                        )
                    _validate_stored_plan(
                        conn,
                        root_fd,
                        historical_fd,
                        private_plan,
                        state="prepared",
                        max_artifact_bytes=max_bytes,
                    )
                    _append_event(conn, private_plan, state="applied")
                    conn.commit()
            except BaseException as exc:
                if conn.in_transaction:
                    conn.rollback()
                if prepared_exists and not isinstance(
                    exc, HistoricalTranscriptRecoveryRequiredError
                ):
                    raise HistoricalTranscriptRecoveryRequiredError(
                        "Historical transcript recovery has a prepared state; "
                        f"replay the same guarded plan: {exc}"
                    ) from exc
                raise
            finally:
                conn.close()

    from lecture_stt.storage_v2.verifier import verify_library

    try:
        verification = verify_library(path, records_root)
    except BaseException as exc:
        raise HistoricalTranscriptRecoveryPostCommitVerificationError(
            "Transcript recovery committed, but verification could not complete: "
            f"{exc}"
        ) from exc
    if not verification.get("ok"):
        raise HistoricalTranscriptRecoveryPostCommitVerificationError(
            "Transcript recovery committed, but verification failed: "
            + _verification_issue_codes(verification)
        )
    assert public_plan is not None
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "ok": True,
        "action": action,
        "storage_key": storage_key,
        "expected_count": 1,
        "plan_sha256": expected_plan_sha256,
        "recovery_state": "applied",
    }


def historical_transcript_recovery_integrity_issues(
    conn: sqlite3.Connection,
    root_fd: int,
) -> list[dict[str, Any]]:
    """Validate recovery journals without requiring the historical source root."""

    issues: list[dict[str, Any]] = []
    job_rows = conn.execute(
        """
        SELECT DISTINCT job_id
        FROM job_events
        WHERE event_type IN (?, ?)
        ORDER BY job_id ASC
        """,
        (PREPARED_EVENT, APPLIED_EVENT),
    ).fetchall()
    journal_job_ids = {int(row["job_id"]) for row in job_rows}
    inferred_rows = conn.execute(
        """
        SELECT DISTINCT
            j.id AS job_id,
            r.storage_key
        FROM transcription_jobs j
        JOIN recordings r ON r.id = j.recording_id
        JOIN engine_runs e
          ON e.job_id = j.id
         AND e.recording_id = j.recording_id
         AND e.provider = 'legacy_import'
        WHERE EXISTS (
            SELECT 1
            FROM artifacts raw
            WHERE raw.job_id = j.id
              AND raw.recording_id = j.recording_id
              AND raw.archived_at IS NULL
              AND raw.artifact_kind = 'transcript_raw_text'
              AND raw.path_rel = j.job_relpath || '/transcript.txt'
              AND raw.mime_type = ?
        )
          AND EXISTS (
            SELECT 1
            FROM artifacts segments
            WHERE segments.job_id = j.id
              AND segments.recording_id = j.recording_id
              AND segments.archived_at IS NULL
              AND segments.artifact_kind = 'transcript_segments_json'
              AND segments.path_rel =
                  j.job_relpath || '/transcript.segments.json'
              AND segments.mime_type = ?
          )
        ORDER BY j.id ASC
        """,
        (_RECOVERY_TEXT_MIME, _RECOVERY_JSON_MIME),
    ).fetchall()
    for inferred in inferred_rows:
        inferred_job_id = int(inferred["job_id"])
        if inferred_job_id not in journal_job_ids:
            issues.append(
                {
                    "job_id": inferred_job_id,
                    "storage_key": str(inferred["storage_key"]),
                    "code": "historical_transcript_recovery_journal_missing",
                }
            )
    for job_row in job_rows:
        job_id = int(job_row["job_id"])
        storage_row = conn.execute(
            """
            SELECT r.storage_key
            FROM transcription_jobs j
            JOIN recordings r ON r.id = j.recording_id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
        storage_key = (
            str(storage_row["storage_key"])
            if storage_row is not None
            else None
        )
        try:
            prepared, applied = _journal_rows(conn, job_id)
            if prepared is None:
                raise HistoricalTranscriptRecoveryConflictError(
                    "Prepared recovery journal is missing"
                )
            prepared_payload = _journal_payload(prepared, "prepared")
            plan = prepared_payload["plan"]
            state = "prepared"
            if applied is not None:
                applied_payload = _journal_payload(applied, "applied")
                if (
                    applied_payload["plan_sha256"]
                    != prepared_payload["plan_sha256"]
                    or applied_payload["plan"] != plan
                ):
                    raise HistoricalTranscriptRecoveryConflictError(
                        "Prepared and applied recovery journals differ"
                    )
                state = "applied"
            if state == "prepared":
                _validate_stored_plan(
                    conn,
                    root_fd,
                    None,
                    plan,
                    state=state,
                    max_artifact_bytes=int(plan["max_artifact_bytes"]),
                )
            else:
                _validate_applied_plan(
                    conn,
                    root_fd,
                    None,
                    plan,
                    max_artifact_bytes=int(plan["max_artifact_bytes"]),
                )
            if state == "prepared":
                issues.append(
                    {
                        "job_id": job_id,
                        "storage_key": storage_key,
                        "code": "historical_transcript_recovery_prepared",
                        "plan_sha256": prepared_payload["plan_sha256"],
                    }
                )
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            issues.append(
                {
                    "job_id": job_id,
                    "storage_key": storage_key,
                    "code": "historical_transcript_recovery_journal_invalid",
                    "message": str(exc),
                }
            )
    return issues
