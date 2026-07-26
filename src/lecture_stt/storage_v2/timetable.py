from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import unicodedata
from typing import Any, Mapping, Sequence

from lecture_stt.storage_v2.repository import (
    apply_migration,
    connect_v2,
    require_v2_schema,
)


_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_ROWS = 2_000
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_STORAGE_KEY_RE = re.compile(r"^[0-9A-Za-z_-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WEEKDAYS = {
    "mon": "mon",
    "monday": "mon",
    "월": "mon",
    "월요일": "mon",
    "tue": "tue",
    "tuesday": "tue",
    "화": "tue",
    "화요일": "tue",
    "wed": "wed",
    "wednesday": "wed",
    "수": "wed",
    "수요일": "wed",
    "thu": "thu",
    "thursday": "thu",
    "목": "thu",
    "목요일": "thu",
    "fri": "fri",
    "friday": "fri",
    "금": "fri",
    "금요일": "fri",
    "sat": "sat",
    "saturday": "sat",
    "토": "sat",
    "토요일": "sat",
    "sun": "sun",
    "sunday": "sun",
    "일": "sun",
    "일요일": "sun",
}
_PYTHON_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_FIELD_ALIASES = {
    "semester": ("semester", "학기"),
    "course_name": ("course_name", "course", "과목명", "수업명"),
    "course_code": ("course_code", "과목코드"),
    "weekday": ("weekday", "day", "요일"),
    "start_time": ("start_time", "시작시간", "시작"),
    "end_time": ("end_time", "종료시간", "종료"),
    "period": ("period", "period_label", "교시"),
    "classroom": ("classroom", "강의실"),
}
_KNOWN_FIELDS = frozenset(
    alias for aliases in _FIELD_ALIASES.values() for alias in aliases
)


class TimetableError(RuntimeError):
    """Base error for timetable and recording classification operations."""


class TimetableNotFoundError(TimetableError):
    """The requested database row or source does not exist."""


class TimetableConflictError(TimetableError):
    """Persisted state conflicts with the requested guarded operation."""


