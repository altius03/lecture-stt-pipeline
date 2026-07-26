from __future__ import annotations

import importlib
import json
import os
import re
import sqlite3
import stat as stat_module
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from lecture_stt.downstream.lib import DownstreamConfig
from lecture_stt.storage_v2.importer import (
    _LegacyDatabaseSnapshot,
    _assert_legacy_database_stable,
    _is_symlink_component,
    _open_legacy_readonly,
    _resolve_legacy_file,
)
from lecture_stt.storage_v2.verifier import _compute_sha256_fd


MAX_REPORTED_ROWS = 1000
MAX_SCANNED_DELIVERY_ROWS = 5000
MAX_SCANNED_JOB_ROWS = 10000
MAX_SCANNED_CANDIDATE_ROWS = 2000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RESULT_STATUSES = (
    "verified_delivered",
    "verified_correction_only",
    "manual_review",
    "blocked",
)


@dataclass(frozen=True)
class ReconciliationIssue:
    severity: str
    code: str
    message: str
    field: str | None = None
    path: str | None = None
    expected: str | None = None
    actual: str | None = None

    @property
    def blocking(self) -> bool:
        return self.severity == "error"

    def as_dict(self) -> dict[str, str | None]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "path": self.path,
            "expected": self.expected,
            "actual": self.actual,
        }


def _load_worker_config_from_module(config_path: str) -> DownstreamConfig:
    worker = importlib.import_module("lecture_stt.downstream.worker")
    return worker.load_worker_config(config_path)


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


def _path_issue(
    severity: str,
    code: str,
    message: str,
    *,
    field: str,
    path: Path | str | None = None,
    expected: str | None = None,
    actual: str | None = None,
) -> ReconciliationIssue:
    return ReconciliationIssue(
        severity=severity,
        code=code,
        message=message,
        field=field,
        path=None if path is None else str(path),
        expected=expected,
        actual=actual,
    )


def _path_within_root(
    value: str | Path,
    *,
    root: Path,
    field: str,
    code_prefix: str,
) -> tuple[Path | None, ReconciliationIssue | None]:
    raw_path = Path(value).expanduser()
    if ".." in raw_path.parts:
        return None, _path_issue(
            "error",
            f"{code_prefix}_path_traversal",
            "Path contains parent traversal",
            field=field,
            path=raw_path,
        )
    candidate = raw_path if raw_path.is_absolute() else root / raw_path
    try:
        prospective = candidate.resolve(strict=False)
        prospective.relative_to(root)
    except ValueError:
        return None, _path_issue(
            "error",
            f"{code_prefix}_outside_root",
            "Path is outside the configured root",
            field=field,
            path=candidate,
            expected=str(root),
        )
    except OSError as exc:
        return None, _path_issue(
            "error",
            f"{code_prefix}_unreadable",
            f"Path cannot be resolved safely: {exc}",
            field=field,
            path=candidate,
        )
    if _is_symlink_component(candidate.absolute(), root):
        return None, _path_issue(
            "error",
            f"{code_prefix}_symlink",
            "Path or one of its components is a symlink",
            field=field,
            path=candidate,
        )
    return candidate, None


def _verify_absent_path(
    value: str | Path,
    *,
    root: Path,
    field: str,
    code_prefix: str,
) -> ReconciliationIssue | None:
    candidate, issue = _path_within_root(
        value,
        root=root,
        field=field,
        code_prefix=code_prefix,
    )
    if issue is not None:
        return issue
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return None
    kind = "regular file" if stat_module.S_ISREG(metadata.st_mode) else "filesystem entry"
    return _path_issue(
        "error",
        f"{code_prefix}_present",
        f"Expected absent path is still present as a {kind}",
        field=field,
        path=candidate,
    )


