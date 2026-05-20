from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

from lecture_stt.shared import db, utils


logger = logging.getLogger(__name__)

DEFAULT_OBSIDIAN_NOTE_DIR = "강의록"
CORRECTION_STATUS_INCOMPLETE = "INCOMPLETE"
CORRECTION_STATUS_DELIVERED = "DELIVERED"
CORRECTION_STATUS_CONFLICT = "CONFLICT"
CORRECTION_STATUS_ERROR = "ERROR"
SUMMARY_STATUS_BLOCKED = "BLOCKED"
SUMMARY_STATUS_DELIVERED = "DELIVERED"
SUMMARY_STATUS_CONFLICT = "CONFLICT"
SUMMARY_STATUS_ERROR = "ERROR"


@dataclass(frozen=True)
class SubjectRoute:
    gh_course_dir: str
    obsidian_course_dir: str
    display_name: str
    obsidian_note_dir: str = DEFAULT_OBSIDIAN_NOTE_DIR

    def gh_origin_dir(self, gh_root: Path) -> Path:
        return gh_root / self.gh_course_dir / "06_lecture_notes" / "02_origin"

    def gh_summary_dir(self, gh_root: Path) -> Path:
        return gh_root / self.gh_course_dir / "06_lecture_notes" / "01_summarize"

    def obsidian_summary_dir(self, obsidian_root: Path) -> Path:
        return obsidian_root / self.obsidian_course_dir / self.obsidian_note_dir


@dataclass(frozen=True)
class DownstreamConfig:
    correction_dir: Path
    summary_dir: Path
    gh_current_semester_root: Path
    obsidian_semester_root: Path
    db_path: Path
    log_jsonl_path: Path
    lock_path: Path
    scan_interval_sec: int
    stable_for_sec: int
    subjects: Dict[str, SubjectRoute]
    log_jsonl_max_bytes: int = 0
    log_jsonl_backup_count: int = 3
    stats_heartbeat_scans: int = 120
    log_suppression_max_keys: int = 4096
    log_routine_scan_events: bool = False


@dataclass(frozen=True)
class ParsedStem:
    logical_stem: str
    subject_abbr: Optional[str]
    raw_subject_abbr: Optional[str]
    error_code: Optional[str]
    error_message: Optional[str]


@dataclass
class CorrectionUnit:
    logical_stem: str
    subject_abbr: Optional[str]
    raw_subject_abbr: Optional[str]
    txt_path: Optional[Path] = None
    json_path: Optional[Path] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def subject_for_db(self) -> str:
        return self.subject_abbr or self.raw_subject_abbr or "UNKNOWN"

    @property
    def is_complete(self) -> bool:
        return bool(self.txt_path and self.json_path and not self.error_code)


@dataclass
class SummaryUnit:
    logical_stem: str
    subject_abbr: Optional[str]
    raw_subject_abbr: Optional[str]
    md_path: Optional[Path] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def subject_for_db(self) -> str:
        return self.subject_abbr or self.raw_subject_abbr or "UNKNOWN"


@dataclass(frozen=True)
class FileSyncOutcome:
    status: str
    destination: Path
    source_sha256: str
    destination_sha256: Optional[str]


class FileConflictError(RuntimeError):
    def __init__(self, src: Path, dst: Path, source_sha256: str, destination_sha256: str):
        self.src = src
        self.dst = dst
        self.source_sha256 = source_sha256
        self.destination_sha256 = destination_sha256
        super().__init__(f"Conflict at {dst}: destination content differs from source")


