from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from lecture_stt.storage_v2.repository import connect_v2, require_v2_schema


REVIEW_STATUSES = ("open", "triaged", "resolved", "dismissed")
ARTIFACT_KINDS = (
    "correction_text",
    "correction_json",
    "summary_markdown",
)
_CASE_KEY_RE = re.compile(r"^[0-9A-Za-z_-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DETAIL_CAPTURE_LIMIT = 100
_DETAIL_REVISION_LIMIT = 500
_STATUS_TRANSITIONS = {
    "open": frozenset({"open", "triaged", "dismissed"}),
    "triaged": frozenset({"open", "triaged", "resolved", "dismissed"}),
    "resolved": frozenset({"resolved", "triaged"}),
    "dismissed": frozenset({"dismissed", "open"}),
}


class ArchiveReviewError(RuntimeError):
    """Base error for archive evidence review operations."""


class ArchiveReviewNotFoundError(ArchiveReviewError):
    """Requested case, recording, or revision does not exist."""


class ArchiveReviewConflictError(ArchiveReviewError):
    """Requested mutation conflicts with current persisted state."""


class ArchiveReviewWriteDisabledError(ArchiveReviewError):
    """A write guard was not explicitly enabled."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_case_key(case_key: str) -> str:
    normalized = str(case_key or "").strip()
    if not _CASE_KEY_RE.fullmatch(normalized):
        raise ValueError("case_key must contain only ASCII letters, digits, '_' or '-'")
    return normalized


def _validate_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized not in REVIEW_STATUSES:
        raise ValueError(
            "review_status must be one of: " + ", ".join(REVIEW_STATUSES)
        )
    return normalized


def _validate_limit_offset(limit: int, offset: int) -> tuple[int, int]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 100:
        raise ValueError("limit must be between 0 and 100")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= 100_000
    ):
        raise ValueError("offset must be between 0 and 100000")
    return limit, offset


def _open_readonly(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    if not path.exists():
        raise ArchiveReviewNotFoundError("Storage v2 database is not available")
    conn = connect_v2(path, readonly=True)
    try:
        require_v2_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _case_row(conn: sqlite3.Connection, case_key: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            c.id,
            c.case_key,
            c.legacy_delivery_key,
            c.logical_stem,
            c.subject_abbr,
            c.review_status,
            c.promoted_recording_id,
            promoted.storage_key AS promoted_storage_key,
            c.created_at,
            c.updated_at,
            c.resolved_at
        FROM archive_evidence_cases AS c
        LEFT JOIN recordings AS promoted
          ON promoted.id = c.promoted_recording_id
        WHERE c.case_key = ?
        """,
        (case_key,),
    ).fetchone()
    if row is None:
        raise ArchiveReviewNotFoundError(f"Archive evidence case not found: {case_key}")
    return row


def _latest_capture_row(
    conn: sqlite3.Connection,
    case_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            id,
            capture_key,
            reconciliation_classification,
            captured_at
        FROM archive_evidence_captures
        WHERE case_id = ?
        ORDER BY captured_at DESC, id DESC
        LIMIT 1
        """,
        (case_id,),
    ).fetchone()


def _confirmed_selection_rows(
    conn: sqlite3.Connection,
    case_id: int,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                selection.artifact_kind,
                selection.revision_id,
                revision.content_sha256,
                revision.bytes,
                revision.mime_type,
                selection.promotion_plan_sha256,
                selection.confirmed_at
            FROM archive_evidence_canonical_selections AS selection
            JOIN archive_evidence_revisions AS revision
              ON revision.id = selection.revision_id
             AND revision.case_id = selection.case_id
             AND revision.artifact_kind = selection.artifact_kind
            WHERE selection.case_id = ?
            ORDER BY selection.artifact_kind
            """,
            (case_id,),
        ).fetchall()
    ]


