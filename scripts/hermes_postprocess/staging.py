from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contract import CORRECTION_DIR, PROMPT_DIR, SCHEMA_VERSION, SUMMARY_DIR, TRANSCRIPT_DIR
from .finals import final_entry_lexists, final_parent_is_real_dir, final_type_name
from .schemas import Candidate
from .validators import validate_correction_artifacts, validate_summary_artifact


class PromoteError(RuntimeError):
    def __init__(self, failure_class: str, message: str, details: dict[str, Any] | None = None):
        self.failure_class = failure_class
        self.details = details or {}
        super().__init__(message)


@dataclass(frozen=True)
class StagingSourceSnapshot:
    path: Path
    kind: str
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "path": str(self.path),
            "device": str(self.device),
            "inode": str(self.inode),
            "mode": str(self.mode),
            "size": str(self.size),
            "mtime_ns": str(self.mtime_ns),
            "sha256": self.sha256,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_open_file(handle: Any) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _open_real_output_parent(dst: Path) -> int:
    parent = dst.parent
    if not os.path.lexists(parent):
        parent.mkdir(parents=True, exist_ok=True)
    if not final_parent_is_real_dir(dst):
        try:
            mode = parent.lstat().st_mode
            file_type = final_type_name(mode)
        except OSError as exc:
            file_type = f"unknown:{type(exc).__name__}"
        raise OSError(f"unsafe final parent directory: {parent} ({file_type})")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(parent, flags)
    except OSError as exc:
        raise OSError(f"unsafe final parent directory: {parent}: {exc.strerror or type(exc).__name__}") from exc


def _unsafe_staging_artifact(src: Path, kind: str) -> dict[str, Any] | None:
    try:
        source_stat = src.lstat()
    except FileNotFoundError:
        return {"kind": kind, "path": str(src), "reason": "missing"}
    except OSError as exc:
        return {"kind": kind, "path": str(src), "reason": type(exc).__name__}
    file_type = final_type_name(source_stat.st_mode)
    if not stat.S_ISREG(source_stat.st_mode):
        return {"kind": kind, "path": str(src), "reason": "non_regular", "file_type": file_type}
    if source_stat.st_nlink != 1:
        return {
            "kind": kind,
            "path": str(src),
            "reason": "unexpected_link_count",
            "file_type": file_type,
            "link_count": str(source_stat.st_nlink),
        }
    return None


def _ensure_safe_staging_sources(plan: list[tuple[Path, Path, str]]) -> None:
    unsafe = [issue for src, _, kind in plan if (issue := _unsafe_staging_artifact(src, kind)) is not None]
    if unsafe:
        raise PromoteError(
            "unsafe_staging_artifact",
            "staging artifacts must be single-link regular files",
            {"artifacts": unsafe},
        )


