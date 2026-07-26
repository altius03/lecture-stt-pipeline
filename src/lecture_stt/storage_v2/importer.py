from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import stat as stat_module
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping, Sequence

from lecture_stt.shared import utils
from lecture_stt.shared.paths import repo_root
from lecture_stt.storage_v2.manifest import (
    build_manifest,
    validate_manifest,
    validate_relative_path,
    validate_storage_key,
    write_manifest,
)
from lecture_stt.storage_v2.repository import apply_migration


_STATUS_MAP = {
    "PENDING": "queued",
    "PROCESSING": "processing",
    "DONE": "done",
    "NEEDS_REVIEW": "needs_review",
    "ERROR": "error",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DELIVERY_ARTIFACT_KINDS = {
    "correction_text",
    "correction_json",
    "summary_markdown",
    "summary_json",
}


@dataclass(frozen=True)
class ImportIssue:
    severity: str
    code: str
    message: str
    path: str | None = None

    @property
    def blocking(self) -> bool:
        return self.severity == "error"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.path:
            payload["path"] = self.path
        return payload


@dataclass(frozen=True)
class ArtifactSource:
    kind: str
    source_path: Path | None
    target_relpath: str
    sha256: str
    bytes: int
    generated_bytes: bytes | None = None
    mime_type: str | None = None
    source_dev: int | None = None
    source_ino: int | None = None

    def manifest_entry(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "path": self.target_relpath,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }
        if self.mime_type:
            payload["mime_type"] = self.mime_type
        return payload


@dataclass(frozen=True)
class ArtifactAbsenceProbe:
    kind: str
    path: Path


@dataclass(frozen=True)
class LegacyCandidate:
    legacy_db_path: Path
    legacy_root: Path
    legacy_job_id: int
    legacy_delivery_key: str | None
    canonical_base: str
    storage_key: str
    job_key: str
    source_fingerprint: str
    original_name_raw: str
    original_name_nfc: str
    legacy_status: str
    v2_status: str
    title: str
    recorded_at: str | None
    received_at: str
    source_sha256: str
    source_bytes: int | None
    source_mime: str | None
    source_available: bool
    requested_profile: str | None
    requested_profile_version: str | None
    engine_name: str
    engine_version: str | None
    engine_params: Mapping[str, Any]
    context: Mapping[str, Any]
    timing: Mapping[str, Any]
    artifacts: tuple[ArtifactSource, ...]
    absent_artifacts: tuple[ArtifactAbsenceProbe, ...]
    issues: tuple[ImportIssue, ...]


@dataclass(frozen=True)
class ImportPlan:
    candidate: LegacyCandidate
    legacy_database_snapshot: _LegacyDatabaseSnapshot

    @property
    def can_apply(self) -> bool:
        return not any(issue.blocking for issue in self.candidate.issues)

    def as_dict(self) -> dict[str, Any]:
        candidate = self.candidate
        return {
            "legacy_job_id": candidate.legacy_job_id,
            "legacy_delivery_key": candidate.legacy_delivery_key,
            "canonical_base": candidate.canonical_base,
            "storage_key": candidate.storage_key,
            "job_key": candidate.job_key,
            "source_fingerprint": candidate.source_fingerprint,
            "legacy_status": candidate.legacy_status,
            "v2_status": candidate.v2_status,
            "title": candidate.title,
            "original_name_nfc": candidate.original_name_nfc,
            "source_available": candidate.source_available,
            "legacy_database_snapshot": (
                self.legacy_database_snapshot.as_dict()
            ),
            "can_apply": self.can_apply,
            "artifacts": [artifact.manifest_entry() for artifact in candidate.artifacts],
            "absent_artifacts": [
                {
                    "kind": probe.kind,
                    "path": probe.path.relative_to(
                        candidate.legacy_root
                    ).as_posix(),
                    "state": "missing",
                }
                for probe in candidate.absent_artifacts
            ],
            "issues": [issue.as_dict() for issue in candidate.issues],
        }


@dataclass(frozen=True)
class ImportResult:
    action: str
    storage_key: str
    legacy_job_id: int
    recording_id: int | None
    job_id: int | None
    manifest_path: Path
    issues: tuple[ImportIssue, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "storage_key": self.storage_key,
            "legacy_job_id": self.legacy_job_id,
            "recording_id": self.recording_id,
            "job_id": self.job_id,
            "manifest_path": str(self.manifest_path),
            "issues": [issue.as_dict() for issue in self.issues],
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


@dataclass(frozen=True)
class _LegacyDatabaseSnapshot:
    main: tuple[int, int, int, int, int, int, int]
    main_sha256: str
    sidecars: tuple[
        tuple[str, tuple[int, int, int, int, int, int, int] | None],
        ...,
    ]

    def as_dict(self) -> dict[str, Any]:
        stat_fields = (
            "device",
            "inode",
            "mode",
            "links",
            "bytes",
            "mtime_ns",
            "ctime_ns",
        )

        def stat_payload(
            value: tuple[int, int, int, int, int, int, int] | None,
        ) -> dict[str, int] | None:
            if value is None:
                return None
            return dict(zip(stat_fields, value, strict=True))

        return {
            "main": stat_payload(self.main),
            "main_sha256": self.main_sha256,
            "sidecars": {
                suffix: stat_payload(state)
                for suffix, state in self.sidecars
            },
        }


def _regular_file_state(path: Path, *, label: str) -> tuple[int, int, int, int, int, int, int]:
    try:
        current = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} disappeared during snapshot validation: {path}") from exc
    if not stat_module.S_ISREG(current.st_mode):
        raise RuntimeError(f"{label} must be a regular file: {path}")
    if current.st_nlink != 1:
        raise RuntimeError(f"{label} must not be hard-linked: {path}")
    return (
        int(current.st_dev),
        int(current.st_ino),
        int(current.st_mode),
        int(current.st_nlink),
        int(current.st_size),
        int(current.st_mtime_ns),
        int(current.st_ctime_ns),
    )


def _optional_sidecar_state(
    path: Path,
    *,
    reject_nonempty: bool,
) -> tuple[int, int, int, int, int, int, int] | None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return None
    if not stat_module.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise RuntimeError(f"Legacy SQLite sidecar is unsafe: {path}")
    if reject_nonempty and current.st_size > 0:
        raise RuntimeError(
            "Legacy DB has uncheckpointed WAL/journal state. "
            "Use a standalone SQLite backup for a strictly read-only import plan."
        )
    return (
        int(current.st_dev),
        int(current.st_ino),
        int(current.st_mode),
        int(current.st_nlink),
        int(current.st_size),
        int(current.st_mtime_ns),
        int(current.st_ctime_ns),
    )


def _legacy_database_snapshot(path: Path) -> _LegacyDatabaseSnapshot:
    main = _regular_file_state(path, label="Legacy DB")
    sidecars = tuple(
        (
            suffix,
            _optional_sidecar_state(
                path.with_name(f"{path.name}{suffix}"),
                reject_nonempty=suffix in {"-wal", "-journal"},
            ),
        )
        for suffix in ("-wal", "-journal", "-shm")
    )
    return _LegacyDatabaseSnapshot(
        main=main,
        main_sha256=utils.compute_sha256(path),
        sidecars=sidecars,
    )


def _assert_legacy_database_stable(
    path: Path,
    baseline: _LegacyDatabaseSnapshot,
) -> None:
    if _legacy_database_snapshot(path) != baseline:
        raise RuntimeError(
            "Legacy DB or its SQLite sidecars changed during the read-only plan. "
            "Use a stable standalone SQLite backup."
        )


def _open_legacy_readonly(
    db_path: Path | str,
) -> tuple[sqlite3.Connection, Path, _LegacyDatabaseSnapshot]:
    raw_path = Path(db_path).expanduser()
    if raw_path.is_symlink():
        raise ValueError(f"Legacy DB must not be a symlink: {raw_path}")
    path = raw_path.resolve(strict=True)
    baseline = _legacy_database_snapshot(path)
    conn = sqlite3.connect(
        f"{path.as_uri()}?mode=ro&immutable=1",
        uri=True,
        timeout=5.0,
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        _assert_legacy_database_stable(path, baseline)
        quick_check = [
            str(row[0])
            for row in conn.execute("PRAGMA quick_check").fetchall()
        ]
        if quick_check != ["ok"]:
            raise RuntimeError(
                "Legacy DB failed PRAGMA quick_check: "
                + ", ".join(quick_check or ["no result"])
            )
        tables = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        _assert_legacy_database_stable(path, baseline)
        if "jobs" not in tables:
            raise RuntimeError(f"Legacy jobs table is missing: {path}")
        return conn, path, baseline
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise RuntimeError(
            f"Legacy DB is not a valid, intact SQLite snapshot: {path}"
        ) from exc
    except BaseException:
        conn.close()
        raise


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _is_symlink_component(path: Path, root: Path) -> bool:
    absolute = path.absolute()
    ancestor = absolute
    while ancestor.parent != ancestor:
        try:
            if ancestor.resolve(strict=False) == root:
                break
        except OSError:
            return True
        ancestor = ancestor.parent
    else:
        return True

    relative = absolute.relative_to(ancestor)
    current = ancestor
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _resolve_legacy_file(
    value: str | Path,
    *,
    legacy_root: Path,
    code_prefix: str,
    required: bool,
) -> tuple[Path | None, ImportIssue | None]:
    raw_path = Path(value).expanduser()
    if ".." in raw_path.parts:
        return None, ImportIssue(
            "error",
            f"{code_prefix}_path_traversal",
            "Legacy artifact path contains parent traversal",
            str(raw_path),
        )
    candidate = raw_path if raw_path.is_absolute() else legacy_root / raw_path
    try:
        prospective = candidate.resolve(strict=False)
        prospective.relative_to(legacy_root)
    except ValueError:
        return None, ImportIssue(
            "error",
            f"{code_prefix}_outside_root",
            "Legacy artifact is outside the configured legacy root",
            str(candidate),
        )
    except OSError as exc:
        return None, ImportIssue(
            "error",
            f"{code_prefix}_unreadable",
            f"Legacy artifact path cannot be resolved safely: {exc}",
            str(candidate),
        )
    if _is_symlink_component(candidate.absolute(), legacy_root):
        return None, ImportIssue(
            "error",
            f"{code_prefix}_symlink",
            "Legacy artifact or one of its path components is a symlink",
            str(candidate),
        )
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        severity = "error" if required else "warning"
        return None, ImportIssue(
            severity,
            f"{code_prefix}_missing",
            "Legacy artifact is missing",
            str(candidate),
        )
    except OSError as exc:
        return None, ImportIssue(
            "error",
            f"{code_prefix}_unreadable",
            f"Legacy artifact cannot be resolved: {exc}",
            str(candidate),
        )

    try:
        resolved.relative_to(legacy_root)
    except ValueError:
        return None, ImportIssue(
            "error",
            f"{code_prefix}_outside_root",
            "Legacy artifact is outside the configured legacy root",
            str(resolved),
        )
    if not resolved.is_file():
        return None, ImportIssue(
            "error",
            f"{code_prefix}_not_regular",
            "Legacy artifact is not a regular file",
            str(resolved),
        )
    return resolved, None


def _assert_unmatched_ownerless_delivery_has_no_artifacts(
    delivery: Mapping[str, Any],
    *,
    legacy_root: Path,
) -> None:
    logical_stem = str(delivery.get("logical_stem") or "").strip()
    artifact_sources = (
        (
            "correction_txt_path",
            delivery.get("correction_txt_path"),
            legacy_root / "03_correction" / f"{logical_stem}.txt",
            "correction_txt",
        ),
        (
            "correction_json_path",
            delivery.get("correction_json_path"),
            legacy_root / "03_correction" / f"{logical_stem}.json",
            "correction_json",
        ),
        (
            "summary_md_path",
            delivery.get("summary_md_path"),
            legacy_root / "04_summarize" / f"{logical_stem}.md",
            "summary",
        ),
    )
    for field, raw_value, fallback, code_prefix in artifact_sources:
        raw_text = str(raw_value or "").strip()
        resolved, issue = _resolve_legacy_file(
            raw_text or fallback,
            legacy_root=legacy_root,
            code_prefix=f"unmatched_delivery_{code_prefix}",
            required=False,
        )
        if resolved is not None:
            raise RuntimeError(
                f"Unmatched ownerless delivery {logical_stem!r} references "
                f"an existing artifact via {field}: {resolved}"
            )
        if issue is not None and issue.blocking:
            raise RuntimeError(
                f"Unmatched ownerless delivery {logical_stem!r} has an "
                f"unsafe {field}: {issue.code}"
            )


def _artifact(
    *,
    kind: str,
    source_path: Path,
    target_relpath: str,
) -> ArtifactSource:
    validate_relative_path(target_relpath)
    stat = source_path.stat()
    sha256 = utils.compute_sha256(source_path)
    mime_type, _ = mimetypes.guess_type(source_path.name)
    return ArtifactSource(
        kind=kind,
        source_path=source_path,
        target_relpath=target_relpath,
        sha256=sha256,
        bytes=int(stat.st_size),
        generated_bytes=None,
        mime_type=mime_type,
        source_dev=int(stat.st_dev),
        source_ino=int(stat.st_ino),
    )


def _missing_source_artifact(
    *,
    legacy_job_id: int,
    original_name_nfc: str,
    expected_sha256: str | None,
) -> ArtifactSource:
    payload = {
        "schema_version": "storage-v2/missing-source@1",
        "availability": "missing",
        "reason": "legacy_source_missing",
        "legacy_job_id": legacy_job_id,
        "original_name_nfc": original_name_nfc,
        "expected_sha256": expected_sha256 or None,
    }
    content = (_canonical_json(payload) + "\n").encode("utf-8")
    return ArtifactSource(
        kind="metadata",
        source_path=None,
        target_relpath="source/unavailable.json",
        sha256=hashlib.sha256(content).hexdigest(),
        bytes=len(content),
        generated_bytes=content,
        mime_type="application/vnd.lecture-stt.missing-source+json",
        source_dev=None,
        source_ino=None,
    )


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"Non-finite JSON value: {value}")


def _parse_json_object(raw: Any) -> tuple[dict[str, Any], ImportIssue | None]:
    if not isinstance(raw, str) or not raw.strip():
        return {}, None
    try:
        payload = json.loads(
            raw,
            parse_constant=_reject_non_finite_json,
        )
    except (TypeError, ValueError) as exc:
        return {}, ImportIssue(
            "warning",
            "engine_params_invalid",
            f"Legacy engine_params is not valid JSON: {exc}",
        )
    if not isinstance(payload, dict):
        return {}, ImportIssue(
            "warning",
            "engine_params_not_object",
            "Legacy engine_params JSON is not an object",
        )
    return payload, None


def _legacy_optional_sha256(
    raw: Any,
    *,
    code_prefix: str,
    issues: list[ImportIssue],
) -> str | None:
    value = str(raw or "").strip().lower()
    if not value:
        return None
    if not _SHA256_RE.fullmatch(value):
        issues.append(
            ImportIssue(
                "error",
                f"{code_prefix}_hash_invalid",
                "Legacy artifact SHA-256 is not a lowercase 64-character digest",
            )
        )
        return None
    return value


def _session_date(canonical_base: str) -> str | None:
    if len(canonical_base) < 6 or not canonical_base[:6].isdigit():
        return None
    try:
        parsed = datetime.strptime(canonical_base[:6], "%y%m%d")
    except ValueError:
        return None
    return parsed.date().isoformat()


def _period_label(canonical_base: str) -> str | None:
    match = re.search(r"_(\d+)$", canonical_base)
    if not match:
        return None
    return f"{int(match.group(1))}교시"


def _context_for(
    canonical_base: str,
    delivery: Mapping[str, Any],
) -> dict[str, Any]:
    subject = str(delivery.get("subject_abbr") or "").strip()
    known_subject = bool(subject and subject.upper() != "UNKNOWN")
    session_date = _session_date(canonical_base) if known_subject else None
    period = _period_label(canonical_base) if known_subject else None
    snapshot = {
        "legacy_canonical_base": canonical_base,
        "legacy_subject_abbr": subject or None,
        "legacy_correction_status": delivery.get("correction_status"),
        "legacy_summary_status": delivery.get("summary_status"),
        "legacy_correction_txt_sha256": delivery.get("correction_txt_sha256"),
        "legacy_correction_json_sha256": delivery.get("correction_json_sha256"),
        "legacy_summary_md_sha256": delivery.get("summary_md_sha256"),
    }
    return {
        "context_type": "class_session" if known_subject else "general",
        "label": canonical_base,
        "semester": None,
        "course_name": None,
        "course_code": subject if known_subject else None,
        "session_date": session_date,
        "period_label": period,
        "period_index": int(period[:-2]) if period else None,
        "context_json": snapshot,
    }


def _candidate_from_rows(
    job: Mapping[str, Any],
    delivery: Mapping[str, Any],
    *,
    legacy_db_path: Path,
    legacy_root: Path,
    include_delivery_artifacts: bool = True,
    initial_issues: Sequence[ImportIssue] = (),
) -> LegacyCandidate:
    issues: list[ImportIssue] = list(initial_issues)
    absent_artifacts: list[ArtifactAbsenceProbe] = []
    legacy_job_id = int(job["id"])
    canonical_base = str(job.get("canonical_base") or "").strip()
    if not canonical_base:
        canonical_base = f"legacy-job-{legacy_job_id}"
        issues.append(
            ImportIssue(
                "warning",
                "canonical_base_missing",
                "Legacy canonical_base is missing; a deterministic placeholder is used",
            )
        )

    original_name_raw = str(job.get("orig_name") or Path(str(job.get("canonical_audio_path") or "")).name)
    if not original_name_raw:
        original_name_raw = f"{canonical_base}.audio"
    original_name_nfc = unicodedata.normalize("NFC", original_name_raw)

    legacy_status = str(job.get("status") or "").upper()
    v2_status = _STATUS_MAP.get(legacy_status, "error")
    if legacy_status not in _STATUS_MAP:
        issues.append(
            ImportIssue(
                "warning",
                "unsupported_legacy_status",
                f"Unsupported legacy status is imported as error: {legacy_status or '<empty>'}",
            )
        )
    if legacy_status in {"PENDING", "PROCESSING"}:
        issues.append(
            ImportIssue(
                "error",
                "active_legacy_job",
                "Active legacy jobs must be settled before preserve-first import",
            )
        )

    engine_params, engine_issue = _parse_json_object(job.get("engine_params"))
    if engine_issue:
        issues.append(engine_issue)
    profile = engine_params.get("profile") if isinstance(engine_params.get("profile"), dict) else {}
    requested_profile = str(profile.get("key")) if profile.get("key") else None
    requested_profile_version = str(profile.get("version")) if profile.get("version") else None
    engine_name = str(engine_params.get("engine") or "faster-whisper")
    engine_version = (
        str(engine_params.get("model_size"))
        if engine_params.get("model_size") is not None
        else None
    )

    artifacts: list[ArtifactSource] = []
    job_relroot_placeholder = f"jobs/legacy_job_{legacy_job_id}"

    audio_raw = str(job.get("canonical_audio_path") or "").strip()
    audio_path: Path | None = None
    if not audio_raw:
        issues.append(
            ImportIssue(
                "warning",
                "source_path_missing",
                "Legacy source audio path is missing; an unavailable marker will be imported",
            )
        )
    else:
        audio_path, issue = _resolve_legacy_file(
            audio_raw,
            legacy_root=legacy_root,
            code_prefix="source",
            required=False,
        )
        if issue:
            issues.append(issue)
            if issue.code == "source_missing" and issue.path:
                absent_artifacts.append(
                    ArtifactAbsenceProbe(
                        kind="source_copy",
                        path=Path(issue.path).resolve(strict=False),
                    )
                )

    raw_source_sha = str(job.get("sha256") or "").strip().lower()
    source_sha = raw_source_sha
    if not _SHA256_RE.fullmatch(source_sha):
        if raw_source_sha:
            issues.append(
                ImportIssue(
                    "warning",
                    "source_hash_invalid",
                    "Legacy jobs.sha256 is not a valid SHA-256 digest",
                )
            )
        source_sha = ""
    if audio_path is not None:
        suffix = audio_path.suffix.lower() or ".bin"
        source_artifact = _artifact(
            kind="source_copy",
            source_path=audio_path,
            target_relpath=f"source/original{suffix}",
        )
        if source_sha and source_sha != source_artifact.sha256:
            issues.append(
                ImportIssue(
                    "error",
                    "source_hash_mismatch",
                    "Legacy source audio does not match jobs.sha256",
                    str(audio_path),
                )
            )
        source_sha = source_artifact.sha256
        artifacts.append(source_artifact)
    elif not any(issue.blocking for issue in issues):
        artifacts.append(
            _missing_source_artifact(
                legacy_job_id=legacy_job_id,
                original_name_nfc=original_name_nfc,
                expected_sha256=source_sha or None,
            )
        )

    def add_optional(
        *,
        raw_value: Any,
        fallback: Path | None,
        kind: str,
        filename: str,
        code_prefix: str,
        expected: bool,
        expected_sha256: Any = None,
        hash_authoritative: bool = True,
    ) -> None:
        nonlocal v2_status
        expected_digest = _legacy_optional_sha256(
            expected_sha256,
            code_prefix=code_prefix,
            issues=issues,
        )
        raw_text = str(raw_value or "").strip()
        source_value: Path | str | None = raw_text or fallback
        if source_value is None:
            if expected:
                issues.append(
                    ImportIssue(
                        "warning",
                        f"{code_prefix}_path_missing",
                        "Expected legacy artifact path is missing",
                    )
                )
            return
        resolved, issue = _resolve_legacy_file(
            source_value,
            legacy_root=legacy_root,
            code_prefix=code_prefix,
            required=False,
        )
        if issue:
            if issue.code == f"{code_prefix}_missing" and issue.path:
                absent_artifacts.append(
                    ArtifactAbsenceProbe(
                        kind=kind,
                        path=Path(issue.path).resolve(strict=False),
                    )
                )
            if expected or issue.blocking:
                issues.append(issue)
            return
        assert resolved is not None
        artifact = _artifact(
            kind=kind,
            source_path=resolved,
            target_relpath=f"{job_relroot_placeholder}/{filename}",
        )
        if expected_digest is not None and artifact.sha256 != expected_digest:
            if hash_authoritative:
                issues.append(
                    ImportIssue(
                        "error",
                        f"{code_prefix}_hash_mismatch",
                        "Legacy artifact does not match its recorded SHA-256",
                        str(resolved),
                    )
                )
            else:
                issues.append(
                    ImportIssue(
                        "warning",
                        f"{code_prefix}_hash_stale_non_success",
                        "Non-success legacy delivery retained a stale SHA-256; "
                        "the current file bytes are preserved for review",
                        str(resolved),
                    )
                )
                if v2_status == "done":
                    v2_status = "needs_review"
        artifacts.append(artifact)

    transcript_txt_fallback = legacy_root / "02_transcripts" / f"{canonical_base}.txt"
    transcript_json_fallback = legacy_root / "02_transcripts" / f"{canonical_base}.json"
    transcript_expected = legacy_status in {"DONE", "NEEDS_REVIEW"}
    add_optional(
        raw_value=job.get("transcript_txt_path"),
        fallback=transcript_txt_fallback,
        kind="transcript_raw_text",
        filename="transcript.txt",
        code_prefix="transcript_txt",
        expected=transcript_expected,
    )
    add_optional(
        raw_value=job.get("transcript_json_path"),
        fallback=transcript_json_fallback,
        kind="transcript_segments_json",
        filename="transcript.segments.json",
        code_prefix="transcript_json",
        expected=transcript_expected,
    )
    quality_fallback = legacy_root / "02_transcripts" / f"{canonical_base}.quality.json"
    add_optional(
        raw_value=quality_fallback,
        fallback=None,
        kind="quality_scorecard",
        filename="quality.json",
        code_prefix="quality",
        expected=False,
    )

    delivery_key = str(delivery.get("logical_stem") or "").strip() or None
    correction_expected = str(delivery.get("correction_status") or "").upper() == "DELIVERED"
    summary_expected = str(delivery.get("summary_status") or "").upper() == "DELIVERED"
    if include_delivery_artifacts:
        add_optional(
            raw_value=delivery.get("correction_txt_path"),
            fallback=legacy_root / "03_correction" / f"{canonical_base}.txt",
            kind="correction_text",
            filename="correction.txt",
            code_prefix="correction_txt",
            expected=correction_expected,
            expected_sha256=delivery.get("correction_txt_sha256"),
            hash_authoritative=correction_expected,
        )
        add_optional(
            raw_value=delivery.get("correction_json_path"),
            fallback=legacy_root / "03_correction" / f"{canonical_base}.json",
            kind="correction_json",
            filename="correction.json",
            code_prefix="correction_json",
            expected=correction_expected,
            expected_sha256=delivery.get("correction_json_sha256"),
            hash_authoritative=correction_expected,
        )
        add_optional(
            raw_value=delivery.get("summary_md_path"),
            fallback=legacy_root / "04_summarize" / f"{canonical_base}.md",
            kind="summary_markdown",
            filename="summary.md",
            code_prefix="summary",
            expected=summary_expected,
            expected_sha256=delivery.get("summary_md_sha256"),
            hash_authoritative=summary_expected,
        )

    artifact_kinds = {artifact.kind for artifact in artifacts}
    if transcript_expected and not {
        "transcript_raw_text",
        "transcript_segments_json",
    }.issubset(artifact_kinds):
        v2_status = "needs_review"
        issues.append(
            ImportIssue(
                "warning",
                "transcript_pair_incomplete",
                "Completed legacy job has an incomplete transcript pair",
            )
        )

    received_at = next(
        (
            str(value).strip()
            for value in (
                job.get("created_at"),
                job.get("updated_at"),
                job.get("started_at"),
                job.get("ended_at"),
            )
            if value is not None and str(value).strip()
        ),
        "1970-01-01T00:00:00+00:00",
    )
    if received_at == "1970-01-01T00:00:00+00:00":
        issues.append(
            ImportIssue(
                "warning",
                "legacy_timestamp_missing",
                "Legacy job has no usable timestamp; the deterministic epoch marker is used",
            )
        )
    recorded_at = (
        str(job.get("started_at") or job.get("created_at") or "").strip()
        or None
    )
    timing = {
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "started_at": job.get("started_at"),
        "ended_at": job.get("ended_at"),
        "preprocess_sec": job.get("preprocess_sec"),
        "transcribe_sec": job.get("transcribe_sec"),
        "total_sec": job.get("total_sec"),
    }
    fingerprint_payload = {
        "legacy_job_id": legacy_job_id,
        "legacy_delivery_key": delivery_key,
        "canonical_base": canonical_base,
        "legacy_status": legacy_status,
        "original_name_raw": original_name_raw,
        "original_name_nfc": original_name_nfc,
        "source_sha256": source_sha or None,
        "engine_params": engine_params,
        "timing": timing,
        "delivery": {
            "subject_abbr": delivery.get("subject_abbr"),
            "correction_status": delivery.get("correction_status"),
            "summary_status": delivery.get("summary_status"),
            "correction_txt_sha256": delivery.get("correction_txt_sha256"),
            "correction_json_sha256": delivery.get("correction_json_sha256"),
            "summary_md_sha256": delivery.get("summary_md_sha256"),
        },
        "absent_artifacts": sorted(
            {
                (
                    probe.kind,
                    probe.path.relative_to(legacy_root).as_posix(),
                    "missing",
                )
                for probe in absent_artifacts
            }
        ),
        "artifacts": [
            {
                "kind": artifact.kind,
                "path": artifact.target_relpath,
                "sha256": artifact.sha256,
                "bytes": artifact.bytes,
            }
            for artifact in sorted(artifacts, key=lambda item: item.target_relpath)
        ],
    }
    fingerprint = hashlib.sha256(
        _canonical_json(fingerprint_payload).encode("utf-8")
    ).hexdigest()
    source_key_part = source_sha[:12] if source_sha else fingerprint[:12]
    storage_key = f"rec_lj{legacy_job_id:08d}_{source_key_part}"
    job_key = f"job_lj{legacy_job_id:08d}_{fingerprint[:12]}"

    # Replace the placeholder path after the deterministic job key is known.
    final_artifacts = tuple(
        ArtifactSource(
            kind=artifact.kind,
            source_path=artifact.source_path,
            target_relpath=artifact.target_relpath.replace(
                job_relroot_placeholder,
                f"jobs/{job_key}",
                1,
            ),
            sha256=artifact.sha256,
            bytes=artifact.bytes,
            generated_bytes=artifact.generated_bytes,
            mime_type=artifact.mime_type,
            source_dev=artifact.source_dev,
            source_ino=artifact.source_ino,
        )
        for artifact in artifacts
    )
    source_entry = next(
        (
            item
            for item in final_artifacts
            if item.target_relpath.startswith("source/")
        ),
        None,
    )
    source_available = bool(source_entry and source_entry.kind == "source_copy")
    source_bytes = source_entry.bytes if source_available and source_entry else None
    source_mime = (
        source_entry.mime_type
        if source_available and source_entry
        else mimetypes.guess_type(original_name_nfc)[0]
    )

    title = Path(original_name_nfc).stem.strip() or canonical_base
    return LegacyCandidate(
        legacy_db_path=legacy_db_path,
        legacy_root=legacy_root,
        legacy_job_id=legacy_job_id,
        legacy_delivery_key=delivery_key,
        canonical_base=canonical_base,
        storage_key=storage_key,
        job_key=job_key,
        source_fingerprint=fingerprint,
        original_name_raw=original_name_raw,
        original_name_nfc=original_name_nfc,
        legacy_status=legacy_status,
        v2_status=v2_status,
        title=title,
        recorded_at=recorded_at,
        received_at=received_at,
        source_sha256=source_sha,
        source_bytes=source_bytes,
        source_mime=source_mime,
        source_available=source_available,
        requested_profile=requested_profile,
        requested_profile_version=requested_profile_version,
        engine_name=engine_name,
        engine_version=engine_version,
        engine_params=engine_params,
        context=_context_for(canonical_base, delivery),
        timing=timing,
        artifacts=final_artifacts,
        absent_artifacts=tuple(
            sorted(
                set(absent_artifacts),
                key=lambda probe: (probe.path.as_posix(), probe.kind),
            )
        ),
        issues=tuple(issues),
    )


def _stable_artifact_relpath(
    artifact: ArtifactSource,
    *,
    legacy_job_id: int,
) -> str:
    if artifact.target_relpath.startswith("source/"):
        return artifact.target_relpath
    parts = PurePosixPath(artifact.target_relpath).parts
    if len(parts) < 3 or parts[0] != "jobs":
        raise RuntimeError(
            f"Unexpected candidate artifact path: {artifact.target_relpath}"
        )
    return PurePosixPath(
        "jobs",
        f"legacy_job_{legacy_job_id}",
        *parts[2:],
    ).as_posix()


def _refinalize_candidate(
    candidate: LegacyCandidate,
    *,
    artifacts: Sequence[ArtifactSource],
    additional_issues: Sequence[ImportIssue] = (),
) -> LegacyCandidate:
    context_json = candidate.context.get("context_json")
    context_snapshot = context_json if isinstance(context_json, Mapping) else {}
    fingerprint_payload = {
        "legacy_job_id": candidate.legacy_job_id,
        "legacy_delivery_key": candidate.legacy_delivery_key,
        "canonical_base": candidate.canonical_base,
        "legacy_status": candidate.legacy_status,
        "original_name_raw": candidate.original_name_raw,
        "original_name_nfc": candidate.original_name_nfc,
        "source_sha256": candidate.source_sha256 or None,
        "engine_params": dict(candidate.engine_params),
        "timing": dict(candidate.timing),
        "delivery": {
            "subject_abbr": context_snapshot.get("legacy_subject_abbr"),
            "correction_status": context_snapshot.get("legacy_correction_status"),
            "summary_status": context_snapshot.get("legacy_summary_status"),
            "correction_txt_sha256": context_snapshot.get(
                "legacy_correction_txt_sha256"
            ),
            "correction_json_sha256": context_snapshot.get(
                "legacy_correction_json_sha256"
            ),
            "summary_md_sha256": context_snapshot.get(
                "legacy_summary_md_sha256"
            ),
        },
        "absent_artifacts": sorted(
            (
                probe.kind,
                probe.path.relative_to(candidate.legacy_root).as_posix(),
                "missing",
            )
            for probe in candidate.absent_artifacts
        ),
        "artifacts": [
            {
                "kind": artifact.kind,
                "path": _stable_artifact_relpath(
                    artifact,
                    legacy_job_id=candidate.legacy_job_id,
                ),
                "sha256": artifact.sha256,
                "bytes": artifact.bytes,
            }
            for artifact in sorted(
                artifacts,
                key=lambda item: _stable_artifact_relpath(
                    item,
                    legacy_job_id=candidate.legacy_job_id,
                ),
            )
        ],
    }
    fingerprint = hashlib.sha256(
        _canonical_json(fingerprint_payload).encode("utf-8")
    ).hexdigest()
    job_key = f"job_lj{candidate.legacy_job_id:08d}_{fingerprint[:12]}"
    finalized_artifacts: list[ArtifactSource] = []
    for artifact in artifacts:
        stable_path = _stable_artifact_relpath(
            artifact,
            legacy_job_id=candidate.legacy_job_id,
        )
        if stable_path.startswith("source/"):
            target_relpath = stable_path
        else:
            stable_parts = PurePosixPath(stable_path).parts
            target_relpath = PurePosixPath(
                "jobs",
                job_key,
                *stable_parts[2:],
            ).as_posix()
        finalized_artifacts.append(
            replace(artifact, target_relpath=target_relpath)
        )
    source_key_part = (
        candidate.source_sha256[:12]
        if candidate.source_sha256
        else fingerprint[:12]
    )
    merged_issues = list((*candidate.issues, *additional_issues))
    finalized_status = candidate.v2_status
    artifact_kinds = {artifact.kind for artifact in finalized_artifacts}
    if (
        candidate.legacy_status in {"DONE", "NEEDS_REVIEW"}
        and not {
            "transcript_raw_text",
            "transcript_segments_json",
        }.issubset(artifact_kinds)
    ):
        finalized_status = "needs_review"
        if not any(
            issue.code == "transcript_pair_incomplete"
            for issue in merged_issues
        ):
            merged_issues.append(
                ImportIssue(
                    "warning",
                    "transcript_pair_incomplete",
                    "Completed legacy job has an incomplete transcript pair after ownership filtering",
                )
            )
    return replace(
        candidate,
        storage_key=f"rec_lj{candidate.legacy_job_id:08d}_{source_key_part}",
        job_key=job_key,
        source_fingerprint=fingerprint,
        v2_status=finalized_status,
        artifacts=tuple(finalized_artifacts),
        issues=tuple(merged_issues),
    )


def discover_candidates(
    legacy_db_path: Path | str,
    legacy_root: Path | str,
    *,
    job_ids: Sequence[int] | None = None,
    canonical_bases: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[ImportPlan]:
    root_input = Path(legacy_root).expanduser()
    if root_input.is_symlink():
        raise ValueError(f"Legacy root must not be a symlink: {root_input}")
    root = root_input.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Legacy root is not a directory: {root}")
    if root in {Path("/").resolve(), Path.home().resolve(), repo_root().resolve()}:
        raise ValueError(f"Refusing unsafe legacy root: {root}")
    if limit is not None and (limit < 1 or limit > 10000):
        raise ValueError("limit must be between 1 and 10000")

    conn, resolved_legacy_db, legacy_snapshot = _open_legacy_readonly(
        legacy_db_path
    )
    try:
        all_jobs = [
            _row_dict(row)
            for row in conn.execute("SELECT * FROM jobs ORDER BY id ASC").fetchall()
        ]
        tables = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        deliveries: list[dict[str, Any]] = []
        if "deliveries" in tables:
            deliveries = [
                _row_dict(row)
                for row in conn.execute(
                    "SELECT * FROM deliveries ORDER BY logical_stem ASC"
                ).fetchall()
            ]
        _assert_legacy_database_stable(
            resolved_legacy_db,
            legacy_snapshot,
        )
    finally:
        conn.close()

    jobs_by_id = {int(job["id"]): job for job in all_jobs}
    jobs_by_base: dict[str, list[dict[str, Any]]] = {}
    successful_job_ids_by_base: dict[str, list[int]] = {}
    for job in all_jobs:
        job_id = int(job["id"])
        base = (
            str(job.get("canonical_base") or "").strip()
            or f"legacy-job-{job_id}"
        )
        jobs_by_base.setdefault(base, []).append(job)
        if str(job.get("status") or "").upper() in {"DONE", "NEEDS_REVIEW"}:
            successful_job_ids_by_base.setdefault(base, []).append(job_id)

    delivery_by_job_id: dict[int, dict[str, Any]] = {}
    ownerless_deliveries: dict[str, dict[str, Any]] = {}
    lineage_issues_by_job: dict[int, list[ImportIssue]] = {}
    lineage_blocked_jobs: set[int] = set()
    for delivery in deliveries:
        logical_stem = str(delivery.get("logical_stem") or "").strip()
        source_job_id = delivery.get("source_job_id")
        if source_job_id is None:
            ownerless_deliveries[logical_stem] = delivery
            continue
        try:
            owner_id = int(source_job_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Delivery {logical_stem!r} has an invalid source_job_id"
            ) from exc
        owner_job = jobs_by_id.get(owner_id)
        if owner_job is None:
            raise RuntimeError(
                f"Delivery {logical_stem!r} has a dangling source_job_id {owner_id}"
            )
        owner_base = (
            str(owner_job.get("canonical_base") or "").strip()
            or f"legacy-job-{owner_id}"
        )
        if logical_stem != owner_base:
            raise RuntimeError(
                f"Delivery {logical_stem!r} source_job_id {owner_id} "
                f"does not match job canonical_base {owner_base!r}"
            )
        if owner_id in delivery_by_job_id:
            raise RuntimeError(
                f"Multiple delivery rows claim legacy job {owner_id}"
            )
        delivery_by_job_id[owner_id] = delivery

    for base, delivery in ownerless_deliveries.items():
        matching_jobs = jobs_by_base.get(base, [])
        if len(matching_jobs) == 1:
            owner_id = int(matching_jobs[0]["id"])
            if owner_id in delivery_by_job_id:
                raise RuntimeError(
                    f"Owned and ownerless deliveries both claim legacy job {owner_id}"
                )
            delivery_by_job_id[owner_id] = delivery
        elif len(matching_jobs) > 1:
            for job in matching_jobs:
                job_id = int(job["id"])
                lineage_blocked_jobs.add(job_id)
                lineage_issues_by_job.setdefault(job_id, []).append(
                    ImportIssue(
                        "error",
                        "ambiguous_delivery_lineage",
                        "Ownerless delivery cannot be assigned across duplicate canonical_base jobs",
                    )
                )
        else:
            _assert_unmatched_ownerless_delivery_has_no_artifacts(
                delivery,
                legacy_root=root,
            )

    preferred_owner_by_base: dict[str, int] = {}
    for owner_id, delivery in delivery_by_job_id.items():
        base = str(delivery.get("logical_stem") or "")
        if base and base not in preferred_owner_by_base:
            preferred_owner_by_base[base] = owner_id
    for base, successful_ids in successful_job_ids_by_base.items():
        if base not in preferred_owner_by_base and len(successful_ids) == 1:
            preferred_owner_by_base[base] = successful_ids[0]

    full_candidates: dict[int, LegacyCandidate] = {}
    assigned_delivery_owner_by_base = {
        str(delivery.get("logical_stem") or ""): owner_id
        for owner_id, delivery in delivery_by_job_id.items()
    }
    for job in all_jobs:
        job_id = int(job["id"])
        base = (
            str(job.get("canonical_base") or "").strip()
            or f"legacy-job-{job_id}"
        )
        assigned_delivery_owner = assigned_delivery_owner_by_base.get(base)
        include_delivery_artifacts = (
            job_id not in lineage_blocked_jobs
            and (
                assigned_delivery_owner is None
                or assigned_delivery_owner == job_id
            )
        )
        full_candidates[job_id] = _candidate_from_rows(
            job,
            delivery_by_job_id.get(job_id, {}),
            legacy_db_path=resolved_legacy_db,
            legacy_root=root,
            include_delivery_artifacts=include_delivery_artifacts,
            initial_issues=lineage_issues_by_job.get(job_id, ()),
        )

    claims: dict[
        tuple[str, object],
        set[tuple[int, int]],
    ] = {}
    for job_id, candidate in full_candidates.items():
        for artifact_index, artifact in enumerate(candidate.artifacts):
            if artifact.source_path is None:
                continue
            reference = (job_id, artifact_index)
            claims.setdefault(
                ("path", str(artifact.source_path)),
                set(),
            ).add(reference)
            if artifact.source_dev is not None and artifact.source_ino is not None:
                claims.setdefault(
                    ("inode", (artifact.source_dev, artifact.source_ino)),
                    set(),
                ).add(reference)

    excluded_artifacts: dict[int, set[int]] = {
        job_id: {
            index
            for index, artifact in enumerate(candidate.artifacts)
            if (
                job_id in lineage_blocked_jobs
                and artifact.kind in _DELIVERY_ARTIFACT_KINDS
            )
        }
        for job_id, candidate in full_candidates.items()
    }
    claim_issues_by_job: dict[int, list[ImportIssue]] = {}
    processed_groups: set[frozenset[tuple[int, int]]] = set()
    for references in claims.values():
        if len(references) < 2:
            continue
        frozen_references = frozenset(references)
        if frozen_references in processed_groups:
            continue
        processed_groups.add(frozen_references)
        artifact_kinds = {
            full_candidates[job_id].artifacts[artifact_index].kind
            for job_id, artifact_index in references
        }
        claimant_jobs = {job_id for job_id, _index in references}
        bases = {
            full_candidates[job_id].canonical_base
            for job_id in claimant_jobs
        }
        if artifact_kinds == {"source_copy"}:
            if len(bases) == 1:
                continue
            for job_id in claimant_jobs:
                claim_issues_by_job.setdefault(job_id, []).append(
                    ImportIssue(
                        "error",
                        "cross_base_artifact_share",
                        "A legacy source path or inode is claimed by different canonical bases",
                    )
                )
            continue
        if len(claimant_jobs) == 1:
            job_id = next(iter(claimant_jobs))
            claim_issues_by_job.setdefault(job_id, []).append(
                ImportIssue(
                    "error",
                    "ambiguous_artifact_role",
                    "One legacy file or inode is claimed by multiple artifact roles",
                )
            )
            continue
        if len(artifact_kinds) != 1:
            for job_id in claimant_jobs:
                claim_issues_by_job.setdefault(job_id, []).append(
                    ImportIssue(
                        "error",
                        "ambiguous_artifact_role",
                        "One legacy file or inode is claimed by different artifact roles",
                    )
                )
            continue
        if len(bases) != 1:
            for job_id in claimant_jobs:
                claim_issues_by_job.setdefault(job_id, []).append(
                    ImportIssue(
                        "error",
                        "cross_base_artifact_share",
                        "A legacy artifact path or inode is claimed by different canonical bases",
                    )
                )
            continue

        base = next(iter(bases))
        preferred_owner = preferred_owner_by_base.get(base)
        if preferred_owner not in claimant_jobs:
            for job_id in claimant_jobs:
                claim_issues_by_job.setdefault(job_id, []).append(
                    ImportIssue(
                        "error",
                        "ambiguous_artifact_lineage",
                        "Shared legacy artifact has no unique owner",
                    )
                )
            continue

        for job_id, artifact_index in references:
            if job_id == preferred_owner:
                continue
            excluded_artifacts.setdefault(job_id, set()).add(artifact_index)
            claim_issues_by_job.setdefault(job_id, []).append(
                ImportIssue(
                    "warning",
                    "shared_artifacts_omitted",
                    "Only the artifact actually shared with its owner was omitted",
                )
            )

    finalized_candidates: list[LegacyCandidate] = []
    for job in all_jobs:
        job_id = int(job["id"])
        candidate = full_candidates[job_id]
        excluded_indexes = excluded_artifacts.get(job_id, set())
        kept_artifacts = [
            artifact
            for index, artifact in enumerate(candidate.artifacts)
            if index not in excluded_indexes
        ]
        unique_additional_issues: list[ImportIssue] = []
        seen_issue_keys = {
            (issue.severity, issue.code, issue.message, issue.path)
            for issue in candidate.issues
        }
        for issue in claim_issues_by_job.get(job_id, ()):
            key = (issue.severity, issue.code, issue.message, issue.path)
            if key not in seen_issue_keys:
                seen_issue_keys.add(key)
                unique_additional_issues.append(issue)
        finalized_candidates.append(
            _refinalize_candidate(
                candidate,
                artifacts=kept_artifacts,
                additional_issues=unique_additional_issues,
            )
        )

    selected_job_ids = {int(value) for value in job_ids} if job_ids else None
    selected_bases = (
        {str(value) for value in canonical_bases}
        if canonical_bases
        else None
    )
    selected = [
        candidate
        for candidate in finalized_candidates
        if (
            selected_job_ids is None
            or candidate.legacy_job_id in selected_job_ids
        )
        and (
            selected_bases is None
            or candidate.canonical_base in selected_bases
        )
    ]
    if limit is not None:
        selected = selected[:limit]
    return [
        ImportPlan(
            candidate=candidate,
            legacy_database_snapshot=legacy_snapshot,
        )
        for candidate in selected
    ]


def _validate_records_root(records_root: Path | str) -> Path:
    raw = Path(records_root).expanduser()
    if raw.is_symlink():
        raise ValueError(f"Records root must not be a symlink: {raw}")
    resolved = raw.resolve()
    unsafe = {Path("/").resolve(), Path.home().resolve(), repo_root().resolve()}
    if resolved in unsafe:
        raise ValueError(f"Refusing unsafe records root: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not resolved.is_dir():
        raise ValueError(f"Records root is not a directory: {resolved}")
    return resolved


def _paths_overlap(first: Path, second: Path) -> bool:
    for child, parent in ((first, second), (second, first)):
        try:
            child.relative_to(parent)
        except ValueError:
            continue
        return True
    return False


@contextmanager
def _exclusive_records_root_lock(root: Path) -> Iterator[None]:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            unsupported = {
                errno.EINVAL,
                getattr(errno, "ENOTSUP", errno.EINVAL),
                getattr(errno, "EOPNOTSUPP", errno.EINVAL),
            }
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(descriptor)


def _fsync_tree_directories(root: Path) -> None:
    directories: list[Path] = []
    for current_root, dirnames, _filenames in os.walk(root, topdown=False):
        current_path = Path(current_root)
        if current_path.is_symlink():
            raise RuntimeError(f"Refusing to fsync a symlinked staging directory: {current_path}")
        for dirname in dirnames:
            child = current_path / dirname
            if child.is_symlink():
                raise RuntimeError(f"Refusing to fsync a symlinked staging directory: {child}")
        directories.append(current_path)
    for directory in directories:
        _fsync_directory(directory)


def _assert_candidate_fresh(candidate: LegacyCandidate) -> None:
    for probe in candidate.absent_artifacts:
        try:
            probe.path.resolve(strict=False).relative_to(candidate.legacy_root)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Legacy artifact absence probe escaped its root: {probe.path}"
            ) from exc
        if (
            _is_symlink_component(
                probe.path.absolute(),
                candidate.legacy_root,
            )
            or os.path.lexists(probe.path)
        ):
            raise RuntimeError(
                "Previously missing legacy artifact reappeared or became unsafe; "
                f"rediscover the plan: {probe.path}"
            )

    for artifact in candidate.artifacts:
        if artifact.source_path is None:
            continue
        source_path = artifact.source_path
        if _is_symlink_component(source_path.absolute(), candidate.legacy_root):
            raise RuntimeError(
                f"Planned legacy artifact became symlinked: {source_path}"
            )
        try:
            resolved = source_path.resolve(strict=True)
            resolved.relative_to(candidate.legacy_root)
            current = resolved.stat()
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise RuntimeError(
                f"Planned legacy artifact is no longer safe: {source_path}"
            ) from exc
        if (
            not stat_module.S_ISREG(current.st_mode)
            or int(current.st_dev) != artifact.source_dev
            or int(current.st_ino) != artifact.source_ino
            or int(current.st_size) != artifact.bytes
            or utils.compute_sha256(resolved) != artifact.sha256
        ):
            raise RuntimeError(
                "Planned legacy artifact changed after discovery; "
                f"rediscover the plan: {source_path}"
            )


def _assert_plan_fresh(plan: ImportPlan) -> None:
    _assert_legacy_database_stable(
        plan.candidate.legacy_db_path,
        plan.legacy_database_snapshot,
    )
    _assert_candidate_fresh(plan.candidate)


def _copy_no_overwrite(destination: Path, *, expected: ArtifactSource) -> None:
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        destination_flags |= os.O_NOFOLLOW

    source_fd: int | None = None
    destination_fd: int | None = None
    try:
        if expected.generated_bytes is not None:
            chunks: Iterable[bytes] = (expected.generated_bytes,)
        else:
            if expected.source_path is None:
                raise RuntimeError(
                    f"Artifact has neither source_path nor generated_bytes: {expected.target_relpath}"
                )
            source_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                source_flags |= os.O_NOFOLLOW
            source_fd = os.open(expected.source_path, source_flags)
            source_stat = os.fstat(source_fd)
            if (
                not stat_module.S_ISREG(source_stat.st_mode)
                or int(source_stat.st_dev) != expected.source_dev
                or int(source_stat.st_ino) != expected.source_ino
                or int(source_stat.st_size) != expected.bytes
            ):
                raise RuntimeError(
                    "Legacy artifact identity changed before copy: "
                    f"{expected.source_path}"
                )

            def source_chunks() -> Iterable[bytes]:
                deadlock_retries = 0
                while True:
                    try:
                        chunk = os.read(source_fd, 1024 * 1024)
                    except OSError as exc:
                        if exc.errno == errno.EDEADLK:
                            if deadlock_retries >= 4:
                                raise RuntimeError(
                                    "Legacy artifact read remained deadlocked after "
                                    f"bounded retries: {expected.source_path}"
                                ) from exc
                            time.sleep(min(0.05 * (2**deadlock_retries), 0.5))
                            deadlock_retries += 1
                            continue
                        raise
                    if not chunk:
                        return
                    yield chunk

            chunks = source_chunks()

        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination_fd = os.open(destination, destination_flags, 0o600)
        for chunk in chunks:
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        os.fsync(destination_fd)
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if source_fd is not None:
            os.close(source_fd)

    copied_sha = utils.compute_sha256(destination)
    copied_bytes = destination.stat().st_size
    if copied_sha != expected.sha256 or copied_bytes != expected.bytes:
        raise RuntimeError(
            f"Copied artifact verification failed: {expected.target_relpath}"
        )


def _manifest_for(candidate: LegacyCandidate) -> dict[str, Any]:
    source_artifact = next(
        artifact
        for artifact in candidate.artifacts
        if artifact.target_relpath.startswith("source/")
    )
    context = dict(candidate.context)
    job_artifact_paths = [
        artifact.target_relpath
        for artifact in candidate.artifacts
        if not artifact.target_relpath.startswith("source/")
    ]
    queued_at = (
        candidate.timing.get("created_at")
        or candidate.received_at
    )
    started_at = candidate.timing.get("started_at")
    finished_at = candidate.timing.get("ended_at")
    return build_manifest(
        storage_key=candidate.storage_key,
        generated_at=datetime.now(timezone.utc).isoformat(),
        original_name_raw=candidate.original_name_raw,
        original_name_nfc=candidate.original_name_nfc,
        recording_created_at=candidate.received_at,
        source={
            "path": source_artifact.target_relpath,
            "availability": "available" if candidate.source_available else "missing",
            "sha256": candidate.source_sha256 or None,
            "bytes": candidate.source_bytes,
            "mime_type": candidate.source_mime,
            "marker_sha256": (
                source_artifact.sha256 if not candidate.source_available else None
            ),
        },
        title={
            "value": candidate.title,
            "source": "legacy_import",
        },
        context={
            "type": context.get("context_type"),
            "label": context.get("label"),
            "semester": context.get("semester"),
            "course_name": context.get("course_name"),
            "course_code": context.get("course_code"),
            "session_date": context.get("session_date"),
            "period_label": context.get("period_label"),
            "source": "legacy_import",
        },
        jobs=[
            {
                "job_key": candidate.job_key,
                "status": candidate.v2_status,
                "requested_profile": candidate.requested_profile,
                "requested_profile_version": candidate.requested_profile_version,
                "queued_at": queued_at,
                "started_at": started_at,
                "finished_at": finished_at,
                "engine": {
                    "name": candidate.engine_name,
                    "version": candidate.engine_version,
                    "started_at": started_at or candidate.received_at,
                    "finished_at": finished_at,
                    "stderr_relpath": None,
                    "log_relpath": None,
                },
                "artifact_paths": job_artifact_paths,
            }
        ],
        artifacts=[artifact.manifest_entry() for artifact in candidate.artifacts],
        legacy={
            "kind": "job",
            "job_id": candidate.legacy_job_id,
            "delivery_key": candidate.legacy_delivery_key,
            "canonical_base": candidate.canonical_base,
            "status": candidate.legacy_status,
            "source_fingerprint": candidate.source_fingerprint,
        },
    )


def _insert_candidate(
    conn: sqlite3.Connection,
    candidate: LegacyCandidate,
    *,
    manifest: Mapping[str, Any],
) -> tuple[int, int]:
    source_artifact = next(
        artifact
        for artifact in candidate.artifacts
        if artifact.target_relpath.startswith("source/")
    )
    cursor = conn.execute(
        """
        INSERT INTO recordings(
            storage_key,
            original_name_raw,
            original_name_nfc,
            source_relpath,
            manifest_relpath,
            ingest_sha256,
            ingest_bytes,
            source_mime,
            source_state,
            source_error_code,
            recorded_at,
            recorded_at_source,
            received_at,
            created_at
        )
        VALUES (?, ?, ?, ?, 'manifest.json', ?, ?, ?, ?, ?, ?, 'legacy_import', ?, ?)
        """,
        (
            candidate.storage_key,
            candidate.original_name_raw,
            candidate.original_name_nfc,
            source_artifact.target_relpath,
            candidate.source_sha256 or None,
            candidate.source_bytes,
            candidate.source_mime,
            "available" if candidate.source_available else "missing",
            None if candidate.source_available else "LEGACY_SOURCE_MISSING",
            candidate.recorded_at,
            candidate.received_at,
            candidate.received_at,
        ),
    )
    recording_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO recording_titles(
            recording_id, title, title_source, locale, confidence, is_current
        )
        VALUES (?, ?, 'legacy_import', 'ko-KR', NULL, 1)
        """,
        (recording_id, candidate.title),
    )

    context = dict(candidate.context)
    conn.execute(
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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'legacy_import', 1)
        """,
        (
            recording_id,
            context.get("context_type") or "general",
            context.get("label"),
            context.get("semester"),
            context.get("course_name"),
            context.get("course_code"),
            context.get("session_date"),
            context.get("period_label"),
            context.get("period_index"),
            _canonical_json(context.get("context_json") or {}),
        ),
    )

    manifest_snapshot = {
        "schema_version": manifest["schema_version"],
        "storage_key": candidate.storage_key,
        "job_key": candidate.job_key,
        "source_fingerprint": candidate.source_fingerprint,
    }
    cursor = conn.execute(
        """
        INSERT INTO transcription_jobs(
            recording_id,
            job_key,
            job_relpath,
            requested_profile,
            requested_profile_version,
            status,
            progress,
            config_json,
            manifest_json,
            error_code,
            error_message,
            is_current,
            queued_at,
            started_at,
            finished_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        """,
        (
            recording_id,
            candidate.job_key,
            f"jobs/{candidate.job_key}",
            candidate.requested_profile,
            candidate.requested_profile_version,
            candidate.v2_status,
            100 if candidate.v2_status in {"done", "needs_review", "error", "canceled"} else 0,
            _canonical_json(dict(candidate.engine_params)),
            _canonical_json(manifest_snapshot),
            "LEGACY_IMPORT_ERROR" if candidate.v2_status == "error" else None,
            "Imported legacy job requires inspection" if candidate.v2_status == "error" else None,
            candidate.timing.get("created_at") or candidate.received_at,
            candidate.timing.get("started_at"),
            candidate.timing.get("ended_at"),
        ),
    )
    job_id = int(cursor.lastrowid)

    engine_status = (
        "succeeded"
        if candidate.v2_status in {"done", "needs_review"}
        else "failed"
        if candidate.v2_status == "error"
        else "superseded"
    )
    cursor = conn.execute(
        """
        INSERT INTO engine_runs(
            job_id,
            recording_id,
            engine_name,
            engine_version,
            provider,
            status,
            is_selected,
            params_json,
            metrics_json,
            started_at,
            finished_at
        )
        VALUES (?, ?, ?, ?, 'legacy_import', ?, 1, ?, ?, ?, ?)
        """,
        (
            job_id,
            recording_id,
            candidate.engine_name,
            candidate.engine_version,
            engine_status,
            _canonical_json(dict(candidate.engine_params)),
            _canonical_json(dict(candidate.timing)),
            candidate.timing.get("started_at") or candidate.received_at,
            candidate.timing.get("ended_at"),
        ),
    )
    engine_run_id = int(cursor.lastrowid)

    artifact_ids: list[int] = []
    for artifact in candidate.artifacts:
        use_engine_run = (
            engine_run_id
            if artifact.kind
            in {"transcript_raw_text", "transcript_segments_json", "quality_scorecard"}
            else None
        )
        cursor = conn.execute(
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
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, 1)
            """,
            (
                recording_id,
                job_id,
                use_engine_run,
                artifact.kind,
                artifact.target_relpath,
                artifact.sha256,
                artifact.bytes,
                artifact.mime_type,
            ),
        )
        artifact_ids.append(int(cursor.lastrowid))

    if candidate.v2_status == "needs_review" or candidate.issues:
        severity = "high" if any(issue.blocking for issue in candidate.issues) else "medium"
        conn.execute(
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
            VALUES (?, ?, NULL, 'open', ?, 'legacy_import_review', ?)
            """,
            (
                recording_id,
                job_id,
                severity,
                _canonical_json(
                    {
                        "legacy_job_id": candidate.legacy_job_id,
                        "issues": [issue.as_dict() for issue in candidate.issues],
                    }
                ),
            ),
        )

    conn.execute(
        """
        INSERT INTO legacy_import_map(
            legacy_kind,
            legacy_key,
            recording_id,
            source_fingerprint,
            legacy_snapshot_json
        )
        VALUES ('job', ?, ?, ?, ?)
        """,
        (
            str(candidate.legacy_job_id),
            recording_id,
            candidate.source_fingerprint,
            _canonical_json(
                {
                    "canonical_base": candidate.canonical_base,
                    "status": candidate.legacy_status,
                }
            ),
        ),
    )
    if candidate.legacy_delivery_key:
        conn.execute(
            """
            INSERT INTO legacy_import_map(
                legacy_kind,
                legacy_key,
                recording_id,
                source_fingerprint,
                legacy_snapshot_json
            )
            VALUES ('delivery', ?, ?, ?, ?)
            """,
            (
                candidate.legacy_delivery_key,
                recording_id,
                candidate.source_fingerprint,
                _canonical_json({"logical_stem": candidate.legacy_delivery_key}),
            ),
        )
    return recording_id, job_id


