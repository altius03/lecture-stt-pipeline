from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence
import uuid

from lecture_stt.stt.controller_readiness import (
    ControllerReadinessError,
    REPORT_SCHEMA_VERSION,
    validate_controller_success_report,
)


START_SCHEMA_VERSION = "lecture-stt/controller-shadow-attempt-start@1"
FINISH_SCHEMA_VERSION = "lecture-stt/controller-shadow-attempt-finish@1"
RESULT_SCHEMA_VERSION = "lecture-stt/controller-shadow-observation-result@1"
SUMMARY_SCHEMA_VERSION = "lecture-stt/controller-shadow-evidence-summary@1"

LOCK_FILENAME = "journal.lock"
MAX_ATTEMPTS = 10_000
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_BINARY_BYTES = 128 * 1024 * 1024
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_TIMEOUT_SEC = 3_600
ZERO_SHA256 = "0" * 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ATTEMPT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_RECORD_NAME_RE = re.compile(r"^(?P<sequence>\d{8})\.(?P<kind>start|finish)\.json$")
_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$"
)
_DIRECT_NAME_REJECT_SUFFIXES = (".tmp", ".part")
_SUCCESS_REPORT_KEYS = {
    "completed_at",
    "kill_switch_configured",
    "mode",
    "ok",
    "plan_checks",
    "polling_match",
    "run_id",
    "scan_comparisons",
    "scan_count",
    "schema_version",
    "started_at",
    "verified_count",
}
_SCAN_KEYS = {
    "go_stable_relative_paths",
    "match",
    "python_stable_relative_paths",
    "scan_index",
}
_FAILURE_ERROR_KINDS = {
    "config_probe_failed",
    "go_scan_failed",
    "interrupted",
    "invalid_config_probe",
    "invalid_options",
    "invalid_schedule",
    "kill_switch_active",
    "kill_switch_invalid",
    "run_metadata_failed",
    "scan_hook_failed",
    "scan_probe_failed",
    "shadow_failed",
    "shadow_limit_exceeded",
    "shadow_mismatch",
}
_OUTCOMES = {
    "failed",
    "input_invalid",
    "input_changed",
    "interrupted",
    "invalid_report",
    "missing_report",
    "spawn_failed",
    "success",
    "timeout",
}
_START_KEYS = {
    "attempt_id",
    "binding_sha256",
    "previous_record_sha256",
    "record_sha256",
    "schema_version",
    "sequence",
    "started_at",
}
_FINISH_KEYS = {
    "attempt_id",
    "completed_at",
    "execution_sha256",
    "exit_code",
    "outcome",
    "previous_record_sha256",
    "record_sha256",
    "report",
    "report_sha256",
    "raw_report_bytes",
    "raw_report_sha256",
    "runner_status",
    "schema_version",
    "sequence",
}
_RUNNER_STATUSES = {
    "completed",
    "interrupted",
    "not_started",
    "spawn_failed",
    "timeout",
}
_REJECTED_PLAN_ERROR_KINDS = {
    "digest_mismatch",
    "evidence_mismatch",
    "invalid_plan",
    "plan_verification_failed",
    "python_plan_rejected",
    "relative_path_mismatch",
    "source_rejected",
}


class ControllerEvidenceError(RuntimeError):
    """Closed failure for the isolated controller attempt journal."""


class ControllerEvidenceWriteDisabledError(ControllerEvidenceError):
    """Raised when an evidence-writing guard is absent."""


class ControllerEvidenceBusyError(ControllerEvidenceError):
    """Raised when another observation owns the journal lock."""


@dataclass(frozen=True)
class _FileEvidence:
    path: Path
    sha256: str
    identity: dict[str, int]


