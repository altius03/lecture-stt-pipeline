from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import unicodedata
from typing import Any, Mapping

from lecture_stt.storage_v2.manifest import (
    ManifestValidationError,
    validate_manifest,
    validate_relative_path,
    validate_storage_key,
)
from lecture_stt.storage_v2.repository import connect_v2, require_v2_schema


_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_SCAN_DEPTH = 64
_MAX_SCAN_ENTRIES = 100_000
_ENGINE_BOUND_ARTIFACT_KINDS = {
    "transcript_raw_text",
    "transcript_segments_json",
    "quality_scorecard",
}
_ENGINE_STATUSES_BY_JOB_STATUS = {
    "queued": {"planned", "superseded"},
    "processing": {"running", "superseded"},
    "done": {"succeeded"},
    "needs_review": {"succeeded"},
    "error": {"failed"},
    "canceled": {"canceled", "superseded"},
}


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _regular_file_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _open_regular_file_at(record_root_fd: int, relpath: str) -> int:
    """Open a regular record file without following any path component."""

    components = PurePosixPath(relpath).parts
    current_fd = os.dup(record_root_fd)
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
                    "record artifact is not a regular file",
                    relpath,
                )
            if metadata.st_nlink != 1:
                raise OSError(
                    errno.EMLINK,
                    "record artifact must not be hard-linked",
                    relpath,
                )
        except BaseException:
            os.close(file_fd)
            raise
        return file_fd
    finally:
        os.close(current_fd)