def _require_exact_row(
    rows: Sequence[sqlite3.Row],
    expected: Mapping[str, Any],
    *,
    label: str,
) -> sqlite3.Row:
    if len(rows) != 1:
        raise RuntimeError(
            f"Existing storage v2 {label} count mismatch: expected 1, got {len(rows)}"
        )
    row = rows[0]
    mismatched = [
        field
        for field, expected_value in expected.items()
        if row[field] != expected_value
    ]
    if mismatched:
        raise RuntimeError(
            f"Existing storage v2 {label} metadata mismatch: "
            + ", ".join(sorted(mismatched))
        )
    return row


def _validate_existing_database_state(
    conn: sqlite3.Connection,
    candidate: LegacyCandidate,
    *,
    recording_id: int,
    manifest: Mapping[str, Any],
) -> int:
    source_artifact = next(
        artifact
        for artifact in candidate.artifacts
        if artifact.target_relpath.startswith("source/")
    )
    recording = _require_exact_row(
        conn.execute(
            "SELECT * FROM recordings WHERE id = ? AND archived_at IS NULL",
            (recording_id,),
        ).fetchall(),
        {
            "storage_key": candidate.storage_key,
            "original_name_raw": candidate.original_name_raw,
            "original_name_nfc": candidate.original_name_nfc,
            "source_relpath": source_artifact.target_relpath,
            "manifest_relpath": "manifest.json",
            "ingest_sha256": candidate.source_sha256 or None,
            "ingest_bytes": candidate.source_bytes,
            "source_mime": candidate.source_mime,
            "source_state": (
                "available" if candidate.source_available else "missing"
            ),
            "source_error_code": (
                None if candidate.source_available else "LEGACY_SOURCE_MISSING"
            ),
            "recorded_at": candidate.recorded_at,
            "recorded_at_source": "legacy_import",
            "received_at": candidate.received_at,
            "created_at": candidate.received_at,
        },
        label="recording",
    )
    if int(recording["id"]) != recording_id:
        raise RuntimeError("Existing storage v2 recording identity mismatch")

    _require_exact_row(
        conn.execute(
            """
            SELECT *
            FROM recording_titles
            WHERE recording_id = ? AND is_current = 1
            """,
            (recording_id,),
        ).fetchall(),
        {
            "title": candidate.title,
            "title_source": "legacy_import",
            "locale": "ko-KR",
            "confidence": None,
        },
        label="current title",
    )

    context = dict(candidate.context)
    _require_exact_row(
        conn.execute(
            """
            SELECT *
            FROM recording_contexts
            WHERE recording_id = ? AND is_selected = 1
            """,
            (recording_id,),
        ).fetchall(),
        {
            "context_type": context.get("context_type") or "general",
            "label": context.get("label"),
            "semester": context.get("semester"),
            "course_name": context.get("course_name"),
            "course_code": context.get("course_code"),
            "session_date": context.get("session_date"),
            "period_label": context.get("period_label"),
            "period_index": context.get("period_index"),
            "context_json": _canonical_json(
                context.get("context_json") or {}
            ),
            "source": "legacy_import",
        },
        label="selected context",
    )

    manifest_snapshot = {
        "schema_version": manifest["schema_version"],
        "storage_key": candidate.storage_key,
        "job_key": candidate.job_key,
        "source_fingerprint": candidate.source_fingerprint,
    }
    job = _require_exact_row(
        conn.execute(
            """
            SELECT *
            FROM transcription_jobs
            WHERE recording_id = ?
              AND is_current = 1
              AND archived_at IS NULL
            """,
            (recording_id,),
        ).fetchall(),
        {
            "job_key": candidate.job_key,
            "job_relpath": f"jobs/{candidate.job_key}",
            "requested_profile": candidate.requested_profile,
            "requested_profile_version": candidate.requested_profile_version,
            "status": candidate.v2_status,
            "progress": (
                100
                if candidate.v2_status
                in {"done", "needs_review", "error", "canceled"}
                else 0
            ),
            "config_json": _canonical_json(dict(candidate.engine_params)),
            "manifest_json": _canonical_json(manifest_snapshot),
            "error_code": (
                "LEGACY_IMPORT_ERROR"
                if candidate.v2_status == "error"
                else None
            ),
            "error_message": (
                "Imported legacy job requires inspection"
                if candidate.v2_status == "error"
                else None
            ),
            "queued_at": (
                candidate.timing.get("created_at")
                or candidate.received_at
            ),
            "started_at": candidate.timing.get("started_at"),
            "finished_at": candidate.timing.get("ended_at"),
            "archived_at": None,
        },
        label="current job",
    )
    job_id = int(job["id"])

    engine_rows = conn.execute(
        """
        SELECT *
        FROM engine_runs
        WHERE job_id = ? AND archived_at IS NULL
        """,
        (job_id,),
    ).fetchall()
    engine_status = (
        "succeeded"
        if candidate.v2_status in {"done", "needs_review"}
        else "failed"
        if candidate.v2_status == "error"
        else "superseded"
    )
    engine = _require_exact_row(
        engine_rows,
        {
            "recording_id": recording_id,
            "engine_name": candidate.engine_name,
            "engine_version": candidate.engine_version,
            "provider": "legacy_import",
            "status": engine_status,
            "is_selected": 1,
            "params_json": _canonical_json(dict(candidate.engine_params)),
            "metrics_json": _canonical_json(dict(candidate.timing)),
            "stderr_relpath": None,
            "log_relpath": None,
            "started_at": (
                candidate.timing.get("started_at")
                or candidate.received_at
            ),
            "finished_at": candidate.timing.get("ended_at"),
            "archived_at": None,
        },
        label="selected engine run",
    )
    engine_run_id = int(engine["id"])

    actual_artifacts = {
        (
            str(row["artifact_kind"]),
            str(row["path_rel"]),
            row["content_sha256"],
            row["bytes"],
            row["mime_type"],
            int(row["revision"]),
            int(row["is_latest"]),
            row["engine_run_id"],
            int(row["job_id"]),
        )
        for row in conn.execute(
            """
            SELECT *
            FROM artifacts
            WHERE recording_id = ? AND archived_at IS NULL
            """,
            (recording_id,),
        ).fetchall()
    }
    expected_artifacts = {
        (
            artifact.kind,
            artifact.target_relpath,
            artifact.sha256,
            artifact.bytes,
            artifact.mime_type,
            1,
            1,
            (
                engine_run_id
                if artifact.kind
                in {
                    "transcript_raw_text",
                    "transcript_segments_json",
                    "quality_scorecard",
                }
                else None
            ),
            job_id,
        )
        for artifact in candidate.artifacts
    }
    if actual_artifacts != expected_artifacts:
        raise RuntimeError(
            "Existing storage v2 artifact index or ownership mismatch"
        )

    review_rows = conn.execute(
        "SELECT * FROM review_items WHERE recording_id = ?",
        (recording_id,),
    ).fetchall()
    review_expected = (
        candidate.v2_status == "needs_review" or bool(candidate.issues)
    )
    if review_expected:
        severity = (
            "high"
            if any(issue.blocking for issue in candidate.issues)
            else "medium"
        )
        _require_exact_row(
            review_rows,
            {
                "job_id": job_id,
                "artifact_id": None,
                "status": "open",
                "severity": severity,
                "reason_code": "legacy_import_review",
                "detail_json": _canonical_json(
                    {
                        "legacy_job_id": candidate.legacy_job_id,
                        "issues": [
                            issue.as_dict()
                            for issue in candidate.issues
                        ],
                    }
                ),
                "resolved_at": None,
            },
            label="legacy import review",
        )
    elif review_rows:
        raise RuntimeError(
            "Existing storage v2 review state exists when none is expected"
        )

    actual_maps = {
        (
            str(row["legacy_kind"]),
            str(row["legacy_key"]),
            str(row["source_fingerprint"]),
            str(row["legacy_snapshot_json"] or ""),
        )
        for row in conn.execute(
            "SELECT * FROM legacy_import_map WHERE recording_id = ?",
            (recording_id,),
        ).fetchall()
    }
    expected_maps = {
        (
            "job",
            str(candidate.legacy_job_id),
            candidate.source_fingerprint,
            _canonical_json(
                {
                    "canonical_base": candidate.canonical_base,
                    "status": candidate.legacy_status,
                }
            ),
        )
    }
    if candidate.legacy_delivery_key:
        expected_maps.add(
            (
                "delivery",
                candidate.legacy_delivery_key,
                candidate.source_fingerprint,
                _canonical_json(
                    {"logical_stem": candidate.legacy_delivery_key}
                ),
            )
        )
    if actual_maps != expected_maps:
        raise RuntimeError(
            "Existing storage v2 legacy import-map provenance mismatch"
        )
    return job_id