class TimetableWriteDisabledError(TimetableError):
    """A timetable write was attempted without every required guard."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _nfc_text(
    value: Any,
    *,
    field: str,
    required: bool = True,
    max_length: int | None = None,
) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, (str, int)):
        raise ValueError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", str(value).strip())
    if not normalized:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if max_length is not None and len(normalized) > max_length:
        raise ValueError(f"{field} exceeds the {max_length}-character limit")
    return normalized


def _row_value(
    row: Mapping[str, Any],
    field: str,
    *,
    required: bool = True,
) -> Any:
    present = [
        alias
        for alias in _FIELD_ALIASES[field]
        if alias in row and row[alias] not in (None, "")
    ]
    if len(present) > 1:
        values = {str(row[alias]).strip() for alias in present}
        if len(values) > 1:
            raise ValueError(
                f"Conflicting aliases for {field}: {', '.join(present)}"
            )
    if not present:
        if required:
            raise ValueError(f"{field} is required")
        return None
    return row[present[0]]


def _normalize_weekday(value: Any) -> str:
    normalized = _nfc_text(value, field="weekday")
    assert normalized is not None
    result = _WEEKDAYS.get(normalized.lower())
    if result is None:
        raise ValueError(f"Unsupported weekday: {normalized}")
    return result


def _normalize_time(value: Any, *, field: str) -> str:
    normalized = _nfc_text(value, field=field)
    assert normalized is not None
    if not _TIME_RE.fullmatch(normalized):
        raise ValueError(f"{field} must use 24-hour HH:MM format")
    return normalized


def _normalize_period(value: Any) -> tuple[str, int | None]:
    normalized = _nfc_text(value, field="period", max_length=128)
    assert normalized is not None
    match = re.fullmatch(r"(\d+)(?:\s*교시)?", normalized)
    if match:
        index = int(match.group(1))
        if index <= 0:
            raise ValueError("period index must be positive")
        return f"{index}교시", index
    return normalized, None


def _read_regular_source(path: Path | str) -> tuple[bytes, str]:
    source = Path(path).expanduser()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except FileNotFoundError as exc:
        raise TimetableNotFoundError("Timetable source is not available") from exc
    except OSError as exc:
        raise ValueError("Timetable source must be a regular non-symlink file") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Timetable source must be a regular file")
        if before.st_nlink != 1:
            raise ValueError("Timetable source must have exactly one hard link")
        if before.st_size <= 0:
            raise ValueError("Timetable source must not be empty")
        if before.st_size > _MAX_SOURCE_BYTES:
            raise ValueError("Timetable source exceeds the 2 MiB limit")
        chunks: list[bytes] = []
        remaining = _MAX_SOURCE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise TimetableConflictError("Timetable source changed while reading")
        if len(payload) != before.st_size:
            raise TimetableConflictError("Timetable source size changed while reading")
        return payload, hashlib.sha256(payload).hexdigest()
    finally:
        os.close(descriptor)


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"Non-finite JSON value is not allowed: {value}")


def _source_rows(
    source_path: Path | str,
) -> tuple[str, bytes, str, list[Mapping[str, Any]], str | None]:
    payload, source_sha256 = _read_regular_source(source_path)
    suffix = Path(source_path).suffix.lower()
    if suffix == ".csv":
        source_format = "csv"
        try:
            text = payload.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("Timetable CSV must be UTF-8") from exc
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if not reader.fieldnames:
            raise ValueError("Timetable CSV requires a header row")
        normalized_headers = [
            unicodedata.normalize("NFC", str(field).strip())
            for field in reader.fieldnames
        ]
        if len(normalized_headers) != len(set(normalized_headers)):
            raise ValueError("Timetable CSV header fields must be unique")
        unknown_headers = set(normalized_headers).difference(_KNOWN_FIELDS)
        if unknown_headers:
            raise ValueError(
                "Unsupported timetable CSV fields: "
                + ", ".join(sorted(unknown_headers))
            )
        rows = list(reader)
        global_semester = None
    elif suffix == ".json":
        source_format = "json"
        try:
            parsed = json.loads(
                payload.decode("utf-8-sig", errors="strict"),
                parse_constant=_reject_non_finite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Timetable JSON must be valid UTF-8 JSON") from exc
        if isinstance(parsed, list):
            rows = parsed
            global_semester = None
        elif isinstance(parsed, dict):
            unknown = set(parsed).difference({"schema_version", "semester", "entries"})
            if unknown:
                raise ValueError(
                    "Unsupported timetable JSON fields: "
                    + ", ".join(sorted(unknown))
                )
            rows = parsed.get("entries")
            global_semester = _nfc_text(
                parsed.get("semester"),
                field="semester",
                required=False,
                max_length=128,
            )
        else:
            raise ValueError("Timetable JSON must be an array or an entries object")
        if not isinstance(rows, list):
            raise ValueError("Timetable JSON entries must be an array")
    else:
        raise ValueError("Timetable source extension must be .csv or .json")
    if not 1 <= len(rows) <= _MAX_ROWS:
        raise ValueError(f"Timetable must contain between 1 and {_MAX_ROWS} rows")
    if not all(isinstance(row, Mapping) for row in rows):
        raise ValueError("Every timetable row must be an object")
    return source_format, payload, source_sha256, list(rows), global_semester


def _normalize_entry(
    raw_row: Mapping[str, Any],
    *,
    row_index: int,
    expected_semester: str | None,
    global_semester: str | None,
) -> dict[str, Any]:
    if None in raw_row and raw_row[None]:
        raise ValueError(
            f"Timetable CSV row {row_index} has more values than header fields"
        )
    normalized_row = {
        unicodedata.normalize("NFC", str(key).strip()): value
        for key, value in raw_row.items()
        if key is not None
    }
    unknown = set(normalized_row).difference(_KNOWN_FIELDS)
    if unknown:
        raise ValueError(
            f"Unsupported timetable fields at row {row_index}: "
            + ", ".join(sorted(unknown))
        )
    row_semester = _nfc_text(
        _row_value(normalized_row, "semester", required=False),
        field="semester",
        required=False,
        max_length=128,
    )
    semester = row_semester or global_semester or expected_semester
    if semester is None:
        raise ValueError(f"semester is required at row {row_index}")
    if expected_semester is not None and semester != expected_semester:
        raise ValueError(
            f"semester mismatch at row {row_index}: "
            f"expected {expected_semester}, found {semester}"
        )
    course_name = _nfc_text(
        _row_value(normalized_row, "course_name"),
        field="course_name",
        max_length=256,
    )
    course_code = _nfc_text(
        _row_value(normalized_row, "course_code", required=False),
        field="course_code",
        required=False,
        max_length=128,
    )
    weekday = _normalize_weekday(_row_value(normalized_row, "weekday"))
    start_time = _normalize_time(
        _row_value(normalized_row, "start_time"),
        field="start_time",
    )
    end_time = _normalize_time(
        _row_value(normalized_row, "end_time"),
        field="end_time",
    )
    if start_time >= end_time:
        raise ValueError(f"end_time must be after start_time at row {row_index}")
    period_label, period_index = _normalize_period(
        _row_value(normalized_row, "period")
    )
    classroom = _nfc_text(
        _row_value(normalized_row, "classroom"),
        field="classroom",
        max_length=256,
    )
    assert course_name is not None and classroom is not None
    identity = {
        "semester": semester,
        "course_name": course_name,
        "course_code": course_code,
        "weekday": weekday,
        "start_time": start_time,
        "end_time": end_time,
        "period_label": period_label,
        "period_index": period_index,
        "classroom": classroom,
    }
    return {
        "row_index": row_index,
        "entry_key": _sha256_json(identity),
        **identity,
    }


def plan_timetable_import(
    source_path: Path | str,
    *,
    semester: str | None = None,
) -> dict[str, Any]:
    """Build a metadata-only deterministic plan from one local CSV or JSON file."""

    expected_semester = _nfc_text(
        semester,
        field="semester",
        required=False,
        max_length=128,
    )
    source_format, _payload, source_sha256, rows, global_semester = _source_rows(
        source_path
    )
    entries = [
        _normalize_entry(
            row,
            row_index=index,
            expected_semester=expected_semester,
            global_semester=global_semester,
        )
        for index, row in enumerate(rows, start=1)
    ]
    semesters = {str(entry["semester"]) for entry in entries}
    if len(semesters) != 1:
        raise ValueError("One timetable import must contain exactly one semester")
    entry_keys = [str(entry["entry_key"]) for entry in entries]
    if len(entry_keys) != len(set(entry_keys)):
        raise ValueError("Timetable contains duplicate normalized entries")
    entries_identity = [
        {
            key: entry[key]
            for key in (
                "entry_key",
                "semester",
                "course_name",
                "course_code",
                "weekday",
                "start_time",
                "end_time",
                "period_label",
                "period_index",
                "classroom",
            )
        }
        for entry in sorted(entries, key=lambda item: str(item["entry_key"]))
    ]
    digest_payload = {
        "schema_version": "storage-v2/timetable-import-plan@1",
        "semester": next(iter(semesters)),
        "source_format": source_format,
        "source_sha256": source_sha256,
        "entries_sha256": _sha256_json(entries_identity),
        "expected_count": len(entries),
        "entries": entries,
    }
    return {
        **digest_payload,
        "mode": "read_only",
        "plan_sha256": _sha256_json(digest_payload),
    }


def _validate_apply_guards(
    plan: Mapping[str, Any],
    *,
    expected_count: int,
    expected_plan_sha256: str,
    allow_write: bool,
    operation: str,
) -> None:
    if not allow_write:
        raise TimetableWriteDisabledError(
            f"{operation} requires allow_write=True"
        )
    if isinstance(expected_count, bool) or expected_count != plan["expected_count"]:
        raise TimetableConflictError(
            f"{operation} expected_count does not match the current plan"
        )
    normalized_digest = str(expected_plan_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized_digest):
        raise ValueError("expected_plan_sha256 must be a canonical SHA-256")
    if normalized_digest != plan["plan_sha256"]:
        raise TimetableConflictError(
            f"{operation} expected_plan_sha256 does not match the current plan"
        )


def _verify_existing_import(
    conn: sqlite3.Connection,
    import_id: int,
    plan: Mapping[str, Any],
) -> None:
    rows = conn.execute(
        """
        SELECT
            row_index,
            entry_key,
            semester,
            course_name,
            course_code,
            weekday,
            start_time,
            end_time,
            period_label,
            period_index,
            classroom
        FROM schedule_entries
        WHERE schedule_import_id = ?
        ORDER BY row_index
        """,
        (import_id,),
    ).fetchall()
    actual = [dict(row) for row in rows]
    expected = [
        {
            key: entry[key]
            for key in (
                "row_index",
                "entry_key",
                "semester",
                "course_name",
                "course_code",
                "weekday",
                "start_time",
                "end_time",
                "period_label",
                "period_index",
                "classroom",
            )
        }
        for entry in plan["entries"]
    ]
    if {
        _canonical_json({k: row[k] for k in row if k != "row_index"})
        for row in actual
    } != {
        _canonical_json({k: row[k] for k in row if k != "row_index"})
        for row in expected
    }:
        raise TimetableConflictError(
            "Existing timetable import does not match its canonical entry digest"
        )


def apply_timetable_import(
    source_path: Path | str,
    db_path: Path | str,
    *,
    semester: str | None = None,
    expected_count: int,
    expected_plan_sha256: str,
    allow_write: bool = False,
) -> dict[str, Any]:
    """Apply one guarded, idempotent timetable import to a v2 database."""

    plan = plan_timetable_import(source_path, semester=semester)
    _validate_apply_guards(
        plan,
        expected_count=expected_count,
        expected_plan_sha256=expected_plan_sha256,
        allow_write=allow_write,
        operation="Timetable import",
    )
    conn = apply_migration(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT id, semester, entries_sha256, row_count
            FROM schedule_imports
            WHERE semester = ? AND entries_sha256 = ?
            """,
            (plan["semester"], plan["entries_sha256"]),
        ).fetchone()
        current_selection = conn.execute(
            """
            SELECT schedule_import_id
            FROM schedule_semester_selections
            WHERE semester = ?
            """,
            (plan["semester"],),
        ).fetchone()
        if existing is not None:
            import_id = int(existing["id"])
            if (
                str(existing["semester"]) != str(plan["semester"])
                or str(existing["entries_sha256"]) != str(plan["entries_sha256"])
                or int(existing["row_count"]) != int(plan["expected_count"])
            ):
                raise TimetableConflictError(
                    "Existing timetable import metadata does not match its entries"
                )
            _verify_existing_import(conn, import_id, plan)
            if (
                current_selection is not None
                and int(current_selection["schedule_import_id"]) == import_id
            ):
                action = "skipped"
            else:
                conn.execute(
                    """
                    INSERT INTO schedule_semester_selections(
                        semester,
                        schedule_import_id,
                        selection_plan_sha256,
                        selected_at
                    )
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(semester) DO UPDATE SET
                        schedule_import_id = excluded.schedule_import_id,
                        selection_plan_sha256 = excluded.selection_plan_sha256,
                        selected_at = CURRENT_TIMESTAMP
                    """,
                    (plan["semester"], import_id, plan["plan_sha256"]),
                )
                action = "selected"
            conn.commit()
        else:
            cursor = conn.execute(
                """
                INSERT INTO schedule_imports(
                    semester,
                    source_format,
                    source_sha256,
                    entries_sha256,
                    row_count
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    plan["semester"],
                    plan["source_format"],
                    plan["source_sha256"],
                    plan["entries_sha256"],
                    plan["expected_count"],
                ),
            )
            import_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO schedule_entries(
                    schedule_import_id,
                    row_index,
                    entry_key,
                    semester,
                    course_name,
                    course_code,
                    weekday,
                    start_time,
                    end_time,
                    period_label,
                    period_index,
                    classroom
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        import_id,
                        entry["row_index"],
                        entry["entry_key"],
                        entry["semester"],
                        entry["course_name"],
                        entry["course_code"],
                        entry["weekday"],
                        entry["start_time"],
                        entry["end_time"],
                        entry["period_label"],
                        entry["period_index"],
                        entry["classroom"],
                    )
                    for entry in plan["entries"]
                ],
            )
            conn.execute(
                """
                INSERT INTO schedule_semester_selections(
                    semester,
                    schedule_import_id,
                    selection_plan_sha256,
                    selected_at
                )
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(semester) DO UPDATE SET
                    schedule_import_id = excluded.schedule_import_id,
                    selection_plan_sha256 = excluded.selection_plan_sha256,
                    selected_at = CURRENT_TIMESTAMP
                """,
                (plan["semester"], import_id, plan["plan_sha256"]),
            )
            conn.commit()
            action = "imported"
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "schema_version": "storage-v2/timetable-import-result@1",
        "ok": True,
        "action": action,
        "import_id": import_id,
        "semester": plan["semester"],
        "count": plan["expected_count"],
        "plan_sha256": plan["plan_sha256"],
    }


def _open_readonly(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    if not path.exists():
        raise TimetableNotFoundError("Storage v2 database is not available")
    conn = connect_v2(path, readonly=True)
    try:
        require_v2_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _validate_pagination(limit: int, offset: int) -> tuple[int, int]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 200:
        raise ValueError("limit must be between 0 and 200")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= 100_000
    ):
        raise ValueError("offset must be between 0 and 100000")
    return limit, offset


def list_timetable_entries(
    db_path: Path | str,
    *,
    semester: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Read an on-demand, metadata-only timetable snapshot."""

    limit, offset = _validate_pagination(limit, offset)
    normalized_semester = _nfc_text(
        semester,
        field="semester",
        required=False,
        max_length=128,
    )
    path = Path(db_path).expanduser()
    if not path.exists():
        return {
            "schema_version": "storage-v2/timetable-list@1",
            "available": False,
            "semester": normalized_semester,
            "total": 0,
            "entries": [],
        }
    conn = _open_readonly(path)
    try:
        where_sql = (
            "WHERE entry.semester = ?" if normalized_semester else ""
        )
        base_params: tuple[Any, ...] = (
            (normalized_semester,) if normalized_semester else ()
        )
        total = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM schedule_entries AS entry
                JOIN schedule_semester_selections AS selection
                  ON selection.schedule_import_id = entry.schedule_import_id
                 AND selection.semester = entry.semester
                {where_sql}
                """,
                base_params,
            ).fetchone()[0]
        )
        rows = conn.execute(
            f"""
            SELECT
                entry.id,
                entry.entry_key,
                entry.semester,
                entry.course_name,
                entry.course_code,
                entry.weekday,
                entry.start_time,
                entry.end_time,
                entry.period_label,
                entry.period_index,
                entry.classroom
            FROM schedule_entries AS entry
            JOIN schedule_semester_selections AS selection
              ON selection.schedule_import_id = entry.schedule_import_id
             AND selection.semester = entry.semester
            {where_sql}
            ORDER BY
                entry.semester DESC,
                CASE entry.weekday
                    WHEN 'mon' THEN 1 WHEN 'tue' THEN 2 WHEN 'wed' THEN 3
                    WHEN 'thu' THEN 4 WHEN 'fri' THEN 5 WHEN 'sat' THEN 6
                    ELSE 7
                END,
                entry.start_time,
                entry.id
            LIMIT ? OFFSET ?
            """,
            (*base_params, limit, offset),
        ).fetchall()
    finally:
        conn.close()
    return {
        "schema_version": "storage-v2/timetable-list@1",
        "available": True,
        "semester": normalized_semester,
        "total": total,
        "entries": [dict(row) for row in rows],
    }


def _minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _parse_recorded_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _classification_plan_from_connection(
    conn: sqlite3.Connection,
    *,
    semester: str,
    storage_keys: Sequence[str] | None,
    margin_minutes: int,
) -> dict[str, Any]:
    if isinstance(margin_minutes, bool) or not 0 <= margin_minutes <= 120:
        raise ValueError("margin_minutes must be between 0 and 120")
    params: list[Any] = []
    where = ["archived_at IS NULL"]
    normalized_keys: list[str] | None = None
    if storage_keys is not None:
        normalized_keys = []
        for storage_key in storage_keys:
            normalized = str(storage_key or "").strip()
            if not _STORAGE_KEY_RE.fullmatch(normalized):
                raise ValueError(f"Invalid storage_key: {normalized}")
            normalized_keys.append(normalized)
        if len(normalized_keys) != len(set(normalized_keys)):
            raise ValueError("storage_keys must not contain duplicates")
        if not normalized_keys:
            recordings: list[sqlite3.Row] = []
        else:
            placeholders = ",".join("?" for _ in normalized_keys)
            where.append(f"storage_key IN ({placeholders})")
            params.extend(normalized_keys)
            recordings = conn.execute(
                f"""
                SELECT id, storage_key, original_name_nfc, recorded_at
                FROM recordings
                WHERE {' AND '.join(where)}
                ORDER BY storage_key
                """,
                tuple(params),
            ).fetchall()
    else:
        recordings = conn.execute(
            """
            SELECT id, storage_key, original_name_nfc, recorded_at
            FROM recordings
            WHERE archived_at IS NULL
            ORDER BY storage_key
            """
        ).fetchall()
    if normalized_keys is not None:
        found = {str(row["storage_key"]) for row in recordings}
        missing = sorted(set(normalized_keys).difference(found))
        if missing:
            raise TimetableNotFoundError(
                "Recording not found: " + ", ".join(missing)
            )
    schedule_rows = conn.execute(
        """
        SELECT
            entry.id,
            entry.entry_key,
            entry.semester,
            entry.course_name,
            entry.course_code,
            entry.weekday,
            entry.start_time,
            entry.end_time,
            entry.period_label,
            entry.period_index,
            entry.classroom
        FROM schedule_entries AS entry
        JOIN schedule_semester_selections AS selection
          ON selection.schedule_import_id = entry.schedule_import_id
         AND selection.semester = entry.semester
        WHERE entry.semester = ?
        ORDER BY entry.weekday, entry.start_time, entry.id
        """,
        (semester,),
    ).fetchall()
    entries = [dict(row) for row in schedule_rows]
    cases: list[dict[str, Any]] = []
    for recording in recordings:
        recorded_at = _parse_recorded_at(recording["recorded_at"])
        if recording["recorded_at"] is None:
            reason = "recorded_at_missing"
            candidates: list[dict[str, Any]] = []
        elif recorded_at is None:
            reason = "recorded_at_invalid"
            candidates = []
        else:
            weekday = _PYTHON_WEEKDAYS[recorded_at.weekday()]
            recorded_minutes = recorded_at.hour * 60 + recorded_at.minute
            candidates = [
                entry
                for entry in entries
                if entry["weekday"] == weekday
                and _minutes(str(entry["start_time"])) - margin_minutes
                <= recorded_minutes
                <= _minutes(str(entry["end_time"])) + margin_minutes
            ]
            reason = (
                "unique_time_match"
                if len(candidates) == 1
                else "no_time_match"
                if not candidates
                else "ambiguous_time_match"
            )
        session_date = recorded_at.date().isoformat() if recorded_at else None
        if len(candidates) == 1 and session_date is not None:
            candidate = candidates[0]
            proposed_title = " ".join(
                part
                for part in (
                    session_date,
                    str(candidate["course_name"]),
                    str(candidate["period_label"] or ""),
                )
                if part
            )
            confidence = 0.75
        else:
            candidate = None
            proposed_title = " ".join(
                part
                for part in (session_date, "분류 확인 필요")
                if part
            )
            confidence = None
        cases.append(
            {
                "recording_id": int(recording["id"]),
                "storage_key": str(recording["storage_key"]),
                "recorded_at": recording["recorded_at"],
                "classification_reason": reason,
                "suggestion_status": (
                    "suggested" if candidate is not None else "needs_review"
                ),
                "proposed_title": proposed_title,
                "confidence": confidence,
                "session_date": session_date,
                "selected_entry": candidate,
                "candidate_entry_keys": [
                    str(item["entry_key"]) for item in candidates
                ],
            }
        )
    digest_payload = {
        "schema_version": "storage-v2/timetable-classification-plan@1",
        "semester": semester,
        "margin_minutes": margin_minutes,
        "storage_keys": normalized_keys,
        "expected_count": len(cases),
        "cases": cases,
    }
    return {
        **digest_payload,
        "mode": "read_only",
        "plan_sha256": _sha256_json(digest_payload),
    }


def plan_recording_classifications(
    db_path: Path | str,
    *,
    semester: str,
    storage_keys: Sequence[str] | None = None,
    margin_minutes: int = 30,
) -> dict[str, Any]:
    """Plan conservative schedule matches without changing canonical metadata."""

    normalized_semester = _nfc_text(
        semester,
        field="semester",
        max_length=128,
    )
    assert normalized_semester is not None
    conn = _open_readonly(db_path)
    try:
        return _classification_plan_from_connection(
            conn,
            semester=normalized_semester,
            storage_keys=storage_keys,
            margin_minutes=margin_minutes,
        )
    finally:
        conn.close()


def _proposal_values(case: Mapping[str, Any], semester: str) -> dict[str, Any]:
    entry = case["selected_entry"]
    if entry is None:
        return {
            "schedule_entry_id": None,
            "context_type": "general",
            "label": None,
            "semester": semester,
            "course_name": None,
            "course_code": None,
            "session_date": case["session_date"],
            "weekday": None,
            "start_time": None,
            "end_time": None,
            "period_label": None,
            "period_index": None,
            "classroom": None,
        }
    return {
        "schedule_entry_id": int(entry["id"]),
        "context_type": "class_session",
        "label": entry["course_name"],
        "semester": semester,
        "course_name": entry["course_name"],
        "course_code": entry["course_code"],
        "session_date": case["session_date"],
        "weekday": entry["weekday"],
        "start_time": entry["start_time"],
        "end_time": entry["end_time"],
        "period_label": entry["period_label"],
        "period_index": entry["period_index"],
        "classroom": entry["classroom"],
    }


def apply_recording_classifications(
    db_path: Path | str,
    *,
    semester: str,
    expected_count: int,
    expected_plan_sha256: str,
    storage_keys: Sequence[str] | None = None,
    margin_minutes: int = 30,
    allow_write: bool = False,
) -> dict[str, Any]:
    """Persist suggestions and reviews, never canonical title/context selections."""

    normalized_semester = _nfc_text(
        semester,
        field="semester",
        max_length=128,
    )
    assert normalized_semester is not None
    path = Path(db_path).expanduser()
    if not path.exists():
        raise TimetableNotFoundError("Storage v2 database is not available")
    if not allow_write:
        raise TimetableWriteDisabledError(
            "Timetable classification requires allow_write=True"
        )
    readonly_conn = _open_readonly(path)
    try:
        readonly_plan = _classification_plan_from_connection(
            readonly_conn,
            semester=normalized_semester,
            storage_keys=storage_keys,
            margin_minutes=margin_minutes,
        )
        _validate_apply_guards(
            readonly_plan,
            expected_count=expected_count,
            expected_plan_sha256=expected_plan_sha256,
            allow_write=allow_write,
            operation="Timetable classification",
        )
    finally:
        readonly_conn.close()
    conn = connect_v2(path)
    try:
        require_v2_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        plan = _classification_plan_from_connection(
            conn,
            semester=normalized_semester,
            storage_keys=storage_keys,
            margin_minutes=margin_minutes,
        )
        _validate_apply_guards(
            plan,
            expected_count=expected_count,
            expected_plan_sha256=expected_plan_sha256,
            allow_write=allow_write,
            operation="Timetable classification",
        )
        imported = 0
        skipped = 0
        for case in plan["cases"]:
            detail = {
                "schema_version": "storage-v2/timetable-classification-review@1",
                "semester": normalized_semester,
                "classification_reason": case["classification_reason"],
                "candidate_entry_keys": case["candidate_entry_keys"],
            }
            values = _proposal_values(case, normalized_semester)
            existing = conn.execute(
                """
                SELECT *
                FROM recording_classification_proposals
                WHERE recording_id = ?
                  AND semester = ?
                  AND status IN ('suggested', 'confirmed')
                """,
                (case["recording_id"], normalized_semester),
            ).fetchone()
            expected_fields = {
                **values,
                "classification_reason": case["classification_reason"],
                "proposed_title": case["proposed_title"],
                "confidence": case["confidence"],
                "detail_json": _canonical_json(detail),
            }
            if existing is not None:
                mismatched = [
                    field
                    for field, expected in expected_fields.items()
                    if existing[field] != expected
                ]
                if mismatched:
                    raise TimetableConflictError(
                        "Existing classification proposal differs for "
                        f"{case['storage_key']}: {', '.join(sorted(mismatched))}"
                    )
                skipped += 1
                continue
            review_reason = (
                "schedule_classification_suggested"
                if case["classification_reason"] == "unique_time_match"
                else f"schedule_classification_{case['classification_reason']}"
            )
            cursor = conn.execute(
                """
                INSERT INTO review_items(
                    recording_id,
                    status,
                    severity,
                    reason_code,
                    detail_json
                )
                VALUES (?, 'open', 'medium', ?, ?)
                """,
                (
                    case["recording_id"],
                    review_reason,
                    _canonical_json(detail),
                ),
            )
            review_item_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO recording_classification_proposals(
                    recording_id,
                    schedule_entry_id,
                    review_item_id,
                    status,
                    classification_reason,
                    proposed_title,
                    context_type,
                    label,
                    semester,
                    course_name,
                    course_code,
                    session_date,
                    weekday,
                    start_time,
                    end_time,
                    period_label,
                    period_index,
                    classroom,
                    confidence,
                    detail_json
                )
                VALUES (
                    ?, ?, ?, 'suggested', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    case["recording_id"],
                    values["schedule_entry_id"],
                    review_item_id,
                    case["classification_reason"],
                    case["proposed_title"],
                    values["context_type"],
                    values["label"],
                    values["semester"],
                    values["course_name"],
                    values["course_code"],
                    values["session_date"],
                    values["weekday"],
                    values["start_time"],
                    values["end_time"],
                    values["period_label"],
                    values["period_index"],
                    values["classroom"],
                    case["confidence"],
                    _canonical_json(detail),
                ),
            )
            imported += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "schema_version": "storage-v2/timetable-classification-result@1",
        "ok": True,
        "semester": normalized_semester,
        "count": expected_count,
        "suggested": imported,
        "skipped": skipped,
        "plan_sha256": expected_plan_sha256.lower(),
        "canonical_metadata_changed": False,
    }


def list_classification_proposals(
    db_path: Path | str,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Return a bounded timetable classification review queue."""

    limit, offset = _validate_pagination(limit, offset)
    normalized_status = str(status or "").strip().lower() or None
    if normalized_status not in {None, "suggested", "confirmed", "rejected"}:
        raise ValueError("status must be suggested, confirmed, or rejected")
    path = Path(db_path).expanduser()
    if not path.exists():
        return {
            "schema_version": "storage-v2/classification-proposal-list@1",
            "available": False,
            "counts": {"suggested": 0, "confirmed": 0, "rejected": 0},
            "total": 0,
            "proposals": [],
        }
    conn = _open_readonly(path)
    try:
        counts = {"suggested": 0, "confirmed": 0, "rejected": 0}
        for row in conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM recording_classification_proposals
            GROUP BY status
            """
        ).fetchall():
            counts[str(row["status"])] = int(row["count"])
        if normalized_status is None:
            where_sql = ""
            params: tuple[Any, ...] = (limit, offset)
            total = sum(counts.values())
        else:
            where_sql = "WHERE proposal.status = ?"
            params = (normalized_status, limit, offset)
            total = counts[normalized_status]
        rows = conn.execute(
            f"""
            SELECT
                proposal.id,
                recording.storage_key,
                proposal.status,
                proposal.classification_reason,
                proposal.proposed_title,
                proposal.context_type,
                proposal.semester,
                proposal.course_name,
                proposal.course_code,
                proposal.session_date,
                proposal.weekday,
                proposal.start_time,
                proposal.end_time,
                proposal.period_label,
                proposal.period_index,
                proposal.classroom,
                proposal.confidence,
                review.status AS review_status,
                proposal.created_at,
                proposal.updated_at,
                proposal.confirmed_at
            FROM recording_classification_proposals AS proposal
            JOIN recordings AS recording ON recording.id = proposal.recording_id
            LEFT JOIN review_items AS review ON review.id = proposal.review_item_id
            {where_sql}
            ORDER BY
                CASE proposal.status
                    WHEN 'suggested' THEN 0
                    WHEN 'confirmed' THEN 1
                    ELSE 2
                END,
                proposal.created_at DESC,
                proposal.id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return {
        "schema_version": "storage-v2/classification-proposal-list@1",
        "available": True,
        "counts": counts,
        "total": total,
        "proposals": [dict(row) for row in rows],
    }


def _proposal_row(conn: sqlite3.Connection, proposal_id: int) -> sqlite3.Row:
    if isinstance(proposal_id, bool) or not isinstance(proposal_id, int) or proposal_id <= 0:
        raise ValueError("proposal_id must be a positive integer")
    row = conn.execute(
        """
        SELECT
            proposal.*,
            recording.storage_key,
            recording.recorded_at,
            review.status AS review_status,
            review.reason_code AS review_reason_code,
            entry.schedule_import_id AS proposal_schedule_import_id,
            selection.schedule_import_id AS active_schedule_import_id,
            EXISTS (
                SELECT 1
                FROM recording_classification_materializations AS materialization
                WHERE materialization.proposal_id = proposal.id
                  AND materialization.state = 'applied'
            ) AS canonical_metadata_changed
        FROM recording_classification_proposals AS proposal
        JOIN recordings AS recording ON recording.id = proposal.recording_id
        LEFT JOIN review_items AS review ON review.id = proposal.review_item_id
        LEFT JOIN schedule_entries AS entry
          ON entry.id = proposal.schedule_entry_id
        LEFT JOIN schedule_semester_selections AS selection
          ON selection.semester = proposal.semester
        WHERE proposal.id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise TimetableNotFoundError(
            f"Classification proposal not found: {proposal_id}"
        )
    return row


def read_classification_proposal(
    db_path: Path | str,
    proposal_id: int,
) -> dict[str, Any]:
    conn = _open_readonly(db_path)
    try:
        row = _proposal_row(conn, proposal_id)
        detail = json.loads(row["detail_json"]) if row["detail_json"] else {}
        return {
            "schema_version": "storage-v2/classification-proposal-detail@1",
            "proposal": {
                key: row[key]
                for key in (
                    "id",
                    "storage_key",
                    "status",
                    "classification_reason",
                    "proposed_title",
                    "context_type",
                    "label",
                    "semester",
                    "course_name",
                    "course_code",
                    "session_date",
                    "weekday",
                    "start_time",
                    "end_time",
                    "period_label",
                    "period_index",
                    "classroom",
                    "confidence",
                    "review_status",
                    "review_reason_code",
                    "created_at",
                    "updated_at",
                    "confirmed_at",
                )
            },
            "candidate_entry_keys": detail.get("candidate_entry_keys", []),
            "canonical_metadata_changed": bool(
                row["canonical_metadata_changed"]
            ),
        }
    finally:
        conn.close()


def update_classification_status(
    db_path: Path | str,
    proposal_id: int,
    *,
    status: str,
    status_writes_enabled: bool = False,
    allow_write: bool = False,
) -> dict[str, Any]:
    """Apply an explicit non-canonical review status transition."""

    normalized_status = str(status or "").strip().lower()
    if normalized_status != "rejected":
        raise ValueError("status must be rejected")
    if not status_writes_enabled:
        raise TimetableWriteDisabledError(
            "Timetable classification status writes are disabled"
        )
    if not allow_write:
        raise TimetableWriteDisabledError(
            "Classification status update requires allow_write=True"
        )
    path = Path(db_path).expanduser()
    if not path.exists():
        raise TimetableNotFoundError("Storage v2 database is not available")
    conn = connect_v2(path)
    try:
        require_v2_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = _proposal_row(conn, proposal_id)
        current_status = str(row["status"])
        review_status = str(row["review_status"])
        if current_status == "rejected":
            if review_status == "dismissed":
                conn.commit()
                return {
                    "schema_version": "storage-v2/classification-status-result@1",
                    "ok": True,
                    "action": "skipped",
                    "proposal_id": proposal_id,
                    "status": "rejected",
                    "review_status": "dismissed",
                    "canonical_metadata_changed": False,
                }
            raise TimetableConflictError(
                "Rejected classification proposal review is not dismissed"
            )
        if current_status != "suggested":
            raise TimetableConflictError(
                "Only suggested proposals can be rejected"
            )
        if review_status not in {"open", "triaged"}:
            raise TimetableConflictError(
                "Classification proposal review is not open"
            )
        cursor = conn.execute(
            """
            UPDATE recording_classification_proposals
            SET status = 'rejected'
            WHERE id = ? AND status = 'suggested'
            """,
            (proposal_id,),
        )
        if cursor.rowcount != 1:
            raise TimetableConflictError(
                "Classification proposal status changed concurrently"
            )
        review_cursor = conn.execute(
            """
            UPDATE review_items
            SET status = 'dismissed',
                resolved_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status IN ('open', 'triaged')
            """,
            (int(row["review_item_id"]),),
        )
        if review_cursor.rowcount != 1:
            raise TimetableConflictError(
                "Classification review status changed concurrently"
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "schema_version": "storage-v2/classification-status-result@1",
        "ok": True,
        "action": "rejected",
        "proposal_id": proposal_id,
        "status": "rejected",
        "review_status": "dismissed",
        "canonical_metadata_changed": False,
    }


def _confirmation_plan_from_connection(
    conn: sqlite3.Connection,
    proposal_id: int,
) -> dict[str, Any]:
    row = _proposal_row(conn, proposal_id)
    if str(row["status"]) != "suggested":
        raise TimetableConflictError("Only suggested proposals can be confirmed")
    if str(row["classification_reason"]) != "unique_time_match":
        raise TimetableConflictError(
            "Unresolved timetable proposals cannot be confirmed"
        )
    if row["schedule_entry_id"] is None:
        raise TimetableConflictError(
            "Classification proposal has no timetable entry"
        )
    if (
        row["proposal_schedule_import_id"] is None
        or row["active_schedule_import_id"] is None
        or int(row["proposal_schedule_import_id"])
        != int(row["active_schedule_import_id"])
    ):
        raise TimetableConflictError(
            "Timetable changed after this classification was suggested"
        )
    if str(row["review_status"]) not in {"open", "triaged"}:
        raise TimetableConflictError(
            "Classification proposal review is not open"
        )
    digest_payload = {
        "schema_version": "storage-v2/classification-confirmation-plan@1",
        "proposal_id": int(row["id"]),
        "storage_key": str(row["storage_key"]),
        "proposal_updated_at": str(row["updated_at"]),
        "review_status": str(row["review_status"]),
        "semester": str(row["semester"]),
        "course_name": str(row["course_name"]),
        "session_date": str(row["session_date"]),
        "period_label": row["period_label"],
        "proposed_title": str(row["proposed_title"]),
        "expected_count": 1,
        "materialization": "confirmation_audit_only",
    }
    return {
        **digest_payload,
        "mode": "read_only",
        "plan_sha256": _sha256_json(digest_payload),
        "canonical_metadata_changed": False,
    }


def plan_classification_confirmation(
    db_path: Path | str,
    proposal_id: int,
) -> dict[str, Any]:
    conn = _open_readonly(db_path)
    try:
        return _confirmation_plan_from_connection(conn, proposal_id)
    finally:
        conn.close()


def apply_classification_confirmation(
    db_path: Path | str,
    proposal_id: int,
    *,
    expected_count: int,
    expected_plan_sha256: str,
    confirmations_enabled: bool = False,
    allow_write: bool = False,
) -> dict[str, Any]:
    """Confirm the reviewed classification decision without rewriting a manifest."""

    if not confirmations_enabled:
        raise TimetableWriteDisabledError(
            "Timetable classification confirmations are disabled"
        )
    if not allow_write:
        raise TimetableWriteDisabledError(
            "Classification confirmation requires allow_write=True"
        )
    if isinstance(expected_count, bool) or expected_count != 1:
        raise TimetableConflictError(
            "Classification confirmation expected_count must equal 1"
        )
    normalized_digest = str(expected_plan_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized_digest):
        raise ValueError("expected_plan_sha256 must be a canonical SHA-256")
    path = Path(db_path).expanduser()
    if not path.exists():
        raise TimetableNotFoundError("Storage v2 database is not available")
    readonly_conn = _open_readonly(path)
    try:
        readonly_row = _proposal_row(readonly_conn, proposal_id)
        if str(readonly_row["status"]) == "confirmed":
            if (
                readonly_row["confirmation_plan_sha256"] == normalized_digest
                and str(readonly_row["review_status"]) == "resolved"
                and readonly_row["confirmed_at"] is not None
            ):
                return {
                    "schema_version": "storage-v2/classification-confirmation-result@1",
                    "ok": True,
                    "action": "skipped",
                    "proposal_id": proposal_id,
                    "status": "confirmed",
                    "plan_sha256": normalized_digest,
                    "canonical_metadata_changed": False,
                }
            raise TimetableConflictError(
                "Classification proposal is already confirmed by another plan"
            )
        readonly_plan = _confirmation_plan_from_connection(
            readonly_conn,
            proposal_id,
        )
        _validate_apply_guards(
            readonly_plan,
            expected_count=expected_count,
            expected_plan_sha256=normalized_digest,
            allow_write=allow_write,
            operation="Classification confirmation",
        )
    finally:
        readonly_conn.close()
    conn = connect_v2(path)
    try:
        require_v2_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = _proposal_row(conn, proposal_id)
        if str(row["status"]) == "confirmed":
            if (
                row["confirmation_plan_sha256"] == normalized_digest
                and expected_count == 1
                and str(row["review_status"]) == "resolved"
                and row["confirmed_at"] is not None
            ):
                conn.commit()
                return {
                    "schema_version": "storage-v2/classification-confirmation-result@1",
                    "ok": True,
                    "action": "skipped",
                    "proposal_id": proposal_id,
                    "status": "confirmed",
                    "plan_sha256": normalized_digest,
                    "canonical_metadata_changed": False,
                }
            raise TimetableConflictError(
                "Classification proposal is already confirmed by another plan"
            )
        plan = _confirmation_plan_from_connection(conn, proposal_id)
        _validate_apply_guards(
            plan,
            expected_count=expected_count,
            expected_plan_sha256=normalized_digest,
            allow_write=allow_write,
            operation="Classification confirmation",
        )
        conn.execute(
            """
            UPDATE recording_classification_proposals
            SET status = 'confirmed',
                confirmation_plan_sha256 = ?,
                confirmed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'suggested'
            """,
            (normalized_digest, proposal_id),
        )
        if row["review_item_id"] is not None:
            conn.execute(
                """
                UPDATE review_items
                SET status = 'resolved',
                    resolved_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status IN ('open', 'triaged')
                """,
                (int(row["review_item_id"]),),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "schema_version": "storage-v2/classification-confirmation-result@1",
        "ok": True,
        "action": "confirmed",
        "proposal_id": proposal_id,
        "status": "confirmed",
        "plan_sha256": normalized_digest,
        "canonical_metadata_changed": False,
    }
