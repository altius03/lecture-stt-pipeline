from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
import sqlite3
import stat
import unicodedata
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Mapping, Sequence

import yaml

from lecture_stt.shared import utils
from lecture_stt.shared.paths import default_db_path, state_dir
from lecture_stt.storage_v2.repository import connect_v2, require_v2_schema


SEMESTER_MANIFEST_SCHEMA_VERSION = "lecture-stt/semester-manifest@2"
SEMESTER_PLAN_SCHEMA_VERSION = "lecture-stt/semester-plan@2"
ACTIVE_SEMESTER_SCHEMA_VERSION = "lecture-stt/active-semester@2"
DEFAULT_ACTIVE_SEMESTER_PATH = state_dir() / "active-semester.json"
DEFAULT_STORAGE_V2_DB_PATH = state_dir() / "storage-v2.sqlite3"
DEFAULT_JOBS_DB_PATH = default_db_path()
DEFAULT_ORIGIN_SUBDIR = "06_lecture_notes/02_origin"
DEFAULT_SUMMARY_SUBDIR = "06_lecture_notes/01_summarize"
_SEOUL = ZoneInfo("Asia/Seoul")


class SemesterManifestError(RuntimeError):
    """The semester manifest or active snapshot is invalid."""


class SemesterApplyConflictError(SemesterManifestError):
    """Apply guards do not match the current canonical plan."""


@dataclass(frozen=True)
class SemesterCourse:
    course_code: str
    course_name: str
    course_dir: str
    aliases: tuple[str, ...]
    course_root: Path
    origin_dir: Path
    summary_dir: Path

    @property
    def primary_route_token(self) -> str:
        if self.aliases:
            return self.aliases[0]
        return self.course_code


@dataclass(frozen=True)
class ActiveSemester:
    semester: str
    vault_root: Path
    semester_root: Path
    timetable_db_path: Path
    origin_subdir: str
    summary_subdir: str
    match_margin_minutes: int
    activated_at: str
    activation_plan_sha256: str
    manifest_source_path: Path
    manifest_source_sha256: str
    expected_course_count: int
    courses: tuple[SemesterCourse, ...]

    def course_for_token(self, token: str) -> tuple[SemesterCourse | None, str | None]:
        normalized = _normalize_token(token)
        alias_matches = [
            course for course in self.courses if normalized in {_normalize_token(alias) for alias in course.aliases}
        ]
        if len(alias_matches) == 1:
            return alias_matches[0], "filename_alias"
        if len(alias_matches) > 1:
            return None, "filename_alias_ambiguous"

        code_matches = [
            course for course in self.courses if _normalize_token(course.course_code) == normalized
        ]
        if len(code_matches) == 1:
            return code_matches[0], "filename_course_code"
        if len(code_matches) > 1:
            return None, "filename_course_code_ambiguous"

        name_matches = [
            course for course in self.courses if _normalize_token(course.course_name) == normalized
        ]
        if len(name_matches) == 1:
            return name_matches[0], "filename_course_name"
        if len(name_matches) > 1:
            return None, "filename_course_name_ambiguous"
        return None, None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_sha256(value: Any, *, field: str) -> str:
    normalized = _normalize_text(value, field=field, max_length=64).lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise SemesterManifestError(f"{field} must be a canonical SHA-256")
    return normalized


def _normalize_aware_iso(value: Any, *, field: str) -> str:
    normalized = _normalize_text(value, field=field, max_length=64)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SemesterManifestError(f"{field} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SemesterManifestError(f"{field} must include a timezone offset")
    return parsed.isoformat()


def _normalize_text(
    value: Any,
    *,
    field: str,
    max_length: int | None = None,
) -> str:
    if not isinstance(value, (str, int)):
        raise SemesterManifestError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", str(value).strip())
    if not normalized:
        raise SemesterManifestError(f"{field} must not be empty")
    if max_length is not None and len(normalized) > max_length:
        raise SemesterManifestError(f"{field} exceeds the {max_length}-character limit")
    return normalized


def _normalize_token(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value).strip()).split()).upper()


def _register_routing_token(
    namespace: dict[str, str],
    value: str,
    *,
    field: str,
) -> None:
    normalized = _normalize_token(value)
    previous = namespace.get(normalized)
    if previous is not None:
        raise SemesterManifestError(
            f"Routing token collision after normalization: {field} conflicts with {previous}"
        )
    namespace[normalized] = field