def _validate_exact_record_tree(
    record_root: Path,
    *,
    expected_files: set[str],
) -> None:
    expected_directories = {"."}
    for relpath in expected_files:
        parent = PurePosixPath(relpath).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent

    actual_files: set[str] = set()
    actual_directories = {"."}
    claimed_file_inodes: dict[tuple[int, int], str] = {}
    pending = [record_root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                entry_path = Path(entry.path)
                relpath = entry_path.relative_to(record_root).as_posix()
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise RuntimeError(
                        f"Existing recording tree entry is unreadable: {relpath}"
                    ) from exc
                if stat_module.S_ISLNK(entry_stat.st_mode):
                    raise RuntimeError(
                        f"Existing recording tree contains a symlink: {relpath}"
                    )
                if stat_module.S_ISDIR(entry_stat.st_mode):
                    actual_directories.add(relpath)
                    pending.append(entry_path)
                elif stat_module.S_ISREG(entry_stat.st_mode):
                    if entry_stat.st_nlink != 1:
                        raise RuntimeError(
                            "Existing recording tree contains a hard-linked "
                            f"file: {relpath}"
                        )
                    identity = (
                        int(entry_stat.st_dev),
                        int(entry_stat.st_ino),
                    )
                    previous = claimed_file_inodes.get(identity)
                    if previous is not None:
                        raise RuntimeError(
                            "Existing recording tree contains duplicate inode "
                            f"claims: {previous}, {relpath}"
                        )
                    claimed_file_inodes[identity] = relpath
                    actual_files.add(relpath)
                else:
                    raise RuntimeError(
                        f"Existing recording tree contains a special entry: {relpath}"
                    )

    if actual_files != expected_files:
        raise RuntimeError(
            "Existing recording tree contains missing or unindexed files"
        )
    if actual_directories != expected_directories:
        raise RuntimeError(
            "Existing recording tree contains unexpected directories"
        )


def _open_single_link_record_file(
    path: Path,
    *,
    label: str,
) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"{label} cannot be opened safely: {path}") from exc
    try:
        opened_stat = os.fstat(descriptor)
        current_stat = path.lstat()
        if (
            not stat_module.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_nlink != 1
            or not stat_module.S_ISREG(current_stat.st_mode)
            or current_stat.st_nlink != 1
            or (opened_stat.st_dev, opened_stat.st_ino)
            != (current_stat.st_dev, current_stat.st_ino)
        ):
            raise RuntimeError(
                f"{label} is hard-linked, non-regular, or changed while opening: {path}"
            )
        return descriptor, opened_stat
    except BaseException:
        os.close(descriptor)
        raise


