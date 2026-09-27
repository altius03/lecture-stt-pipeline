from __future__ import annotations

import argparse
from contextlib import nullcontext
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from lecture_stt.downstream.lib import (
    DEFAULT_OBSIDIAN_NOTE_DIR,
    DownstreamConfig,
    DownstreamDistributor,
    JsonlLogger,
    SubjectRoute,
    default_subject_routes,
)
from lecture_stt.downstream.semester import load_active_semester
from lecture_stt.downstream.transcript_delivery import (
    TranscriptPostprocessSettings,
    TranscriptDeliveryDistributor,
    load_postprocess_settings,
    open_delivery_db,
    reconcile_completed_jobs,
)
from lecture_stt.shared.log_retention import DEFAULT_DOWNSTREAM_BACKUP_COUNT, DEFAULT_LOG_MAX_BYTES
from lecture_stt.shared.paths import (
    default_db_path,
    default_log_dir,
    env_file,
    repo_root,
    resolve_config_path,
    runtime_env,
    state_dir,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS worker path uses fcntl.
    fcntl = None  # type: ignore[assignment]


logger = logging.getLogger("lecture_stt.downstream")


@dataclass(frozen=True)
class TranscriptWorkerConfig:
    enabled: bool
    db_path: Path
    scan_interval_sec: int
    batch_size: int
    log_jsonl_path: Path
    log_jsonl_max_bytes: int
    log_jsonl_backup_count: int
    lock_path: Path
    active_semester_path: Path
    postprocess_settings: TranscriptPostprocessSettings | None = None


class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def __enter__(self) -> "SingleInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
        if fcntl is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _parse_bool(value: Any, *, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    raise ValueError(f"downstream.{name} must be a boolean")


def _default_config() -> dict[str, Any]:
    default_subjects = default_subject_routes()
    state_root = state_dir()
    return {
        "paths": {
            "db_path": str(default_db_path()),
        },
        "transcript_delivery": {
            "enabled": False,
            "active_semester_manifest": str(state_root / "active-semester.json"),
            "correction_staging_dir": "${LECTURE_RECORDINGS_ROOT}/03_correction",
            "summary_staging_dir": "${LECTURE_RECORDINGS_ROOT}/04_summarize",
            "generator": {
                "backend": "codex_cli",
                "codex_binary": "codex",
                "model": "gpt-5.6-terra",
                "reasoning_effort": "low",
                "timeout_sec": 1200,
                "max_attempts": 3,
                "max_correction_chars": 100000,
                "max_summary_chars": 100000,
            },
            "scan_interval_sec": 30,
            "batch_size": 20,
            "log_jsonl_path": str(default_log_dir() / "transcript-delivery.jsonl"),
            "log_jsonl_max_bytes": DEFAULT_LOG_MAX_BYTES,
            "log_jsonl_backup_count": DEFAULT_DOWNSTREAM_BACKUP_COUNT,
            "lock_path": str(state_root / "transcript-delivery.lock"),
        },
        "downstream": {
            "scan_interval_sec": 30,
            "stable_for_sec": 60,
            "log_jsonl_path": str(default_log_dir() / "downstream.jsonl"),
            "log_jsonl_max_bytes": DEFAULT_LOG_MAX_BYTES,
            "log_jsonl_backup_count": DEFAULT_DOWNSTREAM_BACKUP_COUNT,
            "stats_heartbeat_scans": 120,
            "log_suppression_max_keys": 4096,
            "log_routine_scan_events": False,
            "lock_path": str(state_root / "downstream.lock"),
            "subjects": {
                abbr: {
                    "gh_course_dir": route.gh_course_dir,
                    "obsidian_course_dir": route.obsidian_course_dir,
                    "display_name": route.display_name,
                    "obsidian_note_dir": route.obsidian_note_dir,
                }
                for abbr, route in default_subjects.items()
            },
        },
    }


def _load_config_mapping(config_path: str) -> tuple[dict[str, Any], dict[str, str]]:
    root = repo_root()
    path = Path(config_path)
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        raise FileNotFoundError(f"Missing required config file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config must be a YAML mapping in {path}")
    return _deep_merge(_default_config(), loaded), runtime_env(dotenv_path=env_file())


def load_transcript_worker_config(
    config_path: str = "config/config.yaml",
) -> TranscriptWorkerConfig:
    merged, env = _load_config_mapping(config_path)
    paths_cfg = merged.get("paths") or {}
    delivery_cfg = merged.get("transcript_delivery") or {}
    if not isinstance(delivery_cfg, dict):
        raise ValueError("transcript_delivery must be a mapping")

    try:
        enabled = _parse_bool(delivery_cfg.get("enabled", False), name="enabled")
    except ValueError as exc:
        raise ValueError("transcript_delivery.enabled must be a boolean") from exc

    def parse_positive_int(name: str) -> int:
        try:
            value = int(delivery_cfg[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"transcript_delivery.{name} must be an integer") from exc
        if value <= 0:
            raise ValueError(f"transcript_delivery.{name} must be greater than 0")
        return value

    def parse_non_negative_int(name: str) -> int:
        try:
            value = int(delivery_cfg[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"transcript_delivery.{name} must be an integer") from exc
        if value < 0:
            raise ValueError(
                f"transcript_delivery.{name} must be greater than or equal to 0"
            )
        return value

    def resolve_delivery_path(name: str) -> Path:
        raw = delivery_cfg.get(name)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"transcript_delivery.{name} must be a non-empty path string")
        return resolve_config_path(raw, base_dir=repo_root(), env=env)

    active_semester_path = resolve_delivery_path("active_semester_manifest")
    postprocess_settings: TranscriptPostprocessSettings | None = None
    if enabled:
        postprocess_settings = load_postprocess_settings(
            delivery_cfg,
            base_dir=repo_root(),
            env=env,
            default_tmp_root=resolve_config_path(
                str(paths_cfg.get("tmp_dir", str(state_dir()))),
                base_dir=repo_root(),
                env=env,
            ),
        )
        load_active_semester(active_semester_path)
    return TranscriptWorkerConfig(
        enabled=enabled,
        db_path=resolve_config_path(
            str(paths_cfg.get("db_path", str(default_db_path()))),
            base_dir=repo_root(),
            env=env,
        ),
        active_semester_path=active_semester_path,
        scan_interval_sec=parse_positive_int("scan_interval_sec"),
        batch_size=parse_positive_int("batch_size"),
        log_jsonl_path=resolve_delivery_path("log_jsonl_path"),
        log_jsonl_max_bytes=parse_non_negative_int("log_jsonl_max_bytes"),
        log_jsonl_backup_count=parse_non_negative_int("log_jsonl_backup_count"),
        lock_path=resolve_delivery_path("lock_path"),
        postprocess_settings=postprocess_settings,
    )


def load_worker_config(config_path: str = "config/config.yaml") -> DownstreamConfig:
    root = repo_root()
    path = Path(config_path)
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        raise FileNotFoundError(f"Missing required config file: {path}")

    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config must be a YAML mapping in {path}")

    merged = _deep_merge(_default_config(), loaded)
    paths_cfg = merged.get("paths") or {}
    downstream_cfg = merged.get("downstream") or {}

    env = runtime_env(dotenv_path=env_file())

    required_path_keys = [
        "correction_folder",
        "summary_folder",
        "gh_current_semester_root",
        "obsidian_semester_root",
    ]
    for key in required_path_keys:
        value = downstream_cfg.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"downstream.{key} must be a non-empty path string")

    db_path = resolve_config_path(str(paths_cfg.get("db_path", str(default_db_path()))), env=env)
    correction_dir = resolve_config_path(str(downstream_cfg["correction_folder"]), env=env)
    summary_dir = resolve_config_path(str(downstream_cfg["summary_folder"]), env=env)
    gh_root = resolve_config_path(str(downstream_cfg["gh_current_semester_root"]), env=env)
    obsidian_root = resolve_config_path(str(downstream_cfg["obsidian_semester_root"]), env=env)
    log_jsonl_path = resolve_config_path(str(downstream_cfg["log_jsonl_path"]), env=env)
    lock_path = resolve_config_path(str(downstream_cfg["lock_path"]), env=env)

    def parse_int(name: str) -> int:
        try:
            return int(downstream_cfg[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"downstream.{name} must be an integer") from exc

    scan_interval_sec = parse_int("scan_interval_sec")
    stable_for_sec = parse_int("stable_for_sec")
    log_jsonl_max_bytes = parse_int("log_jsonl_max_bytes")
    log_jsonl_backup_count = parse_int("log_jsonl_backup_count")
    stats_heartbeat_scans = parse_int("stats_heartbeat_scans")
    log_suppression_max_keys = parse_int("log_suppression_max_keys")
    try:
        log_routine_scan_events = _parse_bool(
            downstream_cfg["log_routine_scan_events"],
            name="log_routine_scan_events",
        )
    except KeyError as exc:
        raise ValueError("downstream.log_routine_scan_events must be a boolean") from exc
    if scan_interval_sec <= 0:
        raise ValueError("downstream.scan_interval_sec must be greater than 0")
    if stable_for_sec <= 0:
        raise ValueError("downstream.stable_for_sec must be greater than 0")
    if log_jsonl_max_bytes < 0:
        raise ValueError("downstream.log_jsonl_max_bytes must be greater than or equal to 0")
    if log_jsonl_backup_count < 0:
        raise ValueError("downstream.log_jsonl_backup_count must be greater than or equal to 0")
    if stats_heartbeat_scans <= 0:
        raise ValueError("downstream.stats_heartbeat_scans must be greater than 0")
    if log_suppression_max_keys <= 0:
        raise ValueError("downstream.log_suppression_max_keys must be greater than 0")

    subjects_cfg = downstream_cfg.get("subjects") or {}
    if not isinstance(subjects_cfg, dict) or not subjects_cfg:
        raise ValueError("downstream.subjects must be a non-empty mapping")

    subjects: dict[str, SubjectRoute] = {}
    for abbr, payload in subjects_cfg.items():
        if not isinstance(payload, dict):
            raise ValueError(f"downstream.subjects.{abbr} must be a mapping")
        subjects[abbr] = SubjectRoute(
            gh_course_dir=str(payload["gh_course_dir"]),
            obsidian_course_dir=str(payload["obsidian_course_dir"]),
            display_name=str(payload.get("display_name", abbr)),
            obsidian_note_dir=str(payload.get("obsidian_note_dir", DEFAULT_OBSIDIAN_NOTE_DIR)),
        )

    return DownstreamConfig(
        correction_dir=correction_dir,
        summary_dir=summary_dir,
        gh_current_semester_root=gh_root,
        obsidian_semester_root=obsidian_root,
        db_path=db_path,
        log_jsonl_path=log_jsonl_path,
        lock_path=lock_path,
        scan_interval_sec=scan_interval_sec,
        stable_for_sec=stable_for_sec,
        subjects=subjects,
        log_jsonl_max_bytes=log_jsonl_max_bytes,
        log_jsonl_backup_count=log_jsonl_backup_count,
        stats_heartbeat_scans=stats_heartbeat_scans,
        log_suppression_max_keys=log_suppression_max_keys,
        log_routine_scan_events=log_routine_scan_events,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex transcript postprocess worker")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit")
    parser.add_argument("--dry-run", action="store_true", help="Plan actions without mutating files or DB")
    parser.add_argument("--config", default="config/config.yaml", help="Path to config.yaml")
    return parser.parse_args()


def setup_logging() -> logging.Logger:
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


class ScanStatsReporter:
    def __init__(
        self,
        target_logger: logging.Logger,
        *,
        heartbeat_scans: int = 120,
        emit_stdout: bool = True,
    ):
        self.logger = target_logger
        self.heartbeat_scans = max(1, int(heartbeat_scans))
        self.emit_stdout = emit_stdout
        self._last_stats: dict[str, int] | None = None
        self._suppressed_scans = 0

    def log(self, stats: dict[str, int], *, dry_run: bool) -> None:
        if not self.emit_stdout:
            return
        normalized = dict(sorted((key, int(value)) for key, value in stats.items()))
        if self._last_stats is None:
            self._last_stats = normalized
            self._suppressed_scans = 0
            self.logger.info("downstream scan stats initial stats=%s dry_run=%s", normalized, dry_run)
            return

        if normalized != self._last_stats:
            suppressed = self._suppressed_scans
            self._last_stats = normalized
            self._suppressed_scans = 0
            self.logger.info(
                "downstream scan stats changed stats=%s dry_run=%s suppressed_scans=%s",
                normalized,
                dry_run,
                suppressed,
            )
            return

        self._suppressed_scans += 1
        if self._suppressed_scans >= self.heartbeat_scans:
            suppressed = self._suppressed_scans
            self._suppressed_scans = 0
            self.logger.info(
                "downstream scan stats heartbeat stats=%s dry_run=%s suppressed_scans=%s",
                normalized,
                dry_run,
                suppressed,
            )


def run_worker(config: DownstreamConfig, *, dry_run: bool, run_once: bool) -> None:
    """Run the retired correction/summary distributor for archive compatibility only."""
    with SingleInstanceLock(config.lock_path):
        distributor = DownstreamDistributor(config, dry_run=dry_run)
        reporter = ScanStatsReporter(
            logger,
            heartbeat_scans=config.stats_heartbeat_scans,
            emit_stdout=config.log_routine_scan_events,
        )
        try:
            while True:
                stats = distributor.scan_once()
                reporter.log(stats, dry_run=dry_run)
                if run_once:
                    return
                time.sleep(config.scan_interval_sec)
        finally:
            distributor.close()


def _safe_delivery_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    safe: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        safe.append(
            {
                key: item[key]
                for key in ("source_job_id", "status", "error_code", "dry_run")
                if key in item
            }
        )
    return safe


def run_transcript_worker(
    config: TranscriptWorkerConfig,
    *,
    dry_run: bool,
    run_once: bool,
) -> None:
    if not config.enabled:
        logger.info("Transcript postprocess delivery is disabled")
        return
    if config.postprocess_settings is None:
        raise ValueError("Transcript postprocess settings are missing")

    lock = nullcontext() if dry_run else SingleInstanceLock(config.lock_path)
    with lock:
        conn = open_delivery_db(config.db_path, readonly=dry_run)
        jsonl = JsonlLogger(
            config.log_jsonl_path,
            enabled=not dry_run,
            max_bytes=config.log_jsonl_max_bytes,
            backup_count=config.log_jsonl_backup_count,
        )
        distributor = TranscriptDeliveryDistributor(
            conn,
            config.postprocess_settings,
            dry_run=dry_run,
        )
        try:
            while True:
                reconciliation = reconcile_completed_jobs(
                    conn,
                    settings=config.postprocess_settings,
                    limit=config.batch_size,
                    dry_run=dry_run,
                )
                delivery = distributor.process_pending(limit=config.batch_size)
                event = {
                    "event": "transcript_postprocess_scan",
                    "dry_run": dry_run,
                    "reconciled": int(reconciliation.get("queued", 0)),
                    "reconciliation_errors": int(reconciliation.get("errors", 0)),
                    "processed": int(delivery.get("processed", 0)),
                    "delivered": int(delivery.get("delivered", 0)),
                    "conflict": int(delivery.get("conflict", 0)),
                    "needs_review": int(delivery.get("needs_review", 0)),
                    "error": int(delivery.get("error", 0)),
                    "reconciliation_results": _safe_delivery_results(reconciliation),
                    "delivery_results": _safe_delivery_results(delivery),
                }
                jsonl.write(**event)
                if any(
                    event[key]
                    for key in (
                        "reconciled",
                        "reconciliation_errors",
                        "processed",
                        "conflict",
                        "needs_review",
                        "error",
                    )
                ):
                    logger.info(
                        "transcript postprocess scan reconciled=%s processed=%s delivered=%s "
                        "conflict=%s needs_review=%s error=%s reconciliation_errors=%s dry_run=%s",
                        event["reconciled"],
                        event["processed"],
                        event["delivered"],
                        event["conflict"],
                        event["needs_review"],
                        event["error"],
                        event["reconciliation_errors"],
                        dry_run,
                    )
                if run_once:
                    return
                time.sleep(config.scan_interval_sec)
        finally:
            conn.close()


def main() -> None:
    args = parse_args()
    setup_logging()
    config = load_transcript_worker_config(args.config)
    try:
        run_transcript_worker(config, dry_run=args.dry_run, run_once=args.once)
    except BlockingIOError:
        logger.error("Another downstream worker is already running")
        raise SystemExit(1)
    except sqlite3.Error as exc:
        logger.error("Downstream DB error: %s", exc)
        raise SystemExit(1)
    except KeyboardInterrupt:
        logger.info("Downstream worker interrupted")
    except Exception as exc:
        logger.exception("Downstream worker failed: %s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