class JsonlLogger:
    def __init__(self, path: Path, enabled: bool, *, max_bytes: int = 0, backup_count: int = 3):
        self.path = Path(path)
        self.enabled = enabled
        self.max_bytes = max(0, int(max_bytes))
        self.backup_count = max(0, int(backup_count))

    def write(self, **payload: object) -> None:
        if not self.enabled:
            return
        utils.ensure_dir(self.path.parent)
        record = {"ts": utils.now_iso(), **payload}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        self._rotate_if_needed(len(line.encode("utf-8")))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def _rotate_if_needed(self, next_bytes: int) -> None:
        if self.max_bytes <= 0 or not self.path.exists():
            return
        try:
            current_size = self.path.stat().st_size
        except OSError:
            return
        if current_size + max(next_bytes, 0) <= self.max_bytes:
            return
        self._rotate()

    def _rotate(self) -> None:
        if self.backup_count <= 0:
            timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            rotated = self.path.with_name(f"{self.path.name}.{timestamp}.{uuid.uuid4().hex[:8]}")
            try:
                self.path.rename(rotated)
            except OSError:
                logger.warning("Failed to rotate downstream jsonl log %s", self.path, exc_info=True)
            return

        for index in range(self.backup_count - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{index}")
            dst = self.path.with_name(f"{self.path.name}.{index + 1}")
            if not src.exists():
                continue
            try:
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
            except OSError:
                logger.warning("Failed to rotate downstream jsonl backup %s", src, exc_info=True)
                return

        first_backup = self.path.with_name(f"{self.path.name}.1")
        try:
            if first_backup.exists():
                first_backup.unlink()
            self.path.rename(first_backup)
        except OSError:
            logger.warning("Failed to rotate downstream jsonl log %s", self.path, exc_info=True)


def default_subject_routes() -> Dict[str, SubjectRoute]:
    return {
        "LC": SubjectRoute("01_logic_circuits", "01_logic_circuits", "논리회로"),
        "DStr": SubjectRoute("02_data_structures", "02_data_structures", "자료구조"),
        "DS": SubjectRoute("03_data_science", "03_data_science", "데이터사이언스"),
        "LA": SubjectRoute("04_linear_algebra", "04_linear_algebra", "선형대수학"),
        "OOP": SubjectRoute("05_object_oriented_programming", "05_object_oriented_programming", "객체지향언어"),
        "Unix": SubjectRoute("06_unix_fundamentals", "06_unix_fundamentals", "유닉스 기초"),
    }


def _fsync_file(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    except OSError:
        logger.debug("Unable to fsync file %s", path, exc_info=True)


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        logger.debug("Unable to fsync directory %s", path, exc_info=True)
    finally:
        os.close(descriptor)


def _copy_file_no_overwrite(src: Path, dst: Path) -> None:
    utils.ensure_dir(dst.parent)
    temp_path = dst.parent / f".{dst.name}.{uuid.uuid4().hex}.tmp"
    shutil.copy2(src, temp_path)
    _fsync_file(temp_path)

    try:
        os.link(temp_path, dst)
    except FileExistsError as exc:
        raise FileExistsError(str(dst)) from exc
    except OSError as exc:
        # iCloud-backed paths should allow hard links, but fall back to replace in single-writer mode.
        if exc.errno in {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV}:
            if dst.exists():
                raise FileExistsError(str(dst)) from exc
            os.replace(temp_path, dst)
            temp_path = None  # type: ignore[assignment]
        else:
            raise
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)
    _fsync_dir(dst.parent)


class DownstreamDistributor:
    def __init__(
        self,
        config: DownstreamConfig,
        *,
        dry_run: bool = False,
        conn: sqlite3.Connection | None = None,
    ):
        self.config = config
        self.dry_run = dry_run
        self._owns_connection = conn is None
        self._correction_ready_stems: set[str] = set()
        self._log_suppression_max_keys = max(1, int(self.config.log_suppression_max_keys))
        self._logged_repeating_problem_events: OrderedDict[tuple[str, str, str, str, str], None] = OrderedDict()
        self._stats_heartbeat_scans = max(1, int(self.config.stats_heartbeat_scans))
        self._last_routine_scan_signature: tuple[tuple[str, str], ...] | None = None
        self._suppressed_routine_scan_completed = 0

        if conn is not None:
            self.conn = conn
            if not self.dry_run:
                db.init_deliveries_table(self.conn)
        elif self.dry_run:
            if self.config.db_path.exists():
                self.conn = db.connect_db(str(self.config.db_path))
            else:
                self.conn = sqlite3.connect(":memory:")
                self.conn.row_factory = sqlite3.Row
        else:
            self.conn = db.init_db(str(self.config.db_path))

        self.jsonl = JsonlLogger(
            self.config.log_jsonl_path,
            enabled=not self.dry_run,
            max_bytes=self.config.log_jsonl_max_bytes,
            backup_count=self.config.log_jsonl_backup_count,
        )

    def close(self) -> None:
        if self._owns_connection:
            self.conn.close()

    def scan_once(self) -> dict[str, int]:
        self._correction_ready_stems.clear()
        stats = {
            "correction_delivered": 0,
            "summary_delivered": 0,
            "blocked": 0,
            "incomplete": 0,
            "conflicts": 0,
            "errors": 0,
        }

        correction_units = self._collect_correction_units()
        summary_units = self._collect_summary_units()

        self._log_event(
            "scan_started",
            correction_units=len(correction_units),
            dry_run=self.dry_run,
            summary_units=len(summary_units),
        )

        for logical_stem in sorted(correction_units.keys()):
            try:
                result = self._process_correction_unit(correction_units[logical_stem])
            except Exception as exc:
                stats["errors"] += 1
                logger.exception("Unexpected correction error for %s", logical_stem)
                self._log_repeating_problem_once(
                    "correction_unexpected_error",
                    logical_stem=logical_stem,
                    error_code="UNEXPECTED_EXCEPTION",
                    error_message=str(exc),
                )
                continue
            stats[result] += 1

        for logical_stem in sorted(summary_units.keys()):
            try:
                result = self._process_summary_unit(summary_units[logical_stem])
            except Exception as exc:
                stats["errors"] += 1
                logger.exception("Unexpected summary error for %s", logical_stem)
                self._log_repeating_problem_once(
                    "summary_unexpected_error",
                    logical_stem=logical_stem,
                    error_code="UNEXPECTED_EXCEPTION",
                    error_message=str(exc),
                )
                continue
            stats[result] += 1

        self._log_event("scan_completed", dry_run=self.dry_run, stats=stats)
        return stats

    def _collect_correction_units(self) -> Dict[str, CorrectionUnit]:
        units: Dict[str, CorrectionUnit] = {}
        for path in self._iter_stable_files(self.config.correction_dir, {".txt", ".json"}):
            parsed = self._parse_logical_stem(path.stem)
            unit = units.get(path.stem)
            if unit is None:
                unit = CorrectionUnit(
                    logical_stem=path.stem,
                    subject_abbr=parsed.subject_abbr,
                    raw_subject_abbr=parsed.raw_subject_abbr,
                    error_code=parsed.error_code,
                    error_message=parsed.error_message,
                )
                units[path.stem] = unit
            if path.suffix.lower() == ".txt":
                unit.txt_path = path
            elif path.suffix.lower() == ".json":
                unit.json_path = path
        return units

    def _collect_summary_units(self) -> Dict[str, SummaryUnit]:
        units: Dict[str, SummaryUnit] = {}
        for path in self._iter_stable_files(self.config.summary_dir, {".md"}):
            parsed = self._parse_logical_stem(path.stem)
            units[path.stem] = SummaryUnit(
                logical_stem=path.stem,
                subject_abbr=parsed.subject_abbr,
                raw_subject_abbr=parsed.raw_subject_abbr,
                md_path=path,
                error_code=parsed.error_code,
                error_message=parsed.error_message,
            )
        return units

    def _iter_stable_files(self, directory: Path, allowed_suffixes: set[str]) -> Iterable[Path]:
        if not directory.exists():
            return []

        stable_paths: list[Path] = []
        now = time.time()
        try:
            entries = list(directory.iterdir())
        except OSError:
            return []

        for path in entries:
            if not path.is_file():
                continue
            if utils.is_temporary_file(path):
                continue
            if path.suffix.lower() not in allowed_suffixes:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if now - stat.st_mtime < self.config.stable_for_sec:
                continue
            stable_paths.append(path)

        stable_paths.sort(key=lambda item: item.name)
        return stable_paths

    def _parse_logical_stem(self, logical_stem: str) -> ParsedStem:
        if len(logical_stem) < 8 or not logical_stem[:6].isdigit():
            return ParsedStem(
                logical_stem=logical_stem,
                subject_abbr=None,
                raw_subject_abbr=None,
                error_code="INVALID_STEM",
                error_message=f"Invalid logical stem: {logical_stem}",
            )

        suffix = logical_stem[6:]
        for abbr in sorted(self.config.subjects.keys(), key=len, reverse=True):
            if suffix == abbr:
                return ParsedStem(logical_stem, abbr, abbr, None, None)
            if suffix.startswith(f"{abbr}_"):
                remainder = suffix[len(abbr):]
                if remainder.startswith("_") and remainder[1:].isdigit():
                    return ParsedStem(logical_stem, abbr, abbr, None, None)

        raw_subject = suffix.split("_", 1)[0] if suffix else None
        error_code = "UNKNOWN_SUBJECT" if raw_subject and raw_subject.isalpha() else "INVALID_STEM"
        error_message = (
            f"Unknown subject abbreviation in logical stem: {logical_stem}"
            if error_code == "UNKNOWN_SUBJECT"
            else f"Invalid logical stem: {logical_stem}"
        )
        return ParsedStem(logical_stem, None, raw_subject, error_code, error_message)

    def _process_correction_unit(self, unit: CorrectionUnit) -> str:
        base_fields = self._base_delivery_fields(unit.logical_stem, unit.subject_for_db)

        if unit.error_code:
            self._save_delivery(
                unit.logical_stem,
                **base_fields,
                correction_status=CORRECTION_STATUS_ERROR,
                last_error_code=unit.error_code,
                last_error=unit.error_message,
            )
            self._log_repeating_problem_once(
                "correction_invalid",
                logical_stem=unit.logical_stem,
                error_code=unit.error_code,
                error_message=unit.error_message,
            )
            return "errors"

        if not unit.txt_path or not unit.json_path:
            self._save_delivery(
                unit.logical_stem,
                **base_fields,
                correction_txt_path=str(unit.txt_path) if unit.txt_path else None,
                correction_json_path=str(unit.json_path) if unit.json_path else None,
                correction_status=CORRECTION_STATUS_INCOMPLETE,
                last_error_code="INCOMPLETE_CORRECTION_PAIR",
                last_error="Correction pair requires both .txt and .json files",
            )
            self._log_repeating_problem_once(
                "correction_incomplete",
                logical_stem=unit.logical_stem,
                error_code="INCOMPLETE_CORRECTION_PAIR",
                error_message="Correction pair requires both .txt and .json files",
                txt_present=bool(unit.txt_path),
                json_present=bool(unit.json_path),
            )
            return "incomplete"

        route = self.config.subjects[unit.subject_abbr]  # type: ignore[index]
        origin_dir = route.gh_origin_dir(self.config.gh_current_semester_root)
        txt_hash = utils.compute_sha256(unit.txt_path)
        json_hash = utils.compute_sha256(unit.json_path)
        txt_destination = origin_dir / unit.txt_path.name
        json_destination = origin_dir / unit.json_path.name
        copied_destinations: list[Path] = []

        try:
            txt_outcome = self._sync_file(unit.txt_path, txt_destination, txt_hash)
            if txt_outcome.status == "copied":
                copied_destinations.append(txt_destination)
            json_outcome = self._sync_file(unit.json_path, json_destination, json_hash)
            if json_outcome.status == "copied":
                copied_destinations.append(json_destination)
        except FileConflictError as exc:
            rollback_error = self._rollback_copied_files(copied_destinations)
            if rollback_error is not None:
                self._save_delivery(
                    unit.logical_stem,
                    **base_fields,
                    correction_txt_path=str(unit.txt_path),
                    correction_json_path=str(unit.json_path),
                    correction_txt_sha256=txt_hash,
                    correction_json_sha256=json_hash,
                    tuk_origin_txt_path=str(txt_destination),
                    tuk_origin_json_path=str(json_destination),
                    correction_status=CORRECTION_STATUS_ERROR,
                    tuk_origin_done=0,
                    last_error_code="ROLLBACK_FAILED",
                    last_error=f"{exc}; rollback failed: {rollback_error}",
                )
                self._log_repeating_problem_once(
                    "correction_rollback_failed",
                    logical_stem=unit.logical_stem,
                    error_code="ROLLBACK_FAILED",
                    error_message=f"{exc}; rollback failed: {rollback_error}",
                    reason=str(exc.dst),
                    conflict_destination=str(exc.dst),
                    rollback_error=str(rollback_error),
                )
                return "errors"
            self._save_delivery(
                unit.logical_stem,
                **base_fields,
                correction_txt_path=str(unit.txt_path),
                correction_json_path=str(unit.json_path),
                correction_txt_sha256=txt_hash,
                correction_json_sha256=json_hash,
                tuk_origin_txt_path=str(txt_destination),
                tuk_origin_json_path=str(json_destination),
                correction_status=CORRECTION_STATUS_CONFLICT,
                tuk_origin_done=0,
                last_error_code="CONFLICT",
                last_error=str(exc),
            )
            self._log_repeating_problem_once(
                "correction_conflict",
                logical_stem=unit.logical_stem,
                error_code="CONFLICT",
                error_message=str(exc),
                reason=str(exc.dst),
                destination=str(exc.dst),
            )
            return "conflicts"
        except Exception as exc:
            rollback_error = self._rollback_copied_files(copied_destinations)
            if rollback_error is not None:
                exc = RuntimeError(f"{exc}; rollback failed: {rollback_error}")
            self._save_delivery(
                unit.logical_stem,
                **base_fields,
                correction_txt_path=str(unit.txt_path),
                correction_json_path=str(unit.json_path),
                correction_txt_sha256=txt_hash,
                correction_json_sha256=json_hash,
                tuk_origin_txt_path=str(txt_destination),
                tuk_origin_json_path=str(json_destination),
                correction_status=CORRECTION_STATUS_ERROR,
                tuk_origin_done=0,
                last_error_code="UNEXPECTED_ERROR",
                last_error=str(exc),
            )
            self._log_repeating_problem_once(
                "correction_error",
                logical_stem=unit.logical_stem,
                error_code="UNEXPECTED_ERROR",
                error_message=str(exc),
            )
            return "errors"

        completed_at = None
        if not self.dry_run:
            completed_at = self._completed_at(
                unit.logical_stem,
                correction_status=CORRECTION_STATUS_DELIVERED,
                tuk_origin_done=1,
            )
            try:
                self._delete_sources([unit.txt_path, unit.json_path])
            except Exception as exc:
                self._save_delivery(
                    unit.logical_stem,
                    **base_fields,
                    correction_txt_path=str(unit.txt_path),
                    correction_json_path=str(unit.json_path),
                    correction_txt_sha256=txt_hash,
                    correction_json_sha256=json_hash,
                    tuk_origin_txt_path=str(txt_destination),
                    tuk_origin_json_path=str(json_destination),
                    correction_status=CORRECTION_STATUS_ERROR,
                    tuk_origin_done=1,
                    last_error_code="SOURCE_CLEANUP_FAILED",
                    last_error=str(exc),
                )
                self._log_repeating_problem_once(
                    "correction_cleanup_failed",
                    logical_stem=unit.logical_stem,
                    error_code="SOURCE_CLEANUP_FAILED",
                    error_message=str(exc),
                )
                return "errors"

        self._save_delivery(
            unit.logical_stem,
            **base_fields,
            correction_txt_path=str(unit.txt_path),
            correction_json_path=str(unit.json_path),
            correction_txt_sha256=txt_hash,
            correction_json_sha256=json_hash,
            tuk_origin_txt_path=str(txt_destination),
            tuk_origin_json_path=str(json_destination),
            correction_status=CORRECTION_STATUS_DELIVERED,
            tuk_origin_done=1,
            completed_at=completed_at,
            last_error_code=None,
            last_error=None,
        )
        self._correction_ready_stems.add(unit.logical_stem)

        self._log_event(
            "correction_delivered",
            logical_stem=unit.logical_stem,
            txt_status=txt_outcome.status,
            json_status=json_outcome.status,
            dry_run=self.dry_run,
        )
        return "correction_delivered"

    def _process_summary_unit(self, unit: SummaryUnit) -> str:
        base_fields = self._base_delivery_fields(unit.logical_stem, unit.subject_for_db)

        if unit.error_code:
            self._save_delivery(
                unit.logical_stem,
                **base_fields,
                summary_status=SUMMARY_STATUS_ERROR,
                last_error_code=unit.error_code,
                last_error=unit.error_message,
            )
            self._log_repeating_problem_once(
                "summary_invalid",
                logical_stem=unit.logical_stem,
                error_code=unit.error_code,
                error_message=unit.error_message,
            )
            return "errors"

        if not unit.md_path:
            self._save_delivery(
                unit.logical_stem,
                **base_fields,
                summary_status=SUMMARY_STATUS_ERROR,
                last_error_code="MISSING_SUMMARY",
                last_error="Summary file is missing",
            )
            return "errors"

        if not self._correction_is_ready(unit.logical_stem, unit.subject_abbr):  # type: ignore[arg-type]
            existing_row = db.get_delivery(self.conn, unit.logical_stem)
            last_error_code = "CORRECTION_NOT_READY"
            last_error = "Summary delivery requires a delivered correction pair"
            if (
                existing_row
                and existing_row["correction_status"] not in {None, "", "MISSING", CORRECTION_STATUS_DELIVERED}
                and existing_row["last_error_code"]
            ):
                last_error_code = existing_row["last_error_code"]
                last_error = existing_row["last_error"]
            self._save_delivery(
                unit.logical_stem,
                **base_fields,
                summary_md_path=str(unit.md_path),
                summary_status=SUMMARY_STATUS_BLOCKED,
                last_error_code=last_error_code,
                last_error=last_error,
            )
            self._log_repeating_problem_once(
                "summary_blocked",
                logical_stem=unit.logical_stem,
                error_code=last_error_code,
                error_message=last_error,
                reason="correction_not_ready",
            )
            return "blocked"

        route = self.config.subjects[unit.subject_abbr]  # type: ignore[index]
        summary_hash = utils.compute_sha256(unit.md_path)
        tuk_destination = route.gh_summary_dir(self.config.gh_current_semester_root) / unit.md_path.name
        obsidian_destination = route.obsidian_summary_dir(self.config.obsidian_semester_root) / unit.md_path.name
        tuk_summary_done = 0
        obsidian_done = 0

        try:
            tuk_outcome = self._sync_file(unit.md_path, tuk_destination, summary_hash)
            tuk_summary_done = 1
            obsidian_outcome = self._sync_file(unit.md_path, obsidian_destination, summary_hash)
            obsidian_done = 1
        except FileConflictError as exc:
            self._save_delivery(
                unit.logical_stem,
                **base_fields,
                summary_md_path=str(unit.md_path),
                summary_md_sha256=summary_hash,
                tuk_summary_path=str(tuk_destination),
                obsidian_summary_path=str(obsidian_destination),
                summary_status=SUMMARY_STATUS_CONFLICT,
                tuk_summary_done=tuk_summary_done,
                obsidian_done=obsidian_done,
                last_error_code="CONFLICT",
                last_error=str(exc),
            )
            self._log_repeating_problem_once(
                "summary_conflict",
                logical_stem=unit.logical_stem,
                error_code="CONFLICT",
                error_message=str(exc),
                reason=str(exc.dst),
                destination=str(exc.dst),
            )
            return "conflicts"
        except Exception as exc:
            self._save_delivery(
                unit.logical_stem,
                **base_fields,
                summary_md_path=str(unit.md_path),
                summary_md_sha256=summary_hash,
                tuk_summary_path=str(tuk_destination),
                obsidian_summary_path=str(obsidian_destination),
                summary_status=SUMMARY_STATUS_ERROR,
                tuk_summary_done=tuk_summary_done,
                obsidian_done=obsidian_done,
                last_error_code="UNEXPECTED_ERROR",
                last_error=str(exc),
            )
            self._log_repeating_problem_once(
                "summary_error",
                logical_stem=unit.logical_stem,
                error_code="UNEXPECTED_ERROR",
                error_message=str(exc),
            )
            return "errors"

        completed_at = None
        if not self.dry_run:
            completed_at = self._completed_at(
                unit.logical_stem,
                summary_status=SUMMARY_STATUS_DELIVERED,
                tuk_summary_done=1,
                obsidian_done=1,
            )
            try:
                self._delete_sources([unit.md_path])
            except Exception as exc:
                self._save_delivery(
                    unit.logical_stem,
                    **base_fields,
                    summary_md_path=str(unit.md_path),
                    summary_md_sha256=summary_hash,
                    tuk_summary_path=str(tuk_destination),
                    obsidian_summary_path=str(obsidian_destination),
                    summary_status=SUMMARY_STATUS_ERROR,
                    tuk_summary_done=1,
                    obsidian_done=1,
                    last_error_code="SOURCE_CLEANUP_FAILED",
                    last_error=str(exc),
                )
                self._log_repeating_problem_once(
                    "summary_cleanup_failed",
                    logical_stem=unit.logical_stem,
                    error_code="SOURCE_CLEANUP_FAILED",
                    error_message=str(exc),
                )
                return "errors"
        self._save_delivery(
            unit.logical_stem,
            **base_fields,
            summary_md_path=str(unit.md_path),
            summary_md_sha256=summary_hash,
            tuk_summary_path=str(tuk_destination),
            obsidian_summary_path=str(obsidian_destination),
            summary_status=SUMMARY_STATUS_DELIVERED,
            tuk_summary_done=1,
            obsidian_done=1,
            completed_at=completed_at,
            last_error_code=None,
            last_error=None,
        )

        self._log_event(
            "summary_delivered",
            logical_stem=unit.logical_stem,
            tuk_status=tuk_outcome.status,
            obsidian_status=obsidian_outcome.status,
            dry_run=self.dry_run,
        )
        return "summary_delivered"

    def _sync_file(self, source: Path, destination: Path, source_sha256: str) -> FileSyncOutcome:
        if destination.exists():
            destination_sha256 = utils.compute_sha256(destination)
            if destination_sha256 == source_sha256:
                return FileSyncOutcome("identical", destination, source_sha256, destination_sha256)
            raise FileConflictError(source, destination, source_sha256, destination_sha256)

        if self.dry_run:
            return FileSyncOutcome("dry_run", destination, source_sha256, None)

        try:
            _copy_file_no_overwrite(source, destination)
        except FileExistsError:
            destination_sha256 = utils.compute_sha256(destination)
            if destination_sha256 == source_sha256:
                return FileSyncOutcome("identical", destination, source_sha256, destination_sha256)
            raise FileConflictError(source, destination, source_sha256, destination_sha256)

        destination_sha256 = utils.compute_sha256(destination)
        if destination_sha256 != source_sha256:
            raise RuntimeError(f"Hash verification failed for {destination}")
        return FileSyncOutcome("copied", destination, source_sha256, destination_sha256)

    def _correction_is_ready(self, logical_stem: str, subject_abbr: str) -> bool:
        if logical_stem in self._correction_ready_stems:
            return True

        row = db.get_delivery(self.conn, logical_stem)
        if row:
            if (
                row["correction_status"] == CORRECTION_STATUS_DELIVERED
                and int(row["tuk_origin_done"] or 0) == 1
            ):
                return True
            if row["correction_status"] not in {None, "", "MISSING"}:
                return False

        route = self.config.subjects[subject_abbr]
        origin_dir = route.gh_origin_dir(self.config.gh_current_semester_root)
        return (origin_dir / f"{logical_stem}.txt").exists() and (origin_dir / f"{logical_stem}.json").exists()

    def _completed_at(self, logical_stem: str, **overrides: object) -> Optional[str]:
        row = db.get_delivery(self.conn, logical_stem)
        flags = {
            "tuk_origin_done": int(row["tuk_origin_done"] or 0) if row else 0,
            "tuk_summary_done": int(row["tuk_summary_done"] or 0) if row else 0,
            "obsidian_done": int(row["obsidian_done"] or 0) if row else 0,
        }
        correction_status = row["correction_status"] if row else None
        summary_status = row["summary_status"] if row else None
        for key, value in overrides.items():
            if key in flags:
                flags[key] = int(value)
                continue
            if key == "correction_status":
                correction_status = str(value)
                continue
            if key == "summary_status":
                summary_status = str(value)
        if (
            correction_status == CORRECTION_STATUS_DELIVERED
            and summary_status == SUMMARY_STATUS_DELIVERED
            and all(value == 1 for value in flags.values())
        ):
            return utils.now_iso()
        return row["completed_at"] if row and row["completed_at"] else None

    def _base_delivery_fields(self, logical_stem: str, subject_abbr: str) -> dict[str, object]:
        job = db.find_latest_job_by_canonical_base(self.conn, logical_stem)
        return {
            "source_job_id": int(job["id"]) if job else None,
            "subject_abbr": subject_abbr,
            "last_attempted_at": utils.now_iso(),
        }

    def _save_delivery(self, logical_stem: str, **fields: object) -> None:
        if self.dry_run:
            return
        db.upsert_delivery(self.conn, logical_stem, **fields)

    def _delete_sources(self, sources: Iterable[Path]) -> None:
        for source in sources:
            source.unlink(missing_ok=True)

    def _rollback_copied_files(self, destinations: Iterable[Path]) -> Optional[OSError]:
        rollback_error: Optional[OSError] = None
        for destination in reversed(list(destinations)):
            try:
                destination.unlink(missing_ok=True)
            except OSError as exc:
                if rollback_error is None:
                    rollback_error = exc
                logger.warning("Failed to roll back copied destination %s", destination, exc_info=True)
        return rollback_error

    @staticmethod
    def _routine_scan_signature(payload: dict[str, object]) -> tuple[tuple[str, str], ...]:
        stats = payload.get("stats")
        if isinstance(stats, dict):
            return tuple(sorted((str(key), str(value)) for key, value in stats.items()))
        return tuple(
            sorted(
                (str(key), repr(value))
                for key, value in payload.items()
                if key not in {"dry_run"}
            )
        )

    def _routine_scan_completed_payload(self, payload: dict[str, object]) -> tuple[bool, dict[str, object]]:
        signature = self._routine_scan_signature(payload)
        if self._last_routine_scan_signature is None:
            self._last_routine_scan_signature = signature
            self._suppressed_routine_scan_completed = 0
            return True, dict(payload)
        if signature != self._last_routine_scan_signature:
            enriched = dict(payload)
            if self._suppressed_routine_scan_completed:
                enriched["suppressed_scan_count"] = self._suppressed_routine_scan_completed
            self._last_routine_scan_signature = signature
            self._suppressed_routine_scan_completed = 0
            return True, enriched

        self._suppressed_routine_scan_completed += 1
        if self._suppressed_routine_scan_completed >= self._stats_heartbeat_scans:
            enriched = dict(payload)
            enriched["suppressed_scan_count"] = self._suppressed_routine_scan_completed
            self._suppressed_routine_scan_completed = 0
            return True, enriched
        return False, dict(payload)

    def _log_event(self, event: str, **payload: object) -> None:
        routine_events = {"scan_started", "scan_completed"}
        if self.config.log_routine_scan_events or event not in routine_events:
            logger.info("%s %s", event, payload)
            self.jsonl.write(event=event, **payload)
            return
        if event == "scan_completed":
            should_emit, emitted_payload = self._routine_scan_completed_payload(dict(payload))
            if should_emit:
                self.jsonl.write(event=event, **emitted_payload)

    def _log_repeating_problem_once(self, event: str, **payload: object) -> None:
        logical_stem = str(payload.get("logical_stem") or "")
        error_code = str(payload.get("error_code") or "")
        error_message = str(payload.get("error_message") or "")
        reason = str(payload.get("reason") or "")
        key = (event, logical_stem, error_code, error_message, reason)
        if key in self._logged_repeating_problem_events:
            return
        self._logged_repeating_problem_events[key] = None
        while len(self._logged_repeating_problem_events) > self._log_suppression_max_keys:
            self._logged_repeating_problem_events.popitem(last=False)
        self._log_event(event, **payload)