def _require_opened_record_file_stable(
    path: Path,
    descriptor: int,
    baseline: os.stat_result,
    *,
    label: str,
) -> os.stat_result:
    after = os.fstat(descriptor)
    try:
        current = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{label} changed while being read: {path}") from exc
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if (
        any(getattr(after, field) != getattr(baseline, field) for field in stable_fields)
        or not stat_module.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or (current.st_dev, current.st_ino)
        != (after.st_dev, after.st_ino)
    ):
        raise RuntimeError(f"{label} changed while being read: {path}")
    return after


def _existing_record_manifest(
    candidate: LegacyCandidate,
    final_dir: Path,
) -> dict[str, Any]:
    if final_dir.is_symlink() or not final_dir.is_dir():
        raise FileExistsError(f"Unsafe recording path already exists: {final_dir}")
    manifest_path = final_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("Existing recording directory has no safe manifest")
    manifest_fd, manifest_stat = _open_single_link_record_file(
        manifest_path,
        label="Existing recording manifest",
    )
    try:
        if manifest_stat.st_size > 2 * 1024 * 1024:
            raise RuntimeError(
                "Existing recording manifest is unexpectedly large"
            )
        with os.fdopen(manifest_fd, "rb", closefd=False) as manifest_handle:
            manifest_bytes = manifest_handle.read(2 * 1024 * 1024 + 1)
        _require_opened_record_file_stable(
            manifest_path,
            manifest_fd,
            manifest_stat,
            label="Existing recording manifest",
        )
        if len(manifest_bytes) > 2 * 1024 * 1024:
            raise RuntimeError(
                "Existing recording manifest is unexpectedly large"
            )
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Existing recording manifest cannot be parsed: {exc}") from exc
    finally:
        os.close(manifest_fd)
    if not isinstance(manifest, dict):
        raise RuntimeError("Existing recording manifest root is not an object")
    try:
        validate_manifest(manifest)
    except ValueError as exc:
        raise RuntimeError(
            f"Existing recording manifest is invalid: {exc}"
        ) from exc
    if manifest.get("storage_key") != candidate.storage_key:
        raise RuntimeError("Existing recording manifest storage_key mismatch")
    legacy = manifest.get("legacy")
    if not isinstance(legacy, dict) or legacy.get("source_fingerprint") != candidate.source_fingerprint:
        raise RuntimeError("Existing recording manifest fingerprint mismatch")

    expected_manifest = _manifest_for(candidate)
    if set(manifest) != set(expected_manifest):
        raise RuntimeError(
            "Existing recording manifest metadata mismatch: top-level keys"
        )
    for field, expected_value in expected_manifest.items():
        if field == "generated_at":
            continue
        if manifest.get(field) != expected_value:
            raise RuntimeError(
                f"Existing recording manifest metadata mismatch: {field}"
            )

    manifest_artifacts = manifest.get("artifacts")
    assert isinstance(manifest_artifacts, list)
    indexed = {
        str(item["path"]): item
        for item in manifest_artifacts
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    expected_paths = {artifact.target_relpath for artifact in candidate.artifacts}
    if set(indexed) != expected_paths:
        raise RuntimeError("Existing recording manifest artifact index mismatch")
    _validate_exact_record_tree(
        final_dir,
        expected_files={"manifest.json", *expected_paths},
    )
    for artifact in candidate.artifacts:
        path = final_dir / artifact.target_relpath
        current = final_dir
        for part in Path(artifact.target_relpath).parts:
            current = current / part
            if current.is_symlink():
                raise RuntimeError(
                    f"Existing recording artifact uses a symlink: {artifact.target_relpath}"
                )
        if not path.is_file():
            raise RuntimeError(
                f"Existing recording artifact is missing: {artifact.target_relpath}"
            )
        artifact_fd, artifact_stat = _open_single_link_record_file(
            path,
            label=f"Existing recording artifact {artifact.target_relpath}",
        )
        try:
            digest = hashlib.sha256()
            copied_bytes = 0
            while True:
                chunk = os.read(artifact_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                copied_bytes += len(chunk)
            _require_opened_record_file_stable(
                path,
                artifact_fd,
                artifact_stat,
                label=f"Existing recording artifact {artifact.target_relpath}",
            )
        finally:
            os.close(artifact_fd)
        if copied_bytes != artifact.bytes:
            raise RuntimeError(
                f"Existing recording artifact size mismatch: {artifact.target_relpath}"
            )
        if digest.hexdigest() != artifact.sha256:
            raise RuntimeError(
                f"Existing recording artifact hash mismatch: {artifact.target_relpath}"
            )
    return manifest


def _validate_database_root_binding(
    conn: sqlite3.Connection,
    root: Path,
    *,
    recovery_storage_key: str,
) -> None:
    rows = conn.execute(
        """
        SELECT id, storage_key, manifest_relpath
        FROM recordings
        WHERE archived_at IS NULL
        ORDER BY id ASC
        """
    ).fetchall()
    database_storage_keys = {
        str(row["storage_key"])
        for row in rows
    }
    allowed_record_roots = {
        *database_storage_keys,
        recovery_storage_key,
    }
    allowed_top_level = {
        ".locks",
        ".staging",
        *allowed_record_roots,
    }
    with os.scandir(root) as entries:
        for entry in entries:
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(
                    f"Records root entry is unreadable: {entry.name}"
                ) from exc
            if entry.name not in allowed_top_level:
                raise RuntimeError(
                    f"Unexpected records root entry before import: {entry.name}"
                )
            if stat_module.S_ISLNK(entry_stat.st_mode):
                raise RuntimeError(
                    f"Records root entry must not be symlinked: {entry.name}"
                )
            if entry.name in allowed_record_roots and not stat_module.S_ISDIR(
                entry_stat.st_mode
            ):
                raise RuntimeError(
                    f"Recording root entry is not a directory: {entry.name}"
                )

    staging_root = root / ".staging"
    if staging_root.is_symlink() or not staging_root.is_dir():
        raise RuntimeError("Storage v2 staging directory is unsafe")
    stale_staging = list(staging_root.iterdir())
    if stale_staging:
        raise RuntimeError(
            "Storage v2 staging directory is not empty before import"
        )

    locks_root = root / ".locks"
    if locks_root.is_symlink() or not locks_root.is_dir():
        raise RuntimeError("Storage v2 locks directory is unsafe")
    allowed_locks = {
        f"{storage_key}.lock"
        for storage_key in allowed_record_roots
    }
    with os.scandir(locks_root) as entries:
        for entry in entries:
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(
                    f"Storage v2 lock entry is unreadable: {entry.name}"
                ) from exc
            if (
                entry.name not in allowed_locks
                or not stat_module.S_ISREG(entry_stat.st_mode)
                or entry_stat.st_nlink != 1
                or entry_stat.st_size != 0
            ):
                raise RuntimeError(
                    f"Unexpected or unsafe storage v2 lock entry: {entry.name}"
                )

    for row in rows:
        recording_id = int(row["id"])
        storage_key = str(row["storage_key"])
        manifest_relpath = str(row["manifest_relpath"])
        try:
            validate_storage_key(storage_key)
            validate_relative_path(
                manifest_relpath,
                field="manifest_relpath",
            )
        except ValueError as exc:
            raise RuntimeError(
                f"Storage v2 recording {recording_id} has unsafe root binding"
            ) from exc
        record_root = root / storage_key
        if record_root.is_symlink() or not record_root.is_dir():
            raise RuntimeError(
                "Storage v2 database is already bound to a different or "
                f"incomplete records root: {storage_key}"
            )
        manifest_path = record_root / manifest_relpath
        current = record_root
        for part in PurePosixPath(manifest_relpath).parts:
            current = current / part
            if current.is_symlink():
                raise RuntimeError(
                    f"Storage v2 recording manifest is symlinked: {storage_key}"
                )
        try:
            resolved_manifest = manifest_path.resolve(strict=True)
            resolved_manifest.relative_to(record_root)
            if not resolved_manifest.is_file():
                raise RuntimeError("manifest is not a regular file")
            if resolved_manifest.stat().st_size > 2 * 1024 * 1024:
                raise RuntimeError("manifest exceeds metadata limit")
            payload = json.loads(
                resolved_manifest.read_text(encoding="utf-8")
            )
            if not isinstance(payload, dict):
                raise RuntimeError("manifest root is not an object")
            validate_manifest(payload)
        except (OSError, ValueError, RuntimeError) as exc:
            raise RuntimeError(
                "Storage v2 database has an invalid manifest binding for "
                f"{storage_key}"
            ) from exc
        if payload.get("storage_key") != storage_key:
            raise RuntimeError(
                f"Storage v2 manifest storage_key mismatch for {storage_key}"
            )


def import_candidate(
    plan: ImportPlan,
    *,
    target_db_path: Path | str,
    records_root: Path | str,
) -> ImportResult:
    candidate = plan.candidate
    if not plan.can_apply:
        codes = ", ".join(issue.code for issue in candidate.issues if issue.blocking)
        raise RuntimeError(f"Import plan has blocking issues: {codes}")
    if not any(
        artifact.target_relpath.startswith("source/")
        for artifact in candidate.artifacts
    ):
        raise RuntimeError("Import plan has no source record")
    _assert_plan_fresh(plan)
    if Path(target_db_path).expanduser().resolve() == candidate.legacy_db_path:
        raise ValueError("Storage v2 DB must be separate from the legacy DB")

    raw_records_root = Path(records_root).expanduser()
    if raw_records_root.is_symlink():
        raise ValueError(f"Records root must not be a symlink: {raw_records_root}")
    prospective_root = raw_records_root.resolve()
    if _paths_overlap(prospective_root, candidate.legacy_root):
        raise ValueError("Records root must be separate from the legacy root")

    raw_target_db = Path(target_db_path).expanduser()
    if raw_target_db.is_symlink():
        raise ValueError(f"Storage v2 DB must not be a symlink: {raw_target_db}")
    if os.path.lexists(raw_target_db):
        if raw_target_db.stat().st_nlink != 1:
            raise ValueError(
                f"Storage v2 DB must not be a hard-linked file: {raw_target_db}"
            )
        if os.path.samefile(raw_target_db, candidate.legacy_db_path):
            raise ValueError("Storage v2 DB must be separate from the legacy DB")
    target_db_resolved = raw_target_db.resolve()
    try:
        target_db_resolved.relative_to(candidate.legacy_root)
    except ValueError:
        pass
    else:
        raise ValueError("Storage v2 DB must be outside the legacy root")
    try:
        target_db_resolved.relative_to(prospective_root)
    except ValueError:
        pass
    else:
        raise ValueError("Storage v2 DB must be outside the records root")

    root = _validate_records_root(raw_records_root)
    if _paths_overlap(root, candidate.legacy_root):
        raise ValueError("Records root must be separate from the legacy root")
    target_db_resolved = raw_target_db.resolve()
    try:
        target_db_resolved.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("Storage v2 DB must be outside the records root")
    _fsync_directory(root.parent)
    with _exclusive_records_root_lock(root):
        _assert_plan_fresh(plan)
        return _import_candidate_locked(
            plan,
            target_db_path=target_db_path,
            root=root,
        )


def _import_candidate_locked(
    plan: ImportPlan,
    *,
    target_db_path: Path | str,
    root: Path,
) -> ImportResult:
    candidate = plan.candidate
    final_dir = root / candidate.storage_key
    manifest_path = final_dir / "manifest.json"
    locks_dir = root / ".locks"
    staging_root = root / ".staging"
    for internal_dir in (locks_dir, staging_root):
        if os.path.lexists(internal_dir):
            if internal_dir.is_symlink() or not internal_dir.is_dir():
                raise RuntimeError(f"Unsafe storage v2 internal directory: {internal_dir}")
        else:
            internal_dir.mkdir(mode=0o700)
    lock_path = locks_dir / f"{candidate.storage_key}.lock"

    lock_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    lock_fd = os.open(lock_path, lock_flags, 0o600)
    with os.fdopen(lock_fd, "a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        conn = apply_migration(
            target_db_path,
            legacy_db_path=candidate.legacy_db_path,
        )
        stage_dir: Path | None = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            _validate_database_root_binding(
                conn,
                root,
                recovery_storage_key=candidate.storage_key,
            )
            existing = conn.execute(
                """
                SELECT
                    m.recording_id,
                    m.source_fingerprint,
                    r.storage_key
                FROM legacy_import_map AS m
                JOIN recordings AS r ON r.id = m.recording_id
                WHERE m.legacy_kind = 'job' AND m.legacy_key = ?
                """,
                (str(candidate.legacy_job_id),),
            ).fetchone()
            if existing is not None:
                if str(existing["source_fingerprint"]) != candidate.source_fingerprint:
                    raise RuntimeError(
                        "Legacy job was already imported with a different source fingerprint"
                    )
                if str(existing["storage_key"]) != candidate.storage_key:
                    raise RuntimeError(
                        "Import map recording storage_key does not match the current plan"
                    )
                manifest = _existing_record_manifest(candidate, final_dir)
                job_id = _validate_existing_database_state(
                    conn,
                    candidate,
                    recording_id=int(existing["recording_id"]),
                    manifest=manifest,
                )
                _assert_plan_fresh(plan)
                conn.rollback()
                return ImportResult(
                    action="skipped",
                    storage_key=candidate.storage_key,
                    legacy_job_id=candidate.legacy_job_id,
                    recording_id=int(existing["recording_id"]),
                    job_id=job_id,
                    manifest_path=manifest_path,
                    issues=candidate.issues,
                )

            recording_without_map = conn.execute(
                """
                SELECT id
                FROM recordings
                WHERE storage_key = ? AND archived_at IS NULL
                """,
                (candidate.storage_key,),
            ).fetchone()
            if recording_without_map is not None:
                raise RuntimeError(
                    "Existing storage v2 recording is missing its legacy job "
                    "import-map provenance"
                )

            if os.path.lexists(final_dir):
                manifest = _existing_record_manifest(candidate, final_dir)
                _assert_plan_fresh(plan)
                try:
                    recording_id, job_id = _insert_candidate(
                        conn,
                        candidate,
                        manifest=manifest,
                    )
                    _fsync_tree_directories(final_dir)
                    _fsync_directory(staging_root)
                    _fsync_directory(root)
                    _assert_plan_fresh(plan)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                return ImportResult(
                    action="recovered",
                    storage_key=candidate.storage_key,
                    legacy_job_id=candidate.legacy_job_id,
                    recording_id=recording_id,
                    job_id=job_id,
                    manifest_path=manifest_path,
                    issues=candidate.issues,
                )

            _assert_plan_fresh(plan)
            stage_dir = Path(
                tempfile.mkdtemp(
                    prefix=f"{candidate.storage_key}-",
                    dir=staging_root,
                )
            )
            os.chmod(stage_dir, 0o700)
            for artifact in candidate.artifacts:
                target = stage_dir / artifact.target_relpath
                _copy_no_overwrite(target, expected=artifact)
            _assert_plan_fresh(plan)
            manifest = _manifest_for(candidate)
            write_manifest(stage_dir / "manifest.json", manifest)

            recording_id, job_id = _insert_candidate(
                conn,
                candidate,
                manifest=manifest,
            )
            _assert_plan_fresh(plan)
            _fsync_tree_directories(stage_dir)
            stage_dir.rename(final_dir)
            _fsync_directory(staging_root)
            _fsync_directory(root)
            _assert_plan_fresh(plan)
            conn.commit()
            stage_dir = None
            return ImportResult(
                action="imported",
                storage_key=candidate.storage_key,
                legacy_job_id=candidate.legacy_job_id,
                recording_id=recording_id,
                job_id=job_id,
                manifest_path=manifest_path,
                issues=candidate.issues,
            )
        except BaseException:
            if (
                conn.in_transaction
                and stage_dir is not None
                and not stage_dir.exists()
                and final_dir.exists()
            ):
                final_dir.rename(stage_dir)
                _fsync_directory(staging_root)
                _fsync_directory(root)
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            try:
                lock_stat = lock_path.lstat()
                opened_lock_stat = os.fstat(lock_handle.fileno())
                if (
                    stat_module.S_ISREG(lock_stat.st_mode)
                    and lock_stat.st_nlink == 1
                    and lock_stat.st_size == 0
                    and (lock_stat.st_dev, lock_stat.st_ino)
                    == (opened_lock_stat.st_dev, opened_lock_stat.st_ino)
                ):
                    lock_path.unlink()
                    _fsync_directory(locks_dir)
            except FileNotFoundError:
                pass
            raise
        finally:
            if stage_dir is not None and stage_dir.exists() and stage_dir.parent == staging_root:
                shutil.rmtree(stage_dir)
            conn.close()