def _verify_hashed_file(
    value: str | Path,
    *,
    root: Path,
    field: str,
    code_prefix: str,
) -> tuple[Path | None, str | None, ReconciliationIssue | None]:
    candidate, issue = _path_within_root(
        value,
        root=root,
        field=field,
        code_prefix=code_prefix,
    )
    if issue is not None:
        return None, None, issue
    try:
        before_path = candidate.lstat()
    except FileNotFoundError:
        return None, None, _path_issue(
            "error",
            f"{code_prefix}_missing",
            "Expected delivered destination is missing",
            field=field,
            path=candidate,
        )
    if not stat_module.S_ISREG(before_path.st_mode):
        return None, None, _path_issue(
            "error",
            f"{code_prefix}_not_regular",
            "Destination must be a regular file",
            field=field,
            path=candidate,
        )
    if before_path.st_nlink != 1:
        return None, None, _path_issue(
            "error",
            f"{code_prefix}_hardlinked",
            "Destination must not be hard-linked",
            field=field,
            path=candidate,
            actual=str(before_path.st_nlink),
        )

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        return None, None, _path_issue(
            "error",
            f"{code_prefix}_unreadable",
            f"Destination could not be opened safely: {exc}",
            field=field,
            path=candidate,
        )
    try:
        before_open = os.fstat(descriptor)
        if _file_state(before_open) != _file_state(before_path):
            return None, None, _path_issue(
                "error",
                f"{code_prefix}_changed",
                "Destination changed while it was being opened",
                field=field,
                path=candidate,
            )
        if not stat_module.S_ISREG(before_open.st_mode):
            return None, None, _path_issue(
                "error",
                f"{code_prefix}_not_regular",
                "Destination must be a regular file",
                field=field,
                path=candidate,
            )
        if before_open.st_nlink != 1:
            return None, None, _path_issue(
                "error",
                f"{code_prefix}_hardlinked",
                "Destination must not be hard-linked",
                field=field,
                path=candidate,
                actual=str(before_open.st_nlink),
            )
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError:
            return None, None, _path_issue(
                "error",
                f"{code_prefix}_outside_root",
                "Resolved destination escaped the configured root",
                field=field,
                path=resolved,
                expected=str(root),
            )
        digest = _compute_sha256_fd(descriptor, before=before_open)
        try:
            after_path = candidate.lstat()
        except FileNotFoundError:
            return None, None, _path_issue(
                "error",
                f"{code_prefix}_changed",
                "Destination disappeared while it was being hashed",
                field=field,
                path=candidate,
            )
        if _file_state(after_path) != _file_state(before_open):
            return None, None, _path_issue(
                "error",
                f"{code_prefix}_changed",
                "Destination changed while it was being hashed",
                field=field,
                path=candidate,
            )
        return resolved, digest, None
    except OSError as exc:
        code = f"{code_prefix}_changed" if exc.errno == getattr(os, "ESTALE", 116) else f"{code_prefix}_unreadable"
        message = (
            "Destination changed while it was being hashed"
            if code.endswith("_changed")
            else f"Destination could not be read safely: {exc}"
        )
        return None, None, _path_issue(
            "error",
            code,
            message,
            field=field,
            path=candidate,
        )
    finally:
        os.close(descriptor)


def _normalize_digest(
    value: Any,
    *,
    field: str,
    code_prefix: str,
) -> tuple[str | None, ReconciliationIssue | None]:
    digest = str(value or "").strip().lower()
    if not digest:
        return None, _path_issue(
            "error",
            f"{code_prefix}_missing",
            "Required SHA-256 digest is missing",
            field=field,
        )
    if not _SHA256_RE.fullmatch(digest):
        return None, _path_issue(
            "error",
            f"{code_prefix}_invalid",
            "SHA-256 digest must be exactly 64 lowercase hexadecimal characters",
            field=field,
            actual=digest,
        )
    return digest, None


def _route_expected_paths(
    logical_stem: str,
    *,
    config: DownstreamConfig,
    subject_abbr: str,
) -> tuple[dict[str, str | None], list[ReconciliationIssue]]:
    route = config.subjects.get(subject_abbr)
    if route is None:
        return {
            "tuk_origin_txt": None,
            "tuk_origin_json": None,
            "tuk_summary": None,
            "obsidian_summary": None,
        }, [
            _path_issue(
                "error",
                "route_missing",
                "Subject route is not configured for archive reconciliation",
                field="subject_abbr",
                actual=subject_abbr,
            )
        ]
    return {
        "tuk_origin_txt": str(route.gh_origin_dir(config.gh_current_semester_root) / f"{logical_stem}.txt"),
        "tuk_origin_json": str(route.gh_origin_dir(config.gh_current_semester_root) / f"{logical_stem}.json"),
        "tuk_summary": str(route.gh_summary_dir(config.gh_current_semester_root) / f"{logical_stem}.md"),
        "obsidian_summary": str(route.obsidian_summary_dir(config.obsidian_semester_root) / f"{logical_stem}.md"),
    }, []


