from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from lecture_stt.stt.single_job import worker_identity


PLAN_SCHEMA_VERSION = "lecture-stt/retry-job-plan@1"
RESULT_SCHEMA_VERSION = "lecture-stt/retry-job-result@1"
NEXT_PLAN_SCHEMA_VERSION = "lecture-stt/controller-next-retry@1"
MAX_MANIFEST_BYTES = 64 * 1024

_RETRY_STEP_RE = re.compile(r"^전사 재시도 대기 (\d+)/(\d+)$")

_PLAN_KEYS = {
    "schema_version",
    "expected_count",
    "bindings",
    "retry_job",
    "audio",
    "worker",
    "plan_sha256",
}
_BINDING_KEYS = {
    "db_path_sha256",
    "db_identity",
    "audio_root_sha256",
    "transcript_root_sha256",
}
_DB_IDENTITY_KEYS = {"device", "inode", "mode", "nlink"}
_RETRY_JOB_KEYS = {
    "job_id",
    "status",
    "current_step",
    "updated_at",
    "failures",
    "max_retries",
    "orig_name",
    "canonical_base",
    "canonical_audio_relative_path",
    "transcript_txt_relative_path",
    "transcript_json_relative_path",
}
_AUDIO_KEYS = {
    "relative_path",
    "device",
    "inode",
    "size_bytes",
    "mtime_ns",
    "ctime_ns",
    "sha256",
}
_WORKER_KEYS = {"profile", "config_sha256"}
_PROFILE_KEYS = {"key", "version", "config_sha256"}
_NEXT_PLAN_KEYS = {"schema_version", "status", "plan"}


class RetryJobContractError(RuntimeError):
    pass


class RetryJobWriteDisabledError(RetryJobContractError):
    pass