def _require_mapping(payload: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SemesterManifestError(f"{field} must be a mapping")
    return payload


def _ensure_allowed_keys(payload: Mapping[str, Any], *, field: str, allowed: set[str]) -> None:
    unknown = sorted(set(payload).difference(allowed))
    if unknown:
        raise SemesterManifestError(f"{field} has unknown keys: {', '.join(unknown)}")


def _expand_path(raw: Any, *, field: str, base_dir: Path) -> Path:
    if isinstance(raw, os.PathLike):
        text = os.fspath(raw)
    else:
        text = _normalize_text(raw, field=field, max_length=4096)
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve(strict=False)
    return path


def _assert_no_symlink_components(path: Path, *, field: str) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path.cwd()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current = current / part
        if os.path.lexists(current) and current.is_symlink():
            raise SemesterManifestError(f"{field} must not contain symlink components: {current}")


def _validate_existing_directory(path: Path, *, field: str) -> Path:
    _assert_no_symlink_components(path, field=field)
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise SemesterManifestError(f"{field} does not exist: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise SemesterManifestError(f"{field} must be an existing directory: {path}")
    return path.resolve(strict=True)


def _validate_existing_file(path: Path, *, field: str) -> Path:
    _assert_no_symlink_components(path, field=field)
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise SemesterManifestError(f"{field} does not exist: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise SemesterManifestError(f"{field} must be an existing regular file: {path}")
    return path.resolve(strict=True)


def _validate_target_file(path: Path, *, field: str) -> Path:
    _assert_no_symlink_components(path.parent, field=f"{field}.parent")
    if not path.parent.exists():
        raise SemesterManifestError(f"{field}.parent does not exist: {path.parent}")
    if not path.parent.is_dir():
        raise SemesterManifestError(f"{field}.parent must be a directory: {path.parent}")
    if path.exists():
        _validate_existing_file(path, field=field)
    return path


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temp_path = path.parent / f".{path.name}.{utils.short_id(12)}.tmp"
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        written = 0
        while written < len(content):
            written += os.write(descriptor, content[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        temp_path.unlink(missing_ok=True)


def _assert_relative_subpath(raw: Any, *, field: str) -> str:
    value = _normalize_text(raw, field=field, max_length=512)
    path = PurePath(value)
    if path.is_absolute():
        raise SemesterManifestError(f"{field} must be a relative path")
    if not path.parts:
        raise SemesterManifestError(f"{field} must not be empty")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise SemesterManifestError(f"{field} must not contain empty, '.' or '..' segments")
    return str(path)


def _assert_under_root(path: Path, root: Path, *, field: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SemesterManifestError(f"{field} must stay under {root}: {path}") from exc


def _read_manifest_yaml(path: Path) -> dict[str, Any]:
    manifest_path = _validate_existing_file(path, field="manifest_path")
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise SemesterManifestError(f"manifest_path is not valid YAML: {exc}") from exc
    return _require_mapping(payload, field="manifest")


def _selected_timetable_courses(db_path: Path, semester: str) -> tuple[str, list[dict[str, str]]]:
    conn = connect_v2(db_path, readonly=True)
    try:
        require_v2_schema(conn)
        selection = conn.execute(
            """
            SELECT selection_plan_sha256
            FROM schedule_semester_selections
            WHERE semester = ?
            """,
            (semester,),
        ).fetchone()
        if selection is None:
            raise SemesterManifestError(f"No selected timetable semester found for {semester}")
        rows = conn.execute(
            """
            SELECT DISTINCT course_code, course_name
            FROM schedule_entries AS entry
            JOIN schedule_semester_selections AS selection
              ON selection.schedule_import_id = entry.schedule_import_id
             AND selection.semester = entry.semester
            WHERE entry.semester = ?
            ORDER BY course_code, course_name
            """,
            (semester,),
        ).fetchall()
    finally:
        conn.close()

    courses: list[dict[str, str]] = []
    for row in rows:
        code = _normalize_text(row["course_code"], field="schedule_entries.course_code", max_length=128)
        name = _normalize_text(row["course_name"], field="schedule_entries.course_name", max_length=256)
        courses.append({"course_code": code, "course_name": name})
    return str(selection["selection_plan_sha256"]), courses


def _validated_courses(
    payload: Sequence[Any],
    *,
    semester_root: Path,
    origin_subdir: str,
    summary_subdir: str,
    semester: str,
    selected_courses: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload:
        raise SemesterManifestError("courses must be a non-empty list")

    seen_codes: set[str] = set()
    seen_dirs: set[str] = set()
    seen_aliases: set[str] = set()
    routing_tokens: dict[str, str] = {}
    courses: list[dict[str, Any]] = []

    for index, raw_course in enumerate(payload, start=1):
        course = _require_mapping(raw_course, field=f"courses[{index}]")
        _ensure_allowed_keys(
            course,
            field=f"courses[{index}]",
            allowed={"course_code", "course_name", "course_dir", "aliases"},
        )
        course_code = _normalize_text(
            course.get("course_code"),
            field=f"courses[{index}].course_code",
            max_length=128,
        )
        course_name = _normalize_text(
            course.get("course_name"),
            field=f"courses[{index}].course_name",
            max_length=256,
        )
        if (course_code, course_name) not in selected_courses:
            raise SemesterManifestError(
                f"courses[{index}] is not present in the selected timetable set "
                f"for {semester}: {course_code} / {course_name}"
            )
        normalized_code = _normalize_token(course_code)
        if normalized_code in seen_codes:
            raise SemesterManifestError(f"Duplicate course_code: {course_code}")
        seen_codes.add(normalized_code)
        _register_routing_token(
            routing_tokens,
            course_code,
            field=f"courses[{index}].course_code",
        )
        _register_routing_token(
            routing_tokens,
            course_name,
            field=f"courses[{index}].course_name",
        )

        course_dir = _assert_relative_subpath(
            course.get("course_dir"),
            field=f"courses[{index}].course_dir",
        )
        normalized_dir = _normalize_token(course_dir)
        if normalized_dir in seen_dirs:
            raise SemesterManifestError(f"Duplicate course_dir: {course_dir}")
        seen_dirs.add(normalized_dir)

        aliases_raw = course.get("aliases")
        if not isinstance(aliases_raw, list):
            raise SemesterManifestError(f"courses[{index}].aliases must be a list")
        aliases: list[str] = []
        course_aliases_seen: set[str] = set()
        for alias_index, raw_alias in enumerate(aliases_raw, start=1):
            alias = _normalize_text(
                raw_alias,
                field=f"courses[{index}].aliases[{alias_index}]",
                max_length=128,
            )
            normalized_alias = _normalize_token(alias)
            if normalized_alias in course_aliases_seen:
                raise SemesterManifestError(
                    f"Duplicate alias within courses[{index}]: {alias}"
                )
            if normalized_alias in seen_aliases:
                raise SemesterManifestError(f"Duplicate alias across courses: {alias}")
            course_aliases_seen.add(normalized_alias)
            seen_aliases.add(normalized_alias)
            _register_routing_token(
                routing_tokens,
                alias,
                field=f"courses[{index}].aliases[{alias_index}]",
            )
            aliases.append(alias)

        course_root = semester_root / course_dir
        course_root_resolved = _validate_existing_directory(
            course_root,
            field=f"courses[{index}].course_root",
        )
        _assert_under_root(
            course_root_resolved,
            semester_root,
            field=f"courses[{index}].course_root",
        )
        origin_dir = course_root_resolved / origin_subdir
        origin_dir_resolved = _validate_existing_directory(
            origin_dir,
            field=f"courses[{index}].origin_dir",
        )
        _assert_under_root(
            origin_dir_resolved,
            course_root_resolved,
            field=f"courses[{index}].origin_dir",
        )
        summary_dir = course_root_resolved / summary_subdir
        summary_dir_resolved = _validate_existing_directory(
            summary_dir,
            field=f"courses[{index}].summary_dir",
        )
        _assert_under_root(
            summary_dir_resolved,
            course_root_resolved,
            field=f"courses[{index}].summary_dir",
        )

        courses.append(
            {
                "course_code": course_code,
                "course_name": course_name,
                "course_dir": course_dir,
                "aliases": aliases,
            }
        )

    return courses


def plan_semester_manifest(
    manifest_path: Path | str,
    *,
    timetable_db_path: Path | str | None = None,
) -> dict[str, Any]:
    manifest_input = Path(manifest_path).expanduser()
    raw = _read_manifest_yaml(manifest_input)
    _ensure_allowed_keys(
        raw,
        field="manifest",
        allowed={
            "schema_version",
            "semester",
            "vault_root",
            "semester_root",
            "timetable_db_path",
            "origin_subdir",
            "summary_subdir",
            "match_margin_minutes",
            "courses",
        },
    )
    if raw.get("schema_version") != SEMESTER_MANIFEST_SCHEMA_VERSION:
        raise SemesterManifestError(
            "manifest.schema_version must be lecture-stt/semester-manifest@2"
        )

    base_dir = manifest_input.parent.resolve(strict=True)
    semester = _normalize_text(raw.get("semester"), field="semester", max_length=128)
    vault_root = _expand_path(raw.get("vault_root"), field="vault_root", base_dir=base_dir)
    semester_root = _expand_path(
        raw.get("semester_root"),
        field="semester_root",
        base_dir=base_dir,
    )
    selected_db_path = (
        _expand_path(timetable_db_path, field="timetable_db_path", base_dir=base_dir)
        if timetable_db_path is not None
        else _expand_path(raw.get("timetable_db_path"), field="timetable_db_path", base_dir=base_dir)
    )
    origin_subdir = _assert_relative_subpath(
        raw.get("origin_subdir", DEFAULT_ORIGIN_SUBDIR),
        field="origin_subdir",
    )
    summary_subdir = _assert_relative_subpath(
        raw.get("summary_subdir", DEFAULT_SUMMARY_SUBDIR),
        field="summary_subdir",
    )
    match_margin_minutes = raw.get("match_margin_minutes", 30)
    if isinstance(match_margin_minutes, bool) or not isinstance(match_margin_minutes, int):
        raise SemesterManifestError("match_margin_minutes must be an integer")
    if not 0 <= match_margin_minutes <= 120:
        raise SemesterManifestError("match_margin_minutes must be between 0 and 120")

    vault_root_resolved = _validate_existing_directory(vault_root, field="vault_root")
    semester_root_resolved = _validate_existing_directory(
        semester_root,
        field="semester_root",
    )
    _validate_existing_file(selected_db_path, field="timetable_db_path")
    _assert_under_root(semester_root_resolved, vault_root_resolved, field="semester_root")
    _validate_existing_directory(vault_root_resolved / ".obsidian", field="vault_root/.obsidian")

    timetable_selection_plan_sha256, selected_timetable_courses = _selected_timetable_courses(
        selected_db_path,
        semester,
    )
    selected_pairs = {
        (row["course_code"], row["course_name"])
        for row in selected_timetable_courses
    }
    courses = _validated_courses(
        raw.get("courses", []),
        semester_root=semester_root_resolved,
        origin_subdir=origin_subdir,
        summary_subdir=summary_subdir,
        semester=semester,
        selected_courses=selected_pairs,
    )
    manifest_pairs = {
        (course["course_code"], course["course_name"])
        for course in courses
    }
    if manifest_pairs != selected_pairs:
        missing = sorted(selected_pairs.difference(manifest_pairs))
        extra = sorted(manifest_pairs.difference(selected_pairs))
        details: list[str] = []
        if missing:
            details.append(
                "missing from manifest: "
                + ", ".join(f"{code} / {name}" for code, name in missing)
            )
        if extra:
            details.append(
                "extra in manifest: "
                + ", ".join(f"{code} / {name}" for code, name in extra)
            )
        raise SemesterManifestError(
            "courses must exactly match the selected timetable course set for "
            f"{semester}; " + "; ".join(details)
        )

    source_sha256 = utils.compute_sha256(manifest_input.resolve(strict=True))
    digest_payload = {
        "schema_version": SEMESTER_PLAN_SCHEMA_VERSION,
        "semester": semester,
        "vault_root": str(vault_root_resolved),
        "semester_root": str(semester_root_resolved),
        "timetable_db_path": str(selected_db_path.resolve(strict=True)),
        "origin_subdir": origin_subdir,
        "summary_subdir": summary_subdir,
        "match_margin_minutes": match_margin_minutes,
        "courses": courses,
        "selected_timetable_courses": selected_timetable_courses,
        "timetable_selection_plan_sha256": timetable_selection_plan_sha256,
        "expected_course_count": len(courses),
    }
    plan_sha256 = _sha256_json(digest_payload)

    return {
        **digest_payload,
        "source_path": str(manifest_input.resolve(strict=True)),
        "source_sha256": source_sha256,
        "plan_sha256": plan_sha256,
    }


def _validate_apply_guards(
    plan: Mapping[str, Any],
    *,
    expected_course_count: int,
    expected_plan_sha256: str,
    allow_write: bool,
) -> None:
    if not allow_write:
        raise SemesterApplyConflictError("semester apply requires --allow-write")
    if isinstance(expected_course_count, bool) or expected_course_count != plan["expected_course_count"]:
        raise SemesterApplyConflictError(
            "semester apply expected_course_count does not match the current plan"
        )
    normalized_digest = str(expected_plan_sha256 or "").strip().lower()
    if len(normalized_digest) != 64 or any(ch not in "0123456789abcdef" for ch in normalized_digest):
        raise SemesterApplyConflictError("expected_plan_sha256 must be a canonical SHA-256")
    if normalized_digest != str(plan["plan_sha256"]):
        raise SemesterApplyConflictError(
            "semester apply expected_plan_sha256 does not match the current plan"
        )


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _parse_job_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_SEOUL)
    return parsed


def inspect_unqueued_done_since(conn: sqlite3.Connection, cutoff: str) -> dict[str, int]:
    cutoff_dt = _parse_job_datetime(cutoff)
    if cutoff_dt is None:
        raise SemesterApplyConflictError("current active semester activated_at is invalid")
    rows = conn.execute(
        """
        SELECT jobs.id, jobs.created_at, jobs.updated_at, jobs.ended_at, jobs.engine_params
        FROM jobs
        LEFT JOIN transcript_postprocess_jobs AS postprocess
          ON postprocess.source_job_id = jobs.id
        WHERE jobs.status = 'DONE' AND postprocess.source_job_id IS NULL
        """
    ).fetchall()
    unqueued = 0
    invalid = 0
    for row in rows:
        try:
            payload = json.loads(row[4]) if isinstance(row[4], str) and row[4].strip() else {}
        except json.JSONDecodeError:
            payload = {}
        timings = payload.get("timings") if isinstance(payload, dict) else None
        candidates = (
            timings.get("ended_at") if isinstance(timings, dict) else None,
            row[3],
            row[2],
            row[1],
        )
        completed_at = next(
            (parsed for candidate in candidates if (parsed := _parse_job_datetime(candidate))),
            None,
        )
        if completed_at is None:
            invalid += 1
            continue
        if completed_at >= cutoff_dt:
            unqueued += 1
    return {
        "unqueued_done_jobs": unqueued,
        "invalid_unqueued_done_jobs": invalid,
    }


def count_unqueued_done_since(conn: sqlite3.Connection, cutoff: str) -> int:
    return inspect_unqueued_done_since(conn, cutoff)["unqueued_done_jobs"]


def _assert_semester_switch_idle(
    conn: sqlite3.Connection,
    *,
    current_activation_cutoff: str | None,
) -> dict[str, int]:
    required_tables = ("jobs", "transcript_postprocess_jobs")
    missing = [name for name in required_tables if not _table_exists(conn, name)]
    if missing:
        raise SemesterApplyConflictError(
            "semester apply jobs DB is missing required tables: " + ", ".join(missing)
        )

    active_stt = int(
        conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('PENDING', 'PROCESSING')"
        ).fetchone()[0]
    )
    pending_postprocess = int(
        conn.execute(
            "SELECT COUNT(*) FROM transcript_postprocess_jobs WHERE status = 'PENDING'"
        ).fetchone()[0]
    )
    unqueued_inspection = (
        inspect_unqueued_done_since(conn, current_activation_cutoff)
        if current_activation_cutoff is not None
        else {"unqueued_done_jobs": 0, "invalid_unqueued_done_jobs": 0}
    )
    unqueued_done = unqueued_inspection["unqueued_done_jobs"]
    invalid_unqueued_done = unqueued_inspection["invalid_unqueued_done_jobs"]
    if active_stt or pending_postprocess or unqueued_done or invalid_unqueued_done:
        raise SemesterApplyConflictError(
            "semester apply requires an idle pipeline: "
            f"active_stt_jobs={active_stt}, "
            f"pending_postprocess_jobs={pending_postprocess}, "
            f"unqueued_done_jobs={unqueued_done}, "
            f"invalid_unqueued_done_jobs={invalid_unqueued_done}"
        )
    return {
        "active_stt_jobs": active_stt,
        "pending_postprocess_jobs": pending_postprocess,
        "unqueued_done_jobs": unqueued_done,
        "invalid_unqueued_done_jobs": invalid_unqueued_done,
    }


def apply_semester_manifest(
    manifest_path: Path | str,
    *,
    expected_course_count: int,
    expected_plan_sha256: str,
    allow_write: bool = False,
    timetable_db_path: Path | str | None = None,
    active_path: Path | str = DEFAULT_ACTIVE_SEMESTER_PATH,
    jobs_db_path: Path | str = DEFAULT_JOBS_DB_PATH,
) -> dict[str, Any]:
    plan = plan_semester_manifest(
        manifest_path,
        timetable_db_path=timetable_db_path,
    )
    _validate_apply_guards(
        plan,
        expected_course_count=expected_course_count,
        expected_plan_sha256=expected_plan_sha256,
        allow_write=allow_write,
    )
    active_target = _validate_target_file(Path(active_path).expanduser(), field="active_path")
    payload = {
        "schema_version": ACTIVE_SEMESTER_SCHEMA_VERSION,
        "activated_at": utils.now_iso(),
        "activation_plan_sha256": plan["plan_sha256"],
        "manifest_source_path": plan["source_path"],
        "manifest_source_sha256": plan["source_sha256"],
        "expected_course_count": plan["expected_course_count"],
        "semester": plan["semester"],
        "vault_root": plan["vault_root"],
        "semester_root": plan["semester_root"],
        "timetable_db_path": plan["timetable_db_path"],
        "origin_subdir": plan["origin_subdir"],
        "summary_subdir": plan["summary_subdir"],
        "match_margin_minutes": plan["match_margin_minutes"],
        "courses": plan["courses"],
        "selected_timetable_courses": plan["selected_timetable_courses"],
        "timetable_selection_plan_sha256": plan["timetable_selection_plan_sha256"],
    }
    jobs_db = _validate_existing_file(
        Path(jobs_db_path).expanduser(),
        field="jobs_db_path",
    )
    conn = sqlite3.connect(str(jobs_db), timeout=5.0)
    try:
        # Serialize the final idle check with controller/downstream DB writes.
        # Once the snapshot is replaced, newly admitted work sees the new
        # semester boundary.
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("BEGIN IMMEDIATE")
        # Competing semester apply commands use the same DB lock, so re-read
        # the current snapshot only after this process owns the guard window.
        current_active = load_active_semester(active_target) if active_target.exists() else None
        switch_guard = _assert_semester_switch_idle(
            conn,
            current_activation_cutoff=(
                current_active.activated_at if current_active is not None else None
            ),
        )
        _durable_atomic_write_json(active_target, payload)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "schema_version": "lecture-stt/active-semester-apply@2",
        "ok": True,
        "active_path": str(active_target),
        "activated_at": payload["activated_at"],
        "plan_sha256": plan["plan_sha256"],
        "expected_course_count": plan["expected_course_count"],
        "switch_guard": switch_guard,
    }


def _courses_from_payload(payload: Mapping[str, Any]) -> tuple[SemesterCourse, ...]:
    semester_root = _validate_existing_directory(
        Path(_normalize_text(payload.get("semester_root"), field="semester_root", max_length=4096)).expanduser(),
        field="semester_root",
    )
    origin_subdir = _assert_relative_subpath(
        payload.get("origin_subdir", DEFAULT_ORIGIN_SUBDIR),
        field="origin_subdir",
    )
    summary_subdir = _assert_relative_subpath(
        payload.get("summary_subdir", DEFAULT_SUMMARY_SUBDIR),
        field="summary_subdir",
    )
    raw_courses = payload.get("courses")
    if not isinstance(raw_courses, list) or not raw_courses:
        raise SemesterManifestError("courses must be a non-empty list")
    courses: list[SemesterCourse] = []
    seen_codes: set[str] = set()
    seen_dirs: set[str] = set()
    seen_aliases: set[str] = set()
    routing_tokens: dict[str, str] = {}
    for index, raw_course in enumerate(raw_courses, start=1):
        course = _require_mapping(raw_course, field=f"courses[{index}]")
        course_code = _normalize_text(course.get("course_code"), field=f"courses[{index}].course_code", max_length=128)
        course_name = _normalize_text(course.get("course_name"), field=f"courses[{index}].course_name", max_length=256)
        course_dir = _assert_relative_subpath(course.get("course_dir"), field=f"courses[{index}].course_dir")
        aliases_raw = course.get("aliases")
        if not isinstance(aliases_raw, list):
            raise SemesterManifestError(f"courses[{index}].aliases must be a list")
        aliases = tuple(
            _normalize_text(alias, field=f"courses[{index}].aliases[{alias_index}]", max_length=128)
            for alias_index, alias in enumerate(aliases_raw, start=1)
        )
        normalized_code = _normalize_token(course_code)
        normalized_dir = _normalize_token(course_dir)
        if normalized_code in seen_codes:
            raise SemesterManifestError(f"Duplicate course_code in active snapshot: {course_code}")
        if normalized_dir in seen_dirs:
            raise SemesterManifestError(f"Duplicate course_dir in active snapshot: {course_dir}")
        seen_codes.add(normalized_code)
        seen_dirs.add(normalized_dir)
        _register_routing_token(
            routing_tokens,
            course_code,
            field=f"courses[{index}].course_code",
        )
        _register_routing_token(
            routing_tokens,
            course_name,
            field=f"courses[{index}].course_name",
        )
        for alias in aliases:
            normalized_alias = _normalize_token(alias)
            if normalized_alias in seen_aliases:
                raise SemesterManifestError(f"Duplicate alias in active snapshot: {alias}")
            seen_aliases.add(normalized_alias)
            _register_routing_token(
                routing_tokens,
                alias,
                field=f"courses[{index}].aliases",
            )
        course_root = _validate_existing_directory(
            semester_root / course_dir,
            field=f"courses[{index}].course_root",
        )
        _assert_under_root(course_root, semester_root, field=f"courses[{index}].course_root")
        origin_dir = _validate_existing_directory(
            course_root / origin_subdir,
            field=f"courses[{index}].origin_dir",
        )
        _assert_under_root(origin_dir, course_root, field=f"courses[{index}].origin_dir")
        summary_dir = _validate_existing_directory(
            course_root / summary_subdir,
            field=f"courses[{index}].summary_dir",
        )
        _assert_under_root(summary_dir, course_root, field=f"courses[{index}].summary_dir")
        courses.append(
            SemesterCourse(
                course_code=course_code,
                course_name=course_name,
                course_dir=course_dir,
                aliases=aliases,
                course_root=course_root,
                origin_dir=origin_dir,
                summary_dir=summary_dir,
            )
        )
    return tuple(courses)


def load_active_semester(path: Path | str = DEFAULT_ACTIVE_SEMESTER_PATH) -> ActiveSemester:
    active_path = _validate_existing_file(Path(path).expanduser(), field="active_path")
    try:
        payload = json.loads(active_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SemesterManifestError(f"active_path is not valid JSON: {exc}") from exc
    mapping = _require_mapping(payload, field="active")
    _ensure_allowed_keys(
        mapping,
        field="active",
        allowed={
            "schema_version",
            "activated_at",
            "activation_plan_sha256",
            "manifest_source_path",
            "manifest_source_sha256",
            "expected_course_count",
            "semester",
            "vault_root",
            "semester_root",
            "timetable_db_path",
            "origin_subdir",
            "summary_subdir",
            "match_margin_minutes",
            "courses",
            "selected_timetable_courses",
            "timetable_selection_plan_sha256",
        },
    )
    if mapping.get("schema_version") != ACTIVE_SEMESTER_SCHEMA_VERSION:
        raise SemesterManifestError(
            "active.schema_version must be lecture-stt/active-semester@2"
        )
    vault_root = _validate_existing_directory(
        Path(_normalize_text(mapping.get("vault_root"), field="vault_root", max_length=4096)).expanduser(),
        field="vault_root",
    )
    semester_root = _validate_existing_directory(
        Path(_normalize_text(mapping.get("semester_root"), field="semester_root", max_length=4096)).expanduser(),
        field="semester_root",
    )
    _assert_under_root(semester_root, vault_root, field="semester_root")
    _validate_existing_directory(vault_root / ".obsidian", field="vault_root/.obsidian")
    timetable_db_path = _validate_existing_file(
        Path(_normalize_text(mapping.get("timetable_db_path"), field="timetable_db_path", max_length=4096)).expanduser(),
        field="timetable_db_path",
    )
    courses = _courses_from_payload(mapping)
    expected_course_count = mapping.get("expected_course_count")
    if isinstance(expected_course_count, bool) or not isinstance(expected_course_count, int):
        raise SemesterManifestError("expected_course_count must be an integer")
    if expected_course_count != len(courses):
        raise SemesterManifestError("expected_course_count does not match the active course list")
    match_margin_minutes = mapping.get("match_margin_minutes", 30)
    if isinstance(match_margin_minutes, bool) or not isinstance(match_margin_minutes, int):
        raise SemesterManifestError("match_margin_minutes must be an integer")
    if not 0 <= match_margin_minutes <= 120:
        raise SemesterManifestError("match_margin_minutes must be between 0 and 120")
    manifest_source_path = Path(
        _normalize_text(
            mapping.get("manifest_source_path"),
            field="manifest_source_path",
            max_length=4096,
        )
    ).expanduser()
    if not manifest_source_path.is_absolute():
        raise SemesterManifestError("manifest_source_path must be absolute")
    return ActiveSemester(
        semester=_normalize_text(mapping.get("semester"), field="semester", max_length=128),
        vault_root=vault_root,
        semester_root=semester_root,
        timetable_db_path=timetable_db_path,
        origin_subdir=_assert_relative_subpath(mapping.get("origin_subdir", DEFAULT_ORIGIN_SUBDIR), field="origin_subdir"),
        summary_subdir=_assert_relative_subpath(mapping.get("summary_subdir", DEFAULT_SUMMARY_SUBDIR), field="summary_subdir"),
        match_margin_minutes=match_margin_minutes,
        activated_at=_normalize_aware_iso(mapping.get("activated_at"), field="activated_at"),
        activation_plan_sha256=_normalize_sha256(
            mapping.get("activation_plan_sha256"),
            field="activation_plan_sha256",
        ),
        manifest_source_path=manifest_source_path,
        manifest_source_sha256=_normalize_sha256(
            mapping.get("manifest_source_sha256"),
            field="manifest_source_sha256",
        ),
        expected_course_count=expected_course_count,
        courses=courses,
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage lecture_stt active semester routing")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="Validate and print a semester routing plan")
    plan_parser.add_argument("--manifest", required=True, help="Candidate semester YAML path")
    plan_parser.add_argument(
        "--timetable-db-path",
        default=None,
        help="Storage v2 timetable DB path override",
    )

    apply_parser = subparsers.add_parser("apply", help="Activate a guarded semester routing snapshot")
    apply_parser.add_argument("--manifest", required=True, help="Candidate semester YAML path")
    apply_parser.add_argument(
        "--timetable-db-path",
        default=None,
        help="Storage v2 timetable DB path override",
    )
    apply_parser.add_argument(
        "--active-path",
        default=str(DEFAULT_ACTIVE_SEMESTER_PATH),
        help="Target active semester JSON path",
    )
    apply_parser.add_argument(
        "--jobs-db-path",
        default=str(DEFAULT_JOBS_DB_PATH),
        help="Jobs DB used for the final idle-pipeline guard",
    )
    apply_parser.add_argument("--expected-course-count", required=True, type=int)
    apply_parser.add_argument("--expected-plan-sha256", required=True)
    apply_parser.add_argument("--allow-write", action="store_true")

    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "plan":
        result = plan_semester_manifest(
            args.manifest,
            timetable_db_path=args.timetable_db_path,
        )
    else:
        result = apply_semester_manifest(
            args.manifest,
            timetable_db_path=args.timetable_db_path,
            active_path=args.active_path,
            jobs_db_path=args.jobs_db_path,
            expected_course_count=args.expected_course_count,
            expected_plan_sha256=args.expected_plan_sha256,
            allow_write=args.allow_write,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