def _file_snapshot(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_bounded_fd(file_fd: int, *, max_bytes: int) -> bytes:
    before = os.fstat(file_fd)
    if before.st_size > max_bytes:
        raise ManifestValidationError(
            f"Manifest exceeds the {max_bytes // (1024 * 1024)} MiB metadata limit"
        )
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        chunk = os.read(file_fd, min(64 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    after = os.fstat(file_fd)
    if _file_snapshot(before) != _file_snapshot(after):
        raise OSError(errno.ESTALE, "manifest changed while it was being read")
    if total > max_bytes:
        raise ManifestValidationError(
            f"Manifest exceeds the {max_bytes // (1024 * 1024)} MiB metadata limit"
        )
    return b"".join(chunks)


def _compute_sha256_fd(
    file_fd: int,
    *,
    before: os.stat_result,
) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = os.read(file_fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    after = os.fstat(file_fd)
    if _file_snapshot(before) != _file_snapshot(after):
        raise OSError(errno.ESTALE, "artifact changed while it was being hashed")
    return digest.hexdigest()


def _expected_directories(file_paths: set[str]) -> set[str]:
    directories: set[str] = set()
    for raw_path in file_paths:
        for parent in PurePosixPath(raw_path).parents:
            if parent == PurePosixPath("."):
                break
            directories.add(parent.as_posix())
    return directories


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _timetable_integrity_issues(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    imports = conn.execute(
        """
        SELECT
            id,
            semester,
            source_format,
            source_sha256,
            entries_sha256,
            row_count
        FROM schedule_imports
        ORDER BY id
        LIMIT 2001
        """
    ).fetchall()
    if len(imports) > 2000:
        return [{"code": "timetable_import_limit_exceeded", "limit": 2000}]
    for timetable_import in imports:
        import_id = int(timetable_import["id"])
        entries = conn.execute(
            """
            SELECT
                row_index,
                entry_key,
                semester,
                course_name,
                course_code,
                weekday,
                start_time,
                end_time,
                period_label,
                period_index,
                classroom
            FROM schedule_entries
            WHERE schedule_import_id = ?
            ORDER BY row_index
            LIMIT 2001
            """,
            (import_id,),
        ).fetchall()
        if len(entries) > 2000:
            issues.append(
                {
                    "code": "timetable_entry_limit_exceeded",
                    "schedule_import_id": import_id,
                    "limit": 2000,
                }
            )
            continue
        if len(entries) != int(timetable_import["row_count"]):
            issues.append(
                {
                    "code": "timetable_import_row_count_mismatch",
                    "schedule_import_id": import_id,
                    "expected_count": int(timetable_import["row_count"]),
                    "actual_count": len(entries),
                }
            )
        entry_payload = [
            {
                key: entry[key]
                for key in entry.keys()
                if key != "row_index"
            }
            for entry in sorted(
                entries,
                key=lambda item: str(item["entry_key"]),
            )
        ]
        entries_sha256 = hashlib.sha256(
            _canonical_json(entry_payload).encode("utf-8")
        ).hexdigest()
        if entries_sha256 != str(timetable_import["entries_sha256"]):
            issues.append(
                {
                    "code": "timetable_import_entries_digest_mismatch",
                    "schedule_import_id": import_id,
                }
            )
        mismatched_semesters = sum(
            str(entry["semester"]) != str(timetable_import["semester"])
            for entry in entries
        )
        if mismatched_semesters:
            issues.append(
                {
                    "code": "timetable_import_semester_mismatch",
                    "schedule_import_id": import_id,
                    "mismatched_entry_count": mismatched_semesters,
                }
            )
        selection = conn.execute(
            """
            SELECT schedule_import_id, selection_plan_sha256
            FROM schedule_semester_selections
            WHERE semester = ?
            """,
            (str(timetable_import["semester"]),),
        ).fetchone()
        if selection is None:
            issues.append(
                {
                    "code": "timetable_semester_selection_missing",
                    "semester": str(timetable_import["semester"]),
                }
            )
        elif int(selection["schedule_import_id"]) == import_id:
            plan_entries = [dict(entry) for entry in entries]
            digest_payload = {
                "schema_version": "storage-v2/timetable-import-plan@1",
                "semester": str(timetable_import["semester"]),
                "source_format": str(timetable_import["source_format"]),
                "source_sha256": str(timetable_import["source_sha256"]),
                "entries_sha256": str(timetable_import["entries_sha256"]),
                "expected_count": int(timetable_import["row_count"]),
                "entries": plan_entries,
            }
            plan_sha256 = hashlib.sha256(
                _canonical_json(digest_payload).encode("utf-8")
            ).hexdigest()
            if plan_sha256 != str(selection["selection_plan_sha256"]):
                issues.append(
                    {
                        "code": "timetable_selection_plan_digest_mismatch",
                        "schedule_import_id": import_id,
                    }
                )

    proposals = conn.execute(
        """
        SELECT
            proposal.id,
            proposal.recording_id,
            proposal.schedule_entry_id,
            proposal.status,
            proposal.classification_reason,
            proposal.semester,
            proposal.course_name,
            proposal.course_code,
            proposal.weekday,
            proposal.start_time,
            proposal.end_time,
            proposal.period_label,
            proposal.period_index,
            proposal.classroom,
            proposal.detail_json,
            proposal.confirmation_plan_sha256,
            proposal.confirmed_at,
            review.status AS review_status,
            entry.entry_key,
            entry.course_name AS entry_course_name,
            entry.course_code AS entry_course_code,
            entry.weekday AS entry_weekday,
            entry.start_time AS entry_start_time,
            entry.end_time AS entry_end_time,
            entry.period_label AS entry_period_label,
            entry.period_index AS entry_period_index,
            entry.classroom AS entry_classroom
        FROM recording_classification_proposals AS proposal
        JOIN review_items AS review ON review.id = proposal.review_item_id
        LEFT JOIN schedule_entries AS entry ON entry.id = proposal.schedule_entry_id
        ORDER BY proposal.id
        LIMIT 10001
        """
    ).fetchall()
    if len(proposals) > 10_000:
        return [
            *issues,
            {"code": "classification_proposal_limit_exceeded", "limit": 10_000},
        ]
    entry_fields = (
        ("course_name", "entry_course_name"),
        ("course_code", "entry_course_code"),
        ("weekday", "entry_weekday"),
        ("start_time", "entry_start_time"),
        ("end_time", "entry_end_time"),
        ("period_label", "entry_period_label"),
        ("period_index", "entry_period_index"),
        ("classroom", "entry_classroom"),
    )
    for proposal in proposals:
        proposal_id = int(proposal["id"])
        status = str(proposal["status"])
        review_status = str(proposal["review_status"])
        if status == "confirmed" and review_status != "resolved":
            issues.append(
                {
                    "code": "confirmed_classification_review_not_resolved",
                    "proposal_id": proposal_id,
                }
            )
        if status == "suggested" and review_status not in {"open", "triaged"}:
            issues.append(
                {
                    "code": "suggested_classification_review_not_open",
                    "proposal_id": proposal_id,
                }
            )
        if status == "rejected" and review_status != "dismissed":
            issues.append(
                {
                    "code": "rejected_classification_review_not_dismissed",
                    "proposal_id": proposal_id,
                }
            )
        if proposal["schedule_entry_id"] is not None:
            mismatched = [
                proposal_field
                for proposal_field, entry_field in entry_fields
                if proposal[proposal_field] != proposal[entry_field]
            ]
            if mismatched:
                issues.append(
                    {
                        "code": "classification_schedule_metadata_mismatch",
                        "proposal_id": proposal_id,
                        "fields": mismatched,
                    }
                )
        try:
            detail = json.loads(str(proposal["detail_json"]))
        except (TypeError, ValueError):
            detail = None
        allowed_detail_fields = {
            "schema_version",
            "semester",
            "classification_reason",
            "candidate_entry_keys",
        }
        if (
            not isinstance(detail, dict)
            or set(detail) != allowed_detail_fields
            or detail.get("schema_version")
            != "storage-v2/timetable-classification-review@1"
            or detail.get("semester") != proposal["semester"]
            or detail.get("classification_reason")
            != proposal["classification_reason"]
            or not isinstance(detail.get("candidate_entry_keys"), list)
            or any(
                not isinstance(key, str)
                or not re.fullmatch(r"[0-9a-f]{64}", key)
                for key in detail.get("candidate_entry_keys", [])
            )
        ):
            issues.append(
                {
                    "code": "classification_review_detail_invalid",
                    "proposal_id": proposal_id,
                }
            )
    return issues


def _title_suggestion_integrity_issues(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Validate the closed title-suggestion audit and review relationships."""

    issues: list[dict[str, Any]] = []
    rows = conn.execute(
        """
        SELECT
            proposal.id,
            proposal.recording_id,
            proposal.transcript_artifact_id,
            proposal.review_item_id,
            proposal.status,
            proposal.suggestion_reason,
            proposal.proposed_title,
            proposal.confidence,
            proposal.generator_version,
            proposal.detail_json,
            proposal.confirmation_plan_sha256,
            proposal.confirmed_at,
            review.recording_id AS review_recording_id,
            review.job_id AS review_job_id,
            review.artifact_id AS review_artifact_id,
            review.status AS review_status,
            review.severity AS review_severity,
            review.reason_code AS review_reason_code,
            review.detail_json AS review_detail_json,
            review.resolved_at AS review_resolved_at,
            artifact.recording_id AS artifact_recording_id,
            artifact.job_id AS artifact_job_id,
            artifact.artifact_kind,
            artifact.revision AS transcript_revision,
            artifact.is_latest AS transcript_is_latest,
            artifact.archived_at AS transcript_archived_at,
            job.is_current AS transcript_job_is_current,
            job.archived_at AS transcript_job_archived_at,
            classification.recording_id AS classification_recording_id,
            classification.status AS classification_status,
            classification.classification_reason
        FROM recording_title_proposals AS proposal
        LEFT JOIN review_items AS review
          ON review.id = proposal.review_item_id
         AND review.recording_id = proposal.recording_id
        LEFT JOIN artifacts AS artifact
          ON artifact.id = proposal.transcript_artifact_id
         AND artifact.recording_id = proposal.recording_id
        LEFT JOIN transcription_jobs AS job
          ON job.id = artifact.job_id
         AND job.recording_id = artifact.recording_id
        LEFT JOIN recording_classification_proposals AS classification
          ON classification.id = proposal.classification_proposal_id
         AND classification.recording_id = proposal.recording_id
        ORDER BY proposal.id
        LIMIT 10001
        """
    ).fetchall()
    if len(rows) > 10_000:
        return [
            {
                "code": "title_suggestion_proposal_limit_exceeded",
                "limit": 10_000,
            }
        ]

    allowed_detail_fields = {
        "schema_version",
        "suggestion_reason",
        "generator_version",
        "transcript_revision",
        "classification_status",
        "context_type",
    }
    allowed_classification_statuses = {None, "suggested", "confirmed"}
    allowed_context_types = {
        None,
        "general",
        "class_session",
        "daily_note",
        "meeting",
        "memo",
    }
    for row in rows:
        proposal_id = int(row["id"])
        status = str(row["status"])
        review_status = (
            None
            if row["review_status"] is None
            else str(row["review_status"])
        )
        if (
            (
                status == "suggested"
                and (
                    review_status not in {"open", "triaged"}
                    or row["review_resolved_at"] is not None
                )
            )
            or (
                status == "confirmed"
                and (
                    review_status != "resolved"
                    or row["review_resolved_at"] is None
                )
            )
            or (
                status == "rejected"
                and (
                    review_status != "dismissed"
                    or row["review_resolved_at"] is None
                )
            )
        ):
            issues.append(
                {
                    "code": "title_suggestion_review_status_mismatch",
                    "proposal_id": proposal_id,
                    "proposal_status": status,
                    "review_status": review_status,
                }
            )

        expected_reason_code = (
            f"title_suggestion_{row['suggestion_reason']}"
        )
        if (
            row["review_recording_id"] is None
            or row["review_recording_id"] != row["recording_id"]
            or row["review_item_id"] is None
            or row["review_severity"] != "medium"
            or row["review_reason_code"] != expected_reason_code
        ):
            issues.append(
                {
                    "code": "title_suggestion_review_link_mismatch",
                    "proposal_id": proposal_id,
                }
            )
        if (
            row["artifact_recording_id"] != row["recording_id"]
            or row["review_job_id"] != row["artifact_job_id"]
            or row["review_artifact_id"] is None
            or row["review_artifact_id"]
            != row["transcript_artifact_id"]
            or row["artifact_kind"] != "transcript_raw_text"
        ):
            issues.append(
                {
                    "code": "title_suggestion_transcript_link_mismatch",
                    "proposal_id": proposal_id,
                }
            )
        if status in {"suggested", "confirmed"} and (
            row["transcript_archived_at"] is not None
            or row["transcript_job_archived_at"] is not None
            or int(row["transcript_is_latest"] or 0) != 1
            or int(row["transcript_job_is_current"] or 0) != 1
        ):
            issues.append(
                {
                    "code": "active_title_suggestion_transcript_not_current",
                    "proposal_id": proposal_id,
                }
            )

        suggestion_reason = str(row["suggestion_reason"])
        if suggestion_reason == "schedule_content_match":
            if (
                row["classification_recording_id"] != row["recording_id"]
                or row["classification_reason"] != "unique_time_match"
                or (
                    status in {"suggested", "confirmed"}
                    and row["classification_status"]
                    not in {"suggested", "confirmed"}
                )
            ):
                issues.append(
                    {
                        "code": "title_suggestion_classification_link_invalid",
                        "proposal_id": proposal_id,
                    }
                )
        elif row["classification_recording_id"] is not None:
            issues.append(
                {
                    "code": "content_title_suggestion_has_classification_link",
                    "proposal_id": proposal_id,
                }
            )

        try:
            detail = json.loads(str(row["detail_json"]))
        except (TypeError, ValueError):
            detail = None
        detail_valid = (
            isinstance(detail, dict)
            and set(detail) == allowed_detail_fields
            and detail.get("schema_version")
            == "storage-v2/title-suggestion-review@1"
            and detail.get("suggestion_reason") == suggestion_reason
            and detail.get("generator_version")
            == "deterministic-keywords-v1"
            and isinstance(detail.get("transcript_revision"), int)
            and not isinstance(detail.get("transcript_revision"), bool)
            and detail.get("transcript_revision") >= 1
            and detail.get("transcript_revision")
            == row["transcript_revision"]
            and detail.get("classification_status")
            in allowed_classification_statuses
            and detail.get("context_type") in allowed_context_types
        )
        if (
            not detail_valid
            or str(row["detail_json"]) != _canonical_json(detail)
            or row["review_detail_json"] != row["detail_json"]
            or row["generator_version"] != "deterministic-keywords-v1"
            or str(row["proposed_title"])
            != unicodedata.normalize("NFC", str(row["proposed_title"]))
        ):
            issues.append(
                {
                    "code": "title_suggestion_detail_invalid",
                    "proposal_id": proposal_id,
                }
            )
    return issues


def _classification_materialization_integrity(
    conn: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], dict[int, sqlite3.Row]]:
    """Cross-check the closed materialization journal and its DB revisions."""

    # Imported lazily because classification_materialization uses verify_library
    # for its preflight and post-write checks.
    from lecture_stt.storage_v2.classification_materialization import (
        ClassificationMaterializationError,
        CONTEXT_PROVENANCE_SCHEMA_VERSION,
        MATERIALIZATION_PLAN_JSON_MAX_BYTES,
        MATERIALIZED_CONTEXT_JSON_MAX_BYTES,
        materialization_plan_sha256,
        validate_materialization_plan_payload,
    )

    issues: list[dict[str, Any]] = []
    rows = conn.execute(
        """
        SELECT
            materialization.*,
            recording.storage_key AS recording_storage_key,
            recording.manifest_relpath AS recording_manifest_relpath,
            proposal.status AS proposal_status,
            proposal.classification_reason AS proposal_reason,
            proposal.proposed_title AS proposal_title,
            proposal.context_type AS proposal_context_type,
            proposal.label AS proposal_label,
            proposal.semester AS proposal_semester,
            proposal.course_name AS proposal_course_name,
            proposal.course_code AS proposal_course_code,
            proposal.session_date AS proposal_session_date,
            proposal.weekday AS proposal_weekday,
            proposal.start_time AS proposal_start_time,
            proposal.end_time AS proposal_end_time,
            proposal.period_label AS proposal_period_label,
            proposal.period_index AS proposal_period_index,
            proposal.classroom AS proposal_classroom,
            proposal.confidence AS proposal_confidence,
            proposal.schedule_entry_id AS proposal_schedule_entry_id,
            proposal.confirmation_plan_sha256 AS proposal_confirmation_sha256,
            proposal.confirmed_at AS proposal_confirmed_at,
            proposal.updated_at AS proposal_updated_at,
            review.status AS review_status,
            previous_title.recording_id AS previous_title_recording_id,
            previous_title.title AS previous_title_value,
            previous_title.title_source AS previous_title_source,
            previous_title.locale AS previous_title_locale,
            previous_title.confidence AS previous_title_confidence,
            previous_title.is_current AS previous_title_is_current,
            target_title.recording_id AS target_title_recording_id,
            target_title.title AS target_title_value,
            target_title.title_source AS target_title_source,
            target_title.locale AS target_title_locale,
            target_title.confidence AS target_title_confidence,
            target_title.is_current AS target_title_is_current,
            previous_context.recording_id AS previous_context_recording_id,
            previous_context.context_type AS previous_context_type,
            previous_context.label AS previous_context_label,
            previous_context.semester AS previous_context_semester,
            previous_context.course_name AS previous_context_course_name,
            previous_context.course_code AS previous_context_course_code,
            previous_context.session_date AS previous_context_session_date,
            previous_context.period_label AS previous_context_period_label,
            previous_context.period_index AS previous_context_period_index,
            previous_context.source AS previous_context_source,
            previous_context.is_selected AS previous_context_is_selected,
            target_context.recording_id AS target_context_recording_id,
            target_context.context_type AS target_context_type,
            target_context.label AS target_context_label,
            target_context.semester AS target_context_semester,
            target_context.course_name AS target_context_course_name,
            target_context.course_code AS target_context_course_code,
            target_context.session_date AS target_context_session_date,
            target_context.period_label AS target_context_period_label,
            target_context.period_index AS target_context_period_index,
            target_context.context_json AS target_context_json,
            target_context.source AS target_context_source,
            target_context.is_selected AS target_context_is_selected
        FROM recording_classification_materializations AS materialization
        LEFT JOIN recordings AS recording
          ON recording.id = materialization.recording_id
        LEFT JOIN recording_classification_proposals AS proposal
          ON proposal.id = materialization.proposal_id
         AND proposal.recording_id = materialization.recording_id
        LEFT JOIN review_items AS review
          ON review.id = proposal.review_item_id
         AND review.recording_id = proposal.recording_id
        LEFT JOIN recording_titles AS previous_title
          ON previous_title.id = materialization.previous_title_id
         AND previous_title.recording_id = materialization.recording_id
        LEFT JOIN recording_titles AS target_title
          ON target_title.id = materialization.materialized_title_id
         AND target_title.recording_id = materialization.recording_id
        LEFT JOIN recording_contexts AS previous_context
          ON previous_context.id = materialization.previous_context_id
         AND previous_context.recording_id = materialization.recording_id
        LEFT JOIN recording_contexts AS target_context
          ON target_context.id = materialization.materialized_context_id
         AND target_context.recording_id = materialization.recording_id
        ORDER BY materialization.recording_id, materialization.id
        LIMIT 10001
        """
    ).fetchall()
    if len(rows) > 10_000:
        return (
            [
                {
                    "code": "classification_materialization_limit_exceeded",
                    "limit": 10_000,
                }
            ],
            {},
        )

    def title_payload(
        row: sqlite3.Row,
        *,
        prefix: str,
        row_id: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "value": _optional_text(row[f"{prefix}_value"]),
            "source": _optional_text(row[f"{prefix}_source"]),
            "locale": _optional_text(row[f"{prefix}_locale"]),
            "confidence": (
                None
                if row[f"{prefix}_confidence"] is None
                else float(row[f"{prefix}_confidence"])
            ),
        }
        if row_id is not None:
            payload = {"id": row_id, **payload}
        return payload

    def context_payload(
        row: sqlite3.Row,
        *,
        prefix: str,
        row_id: int | None,
        context_json: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": _optional_text(row[f"{prefix}_type"]),
            "label": _optional_text(row[f"{prefix}_label"]),
            "semester": _optional_text(row[f"{prefix}_semester"]),
            "course_name": _optional_text(row[f"{prefix}_course_name"]),
            "course_code": _optional_text(row[f"{prefix}_course_code"]),
            "session_date": _optional_text(row[f"{prefix}_session_date"]),
            "period_label": _optional_text(row[f"{prefix}_period_label"]),
            "period_index": (
                None
                if row[f"{prefix}_period_index"] is None
                else int(row[f"{prefix}_period_index"])
            ),
            "source": _optional_text(row[f"{prefix}_source"]),
        }
        if context_json is not None:
            payload["context_json"] = dict(context_json)
        if row_id is not None:
            payload = {"id": row_id, **payload}
        return payload

    rows_by_recording: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        materialization_id = int(row["id"])
        recording_id = int(row["recording_id"])
        rows_by_recording.setdefault(recording_id, []).append(row)
        state = str(row["state"])
        state_is_valid = (
            state == "prepared" and row["applied_at"] is None
        ) or (
            state == "applied" and row["applied_at"] is not None
        )
        if not state_is_valid or not str(row["prepared_at"] or ""):
            issues.append(
                {
                    "code": "classification_materialization_state_invalid",
                    "materialization_id": materialization_id,
                }
            )
        raw_plan_json = str(row["plan_json"])
        if (
            len(raw_plan_json.encode("utf-8"))
            > MATERIALIZATION_PLAN_JSON_MAX_BYTES
        ):
            issues.append(
                {
                    "code": "classification_materialization_plan_invalid",
                    "materialization_id": materialization_id,
                    "message": "plan JSON exceeds its metadata limit",
                }
            )
            continue
        try:
            decoded_plan = json.loads(raw_plan_json)
            plan = validate_materialization_plan_payload(decoded_plan)
        except (
            ClassificationMaterializationError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            issues.append(
                {
                    "code": "classification_materialization_plan_invalid",
                    "materialization_id": materialization_id,
                    "message": str(exc),
                }
            )
            continue

        try:
            plan_sha256 = materialization_plan_sha256(plan)
        except (
            ClassificationMaterializationError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            issues.append(
                {
                    "code": "classification_materialization_plan_invalid",
                    "materialization_id": materialization_id,
                    "message": str(exc),
                }
            )
            continue
        if plan_sha256 != str(row["materialization_plan_sha256"]):
            issues.append(
                {
                    "code": "classification_materialization_plan_digest_mismatch",
                    "materialization_id": materialization_id,
                }
            )

        journal_matches = (
            int(plan["proposal_id"]) == int(row["proposal_id"])
            and int(plan["recording_id"]) == recording_id
            and int(plan["previous"]["title"]["id"])
            == int(row["previous_title_id"])
            and int(plan["previous"]["context"]["id"])
            == int(row["previous_context_id"])
            and str(plan["confirmation_plan_sha256"])
            == str(row["confirmation_plan_sha256"])
            and str(plan["manifest"]["previous_sha256"])
            == str(row["previous_manifest_sha256"])
            and str(plan["manifest"]["materialized_sha256"])
            == str(row["materialized_manifest_sha256"])
        )
        recording_matches = (
            plan["storage_key"] == row["recording_storage_key"]
            and plan["manifest"]["relpath"]
            == row["recording_manifest_relpath"]
        )
        proposal_matches = (
            row["proposal_status"] == "confirmed"
            and row["proposal_reason"] == "unique_time_match"
            and row["review_status"] == "resolved"
            and plan["proposal_updated_at"] == row["proposal_updated_at"]
            and plan["confirmed_at"] == row["proposal_confirmed_at"]
            and plan["confirmation_plan_sha256"]
            == row["proposal_confirmation_sha256"]
        )
        if not journal_matches or not recording_matches:
            issues.append(
                {
                    "code": "classification_materialization_journal_mismatch",
                    "materialization_id": materialization_id,
                }
            )
        if not proposal_matches:
            issues.append(
                {
                    "code": "classification_materialization_proposal_mismatch",
                    "materialization_id": materialization_id,
                }
            )

        referenced_rows_exist = all(
            row[field] is not None
            for field in (
                "previous_title_recording_id",
                "target_title_recording_id",
                "previous_context_recording_id",
                "target_context_recording_id",
            )
        )
        if not referenced_rows_exist:
            issues.append(
                {
                    "code": "classification_materialization_revision_missing",
                    "materialization_id": materialization_id,
                }
            )
            continue

        previous_title = title_payload(
            row,
            prefix="previous_title",
            row_id=int(row["previous_title_id"]),
        )
        target_title = title_payload(
            row,
            prefix="target_title",
            row_id=None,
        )
        previous_context = context_payload(
            row,
            prefix="previous_context",
            row_id=int(row["previous_context_id"]),
        )
        raw_context_json = str(row["target_context_json"])
        if (
            len(raw_context_json.encode("utf-8"))
            > MATERIALIZED_CONTEXT_JSON_MAX_BYTES
        ):
            decoded_context = None
        else:
            try:
                decoded_context = json.loads(raw_context_json)
            except (TypeError, ValueError):
                decoded_context = None
        target_context = context_payload(
            row,
            prefix="target_context",
            row_id=None,
            context_json=(
                decoded_context if isinstance(decoded_context, dict) else None
            ),
        )
        if previous_title != plan["previous"]["title"]:
            issues.append(
                {
                    "code": "classification_materialization_previous_title_mismatch",
                    "materialization_id": materialization_id,
                }
            )
        if previous_context != plan["previous"]["context"]:
            issues.append(
                {
                    "code": "classification_materialization_previous_context_mismatch",
                    "materialization_id": materialization_id,
                }
            )
        if target_title != plan["target"]["title"]:
            issues.append(
                {
                    "code": "classification_materialization_target_title_mismatch",
                    "materialization_id": materialization_id,
                }
            )
        if (
            not isinstance(decoded_context, dict)
            or target_context != plan["target"]["context"]
        ):
            issues.append(
                {
                    "code": "classification_materialization_target_context_mismatch",
                    "materialization_id": materialization_id,
                }
            )

        expected_provenance = {
            "schema_version": CONTEXT_PROVENANCE_SCHEMA_VERSION,
            "proposal_id": int(row["proposal_id"]),
            "schedule_entry_id": row["proposal_schedule_entry_id"],
            "confirmation_plan_sha256": row["proposal_confirmation_sha256"],
            "weekday": row["proposal_weekday"],
            "start_time": row["proposal_start_time"],
            "end_time": row["proposal_end_time"],
            "classroom": row["proposal_classroom"],
        }
        expected_target_title = {
            "value": row["proposal_title"],
            "source": "schedule",
            "locale": row["previous_title_locale"],
            "confidence": (
                None
                if row["proposal_confidence"] is None
                else float(row["proposal_confidence"])
            ),
        }
        expected_target_context = {
            "type": row["proposal_context_type"],
            "label": row["proposal_label"],
            "semester": row["proposal_semester"],
            "course_name": row["proposal_course_name"],
            "course_code": row["proposal_course_code"],
            "session_date": row["proposal_session_date"],
            "period_label": row["proposal_period_label"],
            "period_index": row["proposal_period_index"],
            "source": "schedule_import",
            "context_json": expected_provenance,
        }
        if (
            plan["target"]["title"] != expected_target_title
            or plan["target"]["context"] != expected_target_context
        ):
            issues.append(
                {
                    "code": "classification_materialization_target_proposal_mismatch",
                    "materialization_id": materialization_id,
                }
            )

    latest_by_recording: dict[int, sqlite3.Row] = {}
    for recording_id, chain in rows_by_recording.items():
        latest = chain[-1]
        latest_by_recording[recording_id] = latest
        for previous, current in zip(chain, chain[1:]):
            if (
                str(previous["state"]) != "applied"
                or int(current["previous_title_id"])
                != int(previous["materialized_title_id"])
                or int(current["previous_context_id"])
                != int(previous["materialized_context_id"])
            ):
                issues.append(
                    {
                        "code": "classification_materialization_chain_mismatch",
                        "recording_id": recording_id,
                        "materialization_id": int(current["id"]),
                    }
                )
        latest_state = str(latest["state"])
        if latest_state == "prepared":
            issues.append(
                {
                    "code": "classification_materialization_recovery_required",
                    "recording_id": recording_id,
                    "materialization_id": int(latest["id"]),
                }
            )
            expected_flags = (1, 1, 0, 0)
        else:
            expected_flags = (0, 0, 1, 1)
        actual_flags = (
            latest["previous_title_is_current"],
            latest["previous_context_is_selected"],
            latest["target_title_is_current"],
            latest["target_context_is_selected"],
        )
        if actual_flags != expected_flags:
            issues.append(
                {
                    "code": "classification_materialization_selection_mismatch",
                    "recording_id": recording_id,
                    "materialization_id": int(latest["id"]),
                }
            )

    return issues, latest_by_recording


def _scan_record_tree(
    record_root_fd: int,
) -> tuple[
    dict[str, str],
    list[dict[str, str]],
    list[dict[str, Any]],
]:
    """Inventory a record root using lstat semantics and never traverse symlinks."""

    entries: dict[str, str] = {}
    failures: list[dict[str, str]] = []
    limits: list[dict[str, Any]] = []
    total_entries = 0
    stopped = False

    def walk(
        directory_fd: int,
        prefix: PurePosixPath | None,
        *,
        depth: int,
    ) -> None:
        nonlocal stopped, total_entries
        if stopped:
            return
        scanned: list[tuple[str, int | None, str | None]] = []
        try:
            with os.scandir(directory_fd) as iterator:
                for entry in iterator:
                    if total_entries >= _MAX_SCAN_ENTRIES:
                        limits.append(
                            {
                                "limit": "entries",
                                "path": (
                                    prefix.as_posix()
                                    if prefix is not None
                                    else "."
                                ),
                                "max_entries": _MAX_SCAN_ENTRIES,
                            }
                        )
                        stopped = True
                        break
                    total_entries += 1
                    try:
                        mode = entry.stat(follow_symlinks=False).st_mode
                    except OSError as exc:
                        scanned.append((entry.name, None, str(exc)))
                    else:
                        scanned.append((entry.name, mode, None))
        except OSError as exc:
            failures.append(
                {
                    "path": prefix.as_posix() if prefix is not None else ".",
                    "message": str(exc),
                }
            )
            return

        for name, mode, error in sorted(scanned, key=lambda item: item[0]):
            relative = PurePosixPath(name) if prefix is None else prefix / name
            relpath = relative.as_posix()
            if mode is None:
                entries[relpath] = "unreadable"
                if error:
                    failures.append({"path": relpath, "message": error})
                continue
            if stat.S_ISLNK(mode):
                entries[relpath] = "symlink"
                continue
            if stat.S_ISDIR(mode):
                entries[relpath] = "directory"
                if depth >= _MAX_SCAN_DEPTH:
                    limits.append(
                        {
                            "limit": "depth",
                            "path": relpath,
                            "max_depth": _MAX_SCAN_DEPTH,
                        }
                    )
                    continue
                try:
                    child_fd = os.open(
                        name,
                        _directory_open_flags(),
                        dir_fd=directory_fd,
                    )
                except OSError as exc:
                    failures.append({"path": relpath, "message": str(exc)})
                    continue
                try:
                    walk(child_fd, relative, depth=depth + 1)
                finally:
                    os.close(child_fd)
                continue
            if stat.S_ISREG(mode):
                entries[relpath] = "file"
                continue
            entries[relpath] = "special"

    walk(record_root_fd, None, depth=0)
    return entries, failures, limits


def _scan_direct_directory(
    directory_fd: int,
) -> tuple[
    dict[str, str],
    list[dict[str, str]],
    dict[str, int] | None,
]:
    entries: dict[str, str] = {}
    failures: list[dict[str, str]] = []
    limit: dict[str, int] | None = None
    try:
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                if len(entries) >= _MAX_SCAN_ENTRIES:
                    limit = {"max_entries": _MAX_SCAN_ENTRIES}
                    break
                try:
                    mode = entry.stat(follow_symlinks=False).st_mode
                except OSError as exc:
                    entries[entry.name] = "unreadable"
                    failures.append(
                        {
                            "path": entry.name,
                            "message": str(exc),
                        }
                    )
                    continue
                if stat.S_ISLNK(mode):
                    entries[entry.name] = "symlink"
                elif stat.S_ISDIR(mode):
                    entries[entry.name] = "directory"
                elif stat.S_ISREG(mode):
                    entries[entry.name] = "file"
                else:
                    entries[entry.name] = "special"
    except OSError as exc:
        failures.append({"path": ".", "message": str(exc)})
    return entries, failures, limit


def verify_library(
    db_path: Path | str,
    records_root: Path | str,
) -> dict[str, Any]:
    root_input = Path(records_root).expanduser()
    if root_input.is_symlink():
        raise ValueError(f"Records root must not be a symlink: {root_input}")
    root = root_input.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Records root is not a directory: {root}")

    issues: list[dict[str, Any]] = []
    checked_artifacts = 0
    current_record_root_fd: int | None = None
    lock_flags = _directory_open_flags()
    root_lock_fd = os.open(root, lock_flags)
    root_identity = _file_snapshot(os.fstat(root_lock_fd))[:2]
    try:
        fcntl.flock(root_lock_fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(root_lock_fd)
        raise
    try:
        conn = connect_v2(db_path, readonly=True)
    except BaseException:
        fcntl.flock(root_lock_fd, fcntl.LOCK_UN)
        os.close(root_lock_fd)
        raise
    try:
        conn.execute("BEGIN")
        require_v2_schema(conn)
        try:
            quick_check = [
                str(row[0])
                for row in conn.execute("PRAGMA quick_check").fetchall()
            ]
        except sqlite3.DatabaseError as exc:
            return {
                "schema_version": "storage-v2/library-verification@1",
                "ok": False,
                "checked_recordings": 0,
                "checked_artifacts": 0,
                "issues": [
                    {
                        "code": "database_quick_check_failed",
                        "details": [str(exc)],
                    }
                ],
            }
        if quick_check != ["ok"]:
            return {
                "schema_version": "storage-v2/library-verification@1",
                "ok": False,
                "checked_recordings": 0,
                "checked_artifacts": 0,
                "issues": [
                    {
                        "code": "database_quick_check_failed",
                        "details": quick_check or ["no result"],
                    }
                ],
            }

        try:
            foreign_key_rows = conn.execute(
                "PRAGMA foreign_key_check"
            ).fetchmany(1001)
        except sqlite3.DatabaseError as exc:
            issues.append(
                {
                    "code": "database_foreign_key_check_failed",
                    "message": str(exc),
                }
            )
        else:
            if foreign_key_rows:
                issues.append(
                    {
                        "code": "database_foreign_key_violation",
                        "reported_violation_count": min(
                            len(foreign_key_rows),
                            1000,
                        ),
                        "truncated": len(foreign_key_rows) > 1000,
                        "violations": [
                            {
                                "table": str(row[0]),
                                "rowid": (
                                    int(row[1])
                                    if row[1] is not None
                                    else None
                                ),
                                "parent": str(row[2]),
                                "foreign_key_id": int(row[3]),
                            }
                            for row in foreign_key_rows[:1000]
                        ],
                    }
                )
        issues.extend(_timetable_integrity_issues(conn))
        issues.extend(_title_suggestion_integrity_issues(conn))
        materialization_issues, latest_materializations = (
            _classification_materialization_integrity(conn)
        )
        issues.extend(materialization_issues)
        # Imported lazily because title_materialization uses verify_library for
        # its own preflight and post-write checks.
        from lecture_stt.storage_v2.title_materialization import (
            title_materialization_integrity_issues,
        )

        (
            title_materialization_issues,
            latest_title_materializations,
        ) = title_materialization_integrity_issues(conn, root_lock_fd)
        issues.extend(title_materialization_issues)
        recordings = conn.execute(
            """
            SELECT
                id,
                storage_key,
                original_name_raw,
                original_name_nfc,
                manifest_relpath,
                source_relpath,
                source_state,
                ingest_sha256,
                ingest_bytes,
                source_mime,
                created_at
            FROM recordings
            WHERE archived_at IS NULL
            ORDER BY id ASC
            """
        ).fetchall()
        expected_record_roots = {
            str(recording["storage_key"])
            for recording in recordings
        }
        for recording in recordings:
            recording_id = int(recording["id"])
            storage_key = str(recording["storage_key"])
            try:
                validate_storage_key(storage_key)
            except ManifestValidationError as exc:
                issues.append(
                    {
                        "recording_id": recording_id,
                        "code": "invalid_storage_key",
                        "message": str(exc),
                    }
                )
                continue
            try:
                current_record_root_fd = os.open(
                    storage_key,
                    _directory_open_flags(),
                    dir_fd=root_lock_fd,
                )
            except OSError as exc:
                issues.append(
                    {
                        "recording_id": recording_id,
                        "storage_key": storage_key,
                        "code": "record_root_missing_or_unsafe",
                        "message": str(exc),
                    }
                )
                continue

            manifest: dict[str, Any] | None = None
            manifest_sha256: str | None = None
            indexed_record_files: set[str] = set()
            manifest_relpath = str(recording["manifest_relpath"])
            try:
                validated_manifest_relpath = validate_relative_path(
                    manifest_relpath,
                    field="manifest_relpath",
                )
            except ManifestValidationError as exc:
                issues.append(
                    {
                        "recording_id": recording_id,
                        "storage_key": storage_key,
                        "code": "invalid_manifest_path",
                        "message": str(exc),
                    }
                )
            else:
                indexed_record_files.add(validated_manifest_relpath)
                manifest_fd: int | None = None
                try:
                    manifest_fd = _open_regular_file_at(
                        current_record_root_fd,
                        validated_manifest_relpath,
                    )
                    manifest_bytes = _read_bounded_fd(
                        manifest_fd,
                        max_bytes=_MAX_MANIFEST_BYTES,
                    )
                    manifest_sha256 = hashlib.sha256(
                        manifest_bytes
                    ).hexdigest()
                except OSError as exc:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "path": validated_manifest_relpath,
                            "code": "manifest_unreadable",
                            "message": str(exc),
                        }
                    )
                except ManifestValidationError as exc:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "code": "manifest_invalid",
                            "message": str(exc),
                        }
                    )
                else:
                    try:
                        parsed_manifest = json.loads(
                            manifest_bytes.decode("utf-8")
                        )
                        if not isinstance(parsed_manifest, dict):
                            raise ManifestValidationError(
                                "Manifest root must be an object"
                            )
                        validate_manifest(parsed_manifest)
                        if parsed_manifest.get("storage_key") != storage_key:
                            raise ManifestValidationError(
                                "Manifest storage_key does not match DB"
                            )
                        manifest = parsed_manifest
                    except (ValueError, ManifestValidationError) as exc:
                        issues.append(
                            {
                                "recording_id": recording_id,
                                "storage_key": storage_key,
                                "code": "manifest_invalid",
                                "message": str(exc),
                            }
                        )
                finally:
                    if manifest_fd is not None:
                        os.close(manifest_fd)

            latest_materialization = latest_materializations.get(recording_id)
            if (
                latest_materialization is not None
                and manifest_sha256 is not None
            ):
                materialization_id = int(latest_materialization["id"])
                previous_sha256 = str(
                    latest_materialization["previous_manifest_sha256"]
                )
                materialized_sha256 = str(
                    latest_materialization["materialized_manifest_sha256"]
                )
                state = str(latest_materialization["state"])
                allowed_digests = (
                    {previous_sha256, materialized_sha256}
                    if state == "prepared"
                    else {materialized_sha256}
                )
                if manifest_sha256 not in allowed_digests:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "materialization_id": materialization_id,
                            "code": (
                                "classification_materialization_manifest_digest_mismatch"
                            ),
                        }
                    )

            latest_title_materialization = (
                latest_title_materializations.get(recording_id)
            )
            if (
                latest_title_materialization is not None
                and manifest_sha256 is not None
            ):
                title_materialization_id = int(
                    latest_title_materialization["id"]
                )
                previous_sha256 = str(
                    latest_title_materialization[
                        "previous_manifest_sha256"
                    ]
                )
                materialized_sha256 = str(
                    latest_title_materialization[
                        "materialized_manifest_sha256"
                    ]
                )
                state = str(latest_title_materialization["state"])
                allowed_digests = (
                    {previous_sha256, materialized_sha256}
                    if state == "prepared"
                    else {materialized_sha256}
                )
                if manifest_sha256 not in allowed_digests:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "materialization_id": title_materialization_id,
                            "code": (
                                "title_materialization_manifest_digest_mismatch"
                            ),
                        }
                    )

            artifacts = conn.execute(
                """
                SELECT
                    id,
                    job_id,
                    engine_run_id,
                    artifact_kind,
                    path_rel,
                    content_sha256,
                    bytes,
                    mime_type
                FROM artifacts
                WHERE recording_id = ? AND archived_at IS NULL
                ORDER BY id ASC
                """,
                (recording_id,),
            ).fetchall()
            artifact_paths = {str(artifact["path_rel"]) for artifact in artifacts}
            source_relpath = str(recording["source_relpath"])
            canonical_source_artifact = next(
                (
                    artifact
                    for artifact in artifacts
                    if str(artifact["path_rel"]) == source_relpath
                ),
                None,
            )
            if (
                str(recording["source_state"]) == "available"
                and canonical_source_artifact is not None
            ):
                source_artifact_metadata = {
                    "sha256": _optional_text(
                        canonical_source_artifact["content_sha256"]
                    ),
                    "bytes": (
                        int(canonical_source_artifact["bytes"])
                        if canonical_source_artifact["bytes"] is not None
                        else None
                    ),
                    "mime_type": _optional_text(
                        canonical_source_artifact["mime_type"]
                    ),
                }
                recording_ingest_metadata = {
                    "sha256": _optional_text(recording["ingest_sha256"]),
                    "bytes": (
                        int(recording["ingest_bytes"])
                        if recording["ingest_bytes"] is not None
                        else None
                    ),
                    "mime_type": _optional_text(recording["source_mime"]),
                }
                mismatched_source_artifact_fields = sorted(
                    field
                    for field in recording_ingest_metadata
                    if source_artifact_metadata[field]
                    != recording_ingest_metadata[field]
                )
                if mismatched_source_artifact_fields:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "artifact_id": int(
                                canonical_source_artifact["id"]
                            ),
                            "path": source_relpath,
                            "code": "source_artifact_metadata_mismatch",
                            "fields": mismatched_source_artifact_fields,
                        }
                    )

            current_jobs = conn.execute(
                """
                WITH selected_engines AS (
                    SELECT
                        job_id,
                        COUNT(*) AS selected_engine_count,
                        MIN(id) AS engine_run_id,
                        MIN(engine_name) AS engine_name,
                        MIN(engine_version) AS engine_version,
                        MIN(status) AS engine_status,
                        MIN(started_at) AS engine_started_at,
                        MIN(finished_at) AS engine_finished_at,
                        MIN(stderr_relpath) AS engine_stderr_relpath,
                        MIN(log_relpath) AS engine_log_relpath
                    FROM engine_runs
                    WHERE is_selected = 1
                      AND archived_at IS NULL
                    GROUP BY job_id
                )
                SELECT
                    j.id,
                    j.job_key,
                    j.job_relpath,
                    j.status,
                    j.requested_profile,
                    j.requested_profile_version,
                    j.queued_at,
                    j.started_at AS job_started_at,
                    j.finished_at AS job_finished_at,
                    COALESCE(e.selected_engine_count, 0)
                        AS selected_engine_count,
                    e.engine_run_id,
                    e.engine_name,
                    e.engine_version,
                    e.engine_status,
                    e.engine_started_at,
                    e.engine_finished_at,
                    e.engine_stderr_relpath,
                    e.engine_log_relpath
                FROM transcription_jobs AS j
                LEFT JOIN selected_engines AS e
                  ON e.job_id = j.id
                WHERE j.recording_id = ?
                  AND j.archived_at IS NULL
                ORDER BY j.id ASC
                """,
                (recording_id,),
            ).fetchall()
            for job in current_jobs:
                selected_engine_count = int(job["selected_engine_count"])
                if selected_engine_count != 1:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "job_id": int(job["id"]),
                            "job_key": str(job["job_key"]),
                            "code": "current_job_selected_engine_invariant",
                            "selected_engine_count": selected_engine_count,
                        }
                    )
                    continue
                job_status = str(job["status"])
                engine_status = str(job["engine_status"])
                if engine_status not in _ENGINE_STATUSES_BY_JOB_STATUS[job_status]:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "job_id": int(job["id"]),
                            "job_key": str(job["job_key"]),
                            "code": "selected_engine_status_mismatch",
                            "job_status": job_status,
                            "engine_status": engine_status,
                            "allowed_engine_statuses": sorted(
                                _ENGINE_STATUSES_BY_JOB_STATUS[job_status]
                            ),
                        }
                    )
            current_job_keys_by_id = {
                int(job["id"]): str(job["job_key"])
                for job in current_jobs
            }
            job_relpaths_by_id = {
                int(job["id"]): str(job["job_relpath"])
                for job in current_jobs
            }
            selected_engine_ids_by_job_id = {
                int(job["id"]): (
                    int(job["engine_run_id"])
                    if int(job["selected_engine_count"]) == 1
                    and job["engine_run_id"] is not None
                    else None
                )
                for job in current_jobs
            }
            for artifact in artifacts:
                artifact_id = int(artifact["id"])
                artifact_job_id = int(artifact["job_id"])
                artifact_kind = str(artifact["artifact_kind"])
                artifact_path = str(artifact["path_rel"])
                actual_engine_run_id = (
                    int(artifact["engine_run_id"])
                    if artifact["engine_run_id"] is not None
                    else None
                )
                if artifact_path == source_relpath:
                    expected_source_kind = (
                        "source_copy"
                        if str(recording["source_state"]) == "available"
                        else "metadata"
                    )
                    if artifact_kind != expected_source_kind:
                        issues.append(
                            {
                                "recording_id": recording_id,
                                "storage_key": storage_key,
                                "artifact_id": artifact_id,
                                "path": artifact_path,
                                "code": "source_artifact_kind_mismatch",
                                "artifact_kind": artifact_kind,
                                "expected_artifact_kind": expected_source_kind,
                            }
                        )
                    if actual_engine_run_id is not None:
                        issues.append(
                            {
                                "recording_id": recording_id,
                                "storage_key": storage_key,
                                "artifact_id": artifact_id,
                                "path": artifact_path,
                                "code": "artifact_engine_run_ownership_mismatch",
                                "job_id": artifact_job_id,
                                "job_key": current_job_keys_by_id.get(
                                    artifact_job_id
                                ),
                                "expected_engine_run_id": None,
                                "actual_engine_run_id": actual_engine_run_id,
                            }
                        )
                    continue
                owner_key = current_job_keys_by_id.get(artifact_job_id)
                if owner_key is None:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "artifact_id": artifact_id,
                            "path": artifact_path,
                            "code": "active_artifact_job_ownership_invariant",
                            "job_id": artifact_job_id,
                        }
                    )
                    continue
                expected_job_prefix = (
                    f"{job_relpaths_by_id[artifact_job_id]}/"
                )
                if not artifact_path.startswith(expected_job_prefix):
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "artifact_id": artifact_id,
                            "path": artifact_path,
                            "code": "artifact_job_namespace_mismatch",
                            "job_id": artifact_job_id,
                            "job_key": owner_key,
                            "expected_prefix": expected_job_prefix,
                        }
                    )
                selected_engine_run_id = selected_engine_ids_by_job_id[
                    artifact_job_id
                ]
                engine_ownership_mismatch = (
                    actual_engine_run_id != selected_engine_run_id
                    if artifact_kind in _ENGINE_BOUND_ARTIFACT_KINDS
                    else (
                        actual_engine_run_id is not None
                        and actual_engine_run_id != selected_engine_run_id
                    )
                )
                if engine_ownership_mismatch:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "artifact_id": artifact_id,
                            "path": artifact_path,
                            "code": "artifact_engine_run_ownership_mismatch",
                            "job_id": artifact_job_id,
                            "job_key": owner_key,
                            "expected_engine_run_id": selected_engine_run_id,
                            "actual_engine_run_id": actual_engine_run_id,
                        }
                    )
            current_title = conn.execute(
                """
                SELECT title, title_source
                FROM recording_titles
                WHERE recording_id = ? AND is_current = 1
                """,
                (recording_id,),
            ).fetchone()
            selected_context = conn.execute(
                """
                SELECT
                    context_type,
                    label,
                    semester,
                    course_name,
                    course_code,
                    session_date,
                    period_label,
                    source
                FROM recording_contexts
                WHERE recording_id = ? AND is_selected = 1
                """,
                (recording_id,),
            ).fetchone()

            if manifest is not None:
                manifest_artifact_index = {
                    str(entry["path"]): (
                        str(entry["kind"]),
                        str(entry["sha256"]),
                        int(entry["bytes"]),
                        _optional_text(entry.get("mime_type")),
                    )
                    for entry in manifest["artifacts"]
                }
                database_artifact_index = {
                    str(artifact["path_rel"]): (
                        str(artifact["artifact_kind"]),
                        (
                            str(artifact["content_sha256"])
                            if artifact["content_sha256"] is not None
                            else None
                        ),
                        int(artifact["bytes"]) if artifact["bytes"] is not None else None,
                        _optional_text(artifact["mime_type"]),
                    )
                    for artifact in artifacts
                }

                manifest_recording_fields = {
                    "original_name_raw": _optional_text(
                        manifest["recording"].get("original_name_raw")
                    ),
                    "original_name_nfc": _optional_text(
                        manifest["recording"].get("original_name_nfc")
                    ),
                    "created_at": _optional_text(
                        manifest["recording"].get("created_at")
                    ),
                }
                database_recording_fields = {
                    "original_name_raw": str(recording["original_name_raw"]),
                    "original_name_nfc": str(recording["original_name_nfc"]),
                    "created_at": str(recording["created_at"]),
                }
                mismatched_recording_fields = sorted(
                    field
                    for field in database_recording_fields
                    if manifest_recording_fields[field]
                    != database_recording_fields[field]
                )
                if mismatched_recording_fields:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "code": "manifest_recording_mismatch",
                            "fields": mismatched_recording_fields,
                        }
                    )

                manifest_title_fields = {
                    "value": _optional_text(manifest["title"].get("value")),
                    "source": _optional_text(manifest["title"].get("source")),
                }
                database_title_fields = {
                    "value": (
                        str(current_title["title"])
                        if current_title is not None
                        else None
                    ),
                    "source": (
                        str(current_title["title_source"])
                        if current_title is not None
                        else None
                    ),
                }
                mismatched_title_fields = sorted(
                    field
                    for field in database_title_fields
                    if manifest_title_fields[field] != database_title_fields[field]
                )
                if mismatched_title_fields:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "code": "manifest_title_mismatch",
                            "fields": mismatched_title_fields,
                        }
                    )

                context_field_map = {
                    "type": "context_type",
                    "label": "label",
                    "semester": "semester",
                    "course_name": "course_name",
                    "course_code": "course_code",
                    "session_date": "session_date",
                    "period_label": "period_label",
                    "source": "source",
                }
                manifest_context_fields = {
                    manifest_field: _optional_text(
                        manifest["context"].get(manifest_field)
                    )
                    for manifest_field in context_field_map
                }
                database_context_fields = {
                    manifest_field: (
                        _optional_text(selected_context[database_field])
                        if selected_context is not None
                        else None
                    )
                    for manifest_field, database_field in context_field_map.items()
                }
                mismatched_context_fields = sorted(
                    field
                    for field in database_context_fields
                    if manifest_context_fields[field]
                    != database_context_fields[field]
                )
                if mismatched_context_fields:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "code": "manifest_context_mismatch",
                            "fields": mismatched_context_fields,
                        }
                    )

                if manifest_artifact_index != database_artifact_index:
                    manifest_paths = set(manifest_artifact_index)
                    database_paths = set(database_artifact_index)
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "code": "manifest_artifact_index_mismatch",
                            "manifest_only_paths": sorted(
                                manifest_paths - database_paths
                            ),
                            "database_only_paths": sorted(
                                database_paths - manifest_paths
                            ),
                            "metadata_mismatch_paths": sorted(
                                path
                                for path in manifest_paths & database_paths
                                if manifest_artifact_index[path]
                                != database_artifact_index[path]
                            ),
                        }
                    )

                manifest_source = manifest["source"]
                database_source_artifact = next(
                    (
                        artifact
                        for artifact in artifacts
                        if str(artifact["path_rel"]) == source_relpath
                    ),
                    None,
                )
                source_fields = {
                    "path": (
                        str(manifest_source.get("path"))
                        if manifest_source.get("path") is not None
                        else None
                    ),
                    "availability": manifest_source.get("availability"),
                    "sha256": manifest_source.get("sha256"),
                    "bytes": manifest_source.get("bytes"),
                    "mime_type": _optional_text(
                        manifest_source.get("mime_type")
                    ),
                    "marker_sha256": _optional_text(
                        manifest_source.get("marker_sha256")
                    ),
                }
                database_source_fields = {
                    "path": str(recording["source_relpath"]),
                    "availability": str(recording["source_state"]),
                    "sha256": (
                        str(recording["ingest_sha256"])
                        if recording["ingest_sha256"] is not None
                        else None
                    ),
                    "bytes": (
                        int(recording["ingest_bytes"])
                        if recording["ingest_bytes"] is not None
                        else None
                    ),
                    "mime_type": _optional_text(recording["source_mime"]),
                    "marker_sha256": (
                        _optional_text(
                            database_source_artifact["content_sha256"]
                        )
                        if recording["source_state"] == "missing"
                        and database_source_artifact is not None
                        else None
                    ),
                }
                mismatched_source_fields = sorted(
                    field
                    for field in database_source_fields
                    if source_fields[field] != database_source_fields[field]
                )
                if mismatched_source_fields:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "code": "manifest_source_mismatch",
                            "fields": mismatched_source_fields,
                        }
                    )

                manifest_jobs = []
                for job in manifest["jobs"]:
                    engine = job.get("engine")
                    engine_fields = engine if isinstance(engine, dict) else {}
                    manifest_jobs.append(
                        (
                            str(job.get("job_key", "")),
                            f"jobs/{job.get('job_key', '')}",
                            str(job.get("status", "")),
                            _optional_text(job.get("requested_profile")),
                            _optional_text(job.get("requested_profile_version")),
                            _optional_text(job.get("queued_at")),
                            _optional_text(job.get("started_at")),
                            _optional_text(job.get("finished_at")),
                            _optional_text(engine_fields.get("name")),
                            _optional_text(engine_fields.get("version")),
                            _optional_text(engine_fields.get("started_at")),
                            _optional_text(engine_fields.get("finished_at")),
                            _optional_text(
                                engine_fields.get("stderr_relpath")
                            ),
                            _optional_text(engine_fields.get("log_relpath")),
                        )
                    )
                manifest_jobs.sort()
                database_jobs = sorted(
                    (
                        str(job["job_key"]),
                        str(job["job_relpath"]),
                        str(job["status"]),
                        _optional_text(job["requested_profile"]),
                        _optional_text(job["requested_profile_version"]),
                        _optional_text(job["queued_at"]),
                        _optional_text(job["job_started_at"]),
                        _optional_text(job["job_finished_at"]),
                        _optional_text(job["engine_name"]),
                        _optional_text(job["engine_version"]),
                        _optional_text(job["engine_started_at"]),
                        _optional_text(job["engine_finished_at"]),
                        _optional_text(job["engine_stderr_relpath"]),
                        _optional_text(job["engine_log_relpath"]),
                    )
                    for job in current_jobs
                )
                if manifest_jobs != database_jobs:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "code": "manifest_jobs_mismatch",
                            "manifest_job_count": len(manifest_jobs),
                            "database_job_count": len(database_jobs),
                        }
                    )

                database_job_artifact_paths = {
                    str(job["job_key"]): set()
                    for job in current_jobs
                }
                for artifact in artifacts:
                    path = str(artifact["path_rel"])
                    artifact_kind = str(artifact["artifact_kind"])
                    if path == source_relpath:
                        continue
                    owner_key = current_job_keys_by_id.get(int(artifact["job_id"]))
                    if owner_key is not None:
                        database_job_artifact_paths[owner_key].add(path)
                manifest_job_artifact_paths = {
                    str(job["job_key"]): {
                        str(path)
                        for path in job.get("artifact_paths", [])
                    }
                    for job in manifest["jobs"]
                }
                manifest_job_owners_by_path: dict[str, list[str]] = {}
                for job_key, paths in manifest_job_artifact_paths.items():
                    for path in paths:
                        manifest_job_owners_by_path.setdefault(
                            path,
                            [],
                        ).append(job_key)
                for artifact in artifacts:
                    path = str(artifact["path_rel"])
                    artifact_kind = str(artifact["artifact_kind"])
                    if path == source_relpath:
                        continue
                    artifact_job_id = int(artifact["job_id"])
                    database_owner = current_job_keys_by_id.get(
                        artifact_job_id
                    )
                    manifest_owners = sorted(
                        manifest_job_owners_by_path.get(path, [])
                    )
                    if (
                        database_owner is None
                        or manifest_owners != [database_owner]
                    ):
                        issues.append(
                            {
                                "recording_id": recording_id,
                                "storage_key": storage_key,
                                "artifact_id": int(artifact["id"]),
                                "path": path,
                                "code": "artifact_manifest_job_ownership_mismatch",
                                "job_id": artifact_job_id,
                                "database_owner_job_key": database_owner,
                                "manifest_owner_job_keys": manifest_owners,
                            }
                        )
                job_path_mismatches = []
                for job_key in sorted(
                    set(manifest_job_artifact_paths)
                    | set(database_job_artifact_paths)
                ):
                    manifest_paths = manifest_job_artifact_paths.get(job_key, set())
                    database_paths = database_job_artifact_paths.get(job_key, set())
                    if manifest_paths != database_paths:
                        job_path_mismatches.append(
                            {
                                "job_key": job_key,
                                "manifest_only_paths": sorted(
                                    manifest_paths - database_paths
                                ),
                                "database_only_paths": sorted(
                                    database_paths - manifest_paths
                                ),
                            }
                        )
                if job_path_mismatches:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "code": "manifest_job_artifact_paths_mismatch",
                            "jobs": job_path_mismatches,
                        }
                    )

                legacy = manifest["legacy"]
                fingerprint = str(legacy.get("source_fingerprint") or "")
                expected_legacy_map: dict[tuple[str, str], str] = {}
                expected_legacy_snapshots: dict[
                    tuple[str, str],
                    dict[str, Any],
                ] = {}
                legacy_field_errors: list[str] = []
                if legacy.get("kind") != "job":
                    legacy_field_errors.append("kind")
                legacy_job_id = legacy.get("job_id")
                if (
                    isinstance(legacy_job_id, bool)
                    or not isinstance(legacy_job_id, int)
                    or legacy_job_id < 1
                ):
                    legacy_field_errors.append("job_id")
                else:
                    job_map_key = ("job", str(legacy_job_id))
                    expected_legacy_map[job_map_key] = fingerprint
                    expected_legacy_snapshots[job_map_key] = {
                        "canonical_base": legacy.get("canonical_base"),
                        "status": legacy.get("status"),
                    }
                delivery_key = legacy.get("delivery_key")
                if delivery_key is not None:
                    if not isinstance(delivery_key, str) or not delivery_key:
                        legacy_field_errors.append("delivery_key")
                    else:
                        delivery_map_key = ("delivery", delivery_key)
                        expected_legacy_map[delivery_map_key] = fingerprint
                        expected_legacy_snapshots[delivery_map_key] = {
                            "logical_stem": delivery_key,
                        }
                legacy_rows = conn.execute(
                    """
                    SELECT
                        legacy_kind,
                        legacy_key,
                        source_fingerprint,
                        legacy_snapshot_json
                    FROM legacy_import_map
                    WHERE recording_id = ?
                      AND legacy_kind IN ('job', 'delivery')
                    ORDER BY legacy_kind, legacy_key
                    """,
                    (recording_id,),
                ).fetchall()
                database_legacy_map = {
                    (
                        str(row["legacy_kind"]),
                        str(row["legacy_key"]),
                    ): str(row["source_fingerprint"])
                    for row in legacy_rows
                }
                database_legacy_snapshots: dict[
                    tuple[str, str],
                    dict[str, Any] | None,
                ] = {}
                for row in legacy_rows:
                    map_key = (
                        str(row["legacy_kind"]),
                        str(row["legacy_key"]),
                    )
                    raw_snapshot = row["legacy_snapshot_json"]
                    if raw_snapshot is None:
                        database_legacy_snapshots[map_key] = None
                        continue
                    try:
                        parsed_snapshot = json.loads(str(raw_snapshot))
                    except ValueError:
                        database_legacy_snapshots[map_key] = None
                    else:
                        database_legacy_snapshots[map_key] = (
                            parsed_snapshot
                            if isinstance(parsed_snapshot, dict)
                            else None
                        )
                shared_legacy_keys = (
                    set(expected_legacy_map) & set(database_legacy_map)
                )
                snapshot_mismatch_keys = {
                    key
                    for key in shared_legacy_keys
                    if expected_legacy_snapshots.get(key)
                    != database_legacy_snapshots.get(key)
                }
                if (
                    legacy_field_errors
                    or expected_legacy_map != database_legacy_map
                    or snapshot_mismatch_keys
                ):
                    expected_keys = set(expected_legacy_map)
                    database_keys = set(database_legacy_map)
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "code": "manifest_legacy_import_map_mismatch",
                            "invalid_manifest_fields": sorted(
                                set(legacy_field_errors)
                            ),
                            "manifest_only_keys": [
                                {"kind": kind, "key": key}
                                for kind, key in sorted(
                                    expected_keys - database_keys
                                )
                            ],
                            "database_only_keys": [
                                {"kind": kind, "key": key}
                                for kind, key in sorted(
                                    database_keys - expected_keys
                                )
                            ],
                            "fingerprint_mismatch_keys": [
                                {"kind": kind, "key": key}
                                for kind, key in sorted(
                                    expected_keys & database_keys
                                )
                                if expected_legacy_map[(kind, key)]
                                != database_legacy_map[(kind, key)]
                            ],
                            "snapshot_mismatch_keys": [
                                {"kind": kind, "key": key}
                                for kind, key in sorted(
                                    snapshot_mismatch_keys
                                )
                            ],
                        }
                    )

            for artifact in artifacts:
                checked_artifacts += 1
                artifact_id = int(artifact["id"])
                relpath = str(artifact["path_rel"])
                try:
                    validate_relative_path(relpath, field="artifact.path_rel")
                except ManifestValidationError as exc:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "artifact_id": artifact_id,
                            "code": "invalid_artifact_path",
                            "message": str(exc),
                        }
                    )
                    continue
                indexed_record_files.add(relpath)
                artifact_fd: int | None = None
                try:
                    artifact_fd = _open_regular_file_at(
                        current_record_root_fd,
                        relpath,
                    )
                    artifact_metadata = os.fstat(artifact_fd)
                    expected_bytes = artifact["bytes"]
                    if (
                        expected_bytes is not None
                        and artifact_metadata.st_size != int(expected_bytes)
                    ):
                        issues.append(
                            {
                                "recording_id": recording_id,
                                "artifact_id": artifact_id,
                                "path": relpath,
                                "code": "artifact_size_mismatch",
                            }
                        )
                        continue
                    expected_sha = str(artifact["content_sha256"] or "")
                    if (
                        expected_sha
                        and _compute_sha256_fd(
                            artifact_fd,
                            before=artifact_metadata,
                        )
                        != expected_sha
                    ):
                        issues.append(
                            {
                                "recording_id": recording_id,
                                "artifact_id": artifact_id,
                                "path": relpath,
                                "code": "artifact_hash_mismatch",
                            }
                        )
                except OSError as exc:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "artifact_id": artifact_id,
                            "path": relpath,
                            "code": "artifact_unreadable",
                            "message": str(exc),
                        }
                    )
                finally:
                    if artifact_fd is not None:
                        os.close(artifact_fd)

            if source_relpath not in artifact_paths:
                issues.append(
                    {
                        "recording_id": recording_id,
                        "storage_key": storage_key,
                        "code": "source_artifact_not_indexed",
                        "path": source_relpath,
                    }
                )
            if recording["source_state"] == "missing" and source_relpath != "source/unavailable.json":
                issues.append(
                    {
                        "recording_id": recording_id,
                        "storage_key": storage_key,
                        "code": "missing_source_marker_mismatch",
                        "path": source_relpath,
                    }
                )

            expected_directories = _expected_directories(indexed_record_files)
            record_entries, scan_failures, scan_limits = _scan_record_tree(
                current_record_root_fd
            )
            for failure in scan_failures:
                issues.append(
                    {
                        "recording_id": recording_id,
                        "storage_key": storage_key,
                        "code": "record_entry_unreadable",
                        **failure,
                    }
                )
            for limit in scan_limits:
                limit_kind = str(limit["limit"])
                issues.append(
                    {
                        "recording_id": recording_id,
                        "storage_key": storage_key,
                        "code": (
                            "record_scan_depth_limit_exceeded"
                            if limit_kind == "depth"
                            else "record_scan_entry_limit_exceeded"
                        ),
                        **{
                            key: value
                            for key, value in limit.items()
                            if key != "limit"
                        },
                    }
                )
            for relpath, entry_type in sorted(record_entries.items()):
                if entry_type == "symlink":
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "code": "record_entry_symlink",
                            "path": relpath,
                        }
                    )
                    continue
                if entry_type == "special":
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "code": "record_entry_special",
                            "path": relpath,
                        }
                    )
                    continue
                if entry_type == "unreadable":
                    continue
                if entry_type == "file" and relpath not in indexed_record_files:
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "code": "unindexed_record_file",
                            "path": relpath,
                        }
                    )
                    continue
                if (
                    entry_type == "directory"
                    and relpath not in expected_directories
                ):
                    issues.append(
                        {
                            "recording_id": recording_id,
                            "storage_key": storage_key,
                            "code": "unindexed_record_directory",
                            "path": relpath,
                        }
                    )

            os.close(current_record_root_fd)
            current_record_root_fd = None

        root_entries, root_failures, root_limit = _scan_direct_directory(
            root_lock_fd
        )
        for failure in root_failures:
            issues.append(
                {
                    "code": "records_root_entry_unreadable",
                    **failure,
                }
            )
        if root_limit is not None:
            issues.append(
                {
                    "code": "records_root_scan_entry_limit_exceeded",
                    **root_limit,
                }
            )
        for entry_name, entry_type in sorted(root_entries.items()):
            if entry_name == ".locks":
                if entry_type != "directory":
                    issues.append(
                        {
                            "code": "internal_directory_unsafe",
                            "path": entry_name,
                            "entry_type": entry_type,
                        }
                    )
                    continue
                try:
                    lock_root_fd = os.open(
                        ".locks",
                        _directory_open_flags(),
                        dir_fd=root_lock_fd,
                    )
                except OSError as exc:
                    issues.append(
                        {
                            "code": "internal_directory_unsafe",
                            "path": entry_name,
                            "message": str(exc),
                        }
                    )
                    continue
                expected_lock_names = {
                    f"{storage_key}.lock"
                    for storage_key in expected_record_roots
                }
                lock_entries, lock_failures, lock_limits = _scan_record_tree(
                    lock_root_fd
                )
                for failure in lock_failures:
                    issues.append(
                        {
                            "code": "internal_entry_unreadable",
                            "path": f".locks/{failure['path']}",
                            "message": failure["message"],
                        }
                    )
                for limit in lock_limits:
                    limit_kind = str(limit["limit"])
                    issues.append(
                        {
                            "code": (
                                "internal_scan_depth_limit_exceeded"
                                if limit_kind == "depth"
                                else "internal_scan_entry_limit_exceeded"
                            ),
                            **{
                                key: value
                                for key, value in limit.items()
                                if key != "limit"
                            },
                        }
                    )
                for relpath, entry_type in sorted(lock_entries.items()):
                    internal_path = f".locks/{relpath}"
                    if (
                        entry_type != "file"
                        or "/" in relpath
                        or relpath not in expected_lock_names
                    ):
                        issues.append(
                            {
                                "code": "internal_unexpected_entry",
                                "path": internal_path,
                                "entry_type": entry_type,
                            }
                        )
                        continue
                    lock_fd: int | None = None
                    try:
                        lock_fd = _open_regular_file_at(
                            lock_root_fd,
                            relpath,
                        )
                        lock_bytes = os.fstat(lock_fd).st_size
                    except OSError as exc:
                        issues.append(
                            {
                                "code": "internal_entry_unreadable",
                                "path": internal_path,
                                "message": str(exc),
                            }
                        )
                    else:
                        if lock_bytes != 0:
                            issues.append(
                                {
                                    "code": "internal_lock_file_not_empty",
                                    "path": internal_path,
                                    "bytes": lock_bytes,
                                }
                            )
                    finally:
                        if lock_fd is not None:
                            os.close(lock_fd)
                os.close(lock_root_fd)
                continue
            if entry_name == ".staging":
                if entry_type != "directory":
                    issues.append(
                        {
                            "code": "internal_directory_unsafe",
                            "path": entry_name,
                            "entry_type": entry_type,
                        }
                    )
                    continue
                staging_root_fd: int | None = None
                try:
                    staging_root_fd = os.open(
                        ".staging",
                        _directory_open_flags(),
                        dir_fd=root_lock_fd,
                    )
                    (
                        staging_entries,
                        staging_failures,
                        staging_limit,
                    ) = _scan_direct_directory(staging_root_fd)
                except OSError as exc:
                    issues.append(
                        {
                            "code": "internal_directory_unsafe",
                            "path": entry_name,
                            "message": str(exc),
                        }
                    )
                    continue
                finally:
                    if staging_root_fd is not None:
                        os.close(staging_root_fd)
                for failure in staging_failures:
                    issues.append(
                        {
                            "code": "internal_entry_unreadable",
                            "path": f".staging/{failure['path']}",
                            "message": failure["message"],
                        }
                    )
                if staging_limit is not None:
                    issues.append(
                        {
                            "code": "internal_scan_entry_limit_exceeded",
                            "path": ".staging",
                            **staging_limit,
                        }
                    )
                stale_entries = sorted(staging_entries)
                if stale_entries:
                    issues.append(
                        {
                            "code": "stale_staging_entries",
                            "count": len(stale_entries),
                            "entries": stale_entries[:20],
                            "truncated": staging_limit is not None,
                        }
                    )
                continue
            if entry_name not in expected_record_roots:
                issues.append(
                    {
                        "code": (
                            "orphan_record_directory"
                            if entry_type == "directory"
                            else "unexpected_records_root_entry"
                        ),
                        "path": entry_name,
                        "entry_type": entry_type,
                    }
                )

        current_root_fd: int | None = None
        try:
            current_root_fd = os.open(root, _directory_open_flags())
            current_root_identity = _file_snapshot(
                os.fstat(current_root_fd)
            )[:2]
        except OSError as exc:
            issues.append(
                {
                    "code": "records_root_identity_changed",
                    "path": str(root),
                    "message": str(exc),
                }
            )
        else:
            if current_root_identity != root_identity:
                issues.append(
                    {
                        "code": "records_root_identity_changed",
                        "path": str(root),
                        "expected_dev": root_identity[0],
                        "expected_ino": root_identity[1],
                        "actual_dev": current_root_identity[0],
                        "actual_ino": current_root_identity[1],
                    }
                )
        finally:
            if current_root_fd is not None:
                os.close(current_root_fd)
    finally:
        try:
            if current_record_root_fd is not None:
                os.close(current_record_root_fd)
        finally:
            try:
                conn.close()
            finally:
                try:
                    fcntl.flock(root_lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(root_lock_fd)

    return {
        "schema_version": "storage-v2/library-verification@1",
        "ok": not issues,
        "checked_recordings": len(recordings),
        "checked_artifacts": checked_artifacts,
        "issues": issues,
    }
