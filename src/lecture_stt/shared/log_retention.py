from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shutil
import time
from typing import Iterable

DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_APP_BACKUP_COUNT = 5
DEFAULT_DOWNSTREAM_BACKUP_COUNT = 5
DEFAULT_LAUNCHD_BACKUP_COUNT = 3
DEFAULT_ARCHIVE_RETENTION_DAYS = 30


@dataclass(frozen=True)
class LogRotationPlan:
    """Result of a bounded plain-text log rotation check."""

    path: Path
    max_bytes: int
    backup_count: int
    current_size: int
    should_rotate: bool
    rotated: bool
    dry_run: bool
    actions: tuple[str, ...] = field(default_factory=tuple)


def _normalize_non_negative_int(value: int, *, name: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if normalized < 0:
        raise ValueError(f"{name} must be greater than or equal to 0")
    return normalized


def rotate_plain_log_if_needed(
    path: Path | str,
    *,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LAUNCHD_BACKUP_COUNT,
    dry_run: bool = False,
) -> LogRotationPlan:
    """Rotate a launchd-style plain log once it exceeds a size guard.

    This intentionally does not compress or delete historical archives. It only
    maintains bounded numeric siblings like ``worker.out.log.1`` through
    ``worker.out.log.N`` and truncates the active file after rotation.
    Date-based compressed archive retention is handled separately by
    ``prune_compressed_archives`` so cleanup/archive remains an explicit step.
    """

    log_path = Path(path)
    limit = _normalize_non_negative_int(max_bytes, name="max_bytes")
    backups = _normalize_non_negative_int(backup_count, name="backup_count")

    if not log_path.exists():
        return LogRotationPlan(
            path=log_path,
            max_bytes=limit,
            backup_count=backups,
            current_size=0,
            should_rotate=False,
            rotated=False,
            dry_run=dry_run,
        )
    if not log_path.is_file():
        raise ValueError(f"Log path is not a file: {log_path}")

    current_size = log_path.stat().st_size
    should_rotate = limit > 0 and current_size > limit
    if not should_rotate:
        return LogRotationPlan(
            path=log_path,
            max_bytes=limit,
            backup_count=backups,
            current_size=current_size,
            should_rotate=False,
            rotated=False,
            dry_run=dry_run,
        )

    actions: list[str] = []
    if backups == 0:
        target = _unique_rotated_path(log_path)
        actions.append(f"copy {log_path} -> {target}")
        actions.append(f"truncate {log_path}")
        if not dry_run:
            shutil.copy2(log_path, target)
            with log_path.open("r+b") as handle:
                handle.truncate(0)
        return LogRotationPlan(
            path=log_path,
            max_bytes=limit,
            backup_count=backups,
            current_size=current_size,
            should_rotate=True,
            rotated=not dry_run,
            dry_run=dry_run,
            actions=tuple(actions),
        )

    oldest = log_path.with_name(f"{log_path.name}.{backups}")
    if oldest.exists():
        actions.append(f"remove {oldest}")
    for index in range(backups - 1, 0, -1):
        src = log_path.with_name(f"{log_path.name}.{index}")
        dst = log_path.with_name(f"{log_path.name}.{index + 1}")
        if src.exists():
            actions.append(f"rename {src} -> {dst}")
    first_backup = log_path.with_name(f"{log_path.name}.1")
    actions.append(f"copy {log_path} -> {first_backup}")
    actions.append(f"truncate {log_path}")

    if not dry_run:
        if oldest.exists():
            oldest.unlink()
        for index in range(backups - 1, 0, -1):
            src = log_path.with_name(f"{log_path.name}.{index}")
            dst = log_path.with_name(f"{log_path.name}.{index + 1}")
            if src.exists():
                src.rename(dst)
        shutil.copy2(log_path, first_backup)
        with log_path.open("r+b") as handle:
            handle.truncate(0)

    return LogRotationPlan(
        path=log_path,
        max_bytes=limit,
        backup_count=backups,
        current_size=current_size,
        should_rotate=True,
        rotated=not dry_run,
        dry_run=dry_run,
        actions=tuple(actions),
    )


def _unique_rotated_path(path: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = path.with_name(f"{path.name}.{stamp}")
    candidate = base
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = path.with_name(f"{path.name}.{stamp}.{suffix}")
    return candidate


def prune_compressed_archives(
    roots: Path | str | Iterable[Path | str],
    *,
    max_age_days: int = DEFAULT_ARCHIVE_RETENTION_DAYS,
    dry_run: bool = True,
    now: float | None = None,
) -> list[Path]:
    """Plan or delete compressed log archives older than the retention window.

    Only ``*.gz`` files are considered. Plain logs and numeric rotation backups
    are intentionally left untouched so operational cleanup cannot accidentally
    remove active launchd/app logs.
    """

    days = _normalize_non_negative_int(max_age_days, name="max_age_days")
    cutoff = (time.time() if now is None else float(now)) - (days * 24 * 3600)
    candidates: list[Path] = []
    for root in _iter_roots(roots):
        if not root.exists():
            continue
        if root.is_file():
            entries = [root]
        elif root.is_dir():
            entries = list(root.rglob("*.gz"))
        else:
            continue
        for entry in entries:
            if entry.is_file() and entry.suffix == ".gz" and entry.stat().st_mtime < cutoff:
                candidates.append(entry)

    candidates = sorted(set(candidates))
    if not dry_run:
        for archive in candidates:
            archive.unlink()
    return candidates


def _iter_roots(roots: Path | str | Iterable[Path | str]) -> Iterable[Path]:
    if isinstance(roots, (str, Path)):
        yield Path(roots)
        return
    for root in roots:
        yield Path(root)