def list_archive_review_cases(
    db_path: Path | str,
    *,
    review_status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Return a bounded metadata-only archive evidence review queue."""

    limit, offset = _validate_limit_offset(limit, offset)
    normalized_status = (
        _validate_status(review_status) if review_status is not None else None
    )
    path = Path(db_path).expanduser()
    if not path.exists():
        return {
            "schema_version": "storage-v2/archive-review-list@1",
            "available": False,
            "filters": {
                "review_status": normalized_status,
                "limit": limit,
                "offset": offset,
            },
            "counts": {status: 0 for status in REVIEW_STATUSES},
            "total": 0,
            "cases": [],
        }

    conn = _open_readonly(path)
    try:
        counts = {status: 0 for status in REVIEW_STATUSES}
        for row in conn.execute(
            """
            SELECT review_status, COUNT(*) AS count
            FROM archive_evidence_cases
            GROUP BY review_status
            """
        ).fetchall():
            counts[str(row["review_status"])] = int(row["count"])

        if normalized_status is None:
            where_sql = ""
            params: tuple[Any, ...] = (limit, offset)
            total = sum(counts.values())
        else:
            where_sql = "WHERE c.review_status = ?"
            params = (normalized_status, limit, offset)
            total = counts[normalized_status]

        rows = conn.execute(
            f"""
            SELECT
                c.case_key,
                c.logical_stem,
                c.subject_abbr,
                c.review_status,
                c.promoted_recording_id,
                promoted.storage_key AS promoted_storage_key,
                c.created_at,
                c.updated_at,
                c.resolved_at,
                COUNT(DISTINCT capture.id) AS capture_count,
                COUNT(DISTINCT revision.id) AS revision_count,
                MAX(capture.captured_at) AS last_captured_at,
                (
                    SELECT latest.reconciliation_classification
                    FROM archive_evidence_captures AS latest
                    WHERE latest.case_id = c.id
                    ORDER BY latest.captured_at DESC, latest.id DESC
                    LIMIT 1
                ) AS latest_classification
            FROM archive_evidence_cases AS c
            LEFT JOIN recordings AS promoted
              ON promoted.id = c.promoted_recording_id
            LEFT JOIN archive_evidence_captures AS capture
              ON capture.case_id = c.id
            LEFT JOIN archive_evidence_revisions AS revision
              ON revision.case_id = c.id
            {where_sql}
            GROUP BY c.id
            ORDER BY
                CASE c.review_status
                    WHEN 'open' THEN 0
                    WHEN 'triaged' THEN 1
                    WHEN 'resolved' THEN 2
                    ELSE 3
                END,
                COALESCE(MAX(capture.captured_at), c.created_at) DESC,
                c.id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    return {
        "schema_version": "storage-v2/archive-review-list@1",
        "available": True,
        "filters": {
            "review_status": normalized_status,
            "limit": limit,
            "offset": offset,
        },
        "counts": counts,
        "total": total,
        "cases": [dict(row) for row in rows],
    }


def _revision_rows(
    conn: sqlite3.Connection,
    case_id: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            revision.id AS revision_id,
            revision.artifact_kind,
            revision.content_sha256,
            revision.bytes,
            revision.mime_type,
            revision.created_at,
            COUNT(observation.id) AS observation_count,
            COUNT(DISTINCT observation.capture_id) AS capture_count,
            MAX(observation.observed_at) AS last_observed_at,
            SUM(CASE WHEN observation.relationship = 'matches_ledger' THEN 1 ELSE 0 END)
                AS matches_ledger_count,
            SUM(CASE WHEN observation.relationship = 'differs_from_ledger' THEN 1 ELSE 0 END)
                AS differs_from_ledger_count,
            SUM(CASE WHEN observation.relationship = 'unclaimed' THEN 1 ELSE 0 END)
                AS unclaimed_count,
            SUM(CASE WHEN observation.relationship = 'unexpected_current' THEN 1 ELSE 0 END)
                AS unexpected_current_count
        FROM archive_evidence_revisions AS revision
        LEFT JOIN archive_evidence_observations AS observation
          ON observation.revision_id = revision.id
         AND observation.case_id = revision.case_id
        WHERE revision.case_id = ?
        GROUP BY revision.id
        ORDER BY revision.artifact_kind, revision.created_at, revision.id
        """,
        (case_id,),
    ).fetchall()
    revisions: list[dict[str, Any]] = []
    for row in rows:
        revision = dict(row)
        roles = [
            str(role_row["source_role"])
            for role_row in conn.execute(
                """
                SELECT DISTINCT source_role
                FROM archive_evidence_observations
                WHERE case_id = ? AND revision_id = ?
                ORDER BY source_role
                """,
                (case_id, int(row["revision_id"])),
            ).fetchall()
        ]
        relationships = [
            str(relationship_row["relationship"])
            for relationship_row in conn.execute(
                """
                SELECT DISTINCT relationship
                FROM archive_evidence_observations
                WHERE case_id = ? AND revision_id = ?
                ORDER BY relationship
                """,
                (case_id, int(row["revision_id"])),
            ).fetchall()
        ]
        revision["source_roles"] = roles
        revision["relationships"] = relationships
        revisions.append(revision)
    return revisions


def _canonical_suggestions(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    revisions: Sequence[Mapping[str, Any]],
    latest_capture_id: int | None,
) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    for artifact_kind in ARTIFACT_KINDS:
        kind_revisions = [
            revision
            for revision in revisions
            if revision["artifact_kind"] == artifact_kind
        ]
        if not kind_revisions:
            continue

        ledger_matches = [
            revision
            for revision in kind_revisions
            if int(revision["matches_ledger_count"] or 0) > 0
        ]
        if len(ledger_matches) == 1:
            selected = ledger_matches[0]
            suggestions.append(
                {
                    "artifact_kind": artifact_kind,
                    "status": "suggested",
                    "revision_id": int(selected["revision_id"]),
                    "content_sha256": str(selected["content_sha256"]),
                    "reason_code": "unique_matches_ledger",
                    "candidate_revision_ids": [
                        int(revision["revision_id"]) for revision in kind_revisions
                    ],
                }
            )
            continue
        if len(ledger_matches) > 1:
            suggestions.append(
                {
                    "artifact_kind": artifact_kind,
                    "status": "unresolved",
                    "revision_id": None,
                    "content_sha256": None,
                    "reason_code": "multiple_ledger_matches",
                    "candidate_revision_ids": [
                        int(revision["revision_id"]) for revision in ledger_matches
                    ],
                }
            )
            continue

        latest_current_ids: list[int] = []
        if latest_capture_id is not None:
            latest_current_ids = [
                int(row["revision_id"])
                for row in conn.execute(
                    """
                    SELECT DISTINCT observation.revision_id
                    FROM archive_evidence_observations AS observation
                    JOIN archive_evidence_revisions AS revision
                      ON revision.id = observation.revision_id
                     AND revision.case_id = observation.case_id
                    WHERE observation.case_id = ?
                      AND observation.capture_id = ?
                      AND observation.source_role IN ('current_gh', 'current_obsidian')
                      AND revision.artifact_kind = ?
                    ORDER BY observation.revision_id
                    """,
                    (case_id, latest_capture_id, artifact_kind),
                ).fetchall()
            ]
        if len(latest_current_ids) == 1:
            selected_id = latest_current_ids[0]
            selected = next(
                revision
                for revision in kind_revisions
                if int(revision["revision_id"]) == selected_id
            )
            suggestions.append(
                {
                    "artifact_kind": artifact_kind,
                    "status": "suggested",
                    "revision_id": selected_id,
                    "content_sha256": str(selected["content_sha256"]),
                    "reason_code": "unique_latest_capture_current",
                    "candidate_revision_ids": [
                        int(revision["revision_id"]) for revision in kind_revisions
                    ],
                }
            )
        else:
            suggestions.append(
                {
                    "artifact_kind": artifact_kind,
                    "status": "unresolved",
                    "revision_id": None,
                    "content_sha256": None,
                    "reason_code": (
                        "multiple_latest_capture_current"
                        if len(latest_current_ids) > 1
                        else "no_unique_current_candidate"
                    ),
                    "candidate_revision_ids": (
                        latest_current_ids
                        if latest_current_ids
                        else [
                            int(revision["revision_id"])
                            for revision in kind_revisions
                        ]
                    ),
                }
            )
    return suggestions


def _detail_from_connection(
    conn: sqlite3.Connection,
    case_key: str,
) -> dict[str, Any]:
    normalized_case_key = _validate_case_key(case_key)
    case = _case_row(conn, normalized_case_key)
    case_id = int(case["id"])
    latest_capture = _latest_capture_row(conn, case_id)
    capture_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM archive_evidence_captures WHERE case_id = ?",
            (case_id,),
        ).fetchone()[0]
    )
    revision_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM archive_evidence_revisions WHERE case_id = ?",
            (case_id,),
        ).fetchone()[0]
    )
    if revision_count > _DETAIL_REVISION_LIMIT:
        raise ArchiveReviewConflictError(
            "Archive evidence case exceeds the bounded revision detail limit"
        )
    revisions = _revision_rows(conn, case_id)
    captures = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                capture_key,
                reconciliation_classification,
                captured_at
            FROM archive_evidence_captures
            WHERE case_id = ?
            ORDER BY captured_at DESC, id DESC
            LIMIT ?
            """,
            (case_id, _DETAIL_CAPTURE_LIMIT),
        ).fetchall()
    ]
    suggestions = _canonical_suggestions(
        conn,
        case_id=case_id,
        revisions=revisions,
        latest_capture_id=(
            int(latest_capture["id"]) if latest_capture is not None else None
        ),
    )
    case_payload = dict(case)
    case_payload.pop("id", None)
    return {
        "schema_version": "storage-v2/archive-review-detail@1",
        "case": case_payload,
        "latest_capture": (
            {
                "capture_key": str(latest_capture["capture_key"]),
                "reconciliation_classification": str(
                    latest_capture["reconciliation_classification"]
                ),
                "captured_at": str(latest_capture["captured_at"]),
            }
            if latest_capture is not None
            else None
        ),
        "capture_count": capture_count,
        "captures_truncated": capture_count > _DETAIL_CAPTURE_LIMIT,
        "revision_count": revision_count,
        "captures": captures,
        "revisions": revisions,
        "canonical_suggestions": suggestions,
        "confirmed_selections": _confirmed_selection_rows(conn, case_id),
    }


