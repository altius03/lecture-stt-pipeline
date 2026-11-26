from __future__ import annotations

import os
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePath
from typing import Any
from zoneinfo import ZoneInfo

from lecture_stt.downstream.semester import ActiveSemester, SemesterCourse
from lecture_stt.storage_v2.repository import connect_v2, require_v2_schema


ROUTE_KIND_ROUTED = "routed"
ROUTE_KIND_SKIPPED = "skipped"
ROUTE_KIND_UNROUTED = "unrouted"

_SEOUL = ZoneInfo("Asia/Seoul")
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class CourseStorageError(RuntimeError):
    """Course-scoped storage or routing inputs are invalid."""


@dataclass(frozen=True)
class CourseRoutingSource:
    canonical_base: str
    orig_name: str
    profile_key: str | None
    recorded_at: str | None

    @property
    def filename_stems(self) -> tuple[str, ...]:
        ordered: list[str] = []
        for candidate in (self.canonical_base, Path(self.orig_name).stem):
            normalized = unicodedata.normalize("NFC", str(candidate).strip())
            if normalized and normalized not in ordered:
                ordered.append(normalized)
        return tuple(ordered)


@dataclass(frozen=True)
class CourseRouteDecision:
    kind: str
    route_method: str
    logical_stem: str
    course: SemesterCourse | None
    error_code: str | None = None
    error_message: str | None = None


def _normalize_text(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value).strip())


def _normalize_token(value: Any) -> str:
    return " ".join(_normalize_text(value).split()).upper()


def _iso_or_none(value: Any) -> str | None:
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
    return parsed.isoformat()


def _is_lecture_profile(profile_key: str | None) -> bool:
    return isinstance(profile_key, str) and _normalize_token(profile_key) == "LECTURE"


def _stem_suffix(stem: str) -> str | None:
    candidate = _normalize_text(stem)
    if len(candidate) <= 6 or not candidate[:6].isdigit():
        return None
    suffix = candidate[6:].strip()
    return suffix or None


def _routing_tokens_for_suffix(suffix: str) -> tuple[str, ...]:
    candidate = _normalize_text(suffix)
    tokens = [candidate]
    if "_" in candidate:
        base, occurrence = candidate.rsplit("_", 1)
        if occurrence.isdigit() and int(occurrence) > 0 and base:
            tokens.append(base)
    ordered: list[str] = []
    for token in tokens:
        if token not in ordered:
            ordered.append(token)
    return tuple(ordered)


def _match_course_by_schedule(
    active: ActiveSemester,
    recorded_at: str | None,
) -> tuple[SemesterCourse | None, str]:
    normalized = _iso_or_none(recorded_at)
    if normalized is None:
        return None, "recorded_at_missing"
    parsed = datetime.fromisoformat(normalized).astimezone(_SEOUL)
    weekday = _WEEKDAYS[parsed.weekday()]
    minutes = parsed.hour * 60 + parsed.minute

    conn = connect_v2(active.timetable_db_path, readonly=True)
    try:
        require_v2_schema(conn)
        rows = conn.execute(
            """
            SELECT DISTINCT course_code, course_name, start_time, end_time
            FROM schedule_entries AS entry
            JOIN schedule_semester_selections AS selection
              ON selection.schedule_import_id = entry.schedule_import_id
             AND selection.semester = entry.semester
            WHERE entry.semester = ?
              AND entry.weekday = ?
            ORDER BY course_code, course_name, start_time, end_time
            """,
            (active.semester, weekday),
        ).fetchall()
    finally:
        conn.close()

    distinct_courses: dict[tuple[str, str], SemesterCourse] = {}
    for row in rows:
        start_hour, start_minute = str(row["start_time"]).split(":", 1)
        end_hour, end_minute = str(row["end_time"]).split(":", 1)
        start = int(start_hour) * 60 + int(start_minute)
        end = int(end_hour) * 60 + int(end_minute)
        if not (start - active.match_margin_minutes <= minutes <= end + active.match_margin_minutes):
            continue
        pair = (str(row["course_code"]), str(row["course_name"]))
        for course in active.courses:
            if course.course_code == pair[0] and course.course_name == pair[1]:
                distinct_courses[pair] = course
                break
    if len(distinct_courses) == 1:
        return next(iter(distinct_courses.values())), "unique_time_match"
    if not distinct_courses:
        return None, "no_time_match"
    return None, "ambiguous_time_match"


def resolve_course_route(
    active: ActiveSemester,
    source: CourseRoutingSource,
) -> CourseRouteDecision:
    alias_ambiguous = False
    for stem in source.filename_stems:
        suffix = _stem_suffix(stem)
        if suffix is None:
            continue
        for route_token in _routing_tokens_for_suffix(suffix):
            course, route_method = active.course_for_token(route_token)
            if course is not None and route_method is not None:
                return CourseRouteDecision(
                    kind=ROUTE_KIND_ROUTED,
                    route_method=route_method,
                    # Routing may fall back to the original filename when the
                    # canonical basename carries a uniqueness suffix.  Output
                    # identity must nevertheless stay pinned to the canonical
                    # basename selected by the STT admission path.
                    logical_stem=source.canonical_base,
                    course=course,
                )
            if route_method is not None and route_method.endswith("_ambiguous"):
                alias_ambiguous = True

    if not _is_lecture_profile(source.profile_key):
        return CourseRouteDecision(
            kind=ROUTE_KIND_SKIPPED,
            route_method="not_lecture",
            logical_stem=source.canonical_base,
            course=None,
        )

    matched_course, reason = _match_course_by_schedule(active, source.recorded_at)
    if matched_course is None:
        return CourseRouteDecision(
            kind=ROUTE_KIND_UNROUTED,
            route_method="timetable_unique_match",
            logical_stem=source.canonical_base,
            course=None,
            error_code="UNROUTED_TIMETABLE",
            error_message=reason,
        )
    route_method = (
        "timetable_unique_match_after_alias_ambiguity"
        if alias_ambiguous
        else "timetable_unique_match"
    )
    return CourseRouteDecision(
        kind=ROUTE_KIND_ROUTED,
        route_method=route_method,
        # A date+course fallback collapses two recordings of the same class on
        # the same day.  The canonical basename is already the pipeline's
        # collision-safe identity, so keep it for every routing method.
        logical_stem=source.canonical_base,
        course=matched_course,
    )


