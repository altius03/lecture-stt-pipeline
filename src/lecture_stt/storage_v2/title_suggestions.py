from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import errno
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import unicodedata
from typing import Any, Iterator, Mapping, Sequence

from lecture_stt.shared.paths import repo_root
from lecture_stt.storage_v2.manifest import (
    ManifestValidationError,
    validate_relative_path,
    validate_storage_key,
)
from lecture_stt.storage_v2.repository import connect_v2, require_v2_schema


PLAN_SCHEMA_VERSION = "storage-v2/title-suggestion-plan@1"
RESULT_SCHEMA_VERSION = "storage-v2/title-suggestion-result@1"
LIST_SCHEMA_VERSION = "storage-v2/title-suggestion-list@1"
CONFIRMATION_PLAN_SCHEMA_VERSION = (
    "storage-v2/title-suggestion-confirmation-plan@1"
)
CONFIRMATION_RESULT_SCHEMA_VERSION = (
    "storage-v2/title-suggestion-confirmation-result@1"
)
STATUS_RESULT_SCHEMA_VERSION = "storage-v2/title-suggestion-status-result@1"
REVIEW_DETAIL_SCHEMA_VERSION = "storage-v2/title-suggestion-review@1"
DETAIL_SCHEMA_VERSION = "storage-v2/title-suggestion-detail@1"
GENERATOR_VERSION = "deterministic-keywords-v1"
DEFAULT_MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 16 * 1024 * 1024
_JS_SAFE_INTEGER_MAX = 9_007_199_254_740_991
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]{2,32}")
_CONTEXT_LABELS = {
    "general": None,
    "class_session": "수업",
    "daily_note": "일상 기록",
    "meeting": "회의",
    "memo": "메모",
}
_CONTEXT_TYPES = frozenset(_CONTEXT_LABELS)
_TITLE_DETAIL_FIELDS = frozenset(
    {
        "schema_version",
        "suggestion_reason",
        "generator_version",
        "transcript_revision",
        "classification_status",
        "context_type",
    }
)
_STOPWORDS = frozenset(
    {
        "그리고",
        "그러면",
        "그래서",
        "그런데",
        "하지만",
        "저희",
        "제가",
        "우리",
        "여러분",
        "오늘",
        "지금",
        "이번",
        "관련",
        "대한",
        "통해서",
        "있습니다",
        "없습니다",
        "합니다",
        "했습니다",
        "하는",
        "하면",
        "이제",
        "약간",
        "정말",
        "그냥",
        "일단",
        "네",
        "예",
        "음",
        "어",
        "the",
        "and",
        "that",
        "this",
        "with",
        "from",
        "have",
        "will",
        "just",
        "about",
    }
)


class TitleSuggestionError(RuntimeError):
    """Base error for transcript-content title suggestion operations."""


class TitleSuggestionNotFoundError(TitleSuggestionError):
    """A requested Storage v2 object is not available."""


class TitleSuggestionConflictError(TitleSuggestionError):
    """Current metadata no longer matches a guarded title operation."""


class TitleSuggestionWriteDisabledError(TitleSuggestionError):
    """A title suggestion write is missing an explicit capability guard."""