def _recorded_paths(delivery: Mapping[str, Any]) -> dict[str, str | None]:
    return {
        "correction_txt_source": str(delivery.get("correction_txt_path") or "").strip() or None,
        "correction_json_source": str(delivery.get("correction_json_path") or "").strip() or None,
        "summary_md_source": str(delivery.get("summary_md_path") or "").strip() or None,
        "tuk_origin_txt": str(delivery.get("tuk_origin_txt_path") or "").strip() or None,
        "tuk_origin_json": str(delivery.get("tuk_origin_json_path") or "").strip() or None,
        "tuk_summary": str(delivery.get("tuk_summary_path") or "").strip() or None,
        "obsidian_summary": str(delivery.get("obsidian_summary_path") or "").strip() or None,
    }


def _hashes(delivery: Mapping[str, Any]) -> dict[str, str | None]:
    def value(key: str) -> str | None:
        text = str(delivery.get(key) or "").strip().lower()
        return text or None

    return {
        "correction_txt_sha256": value("correction_txt_sha256"),
        "correction_json_sha256": value("correction_json_sha256"),
        "summary_md_sha256": value("summary_md_sha256"),
    }


def _flags(delivery: Mapping[str, Any]) -> dict[str, int]:
    def value(key: str) -> int:
        try:
            return int(delivery.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "tuk_origin_done": value("tuk_origin_done"),
        "tuk_summary_done": value("tuk_summary_done"),
        "obsidian_done": value("obsidian_done"),
    }


def _warning(
    code: str,
    message: str,
    *,
    field: str,
    path: Path | str | None = None,
    expected: str | None = None,
    actual: str | None = None,
) -> ReconciliationIssue:
    return _path_issue(
        "warning",
        code,
        message,
        field=field,
        path=path,
        expected=expected,
        actual=actual,
    )


def _info(
    code: str,
    message: str,
    *,
    field: str,
    path: Path | str | None = None,
    expected: str | None = None,
    actual: str | None = None,
) -> ReconciliationIssue:
    return _path_issue(
        "info",
        code,
        message,
        field=field,
        path=path,
        expected=expected,
        actual=actual,
    )


def _normalize_archive_root(path: Path, *, field: str) -> Path:
    root = path.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"{field} must be an existing directory: {root}")
    return root


def _verify_source_absence(
    issues: list[ReconciliationIssue],
    *,
    delivery: Mapping[str, Any],
    legacy_root: Path,
    logical_stem: str,
) -> None:
    probes = (
        (
            "correction_txt_source",
            delivery.get("correction_txt_path"),
            legacy_root / "03_correction" / f"{logical_stem}.txt",
            "legacy_correction_txt_source",
        ),
        (
            "correction_json_source",
            delivery.get("correction_json_path"),
            legacy_root / "03_correction" / f"{logical_stem}.json",
            "legacy_correction_json_source",
        ),
        (
            "summary_md_source",
            delivery.get("summary_md_path"),
            legacy_root / "04_summarize" / f"{logical_stem}.md",
            "legacy_summary_source",
        ),
    )
    for field, raw_value, fallback, code_prefix in probes:
        raw_text = str(raw_value or "").strip()
        resolved, issue = _resolve_legacy_file(
            raw_text or fallback,
            legacy_root=legacy_root,
            code_prefix=code_prefix,
            required=False,
        )
        if resolved is not None:
            issues.append(
                _path_issue(
                    "error",
                    f"{code_prefix}_present",
                    "Legacy source artifact is still present under the legacy root",
                    field=field,
                    path=resolved,
                )
            )
            continue
        if issue is not None and issue.blocking:
            issues.append(
                _path_issue(
                    "error",
                    issue.code,
                    issue.message,
                    field=field,
                    path=issue.path,
                )
            )


def _observed_artifacts_template() -> dict[str, dict[str, str] | None]:
    return {
        "correction_txt": None,
        "correction_json": None,
        "summary_tuk": None,
        "summary_obsidian": None,
    }


