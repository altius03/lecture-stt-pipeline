#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import shutil
import sys

import yaml
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.shared.paths import env_file, resolve_config_path


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
            "tmp_dir": str(REPO_ROOT / "tmp"),
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
    # 삭제 대상이 루트 디렉터리 밖이면 즉시 중단한다.
    candidate = candidate.resolve()
    base = base.resolve()
    if not candidate.is_relative_to(base):
        raise ValueError(f"Refusing to delete outside base directory: {candidate} not under {base}")


def _delete_path(path: Path, dry_run: bool) -> bool:
    # dry-run이면 실제 삭제 없이 대상만 출력한다.
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
    # 컷오프 이전의 오디오 파일만 제거한다.
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
    # 유지할 최소 개수를 제외하고 오래된 트랜스크립트만 정리한다.
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
    # 임시 파일/폴더 잔여물을 정리한다.
    if not tmp_dir.exists():
        return 0
    if not tmp_dir.is_dir():
        raise ValueError(f"Tmp path is not a directory: {tmp_dir}")

    deleted = 0
    preserved_dirs = {"inbox_staging"}
    for item in tmp_dir.iterdir():
        _assert_under_base(item, tmp_dir)
        if item.is_file() and _delete_path(item, dry_run):
            deleted += 1
        elif item.is_dir():
            if item.name in preserved_dirs:
                print(f"preserved tmp directory: {item}")
                continue
            if not dry_run:
                shutil.rmtree(item)
                print(f"removed directory: {item}")
                deleted += 1
            else:
                print(f"[dry-run] would remove directory: {item}")

    return deleted


def main() -> None:
    # argparse로 dry-run/apply 모드를 받아 보존 기준에 따라 삭제를 실행한다.
    parser = argparse.ArgumentParser(description="Cleanup old lecture STT artifacts")
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "config" / "config.yaml"),
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
