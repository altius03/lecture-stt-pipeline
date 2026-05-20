from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import Candidate
from .validators import validate_correction_artifacts, validate_summary_artifact


class PromoteError(RuntimeError):
    def __init__(self, failure_class: str, message: str, details: dict[str, Any] | None = None):
        self.failure_class = failure_class
        self.details = details or {}
        super().__init__(message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_copy_no_overwrite(src: Path, dst: Path) -> None:
    if dst.exists():
        raise FileExistsError(str(dst))
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = src.stat().st_mode & 0o666
    except OSError:
        mode = 0o666
    fd: int | None = None
    try:
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with src.open("rb") as source, os.fdopen(fd, "wb") as target:
            fd = None
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            dst.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise


def _read_validation_report(path: Path, failure_class: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PromoteError("promote_validation_missing", f"missing validation report: {path}", {"path": str(path)}) from exc
    except json.JSONDecodeError as exc:
        raise PromoteError(failure_class, f"invalid validation report: {path}", {"path": str(path)}) from exc
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
    if raw_txt.name != f"{candidate.stem}.txt" or raw_txt.parent.name != "02_transcripts":
        raise PromoteError(
            "invalid_candidate_json",
            "candidate raw transcript path does not match lecture root layout",
            {"field": "raw_txt_path", "stem": candidate.stem},
        )
    lecture_root = raw_txt.parent.parent
    expected = {
        "raw_json_path": lecture_root / "02_transcripts" / f"{candidate.stem}.json",
        "correction_txt_path": lecture_root / "03_correction" / f"{candidate.stem}.txt",
        "correction_json_path": lecture_root / "03_correction" / f"{candidate.stem}.json",
        "summary_md_path": lecture_root / "04_summarize" / f"{candidate.stem}.md",
        "prompt_dir": lecture_root / "05_prompt",
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


def _rollback_copied_artifacts(copied: list[dict[str, str]]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for item in reversed(copied):
        path = Path(item["destination_path"])
        try:
            if path.exists() and _sha256(path) == item["sha256"]:
                path.unlink()
        except Exception as exc:  # pragma: no cover - defensive failure reporting
            errors.append({"path": str(path), "error": str(exc)})
    return errors


def write_staging_manifest(candidate: Candidate) -> Path:
    staging_dir = Path(candidate.staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
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
            "schema_version": 1,
            "passed": False,
            "failure_class": "promote_disabled",
            "message": "promote requires explicit allow_promote=True or CLI --allow-promote",
            "stem": candidate.stem,
        }

    if candidate.blocked_reasons:
        raise PromoteError(
            candidate.blocked_reasons[0],
            "candidate is blocked and cannot be promoted",
            {"blocked_reasons": list(candidate.blocked_reasons), "stem": candidate.stem},
        )

    _ensure_expected_candidate_paths(candidate)

    if candidate.actions.get("correction", {}).get("needed") is True:
        _read_validation_report(staging / "validation-correction.json", "correction_validation_failed")
    if candidate.actions.get("summary", {}).get("needed") is True:
        _read_validation_report(staging / "validation-summary.json", "summary_validation_failed")
    _rerun_required_validators(candidate)

    plan = _promote_plan(candidate)
    missing_sources = [str(src) for src, _, _ in plan if not src.exists()]
    if missing_sources:
        raise PromoteError("promote_failed", "missing staging artifact", {"paths": missing_sources})
    existing_outputs = [str(dst) for _, dst, _ in plan if dst.exists()]
    if existing_outputs:
        raise PromoteError("output_already_exists", "final output already exists", {"paths": existing_outputs})

    copied: list[dict[str, str]] = []
    try:
        for src, dst, kind in plan:
            _atomic_copy_no_overwrite(src, dst)
            copied.append(
                {
                    "kind": kind,
                    "source_path": str(src),
                    "destination_path": str(dst),
                    "sha256": _sha256(dst),
                }
            )
    except Exception as exc:
        rollback_errors = _rollback_copied_artifacts(copied)
        if isinstance(exc, PromoteError):
            raise
        details: dict[str, Any] = {"copied_before_failure": copied}
        if rollback_errors:
            details["rollback_errors"] = rollback_errors
        raise PromoteError("promote_failed", str(exc), details) from exc

    report = {
        "schema_version": 1,
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
