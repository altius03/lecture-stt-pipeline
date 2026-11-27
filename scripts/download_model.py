"""Prepare the same Whisper model and paths as the configured worker."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from dotenv import load_dotenv
from faster_whisper import WhisperModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from lecture_stt.shared.paths import env_file, resolve_config_path
from lecture_stt.stt.main import _normalize_config_paths, load_config, validate_config
from lecture_stt.stt.profiles import merge_transcribe_config, resolve_profile


def prepare(config_path: str, *, create_folders: bool) -> None:
    load_dotenv(env_file(ROOT), override=False)
    cfg = _normalize_config_paths(load_config(config_path))
    if create_folders:
        Path(cfg["paths"]["watch_folder"]).mkdir(parents=True, exist_ok=True)
        cfg = validate_config(config_path, cfg)
        delivery = cfg.get("transcript_delivery") or {}
        downstream = cfg.get("downstream") or {}
        for stage, fallback in (("correction_staging_dir", "correction_folder"),
                                ("summary_staging_dir", "summary_folder")):
            path = delivery.get(stage) or downstream.get(fallback)
            if path:
                resolve_config_path(str(path)).mkdir(parents=True, exist_ok=True)
        Path(cfg["logging"]["file"]).parent.mkdir(parents=True, exist_ok=True)
    transcribe = merge_transcribe_config(cfg["transcribe"], resolve_profile(cfg))
    WhisperModel(transcribe["model_size"], device=transcribe["device"], compute_type=transcribe["compute_type"])
    print("Model ready:", transcribe["model_size"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config" / "config.yaml"))
    parser.add_argument("--create-folders", action="store_true")
    args = parser.parse_args()
    prepare(args.config, create_folders=args.create_folders)