def _select_probe_path(
    *,
    recorded_path: str | None,
    expected_path: str | None,
    root: Path,
    field: str,
    code_prefix: str,
    issues: list[ReconciliationIssue],
) -> Path | None:
    if expected_path is None:
        return None
    deterministic = Path(expected_path)
    if not recorded_path:
        issues.append(
            _info(
                "destination_relocated",
                "Recorded destination path is absent; verified the deterministic current route instead",
                field=field,
                expected=str(deterministic),
            )
        )
        return deterministic

    candidate, issue = _path_within_root(
        recorded_path,
        root=root,
        field=field,
        code_prefix=f"{code_prefix}_recorded",
    )
    if issue is not None:
        if issue.code.endswith("_outside_root"):
            issues.append(
                _info(
                    "destination_relocated",
                    "Recorded destination is outside the current configured root; verified the deterministic current route instead",
                    field=field,
                    path=recorded_path,
                    expected=str(deterministic),
                    actual=recorded_path,
                )
            )
            return deterministic
        issues.append(issue)
        return deterministic

    try:
        candidate.lstat()
    except FileNotFoundError:
        issues.append(
            _info(
                "destination_relocated",
                "Recorded destination is not present under the current configured root; verified the deterministic current route instead",
                field=field,
                path=recorded_path,
                expected=str(deterministic),
                actual=str(candidate),
            )
        )
        return deterministic

    if candidate.resolve(strict=False) != deterministic.resolve(strict=False):
        issues.append(
            _info(
                "destination_relocated",
                "Recorded destination differs from the deterministic current route",
                field=field,
                path=candidate,
                expected=str(deterministic),
                actual=str(candidate),
            )
        )
    return candidate


def _observe_verified_destination(
    *,
    recorded_path: str | None,
    expected_path: str | None,
    root: Path,
    field: str,
    code_prefix: str,
    expected_sha256: str,
    observed_key: str,
    observed_artifacts: dict[str, dict[str, str] | None],
    issues: list[ReconciliationIssue],
    cache: dict[str, tuple[Path, str]] | None = None,
) -> None:
    probe_path = _select_probe_path(
        recorded_path=recorded_path,
        expected_path=expected_path,
        root=root,
        field=field,
        code_prefix=code_prefix,
        issues=issues,
    )
    if probe_path is None:
        issues.append(
            _path_issue(
                "error",
                f"{code_prefix}_missing_expected_path",
                "Deterministic expected destination path could not be computed",
                field=field,
            )
        )
        return

    cache_key = json.dumps(
        {"root": str(root), "probe_path": str(probe_path)},
        sort_keys=True,
    )
    if cache is not None and cache_key in cache:
        resolved, actual_sha256 = cache[cache_key]
    else:
        resolved, actual_sha256, path_issue = _verify_hashed_file(
            probe_path,
            root=root,
            field=field,
            code_prefix=code_prefix,
        )
        if path_issue is not None:
            issues.append(path_issue)
            return
        if cache is not None:
            cache[cache_key] = (resolved, actual_sha256)

    observed_artifacts[observed_key] = {
        "path": str(resolved),
        "sha256": actual_sha256,
    }
    if actual_sha256 != expected_sha256:
        issues.append(
            _path_issue(
                "error",
                f"{code_prefix}_hash_mismatch",
                "Delivered destination hash does not match the stored SHA-256",
                field=f"{observed_key}_sha256",
                path=resolved,
                expected=expected_sha256,
                actual=actual_sha256,
            )
        )