def read_archive_review_case(
    db_path: Path | str,
    case_key: str,
) -> dict[str, Any]:
    """Return one metadata-only case with revision comparison information."""

    conn = _open_readonly(db_path)
    try:
        return _detail_from_connection(conn, case_key)
    finally:
        conn.close()


def update_archive_review_status(
    db_path: Path | str,
    case_key: str,
    review_status: str,
    *,
    allow_write: bool = False,
) -> dict[str, Any]:
    """Apply an explicit review status transition without touching evidence files."""

    if not allow_write:
        raise ArchiveReviewWriteDisabledError(
            "Archive review status writes are disabled without allow_write=True"
        )
    normalized_case_key = _validate_case_key(case_key)
    normalized_status = _validate_status(review_status)
    path = Path(db_path).expanduser()
    if not path.exists():
        raise ArchiveReviewNotFoundError("Storage v2 database is not available")

    conn = connect_v2(path)
    try:
        require_v2_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        case = _case_row(conn, normalized_case_key)
        current_status = str(case["review_status"])
        if case["promoted_recording_id"] is not None and normalized_status != "resolved":
            raise ArchiveReviewConflictError(
                "A promoted archive evidence case cannot leave resolved status"
            )
        if normalized_status not in _STATUS_TRANSITIONS[current_status]:
            raise ArchiveReviewConflictError(
                f"Unsupported archive review status transition: "
                f"{current_status} -> {normalized_status}"
            )
        changed = current_status != normalized_status
        if changed:
            resolved_at_sql = (
                "CURRENT_TIMESTAMP"
                if normalized_status in {"resolved", "dismissed"}
                else "NULL"
            )
            conn.execute(
                f"""
                UPDATE archive_evidence_cases
                SET review_status = ?,
                    updated_at = CURRENT_TIMESTAMP,
                    resolved_at = {resolved_at_sql}
                WHERE id = ?
                """,
                (normalized_status, int(case["id"])),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "schema_version": "storage-v2/archive-review-status@1",
        "ok": True,
        "case_key": normalized_case_key,
        "review_status": normalized_status,
        "changed": changed,
    }


