#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import shutil

import yaml
from dotenv import load_dotenv


def _load_config(config_path: str = "/Users/geonha/lecture_stt/config/config.yaml") -> dict:
    repo_root = Path("/Users/geonha/lecture_stt")
    config_path = Path(config_path)
    if not config_path.is_absolute():
        config_path = repo_root / config_path

    example_path = repo_root / "config" / "config.example.yaml"
    if not config_path.exists():
        if example_path.exists():
            config_path = example_path
        else:
            raise FileNotFoundError("Missing config/config.yaml and config/config.example.yaml")

    return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}


def _ensure_config_defaults(config: dict) -> dict:
    defaults = {
        "paths": {
            "stable_audio_folder": "/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/01_audio",
            "transcript_folder": "/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/02_transcripts",
            "tmp_dir": "/Users/geonha/lecture_stt/tmp",
        },
        "cleanup": {
            "retain_days": 7,
            "retain_min_transcripts": 5,
        },
    }

    merged = defaults.copy()
    for section, values in defaults.items():
        merged[section] = {**values, **(config.get(section, {}) or {})}
    return merged


def _assert_under_base(candidate: Path, base: Path) -> None:
    candidate = candidate.resolve()
    base = base.resolve()
    if not candidate.is_relative_to(base):
        raise ValueError(f"Refusing to delete outside base directory: {candidate} not under {base}")


def _delete_path(path: Path, dry_run: bool) -> bool:
    if dry_run:
        print(f"[dry-run] would remove: {path}")
        return False
    if path.is_file():
        path.unlink()
        return True
    if path.is_dir():
        shutil.rmtree(path)
        return True
    return False


def _cleanup_audio(audio_dir: Path, cutoff_ts: float, dry_run: bool) -> tuple[int, int]:
    if not audio_dir.exists():
        print(f"audio folder missing: {audio_dir}")
        return 0, 0

    if not audio_dir.is_dir():
        raise ValueError(f"Audio path is not a directory: {audio_dir}")

    deleted = 0
    kept = 0
    for item in audio_dir.iterdir():
        _assert_under_base(item, audio_dir)
        if not item.is_file():
            continue
        if item.stat().st_mtime < cutoff_ts:
            if _delete_path(item, dry_run):
                deleted += 1
        else:
            kept += 1

    return deleted, kept


def _cleanup_transcripts(transcript_dir: Path, cutoff_ts: float, dry_run: bool, min_keep: int) -> tuple[int, int]:
    if not transcript_dir.exists():
        return 0, 0
    if not transcript_dir.is_dir():
        raise ValueError(f"Transcript path is not a directory: {transcript_dir}")

    files = [entry for entry in transcript_dir.iterdir() if entry.is_file()]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    keep_paths = set(files[:min_keep]) if min_keep > 0 else set()

    deleted = 0
    kept = len(keep_paths)
    for item in files:
        _assert_under_base(item, transcript_dir)
        if item in keep_paths:
            continue
        if item.stat().st_mtime < cutoff_ts:
            if _delete_path(item, dry_run):
                deleted += 1
            continue
        kept += 1

    return deleted, kept


def _cleanup_tmp(tmp_dir: Path, dry_run: bool) -> int:
    if not tmp_dir.exists():
        return 0
    if not tmp_dir.is_dir():
        raise ValueError(f"Tmp path is not a directory: {tmp_dir}")

    deleted = 0
    for item in tmp_dir.iterdir():
        _assert_under_base(item, tmp_dir)
        if item.is_file() and _delete_path(item, dry_run):
            deleted += 1
        elif item.is_dir():
            if not dry_run:
                shutil.rmtree(item)
                print(f"removed directory: {item}")
                deleted += 1
            else:
                print(f"[dry-run] would remove directory: {item}")

    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(description="Cleanup old lecture STT artifacts")
    parser.add_argument(
        "--config",
        default="/Users/geonha/lecture_stt/config/config.yaml",
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help=(
            "Preview deletions without removing anything. "
            "Use --apply to perform deletions."
        ),
    )
    parser.add_argument("--apply", action="store_true", help="Perform cleanup instead of dry-run")
    args = parser.parse_args()

    dry_run = args.dry_run and not args.apply

    load_dotenv("/Users/geonha/lecture_stt/.env", override=False)
    cfg = _ensure_config_defaults(_load_config(args.config))

    audio_dir = Path(cfg["paths"]["stable_audio_folder"])
    transcript_dir = Path(cfg["paths"]["transcript_folder"])
    tmp_dir = Path(cfg["paths"]["tmp_dir"])
    retain_days = int(cfg["cleanup"]["retain_days"])
    min_transcripts = int(cfg.get("cleanup", {}).get("retain_min_transcripts", 5))

    cutoff_ts = datetime.now().timestamp() - (retain_days * 24 * 3600)

    print(f"dry_run={dry_run}")
    removed_audio, kept_audio = _cleanup_audio(audio_dir, cutoff_ts, dry_run)
    removed_tmp = _cleanup_tmp(tmp_dir, dry_run)
    removed_transcript, kept_transcript = _cleanup_transcripts(
        transcript_dir,
        cutoff_ts,
        dry_run,
        min_keep=min_transcripts,
    )

    print(
        f"audio removed={removed_audio}, kept={kept_audio}, "
        f"tmp removed={removed_tmp}, transcript removed={removed_transcript}, "
        f"transcripts kept(min={min_transcripts})={kept_transcript}",
    )


if __name__ == "__main__":
    main()