@dataclass(frozen=True)
class _JournalState:
    starts: dict[int, dict[str, Any]]
    finishes: dict[int, dict[str, Any]]
    head_sha256: str
    raw_sha256: str


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (_canonical_json(value) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _record_payload(unsigned: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(unsigned)
    result["record_sha256"] = _sha256_json(unsigned)
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ControllerEvidenceError(f"{label} has an invalid closed key set")


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ControllerEvidenceError(f"{label} must be an object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ControllerEvidenceError(f"{label} must be a non-empty string")
    return value


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ControllerEvidenceError(f"{label} must be a boolean")
    return value


def _require_int(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ControllerEvidenceError(f"{label} is outside the closed integer range")
    return value


def _require_sha256(value: Any, label: str) -> str:
    digest = _require_string(value, label)
    if _SHA256_RE.fullmatch(digest) is None:
        raise ControllerEvidenceError(f"{label} must be a lowercase SHA-256")
    return digest


def _require_timestamp(value: Any, label: str) -> str:
    raw = _require_string(value, label)
    if _UTC_RE.fullmatch(raw) is None:
        raise ControllerEvidenceError(f"{label} must be a UTC RFC3339 timestamp")
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControllerEvidenceError(
            f"{label} must be a UTC RFC3339 timestamp"
        ) from exc
    return raw


def _timestamp_value(value: Any, label: str) -> datetime:
    raw = _require_timestamp(value, label)
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _absolute_path(value: str | Path, *, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ControllerEvidenceError(f"{label} must be absolute")
    return Path(os.path.abspath(os.fspath(candidate)))


def _temporary_base() -> Path:
    return Path(tempfile.gettempdir()).resolve(strict=True)


def _require_temporary_path(path: Path, *, label: str) -> None:
    base = _temporary_base()
    try:
        common = Path(os.path.commonpath((os.fspath(path), os.fspath(base))))
    except ValueError as exc:
        raise ControllerEvidenceError(
            f"{label} must be inside the system temporary directory"
        ) from exc
    if common != base or path == base:
        raise ControllerEvidenceError(
            f"{label} must be inside the system temporary directory"
        )


def _real_directory(
    value: str | Path,
    *,
    label: str,
    require_temporary: bool,
) -> tuple[Path, dict[str, int]]:
    requested = _absolute_path(value, label=label)
    try:
        requested_stat = requested.lstat()
    except OSError as exc:
        raise ControllerEvidenceError(f"{label} is not safely inspectable") from exc
    if stat.S_ISLNK(requested_stat.st_mode):
        raise ControllerEvidenceError(f"{label} must not be a symlink")
    resolved = Path(os.path.realpath(os.fspath(requested)))
    try:
        observed = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise ControllerEvidenceError(f"{label} is not safely inspectable") from exc
    if not stat.S_ISDIR(observed.st_mode):
        raise ControllerEvidenceError(f"{label} must be a directory")
    if require_temporary:
        _require_temporary_path(resolved, label=label)
    return resolved, {
        "device": int(observed.st_dev),
        "inode": int(observed.st_ino),
        "mode": int(observed.st_mode),
    }


def _stable_file(
    value: str | Path,
    *,
    label: str,
    maximum_bytes: int,
    executable: bool,
    require_temporary: bool,
    allow_leaf_symlink: bool = False,
) -> _FileEvidence:
    requested = _absolute_path(value, label=label)
    try:
        requested_stat = requested.lstat()
    except OSError as exc:
        raise ControllerEvidenceError(f"{label} is not safely inspectable") from exc
    if stat.S_ISLNK(requested_stat.st_mode) and not allow_leaf_symlink:
        raise ControllerEvidenceError(f"{label} must not be a symlink")
    resolved = Path(os.path.realpath(os.fspath(requested)))
    if require_temporary:
        _require_temporary_path(resolved, label=label)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise ControllerEvidenceError(f"{label} is not safely openable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ControllerEvidenceError(
                f"{label} must be a regular single-link file"
            )
        if before.st_size <= 0 or before.st_size > maximum_bytes:
            raise ControllerEvidenceError(f"{label} size is outside the closed limit")
        if executable and before.st_mode & 0o111 == 0:
            raise ControllerEvidenceError(f"{label} must be executable")
        digest = hashlib.sha256()
        bytes_read = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - bytes_read))
            if not chunk:
                break
            digest.update(chunk)
            bytes_read += len(chunk)
            if bytes_read > maximum_bytes:
                raise ControllerEvidenceError(
                    f"{label} size is outside the closed limit"
                )
        after = os.fstat(descriptor)
        current = os.stat(resolved, follow_symlinks=False)
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            bytes_read != after.st_size
            or any(getattr(before, field) != getattr(after, field) for field in fields)
            or any(getattr(after, field) != getattr(current, field) for field in fields)
        ):
            raise ControllerEvidenceError(f"{label} changed while it was read")
        return _FileEvidence(
            path=resolved,
            sha256=digest.hexdigest(),
            identity={
                "device": int(after.st_dev),
                "inode": int(after.st_ino),
                "mode": int(after.st_mode),
                "nlink": int(after.st_nlink),
                "size": int(after.st_size),
                "mtime_ns": int(after.st_mtime_ns),
                "ctime_ns": int(after.st_ctime_ns),
            },
        )
    finally:
        os.close(descriptor)


def _absent_kill_switch(value: str | Path) -> Path:
    requested = _absolute_path(value, label="kill switch")
    resolved_parent, _ = _real_directory(
        requested.parent,
        label="kill switch parent",
        require_temporary=True,
    )
    path = resolved_parent / requested.name
    _require_temporary_path(path, label="kill switch")
    try:
        path.lstat()
    except FileNotFoundError:
        return path
    except OSError as exc:
        raise ControllerEvidenceError("kill switch is not safely inspectable") from exc
    raise ControllerEvidenceError("kill switch must be absent")


def _open_root(path: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ControllerEvidenceError("journal root is not safely openable") from exc
    try:
        observed = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        stable_fields = ("st_dev", "st_ino", "st_mode", "st_uid")
        if (
            not stat.S_ISDIR(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != 0o700
            or observed.st_uid != os.geteuid()
            or any(
                getattr(observed, field) != getattr(current, field)
                for field in stable_fields
            )
        ):
            raise ControllerEvidenceError(
                "journal root must be an owned stable 0700 directory"
            )
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_lock(root_fd: int, *, create: bool, exclusive: bool) -> int:
    flags = (
        (os.O_RDWR if create or exclusive else os.O_RDONLY)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if create:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(LOCK_FILENAME, flags, 0o600, dir_fd=root_fd)
    except OSError as exc:
        raise ControllerEvidenceError("journal lock is not safely openable") from exc
    observed = os.fstat(descriptor)
    try:
        current = os.stat(
            LOCK_FILENAME,
            dir_fd=root_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        os.close(descriptor)
        raise ControllerEvidenceError(
            "journal lock pathname is not safely inspectable"
        ) from exc
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or stat.S_IMODE(observed.st_mode) != 0o600
        or observed.st_dev != current.st_dev
        or observed.st_ino != current.st_ino
        or observed.st_mode != current.st_mode
        or observed.st_nlink != current.st_nlink
    ):
        os.close(descriptor)
        raise ControllerEvidenceError(
            "journal lock must be a 0600 regular single-link file"
        )
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if exclusive:
        operation |= fcntl.LOCK_NB
    try:
        fcntl.flock(descriptor, operation)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise ControllerEvidenceBusyError("controller evidence journal is busy") from exc
    except OSError as exc:
        os.close(descriptor)
        raise ControllerEvidenceError("journal lock could not be acquired") from exc
    try:
        current = os.stat(
            LOCK_FILENAME,
            dir_fd=root_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        os.close(descriptor)
        raise ControllerEvidenceError(
            "journal lock pathname changed after acquisition"
        ) from exc
    if (
        observed.st_dev != current.st_dev
        or observed.st_ino != current.st_ino
        or observed.st_mode != current.st_mode
        or current.st_nlink != 1
    ):
        os.close(descriptor)
        raise ControllerEvidenceError(
            "journal lock pathname changed after acquisition"
        )
    return descriptor


def _read_record(root_fd: int, filename: str) -> tuple[dict[str, Any], bytes]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(filename, flags, dir_fd=root_fd)
    except OSError as exc:
        raise ControllerEvidenceError("journal record is not safely openable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o400
            or before.st_size <= 0
            or before.st_size > MAX_RECORD_BYTES
        ):
            raise ControllerEvidenceError("journal record file contract is invalid")
        chunks: list[bytes] = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        current = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(raw) != after.st_size
            or any(getattr(before, field) != getattr(after, field) for field in fields)
            or any(getattr(after, field) != getattr(current, field) for field in fields)
        ):
            raise ControllerEvidenceError("journal record changed while it was read")
    finally:
        os.close(descriptor)
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise ControllerEvidenceError("journal record must contain one complete JSON line")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ControllerEvidenceError("journal record contains duplicate keys")
            result[key] = value
        return result

    try:
        decoded = raw.decode("utf-8", errors="strict")
        parsed = json.loads(decoded, object_pairs_hook=reject_duplicates)
    except ControllerEvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControllerEvidenceError("journal record is not valid canonical JSON") from exc
    record = dict(_require_mapping(parsed, "journal record"))
    if _json_bytes(record) != raw:
        raise ControllerEvidenceError("journal record is not canonical JSON")
    return record, raw


def _write_record(root_fd: int, filename: str, record: Mapping[str, Any]) -> bytes:
    raw = _json_bytes(record)
    if len(raw) > MAX_RECORD_BYTES:
        raise ControllerEvidenceError("journal record exceeds the closed size limit")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(filename, flags, 0o400, dir_fd=root_fd)
    except OSError as exc:
        raise ControllerEvidenceError(
            "journal record could not be created exclusively"
        ) from exc
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ControllerEvidenceError("journal record write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        current = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
        if (
            after.st_dev != current.st_dev
            or after.st_ino != current.st_ino
            or after.st_size != current.st_size
            or after.st_nlink != 1
        ):
            raise ControllerEvidenceError(
                "journal record pathname changed while it was written"
            )
    except OSError as exc:
        raise ControllerEvidenceError("journal record could not be written durably") from exc
    finally:
        os.close(descriptor)
    try:
        os.fsync(root_fd)
    except OSError as exc:
        raise ControllerEvidenceError(
            "journal root could not be synchronized durably"
        ) from exc
    return raw


def _validate_record_hash(record: Mapping[str, Any], *, label: str) -> str:
    digest = _require_sha256(record.get("record_sha256"), f"{label}.record_sha256")
    unsigned = dict(record)
    unsigned.pop("record_sha256")
    if _sha256_json(unsigned) != digest:
        raise ControllerEvidenceError(f"{label} record SHA-256 does not match")
    return digest


def _validate_start(record: Mapping[str, Any], *, sequence: int) -> str:
    label = f"attempt {sequence} start"
    _require_exact_keys(record, _START_KEYS, label)
    if record.get("schema_version") != START_SCHEMA_VERSION:
        raise ControllerEvidenceError(f"{label} has an unsupported schema")
    if _require_int(
        record.get("sequence"),
        f"{label}.sequence",
        minimum=1,
        maximum=MAX_ATTEMPTS,
    ) != sequence:
        raise ControllerEvidenceError(f"{label} sequence does not match its filename")
    attempt_id = _require_string(record.get("attempt_id"), f"{label}.attempt_id")
    if _ATTEMPT_ID_RE.fullmatch(attempt_id) is None:
        raise ControllerEvidenceError(f"{label}.attempt_id is invalid")
    _require_timestamp(record.get("started_at"), f"{label}.started_at")
    _require_sha256(
        record.get("previous_record_sha256"),
        f"{label}.previous_record_sha256",
    )
    _require_sha256(record.get("binding_sha256"), f"{label}.binding_sha256")
    return _validate_record_hash(record, label=label)


def _validate_direct_name(value: Any, label: str) -> str:
    name = _require_string(value, label)
    lowered = name.lower()
    if (
        name in {".", ".."}
        or name.startswith((".", "~"))
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or lowered.endswith(_DIRECT_NAME_REJECT_SUFFIXES)
    ):
        raise ControllerEvidenceError(f"{label} is not a safe direct child name")
    return name


def _validate_path_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ControllerEvidenceError(f"{label} must be an array")
    paths = [
        _validate_direct_name(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    ]
    if paths != sorted(set(paths)):
        raise ControllerEvidenceError(f"{label} must be sorted and unique")
    return paths


def _validate_failure_report(report: Mapping[str, Any]) -> None:
    _require_exact_keys(report, _SUCCESS_REPORT_KEYS | {"error_kind"}, "failure report")
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ControllerEvidenceError("failure report has an unsupported schema")
    if report.get("mode") != "read_only" or _require_bool(report.get("ok"), "report.ok"):
        raise ControllerEvidenceError("failure report must be a failed read_only report")
    _require_bool(report.get("polling_match"), "report.polling_match")
    _require_bool(report.get("kill_switch_configured"), "report.kill_switch_configured")
    error_kind = _require_string(report.get("error_kind"), "report.error_kind")
    if error_kind not in _FAILURE_ERROR_KINDS:
        raise ControllerEvidenceError("failure report error_kind is unsupported")
    run_id = report.get("run_id")
    if not (
        isinstance(run_id, str)
        and (
            _ATTEMPT_ID_RE.fullmatch(run_id) is not None
            or (run_id == "" and error_kind == "run_metadata_failed")
        )
    ):
        raise ControllerEvidenceError("failure report run_id is invalid")
    started_at = _timestamp_value(report.get("started_at"), "report.started_at")
    completed_at = _timestamp_value(report.get("completed_at"), "report.completed_at")
    if completed_at < started_at:
        raise ControllerEvidenceError("failure report completed before it started")
    scan_count = _require_int(
        report.get("scan_count"),
        "report.scan_count",
        minimum=0,
        maximum=1_000,
    )
    scans = report.get("scan_comparisons")
    if not isinstance(scans, list) or len(scans) > scan_count:
        raise ControllerEvidenceError("failure report scans exceed scan_count")
    stable_by_scan: dict[int, set[str]] = {}
    for offset, item in enumerate(scans, start=1):
        scan = _require_mapping(item, f"report.scan_comparisons[{offset - 1}]")
        _require_exact_keys(scan, _SCAN_KEYS, "failure scan")
        if _require_int(
            scan.get("scan_index"),
            "failure scan index",
            minimum=1,
            maximum=max(scan_count, 1),
        ) != offset:
            raise ControllerEvidenceError("failure scan indexes are not contiguous")
        _require_bool(scan.get("match"), "failure scan match")
        go_paths = _validate_path_list(
            scan.get("go_stable_relative_paths"),
            "failure scan Go paths",
        )
        python_paths = _validate_path_list(
            scan.get("python_stable_relative_paths"),
            "failure scan Python paths",
        )
        stable_by_scan[offset] = set(go_paths).intersection(python_paths)
    checks = report.get("plan_checks")
    if not isinstance(checks, list) or len(checks) > 4_096:
        raise ControllerEvidenceError("failure report plan checks exceed the limit")
    verified = 0
    seen_checks: set[tuple[int, str]] = set()
    for item in checks:
        check = _require_mapping(item, "failure report plan check")
        status_value = check.get("status")
        if status_value == "verified":
            _require_exact_keys(
                check,
                {"plan_sha256", "relative_path", "scan_index", "status"},
                "verified plan check",
            )
            _require_sha256(check.get("plan_sha256"), "verified plan SHA-256")
            verified += 1
        elif status_value == "rejected":
            _require_exact_keys(
                check,
                {"error_kind", "relative_path", "scan_index", "status"},
                "rejected plan check",
            )
            if (
                _require_string(
                    check.get("error_kind"),
                    "rejected plan error kind",
                )
                not in _REJECTED_PLAN_ERROR_KINDS
            ):
                raise ControllerEvidenceError(
                    "rejected plan error kind is unsupported"
                )
        else:
            raise ControllerEvidenceError("failure report plan status is invalid")
        relative_path = _validate_direct_name(
            check.get("relative_path"),
            "failure plan path",
        )
        scan_index = _require_int(
            check.get("scan_index"),
            "failure plan scan index",
            minimum=1,
            maximum=max(scan_count, 1),
        )
        check_key = (scan_index, relative_path)
        if check_key in seen_checks:
            raise ControllerEvidenceError("failure report duplicates a plan check")
        seen_checks.add(check_key)
        if relative_path not in stable_by_scan.get(scan_index, set()):
            raise ControllerEvidenceError(
                "failure plan check is not linked to its stable scan"
            )
    if _require_int(
        report.get("verified_count"),
        "report.verified_count",
        minimum=0,
        maximum=4_096,
    ) != verified:
        raise ControllerEvidenceError(
            "failure report verified_count does not match its plan checks"
        )


def _report_sha256(report: Mapping[str, Any]) -> str:
    return _sha256_json(report)


def _validate_finish(
    record: Mapping[str, Any],
    *,
    sequence: int,
    expected_attempt_id: str,
) -> str:
    label = f"attempt {sequence} finish"
    _require_exact_keys(record, _FINISH_KEYS, label)
    if record.get("schema_version") != FINISH_SCHEMA_VERSION:
        raise ControllerEvidenceError(f"{label} has an unsupported schema")
    if _require_int(
        record.get("sequence"),
        f"{label}.sequence",
        minimum=1,
        maximum=MAX_ATTEMPTS,
    ) != sequence:
        raise ControllerEvidenceError(f"{label} sequence does not match its filename")
    if record.get("attempt_id") != expected_attempt_id:
        raise ControllerEvidenceError(f"{label} attempt id does not match its start")
    _require_timestamp(record.get("completed_at"), f"{label}.completed_at")
    _require_sha256(
        record.get("previous_record_sha256"),
        f"{label}.previous_record_sha256",
    )
    _require_sha256(record.get("execution_sha256"), f"{label}.execution_sha256")
    outcome = _require_string(record.get("outcome"), f"{label}.outcome")
    if outcome not in _OUTCOMES:
        raise ControllerEvidenceError(f"{label}.outcome is unsupported")
    exit_code = record.get("exit_code")
    if exit_code is not None:
        _require_int(exit_code, f"{label}.exit_code", minimum=-255, maximum=255)
    runner_status = _require_string(
        record.get("runner_status"),
        f"{label}.runner_status",
    )
    if runner_status not in _RUNNER_STATUSES:
        raise ControllerEvidenceError(f"{label}.runner_status is unsupported")
    raw_report_bytes = _require_int(
        record.get("raw_report_bytes"),
        f"{label}.raw_report_bytes",
        minimum=0,
        maximum=MAX_REPORT_BYTES + 1,
    )
    raw_report_digest = _require_sha256(
        record.get("raw_report_sha256"),
        f"{label}.raw_report_sha256",
    )
    if (raw_report_bytes == 0) != (raw_report_digest == ZERO_SHA256):
        raise ControllerEvidenceError(
            f"{label} raw report size and digest are inconsistent"
        )
    report_value = record.get("report")
    report_digest = _require_sha256(
        record.get("report_sha256"),
        f"{label}.report_sha256",
    )
    if report_value is None:
        if report_digest != ZERO_SHA256:
            raise ControllerEvidenceError(f"{label} null report must use the zero digest")
        if outcome in {"success", "failed"}:
            raise ControllerEvidenceError(f"{label} outcome requires a report")
    else:
        report = _require_mapping(report_value, f"{label}.report")
        if _report_sha256(report) != report_digest:
            raise ControllerEvidenceError(f"{label} report SHA-256 does not match")
        if raw_report_bytes == 0:
            raise ControllerEvidenceError(
                f"{label} retained report requires raw output evidence"
            )
        if outcome == "success":
            if exit_code != 0:
                raise ControllerEvidenceError(f"{label} success exit code is invalid")
            try:
                validate_controller_success_report(report)
            except ControllerReadinessError as exc:
                raise ControllerEvidenceError(
                    f"{label} success report is invalid"
                ) from exc
        elif outcome in {"failed", "interrupted"}:
            _validate_failure_report(report)
            error_kind = report.get("error_kind")
            if outcome == "interrupted" and error_kind != "interrupted":
                raise ControllerEvidenceError(
                    f"{label} interrupted report kind is invalid"
                )
            if outcome == "failed" and error_kind == "interrupted":
                raise ControllerEvidenceError(f"{label} failure outcome is invalid")
        elif outcome == "input_changed":
            if report.get("ok") is True and "error_kind" not in report:
                if exit_code != 0 or runner_status != "completed":
                    raise ControllerEvidenceError(
                        f"{label} changed-input success execution is invalid"
                    )
                try:
                    validate_controller_success_report(report)
                except ControllerReadinessError as exc:
                    raise ControllerEvidenceError(
                        f"{label} changed-input success report is invalid"
                    ) from exc
            else:
                _validate_failure_report(report)
                if (
                    exit_code in {None, 0}
                    or runner_status not in {"completed", "interrupted"}
                ):
                    raise ControllerEvidenceError(
                        f"{label} changed-input failure execution is invalid"
                    )
                error_kind = report.get("error_kind")
                if (
                    runner_status == "interrupted"
                    and error_kind != "interrupted"
                ) or (
                    runner_status == "completed"
                    and error_kind == "interrupted"
                ):
                    raise ControllerEvidenceError(
                        f"{label} changed-input failure execution is invalid"
                    )
        else:
            raise ControllerEvidenceError(f"{label} outcome must not retain a report")
    execution_sha256 = str(record["execution_sha256"])
    if outcome == "input_invalid":
        if (
            execution_sha256 != ZERO_SHA256
            or exit_code is not None
            or report_value is not None
            or runner_status != "not_started"
            or raw_report_bytes != 0
        ):
            raise ControllerEvidenceError(f"{label} input_invalid evidence is inconsistent")
    elif execution_sha256 == ZERO_SHA256:
        raise ControllerEvidenceError(f"{label} execution evidence must not be zero")
    expected_statuses = {
        "success": {"completed"},
        "failed": {"completed"},
        "interrupted": {"interrupted"},
        "invalid_report": {"completed"},
        "missing_report": {"completed"},
        "spawn_failed": {"spawn_failed"},
        "timeout": {"timeout"},
        "input_invalid": {"not_started"},
        "input_changed": {
            "completed",
            "interrupted",
            "spawn_failed",
            "timeout",
        },
    }
    if runner_status not in expected_statuses[outcome]:
        raise ControllerEvidenceError(
            f"{label} outcome and runner status are inconsistent"
        )
    if runner_status in {"completed", "interrupted", "timeout"} and exit_code is None:
        raise ControllerEvidenceError(f"{label} completed runner has no exit code")
    if runner_status in {"not_started", "spawn_failed"} and exit_code is not None:
        raise ControllerEvidenceError(
            f"{label} non-started runner has an exit code"
        )
    if runner_status in {"not_started", "spawn_failed"} and raw_report_bytes != 0:
        raise ControllerEvidenceError(
            f"{label} non-started runner has raw output"
        )
    if outcome == "success" and exit_code != 0:
        raise ControllerEvidenceError(f"{label} success exit code is invalid")
    if outcome == "failed" and exit_code in {None, 0}:
        raise ControllerEvidenceError(f"{label} failure exit code is invalid")
    if outcome == "interrupted" and exit_code is None:
        raise ControllerEvidenceError(f"{label} interruption exit code is invalid")
    if outcome == "missing_report" and raw_report_bytes != 0:
        raise ControllerEvidenceError(f"{label} missing report has raw output")
    if outcome == "invalid_report" and raw_report_bytes == 0:
        raise ControllerEvidenceError(f"{label} invalid report has no raw output")
    return _validate_record_hash(record, label=label)


def _load_journal(root_fd: int) -> _JournalState:
    try:
        names = set(os.listdir(root_fd))
    except OSError as exc:
        raise ControllerEvidenceError("journal inventory is not safely readable") from exc
    if LOCK_FILENAME not in names:
        raise ControllerEvidenceError("journal lock file is missing")
    names.remove(LOCK_FILENAME)
    starts_by_sequence: dict[int, str] = {}
    finishes_by_sequence: dict[int, str] = {}
    for name in names:
        match = _RECORD_NAME_RE.fullmatch(name)
        if match is None:
            raise ControllerEvidenceError("journal contains an unexpected entry")
        sequence = int(match.group("sequence"))
        target = (
            starts_by_sequence
            if match.group("kind") == "start"
            else finishes_by_sequence
        )
        if sequence in target:
            raise ControllerEvidenceError("journal contains duplicate record names")
        target[sequence] = name
    if len(starts_by_sequence) > MAX_ATTEMPTS:
        raise ControllerEvidenceError("journal attempt count exceeds the closed limit")
    expected_sequences = list(range(1, len(starts_by_sequence) + 1))
    if sorted(starts_by_sequence) != expected_sequences:
        raise ControllerEvidenceError("journal start sequences are not contiguous")
    if not set(finishes_by_sequence).issubset(starts_by_sequence):
        raise ControllerEvidenceError("journal contains an orphan finish record")

    starts: dict[int, dict[str, Any]] = {}
    finishes: dict[int, dict[str, Any]] = {}
    previous = ZERO_SHA256
    raw_digest = hashlib.sha256()
    for sequence in expected_sequences:
        start, start_raw = _read_record(root_fd, starts_by_sequence[sequence])
        start_sha = _validate_start(start, sequence=sequence)
        if start.get("previous_record_sha256") != previous:
            raise ControllerEvidenceError("journal start hash chain is broken")
        starts[sequence] = start
        previous = start_sha
        raw_digest.update(start_raw)
        finish_name = finishes_by_sequence.get(sequence)
        if finish_name is not None:
            finish, finish_raw = _read_record(root_fd, finish_name)
            finish_sha = _validate_finish(
                finish,
                sequence=sequence,
                expected_attempt_id=str(start["attempt_id"]),
            )
            if finish.get("previous_record_sha256") != previous:
                raise ControllerEvidenceError("journal finish hash chain is broken")
            finishes[sequence] = finish
            if _timestamp_value(
                finish.get("completed_at"),
                f"attempt {sequence} finish.completed_at",
            ) < _timestamp_value(
                start.get("started_at"),
                f"attempt {sequence} start.started_at",
            ):
                raise ControllerEvidenceError(
                    "journal attempt completed before it started"
                )
            previous = finish_sha
            raw_digest.update(finish_raw)
    return _JournalState(
        starts=starts,
        finishes=finishes,
        head_sha256=previous,
        raw_sha256=raw_digest.hexdigest(),
    )


def _requested_binding_sha256(
    *,
    controller_binary: str | Path,
    python_binary: str | Path,
    repo_root: str | Path,
    config: str | Path,
    kill_switch: str | Path,
    timeout_sec: int,
) -> str:
    def raw_path_digest(value: str | Path) -> str:
        return _sha256_bytes(os.fsencode(os.fspath(value)))

    values = {
        "controller_binary_path_sha256": raw_path_digest(controller_binary),
        "python_binary_path_sha256": raw_path_digest(python_binary),
        "repo_root_path_sha256": raw_path_digest(repo_root),
        "config_path_sha256": raw_path_digest(config),
        "kill_switch_path_sha256": raw_path_digest(kill_switch),
        "timeout_sec": timeout_sec,
    }
    return _sha256_json(values)


def _execution_evidence(
    *,
    controller_binary: str | Path,
    python_binary: str | Path,
    repo_root: str | Path,
    config: str | Path,
    kill_switch: str | Path,
) -> tuple[dict[str, Any], list[str]]:
    controller = _stable_file(
        controller_binary,
        label="controller binary",
        maximum_bytes=MAX_BINARY_BYTES,
        executable=True,
        require_temporary=True,
    )
    python = _stable_file(
        python_binary,
        label="Python binary",
        maximum_bytes=MAX_BINARY_BYTES,
        executable=True,
        require_temporary=False,
        allow_leaf_symlink=True,
    )
    config_file = _stable_file(
        config,
        label="controller config",
        maximum_bytes=MAX_CONFIG_BYTES,
        executable=False,
        require_temporary=True,
    )
    repo, repo_identity = _real_directory(
        repo_root,
        label="repo root",
        require_temporary=False,
    )
    marker = _absent_kill_switch(kill_switch)
    evidence = {
        "controller": {
            "path_sha256": _sha256_bytes(os.fsencode(controller.path)),
            "content_sha256": controller.sha256,
            "identity": controller.identity,
        },
        "python": {
            "path_sha256": _sha256_bytes(os.fsencode(python.path)),
            "content_sha256": python.sha256,
            "identity": python.identity,
        },
        "config": {
            "path_sha256": _sha256_bytes(os.fsencode(config_file.path)),
            "content_sha256": config_file.sha256,
            "identity": config_file.identity,
        },
        "repo": {
            "path_sha256": _sha256_bytes(os.fsencode(repo)),
            "identity": repo_identity,
        },
        "kill_switch_path_sha256": _sha256_bytes(os.fsencode(marker)),
    }
    command = [
        os.fspath(controller.path),
        "--python-bin",
        os.fspath(python.path),
        "--repo-root",
        os.fspath(repo),
        "--config",
        os.fspath(config_file.path),
        "--kill-switch",
        os.fspath(marker),
    ]
    return evidence, command


def _decode_report(raw: bytes) -> Mapping[str, Any]:
    if not raw or len(raw) > MAX_REPORT_BYTES:
        raise ControllerEvidenceError("controller report size is outside the closed limit")
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise ControllerEvidenceError("controller must emit exactly one complete report line")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ControllerEvidenceError("controller report contains duplicate keys")
            result[key] = value
        return result

    try:
        decoded = raw.decode("utf-8", errors="strict")
        value = json.loads(decoded, object_pairs_hook=reject_duplicates)
    except ControllerEvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControllerEvidenceError("controller report is invalid JSON") from exc
    report = _require_mapping(value, "controller report")
    if report.get("ok") is True and "error_kind" not in report:
        try:
            validate_controller_success_report(report)
        except ControllerReadinessError as exc:
            raise ControllerEvidenceError("controller success report is invalid") from exc
    else:
        _validate_failure_report(report)
    return report


def _limit_child_output() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_REPORT_BYTES, MAX_REPORT_BYTES))


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    process_group = process.pid

    def group_exists() -> bool:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    if group_exists():
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    if group_exists():
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            raise ControllerEvidenceError(
                "controller process group leader could not be reaped"
            ) from exc
    deadline = time.monotonic() + 2
    while group_exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if group_exists():
        raise ControllerEvidenceError(
            "controller process group descendants could not be reaped"
        )


def _run_controller(command: list[str], *, timeout_sec: int) -> tuple[int | None, bytes, str]:
    received_signal: list[int] = []
    process: subprocess.Popen[Any] | None = None
    previous_handlers: dict[int, Any] = {}

    def forward(signum: int, _frame: Any) -> None:
        received_signal.append(signum)
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, forward)
    try:
        with tempfile.TemporaryFile() as stdout_file:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                    preexec_fn=_limit_child_output,
                )
            except OSError:
                return None, b"", "spawn_failed"
            deadline = time.monotonic() + timeout_sec
            runner_status = "completed"
            while process.poll() is None:
                if received_signal:
                    _terminate_process_group(process)
                    runner_status = "interrupted"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_process_group(process)
                    runner_status = "timeout"
                    break
                try:
                    process.wait(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
            if received_signal:
                runner_status = "interrupted"
            stdout_file.seek(0)
            return (
                process.returncode,
                stdout_file.read(MAX_REPORT_BYTES + 1),
                runner_status,
            )
    finally:
        if process is not None and process.poll() is None:
            _terminate_process_group(process)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _classify_result(
    *,
    exit_code: int | None,
    raw_report: bytes,
    runner_status: str,
) -> tuple[str, Mapping[str, Any] | None]:
    if runner_status == "spawn_failed":
        return "spawn_failed", None
    if runner_status == "timeout":
        return "timeout", None
    try:
        report = _decode_report(raw_report)
    except ControllerEvidenceError:
        if runner_status == "interrupted":
            return "interrupted", None
        if not raw_report:
            return "missing_report", None
        return "invalid_report", None
    if runner_status == "interrupted":
        if report.get("error_kind") == "interrupted":
            return "interrupted", report
        return "interrupted", None
    if report.get("ok") is True and exit_code == 0:
        return "success", report
    if report.get("ok") is False and exit_code not in {None, 0}:
        if report.get("error_kind") == "interrupted":
            return "interrupted", report
        return "failed", report
    return "invalid_report", None


def _finish_record(
    *,
    sequence: int,
    attempt_id: str,
    previous_record_sha256: str,
    outcome: str,
    exit_code: int | None,
    execution_sha256: str,
    raw_report: bytes,
    runner_status: str,
    report: Mapping[str, Any] | None,
) -> dict[str, Any]:
    report_payload = dict(report) if report is not None else None
    unsigned = {
        "schema_version": FINISH_SCHEMA_VERSION,
        "sequence": sequence,
        "attempt_id": attempt_id,
        "completed_at": _utc_now(),
        "previous_record_sha256": previous_record_sha256,
        "outcome": outcome,
        "exit_code": exit_code,
        "execution_sha256": execution_sha256,
        "runner_status": runner_status,
        "raw_report_bytes": len(raw_report),
        "raw_report_sha256": (
            _sha256_bytes(raw_report) if raw_report else ZERO_SHA256
        ),
        "report_sha256": (
            _report_sha256(report_payload)
            if report_payload is not None
            else ZERO_SHA256
        ),
        "report": report_payload,
    }
    return _record_payload(unsigned)


def run_controller_observation(
    *,
    journal_root: str | Path,
    controller_binary: str | Path,
    python_binary: str | Path,
    repo_root: str | Path,
    config: str | Path,
    kill_switch: str | Path,
    timeout_sec: int,
    enable_observation: bool,
    allow_write: bool,
    expected_count: int | None,
) -> dict[str, Any]:
    if not enable_observation:
        raise ControllerEvidenceWriteDisabledError(
            "controller observation is disabled; pass --enable-observation"
        )
    if not allow_write:
        raise ControllerEvidenceWriteDisabledError(
            "controller observation requires --allow-write"
        )
    if isinstance(expected_count, bool) or expected_count != 1:
        raise ControllerEvidenceError(
            "controller observation requires --expected-count 1"
        )
    timeout = _require_int(
        timeout_sec,
        "timeout_sec",
        minimum=1,
        maximum=MAX_TIMEOUT_SEC,
    )
    journal_path, _ = _real_directory(
        journal_root,
        label="journal root",
        require_temporary=True,
    )
    root_fd = _open_root(journal_path)
    lock_fd: int | None = None
    try:
        lock_fd = _open_lock(root_fd, create=True, exclusive=True)
        try:
            os.fsync(root_fd)
        except OSError as exc:
            raise ControllerEvidenceError(
                "journal root could not be synchronized"
            ) from exc
        state = _load_journal(root_fd)
        sequence = len(state.starts) + 1
        if sequence > MAX_ATTEMPTS:
            raise ControllerEvidenceError(
                "journal is full; immutable evidence is never rotated automatically"
            )
        attempt_id = uuid.uuid4().hex
        binding_sha256 = _requested_binding_sha256(
            controller_binary=controller_binary,
            python_binary=python_binary,
            repo_root=repo_root,
            config=config,
            kill_switch=kill_switch,
            timeout_sec=timeout,
        )
        start = _record_payload(
            {
                "schema_version": START_SCHEMA_VERSION,
                "sequence": sequence,
                "attempt_id": attempt_id,
                "started_at": _utc_now(),
                "previous_record_sha256": state.head_sha256,
                "binding_sha256": binding_sha256,
            }
        )
        _write_record(root_fd, f"{sequence:08d}.start.json", start)
        start_sha256 = str(start["record_sha256"])

        execution_sha256 = ZERO_SHA256
        exit_code: int | None = None
        raw_report = b""
        runner_status = "not_started"
        report: Mapping[str, Any] | None = None
        try:
            execution, command = _execution_evidence(
                controller_binary=controller_binary,
                python_binary=python_binary,
                repo_root=repo_root,
                config=config,
                kill_switch=kill_switch,
            )
        except ControllerEvidenceError:
            outcome = "input_invalid"
        else:
            execution_sha256 = _sha256_json(execution)
            exit_code, raw_report, runner_status = _run_controller(
                command,
                timeout_sec=timeout,
            )
            outcome, report = _classify_result(
                exit_code=exit_code,
                raw_report=raw_report,
                runner_status=runner_status,
            )
            try:
                current_execution, _ = _execution_evidence(
                    controller_binary=controller_binary,
                    python_binary=python_binary,
                    repo_root=repo_root,
                    config=config,
                    kill_switch=kill_switch,
                )
            except ControllerEvidenceError:
                outcome = "input_changed"
            else:
                if _sha256_json(current_execution) != execution_sha256:
                    outcome = "input_changed"
        finish = _finish_record(
            sequence=sequence,
            attempt_id=attempt_id,
            previous_record_sha256=start_sha256,
            outcome=outcome,
            exit_code=exit_code,
            execution_sha256=execution_sha256,
            raw_report=raw_report,
            runner_status=runner_status,
            report=report,
        )
        _write_record(root_fd, f"{sequence:08d}.finish.json", finish)
        _load_journal(root_fd)
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "sequence": sequence,
            "outcome": outcome,
            "start_record_sha256": start_sha256,
            "finish_record_sha256": finish["record_sha256"],
            "attempt_count": sequence,
        }
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(root_fd)


def verify_controller_evidence(
    journal_root: str | Path,
    *,
    min_runs: int = 20,
    min_verified: int = 100,
) -> dict[str, Any]:
    required_runs = _require_int(
        min_runs,
        "min_runs",
        minimum=1,
        maximum=MAX_ATTEMPTS,
    )
    required_verified = _require_int(
        min_verified,
        "min_verified",
        minimum=1,
        maximum=1_000_000,
    )
    journal_path, _ = _real_directory(
        journal_root,
        label="journal root",
        require_temporary=True,
    )
    root_fd = _open_root(journal_path)
    lock_fd: int | None = None
    try:
        lock_fd = _open_lock(root_fd, create=False, exclusive=False)
        state = _load_journal(root_fd)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(root_fd)
    consecutive_runs = 0
    consecutive_verified = 0
    suffix_metadata: list[dict[str, Any]] = []
    for sequence in range(len(state.starts), 0, -1):
        finish = state.finishes.get(sequence)
        if finish is None or finish.get("outcome") != "success":
            break
        report = _require_mapping(finish.get("report"), "successful finish report")
        try:
            validated = validate_controller_success_report(report)
        except ControllerReadinessError as exc:
            raise ControllerEvidenceError(
                "successful finish contains an invalid readiness report"
            ) from exc
        consecutive_runs += 1
        consecutive_verified += int(validated["verified_count"])
        suffix_metadata.append(validated)
    chronological = list(reversed(suffix_metadata))
    seen_run_ids: set[str] = set()
    previous_started_at: int | None = None
    previous_completed_at: int | None = None
    for metadata in chronological:
        run_id = str(metadata["run_id"])
        started_at = int(metadata["started_at_ns"])
        completed_at = int(metadata["completed_at_ns"])
        if run_id in seen_run_ids:
            raise ControllerEvidenceError(
                "consecutive evidence contains a duplicate controller run id"
            )
        if previous_started_at is not None and started_at <= previous_started_at:
            raise ControllerEvidenceError(
                "consecutive controller start times are not strictly increasing"
            )
        if previous_completed_at is not None and started_at < previous_completed_at:
            raise ControllerEvidenceError(
                "consecutive controller runs overlap"
            )
        seen_run_ids.add(run_id)
        previous_started_at = started_at
        previous_completed_at = completed_at
    if (
        consecutive_runs < required_runs
        or consecutive_verified < required_verified
    ):
        raise ControllerEvidenceError(
            "attempt journal does not satisfy the consecutive readiness criteria"
        )
    failed_count = sum(
        1
        for finish in state.finishes.values()
        if finish.get("outcome") != "success"
    )
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "ready": True,
        "attempt_count": len(state.starts),
        "completed_count": len(state.finishes),
        "incomplete_count": len(state.starts) - len(state.finishes),
        "failed_count": failed_count,
        "consecutive_success_count": consecutive_runs,
        "consecutive_verified_count": consecutive_verified,
        "head_record_sha256": state.head_sha256,
        "journal_sha256": state.raw_sha256,
        "criteria": {
            "min_runs": required_runs,
            "min_verified": required_verified,
            "report_schema_version": REPORT_SCHEMA_VERSION,
        },
        "rotation_supported": False,
    }


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--journal-root", required=True)
    parser.add_argument("--controller-bin", required=True)
    parser.add_argument("--python-bin", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--kill-switch", required=True)
    parser.add_argument("--timeout-sec", type=int, default=1_800)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--enable-observation", action="store_true")
    parser.add_argument("--allow-write", action="store_true")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run and verify an isolated immutable controller attempt journal"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    _add_run_arguments(run_parser)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--journal-root", required=True)
    verify_parser.add_argument("--min-runs", type=int, default=20)
    verify_parser.add_argument("--min-verified", type=int, default=100)
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "run":
            result = run_controller_observation(
                journal_root=args.journal_root,
                controller_binary=args.controller_bin,
                python_binary=args.python_bin,
                repo_root=args.repo_root,
                config=args.config,
                kill_switch=args.kill_switch,
                timeout_sec=args.timeout_sec,
                enable_observation=args.enable_observation,
                allow_write=args.allow_write,
                expected_count=args.expected_count,
            )
            exit_code = (
                0
                if result["outcome"] == "success"
                else 130
                if result["outcome"] == "interrupted"
                else 2
            )
        else:
            result = verify_controller_evidence(
                args.journal_root,
                min_runs=args.min_runs,
                min_verified=args.min_verified,
            )
            exit_code = 0
    except ControllerEvidenceError as exc:
        print(f"controller evidence rejected: {exc}", file=sys.stderr)
        return 2
    print(_canonical_json(result))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