def _assert_no_symlink_components(path: Path, *, field: str) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path.cwd()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current = current / part
        if os.path.lexists(current) and current.is_symlink():
            raise CourseStorageError(f"{field} must not contain symlink components: {current}")


def _validate_existing_directory(path: Path, *, field: str) -> Path:
    _assert_no_symlink_components(path, field=field)
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise CourseStorageError(f"{field} does not exist: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise CourseStorageError(f"{field} must be a directory: {path}")
    return path.resolve(strict=True)


def _validated_relative_parts(value: str, *, field: str) -> tuple[str, ...]:
    normalized = _normalize_text(value)
    if not normalized:
        raise CourseStorageError(f"{field} must not be empty")
    candidate = PurePath(normalized)
    if candidate.is_absolute():
        raise CourseStorageError(f"{field} must be a relative path")
    parts = candidate.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise CourseStorageError(f"{field} must not contain empty, '.' or '..' segments")
    return tuple(parts)


def _validated_leaf_name(value: str, *, field: str) -> str:
    leaf = _normalize_text(value)
    if not leaf or leaf in {".", ".."}:
        raise CourseStorageError(f"{field} must be a single filename")
    candidate = PurePath(leaf)
    if candidate.is_absolute() or len(candidate.parts) != 1 or candidate.name != leaf:
        raise CourseStorageError(f"{field} must be a single filename")
    return leaf


def ensure_course_storage_parent(
    root: Path,
    *,
    semester: str,
    course_dir: str,
    field: str,
    create: bool,
) -> Path:
    current = _validate_existing_directory(root, field=field)
    relative_parts = _validated_relative_parts(semester, field=f"{field}.semester") + _validated_relative_parts(
        course_dir,
        field=f"{field}.course_dir",
    )
    missing = False
    for part in relative_parts:
        candidate = current / part
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            if not create:
                current = candidate
                missing = True
                continue
            candidate.mkdir(mode=0o700)
            metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise CourseStorageError(f"{field} must not contain symlink components: {candidate}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise CourseStorageError(f"{field} must only contain directory components: {candidate}")
        if not missing:
            current = candidate.resolve(strict=True)
        else:
            current = candidate
    return current


def descendant_relative_path(
    root: Path,
    value: Path | str,
    *,
    field: str,
    expected_filename: str | None = None,
) -> str:
    path = Path(value)
    try:
        relative = path.relative_to(root)
    except ValueError as lexical_error:
        # macOS may expose the same trusted root through aliases such as
        # /var and /private/var. Find the shallowest existing prefix of the
        # supplied path that resolves to the configured root, then preserve
        # every descendant component so later symlink checks cannot be hidden.
        try:
            canonical_root = root.resolve(strict=True)
        except OSError as exc:
            raise CourseStorageError(f"{field} is outside the configured root") from exc
        alias_root: Path | None = None
        for prefix in reversed(path.parents):
            try:
                if prefix.resolve(strict=True) == canonical_root:
                    alias_root = prefix
                    break
            except OSError:
                continue
        if alias_root is None:
            raise CourseStorageError(
                f"{field} is outside the configured root"
            ) from lexical_error
        relative = path.relative_to(alias_root)
    relative_parts = _validated_relative_parts(os.fspath(relative), field=field)
    leaf = _validated_leaf_name(relative_parts[-1], field=f"{field}.name")
    if expected_filename is not None and leaf != expected_filename:
        raise CourseStorageError(f"{field} does not match the expected filename")
    # Canonicalize the trusted root itself while continuing to reject a root
    # that is directly a symlink and every symlink below it.
    try:
        root_metadata = root.lstat()
    except FileNotFoundError:
        # Retry and terminal-recovery plans may reference transcript outputs
        # before the transcript root has ever been created.  The lexical
        # descendant and exact filename checks are still pinned in the plan;
        # existing components are checked once the root exists.
        return os.fspath(Path(*relative_parts))
    if stat.S_ISLNK(root_metadata.st_mode):
        raise CourseStorageError(f"{field}.root must not be a symlink: {root}")
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise CourseStorageError(f"{field}.root must be a directory: {root}")
    current = root.resolve(strict=True)
    for part in relative_parts[:-1]:
        candidate = current / part
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            current = candidate
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise CourseStorageError(f"{field} must not contain symlink components: {candidate}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise CourseStorageError(f"{field} must only contain directory components: {candidate}")
        current = candidate.resolve(strict=True)
    return os.fspath(Path(*relative_parts))
