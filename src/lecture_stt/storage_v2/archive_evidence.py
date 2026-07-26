from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

from lecture_stt.storage_v2.archive_reconciliation import (
    MAX_REPORTED_ROWS,
    _load_worker_config_from_module,
    reconcile_archive,
)
from lecture_stt.storage_v2.importer import (
    _assert_legacy_database_stable,
    _open_legacy_readonly,
)
from lecture_stt.storage_v2.repository import (
    apply_migration,
    connect_v2,
    require_v2_schema,
)


_ASCII_KEY_RE = re.compile(r"^[0-9A-Za-z_-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_VERIFY_ENTRIES = 10_000
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_LOCK_NAME = ".archive-evidence.lock"
_SNAPSHOT_KEYS = frozenset(
    {
        "logical_stem",
        "subject_abbr",
        "correction_status",
        "summary_status",
        "classification",
        "recorded_paths",
        "expected_paths",
        "hashes",
        "flags",
        "shared_summary_destination",
        "reconciliation_issues",
    }
)
_RECORDED_PATH_KEYS = (
    "correction_txt_source",
    "correction_json_source",
    "summary_md_source",
    "tuk_origin_txt",
    "tuk_origin_json",
    "tuk_summary",
    "obsidian_summary",
)
_EXPECTED_PATH_KEYS = (
    "tuk_origin_txt",
    "tuk_origin_json",
    "tuk_summary",
    "obsidian_summary",
)
_SNAPSHOT_PATH_SUFFIXES = {
    "correction_txt_source": ".txt",
    "correction_json_source": ".json",
    "summary_md_source": ".md",
    "tuk_origin_txt": ".txt",
    "tuk_origin_json": ".json",
    "tuk_summary": ".md",
    "obsidian_summary": ".md",
}
_HASH_KEYS = (
    "correction_txt_sha256",
    "correction_json_sha256",
    "summary_md_sha256",
)
_FLAG_KEYS = (
    "tuk_origin_done",
    "tuk_summary_done",
    "obsidian_done",
)
_SOURCE_STATE_KEYS = (
    "device",
    "inode",
    "mode",
    "links",
    "bytes",
    "mtime_ns",
    "ctime_ns",
)
_RECONCILIATION_ISSUE_KEYS = (
    "severity",
    "code",
    "field",
    "path",
)
_RECONCILIATION_SOURCE_ISSUE_KEYS = (
    *_RECONCILIATION_ISSUE_KEYS,
    "message",
    "expected",
    "actual",
)
_RECONCILIATION_CLASSIFICATIONS = frozenset(
    {
        "verified_delivered",
        "verified_correction_only",
        "manual_review",
        "blocked",
    }
)
_ISSUE_SEVERITIES = frozenset({"info", "warning", "error"})
_ISSUE_CODE_RE = re.compile(r"^[a-z0-9_]+$")
_METADATA_TOKEN_RE = re.compile(r"^[0-9A-Za-z_-]+$")
_CORRECTION_STATUSES = frozenset(
    {"MISSING", "INCOMPLETE", "DELIVERED", "CONFLICT", "ERROR"}
)
_SUMMARY_STATUSES = frozenset(
    {"MISSING", "BLOCKED", "DELIVERED", "CONFLICT", "ERROR"}
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_state(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _state_dict(state_value: tuple[int, int, int, int, int, int, int]) -> dict[str, int]:
    return dict(
        zip(
            ("device", "inode", "mode", "links", "bytes", "mtime_ns", "ctime_ns"),
            state_value,
            strict=True,
        )
    )


def _validate_relpath(value: str) -> str:
    if not value or "\\" in value:
        raise ValueError(f"Unsafe relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"Unsafe relative path: {value!r}")
    return path.as_posix()


def _validate_label(value: str) -> str:
    label = str(value).strip()
    if not _ASCII_KEY_RE.fullmatch(label):
        raise ValueError(
            f"Archive source label must use only ASCII letters, digits, '_' or '-': {value!r}"
        )
    return label


def parse_historical_root_specs(values: Sequence[str] | None) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for raw in values or ():
        label, separator, path_text = str(raw).partition("=")
        if not separator or not path_text.strip():
            raise ValueError(
                "Historical root must use LABEL=/absolute/path syntax"
            )
        normalized_label = _validate_label(label)
        if normalized_label in {"current_gh", "current_obsidian"}:
            raise ValueError(f"Historical root label is reserved: {normalized_label}")
        if normalized_label in roots:
            raise ValueError(f"Duplicate historical root label: {normalized_label}")
        roots[normalized_label] = Path(path_text).expanduser()
    return roots


def _has_symlink_component(path: Path) -> bool:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(metadata.st_mode):
            return True
    return False


def _normalize_source_root(value: Path | str, *, label: str) -> Path:
    raw = Path(value).expanduser().absolute()
    if _has_symlink_component(raw):
        raise ValueError(f"Archive source root must not contain symlinks ({label}): {raw}")
    resolved = raw.resolve(strict=True)
    metadata = resolved.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"Archive source root must be a directory ({label}): {resolved}")
    return resolved


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class SourceSnapshot:
    root_label: str
    root_path: Path
    source_role: str
    source_relpath: str
    artifact_kind: str
    claimed_sha256: str | None
    observed_sha256: str
    relationship: str
    state: tuple[int, int, int, int, int, int, int]

    @property
    def bytes(self) -> int:
        return self.state[4]

    @property
    def absolute_path(self) -> Path:
        return self.root_path / self.source_relpath

    def as_dict(self) -> dict[str, Any]:
        return {
            "root_label": self.root_label,
            "root_path": str(self.root_path),
            "source_role": self.source_role,
            "source_relpath": self.source_relpath,
            "artifact_kind": self.artifact_kind,
            "claimed_sha256": self.claimed_sha256,
            "observed_sha256": self.observed_sha256,
            "relationship": self.relationship,
            "state": _state_dict(self.state),
        }

    def manifest_dict(self) -> dict[str, Any]:
        return {
            "source_role": self.source_role,
            "source_root_label": self.root_label,
            "source_relpath": self.source_relpath,
            "claimed_sha256": self.claimed_sha256,
            "observed_sha256": self.observed_sha256,
            "relationship": self.relationship,
        }


@dataclass(frozen=True)
class RevisionPlan:
    artifact_kind: str
    content_sha256: str
    bytes: int
    mime_type: str
    path_rel: str
    observations: tuple[SourceSnapshot, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_kind": self.artifact_kind,
            "content_sha256": self.content_sha256,
            "bytes": self.bytes,
            "mime_type": self.mime_type,
            "path_rel": self.path_rel,
            "observations": [item.as_dict() for item in self.observations],
        }

    def manifest_dict(self) -> dict[str, Any]:
        return {
            "artifact_kind": self.artifact_kind,
            "content_sha256": self.content_sha256,
            "bytes": self.bytes,
            "mime_type": self.mime_type,
            "path_rel": self.path_rel,
        }


@dataclass(frozen=True)
class ArchiveEvidenceCasePlan:
    case_key: str
    capture_key: str
    legacy_delivery_key: str
    logical_stem: str
    subject_abbr: str
    reconciliation_classification: str
    legacy_database_sha256: str
    source_fingerprint: str
    plan_sha256: str
    manifest_relpath: str
    snapshot: Mapping[str, Any]
    revisions: tuple[RevisionPlan, ...]
    issues: tuple[Mapping[str, Any], ...]

    @property
    def can_apply(self) -> bool:
        return not any(issue.get("severity") == "error" for issue in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_key": self.case_key,
            "capture_key": self.capture_key,
            "legacy_delivery_key": self.legacy_delivery_key,
            "logical_stem": self.logical_stem,
            "subject_abbr": self.subject_abbr,
            "reconciliation_classification": self.reconciliation_classification,
            "legacy_database_sha256": self.legacy_database_sha256,
            "source_fingerprint": self.source_fingerprint,
            "plan_sha256": self.plan_sha256,
            "manifest_relpath": self.manifest_relpath,
            "snapshot": dict(self.snapshot),
            "revisions": [revision.as_dict() for revision in self.revisions],
            "issues": [dict(issue) for issue in self.issues],
            "can_apply": self.can_apply,
        }


@dataclass(frozen=True)
class ArchiveEvidencePlanBatch:
    legacy_db_path: Path
    legacy_root: Path
    legacy_database_snapshot: Mapping[str, Any]
    source_roots: Mapping[str, Path]
    candidate_count: int
    truncated: bool
    cases: tuple[ArchiveEvidenceCasePlan, ...]

    @property
    def plan_sha256(self) -> str:
        return _sha256_json(
            {
                "legacy_db_path": str(self.legacy_db_path),
                "legacy_root": str(self.legacy_root),
                "legacy_database_snapshot": dict(self.legacy_database_snapshot),
                "source_roots": {
                    key: str(value)
                    for key, value in sorted(self.source_roots.items())
                },
                "candidate_count": self.candidate_count,
                "truncated": self.truncated,
                "cases": [case.as_dict() for case in self.cases],
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "storage-v2/archive-evidence-plan@1",
            "mode": "read_only",
            "plan_sha256": self.plan_sha256,
            "legacy_db": str(self.legacy_db_path),
            "legacy_root": str(self.legacy_root),
            "legacy_database_snapshot": dict(self.legacy_database_snapshot),
            "summary": {
                "candidate_count": self.candidate_count,
                "planned_count": len(self.cases),
                "applicable": sum(case.can_apply for case in self.cases),
                "blocked": sum(not case.can_apply for case in self.cases),
                "review_required": sum(
                    case.reconciliation_classification
                    in {"manual_review", "blocked"}
                    for case in self.cases
                ),
                "truncated": self.truncated,
            },
            "cases": [case.as_dict() for case in self.cases],
        }


def _relative_under_root(path_value: Path | str, root: Path) -> str | None:
    raw = Path(path_value).expanduser()
    if not raw.is_absolute() or ".." in raw.parts:
        return None
    try:
        relative = raw.absolute().relative_to(root)
    except ValueError:
        return None
    try:
        return _validate_relpath(relative.as_posix())
    except ValueError:
        return None


def _probe_source(
    root: Path,
    relpath: str,
    *,
    root_label: str,
    source_role: str,
    artifact_kind: str,
    claimed_sha256: str | None,
    relationship: str,
) -> SourceSnapshot:
    safe_relpath = _validate_relpath(relpath)
    candidate = root / safe_relpath
    if _has_symlink_component(candidate):
        raise RuntimeError(f"Archive evidence source contains a symlink: {candidate}")
    try:
        path_state = candidate.lstat()
    except FileNotFoundError:
        raise
    if not stat.S_ISREG(path_state.st_mode):
        raise RuntimeError(f"Archive evidence source is not a regular file: {candidate}")
    if path_state.st_nlink != 1:
        raise RuntimeError(f"Archive evidence source is hard-linked: {candidate}")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(candidate, flags)
    try:
        before = os.fstat(descriptor)
        if _file_state(before) != _file_state(path_state):
            raise RuntimeError(f"Archive evidence source changed while opening: {candidate}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        try:
            current = candidate.lstat()
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Archive evidence source disappeared while hashing: {candidate}"
            ) from exc
        if _file_state(after) != _file_state(before) or _file_state(current) != _file_state(before):
            raise RuntimeError(f"Archive evidence source changed while hashing: {candidate}")
    finally:
        os.close(descriptor)
    observed = digest.hexdigest()
    return SourceSnapshot(
        root_label=root_label,
        root_path=root,
        source_role=source_role,
        source_relpath=safe_relpath,
        artifact_kind=artifact_kind,
        claimed_sha256=claimed_sha256,
        observed_sha256=observed,
        relationship=relationship,
        state=_file_state(path_state),
    )


def _normalized_claim(value: Any) -> str | None:
    digest = str(value or "").strip().lower()
    return digest if _SHA256_RE.fullmatch(digest) else None


def _relationship(
    *,
    claimed_sha256: str | None,
    observed_sha256: str,
    unexpected_current: bool,
) -> str:
    if unexpected_current:
        return "unexpected_current"
    if claimed_sha256 is None:
        return "unclaimed"
    if claimed_sha256 == observed_sha256:
        return "matches_ledger"
    return "differs_from_ledger"


def _metadata_mapping(
    value: Any,
    *,
    label: str,
    allowed_keys: Sequence[str],
    require_exact: bool,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    actual_keys = {str(key) for key in value}
    expected_keys = set(allowed_keys)
    unexpected = sorted(actual_keys.difference(expected_keys))
    if unexpected:
        raise ValueError(f"{label} contains unexpected fields: {unexpected}")
    missing = sorted(expected_keys.difference(actual_keys))
    if require_exact and missing:
        raise ValueError(f"{label} is missing required fields: {missing}")
    return value


def _metadata_text(
    value: Any,
    *,
    label: str,
    max_length: int,
    optional: bool = False,
    allow_empty: bool = True,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"{label} must not be empty")
    if len(value) > max_length:
        raise ValueError(f"{label} exceeds {max_length} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} contains control characters")
    return value


def _metadata_absolute_path(
    value: Any,
    *,
    label: str,
    optional: bool,
    suffix: str | None = None,
) -> str | None:
    text_value = _metadata_text(
        value,
        label=label,
        max_length=8192,
        optional=optional,
    )
    if text_value is None:
        return None
    path = Path(text_value)
    if (
        not path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or "\\" in text_value
    ):
        raise ValueError(f"{label} must be a canonical absolute path")
    if suffix is not None and path.suffix.lower() != suffix:
        raise ValueError(f"{label} must end with {suffix}")
    return text_value


def _validate_snapshot_metadata(value: Any) -> None:
    snapshot = _metadata_mapping(
        value,
        label="Archive evidence snapshot",
        allowed_keys=tuple(_SNAPSHOT_KEYS),
        require_exact=True,
    )
    logical_stem = _metadata_text(
        snapshot["logical_stem"],
        label="Archive evidence snapshot logical_stem",
        max_length=255,
        allow_empty=False,
    )
    if logical_stem is None or not _METADATA_TOKEN_RE.fullmatch(logical_stem):
        raise ValueError(
            "Archive evidence snapshot logical_stem is not a canonical metadata token"
        )
    subject_abbr = _metadata_text(
        snapshot["subject_abbr"],
        label="Archive evidence snapshot subject_abbr",
        max_length=64,
    )
    if subject_abbr and not _METADATA_TOKEN_RE.fullmatch(subject_abbr):
        raise ValueError(
            "Archive evidence snapshot subject_abbr is not a canonical metadata token"
        )
    correction_status = _metadata_text(
        snapshot["correction_status"],
        label="Archive evidence snapshot correction_status",
        max_length=32,
        allow_empty=False,
    )
    if correction_status not in _CORRECTION_STATUSES:
        raise ValueError(
            "Archive evidence snapshot correction_status is not recognized"
        )
    summary_status = _metadata_text(
        snapshot["summary_status"],
        label="Archive evidence snapshot summary_status",
        max_length=32,
        allow_empty=False,
    )
    if summary_status not in _SUMMARY_STATUSES:
        raise ValueError(
            "Archive evidence snapshot summary_status is not recognized"
        )
    classification = _metadata_text(
        snapshot["classification"],
        label="Archive evidence snapshot classification",
        max_length=64,
        allow_empty=False,
    )
    if classification not in _RECONCILIATION_CLASSIFICATIONS:
        raise ValueError(
            "Archive evidence snapshot classification is not recognized"
        )

    recorded_paths = _metadata_mapping(
        snapshot["recorded_paths"],
        label="Archive evidence snapshot recorded_paths",
        allowed_keys=_RECORDED_PATH_KEYS,
        require_exact=True,
    )
    for key in _RECORDED_PATH_KEYS:
        _metadata_absolute_path(
            recorded_paths[key],
            label=f"Archive evidence snapshot recorded_paths.{key}",
            optional=True,
            suffix=_SNAPSHOT_PATH_SUFFIXES[key],
        )

    expected_paths = _metadata_mapping(
        snapshot["expected_paths"],
        label="Archive evidence snapshot expected_paths",
        allowed_keys=_EXPECTED_PATH_KEYS,
        require_exact=True,
    )
    for key in _EXPECTED_PATH_KEYS:
        _metadata_absolute_path(
            expected_paths[key],
            label=f"Archive evidence snapshot expected_paths.{key}",
            optional=True,
            suffix=_SNAPSHOT_PATH_SUFFIXES[key],
        )

    hashes = _metadata_mapping(
        snapshot["hashes"],
        label="Archive evidence snapshot hashes",
        allowed_keys=_HASH_KEYS,
        require_exact=True,
    )
    for key in _HASH_KEYS:
        digest = _metadata_text(
            hashes[key],
            label=f"Archive evidence snapshot hashes.{key}",
            max_length=64,
            optional=True,
        )
        if digest is not None and not _SHA256_RE.fullmatch(digest):
            raise ValueError(
                f"Archive evidence snapshot hashes.{key} must be a canonical SHA-256"
            )

    flags = _metadata_mapping(
        snapshot["flags"],
        label="Archive evidence snapshot flags",
        allowed_keys=_FLAG_KEYS,
        require_exact=True,
    )
    for key in _FLAG_KEYS:
        flag = flags[key]
        if type(flag) is not int or flag not in {0, 1}:
            raise ValueError(
                f"Archive evidence snapshot flags.{key} must be 0 or 1"
            )

    if type(snapshot["shared_summary_destination"]) is not bool:
        raise ValueError(
            "Archive evidence snapshot shared_summary_destination must be boolean"
        )

    issue_rows = snapshot["reconciliation_issues"]
    if not isinstance(issue_rows, list):
        raise ValueError(
            "Archive evidence snapshot reconciliation_issues must be an array"
        )
    if len(issue_rows) > 256:
        raise ValueError(
            "Archive evidence snapshot reconciliation_issues exceeds 256 entries"
        )
    for index, issue_value in enumerate(issue_rows):
        issue = _metadata_mapping(
            issue_value,
            label=f"Archive evidence snapshot reconciliation_issues[{index}]",
            allowed_keys=_RECONCILIATION_ISSUE_KEYS,
            require_exact=True,
        )
        severity = _metadata_text(
            issue["severity"],
            label=f"Archive evidence snapshot issue {index} severity",
            max_length=16,
            allow_empty=False,
        )
        if severity not in _ISSUE_SEVERITIES:
            raise ValueError(
                f"Archive evidence snapshot issue {index} severity is not recognized"
            )
        code = _metadata_text(
            issue["code"],
            label=f"Archive evidence snapshot issue {index} code",
            max_length=128,
            allow_empty=False,
        )
        if code is None or not _ISSUE_CODE_RE.fullmatch(code):
            raise ValueError(
                f"Archive evidence snapshot issue {index} code is not canonical"
            )
        field = _metadata_text(
            issue["field"],
            label=f"Archive evidence snapshot issue {index} field",
            max_length=128,
            optional=True,
        )
        if field is not None and not _ISSUE_CODE_RE.fullmatch(field):
            raise ValueError(
                f"Archive evidence snapshot issue {index} field is not canonical"
            )
        _metadata_absolute_path(
            issue["path"],
            label=f"Archive evidence snapshot issue {index} path",
            optional=True,
        )
    if _contains_forbidden_body_key(snapshot):
        raise ValueError("Archive evidence snapshot contains forbidden body fields")


def _observation_metadata_payload(
    state_value: tuple[int, int, int, int, int, int, int],
) -> dict[str, Any]:
    payload = {"state": _state_dict(state_value)}
    _validate_observation_metadata(payload)
    return payload


def _validate_observation_metadata(value: Any) -> None:
    metadata = _metadata_mapping(
        value,
        label="Archive evidence observation metadata",
        allowed_keys=("state",),
        require_exact=True,
    )
    state_value = _metadata_mapping(
        metadata["state"],
        label="Archive evidence observation metadata state",
        allowed_keys=_SOURCE_STATE_KEYS,
        require_exact=True,
    )
    for key in _SOURCE_STATE_KEYS:
        number = state_value[key]
        if type(number) is not int or number < 0:
            raise ValueError(
                f"Archive evidence observation metadata state.{key} "
                "must be a non-negative integer"
            )
    if int(state_value["links"]) != 1:
        raise ValueError(
            "Archive evidence observation metadata state.links must equal 1"
        )
    if not stat.S_ISREG(int(state_value["mode"])):
        raise ValueError(
            "Archive evidence observation metadata state.mode must describe "
            "a regular file"
        )
    if _contains_forbidden_body_key(metadata):
        raise ValueError(
            "Archive evidence observation metadata contains forbidden body fields"
        )


def _normalize_snapshot_mapping(
    value: Any,
    *,
    label: str,
    keys: Sequence[str],
    default: Any,
) -> dict[str, Any]:
    source = _metadata_mapping(
        value,
        label=label,
        allowed_keys=keys,
        require_exact=False,
    )
    return {key: source.get(key, default) for key in keys}


def _snapshot_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    recorded_paths = _normalize_snapshot_mapping(
        row["recorded_paths"],
        label="Archive reconciliation recorded_paths",
        keys=_RECORDED_PATH_KEYS,
        default=None,
    )
    expected_paths = _normalize_snapshot_mapping(
        row["expected_paths"],
        label="Archive reconciliation expected_paths",
        keys=_EXPECTED_PATH_KEYS,
        default=None,
    )
    for values, keys in (
        (recorded_paths, _RECORDED_PATH_KEYS),
        (expected_paths, _EXPECTED_PATH_KEYS),
    ):
        for key in keys:
            try:
                values[key] = _metadata_absolute_path(
                    values[key],
                    label=f"Archive reconciliation path {key}",
                    optional=True,
                    suffix=_SNAPSHOT_PATH_SUFFIXES[key],
                )
            except ValueError:
                values[key] = None
    hashes = _normalize_snapshot_mapping(
        row["hashes"],
        label="Archive reconciliation hashes",
        keys=_HASH_KEYS,
        default=None,
    )
    normalized_hashes: dict[str, str | None] = {}
    for key in _HASH_KEYS:
        digest = str(hashes[key] or "").strip().lower()
        normalized_hashes[key] = digest if _SHA256_RE.fullmatch(digest) else None
    hashes = normalized_hashes
    flags = _normalize_snapshot_mapping(
        row["flags"],
        label="Archive reconciliation flags",
        keys=_FLAG_KEYS,
        default=0,
    )
    issue_rows = row["issues"]
    if not isinstance(issue_rows, list):
        raise ValueError("Archive reconciliation issues must be an array")
    reconciliation_issues: list[dict[str, Any]] = []
    for index, issue_value in enumerate(issue_rows):
        issue = _metadata_mapping(
            issue_value,
            label=f"Archive reconciliation issues[{index}]",
            allowed_keys=_RECONCILIATION_SOURCE_ISSUE_KEYS,
            require_exact=False,
        )
        try:
            issue_path = _metadata_absolute_path(
                issue.get("path"),
                label=f"Archive reconciliation issue {index} path",
                optional=True,
            )
        except ValueError:
            issue_path = None
        reconciliation_issues.append(
            {
                "severity": issue.get("severity"),
                "code": issue.get("code"),
                "field": issue.get("field"),
                "path": issue_path,
            }
        )
    snapshot = {
        "logical_stem": str(row["logical_stem"]),
        "subject_abbr": str(row["subject_abbr"]),
        "correction_status": str(row["correction_status"]),
        "summary_status": str(row["summary_status"]),
        "classification": str(row["classification"]),
        "recorded_paths": recorded_paths,
        "expected_paths": expected_paths,
        "hashes": hashes,
        "flags": flags,
        "shared_summary_destination": bool(row["shared_summary_destination"]),
        "reconciliation_issues": reconciliation_issues,
    }
    _validate_snapshot_metadata(snapshot)
    return snapshot


_SLOT_DEFINITIONS = (
    (
        "correction_txt",
        "tuk_origin_txt",
        "correction_text",
        "correction_txt_sha256",
        "current_gh",
        "text/plain",
        ".txt",
    ),
    (
        "correction_json",
        "tuk_origin_json",
        "correction_json",
        "correction_json_sha256",
        "current_gh",
        "application/json",
        ".json",
    ),
    (
        "summary_tuk",
        "tuk_summary",
        "summary_markdown",
        "summary_md_sha256",
        "current_gh",
        "text/markdown",
        ".md",
    ),
    (
        "summary_obsidian",
        "obsidian_summary",
        "summary_markdown",
        "summary_md_sha256",
        "current_obsidian",
        "text/markdown",
        ".md",
    ),
)


def _build_case_plan(
    row: Mapping[str, Any],
    *,
    legacy_database_sha256: str,
    source_roots: Mapping[str, Path],
    historical_roots: Mapping[str, Path],
) -> ArchiveEvidenceCasePlan:
    issues: list[dict[str, Any]] = []
    observations: list[tuple[SourceSnapshot, str]] = []
    summary_missing = str(row["summary_status"]).upper() == "MISSING"

    for (
        _observed_key,
        expected_key,
        artifact_kind,
        hash_key,
        root_label,
        mime_type,
        extension,
    ) in _SLOT_DEFINITIONS:
        root = source_roots[root_label]
        expected_path = row["expected_paths"].get(expected_key)
        if not expected_path:
            continue
        relpath = _relative_under_root(expected_path, root)
        if relpath is None:
            issues.append(
                {
                    "severity": "error",
                    "code": "current_path_outside_root",
                    "message": "Deterministic current archive path is outside its configured root",
                    "path": str(expected_path),
                }
            )
            continue
        try:
            candidate = root / relpath
            candidate.lstat()
        except FileNotFoundError:
            continue
        claimed = _normalized_claim(row["hashes"].get(hash_key))
        try:
            provisional = _probe_source(
                root,
                relpath,
                root_label=root_label,
                source_role=root_label,
                artifact_kind=artifact_kind,
                claimed_sha256=claimed,
                relationship="unclaimed",
            )
            relation = _relationship(
                claimed_sha256=claimed,
                observed_sha256=provisional.observed_sha256,
                unexpected_current=summary_missing
                and artifact_kind == "summary_markdown",
            )
            observations.append(
                (
                    SourceSnapshot(
                        **{
                            **provisional.__dict__,
                            "relationship": relation,
                        }
                    ),
                    extension,
                )
            )
        except (OSError, RuntimeError, ValueError) as exc:
            issues.append(
                {
                    "severity": "error",
                    "code": "current_source_unsafe",
                    "message": str(exc),
                    "path": str(candidate),
                }
            )

    historical_by_depth = sorted(
        historical_roots.items(),
        key=lambda item: (-len(item[1].parts), item[0]),
    )
    recorded_slot_map = (
        ("tuk_origin_txt", "correction_text", "correction_txt_sha256", "text/plain", ".txt"),
        ("tuk_origin_json", "correction_json", "correction_json_sha256", "application/json", ".json"),
        ("tuk_summary", "summary_markdown", "summary_md_sha256", "text/markdown", ".md"),
        ("obsidian_summary", "summary_markdown", "summary_md_sha256", "text/markdown", ".md"),
    )
    for recorded_key, artifact_kind, hash_key, mime_type, extension in recorded_slot_map:
        recorded_path = row["recorded_paths"].get(recorded_key)
        if not recorded_path:
            continue
        matched: tuple[str, Path, str] | None = None
        for label, root in historical_by_depth:
            relpath = _relative_under_root(recorded_path, root)
            if relpath is not None:
                matched = (label, root, relpath)
                break
        if matched is None:
            continue
        label, root, relpath = matched
        try:
            (root / relpath).lstat()
        except FileNotFoundError:
            continue
        claimed = _normalized_claim(row["hashes"].get(hash_key))
        try:
            provisional = _probe_source(
                root,
                relpath,
                root_label=label,
                source_role="historical",
                artifact_kind=artifact_kind,
                claimed_sha256=claimed,
                relationship="unclaimed",
            )
            observations.append(
                (
                    SourceSnapshot(
                        **{
                            **provisional.__dict__,
                            "relationship": _relationship(
                                claimed_sha256=claimed,
                                observed_sha256=provisional.observed_sha256,
                                unexpected_current=False,
                            ),
                        }
                    ),
                    extension,
                )
            )
        except (OSError, RuntimeError, ValueError) as exc:
            issues.append(
                {
                    "severity": "error",
                    "code": "historical_source_unsafe",
                    "message": str(exc),
                    "path": str(root / relpath),
                }
            )

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    seen_observations: set[tuple[str, str, str, str, str]] = set()
    mime_by_kind = {
        "correction_text": "text/plain",
        "correction_json": "application/json",
        "summary_markdown": "text/markdown",
    }
    extension_by_kind = {
        "correction_text": ".txt",
        "correction_json": ".json",
        "summary_markdown": ".md",
    }
    for observation, _extension in observations:
        observation_key = (
            observation.artifact_kind,
            observation.observed_sha256,
            observation.source_role,
            observation.root_label,
            observation.source_relpath,
        )
        if observation_key in seen_observations:
            continue
        seen_observations.add(observation_key)
        key = (observation.artifact_kind, observation.observed_sha256)
        bucket = grouped.setdefault(
            key,
            {
                "bytes": observation.bytes,
                "observations": [],
            },
        )
        if int(bucket["bytes"]) != observation.bytes:
            issues.append(
                {
                    "severity": "error",
                    "code": "same_hash_size_conflict",
                    "message": "Equal artifact hashes were observed with different byte sizes",
                }
            )
        bucket["observations"].append(observation)

    logical_stem = str(row["logical_stem"])
    legacy_delivery_key = f"delivery:{logical_stem}"
    case_key = f"arc_{hashlib.sha256(legacy_delivery_key.encode('utf-8')).hexdigest()[:24]}"
    revisions: list[RevisionPlan] = []
    for (artifact_kind, digest), bucket in sorted(grouped.items()):
        extension = extension_by_kind[artifact_kind]
        path_rel = (
            f"cases/{case_key}/revisions/{artifact_kind}/{digest}{extension}"
        )
        revisions.append(
            RevisionPlan(
                artifact_kind=artifact_kind,
                content_sha256=digest,
                bytes=int(bucket["bytes"]),
                mime_type=mime_by_kind[artifact_kind],
                path_rel=path_rel,
                observations=tuple(
                    sorted(
                        bucket["observations"],
                        key=lambda item: (
                            item.source_role,
                            item.root_label,
                            item.source_relpath,
                        ),
                    )
                ),
            )
        )

    snapshot = _snapshot_payload(row)
    source_fingerprint = _sha256_json(snapshot)
    capture_seed = {
        "case_key": case_key,
        "legacy_delivery_key": legacy_delivery_key,
        "reconciliation_classification": row["classification"],
        "legacy_database_sha256": legacy_database_sha256,
        "source_fingerprint": source_fingerprint,
        "revisions": [revision.as_dict() for revision in revisions],
        "issues": issues,
    }
    case_plan_sha256 = _sha256_json(capture_seed)
    capture_key = f"cap_{case_plan_sha256[:32]}"
    manifest_relpath = f"cases/{case_key}/captures/{capture_key}.json"
    return ArchiveEvidenceCasePlan(
        case_key=case_key,
        capture_key=capture_key,
        legacy_delivery_key=legacy_delivery_key,
        logical_stem=logical_stem,
        subject_abbr=str(row["subject_abbr"]),
        reconciliation_classification=str(row["classification"]),
        legacy_database_sha256=legacy_database_sha256,
        source_fingerprint=source_fingerprint,
        plan_sha256=case_plan_sha256,
        manifest_relpath=manifest_relpath,
        snapshot=snapshot,
        revisions=tuple(revisions),
        issues=tuple(issues),
    )


def plan_archive_evidence(
    legacy_db_path: Path | str,
    legacy_root: Path | str,
    *,
    config_path: str = "config/config.yaml",
    historical_roots: Mapping[str, Path | str] | None = None,
    logical_stems: Sequence[str] | None = None,
    limit: int = MAX_REPORTED_ROWS,
) -> ArchiveEvidencePlanBatch:
    if limit < 1 or limit > MAX_REPORTED_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_REPORTED_ROWS}")
    config = _load_worker_config_from_module(config_path)
    current_roots = {
        "current_gh": _normalize_source_root(
            config.gh_current_semester_root,
            label="current_gh",
        ),
        "current_obsidian": _normalize_source_root(
            config.obsidian_semester_root,
            label="current_obsidian",
        ),
    }
    normalized_historical: dict[str, Path] = {}
    for raw_label, raw_path in (historical_roots or {}).items():
        label = _validate_label(raw_label)
        if label in current_roots:
            raise ValueError(f"Historical root label is reserved: {label}")
        normalized_historical[label] = _normalize_source_root(raw_path, label=label)
    historical_items = sorted(normalized_historical.items())
    for index, (label, root) in enumerate(historical_items):
        for current_label, current_root in current_roots.items():
            if _paths_overlap(root, current_root):
                raise ValueError(
                    "Historical root must not overlap a current archive root: "
                    f"{label}, {current_label}"
                )
        for other_label, other_root in historical_items[index + 1 :]:
            if _paths_overlap(root, other_root):
                raise ValueError(
                    f"Historical roots must not overlap: {label}, {other_label}"
                )

    report = reconcile_archive(
        legacy_db_path,
        legacy_root,
        downstream_config=config,
        limit=MAX_REPORTED_ROWS,
    )
    if report["summary"]["truncated"]:
        raise RuntimeError(
            "Archive reconciliation output is truncated; refusing to build an "
            "incomplete evidence plan"
        )
    selected = {str(value).strip() for value in logical_stems or () if str(value).strip()}
    rows = [
        row
        for row in report["rows"]
        if not selected or row["logical_stem"] in selected
    ]
    if selected:
        found = {str(row["logical_stem"]) for row in rows}
        missing = sorted(selected.difference(found))
        if missing:
            raise ValueError(
                "Requested logical_stem values are not archive reconciliation candidates: "
                + ", ".join(missing)
            )
    rows.sort(key=lambda row: str(row["logical_stem"]))
    candidate_count = len(rows)
    truncated = candidate_count > limit
    rows = rows[:limit]
    legacy_snapshot = dict(report["legacy_database_snapshot"])
    legacy_database_sha256 = str(legacy_snapshot["main_sha256"])
    source_roots = {**current_roots, **normalized_historical}
    cases = tuple(
        _build_case_plan(
            row,
            legacy_database_sha256=legacy_database_sha256,
            source_roots=current_roots,
            historical_roots=normalized_historical,
        )
        for row in rows
    )
    return ArchiveEvidencePlanBatch(
        legacy_db_path=Path(report["legacy_db"]),
        legacy_root=Path(report["legacy_root"]),
        legacy_database_snapshot=legacy_snapshot,
        source_roots=source_roots,
        candidate_count=candidate_count,
        truncated=truncated,
        cases=cases,
    )


def _assert_legacy_snapshot(batch: ArchiveEvidencePlanBatch) -> None:
    conn, resolved_path, current_snapshot = _open_legacy_readonly(batch.legacy_db_path)
    try:
        if current_snapshot.as_dict() != dict(batch.legacy_database_snapshot):
            raise RuntimeError(
                "Legacy database snapshot changed after archive evidence planning"
            )
        _assert_legacy_database_stable(resolved_path, current_snapshot)
    finally:
        conn.close()
    _assert_legacy_database_stable(resolved_path, current_snapshot)


def _assert_source_fresh(source: SourceSnapshot) -> None:
    current = _probe_source(
        source.root_path,
        source.source_relpath,
        root_label=source.root_label,
        source_role=source.source_role,
        artifact_kind=source.artifact_kind,
        claimed_sha256=source.claimed_sha256,
        relationship=source.relationship,
    )
    if (
        current.state != source.state
        or current.observed_sha256 != source.observed_sha256
    ):
        raise RuntimeError(
            f"Archive evidence source changed after planning: {source.absolute_path}"
        )


def _manifest_payload(case: ArchiveEvidenceCasePlan) -> dict[str, Any]:
    _validate_snapshot_metadata(case.snapshot)
    observations: list[dict[str, Any]] = []
    for revision in case.revisions:
        for source in revision.observations:
            observations.append(
                {
                    "artifact_kind": revision.artifact_kind,
                    "content_sha256": revision.content_sha256,
                    **source.manifest_dict(),
                }
            )
    observations.sort(
        key=lambda item: (
            str(item["artifact_kind"]),
            str(item["content_sha256"]),
            str(item["source_role"]),
            str(item["source_root_label"]),
            str(item["source_relpath"]),
        )
    )
    return {
        "schema_version": "storage-v2/archive-evidence-capture@1",
        "case": {
            "case_key": case.case_key,
            "legacy_delivery_key": case.legacy_delivery_key,
            "logical_stem": case.logical_stem,
            "subject_abbr": case.subject_abbr,
            "review_status": "open",
        },
        "capture": {
            "capture_key": case.capture_key,
            "reconciliation_classification": case.reconciliation_classification,
            "legacy_database_sha256": case.legacy_database_sha256,
            "source_fingerprint": case.source_fingerprint,
            "plan_sha256": case.plan_sha256,
            "manifest_relpath": case.manifest_relpath,
        },
        "snapshot": dict(case.snapshot),
        "revisions": [
            revision.manifest_dict()
            for revision in case.revisions
        ],
        "observations": observations,
        "issues": [dict(issue) for issue in case.issues],
    }


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


def _ensure_directory(path: Path, *, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"Evidence directory escaped its root: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            _fsync_directory(current.parent)
            metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"Unsafe evidence directory: {current}")


def _normalize_evidence_root(value: Path | str) -> Path:
    raw = Path(value).expanduser().absolute()
    if raw.exists():
        if _has_symlink_component(raw):
            raise ValueError(f"Evidence root must not contain symlinks: {raw}")
        resolved = raw.resolve(strict=True)
        metadata = resolved.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"Evidence root must be a directory: {resolved}")
        return resolved
    parent = raw.parent
    if _has_symlink_component(parent):
        raise ValueError(f"Evidence root parent must not contain symlinks: {parent}")
    resolved_parent = parent.resolve(strict=True)
    resolved = resolved_parent / raw.name
    resolved.mkdir(mode=0o700)
    _fsync_directory(resolved_parent)
    return resolved


def _directory_identity(path: Path) -> tuple[int, int, int]:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"Archive evidence root is not a safe directory: {path}")
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
    )


def _assert_directory_identity(
    path: Path,
    expected: tuple[int, int, int],
) -> None:
    if _directory_identity(path) != expected:
        raise RuntimeError(
            f"Archive evidence root identity changed during the operation: {path}"
        )


@contextmanager
def _exclusive_evidence_root_lock(
    root: Path,
) -> Iterator[tuple[int, int, int]]:
    root_identity = _directory_identity(root)
    lock_path = root / _LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        current = lock_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or _file_state(metadata) != _file_state(current)
            or metadata.st_size != 0
        ):
            raise RuntimeError(f"Unsafe archive evidence lock file: {lock_path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _assert_directory_identity(root, root_identity)
        yield root_identity
        _assert_directory_identity(root, root_identity)
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _open_regular_single_link(path: Path, *, label: str) -> tuple[int, os.stat_result]:
    if _has_symlink_component(path):
        raise RuntimeError(f"{label} contains a symlink: {path}")
    try:
        path_metadata = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} is missing: {path}") from exc
    if not stat.S_ISREG(path_metadata.st_mode):
        raise RuntimeError(f"{label} is not a regular file: {path}")
    if path_metadata.st_nlink != 1:
        raise RuntimeError(f"{label} is hard-linked: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    opened = os.fstat(descriptor)
    if _file_state(opened) != _file_state(path_metadata):
        os.close(descriptor)
        raise RuntimeError(f"{label} changed while opening: {path}")
    return descriptor, opened


def _hash_opened_file(
    path: Path,
    descriptor: int,
    before: os.stat_result,
    *,
    label: str,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    after = os.fstat(descriptor)
    try:
        current = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} disappeared while reading: {path}") from exc
    if _file_state(after) != _file_state(before) or _file_state(current) != _file_state(before):
        raise RuntimeError(f"{label} changed while reading: {path}")
    return digest.hexdigest(), total


def _verify_exact_file(
    path: Path,
    *,
    expected_sha256: str,
    expected_bytes: int,
    label: str,
) -> None:
    descriptor, before = _open_regular_single_link(path, label=label)
    try:
        digest, total = _hash_opened_file(
            path,
            descriptor,
            before,
            label=label,
        )
    finally:
        os.close(descriptor)
    if total != expected_bytes:
        raise RuntimeError(
            f"{label} size mismatch: expected {expected_bytes}, found {total}"
        )
    if digest != expected_sha256:
        raise RuntimeError(
            f"{label} hash mismatch: expected {expected_sha256}, found {digest}"
        )


def _write_stage_bytes(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_source_to_stage(source: SourceSnapshot, target: Path) -> None:
    source_fd, before = _open_regular_single_link(
        source.absolute_path,
        label="Archive evidence source",
    )
    target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        target_flags |= os.O_NOFOLLOW
    target_fd = os.open(target, target_flags, 0o600)
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                view = view[written:]
        os.fsync(target_fd)
        after = os.fstat(source_fd)
        current = source.absolute_path.lstat()
        if (
            _file_state(before) != source.state
            or _file_state(after) != source.state
            or _file_state(current) != source.state
        ):
            raise RuntimeError(
                f"Archive evidence source changed while copying: {source.absolute_path}"
            )
        if total != source.bytes or digest.hexdigest() != source.observed_sha256:
            raise RuntimeError(
                f"Archive evidence source content changed while copying: {source.absolute_path}"
            )
    finally:
        os.close(target_fd)
        os.close(source_fd)


def _promote_no_replace(stage_path: Path, final_path: Path) -> None:
    if os.path.lexists(final_path):
        raise FileExistsError(final_path)
    try:
        os.link(stage_path, final_path, follow_symlinks=False)
    except FileExistsError:
        raise
    stage_path.unlink()
    _fsync_directory(final_path.parent)


def _validate_target_db_path(
    target_db_path: Path | str,
    *,
    batch: ArchiveEvidencePlanBatch,
    evidence_root: Path,
) -> Path:
    raw = Path(target_db_path).expanduser().absolute()
    if raw.is_symlink() or _has_symlink_component(raw.parent):
        raise ValueError(f"Storage v2 DB path must not contain symlinks: {raw}")
    if os.path.lexists(raw):
        metadata = raw.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(f"Storage v2 DB must be a single-link regular file: {raw}")
        if os.path.samefile(raw, batch.legacy_db_path):
            raise ValueError("Storage v2 DB must be separate from the legacy DB")
    resolved = raw.resolve()
    if _paths_overlap(resolved, evidence_root):
        raise ValueError("Storage v2 DB must be outside the archive evidence root")
    for label, source_root in batch.source_roots.items():
        if _paths_overlap(resolved, source_root):
            raise ValueError(
                f"Storage v2 DB must be outside archive source root {label}"
            )
    if resolved == batch.legacy_db_path:
        raise ValueError("Storage v2 DB must be separate from the legacy DB")
    if _paths_overlap(resolved, batch.legacy_root):
        raise ValueError("Storage v2 DB must be outside the legacy root")
    return raw


def _prepare_evidence_root(
    value: Path | str,
    *,
    batch: ArchiveEvidencePlanBatch,
) -> Path:
    raw = Path(value).expanduser().absolute()
    prospective = raw.resolve()
    if _paths_overlap(prospective, batch.legacy_root):
        raise ValueError("Archive evidence root must be outside the legacy root")
    for label, source_root in batch.source_roots.items():
        if _paths_overlap(prospective, source_root):
            raise ValueError(
                f"Archive evidence root must be outside archive source root {label}"
            )
    root = _normalize_evidence_root(raw)
    _ensure_directory(root / ".staging", root=root)
    _ensure_directory(root / "cases", root=root)
    return root


def _validate_root_top_level(root: Path) -> None:
    allowed = {_LOCK_NAME, ".staging", "cases"}
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.name not in allowed:
                raise RuntimeError(
                    f"Unexpected archive evidence root entry: {entry.name}"
                )
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise RuntimeError(
                    f"Archive evidence root entry is symlinked: {entry.name}"
                )
            if entry.name in {".staging", "cases"} and not stat.S_ISDIR(
                metadata.st_mode
            ):
                raise RuntimeError(
                    f"Archive evidence root entry is not a directory: {entry.name}"
                )
    staging = root / ".staging"
    if any(staging.iterdir()):
        raise RuntimeError("Archive evidence staging directory is not empty")


def _case_row_matches(row: sqlite3.Row, case: ArchiveEvidenceCasePlan) -> bool:
    return (
        str(row["case_key"]) == case.case_key
        and str(row["legacy_delivery_key"]) == case.legacy_delivery_key
        and str(row["logical_stem"]) == case.logical_stem
        and str(row["subject_abbr"] or "") == case.subject_abbr
    )


def _ensure_case_row(
    conn: sqlite3.Connection,
    case: ArchiveEvidenceCasePlan,
) -> int:
    by_delivery = conn.execute(
        """
        SELECT id, case_key, legacy_delivery_key, logical_stem, subject_abbr
        FROM archive_evidence_cases
        WHERE legacy_delivery_key = ?
        """,
        (case.legacy_delivery_key,),
    ).fetchone()
    by_key = conn.execute(
        """
        SELECT id, case_key, legacy_delivery_key, logical_stem, subject_abbr
        FROM archive_evidence_cases
        WHERE case_key = ?
        """,
        (case.case_key,),
    ).fetchone()
    if by_delivery is not None or by_key is not None:
        if by_delivery is None or by_key is None or int(by_delivery["id"]) != int(by_key["id"]):
            raise RuntimeError("Archive evidence case identity conflicts with existing DB state")
        if not _case_row_matches(by_delivery, case):
            raise RuntimeError("Archive evidence case metadata conflicts with existing DB state")
        return int(by_delivery["id"])
    cursor = conn.execute(
        """
        INSERT INTO archive_evidence_cases(
            case_key,
            legacy_delivery_key,
            logical_stem,
            subject_abbr,
            review_status
        ) VALUES (?, ?, ?, ?, 'open')
        """,
        (
            case.case_key,
            case.legacy_delivery_key,
            case.logical_stem,
            case.subject_abbr,
        ),
    )
    return int(cursor.lastrowid)


def _capture_row(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    capture_key: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM archive_evidence_captures
        WHERE case_id = ? AND capture_key = ?
        """,
        (case_id, capture_key),
    ).fetchone()


def _capture_row_matches(
    row: sqlite3.Row,
    case: ArchiveEvidenceCasePlan,
) -> bool:
    return (
        str(row["reconciliation_classification"])
        == case.reconciliation_classification
        and str(row["legacy_database_sha256"]) == case.legacy_database_sha256
        and str(row["source_fingerprint"]) == case.source_fingerprint
        and str(row["plan_sha256"]) == case.plan_sha256
        and str(row["manifest_relpath"]) == case.manifest_relpath
        and json.loads(str(row["snapshot_json"])) == dict(case.snapshot)
    )


def _planned_path_allowance(
    cases: Sequence[ArchiveEvidenceCasePlan],
) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    for case in cases:
        files.add(_validate_relpath(case.manifest_relpath))
        for revision in case.revisions:
            files.add(_validate_relpath(revision.path_rel))
    for file_path in files:
        parent = PurePosixPath(file_path).parent
        while parent.as_posix() not in {"", "."}:
            directories.add(parent.as_posix())
            parent = parent.parent
    return files, directories


def _insert_capture_rows(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    case: ArchiveEvidenceCasePlan,
) -> int:
    _validate_snapshot_metadata(case.snapshot)
    existing = _capture_row(
        conn,
        case_id=case_id,
        capture_key=case.capture_key,
    )
    if existing is not None:
        if not _capture_row_matches(existing, case):
            raise RuntimeError(
                "Archive evidence capture metadata conflicts with existing DB state"
            )
        return int(existing["id"])

    revision_ids: dict[tuple[str, str], int] = {}
    for revision in case.revisions:
        existing_revision = conn.execute(
            """
            SELECT id, bytes, mime_type, path_rel
            FROM archive_evidence_revisions
            WHERE case_id = ?
              AND artifact_kind = ?
              AND content_sha256 = ?
            """,
            (case_id, revision.artifact_kind, revision.content_sha256),
        ).fetchone()
        if existing_revision is None:
            cursor = conn.execute(
                """
                INSERT INTO archive_evidence_revisions(
                    case_id,
                    artifact_kind,
                    content_sha256,
                    bytes,
                    mime_type,
                    path_rel
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    revision.artifact_kind,
                    revision.content_sha256,
                    revision.bytes,
                    revision.mime_type,
                    revision.path_rel,
                ),
            )
            revision_id = int(cursor.lastrowid)
        else:
            if (
                int(existing_revision["bytes"]) != revision.bytes
                or str(existing_revision["mime_type"] or "") != revision.mime_type
                or str(existing_revision["path_rel"]) != revision.path_rel
            ):
                raise RuntimeError(
                    "Archive evidence revision metadata conflicts with existing DB state"
                )
            revision_id = int(existing_revision["id"])
        revision_ids[(revision.artifact_kind, revision.content_sha256)] = revision_id

    capture_cursor = conn.execute(
        """
        INSERT INTO archive_evidence_captures(
            case_id,
            capture_key,
            reconciliation_classification,
            legacy_database_sha256,
            source_fingerprint,
            plan_sha256,
            manifest_relpath,
            snapshot_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case_id,
            case.capture_key,
            case.reconciliation_classification,
            case.legacy_database_sha256,
            case.source_fingerprint,
            case.plan_sha256,
            case.manifest_relpath,
            _canonical_json(dict(case.snapshot)),
        ),
    )
    capture_id = int(capture_cursor.lastrowid)
    for revision in case.revisions:
        revision_id = revision_ids[
            (revision.artifact_kind, revision.content_sha256)
        ]
        for source in revision.observations:
            if source.observed_sha256 != revision.content_sha256:
                raise RuntimeError(
                    "Archive evidence observation hash does not match its revision"
                )
            conn.execute(
                """
                INSERT INTO archive_evidence_observations(
                    capture_id,
                    case_id,
                    revision_id,
                    source_role,
                    source_root_label,
                    source_relpath,
                    claimed_sha256,
                    observed_sha256,
                    relationship,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capture_id,
                    case_id,
                    revision_id,
                    source.source_role,
                    source.root_label,
                    source.source_relpath,
                    source.claimed_sha256,
                    source.observed_sha256,
                    source.relationship,
                    _canonical_json(_observation_metadata_payload(source.state)),
                ),
            )
    return capture_id


def _verify_case_files_for_plan(
    root: Path,
    case: ArchiveEvidenceCasePlan,
) -> None:
    for revision in case.revisions:
        _verify_exact_file(
            root / revision.path_rel,
            expected_sha256=revision.content_sha256,
            expected_bytes=revision.bytes,
            label="Archive evidence revision",
        )
    manifest_payload = _canonical_json(_manifest_payload(case)).encode("utf-8")
    manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
    _verify_exact_file(
        root / case.manifest_relpath,
        expected_sha256=manifest_sha,
        expected_bytes=len(manifest_payload),
        label="Archive evidence capture manifest",
    )


def _write_case_files(
    root: Path,
    case: ArchiveEvidenceCasePlan,
) -> bool:
    manifest_path = root / case.manifest_relpath
    if os.path.lexists(manifest_path):
        _verify_case_files_for_plan(root, case)
        return False

    staging_root = root / ".staging"
    stage = Path(
        tempfile.mkdtemp(
            prefix=f"{case.capture_key}-",
            dir=staging_root,
        )
    )
    promoted_any = False
    try:
        for revision in case.revisions:
            for source in revision.observations:
                _assert_source_fresh(source)
            final_path = root / revision.path_rel
            _ensure_directory(final_path.parent, root=root)
            if os.path.lexists(final_path):
                _verify_exact_file(
                    final_path,
                    expected_sha256=revision.content_sha256,
                    expected_bytes=revision.bytes,
                    label="Existing archive evidence revision",
                )
                continue
            staged_path = stage / f"{revision.content_sha256}.stage"
            _copy_source_to_stage(revision.observations[0], staged_path)
            try:
                _promote_no_replace(staged_path, final_path)
            except FileExistsError:
                _verify_exact_file(
                    final_path,
                    expected_sha256=revision.content_sha256,
                    expected_bytes=revision.bytes,
                    label="Concurrent archive evidence revision",
                )
                if staged_path.exists():
                    staged_path.unlink()
            promoted_any = True

        payload = _canonical_json(_manifest_payload(case)).encode("utf-8")
        _ensure_directory(manifest_path.parent, root=root)
        staged_manifest = stage / "capture.json"
        _write_stage_bytes(staged_manifest, payload)
        try:
            _promote_no_replace(staged_manifest, manifest_path)
        except FileExistsError:
            expected_sha = hashlib.sha256(payload).hexdigest()
            _verify_exact_file(
                manifest_path,
                expected_sha256=expected_sha,
                expected_bytes=len(payload),
                label="Concurrent archive evidence capture manifest",
            )
            if staged_manifest.exists():
                staged_manifest.unlink()
        promoted_any = True
        _verify_case_files_for_plan(root, case)
        return promoted_any
    finally:
        shutil.rmtree(stage, ignore_errors=False)
        _fsync_directory(staging_root)


@dataclass(frozen=True)
class ArchiveEvidenceApplyResult:
    case_key: str
    capture_key: str
    action: str
    revision_count: int
    manifest_path: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_key": self.case_key,
            "capture_key": self.capture_key,
            "action": self.action,
            "revision_count": self.revision_count,
            "manifest_path": str(self.manifest_path),
        }


def apply_archive_evidence(
    batch: ArchiveEvidencePlanBatch,
    *,
    target_db_path: Path | str,
    evidence_root: Path | str,
) -> list[ArchiveEvidenceApplyResult]:
    blocked = [case for case in batch.cases if not case.can_apply]
    if blocked:
        raise RuntimeError(
            f"Archive evidence plan contains {len(blocked)} unsafe cases"
        )
    _assert_legacy_snapshot(batch)
    for case in batch.cases:
        for revision in case.revisions:
            if not revision.observations:
                raise RuntimeError(
                    f"Archive evidence revision has no source observations: {case.case_key}"
                )
            for source in revision.observations:
                _assert_source_fresh(source)

    prospective_root = Path(evidence_root).expanduser().absolute().resolve()
    target_db = _validate_target_db_path(
        target_db_path,
        batch=batch,
        evidence_root=prospective_root,
    )
    root = _prepare_evidence_root(evidence_root, batch=batch)
    target_db = _validate_target_db_path(
        target_db,
        batch=batch,
        evidence_root=root,
    )
    results: list[ArchiveEvidenceApplyResult] = []
    with _exclusive_evidence_root_lock(root) as root_identity:
        _assert_directory_identity(root, root_identity)
        _validate_root_top_level(root)
        _assert_legacy_snapshot(batch)
        conn = apply_migration(
            target_db,
            legacy_db_path=batch.legacy_db_path,
        )
        try:
            conn.execute("BEGIN IMMEDIATE")
            allowed_files, allowed_directories = _planned_path_allowance(
                batch.cases
            )
            preflight = _verify_archive_evidence_connection(
                conn,
                root,
                allowed_unindexed_files=allowed_files,
                allowed_unindexed_directories=allowed_directories,
            )
            if not preflight["ok"]:
                raise RuntimeError(
                    "Existing archive evidence store failed verification: "
                    + _canonical_json(preflight["issues"])
                )
            for case in batch.cases:
                _assert_directory_identity(root, root_identity)
                for revision in case.revisions:
                    for source in revision.observations:
                        _assert_source_fresh(source)
                case_id = _ensure_case_row(conn, case)
                capture = _capture_row(
                    conn,
                    case_id=case_id,
                    capture_key=case.capture_key,
                )
                manifest_exists = os.path.lexists(root / case.manifest_relpath)
                if capture is not None:
                    if not _capture_row_matches(capture, case):
                        raise RuntimeError(
                            "Archive evidence capture metadata conflicts with existing DB state"
                        )
                    _verify_case_files_for_plan(root, case)
                    action = "skipped"
                else:
                    _write_case_files(root, case)
                    _insert_capture_rows(
                        conn,
                        case_id=case_id,
                        case=case,
                    )
                    action = "recovered" if manifest_exists else "imported"
                results.append(
                    ArchiveEvidenceApplyResult(
                        case_key=case.case_key,
                        capture_key=case.capture_key,
                        action=action,
                        revision_count=len(case.revisions),
                        manifest_path=root / case.manifest_relpath,
                    )
                )
            _assert_legacy_snapshot(batch)
            _assert_directory_identity(root, root_identity)
            verification = _verify_archive_evidence_connection(conn, root)
            if not verification["ok"]:
                raise RuntimeError(
                    "Archive evidence store failed verification before commit: "
                    + _canonical_json(verification["issues"])
                )
            conn.commit()
            _assert_directory_identity(root, root_identity)
            _fsync_directory(root)
            _fsync_directory(target_db.parent)
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        _validate_root_top_level(root)
    return results


def _verification_issue(
    code: str,
    message: str,
    *,
    path: Path | str | None = None,
) -> dict[str, str]:
    payload = {"code": code, "message": message}
    if path is not None:
        payload["path"] = str(path)
    return payload


def _contains_forbidden_body_key(value: Any) -> bool:
    forbidden = {
        "body",
        "content",
        "raw_text",
        "segments",
        "transcript",
        "transcript_text",
    }
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in forbidden:
                return True
            if _contains_forbidden_body_key(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_body_key(item) for item in value)
    return False


def _read_exact_json_file(
    path: Path,
    *,
    label: str,
) -> tuple[bytes, Any]:
    descriptor, before = _open_regular_single_link(path, label=label)
    try:
        if before.st_size > _MAX_MANIFEST_BYTES:
            raise RuntimeError(
                f"{label} exceeds {_MAX_MANIFEST_BYTES} bytes: {path}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 256 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_MANIFEST_BYTES:
                raise RuntimeError(
                    f"{label} exceeds {_MAX_MANIFEST_BYTES} bytes: {path}"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()
        if _file_state(after) != _file_state(before) or _file_state(current) != _file_state(before):
            raise RuntimeError(f"{label} changed while reading: {path}")
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is not canonical UTF-8 JSON: {path}") from exc
    return payload, parsed


def _capture_manifest_from_database(
    conn: sqlite3.Connection,
    capture: sqlite3.Row,
) -> dict[str, Any]:
    case = conn.execute(
        """
        SELECT case_key, legacy_delivery_key, logical_stem, subject_abbr
        FROM archive_evidence_cases
        WHERE id = ?
        """,
        (int(capture["case_id"]),),
    ).fetchone()
    if case is None:
        raise RuntimeError("Archive evidence capture has no owning case")
    observation_rows = conn.execute(
        """
        SELECT
            r.artifact_kind,
            r.content_sha256,
            r.bytes,
            r.mime_type,
            r.path_rel,
            o.source_role,
            o.source_root_label,
            o.source_relpath,
            o.claimed_sha256,
            o.observed_sha256,
            o.relationship,
            o.metadata_json
        FROM archive_evidence_observations AS o
        JOIN archive_evidence_revisions AS r
          ON r.id = o.revision_id
         AND r.case_id = o.case_id
        WHERE o.capture_id = ?
        ORDER BY
            r.artifact_kind,
            r.content_sha256,
            o.source_role,
            o.source_root_label,
            o.source_relpath
        """,
        (int(capture["id"]),),
    ).fetchall()
    revisions: dict[tuple[str, str], dict[str, Any]] = {}
    observations: list[dict[str, Any]] = []
    for row in observation_rows:
        if row["metadata_json"] is None:
            raise RuntimeError(
                "Archive evidence observation metadata is missing"
            )
        try:
            observation_metadata = json.loads(str(row["metadata_json"]))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Archive evidence observation metadata is not valid JSON"
            ) from exc
        _validate_observation_metadata(observation_metadata)
        if int(observation_metadata["state"]["bytes"]) != int(row["bytes"]):
            raise RuntimeError(
                "Archive evidence observation metadata byte size does not "
                "match its revision"
            )
        revision_key = (
            str(row["artifact_kind"]),
            str(row["content_sha256"]),
        )
        revisions[revision_key] = {
            "artifact_kind": revision_key[0],
            "content_sha256": revision_key[1],
            "bytes": int(row["bytes"]),
            "mime_type": str(row["mime_type"]),
            "path_rel": str(row["path_rel"]),
        }
        observations.append(
            {
                "artifact_kind": revision_key[0],
                "content_sha256": revision_key[1],
                "source_role": str(row["source_role"]),
                "source_root_label": str(row["source_root_label"]),
                "source_relpath": str(row["source_relpath"]),
                "claimed_sha256": (
                    None
                    if row["claimed_sha256"] is None
                    else str(row["claimed_sha256"])
                ),
                "observed_sha256": str(row["observed_sha256"]),
                "relationship": str(row["relationship"]),
            }
        )
    try:
        snapshot = json.loads(str(capture["snapshot_json"]))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Archive evidence capture snapshot is not valid JSON"
        ) from exc
    _validate_snapshot_metadata(snapshot)
    return {
        "schema_version": "storage-v2/archive-evidence-capture@1",
        "case": {
            "case_key": str(case["case_key"]),
            "legacy_delivery_key": str(case["legacy_delivery_key"]),
            "logical_stem": str(case["logical_stem"]),
            "subject_abbr": str(case["subject_abbr"] or ""),
            "review_status": "open",
        },
        "capture": {
            "capture_key": str(capture["capture_key"]),
            "reconciliation_classification": str(
                capture["reconciliation_classification"]
            ),
            "legacy_database_sha256": str(capture["legacy_database_sha256"]),
            "source_fingerprint": str(capture["source_fingerprint"]),
            "plan_sha256": str(capture["plan_sha256"]),
            "manifest_relpath": str(capture["manifest_relpath"]),
        },
        "snapshot": snapshot,
        "revisions": [revisions[key] for key in sorted(revisions)],
        "observations": observations,
        "issues": [],
    }


def _expected_evidence_paths(
    conn: sqlite3.Connection,
) -> tuple[set[str], set[str]]:
    files = {_LOCK_NAME}
    directories = {".staging", "cases"}
    for row in conn.execute(
        "SELECT path_rel FROM archive_evidence_revisions ORDER BY path_rel"
    ).fetchall():
        files.add(_validate_relpath(str(row["path_rel"])))
    for row in conn.execute(
        "SELECT manifest_relpath FROM archive_evidence_captures ORDER BY manifest_relpath"
    ).fetchall():
        files.add(_validate_relpath(str(row["manifest_relpath"])))
    for file_path in files:
        if file_path == _LOCK_NAME:
            continue
        parent = PurePosixPath(file_path).parent
        while parent.as_posix() not in {"", "."}:
            directories.add(parent.as_posix())
            parent = parent.parent
    return files, directories


def _scan_evidence_root(
    root: Path,
    *,
    expected_files: set[str],
    expected_directories: set[str],
    allowed_unindexed_files: set[str] | None = None,
    allowed_unindexed_directories: set[str] | None = None,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    seen_files: set[str] = set()
    seen_directories: set[str] = set()
    stack = [root]
    scanned = 0
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                scanned += 1
                if scanned > _MAX_VERIFY_ENTRIES:
                    issues.append(
                        _verification_issue(
                            "archive_evidence_scan_limit_exceeded",
                            f"Archive evidence tree exceeds {_MAX_VERIFY_ENTRIES} entries",
                            path=root,
                        )
                    )
                    return issues
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    issues.append(
                        _verification_issue(
                            "archive_evidence_entry_unreadable",
                            str(exc),
                            path=path,
                        )
                    )
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    issues.append(
                        _verification_issue(
                            "archive_evidence_symlink",
                            "Archive evidence entries must not be symlinks",
                            path=path,
                        )
                    )
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    seen_directories.add(relative)
                    if (
                        relative not in expected_directories
                        and relative not in (allowed_unindexed_directories or set())
                    ):
                        issues.append(
                            _verification_issue(
                                "unexpected_archive_evidence_directory",
                                "Directory is not indexed by the archive evidence DB",
                                path=path,
                            )
                        )
                    stack.append(path)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    issues.append(
                        _verification_issue(
                            "archive_evidence_special_entry",
                            "Archive evidence entries must be regular files or directories",
                            path=path,
                        )
                    )
                    continue
                if metadata.st_nlink != 1:
                    issues.append(
                        _verification_issue(
                            "archive_evidence_hardlink",
                            "Archive evidence files must have exactly one link",
                            path=path,
                        )
                    )
                seen_files.add(relative)
                if (
                    relative not in expected_files
                    and relative not in (allowed_unindexed_files or set())
                ):
                    issues.append(
                        _verification_issue(
                            "unexpected_archive_evidence_file",
                            "File is not indexed by the archive evidence DB",
                            path=path,
                        )
                    )
    for relative in sorted(expected_files.difference(seen_files)):
        issues.append(
            _verification_issue(
                "archive_evidence_file_missing",
                "Indexed archive evidence file is missing",
                path=root / relative,
            )
        )
    for relative in sorted(expected_directories.difference(seen_directories)):
        if relative in {".staging", "cases"}:
            issues.append(
                _verification_issue(
                    "archive_evidence_directory_missing",
                    "Required archive evidence directory is missing",
                    path=root / relative,
                )
            )
    staging = root / ".staging"
    if staging.is_dir() and any(staging.iterdir()):
        issues.append(
            _verification_issue(
                "archive_evidence_staging_not_empty",
                "Archive evidence staging directory is not empty",
                path=staging,
            )
        )
    return issues


def _verify_archive_evidence_connection(
    conn: sqlite3.Connection,
    root: Path,
    *,
    allowed_unindexed_files: set[str] | None = None,
    allowed_unindexed_directories: set[str] | None = None,
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    quick_check = [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
    if quick_check != ["ok"]:
        issues.append(
            _verification_issue(
                "archive_evidence_database_quick_check_failed",
                ", ".join(quick_check or ["no result"]),
            )
        )
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    for row in foreign_keys:
        issues.append(
            _verification_issue(
                "archive_evidence_foreign_key_violation",
                f"table={row[0]} rowid={row[1]} parent={row[2]}",
            )
        )

    case_count = int(
        conn.execute("SELECT COUNT(*) FROM archive_evidence_cases").fetchone()[0]
    )
    capture_rows = conn.execute(
        "SELECT * FROM archive_evidence_captures ORDER BY id"
    ).fetchall()
    revision_rows = conn.execute(
        """
        SELECT r.*, c.case_key
        FROM archive_evidence_revisions AS r
        JOIN archive_evidence_cases AS c ON c.id = r.case_id
        ORDER BY r.id
        """
    ).fetchall()
    observation_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM archive_evidence_observations"
        ).fetchone()[0]
    )
    selection_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM archive_evidence_canonical_selections"
        ).fetchone()[0]
    )
    promotion_state_issues = conn.execute(
        """
        SELECT
            c.id,
            c.case_key,
            c.review_status,
            c.promoted_recording_id,
            c.resolved_at,
            COUNT(selection.id) AS selection_count,
            COUNT(DISTINCT selection.promotion_plan_sha256) AS plan_count
        FROM archive_evidence_cases AS c
        LEFT JOIN archive_evidence_canonical_selections AS selection
          ON selection.case_id = c.id
        GROUP BY c.id
        HAVING
            (
                c.promoted_recording_id IS NULL
                AND COUNT(selection.id) <> 0
            )
            OR (
                c.promoted_recording_id IS NOT NULL
                AND (
                    c.review_status <> 'resolved'
                    OR c.resolved_at IS NULL
                    OR COUNT(selection.id) = 0
                    OR COUNT(DISTINCT selection.promotion_plan_sha256) <> 1
                )
            )
        """
    ).fetchall()
    for row in promotion_state_issues:
        issues.append(
            _verification_issue(
                "archive_evidence_canonical_selection_invalid",
                f"Case {row['case_key']} has inconsistent canonical promotion metadata",
            )
        )
    orphan_revisions = conn.execute(
        """
        SELECT r.id
        FROM archive_evidence_revisions AS r
        LEFT JOIN archive_evidence_observations AS o
          ON o.revision_id = r.id AND o.case_id = r.case_id
        WHERE o.id IS NULL
        """
    ).fetchall()
    for row in orphan_revisions:
        issues.append(
            _verification_issue(
                "archive_evidence_revision_without_observation",
                f"Revision {row['id']} has no provenance observation",
            )
        )
    mismatches = conn.execute(
        """
        SELECT o.id
        FROM archive_evidence_observations AS o
        JOIN archive_evidence_revisions AS r
          ON r.id = o.revision_id AND r.case_id = o.case_id
        WHERE o.observed_sha256 <> r.content_sha256
        """
    ).fetchall()
    for row in mismatches:
        issues.append(
            _verification_issue(
                "archive_evidence_observation_revision_hash_mismatch",
                f"Observation {row['id']} does not match its revision hash",
            )
        )

    for revision in revision_rows:
        extension = {
            "correction_text": ".txt",
            "correction_json": ".json",
            "summary_markdown": ".md",
        }.get(str(revision["artifact_kind"]))
        expected_relpath = (
            None
            if extension is None
            else (
                f"cases/{revision['case_key']}/revisions/"
                f"{revision['artifact_kind']}/{revision['content_sha256']}{extension}"
            )
        )
        if expected_relpath != str(revision["path_rel"]):
            issues.append(
                _verification_issue(
                    "archive_evidence_revision_path_noncanonical",
                    "Revision path does not match its case, kind, and hash",
                    path=root / str(revision["path_rel"]),
                )
            )
        path = root / _validate_relpath(str(revision["path_rel"]))
        try:
            _verify_exact_file(
                path,
                expected_sha256=str(revision["content_sha256"]),
                expected_bytes=int(revision["bytes"]),
                label="Archive evidence revision",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            issues.append(
                _verification_issue(
                    "archive_evidence_revision_invalid",
                    str(exc),
                    path=path,
                )
            )

    for capture in capture_rows:
        case_key_row = conn.execute(
            "SELECT case_key FROM archive_evidence_cases WHERE id = ?",
            (int(capture["case_id"]),),
        ).fetchone()
        expected_manifest_relpath = (
            f"cases/{case_key_row['case_key']}/captures/"
            f"{capture['capture_key']}.json"
            if case_key_row is not None
            else ""
        )
        if expected_manifest_relpath != str(capture["manifest_relpath"]):
            issues.append(
                _verification_issue(
                    "archive_evidence_manifest_path_noncanonical",
                    "Capture manifest path does not match its case and capture keys",
                    path=root / str(capture["manifest_relpath"]),
                )
            )
        path = root / _validate_relpath(str(capture["manifest_relpath"]))
        try:
            expected = _capture_manifest_from_database(conn, capture)
            if _contains_forbidden_body_key(expected):
                raise RuntimeError(
                    "Archive evidence DB snapshot contains forbidden body fields"
                )
            raw, actual = _read_exact_json_file(
                path,
                label="Archive evidence capture manifest",
            )
            expected_raw = _canonical_json(expected).encode("utf-8")
            if actual != expected or raw != expected_raw:
                raise RuntimeError(
                    "Archive evidence capture manifest does not match the DB snapshot"
                )
            if _contains_forbidden_body_key(actual):
                raise RuntimeError(
                    "Archive evidence capture manifest contains forbidden body fields"
                )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            issues.append(
                _verification_issue(
                    "archive_evidence_manifest_invalid",
                    str(exc),
                    path=path,
                )
            )

    try:
        expected_files, expected_directories = _expected_evidence_paths(conn)
        issues.extend(
            _scan_evidence_root(
                root,
                expected_files=expected_files,
                expected_directories=expected_directories,
                allowed_unindexed_files=allowed_unindexed_files,
                allowed_unindexed_directories=allowed_unindexed_directories,
            )
        )
    except (OSError, RuntimeError, ValueError) as exc:
        issues.append(
            _verification_issue(
                "archive_evidence_root_scan_failed",
                str(exc),
                path=root,
            )
        )
    return {
        "schema_version": "storage-v2/archive-evidence-verification@1",
        "ok": not issues,
        "checked_cases": case_count,
        "checked_captures": len(capture_rows),
        "checked_revisions": len(revision_rows),
        "checked_observations": observation_count,
        "checked_canonical_selections": selection_count,
        "issues": issues,
    }


def verify_archive_evidence(
    db_path: Path | str,
    evidence_root: Path | str,
) -> dict[str, Any]:
    root_input = Path(evidence_root).expanduser().absolute()
    try:
        if _has_symlink_component(root_input):
            raise RuntimeError("Archive evidence root contains a symlink")
        root = root_input.resolve(strict=True)
        if not root.is_dir():
            raise RuntimeError("Archive evidence root is not a directory")
    except (OSError, RuntimeError) as exc:
        return {
            "schema_version": "storage-v2/archive-evidence-verification@1",
            "ok": False,
            "checked_cases": 0,
            "checked_captures": 0,
            "checked_revisions": 0,
            "checked_observations": 0,
            "checked_canonical_selections": 0,
            "issues": [
                _verification_issue(
                    "archive_evidence_root_invalid",
                    str(exc),
                    path=root_input,
                )
            ],
        }
    lock_path = root / _LOCK_NAME
    try:
        root_identity = _directory_identity(root)
        lock_fd, lock_state = _open_regular_single_link(
            lock_path,
            label="Archive evidence lock",
        )
    except (OSError, RuntimeError) as exc:
        return {
            "schema_version": "storage-v2/archive-evidence-verification@1",
            "ok": False,
            "checked_cases": 0,
            "checked_captures": 0,
            "checked_revisions": 0,
            "checked_observations": 0,
            "checked_canonical_selections": 0,
            "issues": [
                _verification_issue(
                    "archive_evidence_lock_invalid",
                    str(exc),
                    path=lock_path,
                )
            ],
        }
    try:
        if lock_state.st_size != 0:
            raise RuntimeError("Archive evidence lock file is not empty")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _assert_directory_identity(root, root_identity)
        conn = connect_v2(db_path, readonly=True)
        try:
            require_v2_schema(conn)
            conn.execute("BEGIN")
            result = _verify_archive_evidence_connection(conn, root)
            _assert_directory_identity(root, root_identity)
            conn.rollback()
            return result
        finally:
            conn.close()
    except (OSError, RuntimeError, sqlite3.DatabaseError, ValueError) as exc:
        return {
            "schema_version": "storage-v2/archive-evidence-verification@1",
            "ok": False,
            "checked_cases": 0,
            "checked_captures": 0,
            "checked_revisions": 0,
            "checked_observations": 0,
            "checked_canonical_selections": 0,
            "issues": [
                _verification_issue(
                    "archive_evidence_verification_failed",
                    str(exc),
                )
            ],
        }
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def read_archive_evidence_snapshot(
    db_path: Path | str,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    if limit < 0 or limit > 500:
        raise ValueError("limit must be between 0 and 500")
    path = Path(db_path).expanduser()
    empty_counts = {
        "cases": 0,
        "open": 0,
        "triaged": 0,
        "resolved": 0,
        "dismissed": 0,
        "captures": 0,
        "revisions": 0,
        "observations": 0,
        "canonical_selections": 0,
    }
    if not path.exists():
        return {
            "schema_version": "storage-v2/archive-evidence-snapshot@1",
            "available": False,
            "counts": empty_counts,
            "cases": [],
        }
    conn = connect_v2(path, readonly=True)
    try:
        require_v2_schema(conn)
        status_counts = {
            str(row["review_status"]): int(row["count"])
            for row in conn.execute(
                """
                SELECT review_status, COUNT(*) AS count
                FROM archive_evidence_cases
                GROUP BY review_status
                """
            ).fetchall()
        }
        counts = {
            "cases": int(
                conn.execute(
                    "SELECT COUNT(*) FROM archive_evidence_cases"
                ).fetchone()[0]
            ),
            "open": status_counts.get("open", 0),
            "triaged": status_counts.get("triaged", 0),
            "resolved": status_counts.get("resolved", 0),
            "dismissed": status_counts.get("dismissed", 0),
            "captures": int(
                conn.execute(
                    "SELECT COUNT(*) FROM archive_evidence_captures"
                ).fetchone()[0]
            ),
            "revisions": int(
                conn.execute(
                    "SELECT COUNT(*) FROM archive_evidence_revisions"
                ).fetchone()[0]
            ),
            "observations": int(
                conn.execute(
                    "SELECT COUNT(*) FROM archive_evidence_observations"
                ).fetchone()[0]
            ),
            "canonical_selections": int(
                conn.execute(
                    "SELECT COUNT(*) FROM archive_evidence_canonical_selections"
                ).fetchone()[0]
            ),
        }
        rows = conn.execute(
            """
            SELECT
                c.case_key,
                c.logical_stem,
                c.subject_abbr,
                c.review_status,
                c.promoted_recording_id,
                COUNT(DISTINCT cap.id) AS capture_count,
                COUNT(DISTINCT rev.id) AS revision_count,
                COUNT(DISTINCT selection.id) AS canonical_selection_count,
                MAX(cap.captured_at) AS last_captured_at
            FROM archive_evidence_cases AS c
            LEFT JOIN archive_evidence_captures AS cap
              ON cap.case_id = c.id
            LEFT JOIN archive_evidence_revisions AS rev
              ON rev.case_id = c.id
            LEFT JOIN archive_evidence_canonical_selections AS selection
              ON selection.case_id = c.id
            GROUP BY c.id
            ORDER BY
                CASE c.review_status
                    WHEN 'open' THEN 0
                    WHEN 'triaged' THEN 1
                    ELSE 2
                END,
                COALESCE(MAX(cap.captured_at), c.created_at) DESC,
                c.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "schema_version": "storage-v2/archive-evidence-snapshot@1",
        "available": True,
        "counts": counts,
        "cases": [dict(row) for row in rows],
    }