def _reconcile_row(
    delivery: Mapping[str, Any],
    *,
    legacy_root: Path,
    config: DownstreamConfig,
) -> dict[str, Any]:
    logical_stem = str(delivery.get("logical_stem") or "").strip()
    subject_abbr = str(delivery.get("subject_abbr") or "UNKNOWN").strip() or "UNKNOWN"
    correction_status = str(delivery.get("correction_status") or "MISSING").strip().upper()
    summary_status = str(delivery.get("summary_status") or "MISSING").strip().upper()
    recorded_paths = _recorded_paths(delivery)
    expected_paths, issues = _route_expected_paths(
        logical_stem,
        config=config,
        subject_abbr=subject_abbr,
    )
    hashes = _hashes(delivery)
    flags = _flags(delivery)
    observed_artifacts = _observed_artifacts_template()

    if not logical_stem:
        issues.append(
            _path_issue(
                "error",
                "logical_stem_missing",
                "Delivery logical_stem is missing",
                field="logical_stem",
            )
        )

    _verify_source_absence(
        issues,
        delivery=delivery,
        legacy_root=legacy_root,
        logical_stem=logical_stem,
    )

    if correction_status != "DELIVERED":
        issues.append(
            _warning(
                "correction_status_unexpected",
                "Archive reconciliation only verifies unmatched deliveries with delivered corrections",
                field="correction_status",
                actual=correction_status,
                expected="DELIVERED",
            )
        )
    else:
        if flags["tuk_origin_done"] != 1:
            issues.append(
                _warning(
                    "correction_tuk_origin_done_mismatch",
                    "Correction delivery flag does not confirm the GH origin copy",
                    field="tuk_origin_done",
                    actual=str(flags["tuk_origin_done"]),
                    expected="1",
                )
            )
        for key, field_name, root, code_prefix, expected_key in (
            (
                "correction_txt_sha256",
                "tuk_origin_txt",
                config.gh_current_semester_root,
                "correction_txt_destination",
                "tuk_origin_txt",
            ),
            (
                "correction_json_sha256",
                "tuk_origin_json",
                config.gh_current_semester_root,
                "correction_json_destination",
                "tuk_origin_json",
            ),
        ):
            digest, digest_issue = _normalize_digest(
                hashes[key],
                field=key,
                code_prefix=key.removesuffix("_sha256"),
            )
            if digest_issue is not None:
                issues.append(digest_issue)
                continue
            _observe_verified_destination(
                recorded_path=recorded_paths[field_name],
                expected_path=expected_paths[expected_key],
                root=root,
                field=field_name,
                code_prefix=code_prefix,
                expected_sha256=digest,
                observed_key="correction_txt" if field_name == "tuk_origin_txt" else "correction_json",
                observed_artifacts=observed_artifacts,
                issues=issues,
            )

    shared_summary_destination = False
    if summary_status == "DELIVERED":
        summary_digest, digest_issue = _normalize_digest(
            hashes["summary_md_sha256"],
            field="summary_md_sha256",
            code_prefix="summary",
        )
        if digest_issue is not None:
            issues.append(digest_issue)
        if flags["tuk_summary_done"] != 1:
            issues.append(
                _warning(
                    "summary_tuk_done_mismatch",
                    "Summary delivery flag does not confirm the GH summary copy",
                    field="tuk_summary_done",
                    actual=str(flags["tuk_summary_done"]),
                    expected="1",
                )
            )
        if flags["obsidian_done"] != 1:
            issues.append(
                _warning(
                    "summary_obsidian_done_mismatch",
                    "Summary delivery flag does not confirm the Obsidian copy",
                    field="obsidian_done",
                    actual=str(flags["obsidian_done"]),
                    expected="1",
                )
            )
        seen_paths: dict[str, tuple[Path, str]] = {}
        for field_name, root, code_prefix, expected_key in (
            ("tuk_summary", config.gh_current_semester_root, "summary_tuk_destination", "tuk_summary"),
            ("obsidian_summary", config.obsidian_semester_root, "summary_obsidian_destination", "obsidian_summary"),
        ):
            if summary_digest is None:
                continue
            _observe_verified_destination(
                recorded_path=recorded_paths[field_name],
                expected_path=expected_paths[expected_key],
                root=root,
                field=field_name,
                code_prefix=code_prefix,
                expected_sha256=summary_digest,
                observed_key="summary_tuk" if field_name == "tuk_summary" else "summary_obsidian",
                observed_artifacts=observed_artifacts,
                issues=issues,
                cache=seen_paths,
            )
        observed_tuk = observed_artifacts["summary_tuk"]
        observed_obsidian = observed_artifacts["summary_obsidian"]
        shared_summary_destination = (
            observed_tuk is not None
            and observed_obsidian is not None
            and observed_tuk["path"] == observed_obsidian["path"]
        )
    elif summary_status == "MISSING":
        if flags["tuk_summary_done"] != 0:
            issues.append(
                _warning(
                    "summary_tuk_done_mismatch",
                    "Summary status is MISSING but GH summary flag is not zero",
                    field="tuk_summary_done",
                    actual=str(flags["tuk_summary_done"]),
                    expected="0",
                )
            )
        if flags["obsidian_done"] != 0:
            issues.append(
                _warning(
                    "summary_obsidian_done_mismatch",
                    "Summary status is MISSING but Obsidian flag is not zero",
                    field="obsidian_done",
                    actual=str(flags["obsidian_done"]),
                    expected="0",
                )
            )
        for field_name, root, code_prefix, expected_key in (
            ("tuk_summary", config.gh_current_semester_root, "summary_expected_tuk_destination", "tuk_summary"),
            ("obsidian_summary", config.obsidian_semester_root, "summary_expected_obsidian_destination", "obsidian_summary"),
        ):
            expected_path = expected_paths[expected_key]
            if expected_path is None:
                continue
            issue = _verify_absent_path(
                expected_path,
                root=root,
                field=field_name,
                code_prefix=code_prefix,
            )
            if issue is not None:
                issues.append(issue)
            recorded_path = recorded_paths[field_name]
            if recorded_path:
                candidate, recorded_issue = _path_within_root(
                    recorded_path,
                    root=root,
                    field=field_name,
                    code_prefix=f"{code_prefix}_recorded",
                )
                if recorded_issue is not None:
                    if recorded_issue.code.endswith("_outside_root"):
                        issues.append(
                            _info(
                                "destination_relocated",
                                "Recorded destination is outside the current configured root",
                                field=field_name,
                                path=recorded_path,
                                expected=expected_path,
                                actual=recorded_path,
                            )
                        )
                    else:
                        issues.append(recorded_issue)
                elif candidate.resolve(strict=False) != Path(expected_path).resolve(strict=False):
                    issues.append(
                        _info(
                            "destination_relocated",
                            "Recorded destination differs from the deterministic current route",
                            field=field_name,
                            path=candidate,
                            expected=expected_path,
                            actual=str(candidate),
                        )
                    )
        if hashes["summary_md_sha256"] is not None:
            issues.append(
                _warning(
                    "summary_missing_hash_present",
                    "Summary status is MISSING but a stored summary SHA-256 is still present",
                    field="summary_md_sha256",
                    actual=hashes["summary_md_sha256"],
                )
            )
        if recorded_paths["tuk_summary"] or recorded_paths["obsidian_summary"]:
            issues.append(
                _info(
                    "summary_missing_destination_record_present",
                    "Summary status is MISSING but delivered destination paths are still recorded",
                    field="summary_status",
                )
            )
    else:
        issues.append(
            _warning(
                "summary_status_unexpected",
                "Archive reconciliation only auto-classifies DELIVERED or MISSING summaries",
                field="summary_status",
                actual=summary_status,
                expected="DELIVERED or MISSING",
            )
        )

    if any(issue.blocking for issue in issues):
        classification = "blocked"
    elif any(issue.severity == "warning" for issue in issues):
        classification = "manual_review"
    elif summary_status == "DELIVERED":
        classification = "verified_delivered"
    else:
        classification = "verified_correction_only"

    return {
        "logical_stem": logical_stem,
        "subject_abbr": subject_abbr,
        "correction_status": correction_status,
        "summary_status": summary_status,
        "classification": classification,
        "recorded_paths": recorded_paths,
        "expected_paths": expected_paths,
        "hashes": hashes,
        "flags": flags,
        "observed_artifacts": observed_artifacts,
        "shared_summary_destination": shared_summary_destination,
        "issues": [issue.as_dict() for issue in issues],
    }