class RetryJobConflictError(RetryJobContractError):
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


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise RetryJobContractError(
            f"{label} keys mismatch: missing={missing} unexpected={unexpected}"
        )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RetryJobContractError(f"{label} must be an object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RetryJobContractError(f"{label} must be a non-empty string")
    return value


def _require_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RetryJobContractError(f"{label} must be a non-negative integer")
    return value


def _require_positive_int(value: Any, label: str) -> int:
    result = _require_nonnegative_int(value, label)
    if result == 0:
        raise RetryJobContractError(f"{label} must be a positive integer")
    return result


def _require_sha256(value: Any, label: str) -> str:
    digest = _require_string(value, label)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise RetryJobContractError(f"{label} must be a lowercase SHA-256")
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
    transcript_root = Path(
        _require_string(paths.get("transcript_folder"), "config.paths.transcript_folder")
    )
    return {
        "db_path_sha256": _path_binding_sha256(db_path),
        "audio_root_sha256": _path_binding_sha256(audio_root),
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
        raise RetryJobConflictError(f"{label} is not safely openable: {path}") from exc
    root_stat = os.fstat(root_fd)
    if not stat.S_ISDIR(root_stat.st_mode):
        os.close(root_fd)
        raise RetryJobConflictError(f"{label} is not a directory: {path}")
    return root_fd


def _validate_direct_child_relative_path(value: Any, *, label: str) -> str:
    relative_path = _require_string(value, label)
    if "\x00" in relative_path:
        raise RetryJobContractError(f"{label} contains NUL")
    candidate = Path(relative_path)
    if candidate.is_absolute() or len(candidate.parts) != 1:
        raise RetryJobContractError(f"{label} must name exactly one direct child")
    if candidate.name in {"", ".", ".."} or candidate.name != relative_path:
        raise RetryJobContractError(f"{label} must name exactly one direct child")
    return relative_path


def _load_audio_evidence(audio_root: Path, relative_path: str) -> dict[str, Any]:
    relative_path = _validate_direct_child_relative_path(
        relative_path,
        label="retry_job.canonical_audio_relative_path",
    )
    root_fd = _open_root(audio_root, label="stable audio root")
    audio_fd: int | None = None
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            audio_fd = os.open(relative_path, flags, dir_fd=root_fd)
        except OSError as exc:
            raise RetryJobConflictError(
                f"retry audio is not safely openable: {relative_path}"
            ) from exc

        before = os.fstat(audio_fd)
        if not stat.S_ISREG(before.st_mode):
            raise RetryJobConflictError("retry audio must be a regular file")
        if before.st_nlink != 1:
            raise RetryJobConflictError("retry audio must have exactly one hard link")

        digest = hashlib.sha256()
        while True:
            chunk = os.read(audio_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)

        after = os.fstat(audio_fd)
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
            raise RetryJobConflictError("retry audio changed while it was hashed")
        if any(getattr(after, field) != getattr(current, field) for field in stable_fields):
            raise RetryJobConflictError("retry audio pathname changed while it was hashed")
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise RetryJobConflictError("retry audio pathname is not a single-link regular file")

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
        if audio_fd is not None:
            os.close(audio_fd)
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
            raise RetryJobConflictError("jobs db must be a regular file")
        if db_stat.st_nlink != 1:
            os.close(fd)
            raise RetryJobConflictError("jobs db must have exactly one hard link")
        return fd, _db_identity_from_stat_result(db_stat)
    except OSError as exc:
        raise RetryJobConflictError(f"jobs db is not safely openable: {path}") from exc


def _assert_db_path_identity(path: Path, expected_identity: Mapping[str, Any], *, label: str) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise RetryJobConflictError(f"{label} is not safely stat-able: {path}") from exc
    current_identity = _db_identity_from_stat_result(current)
    if current_identity != dict(expected_identity):
        raise RetryJobConflictError(f"{label} identity changed")
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise RetryJobConflictError(f"{label} is not a single-link regular file")


def _assert_database_list_main_matches(
    conn: sqlite3.Connection,
    configured_db_path: Path,
    expected_identity: Mapping[str, Any],
) -> None:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error as exc:
        raise RetryJobConflictError("sqlite main database is unavailable") from exc
    if row is None or row[1] != "main":
        raise RetryJobConflictError("sqlite main database is unavailable")
    opened_filename = row[2]
    if not isinstance(opened_filename, str) or not opened_filename:
        raise RetryJobConflictError("sqlite main database filename is unavailable")
    opened_path = Path(opened_filename)
    if os.path.realpath(os.fspath(opened_path)) != os.path.realpath(os.fspath(configured_db_path)):
        raise RetryJobConflictError("sqlite main database does not resolve to the configured db path")
    _assert_db_path_identity(opened_path, expected_identity, label="sqlite main database")


def _readonly_db_connection(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(os.fspath(db_path))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise RetryJobConflictError(f"unable to open jobs db read-only: {db_path}") from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.Error as exc:
        conn.close()
        raise RetryJobConflictError(
            f"unable to configure jobs db read-only: {db_path}"
        ) from exc
    return conn


def _parse_retry_step(step: str, *, label: str) -> tuple[int, int]:
    match = _RETRY_STEP_RE.fullmatch(step)
    if match is None:
        raise RetryJobConflictError(f"{label} is not an exact retry step")
    failures = int(match.group(1))
    max_retries = int(match.group(2))
    if failures <= 0 or max_retries < failures:
        raise RetryJobConflictError(f"{label} has invalid retry counters")
    return failures, max_retries


def _retry_row_payload(row: sqlite3.Row) -> dict[str, Any]:
    status = _require_string(row["status"], "jobs.status")
    current_step = _require_string(row["current_step"], "jobs.current_step")
    updated_at = _require_string(row["updated_at"], "jobs.updated_at")
    orig_name = _require_string(row["orig_name"], "jobs.orig_name")
    canonical_base = _require_string(row["canonical_base"], "jobs.canonical_base")
    canonical_audio_path = Path(
        _require_string(row["canonical_audio_path"], "jobs.canonical_audio_path")
    )
    transcript_txt_path = Path(
        _require_string(row["transcript_txt_path"], "jobs.transcript_txt_path")
    )
    transcript_json_path = Path(
        _require_string(row["transcript_json_path"], "jobs.transcript_json_path")
    )
    failures, max_retries = _parse_retry_step(current_step, label="jobs.current_step")
    engine_params_raw = row["engine_params"]
    try:
        engine_params = json.loads(engine_params_raw) if engine_params_raw else {}
    except json.JSONDecodeError as exc:
        raise RetryJobConflictError("jobs.engine_params is not valid JSON") from exc
    if not isinstance(engine_params, dict):
        raise RetryJobConflictError("jobs.engine_params must be a JSON object")
    metadata_failures = engine_params.get("transcription_failures")
    metadata_max_retries = engine_params.get("transcription_max_retries")
    if metadata_failures != failures or metadata_max_retries != max_retries:
        raise RetryJobConflictError("retry metadata does not match jobs.current_step")
    return {
        "job_id": _require_positive_int(row["id"], "jobs.id"),
        "status": status,
        "current_step": current_step,
        "updated_at": updated_at,
        "failures": failures,
        "max_retries": max_retries,
        "orig_name": orig_name,
        "canonical_base": canonical_base,
        "canonical_audio_path": canonical_audio_path,
        "transcript_txt_path": transcript_txt_path,
        "transcript_json_path": transcript_json_path,
        "engine_params_raw": engine_params_raw or "{}",
    }


def _select_retry_row(conn: sqlite3.Connection, *, job_id: int | None) -> sqlite3.Row:
    query = (
        "SELECT id, status, current_step, updated_at, orig_name, canonical_base, "
        "canonical_audio_path, transcript_txt_path, transcript_json_path, engine_params "
        "FROM jobs WHERE status = ? AND current_step LIKE ?"
    )
    params: list[Any] = ["PENDING", "전사 재시도 대기 %"]
    try:
        if job_id is not None:
            params.append(job_id)
            row = conn.execute(f"{query} AND id = ?", params).fetchone()
            if row is None:
                raise RetryJobConflictError("job_id is not an eligible retry job")
            return row

        rows = conn.execute(
            f"{query} ORDER BY updated_at ASC, id ASC",
            params,
        ).fetchall()
    except RetryJobConflictError:
        raise
    except sqlite3.Error as exc:
        raise RetryJobConflictError("unable to read the retry queue") from exc
    if not rows:
        raise RetryJobConflictError("expected exactly one retry job candidate, found 0")
    if len(rows) != 1:
        raise RetryJobConflictError(
            f"expected exactly one retry job candidate, found {len(rows)}; pass job_id explicitly"
        )
    return rows[0]


def _select_oldest_retry_job_id(conn: sqlite3.Connection) -> int | None:
    query = (
        "SELECT id FROM jobs WHERE status = ? AND current_step LIKE ? "
        "ORDER BY updated_at ASC, id ASC LIMIT 1"
    )
    try:
        row = conn.execute(query, ["PENDING", "전사 재시도 대기 %"]).fetchone()
    except sqlite3.Error as exc:
        raise RetryJobConflictError("unable to read the retry queue") from exc
    if row is None:
        return None
    return _require_positive_int(row["id"], "jobs.id")


def _canonical_audio_relative_path(audio_root: Path, canonical_audio_path: Path) -> str:
    try:
        relative_path = canonical_audio_path.relative_to(audio_root)
    except ValueError as exc:
        raise RetryJobConflictError("retry audio path is outside the configured stable audio root") from exc
    return _validate_direct_child_relative_path(
        os.fspath(relative_path),
        label="retry_job.canonical_audio_relative_path",
    )


def _transcript_relative_path(
    transcript_root: Path,
    transcript_path: Path,
    *,
    canonical_base: str,
    suffix: str,
    label: str,
) -> str:
    try:
        relative_path = transcript_path.relative_to(transcript_root)
    except ValueError as exc:
        raise RetryJobConflictError(
            f"{label} is outside the configured transcript root"
        ) from exc
    relative_name = _validate_direct_child_relative_path(
        os.fspath(relative_path),
        label=label,
    )
    if relative_name != f"{canonical_base}{suffix}":
        raise RetryJobConflictError(
            f"{label} does not match the retry job canonical base"
        )
    return relative_name


def build_retry_job_plan(
    config: Mapping[str, Any],
    *,
    job_id: int | None = None,
) -> dict[str, Any]:
    if job_id is not None:
        _require_positive_int(job_id, "job_id")
    paths = _require_mapping(config.get("paths"), "config.paths")
    db_path = Path(_require_string(paths.get("db_path"), "config.paths.db_path"))
    audio_root = Path(
        _require_string(paths.get("stable_audio_folder"), "config.paths.stable_audio_folder")
    )
    transcript_root = Path(
        _require_string(paths.get("transcript_folder"), "config.paths.transcript_folder")
    )
    db_guard_fd, db_identity = _open_db_guard(db_path)
    conn: sqlite3.Connection | None = None
    try:
        conn = _readonly_db_connection(db_path)
        _assert_database_list_main_matches(conn, db_path, db_identity)
        row = _select_retry_row(conn, job_id=job_id)
        row_payload = _retry_row_payload(row)
    finally:
        if conn is not None:
            conn.close()
        os.close(db_guard_fd)
    _assert_db_path_identity(db_path, db_identity, label="jobs db path")
    relative_path = _canonical_audio_relative_path(audio_root, row_payload["canonical_audio_path"])
    transcript_txt_relative_path = _transcript_relative_path(
        transcript_root,
        row_payload["transcript_txt_path"],
        canonical_base=row_payload["canonical_base"],
        suffix=".txt",
        label="retry_job.transcript_txt_relative_path",
    )
    transcript_json_relative_path = _transcript_relative_path(
        transcript_root,
        row_payload["transcript_json_path"],
        canonical_base=row_payload["canonical_base"],
        suffix=".json",
        label="retry_job.transcript_json_relative_path",
    )
    retry_job = {
        "job_id": row_payload["job_id"],
        "status": row_payload["status"],
        "current_step": row_payload["current_step"],
        "updated_at": row_payload["updated_at"],
        "failures": row_payload["failures"],
        "max_retries": row_payload["max_retries"],
        "orig_name": row_payload["orig_name"],
        "canonical_base": row_payload["canonical_base"],
        "canonical_audio_relative_path": relative_path,
        "transcript_txt_relative_path": transcript_txt_relative_path,
        "transcript_json_relative_path": transcript_json_relative_path,
    }
    payload: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "expected_count": 1,
        "bindings": {
            **_bindings(config),
            "db_identity": db_identity,
        },
        "retry_job": retry_job,
        "audio": _load_audio_evidence(audio_root, relative_path),
        "worker": worker_identity(config),
    }
    payload["plan_sha256"] = _sha256_json(payload)
    return payload


def build_next_retry_job_plan(config: Mapping[str, Any]) -> dict[str, Any]:
    paths = _require_mapping(config.get("paths"), "config.paths")
    db_path = Path(_require_string(paths.get("db_path"), "config.paths.db_path"))
    db_guard_fd, db_identity = _open_db_guard(db_path)
    conn: sqlite3.Connection | None = None
    try:
        conn = _readonly_db_connection(db_path)
        _assert_database_list_main_matches(conn, db_path, db_identity)
        job_id = _select_oldest_retry_job_id(conn)
    finally:
        if conn is not None:
            conn.close()
        os.close(db_guard_fd)
    _assert_db_path_identity(db_path, db_identity, label="jobs db path")
    if job_id is None:
        return {
            "schema_version": NEXT_PLAN_SCHEMA_VERSION,
            "status": "empty",
            "plan": None,
        }
    return {
        "schema_version": NEXT_PLAN_SCHEMA_VERSION,
        "status": "planned",
        "plan": build_retry_job_plan(config, job_id=job_id),
    }


def validate_next_retry_job_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    _require_exact_keys(value, _NEXT_PLAN_KEYS, "controller-next-retry payload")
    if value.get("schema_version") != NEXT_PLAN_SCHEMA_VERSION:
        raise RetryJobContractError(
            f"unsupported controller-next-retry schema: {value.get('schema_version')!r}"
        )
    status = _require_string(value.get("status"), "status")
    retry_job_plan = value.get("plan")
    if status == "empty":
        if retry_job_plan is not None:
            raise RetryJobContractError(
                "controller-next-retry empty payload must set plan to null"
            )
        return {
            "schema_version": NEXT_PLAN_SCHEMA_VERSION,
            "status": "empty",
            "plan": None,
        }
    if status != "planned":
        raise RetryJobContractError(
            "controller-next-retry status must be either 'empty' or 'planned'"
        )
    if not isinstance(retry_job_plan, Mapping):
        raise RetryJobContractError(
            "controller-next-retry planned payload must include plan"
        )
    return {
        "schema_version": NEXT_PLAN_SCHEMA_VERSION,
        "status": "planned",
        "plan": validate_retry_job_plan(retry_job_plan),
    }


def validate_retry_job_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    _require_exact_keys(plan, _PLAN_KEYS, "retry-job plan")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise RetryJobContractError(
            f"unsupported retry-job plan schema: {plan.get('schema_version')!r}"
        )
    if plan.get("expected_count") != 1 or isinstance(plan.get("expected_count"), bool):
        raise RetryJobContractError("retry-job plan expected_count must be exactly 1")

    bindings = _require_mapping(plan.get("bindings"), "bindings")
    _require_exact_keys(bindings, _BINDING_KEYS, "bindings")
    _require_sha256(bindings.get("db_path_sha256"), "bindings.db_path_sha256")
    _require_sha256(bindings.get("audio_root_sha256"), "bindings.audio_root_sha256")
    _require_sha256(
        bindings.get("transcript_root_sha256"),
        "bindings.transcript_root_sha256",
    )
    _require_db_identity(bindings.get("db_identity"), "bindings.db_identity")

    retry_job = _require_mapping(plan.get("retry_job"), "retry_job")
    _require_exact_keys(retry_job, _RETRY_JOB_KEYS, "retry_job")
    _require_positive_int(retry_job.get("job_id"), "retry_job.job_id")
    if _require_string(retry_job.get("status"), "retry_job.status") != "PENDING":
        raise RetryJobContractError("retry_job.status must be PENDING")
    current_step = _require_string(retry_job.get("current_step"), "retry_job.current_step")
    failures, max_retries = _parse_retry_step(current_step, label="retry_job.current_step")
    if _require_nonnegative_int(retry_job.get("failures"), "retry_job.failures") != failures:
        raise RetryJobContractError("retry_job.failures does not match retry_job.current_step")
    if _require_nonnegative_int(retry_job.get("max_retries"), "retry_job.max_retries") != max_retries:
        raise RetryJobContractError("retry_job.max_retries does not match retry_job.current_step")
    _require_string(retry_job.get("updated_at"), "retry_job.updated_at")
    _require_string(retry_job.get("orig_name"), "retry_job.orig_name")
    _require_string(retry_job.get("canonical_base"), "retry_job.canonical_base")
    relative_path = _validate_direct_child_relative_path(
        retry_job.get("canonical_audio_relative_path"),
        label="retry_job.canonical_audio_relative_path",
    )
    canonical_base = _require_string(
        retry_job.get("canonical_base"),
        "retry_job.canonical_base",
    )
    transcript_txt_relative_path = _validate_direct_child_relative_path(
        retry_job.get("transcript_txt_relative_path"),
        label="retry_job.transcript_txt_relative_path",
    )
    transcript_json_relative_path = _validate_direct_child_relative_path(
        retry_job.get("transcript_json_relative_path"),
        label="retry_job.transcript_json_relative_path",
    )
    if transcript_txt_relative_path != f"{canonical_base}.txt":
        raise RetryJobContractError(
            "retry_job.transcript_txt_relative_path must match canonical_base"
        )
    if transcript_json_relative_path != f"{canonical_base}.json":
        raise RetryJobContractError(
            "retry_job.transcript_json_relative_path must match canonical_base"
        )

    audio = _require_mapping(plan.get("audio"), "audio")
    _require_exact_keys(audio, _AUDIO_KEYS, "audio")
    if _validate_direct_child_relative_path(audio.get("relative_path"), label="audio.relative_path") != relative_path:
        raise RetryJobContractError("audio.relative_path must match retry_job.canonical_audio_relative_path")
    for key in ("device", "inode", "size_bytes", "mtime_ns", "ctime_ns"):
        _require_nonnegative_int(audio.get(key), f"audio.{key}")
    _require_sha256(audio.get("sha256"), "audio.sha256")

    worker = _require_mapping(plan.get("worker"), "worker")
    _require_exact_keys(worker, _WORKER_KEYS, "worker")
    profile = _require_mapping(worker.get("profile"), "worker.profile")
    _require_exact_keys(profile, _PROFILE_KEYS, "worker.profile")
    _require_string(profile.get("key"), "worker.profile.key")
    _require_string(profile.get("version"), "worker.profile.version")
    _require_sha256(profile.get("config_sha256"), "worker.profile.config_sha256")
    _require_sha256(worker.get("config_sha256"), "worker.config_sha256")

    plan_sha256 = _require_sha256(plan.get("plan_sha256"), "plan_sha256")
    digest_input = dict(plan)
    digest_input.pop("plan_sha256")
    if _sha256_json(digest_input) != plan_sha256:
        raise RetryJobContractError("retry-job plan SHA-256 does not match its closed payload")
    return json.loads(_canonical_json(plan))


def load_retry_job_plan(path: Path) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RetryJobContractError(f"duplicate JSON key in retry-job manifest: {key}")
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
            raise RetryJobContractError(
                "retry-job manifest must be a regular file with exactly one hard link"
            )
        if before.st_size > MAX_MANIFEST_BYTES:
            raise RetryJobContractError("retry-job manifest exceeds the size limit")

        raw_chunks: list[bytes] = []
        bytes_read = 0
        while True:
            chunk = os.read(fd, min(16 * 1024, MAX_MANIFEST_BYTES + 1 - bytes_read))
            if not chunk:
                break
            raw_chunks.append(chunk)
            bytes_read += len(chunk)
            if bytes_read > MAX_MANIFEST_BYTES:
                raise RetryJobContractError("retry-job manifest exceeds the size limit")
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
            raise RetryJobContractError("retry-job manifest changed while it was read")
        if any(getattr(after, field) != getattr(current, field) for field in stable_fields):
            raise RetryJobContractError("retry-job manifest pathname changed while it was read")

        decoded = b"".join(raw_chunks).decode("utf-8", errors="strict")
        value = json.loads(decoded, object_pairs_hook=reject_duplicates)
    except RetryJobContractError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetryJobContractError(f"invalid retry-job manifest: {path}") from exc
    finally:
        if fd is not None:
            os.close(fd)
    return validate_retry_job_plan(_require_mapping(value, "retry-job manifest"))


def validate_retry_apply_guards(
    plan: Mapping[str, Any],
    *,
    enabled: bool,
    allow_write: bool,
    expected_count: int | None,
    expected_plan_sha256: str | None,
) -> None:
    validated = validate_retry_job_plan(plan)
    if not enabled:
        raise RetryJobWriteDisabledError(
            "retry-job execution is disabled; pass --enable-retry-job explicitly"
        )
    if not allow_write:
        raise RetryJobWriteDisabledError("retry-job execution requires --allow-write")
    if expected_count != 1 or isinstance(expected_count, bool):
        raise RetryJobContractError("retry-job execution requires --expected-count 1")
    if expected_plan_sha256 != validated["plan_sha256"]:
        raise RetryJobContractError(
            "retry-job execution requires the exact --expected-plan-sha256"
        )


def revalidate_retry_job_plan(
    config: Mapping[str, Any],
    conn: sqlite3.Connection,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    validated = validate_retry_job_plan(plan)
    if worker_identity(config) != validated["worker"]:
        raise RetryJobConflictError("retry-job worker config/profile changed after planning")
    expected_bindings = validated["bindings"]
    current_bindings = _bindings(config)
    if {
        "db_path_sha256": current_bindings["db_path_sha256"],
        "audio_root_sha256": current_bindings["audio_root_sha256"],
        "transcript_root_sha256": current_bindings["transcript_root_sha256"],
    } != {
        "db_path_sha256": expected_bindings["db_path_sha256"],
        "audio_root_sha256": expected_bindings["audio_root_sha256"],
        "transcript_root_sha256": expected_bindings["transcript_root_sha256"],
    }:
        raise RetryJobConflictError(
            "retry-job db/audio/transcript root binding changed after planning"
        )

    retry_plan = validated["retry_job"]
    paths = _require_mapping(config.get("paths"), "config.paths")
    db_path = Path(_require_string(paths.get("db_path"), "config.paths.db_path"))
    expected_db_identity = _require_db_identity(expected_bindings["db_identity"], "bindings.db_identity")
    _assert_db_path_identity(db_path, expected_db_identity, label="jobs db path")
    _assert_database_list_main_matches(conn, db_path, expected_db_identity)
    row = _select_retry_row(conn, job_id=int(retry_plan["job_id"]))
    row_payload = _retry_row_payload(row)
    audio_root = Path(
        _require_string(paths.get("stable_audio_folder"), "config.paths.stable_audio_folder")
    )
    transcript_root = Path(
        _require_string(paths.get("transcript_folder"), "config.paths.transcript_folder")
    )
    relative_path = _canonical_audio_relative_path(audio_root, row_payload["canonical_audio_path"])
    transcript_txt_relative_path = _transcript_relative_path(
        transcript_root,
        row_payload["transcript_txt_path"],
        canonical_base=row_payload["canonical_base"],
        suffix=".txt",
        label="retry_job.transcript_txt_relative_path",
    )
    transcript_json_relative_path = _transcript_relative_path(
        transcript_root,
        row_payload["transcript_json_path"],
        canonical_base=row_payload["canonical_base"],
        suffix=".json",
        label="retry_job.transcript_json_relative_path",
    )
    current_retry_job = {
        "job_id": row_payload["job_id"],
        "status": row_payload["status"],
        "current_step": row_payload["current_step"],
        "updated_at": row_payload["updated_at"],
        "failures": row_payload["failures"],
        "max_retries": row_payload["max_retries"],
        "orig_name": row_payload["orig_name"],
        "canonical_base": row_payload["canonical_base"],
        "canonical_audio_relative_path": relative_path,
        "transcript_txt_relative_path": transcript_txt_relative_path,
        "transcript_json_relative_path": transcript_json_relative_path,
    }
    if current_retry_job != retry_plan:
        raise RetryJobConflictError("retry-job row changed after planning")

    current_audio = _load_audio_evidence(audio_root, relative_path)
    if current_audio != validated["audio"]:
        raise RetryJobConflictError("retry audio changed after planning")

    return {
        "job_id": int(retry_plan["job_id"]),
        "canonical_audio_path": audio_root / relative_path,
        "claim_fence": {
            "updated_at": row_payload["updated_at"],
            "current_step": row_payload["current_step"],
            "orig_name": row_payload["orig_name"],
            "canonical_base": row_payload["canonical_base"],
            "canonical_audio_path": os.fspath(audio_root / relative_path),
            "transcript_txt_path": os.fspath(
                transcript_root / transcript_txt_relative_path
            ),
            "transcript_json_path": os.fspath(
                transcript_root / transcript_json_relative_path
            ),
            "engine_params": row_payload["engine_params_raw"],
        },
    }


def retry_job_result(
    *,
    plan_sha256: str,
    status: str,
    job_id: int | None,
) -> dict[str, Any]:
    if status not in {
        "completed",
        "needs_review",
        "retry_pending",
        "busy",
        "failed",
    }:
        raise ValueError(f"unsupported retry-job result status: {status}")
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "expected_count": 1,
        "plan_sha256": plan_sha256,
        "status": status,
        "job_id": job_id,
    }
