from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from lecture_stt.downstream.lib import (
    DownstreamConfig,
    DownstreamDistributor,
    SubjectRoute,
    default_subject_routes,
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
            obsidian_note_dir=str(payload.get("obsidian_note_dir", "강의록")),
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
    parser = argparse.ArgumentParser(description="Lecture downstream distribution worker")
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


def main() -> None:
    args = parse_args()
    setup_logging()
    config = load_worker_config(args.config)
    try:
        run_worker(config, dry_run=args.dry_run, run_once=args.once)
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
