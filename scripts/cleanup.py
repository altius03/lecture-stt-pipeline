#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
from datetime import datetime
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import sys
import unicodedata

import yaml
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.shared.paths import default_tmp_dir, env_file, resolve_config_path
from lecture_stt.shared.temporary_wav import TemporaryWav, WORKSPACE_PREFIX, is_private_workspace, witness_name


def _load_config(config_path: str = str(REPO_ROOT / "config" / "config.yaml")) -> dict:
    # 실행에 필요한 설정을 읽는다.
    config_path = resolve_config_path(config_path, base_dir=REPO_ROOT)
    if not config_path.exists():
        raise FileNotFoundError(f"Missing config file: {config_path}")

    return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}


def _ensure_config_defaults(config: dict) -> dict:
    # 누락 값은 기본값으로 채워 정리 작업이 중단되지 않게 한다.
    defaults = {
        "paths": {
            "tmp_dir": str(default_tmp_dir()),
            "db_path": str(REPO_ROOT / "state" / "jobs.sqlite3"),
        },
        "cleanup": {
            "retain_days": 7,
        },
    }

    merged = dict(config)
    for section, values in defaults.items():
        merged[section] = {**values, **(config.get(section, {}) or {})}
    return merged


def _retired_wavs(
    conn: sqlite3.Connection, delivery_enabled: bool
) -> dict[str, tuple[Path, str]]:
    """Return inactive temp WAV names with their canonical source and digest."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conditions = ["j.status = 'DONE'"]
    for table in ("transcript_postprocess_jobs", "transcript_deliveries"):
        if table in tables:
            conditions.append(
                f"NOT EXISTS (SELECT 1 FROM {table} d WHERE d.source_job_id=j.id "
                "AND d.status IS NOT 'DELIVERED')"
            )
    if "deliveries" in tables:
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.source_job_id=j.id AND d.completed_at IS NULL)"
        )
    if delivery_enabled:
        if "transcript_postprocess_jobs" not in tables:
            return {}
        conditions.append("EXISTS (SELECT 1 FROM transcript_postprocess_jobs d WHERE d.source_job_id=j.id)")
    rows = conn.execute(
        "SELECT canonical_base, canonical_audio_path, sha256, "
        + " AND ".join(conditions) + " FROM jobs j"
    )
    candidates: dict[str, tuple[str, Path, str]] = {}
    seen: set[str] = set()
    for stem, audio, digest, retired in rows:
        if not stem or Path(stem).name != stem:
            continue
        name = unicodedata.normalize("NFC", f"{stem}.wav").casefold()
        if name in seen:
            candidates.pop(name, None)
        elif retired and audio and digest:
            candidates[name] = (stem, Path(audio), digest)
        seen.add(name)
    return {witness_name(source, stem): (source, digest) for stem, source, digest in candidates.values()}


def _verified_source(source: Path, audio_dir: Path, digest: str, wav: TemporaryWav) -> bool:
    if source.parent.resolve() != audio_dir.resolve() or source.is_symlink() or not source.is_file():
        return False
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or os.path.samestat(before, os.fstat(wav.handle.fileno()))):
                return False
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
            after = source.lstat()
            return (actual == digest and os.path.samestat(before, after)
                    and before.st_ctime_ns == after.st_ctime_ns and before.st_size == after.st_size)
    except (FileNotFoundError, IsADirectoryError):
        return False


def _cleanup_tmp(tmp_dir: Path, audio_dir: Path, candidates: dict[str, tuple[Path, str]],
                 cutoff_ts: float, dry_run: bool) -> int:
    deleted = 0
    for directory in tmp_dir.glob(WORKSPACE_PREFIX + "*"):
        if not is_private_workspace(directory):
            continue
        for witness in directory.glob("owner-*"):
            if witness.name not in candidates or witness.is_symlink() or not witness.is_file():
                continue
            wav = TemporaryWav.reopen(directory, witness)
            if wav is None:
                continue
            with wav.handle:
                if os.fstat(wav.handle.fileno()).st_mtime >= cutoff_ts:
                    continue
                source, digest = candidates[witness.name]
                if not _verified_source(source, audio_dir, digest, wav):
                    continue
                if dry_run:
                    print(f"[dry-run] would remove: {wav.path}")
                else:
                    deleted += int(wav.remove())
    return deleted


def main() -> None:
    # argparse로 dry-run/apply 모드를 받아 보존 기준에 따라 삭제를 실행한다.
    parser = argparse.ArgumentParser(description="Cleanup old lecture STT artifacts")
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "config" / "config.yaml"),
        help="Path to config.yaml",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Preview deletions without removing anything. "
            "Use --apply to perform deletions."
        ),
    )
    mode.add_argument("--apply", action="store_true", help="Perform cleanup instead of dry-run")
    args = parser.parse_args()

    dry_run = not args.apply

    load_dotenv(str(env_file(REPO_ROOT)), override=False)
    cfg = _ensure_config_defaults(_load_config(args.config))
    paths_cfg = cfg.get("paths") or {}

    for key in ["stable_audio_folder", "transcript_folder"]:
        value = paths_cfg.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"paths.{key} must be configured for cleanup")

    audio_dir = resolve_config_path(str(paths_cfg["stable_audio_folder"]), base_dir=REPO_ROOT)
    transcript_dir = resolve_config_path(str(paths_cfg["transcript_folder"]), base_dir=REPO_ROOT)
    tmp_dir = resolve_config_path(str(paths_cfg["tmp_dir"]), base_dir=REPO_ROOT)
    db_path = resolve_config_path(str(paths_cfg["db_path"]), base_dir=REPO_ROOT)
    retain_days = int(cfg["cleanup"]["retain_days"])
    protected_dirs = [audio_dir, transcript_dir]
    for key in ("watch_folder", "error_folder"):
        if paths_cfg.get(key):
            protected_dirs.append(resolve_config_path(str(paths_cfg[key]), base_dir=REPO_ROOT))
    for protected in protected_dirs:
        if tmp_dir.resolve().is_relative_to(protected.resolve()) or protected.resolve().is_relative_to(tmp_dir.resolve()):
            parser.error("tmp_dir must be separate from input, audio, transcript and error roots")
    if retain_days < 0:
        parser.error("retention settings must be non-negative")
    print(f"dry_run={dry_run}")
    if not db_path.is_file():
        if dry_run:
            print("No jobs database; retaining all files.")
            return
        parser.exit(1, "Cannot apply cleanup without the jobs database.\n")

    cutoff_ts = datetime.now().timestamp() - (retain_days * 24 * 3600)
    with ExitStack() as stack:
        if not dry_run:
            # All STT owners take this lock before moving or processing a recording.
            lock = stack.enter_context((db_path.parent / "stt.lock").open("a+"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.exit(1, "Cleanup deferred: the STT worker is active.\n")
        access = "ro" if dry_run else "rw"
        conn = stack.enter_context(closing(sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode={access}", uri=True, timeout=3
        )))
        # Keep downstream queue transitions and cleanup decisions in one snapshot.
        conn.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
        delivery = (cfg.get("transcript_delivery") or {}).get("enabled", False)
        enabled = str(delivery).strip().lower() not in {"false", "0", "no", "off"}
        candidates = _retired_wavs(conn, enabled)
        removed_tmp = _cleanup_tmp(tmp_dir, audio_dir, candidates, cutoff_ts, dry_run)
    print(f"tmp removed={removed_tmp}. Canonical audio and transcript sets are retained.")


if __name__ == "__main__":
    main()