def _normalize_selection_mapping(
    selected_revisions: Mapping[str, int],
) -> dict[str, int]:
    if not isinstance(selected_revisions, Mapping) or not selected_revisions:
        raise ValueError("selected_revisions must be a non-empty object")
    normalized: dict[str, int] = {}
    for raw_kind, raw_revision_id in selected_revisions.items():
        artifact_kind = str(raw_kind or "").strip()
        if artifact_kind not in ARTIFACT_KINDS:
            raise ValueError(f"Unsupported artifact kind: {artifact_kind}")
        if (
            isinstance(raw_revision_id, bool)
            or not isinstance(raw_revision_id, int)
            or raw_revision_id <= 0
        ):
            raise ValueError(
                f"Revision id for {artifact_kind} must be a positive integer"
            )
        normalized[artifact_kind] = raw_revision_id
    return dict(sorted(normalized.items()))


def _require_promotion_database_integrity(
    conn: sqlite3.Connection,
) -> None:
    quick_check = [
        str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()
    ]
    if quick_check != ["ok"]:
        raise ArchiveReviewConflictError(
            "Storage v2 database failed quick_check"
        )
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ArchiveReviewConflictError(
            "Storage v2 database has foreign key violations"
        )


def _promotion_plan_from_connection(
    conn: sqlite3.Connection,
    *,
    case_key: str,
    target_storage_key: str,
    selected_revisions: Mapping[str, int],
) -> dict[str, Any]:
    _require_promotion_database_integrity(conn)
    normalized_case_key = _validate_case_key(case_key)
    normalized_storage_key = str(target_storage_key or "").strip()
    if not _CASE_KEY_RE.fullmatch(normalized_storage_key):
        raise ValueError(
            "target_storage_key must contain only ASCII letters, digits, '_' or '-'"
        )
    selections = _normalize_selection_mapping(selected_revisions)
    case = _case_row(conn, normalized_case_key)
    if case["promoted_recording_id"] is not None:
        raise ArchiveReviewConflictError(
            f"Archive evidence case is already promoted: {normalized_case_key}"
        )
    if str(case["review_status"]) in {"resolved", "dismissed"}:
        raise ArchiveReviewConflictError(
            "Resolved or dismissed archive evidence cases cannot be promoted"
        )

    latest_capture = _latest_capture_row(conn, int(case["id"]))
    if latest_capture is None:
        raise ArchiveReviewConflictError(
            "Archive evidence case has no capture to promote"
        )
    latest_classification = str(
        latest_capture["reconciliation_classification"]
    )
    if latest_classification == "blocked":
        raise ArchiveReviewConflictError(
            "Blocked archive evidence cases cannot be canonically promoted"
        )

    recording = conn.execute(
        """
        SELECT id, storage_key
        FROM recordings
        WHERE storage_key = ? AND archived_at IS NULL
        """,
        (normalized_storage_key,),
    ).fetchone()
    if recording is None:
        raise ArchiveReviewNotFoundError(
            f"Target recording not found: {normalized_storage_key}"
        )

    available_kinds = {
        str(row["artifact_kind"])
        for row in conn.execute(
            """
            SELECT DISTINCT artifact_kind
            FROM archive_evidence_revisions
            WHERE case_id = ?
            """,
            (int(case["id"]),),
        ).fetchall()
    }
    if set(selections) != available_kinds:
        missing = sorted(available_kinds.difference(selections))
        unexpected = sorted(set(selections).difference(available_kinds))
        details: list[str] = []
        if missing:
            details.append("missing kinds: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected kinds: " + ", ".join(unexpected))
        raise ArchiveReviewConflictError(
            "Canonical promotion must select exactly one revision for every "
            "available artifact kind"
            + (f" ({'; '.join(details)})" if details else "")
        )

    selected_metadata: list[dict[str, Any]] = []
    for artifact_kind, revision_id in selections.items():
        revision = conn.execute(
            """
            SELECT
                revision.id,
                revision.artifact_kind,
                revision.content_sha256,
                revision.bytes,
                revision.mime_type,
                COUNT(observation.id) AS observation_count
            FROM archive_evidence_revisions AS revision
            LEFT JOIN archive_evidence_observations AS observation
              ON observation.revision_id = revision.id
             AND observation.case_id = revision.case_id
            WHERE revision.id = ?
              AND revision.case_id = ?
              AND revision.artifact_kind = ?
            GROUP BY revision.id
            """,
            (revision_id, int(case["id"]), artifact_kind),
        ).fetchone()
        if revision is None:
            raise ArchiveReviewConflictError(
                f"Revision {revision_id} does not belong to case "
                f"{normalized_case_key} as {artifact_kind}"
            )
        if int(revision["observation_count"]) <= 0:
            raise ArchiveReviewConflictError(
                f"Revision {revision_id} has no provenance observation"
            )
        selected_metadata.append(
            {
                "artifact_kind": artifact_kind,
                "revision_id": int(revision["id"]),
                "content_sha256": str(revision["content_sha256"]),
                "bytes": int(revision["bytes"]),
                "mime_type": revision["mime_type"],
            }
        )

    digest_payload = {
        "schema_version": "storage-v2/archive-review-promotion-plan@1",
        "case_key": normalized_case_key,
        "case_updated_at": str(case["updated_at"]),
        "review_status": str(case["review_status"]),
        "target_recording_id": int(recording["id"]),
        "target_storage_key": str(recording["storage_key"]),
        "latest_capture_key": str(latest_capture["capture_key"]),
        "latest_capture_classification": latest_classification,
        "selected_revisions": selected_metadata,
        "expected_count": 1,
    }
    return {
        **digest_payload,
        "mode": "read_only",
        "plan_sha256": _sha256_json(digest_payload),
    }


