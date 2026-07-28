from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Mapping


REPORT_SCHEMA_VERSION = "lecture-stt/controller-shadow-report@4"
SUMMARY_SCHEMA_VERSION = "lecture-stt/controller-readiness-summary@1"

DEFAULT_MIN_RUNS = 20
DEFAULT_MIN_VERIFIED = 100
MAX_LEDGER_BYTES = 8 * 1024 * 1024
MAX_LEDGER_RUNS = 10_000
MAX_SCANS_PER_RUN = 1_000
MAX_PLAN_CHECKS_PER_RUN = 4_096
MAX_CRITERION_VERIFIED = 1_000_000

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_UTC_RFC3339_NANO_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T"
    r"(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?Z$"
)

_REPORT_KEYS = {
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
_PLAN_CHECK_KEYS = {
    "plan_sha256",
    "relative_path",
    "scan_index",
    "status",
}


class ControllerReadinessError(RuntimeError):
    pass


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise ControllerReadinessError(
            f"{label} keys mismatch: missing={missing} unexpected={unexpected}"
        )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ControllerReadinessError(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ControllerReadinessError(f"{label} must be an array")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ControllerReadinessError(f"{label} must be a non-empty string")
    return value


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ControllerReadinessError(f"{label} must be a boolean")
    return value


def _require_int(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ControllerReadinessError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return value


def _parse_utc_timestamp(value: Any, label: str) -> int:
    raw = _require_string(value, label)
    match = _UTC_RFC3339_NANO_RE.fullmatch(raw)
    if match is None:
        raise ControllerReadinessError(f"{label} must be UTC RFC3339")
    try:
        parsed = datetime.strptime(
            f"{match.group('date')}T{match.group('time')}",
            "%Y-%m-%dT%H:%M:%S",
        ).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ControllerReadinessError(f"{label} must be UTC RFC3339") from exc
    fraction = (match.group("fraction") or "").ljust(9, "0")
    seconds = (parsed - datetime(1970, 1, 1, tzinfo=timezone.utc)).days * 86_400
    seconds += parsed.hour * 3_600 + parsed.minute * 60 + parsed.second
    return seconds * 1_000_000_000 + int(fraction or "0")


def _is_temporary_name(name: str) -> bool:
    if name.startswith((".", "~")):
        return True
    lowered = name.lower()
    return lowered.endswith((".tmp", ".part"))


def _validate_direct_name(value: Any, label: str) -> str:
    name = _require_string(value, label)
    if (
        name in {".", ".."}
        or "\x00" in name
        or "/" in name
        or "\\" in name
        or os.path.isabs(name)
        or _is_temporary_name(name)
    ):
        raise ControllerReadinessError(
            f"{label} must be one non-temporary direct child"
        )
    return name


def _validate_path_list(value: Any, label: str) -> list[str]:
    raw_paths = _require_list(value, label)
    paths = [
        _validate_direct_name(item, f"{label}[{index}]")
        for index, item in enumerate(raw_paths)
    ]
    if len(paths) != len(set(paths)):
        raise ControllerReadinessError(f"{label} contains duplicate paths")
    if paths != sorted(paths):
        raise ControllerReadinessError(f"{label} must be sorted")
    return paths


def _decode_json_line(line: str, line_index: int) -> Mapping[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ControllerReadinessError(
                    f"report line {line_index} contains a duplicate JSON key"
                )
            result[key] = value
        return result

    try:
        decoded = json.loads(line, object_pairs_hook=reject_duplicates)
    except ControllerReadinessError:
        raise
    except json.JSONDecodeError as exc:
        raise ControllerReadinessError(
            f"report line {line_index} is not valid JSON"
        ) from exc
    return _require_mapping(decoded, f"report line {line_index}")


def _read_ledger(path: Path) -> tuple[bytes, list[Mapping[str, Any]]]:
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
            raise ControllerReadinessError(
                "evidence input must be a regular file with exactly one hard link"
            )
        if before.st_size <= 0 or before.st_size > MAX_LEDGER_BYTES:
            raise ControllerReadinessError("evidence input size is outside the closed limit")

        chunks: list[bytes] = []
        bytes_read = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, MAX_LEDGER_BYTES + 1 - bytes_read))
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)
            if bytes_read > MAX_LEDGER_BYTES:
                raise ControllerReadinessError(
                    "evidence input size is outside the closed limit"
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
        if any(
            getattr(before, field) != getattr(after, field)
            for field in stable_fields
        ):
            raise ControllerReadinessError("evidence input changed while it was read")
        if any(
            getattr(after, field) != getattr(current, field)
            for field in stable_fields
        ):
            raise ControllerReadinessError(
                "evidence input pathname changed while it was read"
            )
    except ControllerReadinessError:
        raise
    except OSError as exc:
        raise ControllerReadinessError(
            "evidence input is not safely readable"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)

    raw = b"".join(chunks)
    if not raw.endswith(b"\n"):
        raise ControllerReadinessError(
            "evidence input must end with one complete JSONL record"
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ControllerReadinessError("evidence input must be strict UTF-8") from exc
    lines = text.splitlines()
    if not lines or len(lines) > MAX_LEDGER_RUNS:
        raise ControllerReadinessError("evidence input run count is outside the closed limit")
    if any(not line.strip() for line in lines):
        raise ControllerReadinessError("evidence input contains a blank JSONL record")
    reports = [
        _decode_json_line(line, line_index)
        for line_index, line in enumerate(lines, start=1)
    ]
    return raw, reports


def _validate_report(
    report: Mapping[str, Any],
    *,
    line_index: int,
) -> tuple[str, int, int, int, int]:
    label = f"report line {line_index}"
    _require_exact_keys(report, _REPORT_KEYS, label)
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ControllerReadinessError(f"{label} has an unsupported schema")
    if report.get("mode") != "read_only":
        raise ControllerReadinessError(f"{label} is not read_only")
    for field in ("ok", "polling_match", "kill_switch_configured"):
        if not _require_bool(report.get(field), f"{label}.{field}"):
            raise ControllerReadinessError(f"{label}.{field} must be true")

    run_id = _require_string(report.get("run_id"), f"{label}.run_id")
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise ControllerReadinessError(f"{label}.run_id is invalid")
    started_at = _parse_utc_timestamp(report.get("started_at"), f"{label}.started_at")
    completed_at = _parse_utc_timestamp(
        report.get("completed_at"),
        f"{label}.completed_at",
    )
    if completed_at < started_at:
        raise ControllerReadinessError(f"{label} completed before it started")

    scan_count = _require_int(
        report.get("scan_count"),
        f"{label}.scan_count",
        minimum=1,
        maximum=MAX_SCANS_PER_RUN,
    )
    raw_scans = _require_list(report.get("scan_comparisons"), f"{label}.scan_comparisons")
    if len(raw_scans) != scan_count:
        raise ControllerReadinessError(f"{label} scan_count does not match its scans")

    stable_by_scan: dict[int, set[str]] = {}
    for offset, raw_scan in enumerate(raw_scans, start=1):
        scan_label = f"{label}.scan_comparisons[{offset - 1}]"
        scan = _require_mapping(raw_scan, scan_label)
        _require_exact_keys(scan, _SCAN_KEYS, scan_label)
        scan_index = _require_int(
            scan.get("scan_index"),
            f"{scan_label}.scan_index",
            minimum=1,
            maximum=scan_count,
        )
        if scan_index != offset:
            raise ControllerReadinessError(f"{label} scan indexes are not contiguous")
        if not _require_bool(scan.get("match"), f"{scan_label}.match"):
            raise ControllerReadinessError(f"{scan_label}.match must be true")
        go_paths = _validate_path_list(
            scan.get("go_stable_relative_paths"),
            f"{scan_label}.go_stable_relative_paths",
        )
        python_paths = _validate_path_list(
            scan.get("python_stable_relative_paths"),
            f"{scan_label}.python_stable_relative_paths",
        )
        if go_paths != python_paths:
            raise ControllerReadinessError(f"{scan_label} path sets do not match")
        stable_by_scan[scan_index] = set(go_paths)

    raw_checks = _require_list(report.get("plan_checks"), f"{label}.plan_checks")
    if len(raw_checks) > MAX_PLAN_CHECKS_PER_RUN:
        raise ControllerReadinessError(f"{label} has too many plan checks")
    seen_checks: set[tuple[int, str]] = set()
    for check_offset, raw_check in enumerate(raw_checks):
        check_label = f"{label}.plan_checks[{check_offset}]"
        check = _require_mapping(raw_check, check_label)
        _require_exact_keys(check, _PLAN_CHECK_KEYS, check_label)
        if check.get("status") != "verified":
            raise ControllerReadinessError(f"{check_label} is not verified")
        digest = _require_string(check.get("plan_sha256"), f"{check_label}.plan_sha256")
        if _SHA256_RE.fullmatch(digest) is None:
            raise ControllerReadinessError(f"{check_label}.plan_sha256 is invalid")
        relative_path = _validate_direct_name(
            check.get("relative_path"),
            f"{check_label}.relative_path",
        )
        scan_index = _require_int(
            check.get("scan_index"),
            f"{check_label}.scan_index",
            minimum=1,
            maximum=scan_count,
        )
        key = (scan_index, relative_path)
        if key in seen_checks:
            raise ControllerReadinessError(f"{check_label} duplicates a plan check")
        seen_checks.add(key)
        if relative_path not in stable_by_scan[scan_index]:
            raise ControllerReadinessError(
                f"{check_label} is not linked to its stable scan"
            )

    verified_count = _require_int(
        report.get("verified_count"),
        f"{label}.verified_count",
        minimum=0,
        maximum=MAX_PLAN_CHECKS_PER_RUN,
    )
    if verified_count != len(raw_checks):
        raise ControllerReadinessError(
            f"{label} verified_count does not match plan checks"
        )
    return run_id, started_at, completed_at, scan_count, verified_count


def validate_controller_success_report(
    report: Mapping[str, Any],
    *,
    line_index: int = 1,
) -> dict[str, Any]:
    """Validate one successful report@4 without applying ledger thresholds."""

    run_id, started_at, completed_at, scan_count, verified_count = _validate_report(
        report,
        line_index=line_index,
    )
    return {
        "run_id": run_id,
        "started_at_ns": started_at,
        "completed_at_ns": completed_at,
        "scan_count": scan_count,
        "verified_count": verified_count,
    }


def verify_controller_readiness(
    path: Path,
    *,
    min_runs: int = DEFAULT_MIN_RUNS,
    min_verified: int = DEFAULT_MIN_VERIFIED,
) -> dict[str, Any]:
    min_runs = _require_int(
        min_runs,
        "min_runs",
        minimum=1,
        maximum=MAX_LEDGER_RUNS,
    )
    min_verified = _require_int(
        min_verified,
        "min_verified",
        minimum=1,
        maximum=MAX_CRITERION_VERIFIED,
    )
    raw, reports = _read_ledger(path)
    seen_run_ids: set[str] = set()
    previous_started_at: int | None = None
    previous_completed_at: int | None = None
    scan_count = 0
    verified_count = 0
    for line_index, report in enumerate(reports, start=1):
        validated = validate_controller_success_report(
            report,
            line_index=line_index,
        )
        run_id = str(validated["run_id"])
        started_at = int(validated["started_at_ns"])
        completed_at = int(validated["completed_at_ns"])
        scans = int(validated["scan_count"])
        verified = int(validated["verified_count"])
        if run_id in seen_run_ids:
            raise ControllerReadinessError("evidence input contains a duplicate run id")
        if previous_started_at is not None and started_at <= previous_started_at:
            raise ControllerReadinessError(
                "evidence input run start times are not strictly increasing"
            )
        if previous_completed_at is not None and started_at < previous_completed_at:
            raise ControllerReadinessError("evidence input contains overlapping runs")
        seen_run_ids.add(run_id)
        previous_started_at = started_at
        previous_completed_at = completed_at
        scan_count += scans
        verified_count += verified

    run_count = len(reports)
    if run_count < min_runs or verified_count < min_verified:
        raise ControllerReadinessError(
            "evidence input does not satisfy the closed readiness criteria"
        )
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "ready": True,
        "run_count": run_count,
        "scan_count": scan_count,
        "verified_count": verified_count,
        "ledger_sha256": hashlib.sha256(raw).hexdigest(),
        "criteria": {
            "min_runs": min_runs,
            "min_verified": min_verified,
            "report_schema_version": REPORT_SCHEMA_VERSION,
        },
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a closed read-only controller shadow evidence ledger"
    )
    parser.add_argument("--input", required=True, help="JSONL shadow report ledger")
    parser.add_argument("--min-runs", type=int, default=DEFAULT_MIN_RUNS)
    parser.add_argument("--min-verified", type=int, default=DEFAULT_MIN_VERIFIED)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = verify_controller_readiness(
            Path(args.input),
            min_runs=args.min_runs,
            min_verified=args.min_verified,
        )
    except ControllerReadinessError as exc:
        print(f"Controller readiness evidence rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
