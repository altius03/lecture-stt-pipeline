from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from lecture_stt.shared import db, utils
from lecture_stt.stt.controller_gate import controller_kill_switch_is_active


PLAN_SCHEMA_VERSION = "lecture-stt/terminal-error-recovery-plan@1"
RESULT_SCHEMA_VERSION = "lecture-stt/terminal-error-recovery-result@1"
MAX_MANIFEST_BYTES = 64 * 1024
RECOVERY_METADATA_SCHEMA_VERSION = "lecture-stt/terminal-error-recovery-metadata@1"
_FAILED_TRANSCRIPTION_STEP = "실패: 전사 실행"

_PLAN_KEYS = {
    "schema_version",
    "expected_count",
    "bindings",
    "error_job",
    "source",
    "target",
    "recovery_policy",
    "plan_sha256",
}
_BINDING_KEYS = {
    "db_path_sha256",
    "db_identity",
    "audio_root_sha256",
    "error_root_sha256",
    "transcript_root_sha256",
}
_DB_IDENTITY_KEYS = {"device", "inode", "mode", "nlink"}
_ERROR_JOB_KEYS = {
    "job_id",
    "status",
    "current_step",
    "updated_at",
    "orig_name",
    "canonical_base",
    "error_audio_relative_path",
    "transcript_txt_relative_path",
    "transcript_json_relative_path",
    "sha256",
    "engine_params_sha256",
    "error_message_sha256",
    "error_trace_sha256",
}
_SOURCE_KEYS = {
    "relative_path",
    "device",
    "inode",
    "size_bytes",
    "mtime_ns",
    "ctime_ns",
    "sha256",
}
_TARGET_KEYS = {"relative_path", "expected_size_bytes", "expected_sha256"}
_RECOVERY_POLICY_KEYS = {"transcribe_max_retries", "retry_current_step"}


class TerminalErrorRecoveryContractError(RuntimeError):
    pass


class TerminalErrorRecoveryWriteDisabledError(TerminalErrorRecoveryContractError):
    pass


