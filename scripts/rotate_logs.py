#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.shared.log_retention import (  # noqa: E402
    DEFAULT_ARCHIVE_RETENTION_DAYS,
    DEFAULT_LAUNCHD_BACKUP_COUNT,
    DEFAULT_LOG_MAX_BYTES,
    prune_compressed_archives,
    rotate_plain_log_if_needed,
)
from lecture_stt.shared.paths import default_log_dir  # noqa: E402


def _default_launchd_log_paths() -> list[Path]:
    log_root = default_log_dir()
    return [
        log_root / "launchd.out.log",
        log_root / "launchd.err.log",
        log_root / "downstream.out.log",
        log_root / "downstream.err.log",
        log_root / "cleanup.out.log",
        log_root / "cleanup.err.log",
        log_root / "webpanel.out.log",
        log_root / "webpanel.err.log",
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run/apply bounded rotation for lecture_stt launchd stdout/stderr logs."
    )
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="Plain log path to rotate. Repeatable. Defaults to ~/Library/Logs/lecture_stt launchd stdout/stderr files.",
    )
    parser.add_argument(
        "--archive-root",
        action="append",
        default=[],
        help="Directory/file containing compressed *.gz archives to prune by age. Repeatable.",
    )
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_LOG_MAX_BYTES)
    parser.add_argument("--backup-count", type=int, default=DEFAULT_LAUNCHD_BACKUP_COUNT)
    parser.add_argument("--archive-retain-days", type=int, default=DEFAULT_ARCHIVE_RETENTION_DAYS)
    parser.add_argument("--apply", action="store_true", help="Mutate logs. Default is dry-run only.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dry_run = not args.apply
    paths = [Path(item) for item in args.path] or _default_launchd_log_paths()

    print(f"dry_run={dry_run}")
    for path in paths:
        plan = rotate_plain_log_if_needed(
            path,
            max_bytes=args.max_bytes,
            backup_count=args.backup_count,
            dry_run=dry_run,
        )
        status = "rotate" if plan.should_rotate else "keep"
        outcome = "applied" if plan.rotated else "planned" if plan.should_rotate else "unchanged"
        print(
            f"{status}: {path} size={plan.current_size} "
            f"max_bytes={plan.max_bytes} backups={plan.backup_count} outcome={outcome}"
        )
        for action in plan.actions:
            print(f"  - {action}")

    archive_roots = [Path(item) for item in args.archive_root]
    if archive_roots:
        archives = prune_compressed_archives(
            archive_roots,
            max_age_days=args.archive_retain_days,
            dry_run=dry_run,
        )
        action = "would prune" if dry_run else "pruned"
        for archive in archives:
            print(f"{action}: {archive}")
        print(f"compressed_archives_{action.replace(' ', '_')}={len(archives)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