def plan_archive_review_promotion(
    db_path: Path | str,
    case_key: str,
    *,
    target_storage_key: str,
    selected_revisions: Mapping[str, int],
) -> dict[str, Any]:
    """Plan a metadata-only canonical linkage to an existing recording."""

    conn = _open_readonly(db_path)
    try:
        return _promotion_plan_from_connection(
            conn,
            case_key=case_key,
            target_storage_key=target_storage_key,
            selected_revisions=selected_revisions,
        )
    finally:
        conn.close()


def apply_archive_review_promotion(
    db_path: Path | str,
    case_key: str,
    *,
    target_storage_key: str,
    selected_revisions: Mapping[str, int],
    expected_count: int,
    expected_plan_sha256: str,
    promotions_enabled: bool = False,
    allow_write: bool = False,
) -> dict[str, Any]:
    """Persist a guarded metadata-only canonical selection and recording link."""

    if not promotions_enabled:
        raise ArchiveReviewWriteDisabledError(
            "Archive review canonical promotion is disabled by configuration"
        )
    if not allow_write:
        raise ArchiveReviewWriteDisabledError(
            "Archive review canonical promotion requires allow_write=True"
        )
    if isinstance(expected_count, bool) or expected_count != 1:
        raise ArchiveReviewConflictError(
            "Archive review promotion expected_count must equal 1"
        )
    normalized_digest = str(expected_plan_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized_digest):
        raise ValueError("expected_plan_sha256 must be a canonical SHA-256")

    normalized_case_key = _validate_case_key(case_key)
    normalized_storage_key = str(target_storage_key or "").strip()
    selections = _normalize_selection_mapping(selected_revisions)
    path = Path(db_path).expanduser()
    if not path.exists():
        raise ArchiveReviewNotFoundError("Storage v2 database is not available")

    conn = connect_v2(path)
    try:
        require_v2_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        case = _case_row(conn, normalized_case_key)
        if case["promoted_recording_id"] is not None:
            persisted = _confirmed_selection_rows(conn, int(case["id"]))
            persisted_mapping = {
                str(row["artifact_kind"]): int(row["revision_id"])
                for row in persisted
            }
            persisted_digests = {
                str(row["promotion_plan_sha256"]) for row in persisted
            }
            if (
                str(case["promoted_storage_key"]) == normalized_storage_key
                and persisted_mapping == selections
                and persisted_digests == {normalized_digest}
            ):
                conn.rollback()
                return {
                    "schema_version": "storage-v2/archive-review-promotion-result@1",
                    "ok": True,
                    "status": "skipped",
                    "case_key": normalized_case_key,
                    "target_storage_key": normalized_storage_key,
                    "plan_sha256": normalized_digest,
                    "selected_count": len(persisted),
                }
            raise ArchiveReviewConflictError(
                "Archive evidence case was already promoted with different metadata"
            )

        plan = _promotion_plan_from_connection(
            conn,
            case_key=normalized_case_key,
            target_storage_key=normalized_storage_key,
            selected_revisions=selections,
        )
        if plan["expected_count"] != expected_count:
            raise ArchiveReviewConflictError(
                "Archive review promotion count changed; create a new plan"
            )
        if plan["plan_sha256"] != normalized_digest:
            raise ArchiveReviewConflictError(
                "Archive review promotion plan changed; create a new plan"
            )

        case_id = int(case["id"])
        for selected in plan["selected_revisions"]:
            conn.execute(
                """
                INSERT INTO archive_evidence_canonical_selections(
                    case_id,
                    revision_id,
                    artifact_kind,
                    promotion_plan_sha256
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    case_id,
                    int(selected["revision_id"]),
                    str(selected["artifact_kind"]),
                    normalized_digest,
                ),
            )
        updated = conn.execute(
            """
            UPDATE archive_evidence_cases
            SET promoted_recording_id = ?,
                review_status = 'resolved',
                updated_at = CURRENT_TIMESTAMP,
                resolved_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND promoted_recording_id IS NULL
              AND review_status IN ('open', 'triaged')
            """,
            (int(plan["target_recording_id"]), case_id),
        )
        if updated.rowcount != 1:
            raise ArchiveReviewConflictError(
                "Archive evidence case changed during promotion"
            )
        foreign_key_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_issues:
            raise ArchiveReviewConflictError(
                "Archive review promotion would violate foreign keys"
            )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "schema_version": "storage-v2/archive-review-promotion-result@1",
        "ok": True,
        "status": "promoted",
        "case_key": normalized_case_key,
        "target_storage_key": normalized_storage_key,
        "plan_sha256": normalized_digest,
        "selected_count": len(selections),
    }
