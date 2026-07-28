from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping

from lecture_stt.shared import utils
from lecture_stt.stt.profiles import merge_transcribe_config, resolve_profile


PLAN_SCHEMA_VERSION = "lecture-stt/single-job-plan@1"
RESULT_SCHEMA_VERSION = "lecture-stt/single-job-result@1"
MAX_MANIFEST_BYTES = 64 * 1024

_PLAN_KEYS = {
    "schema_version",
    "expected_count",
    "source",
    "worker",
    "plan_sha256",
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
_WORKER_KEYS = {"profile", "config_sha256"}
_PROFILE_KEYS = {"key", "version", "config_sha256"}


class SingleJobContractError(RuntimeError):
    pass


class SingleJobWriteDisabledError(SingleJobContractError):
    pass


class SingleJobConflictError(SingleJobContractError):
    pass


def _stable_for_sec(config: Mapping[str, Any]) -> int:
    app = _require_mapping(config.get("app"), "config.app")
    return _require_nonnegative_int(app.get("stable_for_sec"), "config.app.stable_for_sec")


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
        raise SingleJobContractError(
            f"{label} keys mismatch: missing={missing} unexpected={unexpected}"
        )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SingleJobContractError(f"{label} must be an object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SingleJobContractError(f"{label} must be a non-empty string")
    return value


def _require_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SingleJobContractError(f"{label} must be a non-negative integer")
    return value


def _require_sha256(value: Any, label: str) -> str:
    digest = _require_string(value, label)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise SingleJobContractError(f"{label} must be a lowercase SHA-256")
    return digest


def _validate_relative_source_path(value: Any) -> str:
    relative_path = _require_string(value, "source.relative_path")
    if "\x00" in relative_path:
        raise SingleJobContractError("source.relative_path contains NUL")
    candidate = Path(relative_path)
    if candidate.is_absolute() or len(candidate.parts) != 1:
        raise SingleJobContractError(
            "source.relative_path must name exactly one direct child of the watch root"
        )
    if candidate.name in {"", ".", ".."} or candidate.name != relative_path:
        raise SingleJobContractError(
            "source.relative_path must name exactly one direct child of the watch root"
        )
    if utils.is_temporary_file(candidate):
        raise SingleJobContractError(
            "source.relative_path is excluded by the canonical polling watcher"
        )
    return relative_path


def _open_root(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(path, flags)
    except OSError as exc:
        raise SingleJobConflictError(f"watch root is not safely openable: {path}") from exc
    root_stat = os.fstat(root_fd)
    if not stat.S_ISDIR(root_stat.st_mode):
        os.close(root_fd)
        raise SingleJobConflictError(f"watch root is not a directory: {path}")
    return root_fd


def _source_evidence(watch_root: Path, relative_path: str) -> dict[str, Any]:
    relative_path = _validate_relative_source_path(relative_path)
    root_fd = _open_root(watch_root)
    source_fd: int | None = None
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            source_fd = os.open(relative_path, flags, dir_fd=root_fd)
        except OSError as exc:
            raise SingleJobConflictError(
                f"single-job source is not safely openable: {relative_path}"
            ) from exc

        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise SingleJobConflictError("single-job source must be a regular file")
        if before.st_nlink != 1:
            raise SingleJobConflictError("single-job source must have exactly one hard link")

        digest = hashlib.sha256()
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)

        after = os.fstat(source_fd)
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
            raise SingleJobConflictError("single-job source changed while it was hashed")
        if any(getattr(after, field) != getattr(current, field) for field in stable_fields):
            raise SingleJobConflictError("single-job source pathname changed while it was hashed")
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise SingleJobConflictError("single-job source pathname is not a single-link regular file")

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
        if source_fd is not None:
            os.close(source_fd)
        os.close(root_fd)


def worker_identity(config: Mapping[str, Any]) -> dict[str, Any]:
    profile = resolve_profile(config)
    transcribe = merge_transcribe_config(
        _require_mapping(config.get("transcribe"), "config.transcribe"),
        profile,
    )
    paths = _require_mapping(config.get("paths"), "config.paths")
    app = _require_mapping(config.get("app"), "config.app")
    ffmpeg = _require_mapping(config.get("ffmpeg"), "config.ffmpeg")
    identity_input = {
        "paths": {
            key: str(paths.get(key, ""))
            for key in (
                "watch_folder",
                "stable_audio_folder",
                "transcript_folder",
                "error_folder",
                "tmp_dir",
                "db_path",
            )
        },
        "app": {
            key: app.get(key)
            for key in (
                "stable_for_sec",
                "stale_processing_hours",
                "transcribe_max_retries",
            )
        },
        "ffmpeg": {"binary_path": ffmpeg.get("binary_path")},
        "engine": dict(_require_mapping(config.get("engine") or {}, "config.engine")),
        "transcribe": dict(transcribe),
        "profile": profile.snapshot(),
    }
    return {
        "profile": profile.snapshot(),
        "config_sha256": _sha256_json(identity_input),
    }


def _enforce_stability_window(config: Mapping[str, Any], source: Mapping[str, Any]) -> None:
    stable_for_sec = _stable_for_sec(config)
    if stable_for_sec == 0:
        return
    mtime_ns = _require_nonnegative_int(source.get("mtime_ns"), "source.mtime_ns")
    age_ns = max(0, int(utils.now() * 1_000_000_000) - mtime_ns)
    if age_ns < stable_for_sec * 1_000_000_000:
        raise SingleJobConflictError(
            f"single-job source has not satisfied stable_for_sec={stable_for_sec}"
        )


def build_single_job_plan(
    config: Mapping[str, Any],
    relative_path: str,
) -> dict[str, Any]:
    paths = _require_mapping(config.get("paths"), "config.paths")
    watch_root = Path(_require_string(paths.get("watch_folder"), "config.paths.watch_folder"))
    source = _source_evidence(watch_root, relative_path)
    _enforce_stability_window(config, source)
    payload: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "expected_count": 1,
        "source": source,
        "worker": worker_identity(config),
    }
    payload["plan_sha256"] = _sha256_json(payload)
    return payload


def validate_single_job_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    _require_exact_keys(plan, _PLAN_KEYS, "single-job plan")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise SingleJobContractError(
            f"unsupported single-job plan schema: {plan.get('schema_version')!r}"
        )
    if plan.get("expected_count") != 1 or isinstance(plan.get("expected_count"), bool):
        raise SingleJobContractError("single-job plan expected_count must be exactly 1")

    source = _require_mapping(plan.get("source"), "source")
    _require_exact_keys(source, _SOURCE_KEYS, "source")
    _validate_relative_source_path(source.get("relative_path"))
    for key in ("device", "inode", "size_bytes", "mtime_ns", "ctime_ns"):
        _require_nonnegative_int(source.get(key), f"source.{key}")
    _require_sha256(source.get("sha256"), "source.sha256")

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
        raise SingleJobContractError("single-job plan SHA-256 does not match its closed payload")
    return json.loads(_canonical_json(plan))


def load_single_job_plan(path: Path) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SingleJobContractError(f"duplicate JSON key in single-job manifest: {key}")
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
            raise SingleJobContractError(
                "single-job manifest must be a regular file with exactly one hard link"
            )
        if before.st_size > MAX_MANIFEST_BYTES:
            raise SingleJobContractError("single-job manifest exceeds the size limit")

        raw_chunks: list[bytes] = []
        bytes_read = 0
        while True:
            chunk = os.read(fd, min(16 * 1024, MAX_MANIFEST_BYTES + 1 - bytes_read))
            if not chunk:
                break
            raw_chunks.append(chunk)
            bytes_read += len(chunk)
            if bytes_read > MAX_MANIFEST_BYTES:
                raise SingleJobContractError("single-job manifest exceeds the size limit")
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
            raise SingleJobContractError("single-job manifest changed while it was read")
        if any(getattr(after, field) != getattr(current, field) for field in stable_fields):
            raise SingleJobContractError("single-job manifest pathname changed while it was read")

        decoded = b"".join(raw_chunks).decode("utf-8", errors="strict")
        value = json.loads(decoded, object_pairs_hook=reject_duplicates)
    except SingleJobContractError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SingleJobContractError(f"invalid single-job manifest: {path}") from exc
    finally:
        if fd is not None:
            os.close(fd)
    return validate_single_job_plan(_require_mapping(value, "single-job manifest"))


def validate_apply_guards(
    plan: Mapping[str, Any],
    *,
    enabled: bool,
    allow_write: bool,
    expected_count: int | None,
    expected_plan_sha256: str | None,
) -> None:
    validated = validate_single_job_plan(plan)
    if not enabled:
        raise SingleJobWriteDisabledError(
            "single-job execution is disabled; pass --enable-single-job explicitly"
        )
    if not allow_write:
        raise SingleJobWriteDisabledError("single-job execution requires --allow-write")
    if expected_count != 1 or isinstance(expected_count, bool):
        raise SingleJobContractError("single-job execution requires --expected-count 1")
    if expected_plan_sha256 != validated["plan_sha256"]:
        raise SingleJobContractError(
            "single-job execution requires the exact --expected-plan-sha256"
        )


def revalidate_single_job_plan(
    config: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> Path:
    validated = validate_single_job_plan(plan)
    current_worker = worker_identity(config)
    if current_worker != validated["worker"]:
        raise SingleJobConflictError("single-job worker config/profile changed after planning")

    paths = _require_mapping(config.get("paths"), "config.paths")
    watch_root = Path(_require_string(paths.get("watch_folder"), "config.paths.watch_folder"))
    expected_source = validated["source"]
    current_source = _source_evidence(watch_root, expected_source["relative_path"])
    _enforce_stability_window(config, current_source)
    if current_source != expected_source:
        raise SingleJobConflictError("single-job source changed after planning")
    return watch_root / expected_source["relative_path"]


def single_job_result(
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
        "skipped",
        "failed",
    }:
        raise ValueError(f"unsupported single-job result status: {status}")
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "expected_count": 1,
        "plan_sha256": plan_sha256,
        "status": status,
        "job_id": job_id,
    }