@dataclass(frozen=True)
class _PlanBundle:
    public: dict[str, Any]
    cases: tuple[dict[str, Any], ...]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_snapshot(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
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


def _regular_file_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


@contextmanager
def _locked_records_root(
    records_root: Path | str,
    *,
    exclusive: bool,
) -> Iterator[int]:
    raw = Path(records_root).expanduser()
    try:
        raw_metadata = raw.lstat()
    except FileNotFoundError as exc:
        raise TitleSuggestionNotFoundError(
            "Storage v2 records root is not available"
        ) from exc
    if raw.is_symlink() or not stat.S_ISDIR(raw_metadata.st_mode):
        raise ValueError(
            "Storage v2 records root must be a non-symlink directory"
        )
    resolved = raw.resolve(strict=True)
    unsafe = {
        Path("/").resolve(),
        Path.home().resolve(),
        repo_root().resolve(),
    }
    if resolved in unsafe:
        raise ValueError("Refusing unsafe Storage v2 records root")
    root_fd = os.open(resolved, _directory_flags())
    baseline = _file_snapshot(os.fstat(root_fd))
    try:
        fcntl.flock(
            root_fd,
            fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
        )
        current = _file_snapshot(os.stat(resolved, follow_symlinks=False))
        if current[:4] != baseline[:4] or not stat.S_ISDIR(current[2]):
            raise TitleSuggestionConflictError(
                "Storage v2 records root identity changed while locking"
            )
        yield root_fd
        after = _file_snapshot(os.stat(resolved, follow_symlinks=False))
        if after[:4] != baseline[:4] or not stat.S_ISDIR(after[2]):
            raise TitleSuggestionConflictError(
                "Storage v2 records root identity changed during title planning"
            )
    finally:
        try:
            fcntl.flock(root_fd, fcntl.LOCK_UN)
        finally:
            os.close(root_fd)


def _open_transcript_fd(
    root_fd: int,
    *,
    storage_key: str,
    path_rel: str,
) -> int:
    components = (storage_key, *PurePosixPath(path_rel).parts)
    current_fd = os.dup(root_fd)
    try:
        for component in components[:-1]:
            next_fd = os.open(
                component,
                _directory_flags(),
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        file_fd = os.open(
            components[-1],
            _regular_file_flags(),
            dir_fd=current_fd,
        )
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(
                    errno.EINVAL,
                    "Transcript artifact is not a regular file",
                )
            if metadata.st_nlink != 1:
                raise OSError(
                    errno.EMLINK,
                    "Transcript artifact must not be hard-linked",
                )
        except BaseException:
            os.close(file_fd)
            raise
        return file_fd
    finally:
        os.close(current_fd)


def _read_transcript_text(
    root_fd: int,
    *,
    storage_key: Any,
    path_rel: Any,
    expected_sha256: Any,
    expected_bytes: Any,
    max_transcript_bytes: int,
) -> str:
    try:
        normalized_key = validate_storage_key(
            str(storage_key),
            field="storage_key",
        )
        normalized_path = validate_relative_path(
            str(path_rel),
            field="transcript.path_rel",
        )
    except ManifestValidationError as exc:
        raise TitleSuggestionConflictError(
            f"Transcript metadata is invalid for {storage_key}"
        ) from exc
    if (
        not isinstance(expected_sha256, str)
        or not _SHA256_RE.fullmatch(expected_sha256)
        or isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes < 0
    ):
        raise TitleSuggestionConflictError(
            f"Transcript metadata is invalid for {normalized_key}"
        )
    if expected_bytes > max_transcript_bytes:
        raise TitleSuggestionConflictError(
            f"Transcript exceeds the configured read limit for {normalized_key}"
        )
    try:
        descriptor = _open_transcript_fd(
            root_fd,
            storage_key=normalized_key,
            path_rel=normalized_path,
        )
    except (FileNotFoundError, OSError) as exc:
        raise TitleSuggestionConflictError(
            f"Transcript artifact is not safely readable for {normalized_key}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if before.st_size > max_transcript_bytes:
            raise TitleSuggestionConflictError(
                f"Transcript exceeds the configured read limit for {normalized_key}"
            )
        payload = bytearray()
        while True:
            remaining = max_transcript_bytes + 1 - len(payload)
            if remaining <= 0:
                raise TitleSuggestionConflictError(
                    f"Transcript exceeds the configured read limit for {normalized_key}"
                )
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        if _file_snapshot(before) != _file_snapshot(after):
            raise TitleSuggestionConflictError(
                f"Transcript changed while reading for {normalized_key}"
            )
        if len(payload) != before.st_size:
            raise TitleSuggestionConflictError(
                f"Transcript size changed while reading for {normalized_key}"
            )
        observed_sha256 = hashlib.sha256(payload).hexdigest()
        if len(payload) != expected_bytes or not hmac.compare_digest(
            observed_sha256,
            expected_sha256,
        ):
            raise TitleSuggestionConflictError(
                f"Transcript artifact metadata does not match for {normalized_key}"
            )
        try:
            decoded = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise TitleSuggestionConflictError(
                f"Transcript is not valid UTF-8 for {normalized_key}"
            ) from exc
    finally:
        os.close(descriptor)
    if "\x00" in decoded:
        raise TitleSuggestionConflictError(
            f"Transcript contains unsupported NUL bytes for {normalized_key}"
        )
    return unicodedata.normalize("NFC", decoded)


def _validate_max_transcript_bytes(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_TRANSCRIPT_BYTES
    ):
        raise ValueError(
            f"max_transcript_bytes must be between 1 and {MAX_TRANSCRIPT_BYTES}"
        )
    return value


def _normalize_storage_keys(
    storage_keys: Sequence[str] | None,
) -> list[str] | None:
    if storage_keys is None:
        return None
    normalized = [
        validate_storage_key(str(value), field="storage_key")
        for value in storage_keys
    ]
    if len(normalized) != len(set(normalized)):
        raise ValueError("storage_keys must not contain duplicates")
    return sorted(normalized)


def _open_readonly(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    if not path.exists():
        raise TitleSuggestionNotFoundError(
            "Storage v2 database is not available"
        )
    conn = connect_v2(path, readonly=True)
    try:
        require_v2_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _assert_database_outside_records_root(
    db_path: Path | str,
    records_root: Path | str,
) -> None:
    try:
        database = Path(db_path).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise TitleSuggestionNotFoundError(
            "Storage v2 database is not available"
        ) from exc
    try:
        root = Path(records_root).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise TitleSuggestionNotFoundError(
            "Storage v2 records root is not available"
        ) from exc
    try:
        database.relative_to(root)
    except ValueError:
        return
    raise ValueError("Storage v2 database must be outside the records root")


def _recording_rows(
    conn: sqlite3.Connection,
    storage_keys: list[str] | None,
) -> list[sqlite3.Row]:
    where = ["recording.archived_at IS NULL"]
    params: list[Any] = []
    if storage_keys is not None:
        if not storage_keys:
            return []
        placeholders = ",".join("?" for _ in storage_keys)
        where.append(f"recording.storage_key IN ({placeholders})")
        params.extend(storage_keys)
    rows = conn.execute(
        f"""
        SELECT
            recording.id AS recording_id,
            recording.storage_key,
            recording.recorded_at,
            title.id AS current_title_id,
            title.title AS current_title_value,
            title.title_source AS current_title_source,
            context.id AS selected_context_id,
            context.context_type,
            context.label AS context_label,
            job.id AS job_id,
            artifact.id AS transcript_artifact_id,
            artifact.path_rel AS transcript_path_rel,
            artifact.content_sha256 AS transcript_sha256,
            artifact.bytes AS transcript_bytes,
            artifact.revision AS transcript_revision
        FROM recordings AS recording
        LEFT JOIN recording_titles AS title
          ON title.recording_id = recording.id
         AND title.is_current = 1
        LEFT JOIN recording_contexts AS context
          ON context.recording_id = recording.id
         AND context.is_selected = 1
        LEFT JOIN transcription_jobs AS job
          ON job.recording_id = recording.id
         AND job.is_current = 1
         AND job.archived_at IS NULL
        LEFT JOIN artifacts AS artifact
          ON artifact.recording_id = recording.id
         AND artifact.job_id = job.id
         AND artifact.artifact_kind = 'transcript_raw_text'
         AND artifact.is_latest = 1
         AND artifact.archived_at IS NULL
        WHERE {' AND '.join(where)}
        ORDER BY recording.storage_key
        """,
        tuple(params),
    ).fetchall()
    if storage_keys is not None:
        found = {str(row["storage_key"]) for row in rows}
        missing = sorted(set(storage_keys).difference(found))
        if missing:
            raise TitleSuggestionNotFoundError(
                "Recording not found: " + ", ".join(missing)
            )
    return rows


def _active_classifications(
    conn: sqlite3.Connection,
    recording_id: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            id,
            status,
            classification_reason,
            proposed_title,
            course_name,
            course_code
        FROM recording_classification_proposals
        WHERE recording_id = ?
          AND status IN ('suggested', 'confirmed')
          AND classification_reason = 'unique_time_match'
        ORDER BY
            CASE status WHEN 'confirmed' THEN 0 ELSE 1 END,
            updated_at DESC,
            id DESC
        LIMIT 2
        """,
        (recording_id,),
    ).fetchall()


def _normalized_search_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _classification_is_in_transcript(
    classification: Mapping[str, Any],
    transcript: str,
) -> bool:
    haystack = _normalized_search_text(transcript)
    for field in ("course_name", "course_code"):
        raw = classification[field]
        if raw is None:
            continue
        needle = _normalized_search_text(
            unicodedata.normalize("NFC", str(raw))
        )
        if len(needle) < 2:
            continue
        if field == "course_code":
            if re.search(
                rf"(?<![0-9a-z가-힣]){re.escape(needle)}"
                r"(?![0-9a-z가-힣])",
                haystack,
            ):
                return True
        elif needle in haystack:
            return True
    return False


def _topic_terms(transcript: str) -> list[str]:
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    first_position: dict[str, int] = {}
    for position, match in enumerate(_TOKEN_RE.finditer(transcript)):
        token = unicodedata.normalize("NFC", match.group(0))
        key = token.casefold()
        if (
            key in _STOPWORDS
            or token.isdigit()
            or len(key) < 2
        ):
            continue
        counts[key] += 1
        display.setdefault(key, token)
        first_position.setdefault(key, position)
    ranked = sorted(
        counts,
        key=lambda key: (
            -counts[key],
            first_position[key],
            key,
        ),
    )
    if len(ranked) < 2:
        return []
    return [display[key] for key in ranked[:3]]


def _recorded_date(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized).date().isoformat()
    except ValueError:
        return None


def _context_prefix(context_type: Any, label: Any) -> str | None:
    normalized_type = (
        str(context_type)
        if context_type in _CONTEXT_TYPES
        else None
    )
    if label is not None:
        normalized_label = unicodedata.normalize(
            "NFC",
            str(label).strip(),
        )
        if (
            normalized_label
            and "\n" not in normalized_label
            and "\r" not in normalized_label
            and len(normalized_label) <= 40
        ):
            return normalized_label
    return _CONTEXT_LABELS.get(normalized_type)


def _bounded_title(parts: Sequence[str]) -> str:
    title = unicodedata.normalize(
        "NFC",
        " ".join(part for part in parts if part).strip(),
    )
    if not title:
        raise TitleSuggestionConflictError(
            "Title suggestion unexpectedly became empty"
        )
    if len(title) > 512:
        title = title[:511].rstrip() + "…"
    return title


def _detail_payload(
    *,
    suggestion_reason: str,
    transcript_revision: int,
    classification_status: str | None,
    context_type: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": REVIEW_DETAIL_SCHEMA_VERSION,
        "suggestion_reason": suggestion_reason,
        "generator_version": GENERATOR_VERSION,
        "transcript_revision": transcript_revision,
        "classification_status": classification_status,
        "context_type": context_type,
    }


def _decode_stored_detail(
    raw_detail: Any,
    *,
    suggestion_reason: Any,
    generator_version: Any,
    transcript_revision: Any,
) -> dict[str, Any]:
    try:
        detail = json.loads(str(raw_detail))
    except (TypeError, ValueError) as exc:
        raise TitleSuggestionConflictError(
            "Stored title suggestion detail is invalid"
        ) from exc
    if not isinstance(detail, dict):
        raise TitleSuggestionConflictError(
            "Stored title suggestion detail is invalid"
        )
    reason = str(suggestion_reason)
    revision = transcript_revision
    classification_status = detail.get("classification_status")
    valid = (
        set(detail) == _TITLE_DETAIL_FIELDS
        and detail.get("schema_version") == REVIEW_DETAIL_SCHEMA_VERSION
        and detail.get("suggestion_reason") == reason
        and detail.get("generator_version") == GENERATOR_VERSION
        and str(generator_version) == GENERATOR_VERSION
        and isinstance(revision, int)
        and not isinstance(revision, bool)
        and revision >= 1
        and detail.get("transcript_revision") == revision
        and detail.get("context_type") in _CONTEXT_TYPES | {None}
        and (
            (
                reason == "schedule_content_match"
                and classification_status in {"suggested", "confirmed"}
            )
            or (
                reason == "content_topic"
                and classification_status is None
            )
        )
        and str(raw_detail) == _canonical_json(detail)
    )
    if not valid:
        raise TitleSuggestionConflictError(
            "Stored title suggestion detail is invalid"
        )
    return detail


def _build_plan(
    conn: sqlite3.Connection,
    root_fd: int,
    *,
    storage_keys: list[str] | None,
    max_transcript_bytes: int,
    current_title_overrides: Mapping[int, Mapping[str, Any]] | None = None,
) -> _PlanBundle:
    rows = _recording_rows(conn, storage_keys)
    coverage = {
        "selected_recordings": len(rows),
        "current_title_protected": 0,
        "transcript_missing": 0,
        "insufficient_content": 0,
        "schedule_content_match": 0,
        "content_topic": 0,
    }
    public_cases: list[dict[str, Any]] = []
    internal_cases: list[dict[str, Any]] = []
    hidden_evidence: list[dict[str, Any]] = []

    for row in rows:
        storage_key = str(row["storage_key"])
        recording_id = int(row["recording_id"])
        override = (
            None
            if current_title_overrides is None
            else current_title_overrides.get(recording_id)
        )
        if override is None:
            current_title_id = row["current_title_id"]
            current_title_value = row["current_title_value"]
            current_title_source = row["current_title_source"]
        else:
            if set(override) != {"id", "value", "source"}:
                raise TitleSuggestionConflictError(
                    "Materialization title replay override is invalid"
                )
            current_title_id = override["id"]
            current_title_value = override["value"]
            current_title_source = override["source"]
            if (
                isinstance(current_title_id, bool)
                or not isinstance(current_title_id, int)
                or current_title_id <= 0
                or not isinstance(current_title_value, str)
                or not current_title_value
                or not isinstance(current_title_source, str)
                or not current_title_source
            ):
                raise TitleSuggestionConflictError(
                    "Materialization title replay override is invalid"
                )
        if current_title_source in {"manual", "schedule"}:
            coverage["current_title_protected"] += 1
            continue
        if row["transcript_artifact_id"] is None:
            coverage["transcript_missing"] += 1
            continue
        transcript = _read_transcript_text(
            root_fd,
            storage_key=storage_key,
            path_rel=row["transcript_path_rel"],
            expected_sha256=row["transcript_sha256"],
            expected_bytes=row["transcript_bytes"],
            max_transcript_bytes=max_transcript_bytes,
        )
        classifications = _active_classifications(
            conn,
            int(row["recording_id"]),
        )
        selected_classification = (
            classifications[0]
            if len(classifications) == 1
            and _classification_is_in_transcript(
                classifications[0],
                transcript,
            )
            else None
        )
        context_type = (
            str(row["context_type"])
            if row["context_type"] in _CONTEXT_TYPES
            else None
        )
        date = _recorded_date(row["recorded_at"])
        if selected_classification is not None:
            suggestion_reason = "schedule_content_match"
            proposed_title = _bounded_title(
                [str(selected_classification["proposed_title"])]
            )
            confidence = 0.9
            classification_id = int(selected_classification["id"])
            classification_status = str(selected_classification["status"])
        else:
            terms = _topic_terms(transcript)
            if not terms:
                coverage["insufficient_content"] += 1
                continue
            suggestion_reason = "content_topic"
            topic = " · ".join(terms)
            proposed_title = _bounded_title(
                [
                    date or "",
                    _context_prefix(
                        context_type,
                        row["context_label"],
                    )
                    or "",
                    topic,
                ]
            )
            confidence = 0.55
            classification_id = None
            classification_status = None
        coverage[suggestion_reason] += 1
        transcript_revision = int(row["transcript_revision"])
        detail = _detail_payload(
            suggestion_reason=suggestion_reason,
            transcript_revision=transcript_revision,
            classification_status=classification_status,
            context_type=context_type,
        )
        public_case = {
            "storage_key": storage_key,
            "suggestion_reason": suggestion_reason,
            "proposed_title": proposed_title,
            "confidence": confidence,
            "recorded_date": date,
            "transcript_revision": transcript_revision,
            "classification_status": classification_status,
            "context_type": context_type,
        }
        public_cases.append(public_case)
        internal_cases.append(
            {
                **public_case,
                "recording_id": int(row["recording_id"]),
                "job_id": int(row["job_id"]),
                "transcript_artifact_id": int(
                    row["transcript_artifact_id"]
                ),
                "classification_proposal_id": classification_id,
                "detail_json": _canonical_json(detail),
            }
        )
        hidden_evidence.append(
            {
                "storage_key": storage_key,
                "recording_id": recording_id,
                "recorded_at": (
                    None
                    if row["recorded_at"] is None
                    else str(row["recorded_at"])
                ),
                "current_title_id": (
                    None
                    if current_title_id is None
                    else int(current_title_id)
                ),
                "current_title_value": (
                    None
                    if current_title_value is None
                    else str(current_title_value)
                ),
                "current_title_source": (
                    None
                    if current_title_source is None
                    else str(current_title_source)
                ),
                "selected_context_id": (
                    None
                    if row["selected_context_id"] is None
                    else int(row["selected_context_id"])
                ),
                "selected_context_type": (
                    None
                    if row["context_type"] is None
                    else str(row["context_type"])
                ),
                "selected_context_label": (
                    None
                    if row["context_label"] is None
                    else str(row["context_label"])
                ),
                "job_id": int(row["job_id"]),
                "transcript_artifact_id": int(
                    row["transcript_artifact_id"]
                ),
                "transcript_path_rel": str(row["transcript_path_rel"]),
                "transcript_sha256": str(row["transcript_sha256"]),
                "transcript_bytes": int(row["transcript_bytes"]),
                "transcript_revision": transcript_revision,
                "classification_proposal_id": classification_id,
                "active_classifications": [
                    {
                        "id": int(classification["id"]),
                        "status": str(classification["status"]),
                        "classification_reason": str(
                            classification["classification_reason"]
                        ),
                        "proposed_title": str(
                            classification["proposed_title"]
                        ),
                        "course_name": (
                            None
                            if classification["course_name"] is None
                            else str(classification["course_name"])
                        ),
                        "course_code": (
                            None
                            if classification["course_code"] is None
                            else str(classification["course_code"])
                        ),
                    }
                    for classification in classifications
                ],
            }
        )
    digest_payload = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "max_transcript_bytes": max_transcript_bytes,
        "storage_keys": storage_keys,
        "expected_count": len(public_cases),
        "coverage": coverage,
        "cases": public_cases,
        "source_evidence": hidden_evidence,
    }
    public = {
        key: value
        for key, value in digest_payload.items()
        if key != "source_evidence"
    }
    public["mode"] = "read_only"
    public["plan_sha256"] = _sha256_json(digest_payload)
    public["canonical_metadata_changed"] = False
    return _PlanBundle(
        public=public,
        cases=tuple(internal_cases),
    )


def plan_title_suggestions(
    db_path: Path | str,
    records_root: Path | str,
    *,
    storage_keys: Sequence[str] | None = None,
    max_transcript_bytes: int = DEFAULT_MAX_TRANSCRIPT_BYTES,
) -> dict[str, Any]:
    """Build review-only title suggestions from verified transcript bytes."""

    normalized_keys = _normalize_storage_keys(storage_keys)
    max_bytes = _validate_max_transcript_bytes(max_transcript_bytes)
    _assert_database_outside_records_root(db_path, records_root)
    with _locked_records_root(records_root, exclusive=False) as root_fd:
        conn = _open_readonly(db_path)
        try:
            conn.execute("BEGIN")
            return _build_plan(
                conn,
                root_fd,
                storage_keys=normalized_keys,
                max_transcript_bytes=max_bytes,
            ).public
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()


def _validate_apply_guards(
    plan: Mapping[str, Any],
    *,
    expected_count: int,
    expected_plan_sha256: str,
    operation: str,
) -> str:
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 0
    ):
        raise ValueError("expected_count must be a non-negative integer")
    digest = str(expected_plan_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError(
            "expected_plan_sha256 must be a canonical SHA-256"
        )
    if expected_count != int(plan["expected_count"]):
        raise TitleSuggestionConflictError(
            f"{operation} expected_count does not match the current plan"
        )
    if not hmac.compare_digest(digest, str(plan["plan_sha256"])):
        raise TitleSuggestionConflictError(
            f"{operation} plan digest does not match the current plan"
        )
    return digest


def apply_title_suggestions(
    db_path: Path | str,
    records_root: Path | str,
    *,
    expected_count: int,
    expected_plan_sha256: str,
    storage_keys: Sequence[str] | None = None,
    max_transcript_bytes: int = DEFAULT_MAX_TRANSCRIPT_BYTES,
    suggestions_enabled: bool = False,
    allow_write: bool = False,
) -> dict[str, Any]:
    """Persist proposal/review rows only; never change canonical metadata."""

    if not suggestions_enabled:
        raise TitleSuggestionWriteDisabledError(
            "Title suggestion writes are disabled"
        )
    if not allow_write:
        raise TitleSuggestionWriteDisabledError(
            "Title suggestion apply requires allow_write=True"
        )
    normalized_keys = _normalize_storage_keys(storage_keys)
    max_bytes = _validate_max_transcript_bytes(max_transcript_bytes)
    path = Path(db_path).expanduser()
    if not path.exists():
        raise TitleSuggestionNotFoundError(
            "Storage v2 database is not available"
        )
    _assert_database_outside_records_root(path, records_root)

    with _locked_records_root(records_root, exclusive=False) as root_fd:
        readonly_conn = _open_readonly(path)
        try:
            readonly_conn.execute("BEGIN")
            readonly_plan = _build_plan(
                readonly_conn,
                root_fd,
                storage_keys=normalized_keys,
                max_transcript_bytes=max_bytes,
            )
            normalized_digest = _validate_apply_guards(
                readonly_plan.public,
                expected_count=expected_count,
                expected_plan_sha256=expected_plan_sha256,
                operation="Title suggestion apply",
            )
        finally:
            try:
                readonly_conn.rollback()
            finally:
                readonly_conn.close()

    with _locked_records_root(records_root, exclusive=True) as root_fd:
        conn = connect_v2(path)
        try:
            require_v2_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            plan = _build_plan(
                conn,
                root_fd,
                storage_keys=normalized_keys,
                max_transcript_bytes=max_bytes,
            )
            _validate_apply_guards(
                plan.public,
                expected_count=expected_count,
                expected_plan_sha256=normalized_digest,
                operation="Title suggestion apply",
            )
            created = 0
            skipped = 0
            for case in plan.cases:
                existing = conn.execute(
                    """
                    SELECT
                        proposal.*,
                        review.id AS linked_review_id,
                        review.recording_id AS review_recording_id,
                        review.job_id AS review_job_id,
                        review.artifact_id AS review_artifact_id,
                        review.status AS review_status,
                        review.severity AS review_severity,
                        review.reason_code AS review_reason_code,
                        review.detail_json AS review_detail_json,
                        review.resolved_at AS review_resolved_at
                    FROM recording_title_proposals AS proposal
                    LEFT JOIN review_items AS review
                      ON review.id = proposal.review_item_id
                     AND review.recording_id = proposal.recording_id
                    WHERE proposal.recording_id = ?
                      AND proposal.status IN ('suggested', 'confirmed')
                    """,
                    (case["recording_id"],),
                ).fetchone()
                expected_fields = {
                    "transcript_artifact_id": case[
                        "transcript_artifact_id"
                    ],
                    "classification_proposal_id": case[
                        "classification_proposal_id"
                    ],
                    "suggestion_reason": case["suggestion_reason"],
                    "proposed_title": case["proposed_title"],
                    "confidence": case["confidence"],
                    "generator_version": GENERATOR_VERSION,
                    "detail_json": case["detail_json"],
                }
                if existing is not None:
                    mismatched = [
                        field
                        for field, expected in expected_fields.items()
                        if existing[field] != expected
                    ]
                    expected_review_statuses = (
                        {"open", "triaged"}
                        if existing["status"] == "suggested"
                        else {"resolved"}
                    )
                    review_mismatches = {
                        "review_item_id": (
                            existing["linked_review_id"]
                            != existing["review_item_id"]
                        ),
                        "review_recording_id": (
                            existing["review_recording_id"]
                            != case["recording_id"]
                        ),
                        "review_job_id": (
                            existing["review_job_id"] != case["job_id"]
                        ),
                        "review_artifact_id": (
                            existing["review_artifact_id"]
                            != case["transcript_artifact_id"]
                        ),
                        "review_status": (
                            existing["review_status"]
                            not in expected_review_statuses
                        ),
                        "review_severity": (
                            existing["review_severity"] != "medium"
                        ),
                        "review_reason_code": (
                            existing["review_reason_code"]
                            != (
                                "title_suggestion_"
                                f"{case['suggestion_reason']}"
                            )
                        ),
                        "review_detail_json": (
                            existing["review_detail_json"]
                            != case["detail_json"]
                        ),
                        "review_resolved_at": (
                            (
                                existing["status"] == "suggested"
                                and existing["review_resolved_at"] is not None
                            )
                            or (
                                existing["status"] == "confirmed"
                                and existing["review_resolved_at"] is None
                            )
                        ),
                    }
                    mismatched.extend(
                        field
                        for field, is_mismatched in review_mismatches.items()
                        if is_mismatched
                    )
                    if mismatched:
                        raise TitleSuggestionConflictError(
                            "Existing active title suggestion differs for "
                            f"{case['storage_key']}: "
                            + ", ".join(sorted(mismatched))
                        )
                    skipped += 1
                    continue
                review_cursor = conn.execute(
                    """
                    INSERT INTO review_items(
                        recording_id,
                        job_id,
                        artifact_id,
                        status,
                        severity,
                        reason_code,
                        detail_json
                    )
                    VALUES (?, ?, ?, 'open', 'medium', ?, ?)
                    """,
                    (
                        case["recording_id"],
                        case["job_id"],
                        case["transcript_artifact_id"],
                        f"title_suggestion_{case['suggestion_reason']}",
                        case["detail_json"],
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO recording_title_proposals(
                        recording_id,
                        transcript_artifact_id,
                        classification_proposal_id,
                        review_item_id,
                        status,
                        suggestion_reason,
                        proposed_title,
                        confidence,
                        generator_version,
                        detail_json
                    )
                    VALUES (?, ?, ?, ?, 'suggested', ?, ?, ?, ?, ?)
                    """,
                    (
                        case["recording_id"],
                        case["transcript_artifact_id"],
                        case["classification_proposal_id"],
                        int(review_cursor.lastrowid),
                        case["suggestion_reason"],
                        case["proposed_title"],
                        case["confidence"],
                        GENERATOR_VERSION,
                        case["detail_json"],
                    ),
                )
                created += 1

            final_plan = _build_plan(
                conn,
                root_fd,
                storage_keys=normalized_keys,
                max_transcript_bytes=max_bytes,
            )
            _validate_apply_guards(
                final_plan.public,
                expected_count=expected_count,
                expected_plan_sha256=normalized_digest,
                operation="Title suggestion final recheck",
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "ok": True,
        "count": expected_count,
        "created": created,
        "skipped": skipped,
        "plan_sha256": normalized_digest,
        "canonical_metadata_changed": False,
    }


def _validate_pagination(limit: int, offset: int) -> tuple[int, int]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 0 <= limit <= 200
    ):
        raise ValueError("limit must be between 0 and 200")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= 100_000
    ):
        raise ValueError("offset must be between 0 and 100000")
    return limit, offset


def _validated_public_proposal_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("proposal_id must be a positive integer")
    if value <= 0:
        raise ValueError("proposal_id must be a positive integer")
    if value > _JS_SAFE_INTEGER_MAX:
        raise ValueError("proposal_id exceeds the public integer limit")
    return int(value)


def _validate_list_lifecycle(
    *,
    status: str,
    review_status: str,
    confirmed_at: Any,
    confirmation_plan_sha256: Any,
    review_resolved_at: Any,
) -> None:
    if status == "suggested":
        valid = (
            review_status in {"open", "triaged"}
            and confirmed_at is None
            and confirmation_plan_sha256 is None
            and review_resolved_at is None
        )
    elif status == "confirmed":
        valid = (
            review_status == "resolved"
            and confirmed_at is not None
            and confirmation_plan_sha256 is not None
            and review_resolved_at is not None
        )
    elif status == "rejected":
        valid = (
            review_status == "dismissed"
            and confirmed_at is None
            and confirmation_plan_sha256 is None
            and review_resolved_at is not None
        )
    else:
        valid = False
    if not valid:
        raise TitleSuggestionConflictError(
            "Stored title suggestion lifecycle is inconsistent"
        )


def list_title_suggestions(
    db_path: Path | str,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Read a bounded metadata-only title suggestion queue."""

    limit, offset = _validate_pagination(limit, offset)
    normalized_status = str(status or "").strip().lower() or None
    if normalized_status not in {
        None,
        "suggested",
        "confirmed",
        "rejected",
    }:
        raise ValueError("status must be suggested, confirmed, or rejected")
    path = Path(db_path).expanduser()
    if not path.exists():
        return {
            "schema_version": LIST_SCHEMA_VERSION,
            "available": False,
            "counts": {
                "suggested": 0,
                "confirmed": 0,
                "rejected": 0,
            },
            "total": 0,
            "proposals": [],
        }
    conn = _open_readonly(path)
    try:
        counts = {
            "suggested": 0,
            "confirmed": 0,
            "rejected": 0,
        }
        for row in conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM recording_title_proposals
            GROUP BY status
            """
        ).fetchall():
            counts[str(row["status"])] = int(row["count"])
        if normalized_status is None:
            where_sql = ""
            params: tuple[Any, ...] = (limit, offset)
            total = sum(counts.values())
        else:
            where_sql = "WHERE proposal.status = ?"
            params = (normalized_status, limit, offset)
            total = counts[normalized_status]
        rows = conn.execute(
            f"""
            SELECT
                proposal.id,
                recording.storage_key,
                proposal.status,
                proposal.suggestion_reason,
                proposal.proposed_title,
                proposal.confidence,
                proposal.generator_version,
                proposal.detail_json,
                review.status AS review_status,
                review.resolved_at AS review_resolved_at,
                artifact.revision AS transcript_revision,
                proposal.created_at,
                proposal.updated_at,
                proposal.confirmed_at,
                proposal.confirmation_plan_sha256
            FROM recording_title_proposals AS proposal
            JOIN recordings AS recording
              ON recording.id = proposal.recording_id
            JOIN review_items AS review
              ON review.id = proposal.review_item_id
             AND review.recording_id = proposal.recording_id
            JOIN artifacts AS artifact
              ON artifact.id = proposal.transcript_artifact_id
             AND artifact.recording_id = proposal.recording_id
            {where_sql}
            ORDER BY
                CASE proposal.status
                    WHEN 'suggested' THEN 0
                    WHEN 'confirmed' THEN 1
                    ELSE 2
                END,
                proposal.updated_at DESC,
                proposal.id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    proposals: list[dict[str, Any]] = []
    for row in rows:
        _validate_list_lifecycle(
            status=str(row["status"]),
            review_status=str(row["review_status"]),
            confirmed_at=row["confirmed_at"],
            confirmation_plan_sha256=row["confirmation_plan_sha256"],
            review_resolved_at=row["review_resolved_at"],
        )
        detail = _decode_stored_detail(
            row["detail_json"],
            suggestion_reason=row["suggestion_reason"],
            generator_version=row["generator_version"],
            transcript_revision=row["transcript_revision"],
        )
        proposed_title = str(row["proposed_title"])
        if proposed_title != unicodedata.normalize("NFC", proposed_title):
            raise TitleSuggestionConflictError(
                "Stored title suggestion is not NFC-normalized"
            )
        proposals.append(
            {
                "id": _validated_public_proposal_id(row["id"]),
                "storage_key": str(row["storage_key"]),
                "status": str(row["status"]),
                "suggestion_reason": str(row["suggestion_reason"]),
                "proposed_title": proposed_title,
                "confidence": float(row["confidence"]),
                "generator_version": str(row["generator_version"]),
                "review_status": str(row["review_status"]),
                "transcript_revision": int(
                    row["transcript_revision"]
                ),
                "classification_status": detail.get(
                    "classification_status"
                ),
                "context_type": detail.get("context_type"),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "confirmed_at": (
                    None
                    if row["confirmed_at"] is None
                    else str(row["confirmed_at"])
                ),
                "canonical_metadata_changed": False,
            }
        )
    return {
        "schema_version": LIST_SCHEMA_VERSION,
        "available": True,
        "counts": counts,
        "total": total,
        "proposals": proposals,
    }


def _linked_classification_payload(row: sqlite3.Row) -> dict[str, Any] | None:
    suggestion_reason = str(row["suggestion_reason"])
    if suggestion_reason == "content_topic":
        if any(
            row[field] is not None
            for field in (
                "classification_proposal_id",
                "classification_status",
                "classification_reason",
                "classification_proposed_title",
                "classification_course_name",
                "classification_course_code",
            )
        ):
            raise TitleSuggestionConflictError(
                "Content title suggestion has an unexpected classification"
            )
        return None
    if suggestion_reason != "schedule_content_match":
        raise TitleSuggestionConflictError(
            "Stored title suggestion reason is invalid"
        )
    if (
        row["classification_proposal_id"] is None
        or row["classification_status"] not in {"suggested", "confirmed", "rejected"}
        or row["classification_reason"] != "unique_time_match"
        or unicodedata.normalize(
            "NFC",
            str(row["classification_proposed_title"]),
        )
        != unicodedata.normalize(
            "NFC",
            str(row["proposed_title"]),
        )
    ):
        raise TitleSuggestionConflictError(
            "Linked timetable classification is inconsistent"
        )
    return {
        "status": str(row["classification_status"]),
        "classification_reason": "unique_time_match",
        "proposed_title": unicodedata.normalize(
            "NFC",
            str(row["classification_proposed_title"]),
        ),
        "course_name": (
            None
            if row["classification_course_name"] is None
            else unicodedata.normalize(
                "NFC",
                str(row["classification_course_name"]),
            )
        ),
        "course_code": (
            None
            if row["classification_course_code"] is None
            else unicodedata.normalize(
                "NFC",
                str(row["classification_course_code"]),
            )
        ),
    }


def read_title_suggestion(
    db_path: Path | str,
    proposal_id: int,
) -> dict[str, Any]:
    """Read one metadata-only title suggestion detail."""

    conn = _open_readonly(db_path)
    try:
        row = _proposal_row(conn, proposal_id)
        lifecycle = str(row["status"])
        if lifecycle not in {"suggested", "confirmed", "rejected"}:
            raise TitleSuggestionConflictError(
                "Title suggestion lifecycle is inconsistent"
            )
        detail = _validate_proposal_review_evidence(
            row,
            lifecycle=lifecycle,
        )
        return {
            "schema_version": DETAIL_SCHEMA_VERSION,
            "proposal": {
                "id": _validated_public_proposal_id(row["id"]),
                "storage_key": str(row["storage_key"]),
                "status": lifecycle,
                "suggestion_reason": str(row["suggestion_reason"]),
                "proposed_title": unicodedata.normalize(
                    "NFC",
                    str(row["proposed_title"]),
                ),
                "confidence": float(row["confidence"]),
                "generator_version": str(row["generator_version"]),
                "review_status": str(row["review_status"]),
                "transcript_revision": int(row["transcript_revision"]),
                "classification_status": detail.get(
                    "classification_status"
                ),
                "context_type": detail.get("context_type"),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "confirmed_at": (
                    None
                    if row["confirmed_at"] is None
                    else str(row["confirmed_at"])
                ),
            },
            "linked_classification": _linked_classification_payload(row),
            "canonical_metadata_changed": False,
        }
    finally:
        conn.close()


def _proposal_row(
    conn: sqlite3.Connection,
    proposal_id: int,
) -> sqlite3.Row:
    proposal_id = _validated_public_proposal_id(proposal_id)
    row = conn.execute(
        """
        SELECT
            proposal.*,
            recording.storage_key,
            recording.manifest_relpath,
            recording.archived_at AS recording_archived_at,
            review.status AS review_status,
            review.job_id AS review_job_id,
            review.artifact_id AS review_artifact_id,
            review.severity AS review_severity,
            review.reason_code AS review_reason_code,
            review.detail_json AS review_detail_json,
            review.resolved_at AS review_resolved_at,
            artifact.job_id AS transcript_job_id,
            artifact.artifact_kind AS transcript_artifact_kind,
            artifact.revision AS transcript_revision,
            artifact.path_rel AS transcript_path_rel,
            artifact.content_sha256 AS transcript_sha256,
            artifact.bytes AS transcript_bytes,
            artifact.is_latest AS transcript_is_latest,
            artifact.archived_at AS transcript_archived_at,
            job.is_current AS transcript_job_is_current,
            job.archived_at AS transcript_job_archived_at,
            classification.status AS classification_status,
            classification.classification_reason,
            classification.proposed_title AS classification_proposed_title,
            classification.course_name AS classification_course_name,
            classification.course_code AS classification_course_code
        FROM recording_title_proposals AS proposal
        JOIN recordings AS recording
          ON recording.id = proposal.recording_id
        JOIN review_items AS review
          ON review.id = proposal.review_item_id
         AND review.recording_id = proposal.recording_id
        JOIN artifacts AS artifact
          ON artifact.id = proposal.transcript_artifact_id
         AND artifact.recording_id = proposal.recording_id
        JOIN transcription_jobs AS job
          ON job.id = artifact.job_id
         AND job.recording_id = artifact.recording_id
        LEFT JOIN recording_classification_proposals AS classification
          ON classification.id = proposal.classification_proposal_id
         AND classification.recording_id = proposal.recording_id
        WHERE proposal.id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise TitleSuggestionNotFoundError(
            f"Title suggestion not found: {proposal_id}"
        )
    return row


def _validate_proposal_review_evidence(
    row: sqlite3.Row,
    *,
    lifecycle: str,
) -> dict[str, Any]:
    if str(row["status"]) != lifecycle:
        raise TitleSuggestionConflictError(
            "Title suggestion lifecycle is inconsistent"
        )
    if lifecycle == "suggested":
        expected_statuses = {"open", "triaged"}
    elif lifecycle == "confirmed":
        expected_statuses = {"resolved"}
    elif lifecycle == "rejected":
        expected_statuses = {"dismissed"}
    else:
        raise ValueError("Unsupported title suggestion lifecycle")
    if str(row["review_status"]) not in expected_statuses:
        raise TitleSuggestionConflictError(
            "Title suggestion review state is inconsistent"
        )
    detail = _decode_stored_detail(
        row["detail_json"],
        suggestion_reason=row["suggestion_reason"],
        generator_version=row["generator_version"],
        transcript_revision=row["transcript_revision"],
    )
    expected_review_reason = (
        f"title_suggestion_{row['suggestion_reason']}"
    )
    if (
        row["review_job_id"] != row["transcript_job_id"]
        or row["review_artifact_id"] != row["transcript_artifact_id"]
        or row["review_severity"] != "medium"
        or row["review_reason_code"] != expected_review_reason
        or row["review_detail_json"] != row["detail_json"]
        or (
            lifecycle == "confirmed"
            and (
                row["review_resolved_at"] is None
                or row["confirmation_plan_sha256"] is None
                or row["confirmed_at"] is None
            )
        )
        or (
            lifecycle == "rejected"
            and (
                row["review_resolved_at"] is None
                or row["confirmation_plan_sha256"] is not None
                or row["confirmed_at"] is not None
            )
        )
        or (
            lifecycle == "suggested"
            and (
                row["review_resolved_at"] is not None
                or row["confirmation_plan_sha256"] is not None
                or row["confirmed_at"] is not None
            )
        )
        or row["transcript_artifact_kind"] != "transcript_raw_text"
        or str(row["proposed_title"])
        != unicodedata.normalize("NFC", str(row["proposed_title"]))
    ):
        raise TitleSuggestionConflictError(
            "Title suggestion review evidence is inconsistent"
        )
    return detail


def _validate_linked_classification(
    row: sqlite3.Row,
    current_case: Mapping[str, Any],
) -> dict[str, Any] | None:
    reason = str(row["suggestion_reason"])
    if reason == "content_topic":
        if (
            row["classification_proposal_id"] is not None
            or row["classification_status"] is not None
            or row["classification_reason"] is not None
            or row["classification_proposed_title"] is not None
            or row["classification_course_name"] is not None
            or row["classification_course_code"] is not None
            or current_case["classification_proposal_id"] is not None
        ):
            raise TitleSuggestionConflictError(
                "Content title suggestion has an unexpected classification"
            )
        return None
    if (
        row["classification_proposal_id"] is None
        or row["classification_status"] not in {"suggested", "confirmed"}
        or row["classification_reason"] != "unique_time_match"
        or row["classification_proposed_title"] != row["proposed_title"]
        or current_case["classification_proposal_id"]
        != row["classification_proposal_id"]
    ):
        raise TitleSuggestionConflictError(
            "Linked timetable classification is no longer consistent"
        )
    return {
        "proposal_id": int(row["classification_proposal_id"]),
        "active_state": "suggested_or_confirmed",
        "classification_reason": "unique_time_match",
        "proposed_title": str(row["classification_proposed_title"]),
        "course_name": (
            None
            if row["classification_course_name"] is None
            else str(row["classification_course_name"])
        ),
        "course_code": (
            None
            if row["classification_course_code"] is None
            else str(row["classification_course_code"])
        ),
    }


def _current_suggestion_evidence(
    conn: sqlite3.Connection,
    root_fd: int,
    row: sqlite3.Row,
    *,
    max_transcript_bytes: int,
    current_title_override: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    current_plan = _build_plan(
        conn,
        root_fd,
        storage_keys=[str(row["storage_key"])],
        max_transcript_bytes=max_transcript_bytes,
        current_title_overrides=(
            None
            if current_title_override is None
            else {
                int(row["recording_id"]): dict(current_title_override),
            }
        ),
    )
    if (
        current_plan.public["expected_count"] != 1
        or len(current_plan.cases) != 1
    ):
        raise TitleSuggestionConflictError(
            "Title suggestion is no longer supported by current metadata"
        )
    current_case = current_plan.cases[0]
    expected_fields = {
        "storage_key": str(row["storage_key"]),
        "recording_id": int(row["recording_id"]),
        "job_id": int(row["transcript_job_id"]),
        "transcript_artifact_id": int(row["transcript_artifact_id"]),
        "classification_proposal_id": row[
            "classification_proposal_id"
        ],
        "suggestion_reason": str(row["suggestion_reason"]),
        "proposed_title": str(row["proposed_title"]),
        "confidence": float(row["confidence"]),
        "transcript_revision": int(row["transcript_revision"]),
        "detail_json": str(row["detail_json"]),
    }
    mismatched = [
        field
        for field, expected in expected_fields.items()
        if current_case[field] != expected
    ]
    if mismatched:
        raise TitleSuggestionConflictError(
            "Title suggestion no longer matches current inference: "
            + ", ".join(sorted(mismatched))
        )
    classification_evidence = _validate_linked_classification(
        row,
        current_case,
    )
    return (
        str(current_plan.public["plan_sha256"]),
        classification_evidence,
    )


def _confirmation_digest_payload(
    row: sqlite3.Row,
    *,
    detail: Mapping[str, Any],
    suggestion_plan_sha256: str,
    classification_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": CONFIRMATION_PLAN_SCHEMA_VERSION,
        "proposal_id": int(row["id"]),
        "storage_key": str(row["storage_key"]),
        "suggestion_reason": str(row["suggestion_reason"]),
        "proposed_title": str(row["proposed_title"]),
        "transcript_revision": int(row["transcript_revision"]),
        "expected_count": 1,
        "materialization": "confirmation_audit_only",
        "source_evidence": {
            "recording_id": int(row["recording_id"]),
            "proposal_created_at": str(row["created_at"]),
            "confidence": float(row["confidence"]),
            "generator_version": str(row["generator_version"]),
            "proposal_detail": dict(detail),
            "suggestion_plan_sha256": suggestion_plan_sha256,
            "transcript_artifact_id": int(
                row["transcript_artifact_id"]
            ),
            "transcript_job_id": int(row["transcript_job_id"]),
            "transcript_path_rel": str(row["transcript_path_rel"]),
            "transcript_sha256": str(row["transcript_sha256"]),
            "transcript_bytes": int(row["transcript_bytes"]),
            "review_item_id": int(row["review_item_id"]),
            "review_precondition": "open_or_triaged",
            "review_job_id": int(row["review_job_id"]),
            "review_artifact_id": int(row["review_artifact_id"]),
            "review_severity": str(row["review_severity"]),
            "review_reason_code": str(row["review_reason_code"]),
            "review_detail_json": str(row["review_detail_json"]),
            "classification": (
                None
                if classification_evidence is None
                else dict(classification_evidence)
            ),
        },
    }


def _confirmation_plan_from_connection(
    conn: sqlite3.Connection,
    root_fd: int,
    proposal_id: int,
    *,
    max_transcript_bytes: int,
) -> dict[str, Any]:
    row = _proposal_row(conn, proposal_id)
    if str(row["status"]) != "suggested":
        raise TitleSuggestionConflictError(
            "Only suggested title proposals can be confirmed"
        )
    detail = _validate_proposal_review_evidence(
        row,
        lifecycle="suggested",
    )
    if (
        row["transcript_archived_at"] is not None
        or row["transcript_job_archived_at"] is not None
        or int(row["transcript_is_latest"]) != 1
        or int(row["transcript_job_is_current"]) != 1
    ):
        raise TitleSuggestionConflictError(
            "Title suggestion transcript is no longer current"
        )
    suggestion_plan_sha256, classification_evidence = (
        _current_suggestion_evidence(
            conn,
            root_fd,
            row,
            max_transcript_bytes=max_transcript_bytes,
        )
    )
    digest_payload = _confirmation_digest_payload(
        row,
        detail=detail,
        suggestion_plan_sha256=suggestion_plan_sha256,
        classification_evidence=classification_evidence,
    )
    public_payload = {
        key: value
        for key, value in digest_payload.items()
        if key != "source_evidence"
    }
    return {
        **public_payload,
        "mode": "read_only",
        "plan_sha256": _sha256_json(digest_payload),
        "canonical_metadata_changed": False,
    }


def plan_title_suggestion_confirmation(
    db_path: Path | str,
    records_root: Path | str,
    proposal_id: int,
    *,
    max_transcript_bytes: int = DEFAULT_MAX_TRANSCRIPT_BYTES,
) -> dict[str, Any]:
    max_bytes = _validate_max_transcript_bytes(max_transcript_bytes)
    _assert_database_outside_records_root(db_path, records_root)
    with _locked_records_root(records_root, exclusive=False) as root_fd:
        conn = _open_readonly(db_path)
        try:
            return _confirmation_plan_from_connection(
                conn,
                root_fd,
                proposal_id,
                max_transcript_bytes=max_bytes,
            )
        finally:
                conn.close()


def _validate_confirmed_replay(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    root_fd: int,
    *,
    expected_plan_sha256: str,
    max_transcript_bytes: int,
    current_title_override: Mapping[str, Any] | None = None,
) -> None:
    if str(row["status"]) != "confirmed":
        raise TitleSuggestionConflictError(
            "Title suggestion is not confirmed"
        )
    detail = _validate_proposal_review_evidence(
        row,
        lifecycle="confirmed",
    )
    suggestion_plan_sha256, classification_evidence = (
        _current_suggestion_evidence(
            conn,
            root_fd,
            row,
            max_transcript_bytes=max_transcript_bytes,
            current_title_override=current_title_override,
        )
    )
    observed_digest = _sha256_json(
        _confirmation_digest_payload(
            row,
            detail=detail,
            suggestion_plan_sha256=suggestion_plan_sha256,
            classification_evidence=classification_evidence,
        )
    )
    stored_digest = str(row["confirmation_plan_sha256"] or "")
    if (
        not hmac.compare_digest(stored_digest, expected_plan_sha256)
        or not hmac.compare_digest(observed_digest, expected_plan_sha256)
    ):
        raise TitleSuggestionConflictError(
            "Title suggestion was confirmed by another or changed plan"
        )


def _validated_confirmed_materialization_row(
    conn: sqlite3.Connection,
    root_fd: int,
    proposal_id: int,
    *,
    max_transcript_bytes: int,
    current_title_override: Mapping[str, Any] | None = None,
) -> sqlite3.Row:
    """Revalidate one confirmed proposal for canonical materialization.

    A normal confirmed replay always uses the actual current title. A
    materialized replay may provide the journal's previous title solely to
    reconstruct the original inference digest; the materialization layer must
    independently prove that the actual current title is the journal target.
    """

    row = _proposal_row(conn, proposal_id)
    if (
        row["recording_archived_at"] is not None
        or row["transcript_archived_at"] is not None
        or row["transcript_job_archived_at"] is not None
        or int(row["transcript_is_latest"]) != 1
        or int(row["transcript_job_is_current"]) != 1
    ):
        raise TitleSuggestionConflictError(
            "Title suggestion transcript is no longer current"
        )
    stored_digest = str(row["confirmation_plan_sha256"] or "")
    if not _SHA256_RE.fullmatch(stored_digest):
        raise TitleSuggestionConflictError(
            "Title suggestion confirmation digest is invalid"
        )
    _validate_confirmed_replay(
        conn,
        row,
        root_fd,
        expected_plan_sha256=stored_digest,
        max_transcript_bytes=max_transcript_bytes,
        current_title_override=current_title_override,
    )
    return row


def apply_title_suggestion_confirmation(
    db_path: Path | str,
    records_root: Path | str,
    proposal_id: int,
    *,
    expected_count: int,
    expected_plan_sha256: str,
    confirmations_enabled: bool = False,
    allow_write: bool = False,
    max_transcript_bytes: int = DEFAULT_MAX_TRANSCRIPT_BYTES,
) -> dict[str, Any]:
    """Confirm a reviewed proposal without changing the canonical title."""

    if not confirmations_enabled:
        raise TitleSuggestionWriteDisabledError(
            "Title suggestion confirmations are disabled"
        )
    if not allow_write:
        raise TitleSuggestionWriteDisabledError(
            "Title suggestion confirmation requires allow_write=True"
        )
    if isinstance(expected_count, bool) or expected_count != 1:
        raise TitleSuggestionConflictError(
            "Title suggestion confirmation expected_count must equal 1"
        )
    digest = str(expected_plan_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError(
            "expected_plan_sha256 must be a canonical SHA-256"
        )
    path = Path(db_path).expanduser()
    max_bytes = _validate_max_transcript_bytes(max_transcript_bytes)
    _assert_database_outside_records_root(path, records_root)
    with _locked_records_root(records_root, exclusive=False) as root_fd:
        readonly_conn = _open_readonly(path)
        try:
            row = _proposal_row(readonly_conn, proposal_id)
            if str(row["status"]) == "confirmed":
                _validate_confirmed_replay(
                    readonly_conn,
                    row,
                    root_fd,
                    expected_plan_sha256=digest,
                    max_transcript_bytes=max_bytes,
                )
                return {
                    "schema_version": CONFIRMATION_RESULT_SCHEMA_VERSION,
                    "ok": True,
                    "action": "skipped",
                    "proposal_id": proposal_id,
                    "status": "confirmed",
                    "plan_sha256": digest,
                    "canonical_metadata_changed": False,
                }
            plan = _confirmation_plan_from_connection(
                readonly_conn,
                root_fd,
                proposal_id,
                max_transcript_bytes=max_bytes,
            )
            _validate_apply_guards(
                plan,
                expected_count=expected_count,
                expected_plan_sha256=digest,
                operation="Title suggestion confirmation",
            )
        finally:
            readonly_conn.close()

    with _locked_records_root(records_root, exclusive=True) as root_fd:
        conn = connect_v2(path)
        try:
            require_v2_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = _proposal_row(conn, proposal_id)
            if str(row["status"]) == "confirmed":
                _validate_confirmed_replay(
                    conn,
                    row,
                    root_fd,
                    expected_plan_sha256=digest,
                    max_transcript_bytes=max_bytes,
                )
                conn.commit()
                return {
                    "schema_version": CONFIRMATION_RESULT_SCHEMA_VERSION,
                    "ok": True,
                    "action": "skipped",
                    "proposal_id": proposal_id,
                    "status": "confirmed",
                    "plan_sha256": digest,
                    "canonical_metadata_changed": False,
                }
            plan = _confirmation_plan_from_connection(
                conn,
                root_fd,
                proposal_id,
                max_transcript_bytes=max_bytes,
            )
            _validate_apply_guards(
                plan,
                expected_count=expected_count,
                expected_plan_sha256=digest,
                operation="Title suggestion confirmation",
            )
            proposal_cursor = conn.execute(
                """
                UPDATE recording_title_proposals
                SET status = 'confirmed',
                    confirmation_plan_sha256 = ?,
                    confirmed_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'suggested'
                """,
                (digest, proposal_id),
            )
            if proposal_cursor.rowcount != 1:
                raise TitleSuggestionConflictError(
                    "Title suggestion changed concurrently"
                )
            review_cursor = conn.execute(
                """
                UPDATE review_items
                SET status = 'resolved',
                    resolved_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status IN ('open', 'triaged')
                """,
                (int(row["review_item_id"]),),
            )
            if review_cursor.rowcount != 1:
                raise TitleSuggestionConflictError(
                    "Title suggestion review changed concurrently"
                )
            final_row = _proposal_row(conn, proposal_id)
            _validate_confirmed_replay(
                conn,
                final_row,
                root_fd,
                expected_plan_sha256=digest,
                max_transcript_bytes=max_bytes,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {
        "schema_version": CONFIRMATION_RESULT_SCHEMA_VERSION,
        "ok": True,
        "action": "confirmed",
        "proposal_id": proposal_id,
        "status": "confirmed",
        "plan_sha256": digest,
        "canonical_metadata_changed": False,
    }


def reject_title_suggestion(
    db_path: Path | str,
    proposal_id: int,
    *,
    status_writes_enabled: bool = False,
    allow_write: bool = False,
) -> dict[str, Any]:
    """Reject a suggestion and dismiss its review without canonical writes."""

    if not status_writes_enabled:
        raise TitleSuggestionWriteDisabledError(
            "Title suggestion status writes are disabled"
        )
    if not allow_write:
        raise TitleSuggestionWriteDisabledError(
            "Title suggestion rejection requires allow_write=True"
        )
    path = Path(db_path).expanduser()
    if not path.exists():
        raise TitleSuggestionNotFoundError(
            "Storage v2 database is not available"
        )
    conn = connect_v2(path)
    try:
        require_v2_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = _proposal_row(conn, proposal_id)
        if str(row["status"]) == "rejected":
            _validate_proposal_review_evidence(
                row,
                lifecycle="rejected",
            )
            conn.commit()
            return {
                "schema_version": STATUS_RESULT_SCHEMA_VERSION,
                "ok": True,
                "action": "skipped",
                "proposal_id": proposal_id,
                "status": "rejected",
                "review_status": "dismissed",
                "canonical_metadata_changed": False,
            }
        if str(row["status"]) != "suggested":
            raise TitleSuggestionConflictError(
                "Only suggested title proposals can be rejected"
            )
        _validate_proposal_review_evidence(
            row,
            lifecycle="suggested",
        )
        proposal_cursor = conn.execute(
            """
            UPDATE recording_title_proposals
            SET status = 'rejected'
            WHERE id = ? AND status = 'suggested'
            """,
            (proposal_id,),
        )
        if proposal_cursor.rowcount != 1:
            raise TitleSuggestionConflictError(
                "Title suggestion changed concurrently"
            )
        review_cursor = conn.execute(
            """
            UPDATE review_items
            SET status = 'dismissed',
                resolved_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status IN ('open', 'triaged')
            """,
            (int(row["review_item_id"]),),
        )
        if review_cursor.rowcount != 1:
            raise TitleSuggestionConflictError(
                "Title suggestion review changed concurrently"
            )
        final_row = _proposal_row(conn, proposal_id)
        _validate_proposal_review_evidence(
            final_row,
            lifecycle="rejected",
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "schema_version": STATUS_RESULT_SCHEMA_VERSION,
        "ok": True,
        "action": "rejected",
        "proposal_id": proposal_id,
        "status": "rejected",
        "review_status": "dismissed",
        "canonical_metadata_changed": False,
    }
