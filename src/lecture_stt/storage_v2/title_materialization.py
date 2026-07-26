from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime
import hmac
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Mapping

from lecture_stt.storage_v2.classification_materialization import (
    ClassificationMaterializationConflictError,
    ClassificationMaterializationNotFoundError,
    _canonical_json,
    _digest_bytes,
    _locked_records_root as _classification_locked_records_root,
    _manifest_bytes,
    _read_record_manifest as _classification_read_record_manifest,
    _replace_record_manifest as _classification_replace_record_manifest,
    _require_database_integrity,
    _verification_issue_codes,
)
from lecture_stt.storage_v2.manifest import (
    validate_relative_path,
    validate_storage_key,
)
from lecture_stt.storage_v2.repository import connect_v2, require_v2_schema
from lecture_stt.storage_v2.title_suggestions import (
    DEFAULT_MAX_TRANSCRIPT_BYTES,
    GENERATOR_VERSION,
    TitleSuggestionConflictError,
    TitleSuggestionNotFoundError,
    _assert_database_outside_records_root,
    _validate_max_transcript_bytes,
    _validated_confirmed_materialization_row,
)
from lecture_stt.storage_v2.verifier import verify_library


PLAN_SCHEMA_VERSION = "storage-v2/title-materialization-plan@1"
RESULT_SCHEMA_VERSION = "storage-v2/title-materialization-result@1"
MATERIALIZATION_PLAN_JSON_MAX_BYTES = 64 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TITLE_SOURCES = {
    "manual",
    "schedule",
    "filename_inference",
    "legacy_import",
    "system",
}
_PLAN_FIELDS = {
    "schema_version",
    "proposal_id",
    "recording_id",
    "storage_key",
    "proposal_updated_at",
    "confirmation_plan_sha256",
    "confirmed_at",
    "suggestion_reason",
    "generator_version",
    "max_transcript_bytes",
    "previous",
    "target",
    "manifest",
    "source_evidence",
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
_SOURCE_EVIDENCE_FIELDS = {
    "transcript_artifact_id",
    "transcript_job_id",
    "transcript_revision",
    "transcript_path_rel",
    "transcript_sha256",
    "transcript_bytes",
    "review_item_id",
    "review_job_id",
    "review_artifact_id",
    "review_status",
    "review_resolved_at",
    "review_severity",
    "review_reason_code",
    "review_detail_json",
    "proposal_detail_json",
    "classification_proposal_id",
}


class TitleMaterializationError(RuntimeError):
    """Base error for canonical content-title materialization."""


class TitleMaterializationNotFoundError(TitleMaterializationError):
    """The requested database, proposal, record, or manifest is unavailable."""


class TitleMaterializationConflictError(TitleMaterializationError):
    """Current DB, inference, or filesystem evidence differs from the plan."""


class TitleMaterializationWriteDisabledError(TitleMaterializationError):
    """A canonical title write is missing an explicit capability guard."""


class TitleMaterializationRecoveryRequiredError(TitleMaterializationError):
    """A prepared or applied write exists and requires guarded replay."""


class TitleMaterializationPostCommitVerificationError(
    TitleMaterializationRecoveryRequiredError
):
    """Canonical writes committed, but final verification did not pass."""


def _confirmed_materialization_row(
    conn: sqlite3.Connection,
    root_fd: int,
    proposal_id: int,
    *,
    max_transcript_bytes: int,
    current_title_override: Mapping[str, Any] | None = None,
) -> sqlite3.Row:
    try:
        return _validated_confirmed_materialization_row(
            conn,
            root_fd,
            proposal_id,
            max_transcript_bytes=max_transcript_bytes,
            current_title_override=current_title_override,
        )
    except TitleSuggestionNotFoundError as exc:
        raise TitleMaterializationNotFoundError(str(exc)) from exc
    except TitleSuggestionConflictError as exc:
        raise TitleMaterializationConflictError(str(exc)) from exc


def _sha256_json(value: Any) -> str:
    import hashlib

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TitleMaterializationConflictError(
            f"{field} must be a positive integer"
        )
    return value


def _digest(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise TitleMaterializationConflictError(
            f"{field} must be a canonical SHA-256"
        )
    return value


def _timestamp(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise TitleMaterializationConflictError(f"{field} is invalid")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TitleMaterializationConflictError(
            f"{field} is invalid"
        ) from exc
    return value


def _confidence(value: Any, *, field: str) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise TitleMaterializationConflictError(
            f"{field} must be null or a finite number between 0 and 1"
        )
    return float(value)


def _validate_title(
    value: Any,
    *,
    field: str,
    require_id: bool,
) -> dict[str, Any]:
    expected = _TITLE_FIELDS if require_id else _TITLE_FIELDS - {"id"}
    if not isinstance(value, dict) or set(value) != expected:
        raise TitleMaterializationConflictError(
            f"{field} has an invalid schema"
        )
    if require_id:
        _positive_int(value.get("id"), field=f"{field}.id")
    if (
        not isinstance(value.get("value"), str)
        or not value["value"]
        or len(value["value"]) > 512
    ):
        raise TitleMaterializationConflictError(
            f"{field}.value is invalid"
        )
    if value.get("source") not in _TITLE_SOURCES:
        raise TitleMaterializationConflictError(
            f"{field}.source is invalid"
        )
    locale = value.get("locale")
    if locale is not None and (
        not isinstance(locale, str) or len(locale) > 64
    ):
        raise TitleMaterializationConflictError(
            f"{field}.locale is invalid"
        )
    _confidence(value.get("confidence"), field=f"{field}.confidence")
    return value


def _validate_source_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _SOURCE_EVIDENCE_FIELDS:
        raise TitleMaterializationConflictError(
            "Stored title materialization source evidence is invalid"
        )
    for field in (
        "transcript_artifact_id",
        "transcript_job_id",
        "transcript_revision",
        "review_item_id",
        "review_job_id",
        "review_artifact_id",
    ):
        _positive_int(value.get(field), field=f"source_evidence.{field}")
    classification_id = value.get("classification_proposal_id")
    if classification_id is not None:
        _positive_int(
            classification_id,
            field="source_evidence.classification_proposal_id",
        )
    if not isinstance(value.get("transcript_path_rel"), str):
        raise TitleMaterializationConflictError(
            "Stored transcript path evidence is invalid"
        )
    try:
        validate_relative_path(
            value["transcript_path_rel"],
            field="source_evidence.transcript_path_rel",
        )
    except (TypeError, ValueError) as exc:
        raise TitleMaterializationConflictError(
            "Stored transcript path evidence is invalid"
        ) from exc
    _digest(
        value.get("transcript_sha256"),
        field="source_evidence.transcript_sha256",
    )
    transcript_bytes = value.get("transcript_bytes")
    if (
        isinstance(transcript_bytes, bool)
        or not isinstance(transcript_bytes, int)
        or transcript_bytes < 0
    ):
        raise TitleMaterializationConflictError(
            "source_evidence.transcript_bytes is invalid"
        )
    if (
        value.get("review_status") != "resolved"
        or _timestamp(
            value.get("review_resolved_at"),
            field="source_evidence.review_resolved_at",
        )
        != value.get("review_resolved_at")
        or value.get("review_severity") != "medium"
        or not isinstance(value.get("review_reason_code"), str)
        or not value["review_reason_code"].startswith("title_suggestion_")
        or not isinstance(value.get("review_detail_json"), str)
        or not isinstance(value.get("proposal_detail_json"), str)
        or value["review_detail_json"] != value["proposal_detail_json"]
    ):
        raise TitleMaterializationConflictError(
            "Stored linked review evidence is invalid"
        )
    return value


def validate_title_materialization_plan_payload(
    payload: Any,
) -> dict[str, Any]:
    """Validate the closed metadata-only title journal plan."""

    if not isinstance(payload, dict) or set(payload) != _PLAN_FIELDS:
        raise TitleMaterializationConflictError(
            "Stored title materialization plan has an invalid schema"
        )
    if payload.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise TitleMaterializationConflictError(
            "Stored title materialization plan schema is unsupported"
        )
    _positive_int(payload.get("proposal_id"), field="proposal_id")
    _positive_int(payload.get("recording_id"), field="recording_id")
    if not isinstance(payload.get("storage_key"), str):
        raise TitleMaterializationConflictError("storage_key is invalid")
    try:
        validate_storage_key(payload["storage_key"])
    except ValueError as exc:
        raise TitleMaterializationConflictError(
            "storage_key is invalid"
        ) from exc
    _timestamp(payload.get("proposal_updated_at"), field="proposal_updated_at")
    _timestamp(payload.get("confirmed_at"), field="confirmed_at")
    _digest(
        payload.get("confirmation_plan_sha256"),
        field="confirmation_plan_sha256",
    )
    if payload.get("suggestion_reason") not in {
        "schedule_content_match",
        "content_topic",
    }:
        raise TitleMaterializationConflictError(
            "suggestion_reason is invalid"
        )
    if payload.get("generator_version") != GENERATOR_VERSION:
        raise TitleMaterializationConflictError(
            "generator_version is invalid"
        )
    _validate_max_transcript_bytes(payload.get("max_transcript_bytes"))
    if (
        payload.get("expected_count") != 1
        or payload.get("materialization")
        != "canonical_content_title_manifest"
    ):
        raise TitleMaterializationConflictError(
            "Stored title materialization mode is invalid"
        )
    previous = payload.get("previous")
    target = payload.get("target")
    manifest = payload.get("manifest")
    if (
        not isinstance(previous, dict)
        or set(previous) != {"title"}
        or not isinstance(target, dict)
        or set(target) != {"title"}
        or not isinstance(manifest, dict)
        or set(manifest)
        != {"relpath", "previous_sha256", "materialized_sha256"}
    ):
        raise TitleMaterializationConflictError(
            "Stored title materialization metadata shape is invalid"
        )
    _validate_title(previous["title"], field="previous.title", require_id=True)
    target_title = _validate_title(
        target["title"],
        field="target.title",
        require_id=False,
    )
    if target_title["source"] != "system":
        raise TitleMaterializationConflictError(
            "Canonical content title source must be system"
        )
    try:
        validate_relative_path(
            manifest.get("relpath"),
            field="manifest.relpath",
        )
    except (TypeError, ValueError) as exc:
        raise TitleMaterializationConflictError(
            "Stored manifest path is invalid"
        ) from exc
    _digest(manifest.get("previous_sha256"), field="manifest.previous_sha256")
    _digest(
        manifest.get("materialized_sha256"),
        field="manifest.materialized_sha256",
    )
    _validate_source_evidence(payload.get("source_evidence"))
    return payload


def title_materialization_plan_sha256(payload: Any) -> str:
    return _sha256_json(validate_title_materialization_plan_payload(payload))


def _translate_support_error(exc: BaseException) -> TitleMaterializationError:
    if isinstance(exc, ClassificationMaterializationNotFoundError):
        return TitleMaterializationNotFoundError(str(exc))
    if isinstance(exc, ClassificationMaterializationConflictError):
        return TitleMaterializationConflictError(str(exc))
    raise exc


@contextmanager
def _locked_records_root(
    records_root: Path | str,
) -> Iterator[tuple[Path, int]]:
    try:
        with _classification_locked_records_root(records_root) as locked:
            yield locked
    except (
        ClassificationMaterializationNotFoundError,
        ClassificationMaterializationConflictError,
    ) as exc:
        raise _translate_support_error(exc) from exc
    except OSError as exc:
        raise TitleMaterializationConflictError(
            "Storage v2 records root is not safely accessible"
        ) from exc


def _read_record_manifest(
    root_fd: int,
    *,
    storage_key: str,
    manifest_relpath: str,
) -> tuple[dict[str, Any], bytes, str, tuple[int, ...]]:
    try:
        return _classification_read_record_manifest(
            root_fd,
            storage_key=storage_key,
            manifest_relpath=manifest_relpath,
        )
    except (
        ClassificationMaterializationNotFoundError,
        ClassificationMaterializationConflictError,
    ) as exc:
        raise _translate_support_error(exc) from exc
    except (OSError, ValueError) as exc:
        raise TitleMaterializationConflictError(
            "Recording manifest is not safely readable"
        ) from exc


def _replace_record_manifest(
    root_fd: int,
    *,
    storage_key: str,
    manifest_relpath: str,
    expected_sha256: str,
    payload: Mapping[str, Any],
) -> str:
    try:
        return _classification_replace_record_manifest(
            root_fd,
            storage_key=storage_key,
            manifest_relpath=manifest_relpath,
            expected_sha256=expected_sha256,
            payload=payload,
        )
    except (
        ClassificationMaterializationNotFoundError,
        ClassificationMaterializationConflictError,
    ) as exc:
        raise _translate_support_error(exc) from exc
    except (OSError, ValueError) as exc:
        raise TitleMaterializationConflictError(
            "Recording manifest could not be replaced safely"
        ) from exc


def _title_payload(
    row: Mapping[str, Any],
    *,
    include_id: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "value": str(row["title"]),
        "source": str(row["title_source"]),
        "locale": None if row["locale"] is None else str(row["locale"]),
        "confidence": (
            None if row["confidence"] is None else float(row["confidence"])
        ),
    }
    if include_id:
        payload = {"id": int(row["id"]), **payload}
    return payload


def _manifest_title(title: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "value": str(title["value"]),
        "source": str(title["source"]),
    }


def _require_manifest_title(
    manifest: Mapping[str, Any],
    title: Mapping[str, Any],
) -> None:
    if manifest.get("title") != _manifest_title(title):
        raise TitleMaterializationConflictError(
            "Recording manifest title does not match its guarded DB revision"
        )


def _exact_current_title(
    conn: sqlite3.Connection,
    recording_id: int,
) -> sqlite3.Row:
    rows = conn.execute(
        """
        SELECT *
        FROM recording_titles
        WHERE recording_id = ? AND is_current = 1
        ORDER BY id
        LIMIT 2
        """,
        (recording_id,),
    ).fetchall()
    if len(rows) != 1:
        raise TitleMaterializationConflictError(
            "Recording must have exactly one current title"
        )
    return rows[0]


def _row_by_title_id(
    conn: sqlite3.Connection,
    *,
    title_id: int,
    recording_id: int,
    label: str,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT *
        FROM recording_titles
        WHERE id = ? AND recording_id = ?
        """,
        (title_id, recording_id),
    ).fetchone()
    if row is None:
        raise TitleMaterializationConflictError(
            f"Title materialization {label} revision is missing"
        )
    return row


def _journal_row(
    conn: sqlite3.Connection,
    proposal_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM recording_title_materializations
        WHERE proposal_id = ?
        """,
        (proposal_id,),
    ).fetchone()


def _journal_for_recording(
    conn: sqlite3.Connection,
    recording_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM recording_title_materializations
        WHERE recording_id = ?
        ORDER BY id
        LIMIT 2
        """,
        (recording_id,),
    ).fetchone()


def _reject_mixed_materialization_history(
    conn: sqlite3.Connection,
    recording_id: int,
) -> None:
    if conn.execute(
        """
        SELECT 1
        FROM recording_classification_materializations
        WHERE recording_id = ?
        LIMIT 1
        """,
        (recording_id,),
    ).fetchone() is not None:
        raise TitleMaterializationConflictError(
            "Content-title and timetable materialization histories cannot be "
            "mixed in this CLI-first revision"
        )


def _source_evidence(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "transcript_artifact_id": int(row["transcript_artifact_id"]),
        "transcript_job_id": int(row["transcript_job_id"]),
        "transcript_revision": int(row["transcript_revision"]),
        "transcript_path_rel": str(row["transcript_path_rel"]),
        "transcript_sha256": str(row["transcript_sha256"]),
        "transcript_bytes": int(row["transcript_bytes"]),
        "review_item_id": int(row["review_item_id"]),
        "review_job_id": int(row["review_job_id"]),
        "review_artifact_id": int(row["review_artifact_id"]),
        "review_status": str(row["review_status"]),
        "review_resolved_at": str(row["review_resolved_at"]),
        "review_severity": str(row["review_severity"]),
        "review_reason_code": str(row["review_reason_code"]),
        "review_detail_json": str(row["review_detail_json"]),
        "proposal_detail_json": str(row["detail_json"]),
        "classification_proposal_id": (
            None
            if row["classification_proposal_id"] is None
            else int(row["classification_proposal_id"])
        ),
    }


def _target_title(
    proposal: sqlite3.Row,
    previous_title: sqlite3.Row,
) -> dict[str, Any]:
    return {
        "value": str(proposal["proposed_title"]),
        "source": "system",
        "locale": (
            None
            if previous_title["locale"] is None
            else str(previous_title["locale"])
        ),
        "confidence": float(proposal["confidence"]),
    }


def _public_plan(
    payload: Mapping[str, Any],
    *,
    state: str,
) -> dict[str, Any]:
    validated = validate_title_materialization_plan_payload(dict(payload))
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "proposal_id": int(validated["proposal_id"]),
        "storage_key": str(validated["storage_key"]),
        "suggestion_reason": str(validated["suggestion_reason"]),
        "previous_title": {
            "value": str(validated["previous"]["title"]["value"]),
            "source": str(validated["previous"]["title"]["source"]),
        },
        "target_title": {
            "value": str(validated["target"]["title"]["value"]),
            "source": str(validated["target"]["title"]["source"]),
            "confidence": validated["target"]["title"]["confidence"],
        },
        "max_transcript_bytes": int(validated["max_transcript_bytes"]),
        "expected_count": 1,
        "materialization": "canonical_content_title_manifest",
        "mode": "read_only",
        "plan_sha256": _sha256_json(validated),
        "materialization_state": state,
    }


def _new_plan_from_connection(
    conn: sqlite3.Connection,
    root_fd: int,
    proposal_id: int,
    *,
    max_transcript_bytes: int,
) -> dict[str, Any]:
    _require_database_integrity(conn)
    proposal = _confirmed_materialization_row(
        conn,
        root_fd,
        proposal_id,
        max_transcript_bytes=max_transcript_bytes,
    )
    recording_id = int(proposal["recording_id"])
    _reject_mixed_materialization_history(conn, recording_id)
    existing = _journal_row(conn, proposal_id)
    if existing is not None:
        return _stored_plan_from_connection(
            conn,
            root_fd,
            proposal_id,
            journal=existing,
        )
    other = _journal_for_recording(conn, recording_id)
    if other is not None:
        raise TitleMaterializationConflictError(
            "Recording already has another content-title materialization"
        )
    previous_title_row = _exact_current_title(conn, recording_id)
    previous_title = _title_payload(previous_title_row, include_id=True)
    storage_key = str(proposal["storage_key"])
    manifest_relpath = str(proposal["manifest_relpath"])
    manifest, _raw, previous_manifest_sha256, _snapshot = (
        _read_record_manifest(
            root_fd,
            storage_key=storage_key,
            manifest_relpath=manifest_relpath,
        )
    )
    _require_manifest_title(manifest, previous_title)
    target_title = _target_title(proposal, previous_title_row)
    target_manifest = copy.deepcopy(manifest)
    target_manifest["title"] = _manifest_title(target_title)
    materialized_manifest_sha256 = _digest_bytes(
        _manifest_bytes(target_manifest)
    )
    payload = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "proposal_id": int(proposal["id"]),
        "recording_id": recording_id,
        "storage_key": storage_key,
        "proposal_updated_at": str(proposal["updated_at"]),
        "confirmation_plan_sha256": str(
            proposal["confirmation_plan_sha256"]
        ),
        "confirmed_at": str(proposal["confirmed_at"]),
        "suggestion_reason": str(proposal["suggestion_reason"]),
        "generator_version": str(proposal["generator_version"]),
        "max_transcript_bytes": max_transcript_bytes,
        "previous": {"title": previous_title},
        "target": {"title": target_title},
        "manifest": {
            "relpath": manifest_relpath,
            "previous_sha256": previous_manifest_sha256,
            "materialized_sha256": materialized_manifest_sha256,
        },
        "source_evidence": _source_evidence(proposal),
        "expected_count": 1,
        "materialization": "canonical_content_title_manifest",
    }
    return _public_plan(payload, state="not_prepared")


def _load_stored_payload(journal: sqlite3.Row) -> dict[str, Any]:
    raw = str(journal["plan_json"])
    if len(raw.encode("utf-8")) > MATERIALIZATION_PLAN_JSON_MAX_BYTES:
        raise TitleMaterializationConflictError(
            "Stored title materialization plan exceeds its metadata limit"
        )
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise TitleMaterializationConflictError(
            "Stored title materialization plan is invalid JSON"
        ) from exc
    return validate_title_materialization_plan_payload(decoded)


def _stored_plan_from_connection(
    conn: sqlite3.Connection,
    root_fd: int,
    proposal_id: int,
    *,
    journal: sqlite3.Row | None = None,
) -> dict[str, Any]:
    journal_row = journal or _journal_row(conn, proposal_id)
    if journal_row is None:
        raise TitleMaterializationNotFoundError(
            f"Title materialization journal not found for proposal {proposal_id}"
        )
    plan = _load_stored_payload(journal_row)
    plan_sha256 = _sha256_json(plan)
    if (
        plan_sha256 != str(journal_row["materialization_plan_sha256"])
        or int(journal_row["proposal_id"]) != int(plan["proposal_id"])
        or int(journal_row["recording_id"]) != int(plan["recording_id"])
        or int(journal_row["previous_title_id"])
        != int(plan["previous"]["title"]["id"])
        or str(journal_row["confirmation_plan_sha256"])
        != str(plan["confirmation_plan_sha256"])
        or str(journal_row["previous_manifest_sha256"])
        != str(plan["manifest"]["previous_sha256"])
        or str(journal_row["materialized_manifest_sha256"])
        != str(plan["manifest"]["materialized_sha256"])
    ):
        raise TitleMaterializationConflictError(
            "Title materialization journal does not match its stored plan"
        )
    recording_id = int(plan["recording_id"])
    _reject_mixed_materialization_history(conn, recording_id)
    siblings = conn.execute(
        """
        SELECT id
        FROM recording_title_materializations
        WHERE recording_id = ?
        ORDER BY id
        LIMIT 2
        """,
        (recording_id,),
    ).fetchall()
    if len(siblings) != 1 or int(siblings[0]["id"]) != int(journal_row["id"]):
        raise TitleMaterializationConflictError(
            "Recording has an unsupported title materialization chain"
        )
    previous_title_row = _row_by_title_id(
        conn,
        title_id=int(journal_row["previous_title_id"]),
        recording_id=recording_id,
        label="previous",
    )
    target_title_row = _row_by_title_id(
        conn,
        title_id=int(journal_row["materialized_title_id"]),
        recording_id=recording_id,
        label="target",
    )
    if _title_payload(previous_title_row, include_id=True) != plan["previous"]["title"]:
        raise TitleMaterializationConflictError(
            "Previous title revision no longer matches its journal"
        )
    if _title_payload(target_title_row, include_id=False) != plan["target"]["title"]:
        raise TitleMaterializationConflictError(
            "Target title revision no longer matches its journal"
        )
    state = str(journal_row["state"])
    if state == "prepared":
        expected_flags = (1, 0)
        replay_override = None
    elif state == "applied":
        expected_flags = (0, 1)
        replay_override = {
            "id": int(previous_title_row["id"]),
            "value": str(previous_title_row["title"]),
            "source": str(previous_title_row["title_source"]),
        }
    else:
        raise TitleMaterializationConflictError(
            "Title materialization journal state is invalid"
        )
    if (
        int(previous_title_row["is_current"]),
        int(target_title_row["is_current"]),
    ) != expected_flags:
        raise TitleMaterializationConflictError(
            "Title materialization selection state is inconsistent"
        )
    proposal = _confirmed_materialization_row(
        conn,
        root_fd,
        proposal_id,
        max_transcript_bytes=int(plan["max_transcript_bytes"]),
        current_title_override=replay_override,
    )
    if (
        int(proposal["recording_id"]) != recording_id
        or str(proposal["storage_key"]) != str(plan["storage_key"])
        or str(proposal["manifest_relpath"])
        != str(plan["manifest"]["relpath"])
        or str(proposal["updated_at"]) != str(plan["proposal_updated_at"])
        or str(proposal["confirmation_plan_sha256"])
        != str(plan["confirmation_plan_sha256"])
        or str(proposal["confirmed_at"]) != str(plan["confirmed_at"])
        or str(proposal["suggestion_reason"])
        != str(plan["suggestion_reason"])
        or str(proposal["generator_version"])
        != str(plan["generator_version"])
        or _source_evidence(proposal) != plan["source_evidence"]
        or _target_title(proposal, previous_title_row)
        != plan["target"]["title"]
    ):
        raise TitleMaterializationConflictError(
            "Confirmed title proposal or inference evidence changed after prepare"
        )
    manifest, _raw, manifest_sha256, _snapshot = _read_record_manifest(
        root_fd,
        storage_key=str(plan["storage_key"]),
        manifest_relpath=str(plan["manifest"]["relpath"]),
    )
    previous_sha256 = str(plan["manifest"]["previous_sha256"])
    target_sha256 = str(plan["manifest"]["materialized_sha256"])
    if state == "prepared":
        if manifest_sha256 not in {previous_sha256, target_sha256}:
            raise TitleMaterializationConflictError(
                "Prepared title materialization manifest has an unknown digest"
            )
        expected_title = (
            plan["previous"]["title"]
            if manifest_sha256 == previous_sha256
            else plan["target"]["title"]
        )
    else:
        if manifest_sha256 != target_sha256:
            raise TitleMaterializationConflictError(
                "Applied title materialization manifest digest is invalid"
            )
        expected_title = plan["target"]["title"]
    _require_manifest_title(manifest, expected_title)
    return _public_plan(plan, state=state)


def title_materialization_integrity_issues(
    conn: sqlite3.Connection,
    root_fd: int,
) -> tuple[list[dict[str, Any]], dict[int, sqlite3.Row]]:
    """Cross-check title journals, inference replay, revisions, and manifest."""

    rows = conn.execute(
        """
        SELECT *
        FROM recording_title_materializations
        ORDER BY recording_id, id
        LIMIT 10001
        """
    ).fetchall()
    if len(rows) > 10_000:
        return (
            [
                {
                    "code": "title_materialization_limit_exceeded",
                    "limit": 10_000,
                }
            ],
            {},
        )
    issues: list[dict[str, Any]] = []
    latest_by_recording: dict[int, sqlite3.Row] = {}
    for row in rows:
        recording_id = int(row["recording_id"])
        materialization_id = int(row["id"])
        if recording_id in latest_by_recording:
            issues.append(
                {
                    "code": "title_materialization_chain_unsupported",
                    "recording_id": recording_id,
                    "materialization_id": materialization_id,
                }
            )
            continue
        latest_by_recording[recording_id] = row
        try:
            _stored_plan_from_connection(
                conn,
                root_fd,
                int(row["proposal_id"]),
                journal=row,
            )
        except (
            TitleMaterializationError,
            TitleSuggestionConflictError,
            ClassificationMaterializationConflictError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            issues.append(
                {
                    "code": "title_materialization_journal_invalid",
                    "recording_id": recording_id,
                    "materialization_id": materialization_id,
                    "message": str(exc),
                }
            )
            continue
        if str(row["state"]) == "prepared":
            issues.append(
                {
                    "code": "title_materialization_recovery_required",
                    "recording_id": recording_id,
                    "materialization_id": materialization_id,
                }
            )
    return issues, latest_by_recording


def _readonly_connection(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    if not path.exists():
        raise TitleMaterializationNotFoundError(
            "Storage v2 database is not available"
        )
    conn = connect_v2(path, readonly=True)
    try:
        require_v2_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def plan_title_materialization(
    db_path: Path | str,
    records_root: Path | str,
    proposal_id: int,
    *,
    max_transcript_bytes: int = DEFAULT_MAX_TRANSCRIPT_BYTES,
) -> dict[str, Any]:
    """Build a read-only plan for one confirmed content-title proposal."""

    max_bytes = _validate_max_transcript_bytes(max_transcript_bytes)
    path = Path(db_path).expanduser()
    if not path.exists():
        raise TitleMaterializationNotFoundError(
            "Storage v2 database is not available"
        )
    _assert_database_outside_records_root(path, records_root)
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
        raise TitleMaterializationConflictError(
            "Storage v2 library must verify cleanly before a new title "
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
                max_transcript_bytes=max_bytes,
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
        raise TitleMaterializationWriteDisabledError(
            "Title materializations are disabled"
        )
    if not allow_write:
        raise TitleMaterializationWriteDisabledError(
            "Title materialization requires allow_write=True"
        )
    if isinstance(expected_count, bool) or expected_count != 1:
        raise TitleMaterializationConflictError(
            "Title materialization expected_count must equal 1"
        )
    digest = str(expected_plan_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError(
            "expected_plan_sha256 must be a canonical SHA-256"
        )
    if plan.get("expected_count") != 1 or not hmac.compare_digest(
        str(plan.get("plan_sha256") or ""),
        digest,
    ):
        raise TitleMaterializationConflictError(
            "Title materialization plan changed; create a new plan"
        )
    return digest


def _prepare_materialization(
    conn: sqlite3.Connection,
    root_fd: int,
    *,
    proposal_id: int,
    expected_plan_sha256: str,
    max_transcript_bytes: int,
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
            raise TitleMaterializationConflictError(
                "Existing title materialization belongs to another plan"
            )
        return stored, existing, False
    current = _new_plan_from_connection(
        conn,
        root_fd,
        proposal_id,
        max_transcript_bytes=max_transcript_bytes,
    )
    if current["plan_sha256"] != expected_plan_sha256:
        raise TitleMaterializationConflictError(
            "Title materialization plan changed before prepare"
        )
    # Rebuild the private payload after the guarded public plan comparison.
    proposal = _confirmed_materialization_row(
        conn,
        root_fd,
        proposal_id,
        max_transcript_bytes=max_transcript_bytes,
    )
    recording_id = int(proposal["recording_id"])
    previous_title_row = _exact_current_title(conn, recording_id)
    previous_title = _title_payload(previous_title_row, include_id=True)
    manifest, _raw, previous_manifest_sha256, _snapshot = (
        _read_record_manifest(
            root_fd,
            storage_key=str(proposal["storage_key"]),
            manifest_relpath=str(proposal["manifest_relpath"]),
        )
    )
    _require_manifest_title(manifest, previous_title)
    target_title = _target_title(proposal, previous_title_row)
    target_manifest = copy.deepcopy(manifest)
    target_manifest["title"] = _manifest_title(target_title)
    private_plan = validate_title_materialization_plan_payload(
        {
            "schema_version": PLAN_SCHEMA_VERSION,
            "proposal_id": proposal_id,
            "recording_id": recording_id,
            "storage_key": str(proposal["storage_key"]),
            "proposal_updated_at": str(proposal["updated_at"]),
            "confirmation_plan_sha256": str(
                proposal["confirmation_plan_sha256"]
            ),
            "confirmed_at": str(proposal["confirmed_at"]),
            "suggestion_reason": str(proposal["suggestion_reason"]),
            "generator_version": str(proposal["generator_version"]),
            "max_transcript_bytes": max_transcript_bytes,
            "previous": {"title": previous_title},
            "target": {"title": target_title},
            "manifest": {
                "relpath": str(proposal["manifest_relpath"]),
                "previous_sha256": previous_manifest_sha256,
                "materialized_sha256": _digest_bytes(
                    _manifest_bytes(target_manifest)
                ),
            },
            "source_evidence": _source_evidence(proposal),
            "expected_count": 1,
            "materialization": "canonical_content_title_manifest",
        }
    )
    if _sha256_json(private_plan) != expected_plan_sha256:
        raise TitleMaterializationConflictError(
            "Title materialization private evidence changed before prepare"
        )
    target_cursor = conn.execute(
        """
        INSERT INTO recording_titles(
            recording_id,
            title,
            title_source,
            locale,
            confidence,
            is_current
        )
        VALUES (?, ?, 'system', ?, ?, 0)
        """,
        (
            recording_id,
            str(target_title["value"]),
            target_title["locale"],
            target_title["confidence"],
        ),
    )
    target_title_id = int(target_cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO recording_title_materializations(
            proposal_id,
            recording_id,
            previous_title_id,
            materialized_title_id,
            confirmation_plan_sha256,
            materialization_plan_sha256,
            previous_manifest_sha256,
            materialized_manifest_sha256,
            plan_json,
            state
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared')
        """,
        (
            proposal_id,
            recording_id,
            int(previous_title["id"]),
            target_title_id,
            str(private_plan["confirmation_plan_sha256"]),
            expected_plan_sha256,
            str(private_plan["manifest"]["previous_sha256"]),
            str(private_plan["manifest"]["materialized_sha256"]),
            _canonical_json(private_plan),
        ),
    )
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise TitleMaterializationConflictError(
            "Title materialization prepare would violate foreign keys"
        )
    journal = _journal_row(conn, proposal_id)
    assert journal is not None
    return current, journal, True


def _private_plan_for_journal(
    journal: sqlite3.Row,
) -> dict[str, Any]:
    return _load_stored_payload(journal)


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
    target_sha256 = str(plan["manifest"]["materialized_sha256"])
    if current_sha256 == target_sha256:
        _require_manifest_title(manifest, plan["target"]["title"])
        return manifest, current_sha256
    if current_sha256 != previous_sha256:
        raise TitleMaterializationConflictError(
            "Manifest is neither the planned previous nor target revision"
        )
    _require_manifest_title(manifest, plan["previous"]["title"])
    target_manifest = copy.deepcopy(manifest)
    target_manifest["title"] = _manifest_title(plan["target"]["title"])
    if _digest_bytes(_manifest_bytes(target_manifest)) != target_sha256:
        raise TitleMaterializationConflictError(
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
        raise TitleMaterializationConflictError(
            "Title materialization journal disappeared before finalize"
        )
    stored = _stored_plan_from_connection(
        conn,
        root_fd,
        proposal_id,
        journal=journal,
    )
    if stored["plan_sha256"] != expected_plan_sha256:
        raise TitleMaterializationConflictError(
            "Title materialization journal digest changed before finalize"
        )
    if str(journal["state"]) == "applied":
        return "skipped"
    recording_id = int(journal["recording_id"])
    demoted = conn.execute(
        """
        UPDATE recording_titles
        SET is_current = 0
        WHERE id = ? AND recording_id = ? AND is_current = 1
        """,
        (int(journal["previous_title_id"]), recording_id),
    )
    promoted = conn.execute(
        """
        UPDATE recording_titles
        SET is_current = 1
        WHERE id = ? AND recording_id = ? AND is_current = 0
        """,
        (int(journal["materialized_title_id"]), recording_id),
    )
    if (demoted.rowcount, promoted.rowcount) != (1, 1):
        raise TitleMaterializationConflictError(
            "Title selections changed concurrently"
        )
    updated = conn.execute(
        """
        UPDATE recording_title_materializations
        SET state = 'applied',
            applied_at = CURRENT_TIMESTAMP
        WHERE id = ? AND state = 'prepared' AND applied_at IS NULL
        """,
        (int(journal["id"]),),
    )
    if updated.rowcount != 1:
        raise TitleMaterializationConflictError(
            "Title materialization journal changed concurrently"
        )
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise TitleMaterializationConflictError(
            "Title materialization would violate foreign keys"
        )
    return "materialized"


def apply_title_materialization(
    db_path: Path | str,
    records_root: Path | str,
    proposal_id: int,
    *,
    expected_count: int,
    expected_plan_sha256: str,
    materializations_enabled: bool = False,
    allow_write: bool = False,
    max_transcript_bytes: int = DEFAULT_MAX_TRANSCRIPT_BYTES,
) -> dict[str, Any]:
    """Apply or forward-recover one guarded canonical content title."""

    if not materializations_enabled:
        raise TitleMaterializationWriteDisabledError(
            "Title materializations are disabled"
        )
    if not allow_write:
        raise TitleMaterializationWriteDisabledError(
            "Title materialization requires allow_write=True"
        )
    if isinstance(expected_count, bool) or expected_count != 1:
        raise TitleMaterializationConflictError(
            "Title materialization expected_count must equal 1"
        )
    max_bytes = _validate_max_transcript_bytes(max_transcript_bytes)
    initial_plan = plan_title_materialization(
        db_path,
        records_root,
        proposal_id,
        max_transcript_bytes=max_bytes,
    )
    if int(initial_plan["max_transcript_bytes"]) != max_bytes:
        raise TitleMaterializationConflictError(
            "Title materialization replay must use the planned "
            "max_transcript_bytes"
        )
    digest = _validate_apply_guards(
        initial_plan,
        expected_count=expected_count,
        expected_plan_sha256=expected_plan_sha256,
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
            public_plan, journal, prepared_now = _prepare_materialization(
                conn,
                root_fd,
                proposal_id=proposal_id,
                expected_plan_sha256=digest,
                max_transcript_bytes=max_bytes,
            )
            if public_plan["plan_sha256"] != digest:
                raise TitleMaterializationConflictError(
                    "Title materialization plan changed while locking"
                )
            if str(journal["state"]) == "applied":
                conn.rollback()
                action = "skipped"
            else:
                conn.commit()
                recovery_state_exists = True
                private_plan = _private_plan_for_journal(journal)
                # Re-open and revalidate the committed journal before any
                # filesystem replacement.
                readonly = _readonly_connection(path)
                try:
                    readonly.execute("BEGIN")
                    committed = _journal_row(readonly, proposal_id)
                    assert committed is not None
                    _stored_plan_from_connection(
                        readonly,
                        root_fd,
                        proposal_id,
                        journal=committed,
                    )
                finally:
                    readonly.close()
                target_manifest, current_manifest_sha256 = (
                    _target_manifest_from_plan(root_fd, private_plan)
                )
                if current_manifest_sha256 != str(
                    private_plan["manifest"]["materialized_sha256"]
                ):
                    written_sha256 = _replace_record_manifest(
                        root_fd,
                        storage_key=str(private_plan["storage_key"]),
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
                        raise TitleMaterializationConflictError(
                            "Manifest digest changed after title replacement"
                        )
                conn.execute("BEGIN IMMEDIATE")
                finalized = _finalize_materialization(
                    conn,
                    root_fd,
                    proposal_id=proposal_id,
                    expected_plan_sha256=digest,
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
                TitleMaterializationRecoveryRequiredError,
            ):
                raise TitleMaterializationRecoveryRequiredError(
                    "Title materialization has a recoverable prepared state; "
                    f"replay the same guarded plan: {exc}"
                ) from exc
            raise
        finally:
            conn.close()
    try:
        verification = verify_library(path, records_root)
    except BaseException as exc:
        raise TitleMaterializationPostCommitVerificationError(
            "Title metadata committed, but final library verification could "
            f"not complete: {exc}"
        ) from exc
    if not verification.get("ok"):
        raise TitleMaterializationPostCommitVerificationError(
            "Title metadata committed, but final library verification failed: "
            + _verification_issue_codes(verification)
        )
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "ok": True,
        "action": action,
        "proposal_id": proposal_id,
        "storage_key": str(initial_plan["storage_key"]),
        "expected_count": 1,
        "plan_sha256": digest,
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