class TerminalErrorRecoveryConflictError(TerminalErrorRecoveryContractError):
    pass


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_text(value: Any) -> str:
    if value is None:
        return hashlib.sha256(b"").hexdigest()
    if not isinstance(value, str):
        value = str(value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise TerminalErrorRecoveryContractError(
            f"{label} keys mismatch: missing={missing} unexpected={unexpected}"
        )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TerminalErrorRecoveryContractError(f"{label} must be an object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TerminalErrorRecoveryContractError(f"{label} must be a non-empty string")
    return value


def _require_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TerminalErrorRecoveryContractError(f"{label} must be a non-negative integer")
    return value


def _require_positive_int(value: Any, label: str) -> int:
    result = _require_nonnegative_int(value, label)
    if result == 0:
        raise TerminalErrorRecoveryContractError(f"{label} must be a positive integer")
    return result


def _require_sha256(value: Any, label: str) -> str:
    digest = _require_string(value, label)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise TerminalErrorRecoveryContractError(f"{label} must be a lowercase SHA-256")
    return digest


def _path_binding_sha256(path: Path) -> str:
    normalized = os.path.realpath(os.fspath(path))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _bindings(config: Mapping[str, Any]) -> dict[str, str]:
    paths = _require_mapping(config.get("paths"), "config.paths")
    db_path = Path(_require_string(paths.get("db_path"), "config.paths.db_path"))
    audio_root = Path(
        _require_string(paths.get("stable_audio_folder"), "config.paths.stable_audio_folder")
    )
    error_root = Path(
        _require_string(paths.get("error_folder"), "config.paths.error_folder")
    )
    transcript_root = Path(
        _require_string(paths.get("transcript_folder"), "config.paths.transcript_folder")
    )
    return {
        "db_path_sha256": _path_binding_sha256(db_path),
        "audio_root_sha256": _path_binding_sha256(audio_root),
        "error_root_sha256": _path_binding_sha256(error_root),
        "transcript_root_sha256": _path_binding_sha256(transcript_root),
    }


def _db_identity_from_stat_result(path_stat: os.stat_result) -> dict[str, int]:
    return {
        "device": int(path_stat.st_dev),
        "inode": int(path_stat.st_ino),
        "mode": int(path_stat.st_mode),
        "nlink": int(path_stat.st_nlink),
    }


def _require_db_identity(value: Any, label: str) -> dict[str, int]:
    mapping = _require_mapping(value, label)
    _require_exact_keys(mapping, _DB_IDENTITY_KEYS, label)
    return {
        "device": _require_nonnegative_int(mapping.get("device"), f"{label}.device"),
        "inode": _require_nonnegative_int(mapping.get("inode"), f"{label}.inode"),
        "mode": _require_nonnegative_int(mapping.get("mode"), f"{label}.mode"),
        "nlink": _require_nonnegative_int(mapping.get("nlink"), f"{label}.nlink"),
    }


def _open_root(path: Path, *, label: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(path, flags)
    except OSError as exc:
        raise TerminalErrorRecoveryConflictError(
            f"{label} is not safely openable: {path}"
        ) from exc
    root_stat = os.fstat(root_fd)
    if not stat.S_ISDIR(root_stat.st_mode):
        os.close(root_fd)
        raise TerminalErrorRecoveryConflictError(f"{label} is not a directory: {path}")
    return root_fd


def _validate_direct_child_relative_path(value: Any, *, label: str) -> str:
    relative_path = _require_string(value, label)
    if "\x00" in relative_path:
        raise TerminalErrorRecoveryContractError(f"{label} contains NUL")
    candidate = Path(relative_path)
    if candidate.is_absolute() or len(candidate.parts) != 1:
        raise TerminalErrorRecoveryContractError(f"{label} must name exactly one direct child")
    if candidate.name in {"", ".", ".."} or candidate.name != relative_path:
        raise TerminalErrorRecoveryContractError(f"{label} must name exactly one direct child")
    return relative_path


def _safe_canonical_base(value: Any, label: str) -> str:
    canonical_base = _require_string(value, label)
    if "\x00" in canonical_base or "/" in canonical_base or "\\" in canonical_base:
        raise TerminalErrorRecoveryConflictError(f"{label} is not a single safe filename")
    if Path(canonical_base).name != canonical_base or canonical_base in {".", ".."}:
        raise TerminalErrorRecoveryConflictError(f"{label} is not a single safe filename")
    return canonical_base


def _load_direct_file_evidence(root: Path, relative_path: str, *, label: str) -> dict[str, Any]:
    relative_path = _validate_direct_child_relative_path(relative_path, label=f"{label}.relative_path")
    root_fd = _open_root(root, label=label)
    file_fd: int | None = None
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            file_fd = os.open(relative_path, flags, dir_fd=root_fd)
        except OSError as exc:
            raise TerminalErrorRecoveryConflictError(
                f"{label} is not safely openable: {relative_path}"
            ) from exc

        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise TerminalErrorRecoveryConflictError(f"{label} must be a regular file")
        if before.st_nlink != 1:
            raise TerminalErrorRecoveryConflictError(
                f"{label} must have exactly one hard link"
            )

        digest = hashlib.sha256()
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)

        after = os.fstat(file_fd)
        current = os.stat(relative_path, dir_fd=root_fd, follow_symlinks=False)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise TerminalErrorRecoveryConflictError(
                f"{label} changed while it was hashed"
            )
        if any(getattr(after, field) != getattr(current, field) for field in stable_fields):
            raise TerminalErrorRecoveryConflictError(
                f"{label} pathname changed while it was hashed"
            )
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise TerminalErrorRecoveryConflictError(
                f"{label} pathname is not a single-link regular file"
            )
        return {
            "relative_path": relative_path,
            "device": int(after.st_dev),
            "inode": int(after.st_ino),
            "size_bytes": int(after.st_size),
            "mtime_ns": int(after.st_mtime_ns),
            "ctime_ns": int(after.st_ctime_ns),
            "sha256": digest.hexdigest(),
        }
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(root_fd)


def _open_db_guard(path: Path) -> tuple[int, dict[str, int]]:
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        fd = os.open(path, flags)
        db_stat = os.fstat(fd)
        if not stat.S_ISREG(db_stat.st_mode):
            os.close(fd)
            raise TerminalErrorRecoveryConflictError("jobs db must be a regular file")
        if db_stat.st_nlink != 1:
            os.close(fd)
            raise TerminalErrorRecoveryConflictError("jobs db must have exactly one hard link")
        return fd, _db_identity_from_stat_result(db_stat)
    except OSError as exc:
        raise TerminalErrorRecoveryConflictError(f"jobs db is not safely openable: {path}") from exc


def _assert_db_path_identity(path: Path, expected_identity: Mapping[str, Any], *, label: str) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise TerminalErrorRecoveryConflictError(f"{label} is not safely stat-able: {path}") from exc
    current_identity = _db_identity_from_stat_result(current)
    if current_identity != dict(expected_identity):
        raise TerminalErrorRecoveryConflictError(f"{label} identity changed")
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise TerminalErrorRecoveryConflictError(f"{label} is not a single-link regular file")


def _assert_database_list_main_matches(
    conn: sqlite3.Connection,
    configured_db_path: Path,
    expected_identity: Mapping[str, Any],
) -> None:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error as exc:
        raise TerminalErrorRecoveryConflictError("sqlite main database is unavailable") from exc
    if row is None or row[1] != "main":
        raise TerminalErrorRecoveryConflictError("sqlite main database is unavailable")
    opened_filename = row[2]
    if not isinstance(opened_filename, str) or not opened_filename:
        raise TerminalErrorRecoveryConflictError("sqlite main database filename is unavailable")
    opened_path = Path(opened_filename)
    if os.path.realpath(os.fspath(opened_path)) != os.path.realpath(os.fspath(configured_db_path)):
        raise TerminalErrorRecoveryConflictError(
            "sqlite main database does not resolve to the configured db path"
        )
    _assert_db_path_identity(opened_path, expected_identity, label="sqlite main database")


def _readonly_db_connection(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(os.fspath(db_path))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise TerminalErrorRecoveryConflictError(
            f"unable to open jobs db read-only: {db_path}"
        ) from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.Error as exc:
        conn.close()
        raise TerminalErrorRecoveryConflictError(
            f"unable to configure jobs db read-only: {db_path}"
        ) from exc
    return conn


def _require_retry_limit(config: Mapping[str, Any]) -> int:
    app = _require_mapping(config.get("app"), "config.app")
    return _require_positive_int(
        app.get("transcribe_max_retries"),
        "config.app.transcribe_max_retries",
    )


def _select_job_row(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    except sqlite3.Error as exc:
        raise TerminalErrorRecoveryConflictError("unable to read terminal error row") from exc
    if row is None:
        raise TerminalErrorRecoveryConflictError("job_id is not a terminal transcription error row")
    return row


def _row_path_relative(root: Path, value: Any, *, label: str) -> str:
    path = Path(_require_string(value, label))
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise TerminalErrorRecoveryConflictError(
            f"{label} is outside the configured root"
        ) from exc
    return _validate_direct_child_relative_path(os.fspath(relative), label=label)


def _target_relative_path(canonical_base: str, orig_name: str, error_relative_path: str) -> str:
    suffix = Path(orig_name).suffix.lower() or Path(error_relative_path).suffix.lower()
    if not suffix:
        raise TerminalErrorRecoveryConflictError(
            "terminal recovery target requires a source suffix"
        )
    return _validate_direct_child_relative_path(
        f"{canonical_base}{suffix}",
        label="target.relative_path",
    )


def _error_job_payload(
    row: sqlite3.Row,
    *,
    error_root: Path,
    transcript_root: Path,
) -> dict[str, Any]:
    status = _require_string(row["status"], "jobs.status")
    current_step = _require_string(row["current_step"], "jobs.current_step")
    if status != db.STATUS_ERROR or current_step != _FAILED_TRANSCRIPTION_STEP:
        raise TerminalErrorRecoveryConflictError(
            "job_id is not an exact terminal transcription error row"
        )
    canonical_base = _safe_canonical_base(row["canonical_base"], "jobs.canonical_base")
    error_audio_relative_path = _row_path_relative(
        error_root,
        row["canonical_audio_path"],
        label="jobs.canonical_audio_path",
    )
    transcript_txt_relative_path = _row_path_relative(
        transcript_root,
        row["transcript_txt_path"],
        label="jobs.transcript_txt_path",
    )
    transcript_json_relative_path = _row_path_relative(
        transcript_root,
        row["transcript_json_path"],
        label="jobs.transcript_json_path",
    )
    if transcript_txt_relative_path != f"{canonical_base}.txt":
        raise TerminalErrorRecoveryConflictError(
            "jobs.transcript_txt_path does not match canonical_base"
        )
    if transcript_json_relative_path != f"{canonical_base}.json":
        raise TerminalErrorRecoveryConflictError(
            "jobs.transcript_json_path does not match canonical_base"
        )
    sha256 = _require_sha256(row["sha256"], "jobs.sha256")
    return {
        "job_id": _require_positive_int(row["id"], "jobs.id"),
        "status": status,
        "current_step": current_step,
        "updated_at": _require_string(row["updated_at"], "jobs.updated_at"),
        "orig_name": _require_string(row["orig_name"], "jobs.orig_name"),
        "canonical_base": canonical_base,
        "error_audio_relative_path": error_audio_relative_path,
        "transcript_txt_relative_path": transcript_txt_relative_path,
        "transcript_json_relative_path": transcript_json_relative_path,
        "sha256": sha256,
        "engine_params_sha256": _sha256_text(row["engine_params"]),
        "error_message_sha256": _sha256_text(row["error_message"]),
        "error_trace_sha256": _sha256_text(row["error_trace"]),
    }


def build_terminal_error_recovery_plan(
    config: Mapping[str, Any],
    *,
    job_id: int,
) -> dict[str, Any]:
    _require_positive_int(job_id, "job_id")
    retry_limit = _require_retry_limit(config)
    paths = _require_mapping(config.get("paths"), "config.paths")
    db_path = Path(_require_string(paths.get("db_path"), "config.paths.db_path"))
    audio_root = Path(
        _require_string(paths.get("stable_audio_folder"), "config.paths.stable_audio_folder")
    )
    error_root = Path(
        _require_string(paths.get("error_folder"), "config.paths.error_folder")
    )
    transcript_root = Path(
        _require_string(paths.get("transcript_folder"), "config.paths.transcript_folder")
    )
    db_guard_fd, db_identity = _open_db_guard(db_path)
    conn: sqlite3.Connection | None = None
    try:
        conn = _readonly_db_connection(db_path)
        _assert_database_list_main_matches(conn, db_path, db_identity)
        row = _select_job_row(conn, job_id)
        error_job = _error_job_payload(
            row,
            error_root=error_root,
            transcript_root=transcript_root,
        )
    finally:
        if conn is not None:
            conn.close()
        os.close(db_guard_fd)
    _assert_db_path_identity(db_path, db_identity, label="jobs db path")

    source = _load_direct_file_evidence(
        error_root,
        error_job["error_audio_relative_path"],
        label="terminal recovery source",
    )
    if source["sha256"] != error_job["sha256"]:
        raise TerminalErrorRecoveryConflictError(
            "terminal recovery source does not match jobs.sha256"
        )
    target_relative_path = _target_relative_path(
        error_job["canonical_base"],
        error_job["orig_name"],
        error_job["error_audio_relative_path"],
    )
    if (audio_root / target_relative_path).exists():
        raise TerminalErrorRecoveryConflictError(
            "terminal recovery target already exists before planning"
        )
    payload: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "expected_count": 1,
        "bindings": {
            **_bindings(config),
            "db_identity": db_identity,
        },
        "error_job": error_job,
        "source": source,
        "target": {
            "relative_path": target_relative_path,
            "expected_size_bytes": source["size_bytes"],
            "expected_sha256": source["sha256"],
        },
        "recovery_policy": {
            "transcribe_max_retries": retry_limit,
            "retry_current_step": f"전사 재시도 대기 1/{retry_limit}",
        },
    }
    payload["plan_sha256"] = _sha256_json(payload)
    return payload


def validate_terminal_error_recovery_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    _require_exact_keys(plan, _PLAN_KEYS, "terminal-error-recovery plan")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise TerminalErrorRecoveryContractError(
            f"unsupported terminal-error-recovery schema: {plan.get('schema_version')!r}"
        )
    if plan.get("expected_count") != 1 or isinstance(plan.get("expected_count"), bool):
        raise TerminalErrorRecoveryContractError(
            "terminal-error-recovery plan expected_count must be exactly 1"
        )

    bindings = _require_mapping(plan.get("bindings"), "bindings")
    _require_exact_keys(bindings, _BINDING_KEYS, "bindings")
    _require_sha256(bindings.get("db_path_sha256"), "bindings.db_path_sha256")
    _require_sha256(bindings.get("audio_root_sha256"), "bindings.audio_root_sha256")
    _require_sha256(bindings.get("error_root_sha256"), "bindings.error_root_sha256")
    _require_sha256(
        bindings.get("transcript_root_sha256"),
        "bindings.transcript_root_sha256",
    )
    _require_db_identity(bindings.get("db_identity"), "bindings.db_identity")

    error_job = _require_mapping(plan.get("error_job"), "error_job")
    _require_exact_keys(error_job, _ERROR_JOB_KEYS, "error_job")
    _require_positive_int(error_job.get("job_id"), "error_job.job_id")
    if _require_string(error_job.get("status"), "error_job.status") != db.STATUS_ERROR:
        raise TerminalErrorRecoveryContractError("error_job.status must be ERROR")
    if (
        _require_string(error_job.get("current_step"), "error_job.current_step")
        != _FAILED_TRANSCRIPTION_STEP
    ):
        raise TerminalErrorRecoveryContractError(
            "error_job.current_step must be the exact failed transcription step"
        )
    _require_string(error_job.get("updated_at"), "error_job.updated_at")
    _require_string(error_job.get("orig_name"), "error_job.orig_name")
    _safe_canonical_base(error_job.get("canonical_base"), "error_job.canonical_base")
    _validate_direct_child_relative_path(
        error_job.get("error_audio_relative_path"),
        label="error_job.error_audio_relative_path",
    )
    _validate_direct_child_relative_path(
        error_job.get("transcript_txt_relative_path"),
        label="error_job.transcript_txt_relative_path",
    )
    _validate_direct_child_relative_path(
        error_job.get("transcript_json_relative_path"),
        label="error_job.transcript_json_relative_path",
    )
    _require_sha256(error_job.get("sha256"), "error_job.sha256")
    _require_sha256(error_job.get("engine_params_sha256"), "error_job.engine_params_sha256")
    _require_sha256(error_job.get("error_message_sha256"), "error_job.error_message_sha256")
    _require_sha256(error_job.get("error_trace_sha256"), "error_job.error_trace_sha256")

    source = _require_mapping(plan.get("source"), "source")
    _require_exact_keys(source, _SOURCE_KEYS, "source")
    if _validate_direct_child_relative_path(source.get("relative_path"), label="source.relative_path") != _require_string(
        error_job.get("error_audio_relative_path"), "error_job.error_audio_relative_path"
    ):
        raise TerminalErrorRecoveryContractError(
            "source.relative_path must match error_job.error_audio_relative_path"
        )
    for key in ("device", "inode", "size_bytes", "mtime_ns", "ctime_ns"):
        _require_nonnegative_int(source.get(key), f"source.{key}")
    if _require_sha256(source.get("sha256"), "source.sha256") != _require_string(
        error_job.get("sha256"),
        "error_job.sha256",
    ):
        raise TerminalErrorRecoveryContractError("source.sha256 must match error_job.sha256")

    target = _require_mapping(plan.get("target"), "target")
    _require_exact_keys(target, _TARGET_KEYS, "target")
    _validate_direct_child_relative_path(target.get("relative_path"), label="target.relative_path")
    _require_nonnegative_int(target.get("expected_size_bytes"), "target.expected_size_bytes")
    if _require_sha256(target.get("expected_sha256"), "target.expected_sha256") != _require_string(
        source.get("sha256"),
        "source.sha256",
    ):
        raise TerminalErrorRecoveryContractError(
            "target.expected_sha256 must match source.sha256"
        )
    if _require_nonnegative_int(
        target.get("expected_size_bytes"),
        "target.expected_size_bytes",
    ) != _require_nonnegative_int(source.get("size_bytes"), "source.size_bytes"):
        raise TerminalErrorRecoveryContractError(
            "target.expected_size_bytes must match source.size_bytes"
        )

    policy = _require_mapping(plan.get("recovery_policy"), "recovery_policy")
    _require_exact_keys(policy, _RECOVERY_POLICY_KEYS, "recovery_policy")
    retries = _require_positive_int(
        policy.get("transcribe_max_retries"),
        "recovery_policy.transcribe_max_retries",
    )
    if (
        _require_string(policy.get("retry_current_step"), "recovery_policy.retry_current_step")
        != f"전사 재시도 대기 1/{retries}"
    ):
        raise TerminalErrorRecoveryContractError(
            "recovery_policy.retry_current_step must exactly match transcribe_max_retries"
        )

    plan_sha256 = _require_sha256(plan.get("plan_sha256"), "plan_sha256")
    digest_input = dict(plan)
    digest_input.pop("plan_sha256")
    if _sha256_json(digest_input) != plan_sha256:
        raise TerminalErrorRecoveryContractError(
            "terminal-error-recovery plan SHA-256 does not match its closed payload"
        )
    return json.loads(_canonical_json(plan))


def load_terminal_error_recovery_plan(path: Path) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise TerminalErrorRecoveryContractError(
                    f"duplicate JSON key in terminal-error-recovery manifest: {key}"
                )
            result[key] = value
        return result

    fd: int | None = None
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        fd = os.open(path, flags)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise TerminalErrorRecoveryContractError(
                "terminal-error-recovery manifest must be a regular file with exactly one hard link"
            )
        if before.st_size > MAX_MANIFEST_BYTES:
            raise TerminalErrorRecoveryContractError(
                "terminal-error-recovery manifest exceeds the size limit"
            )

        raw_chunks: list[bytes] = []
        bytes_read = 0
        while True:
            chunk = os.read(fd, min(16 * 1024, MAX_MANIFEST_BYTES + 1 - bytes_read))
            if not chunk:
                break
            raw_chunks.append(chunk)
            bytes_read += len(chunk)
            if bytes_read > MAX_MANIFEST_BYTES:
                raise TerminalErrorRecoveryContractError(
                    "terminal-error-recovery manifest exceeds the size limit"
                )
        after = os.fstat(fd)
        current = path.lstat()
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise TerminalErrorRecoveryContractError(
                "terminal-error-recovery manifest changed while it was read"
            )
        if any(getattr(after, field) != getattr(current, field) for field in stable_fields):
            raise TerminalErrorRecoveryContractError(
                "terminal-error-recovery manifest pathname changed while it was read"
            )

        decoded = b"".join(raw_chunks).decode("utf-8", errors="strict")
        value = json.loads(decoded, object_pairs_hook=reject_duplicates)
    except TerminalErrorRecoveryContractError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalErrorRecoveryContractError(
            f"invalid terminal-error-recovery manifest: {path}"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
    return validate_terminal_error_recovery_plan(
        _require_mapping(value, "terminal-error-recovery manifest")
    )


def validate_terminal_error_recovery_apply_guards(
    plan: Mapping[str, Any],
    *,
    enabled: bool,
    allow_write: bool,
    expected_count: int | None,
    expected_plan_sha256: str | None,
) -> None:
    validated = validate_terminal_error_recovery_plan(plan)
    if not enabled:
        raise TerminalErrorRecoveryWriteDisabledError(
            "terminal-error-recovery execution is disabled; pass --enable-terminal-error-recovery explicitly"
        )
    if not allow_write:
        raise TerminalErrorRecoveryWriteDisabledError(
            "terminal-error-recovery execution requires --allow-write"
        )
    if expected_count != 1 or isinstance(expected_count, bool):
        raise TerminalErrorRecoveryContractError(
            "terminal-error-recovery execution requires --expected-count 1"
        )
    if expected_plan_sha256 != validated["plan_sha256"]:
        raise TerminalErrorRecoveryContractError(
            "terminal-error-recovery execution requires the exact --expected-plan-sha256"
        )


def _target_matches_plan(audio_root: Path, target: Mapping[str, Any]) -> bool:
    try:
        current = _load_direct_file_evidence(
            audio_root,
            _require_string(target.get("relative_path"), "target.relative_path"),
            label="terminal recovery target",
        )
    except TerminalErrorRecoveryConflictError:
        return False
    return current["size_bytes"] == int(target["expected_size_bytes"]) and current["sha256"] == str(
        target["expected_sha256"]
    )


def _row_matches_planned_error_state(
    row: sqlite3.Row,
    *,
    plan: Mapping[str, Any],
    error_root: Path,
    transcript_root: Path,
) -> bool:
    try:
        current = _error_job_payload(
            row,
            error_root=error_root,
            transcript_root=transcript_root,
        )
    except TerminalErrorRecoveryConflictError:
        return False
    return current == dict(plan["error_job"])


def _row_matches_recovered_state(
    row: sqlite3.Row,
    *,
    plan: Mapping[str, Any],
    audio_root: Path,
    transcript_root: Path,
) -> bool:
    try:
        if _require_positive_int(row["id"], "jobs.id") != int(plan["error_job"]["job_id"]):
            return False
        if _require_string(row["status"], "jobs.status") != db.STATUS_PENDING:
            return False
        if _require_string(row["current_step"], "jobs.current_step") != str(
            plan["recovery_policy"]["retry_current_step"]
        ):
            return False
        if row["started_at"] is not None or row["ended_at"] is not None:
            return False
        if _require_string(row["orig_name"], "jobs.orig_name") != str(plan["error_job"]["orig_name"]):
            return False
        if _safe_canonical_base(row["canonical_base"], "jobs.canonical_base") != str(
            plan["error_job"]["canonical_base"]
        ):
            return False
        target_relative_path = _validate_direct_child_relative_path(
            plan["target"]["relative_path"],
            label="target.relative_path",
        )
        if _row_path_relative(audio_root, row["canonical_audio_path"], label="jobs.canonical_audio_path") != target_relative_path:
            return False
        if _row_path_relative(transcript_root, row["transcript_txt_path"], label="jobs.transcript_txt_path") != str(
            plan["error_job"]["transcript_txt_relative_path"]
        ):
            return False
        if _row_path_relative(transcript_root, row["transcript_json_path"], label="jobs.transcript_json_path") != str(
            plan["error_job"]["transcript_json_relative_path"]
        ):
            return False
        if _require_sha256(row["sha256"], "jobs.sha256") != str(plan["error_job"]["sha256"]):
            return False
        metadata = json.loads(row["engine_params"] or "{}")
        if not isinstance(metadata, dict):
            return False
        if int(metadata.get("transcription_failures", 0) or 0) != 1:
            return False
        if int(metadata.get("transcription_max_retries", 0) or 0) != int(
            plan["recovery_policy"]["transcribe_max_retries"]
        ):
            return False
        recovery = metadata.get("terminal_error_recovery")
        if not isinstance(recovery, dict):
            return False
        return (
            recovery.get("schema_version") == RECOVERY_METADATA_SCHEMA_VERSION
            and recovery.get("plan_sha256") == plan["plan_sha256"]
            and recovery.get("source_error_relative_path") == plan["source"]["relative_path"]
            and recovery.get("target_audio_relative_path") == plan["target"]["relative_path"]
        )
    except (TerminalErrorRecoveryContractError, TypeError, ValueError, json.JSONDecodeError):
        return False


def revalidate_terminal_error_recovery_plan(
    config: Mapping[str, Any],
    conn: sqlite3.Connection,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    validated = validate_terminal_error_recovery_plan(plan)
    retry_limit = _require_retry_limit(config)
    if retry_limit != int(validated["recovery_policy"]["transcribe_max_retries"]):
        raise TerminalErrorRecoveryConflictError(
            "terminal-error-recovery retry policy changed after planning"
        )
    expected_bindings = validated["bindings"]
    current_bindings = _bindings(config)
    if {
        "db_path_sha256": current_bindings["db_path_sha256"],
        "audio_root_sha256": current_bindings["audio_root_sha256"],
        "error_root_sha256": current_bindings["error_root_sha256"],
        "transcript_root_sha256": current_bindings["transcript_root_sha256"],
    } != {
        "db_path_sha256": expected_bindings["db_path_sha256"],
        "audio_root_sha256": expected_bindings["audio_root_sha256"],
        "error_root_sha256": expected_bindings["error_root_sha256"],
        "transcript_root_sha256": expected_bindings["transcript_root_sha256"],
    }:
        raise TerminalErrorRecoveryConflictError(
            "terminal-error-recovery db/audio/error/transcript root binding changed after planning"
        )

    paths = _require_mapping(config.get("paths"), "config.paths")
    db_path = Path(_require_string(paths.get("db_path"), "config.paths.db_path"))
    audio_root = Path(
        _require_string(paths.get("stable_audio_folder"), "config.paths.stable_audio_folder")
    )
    error_root = Path(
        _require_string(paths.get("error_folder"), "config.paths.error_folder")
    )
    transcript_root = Path(
        _require_string(paths.get("transcript_folder"), "config.paths.transcript_folder")
    )
    expected_db_identity = _require_db_identity(
        expected_bindings["db_identity"],
        "bindings.db_identity",
    )
    _assert_db_path_identity(db_path, expected_db_identity, label="jobs db path")
    _assert_database_list_main_matches(conn, db_path, expected_db_identity)

    current_source = _load_direct_file_evidence(
        error_root,
        str(validated["source"]["relative_path"]),
        label="terminal recovery source",
    )
    if current_source != dict(validated["source"]):
        raise TerminalErrorRecoveryConflictError(
            "terminal recovery source changed after planning"
        )

    row = _select_job_row(conn, int(validated["error_job"]["job_id"]))
    target_path = audio_root / str(validated["target"]["relative_path"])
    if _row_matches_recovered_state(
        row,
        plan=validated,
        audio_root=audio_root,
        transcript_root=transcript_root,
    ):
        if not _target_matches_plan(audio_root, validated["target"]):
            raise TerminalErrorRecoveryConflictError(
                "terminal recovery target no longer matches the replayed plan"
            )
        return {
            "mode": "replayed",
            "job_id": int(validated["error_job"]["job_id"]),
            "source_path": error_root / str(validated["source"]["relative_path"]),
            "target_path": target_path,
        }

    if not _row_matches_planned_error_state(
        row,
        plan=validated,
        error_root=error_root,
        transcript_root=transcript_root,
    ):
        raise TerminalErrorRecoveryConflictError(
            "terminal recovery row changed after planning"
        )

    target_exists = target_path.exists()
    if target_exists and not _target_matches_plan(audio_root, validated["target"]):
        raise TerminalErrorRecoveryConflictError(
            "terminal recovery target exists but does not match the plan"
        )
    return {
        "mode": "forward_complete" if target_exists else "apply",
        "job_id": int(validated["error_job"]["job_id"]),
        "source_path": error_root / str(validated["source"]["relative_path"]),
        "target_path": target_path,
    }


def _fsync_directory(path: Path) -> None:
    fd = _open_root(path, label="fsync directory")
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise TerminalErrorRecoveryConflictError(
                "terminal recovery target write did not make forward progress"
            )
        offset += written


def _copy_source_to_target(
    *,
    error_root: Path,
    audio_root: Path,
    source_relative_path: str,
    target_relative_path: str,
    expected_source: Mapping[str, Any],
) -> None:
    error_root_fd = _open_root(error_root, label="error root")
    audio_root_fd = _open_root(audio_root, label="audio root")
    source_fd: int | None = None
    target_fd: int | None = None
    try:
        source_fd = os.open(
            source_relative_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=error_root_fd,
        )
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise TerminalErrorRecoveryConflictError(
                "terminal recovery source must remain a single-link regular file"
            )
        target_fd = os.open(
            target_relative_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=audio_root_fd,
        )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            _write_all(target_fd, chunk)
        os.fsync(target_fd)
        after = os.fstat(source_fd)
        current = os.stat(source_relative_path, dir_fd=error_root_fd, follow_symlinks=False)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise TerminalErrorRecoveryConflictError(
                "terminal recovery source changed while it was copied"
            )
        if any(getattr(after, field) != getattr(current, field) for field in stable_fields):
            raise TerminalErrorRecoveryConflictError(
                "terminal recovery source pathname changed while it was copied"
            )
        if digest.hexdigest() != str(expected_source["sha256"]) or int(after.st_size) != int(
            expected_source["size_bytes"]
        ):
            raise TerminalErrorRecoveryConflictError(
                "terminal recovery source no longer matches the planned evidence"
            )
    except Exception:
        if target_fd is not None:
            try:
                os.close(target_fd)
            except OSError:
                pass
            target_fd = None
        try:
            os.unlink(target_relative_path, dir_fd=audio_root_fd)
        except OSError:
            pass
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if target_fd is not None:
            os.close(target_fd)
        os.close(error_root_fd)
        os.close(audio_root_fd)
    _fsync_directory(audio_root)
    verified_target = _load_direct_file_evidence(
        audio_root,
        target_relative_path,
        label="terminal recovery target",
    )
    if (
        verified_target["sha256"] != str(expected_source["sha256"])
        or verified_target["size_bytes"] != int(expected_source["size_bytes"])
    ):
        raise TerminalErrorRecoveryConflictError(
            "terminal recovery target does not match the copied source"
        )


def _build_recovered_engine_params(
    row: sqlite3.Row,
    *,
    plan: Mapping[str, Any],
) -> str:
    raw = row["engine_params"]
    try:
        metadata = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise TerminalErrorRecoveryConflictError(
            "jobs.engine_params is not valid JSON"
        ) from exc
    if not isinstance(metadata, dict):
        raise TerminalErrorRecoveryConflictError("jobs.engine_params must be a JSON object")
    prior_metadata = json.loads(json.dumps(metadata, ensure_ascii=False))
    metadata["transcription_failures"] = 1
    metadata["transcription_max_retries"] = int(plan["recovery_policy"]["transcribe_max_retries"])
    metadata["terminal_error_recovery"] = {
        "schema_version": RECOVERY_METADATA_SCHEMA_VERSION,
        "plan_sha256": str(plan["plan_sha256"]),
        "source_error_relative_path": str(plan["source"]["relative_path"]),
        "target_audio_relative_path": str(plan["target"]["relative_path"]),
        "prior_status": str(plan["error_job"]["status"]),
        "prior_current_step": str(plan["error_job"]["current_step"]),
        "prior_error_message_sha256": _sha256_text(row["error_message"]),
        "prior_error_trace_sha256": _sha256_text(row["error_trace"]),
        "prior_engine_params": prior_metadata,
    }
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True)


def _require_active_kill_switch(kill_switch_path: Path | None) -> None:
    if kill_switch_path is None:
        return
    if not controller_kill_switch_is_active(kill_switch_path):
        raise TerminalErrorRecoveryConflictError(
            "terminal recovery kill switch changed before apply"
        )


def apply_terminal_error_recovery_plan(
    config: Mapping[str, Any],
    conn: sqlite3.Connection,
    plan: Mapping[str, Any],
    *,
    kill_switch_path: Path | None = None,
) -> dict[str, Any]:
    validated = validate_terminal_error_recovery_plan(plan)
    first_pass = revalidate_terminal_error_recovery_plan(config, conn, validated)
    paths = _require_mapping(config.get("paths"), "config.paths")
    audio_root = Path(
        _require_string(paths.get("stable_audio_folder"), "config.paths.stable_audio_folder")
    )
    error_root = Path(
        _require_string(paths.get("error_folder"), "config.paths.error_folder")
    )
    transcript_root = Path(
        _require_string(paths.get("transcript_folder"), "config.paths.transcript_folder")
    )

    if first_pass["mode"] == "replayed":
        return terminal_error_recovery_result(
            plan_sha256=str(validated["plan_sha256"]),
            status="skipped",
            job_id=int(validated["error_job"]["job_id"]),
        )

    if first_pass["mode"] == "apply":
        _require_active_kill_switch(kill_switch_path)
        _copy_source_to_target(
            error_root=error_root,
            audio_root=audio_root,
            source_relative_path=str(validated["source"]["relative_path"]),
            target_relative_path=str(validated["target"]["relative_path"]),
            expected_source=validated["source"],
        )

    second_pass = revalidate_terminal_error_recovery_plan(config, conn, validated)
    if second_pass["mode"] == "replayed":
        return terminal_error_recovery_result(
            plan_sha256=str(validated["plan_sha256"]),
            status="skipped",
            job_id=int(validated["error_job"]["job_id"]),
        )
    if second_pass["mode"] not in {"apply", "forward_complete"}:
        raise TerminalErrorRecoveryConflictError(
            "terminal-error-recovery entered an unsupported pre-commit state"
        )

    row = _select_job_row(conn, int(validated["error_job"]["job_id"]))
    if not _row_matches_planned_error_state(
        row,
        plan=validated,
        error_root=error_root,
        transcript_root=transcript_root,
    ):
        raise TerminalErrorRecoveryConflictError(
            "terminal recovery row changed before transition"
        )
    if not _target_matches_plan(audio_root, validated["target"]):
        raise TerminalErrorRecoveryConflictError(
            "terminal recovery target is not durable at apply time"
        )
    final_source = _load_direct_file_evidence(
        error_root,
        str(validated["source"]["relative_path"]),
        label="terminal recovery source",
    )
    if final_source != dict(validated["source"]):
        raise TerminalErrorRecoveryConflictError(
            "terminal recovery source changed after second revalidation"
        )

    new_engine_params = _build_recovered_engine_params(row, plan=validated)
    _require_active_kill_switch(kill_switch_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = _select_job_row(conn, int(validated["error_job"]["job_id"]))
        if not _row_matches_planned_error_state(
            current,
            plan=validated,
            error_root=error_root,
            transcript_root=transcript_root,
        ):
            raise TerminalErrorRecoveryConflictError(
                "terminal recovery row changed before exact transition"
            )
        updated_at = utils.now_iso()
        cur = conn.execute(
            "UPDATE jobs SET "
            "status = :pending, "
            "canonical_audio_path = :canonical_audio_path, "
            "current_step = :current_step, "
            "progress_pct = :progress_pct, "
            "eta_sec = :eta_sec, "
            "started_at = NULL, "
            "ended_at = NULL, "
            "engine_params = :engine_params, "
            "updated_at = :updated_at "
            "WHERE id = :job_id "
            "AND status = :error_status "
            "AND updated_at = :expected_updated_at "
            "AND current_step = :expected_current_step "
            "AND orig_name = :expected_orig_name "
            "AND canonical_base = :expected_canonical_base "
            "AND canonical_audio_path = :expected_canonical_audio_path "
            "AND transcript_txt_path = :expected_transcript_txt_path "
            "AND transcript_json_path = :expected_transcript_json_path "
            "AND sha256 = :expected_sha256",
            {
                "pending": db.STATUS_PENDING,
                "canonical_audio_path": os.fspath(audio_root / str(validated["target"]["relative_path"])),
                "current_step": str(validated["recovery_policy"]["retry_current_step"]),
                "progress_pct": 18,
                "eta_sec": None,
                "engine_params": new_engine_params,
                "updated_at": updated_at,
                "job_id": int(validated["error_job"]["job_id"]),
                "error_status": db.STATUS_ERROR,
                "expected_updated_at": str(validated["error_job"]["updated_at"]),
                "expected_current_step": str(validated["error_job"]["current_step"]),
                "expected_orig_name": str(validated["error_job"]["orig_name"]),
                "expected_canonical_base": str(validated["error_job"]["canonical_base"]),
                "expected_canonical_audio_path": os.fspath(
                    error_root / str(validated["error_job"]["error_audio_relative_path"])
                ),
                "expected_transcript_txt_path": os.fspath(
                    transcript_root / str(validated["error_job"]["transcript_txt_relative_path"])
                ),
                "expected_transcript_json_path": os.fspath(
                    transcript_root / str(validated["error_job"]["transcript_json_relative_path"])
                ),
                "expected_sha256": str(validated["error_job"]["sha256"]),
            },
        )
        if cur.rowcount != 1:
            raise TerminalErrorRecoveryConflictError(
                "terminal recovery exact row fence changed before transition"
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise

    final_row = _select_job_row(conn, int(validated["error_job"]["job_id"]))
    if not _row_matches_recovered_state(
        final_row,
        plan=validated,
        audio_root=audio_root,
        transcript_root=transcript_root,
    ):
        raise TerminalErrorRecoveryConflictError(
            "terminal recovery row does not match its committed replay contract"
        )
    return terminal_error_recovery_result(
        plan_sha256=str(validated["plan_sha256"]),
        status="applied",
        job_id=int(validated["error_job"]["job_id"]),
    )


def terminal_error_recovery_result(
    *,
    plan_sha256: str,
    status: str,
    job_id: int | None,
) -> dict[str, Any]:
    if status not in {"applied", "skipped", "busy"}:
        raise ValueError(f"unsupported terminal-error-recovery result status: {status}")
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "expected_count": 1,
        "plan_sha256": plan_sha256,
        "status": status,
        "job_id": job_id,
    }