def reconcile_archive(
    legacy_db_path: Path | str,
    legacy_root: Path | str,
    *,
    downstream_config: DownstreamConfig,
    limit: int = MAX_REPORTED_ROWS,
) -> dict[str, Any]:
    if limit < 1 or limit > MAX_REPORTED_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_REPORTED_ROWS}")

    root_input = Path(legacy_root).expanduser()
    if root_input.is_symlink():
        raise ValueError(f"Legacy root must not be a symlink: {root_input}")
    resolved_legacy_root = root_input.resolve(strict=True)
    normalized_config = replace(
        downstream_config,
        gh_current_semester_root=_normalize_archive_root(
            downstream_config.gh_current_semester_root,
            field="gh_current_semester_root",
        ),
        obsidian_semester_root=_normalize_archive_root(
            downstream_config.obsidian_semester_root,
            field="obsidian_semester_root",
        ),
    )

    conn, resolved_legacy_db, legacy_snapshot = _open_legacy_readonly(legacy_db_path)
    try:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM deliveries ORDER BY logical_stem ASC LIMIT ?",
                (MAX_SCANNED_DELIVERY_ROWS + 1,),
            ).fetchall()
        ]
        if len(rows) > MAX_SCANNED_DELIVERY_ROWS:
            raise RuntimeError(
                "Legacy deliveries row count exceeds the archive reconciliation safety cap "
                f"({len(rows)} > {MAX_SCANNED_DELIVERY_ROWS})"
            )
        job_rows = conn.execute(
            "SELECT canonical_base FROM jobs LIMIT ?",
            (MAX_SCANNED_JOB_ROWS + 1,),
        ).fetchall()
        if len(job_rows) > MAX_SCANNED_JOB_ROWS:
            raise RuntimeError(
                "Legacy jobs row count exceeds the archive reconciliation safety cap "
                f"({len(job_rows)} > {MAX_SCANNED_JOB_ROWS})"
            )
        job_bases = {
            str(row["canonical_base"]).strip()
            for row in job_rows
            if str(row["canonical_base"] or "").strip()
        }
        _assert_legacy_database_stable(resolved_legacy_db, legacy_snapshot)
    finally:
        conn.close()
    _assert_legacy_database_stable(resolved_legacy_db, legacy_snapshot)

    owned_rows = [row for row in rows if row.get("source_job_id") is not None]
    ownerless_rows = [row for row in rows if row.get("source_job_id") is None]
    matched_ownerless_rows = [
        row for row in ownerless_rows if str(row.get("logical_stem") or "").strip() in job_bases
    ]
    unmatched_ownerless_rows = [
        row for row in ownerless_rows if str(row.get("logical_stem") or "").strip() not in job_bases
    ]
    if len(unmatched_ownerless_rows) > MAX_SCANNED_CANDIDATE_ROWS:
        raise RuntimeError(
            "Archive reconciliation candidate row count exceeds the safety cap "
            f"({len(unmatched_ownerless_rows)} > {MAX_SCANNED_CANDIDATE_ROWS})"
        )

    reconciled_rows = [
        _reconcile_row(
            row,
            legacy_root=resolved_legacy_root,
            config=normalized_config,
        )
        for row in unmatched_ownerless_rows
    ]
    _assert_legacy_database_stable(resolved_legacy_db, legacy_snapshot)
    result_counts = {status: 0 for status in _RESULT_STATUSES}
    for row in reconciled_rows:
        result_counts[row["classification"]] += 1

    return {
        "schema_version": "storage-v2/archive-reconciliation@1",
        "mode": "read_only",
        "legacy_db": str(resolved_legacy_db),
        "legacy_root": str(resolved_legacy_root),
        "legacy_database_snapshot": legacy_snapshot.as_dict(),
        "summary": {
            "candidate_rows": len(unmatched_ownerless_rows),
            "reported_rows": min(len(reconciled_rows), limit),
            "truncated": len(reconciled_rows) > limit,
            "result_counts": result_counts,
        },
        "excluded_counts": {
            "delivery_rows_total": len(rows),
            "owned_delivery_rows": len(owned_rows),
            "ownerless_matched_rows": len(matched_ownerless_rows),
        },
        "rows": reconciled_rows[:limit],
        "issues": [],
    }


def reconcile_archive_from_config(
    legacy_db_path: Path | str,
    legacy_root: Path | str,
    *,
    config_path: str = "config/config.yaml",
    limit: int = MAX_REPORTED_ROWS,
) -> dict[str, Any]:
    return reconcile_archive(
        legacy_db_path,
        legacy_root,
        downstream_config=_load_worker_config_from_module(config_path),
        limit=limit,
    )