def _open_safe_staging_source(src: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(src, flags)
    try:
        source_stat = os.fstat(fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise OSError(f"unsafe staging artifact is not a regular file: {src}")
        if source_stat.st_nlink != 1:
            raise OSError(f"unsafe staging artifact link count {source_stat.st_nlink}: {src}")
        return fd, source_stat
    except Exception:
        os.close(fd)
        raise


def _snapshot_safe_staging_source(src: Path, kind: str) -> StagingSourceSnapshot:
    try:
        fd, source_stat = _open_safe_staging_source(src)
    except OSError as exc:
        raise PromoteError(
            "unsafe_staging_artifact",
            "staging artifacts must be single-link regular files",
            {"artifacts": [{"kind": kind, "path": str(src), "reason": type(exc).__name__}]},
        ) from exc
    try:
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            source_hash = _sha256_open_file(handle)
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
    return StagingSourceSnapshot(
        path=src,
        kind=kind,
        device=source_stat.st_dev,
        inode=source_stat.st_ino,
        mode=source_stat.st_mode,
        size=source_stat.st_size,
        mtime_ns=source_stat.st_mtime_ns,
        sha256=source_hash,
    )


def _snapshot_staging_sources(plan: list[tuple[Path, Path, str]]) -> dict[tuple[str, str], StagingSourceSnapshot]:
    _ensure_safe_staging_sources(plan)
    return {(str(src), kind): _snapshot_safe_staging_source(src, kind) for src, _, kind in plan}


def preflight_safe_staging_artifacts(candidate: Candidate) -> None:
    _snapshot_staging_sources(_promote_plan(candidate))


def _staging_snapshot_mismatch(expected: StagingSourceSnapshot, current: StagingSourceSnapshot) -> dict[str, Any] | None:
    fields = ["device", "inode", "mode", "size", "mtime_ns", "sha256"]
    changed_fields = [field for field in fields if getattr(expected, field) != getattr(current, field)]
    if not changed_fields:
        return None
    return {
        "kind": expected.kind,
        "path": str(expected.path),
        "changed_fields": changed_fields,
        "expected": expected.to_dict(),
        "current": current.to_dict(),
    }


def _snapshot_from_stat(path: Path, kind: str, source_stat: os.stat_result, source_hash: str) -> StagingSourceSnapshot:
    return StagingSourceSnapshot(
        path=path,
        kind=kind,
        device=source_stat.st_dev,
        inode=source_stat.st_ino,
        mode=source_stat.st_mode,
        size=source_stat.st_size,
        mtime_ns=source_stat.st_mtime_ns,
        sha256=source_hash,
    )


def _raise_if_staging_snapshot_changed(expected: StagingSourceSnapshot, current: StagingSourceSnapshot) -> None:
    mismatch = _staging_snapshot_mismatch(expected, current)
    if mismatch is not None:
        raise PromoteError(
            "staging_artifact_changed_after_validation",
            "staging artifact changed after parent validation and before promote copy",
            {"artifacts": [mismatch]},
        )


def _verify_staging_sources_unchanged(snapshots: dict[tuple[str, str], StagingSourceSnapshot]) -> None:
    mismatches: list[dict[str, Any]] = []
    for expected in snapshots.values():
        current = _snapshot_safe_staging_source(expected.path, expected.kind)
        mismatch = _staging_snapshot_mismatch(expected, current)
        if mismatch is not None:
            mismatches.append(mismatch)
    if mismatches:
        raise PromoteError(
            "staging_artifact_changed_after_validation",
            "staging artifacts changed after parent validation and before promote copy",
            {"artifacts": mismatches},
        )


def _atomic_copy_no_overwrite(src: Path, dst: Path, expected_source: StagingSourceSnapshot | None = None) -> dict[str, str]:
    if final_entry_lexists(dst):
        raise FileExistsError(str(dst))
    parent_fd: int | None = None
    fd: int | None = None
    source_fd: int | None = None
    created_identity: tuple[int, int] | None = None
    try:
        source_fd, source_stat = _open_safe_staging_source(src)
        if expected_source is not None:
            pre_copy_snapshot = _snapshot_from_stat(src, expected_source.kind, source_stat, expected_source.sha256)
            _raise_if_staging_snapshot_changed(expected_source, pre_copy_snapshot)
        mode = source_stat.st_mode & 0o666
        parent_fd = _open_real_output_parent(dst)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(dst.name, flags, mode, dir_fd=parent_fd)
        created_stat = os.fstat(fd)
        created_identity = (created_stat.st_dev, created_stat.st_ino)
        digest = hashlib.sha256()
        with os.fdopen(source_fd, "rb") as source, os.fdopen(fd, "wb") as target:
            source_fd = None
            fd = None
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
            final_stat = os.fstat(target.fileno())
            source_after_stat = os.fstat(source.fileno())
        source_hash = digest.hexdigest()
        if expected_source is not None:
            post_copy_snapshot = _snapshot_from_stat(src, expected_source.kind, source_after_stat, source_hash)
            _raise_if_staging_snapshot_changed(expected_source, post_copy_snapshot)
        return {
            "sha256": source_hash,
            "device": str(final_stat.st_dev),
            "inode": str(final_stat.st_ino),
            "mode": str(final_stat.st_mode),
            "size": str(final_stat.st_size),
        }
    except Exception:
        if source_fd is not None:
            try:
                os.close(source_fd)
            except OSError:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if created_identity is not None and parent_fd is not None:
            try:
                current = os.stat(dst.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            except OSError:
                current = None
            if current is not None and stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == created_identity:
                try:
                    os.unlink(dst.name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
        raise
    finally:
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _read_validation_report(path: Path, failure_class: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PromoteError("promote_validation_missing", f"missing validation report: {path}", {"path": str(path)}) from exc
    except json.JSONDecodeError as exc:
        raise PromoteError("invalid_validation_report", f"invalid validation report: {path}", {"path": str(path)}) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise PromoteError(
            "invalid_validation_report",
            "validation report schema_version is missing or unsupported",
            {"path": str(path), "expected_schema_version": SCHEMA_VERSION},
        )
    if payload.get("raw_transcript_body_included") is not False:
        raise PromoteError(
            "invalid_validation_report",
            "validation report must explicitly declare raw_transcript_body_included=false",
            {"path": str(path)},
        )
    if payload.get("passed") is not True:
        raise PromoteError(
            failure_class,
            f"validation report is not passing: {path}",
            {"path": str(path), "report_failure_class": payload.get("failure_class")},
        )


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _ensure_expected_candidate_paths(candidate: Candidate) -> None:
    raw_txt = Path(candidate.raw_txt_path)
    if raw_txt.name != f"{candidate.stem}.txt" or raw_txt.parent.name != TRANSCRIPT_DIR:
        raise PromoteError(
            "invalid_candidate_json",
            "candidate raw transcript path does not match lecture root layout",
            {"field": "raw_txt_path", "stem": candidate.stem},
        )
    lecture_root = raw_txt.parent.parent
    expected = {
        "raw_json_path": lecture_root / TRANSCRIPT_DIR / f"{candidate.stem}.json",
        "correction_txt_path": lecture_root / CORRECTION_DIR / f"{candidate.stem}.txt",
        "correction_json_path": lecture_root / CORRECTION_DIR / f"{candidate.stem}.json",
        "summary_md_path": lecture_root / SUMMARY_DIR / f"{candidate.stem}.md",
        "prompt_dir": lecture_root / PROMPT_DIR,
    }
    actual = {
        "raw_json_path": Path(candidate.raw_json_path),
        "correction_txt_path": Path(candidate.correction_txt_path),
        "correction_json_path": Path(candidate.correction_json_path),
        "summary_md_path": Path(candidate.summary_md_path),
        "prompt_dir": Path(candidate.prompt_dir),
    }
    mismatches = [field for field, expected_path in expected.items() if _resolve(actual[field]) != _resolve(expected_path)]
    if mismatches:
        raise PromoteError(
            "invalid_candidate_json",
            "candidate paths do not match the raw transcript lecture root",
            {"fields": mismatches, "stem": candidate.stem},
        )


def _raise_if_validation_failed(result: Any) -> None:
    if result.passed:
        return
    raise PromoteError(
        result.failure_class or "promote_validation_failed",
        result.message,
        result.details or {},
    )


def _rerun_required_validators(candidate: Candidate) -> None:
    staging = Path(candidate.staging_dir)
    if candidate.actions.get("correction", {}).get("needed") is True:
        _raise_if_validation_failed(
            validate_correction_artifacts(
                raw_json_path=candidate.raw_json_path,
                correction_txt_path=staging / "correction.txt",
                correction_json_path=staging / "correction.json",
                final_txt_path=candidate.correction_txt_path,
                final_json_path=candidate.correction_json_path,
            )
        )
    if candidate.actions.get("summary", {}).get("needed") is True:
        input_source = candidate.actions.get("summary", {}).get("input_source")
        if input_source == "staging_correction":
            corrected_txt_path = staging / "correction.txt"
        elif input_source == "final_correction":
            corrected_txt_path = Path(candidate.correction_txt_path)
        else:
            raise PromoteError(
                "summary_validation_failed",
                "summary input source is not available",
                {"input_source": input_source, "stem": candidate.stem},
            )
        _raise_if_validation_failed(
            validate_summary_artifact(
                summary_md_path=staging / "summary.md",
                corrected_txt_path=corrected_txt_path,
                final_md_path=candidate.summary_md_path,
            )
        )


def _copied_artifact_record(src: Path, dst: Path, kind: str, metadata: dict[str, str] | None) -> dict[str, str]:
    record: dict[str, str] = {
        "kind": kind,
        "source_path": str(src),
        "destination_path": str(dst),
    }
    if metadata is not None:
        record.update(metadata)
        return record
    try:
        current = dst.lstat()
    except OSError as exc:
        record.update({"sha256": _sha256(src), "destination_file_type": "missing", "destination_state_error": type(exc).__name__})
        return record
    record["destination_file_type"] = final_type_name(current.st_mode)
    if stat.S_ISREG(current.st_mode):
        record.update(
            {
                "sha256": _sha256(dst),
                "device": str(current.st_dev),
                "inode": str(current.st_ino),
                "mode": str(current.st_mode),
                "size": str(current.st_size),
            }
        )
    else:
        record["sha256"] = _sha256(src)
    return record


def _verify_copied_destination_intact(record: dict[str, str]) -> None:
    path = Path(record["destination_path"])
    if not final_entry_lexists(path):
        raise OSError(f"promoted destination disappeared before verification: {path}")
    current = path.lstat()
    if not stat.S_ISREG(current.st_mode):
        raise OSError(f"promoted destination is not a regular file: {path}")
    expected_device = record.get("device")
    expected_inode = record.get("inode")
    if expected_device is not None and expected_inode is not None:
        if str(current.st_dev) != expected_device or str(current.st_ino) != expected_inode:
            raise OSError(f"promoted destination was replaced before verification: {path}")
    if _sha256(path) != record.get("sha256"):
        raise OSError(f"promoted destination hash changed before verification: {path}")


def _rollback_copied_artifacts(copied: list[dict[str, str]]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for item in reversed(copied):
        path = Path(item["destination_path"])
        try:
            if not final_entry_lexists(path):
                continue
            current = path.lstat()
            if not stat.S_ISREG(current.st_mode):
                errors.append({"path": str(path), "error": "rollback_skipped_non_regular_destination", "file_type": final_type_name(current.st_mode)})
                continue
            expected_device = item.get("device")
            expected_inode = item.get("inode")
            if expected_device is not None and expected_inode is not None:
                if str(current.st_dev) != expected_device or str(current.st_ino) != expected_inode:
                    errors.append({"path": str(path), "error": "rollback_skipped_replaced_destination"})
                    continue
            if _sha256(path) == item["sha256"]:
                path.unlink()
            else:
                errors.append({"path": str(path), "error": "rollback_skipped_hash_mismatch"})
        except Exception as exc:  # pragma: no cover - defensive failure reporting
            errors.append({"path": str(path), "error": str(exc)})
    return errors


def write_staging_manifest(candidate: Candidate) -> Path:
    staging_dir = Path(candidate.staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": "hermes_postprocess_staging_manifest",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "stem": candidate.stem,
        "subject": candidate.subject,
        "paths": candidate.paths.to_dict(),
        "actions": candidate.actions,
        "blocked_reasons": list(candidate.blocked_reasons),
        "privacy": {
            "metadata_only": True,
            "raw_transcript_body_included": False,
            "timed_chunk_array_included": False,
        },
    }
    path = staging_dir / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _promote_plan(candidate: Candidate) -> list[tuple[Path, Path, str]]:
    plan: list[tuple[Path, Path, str]] = []
    staging = Path(candidate.staging_dir)
    if candidate.actions.get("correction", {}).get("needed") is True:
        plan.append((staging / "correction.txt", Path(candidate.correction_txt_path), "correction_txt"))
        plan.append((staging / "correction.json", Path(candidate.correction_json_path), "correction_json"))
    if candidate.actions.get("summary", {}).get("needed") is True:
        plan.append((staging / "summary.md", Path(candidate.summary_md_path), "summary_md"))
    return plan


def promote_candidate(candidate: Candidate, *, allow_promote: bool = False) -> dict[str, Any]:
    staging = Path(candidate.staging_dir)
    if not allow_promote:
        return {
            "schema_version": SCHEMA_VERSION,
            "passed": False,
            "failure_class": "promote_disabled",
            "message": "promote requires explicit allow_promote=True or CLI --allow-promote",
            "stem": candidate.stem,
            "raw_transcript_body_included": False,
        }

    if candidate.blocked_reasons:
        raise PromoteError(
            candidate.blocked_reasons[0],
            "candidate is blocked and cannot be promoted",
            {"blocked_reasons": list(candidate.blocked_reasons), "stem": candidate.stem},
        )

    _ensure_expected_candidate_paths(candidate)
    plan = _promote_plan(candidate)
    staging_snapshots = _snapshot_staging_sources(plan)

    if candidate.actions.get("correction", {}).get("needed") is True:
        _read_validation_report(staging / "validation-correction.json", "correction_validation_failed")
    if candidate.actions.get("summary", {}).get("needed") is True:
        _read_validation_report(staging / "validation-summary.json", "summary_validation_failed")
    _rerun_required_validators(candidate)
    _verify_staging_sources_unchanged(staging_snapshots)
    existing_outputs = [str(dst) for _, dst, _ in plan if final_entry_lexists(dst)]
    if existing_outputs:
        raise PromoteError("output_already_exists", "final output already exists", {"paths": existing_outputs})

    copied: list[dict[str, str]] = []
    try:
        for src, dst, kind in plan:
            metadata = _atomic_copy_no_overwrite(src, dst, staging_snapshots[(str(src), kind)])
            record = _copied_artifact_record(src, dst, kind, metadata)
            copied.append(record)
            _verify_copied_destination_intact(record)
    except Exception as exc:
        rollback_errors = _rollback_copied_artifacts(copied)
        if isinstance(exc, PromoteError):
            raise
        details: dict[str, Any] = {"copied_before_failure": copied}
        if rollback_errors:
            details["rollback_errors"] = rollback_errors
        raise PromoteError("promote_failed", str(exc), details) from exc

    report = {
        "schema_version": SCHEMA_VERSION,
        "passed": True,
        "failure_class": None,
        "message": "promote passed",
        "stem": candidate.stem,
        "artifacts": copied,
        "raw_transcript_body_included": False,
    }
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "promote.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report
