from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import time
import traceback
from typing import Any, Dict

from dotenv import load_dotenv
import yaml

import db as db
from db import (
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_PROCESSING,
)
from notifier import DiscordNotifier
from transcribe import EngineParams, STTWorker
from watcher import PollingWatcher
import utils


def load_config(config_path: str = "config/config.yaml") -> dict:
    repo_root = Path(__file__).resolve().parents[1]
    explicit = Path(config_path)
    if not explicit.is_absolute():
        explicit = repo_root / explicit

    example_path = repo_root / "config" / "config.example.yaml"

    if explicit.exists():
        path_to_load = explicit
    elif example_path.exists():
        path_to_load = example_path
        logger = logging.getLogger("lecture_stt")
        logger.warning("config/config.yaml missing. Falling back to config/config.example.yaml")
    else:
        raise FileNotFoundError("Neither config/config.yaml nor config/config.example.yaml exists")

    with open(path_to_load, "r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}

    return loaded


def _ensure_config_defaults(config: dict) -> dict:
    defaults = {
        "app": {
            "polling_interval_sec": 10,
            "stable_for_sec": 90,
            "stale_processing_hours": 6,
        },
        "paths": {
            "watch_folder": "/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/00_inbox",
            "stable_audio_folder": "/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/01_audio",
            "transcript_folder": "/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/02_transcripts",
            "error_folder": "/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/99_errors",
            "tmp_dir": "/Users/geonha/lecture_stt/tmp",
            "db_path": "/Users/geonha/lecture_stt/state/jobs.sqlite3",
        },
        "engine": {"engine": "faster-whisper"},
        "transcribe": {
            "model_size": "large-v3",
            "device": "cpu",
            "compute_type": "int8",
            "language": "ko",
            "task": "transcribe",
            "beam_size": 5,
            "vad_filter": False,
            "word_timestamps": False,
        },
        "ffmpeg": {"binary_path": "/opt/homebrew/bin/ffmpeg"},
        "logging": {
            "file": "logs/app.log",
            "max_bytes": 5 * 1024 * 1024,
            "backup_count": 5,
        },
        "cleanup": {
            "retain_days": 7,
            "retain_min_transcripts": 5,
        },
    }

    merged = defaults.copy()
    for section, values in defaults.items():
        merged[section] = {**values, **(config.get(section, {}) or {})}

    for section in defaults:
        if section not in merged:
            merged[section] = defaults[section]

    return merged


class _JobLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "job_id"):
            record.job_id = "-"
        if not hasattr(record, "canonical_base"):
            record.canonical_base = "-"
        return True


def setup_logging(log_file: str, max_bytes: int, backup_count: int) -> logging.Logger:
    logger = logging.getLogger("lecture_stt")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [job=%(job_id)s base=%(canonical_base)s] %(message)s")

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    stream_handler = logging.StreamHandler()
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
        handler.setLevel(logging.INFO)
        handler.addFilter(_JobLogFilter())

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


class STTPipeline:
    def __init__(self, config: dict, logger: logging.Logger):
        self.config = _ensure_config_defaults(config)
        self.logger = logger

        paths = self.config["paths"]
        self.watch_dir = Path(paths["watch_folder"])
        self.audio_dir = Path(paths["stable_audio_folder"])
        self.transcript_dir = Path(paths["transcript_folder"])
        self.error_dir = Path(paths["error_folder"])
        self.tmp_dir = Path(paths["tmp_dir"])
        self.db_path = Path(paths["db_path"])

        app_cfg = self.config["app"]
        self.polling_interval_sec = int(app_cfg["polling_interval_sec"])
        self.stable_for_sec = int(app_cfg["stable_for_sec"])
        self.stale_processing_hours = int(app_cfg["stale_processing_hours"])

        trans = self.config["transcribe"]
        self.params = EngineParams(
            model_size=trans["model_size"],
            device=trans["device"],
            compute_type=trans["compute_type"],
            language=trans["language"],
            task=trans["task"],
            beam_size=int(trans["beam_size"]),
            vad_filter=bool(trans["vad_filter"]),
            word_timestamps=bool(trans["word_timestamps"]),
        )

        self.worker = STTWorker(self.params, self.config["ffmpeg"]["binary_path"], str(self.tmp_dir))
        self.watcher = PollingWatcher(
            watch_folder=str(self.watch_dir),
            stable_for_sec=self.stable_for_sec,
            polling_interval_sec=self.polling_interval_sec,
        )

        self.conn = db.init_db(str(self.db_path))
        self.notifier = DiscordNotifier(os.getenv("DISCORD_WEBHOOK_URL"))

    def startup_recovery(self) -> None:
        counts = db.recover_processing_jobs(self.conn, stale_processing_hours=self.stale_processing_hours)
        if any(counts.values()):
            self._log(
                logging.INFO,
                "Recovered PROCESSING jobs: done=%s pending=%s error=%s",
                {"job_id": "-", "canonical_base": "-"},
                counts["done"],
                counts["pending"],
                counts["error"],
            )

    def _log(self, level: int, message: str, job_ctx: Dict[str, str], *args: Any) -> None:
        self.logger.log(level, message, *args, extra=job_ctx)

    def _job_paths(self, source_path: Path) -> Dict[str, Any]:
        base = f"{utils.local_timestamp()}__{utils.sanitize_stem(source_path.stem)}__{utils.short_id(8)}"
        return {
            "canonical_base": base,
            "canonical_audio_path": self.audio_dir / f"{base}{source_path.suffix.lower()}",
            "transcript_txt_path": self.transcript_dir / f"{base}.txt",
            "transcript_json_path": self.transcript_dir / f"{base}.json",
        }

    def _metadata(self, canonical_base: str, started_at: str, ended_at: str, preprocess_sec: float,
                  transcribe_sec: float, total_sec: float, orig_inbox_path: str,
                  orig_name: str, canonical_audio_path: str, transcript_txt_path: str,
                  transcript_json_path: str, deduped: bool = False,
                  deduped_from_job_id: int | None = None,
                  source_sha256: str | None = None) -> Dict[str, Any]:
        payload = {
            "orig_name": orig_name,
            "orig_inbox_path": orig_inbox_path,
            "canonical_audio_path": canonical_audio_path,
            "transcript_txt_path": transcript_txt_path,
            "transcript_json_path": transcript_json_path,
            "engine": "faster-whisper",
            "model_size": self.params.model_size,
            "device": self.params.device,
            "compute_type": self.params.compute_type,
            "language": self.params.language,
            "task": self.params.task,
            "beam_size": self.params.beam_size,
            "vad_filter": self.params.vad_filter,
            "word_timestamps": self.params.word_timestamps,
            "timings": {
                "preprocess_sec": round(float(preprocess_sec), 6),
                "transcribe_sec": round(float(transcribe_sec), 6),
                "total_sec": round(float(total_sec), 6),
                "started_at": started_at,
                "ended_at": ended_at,
            },
            "deduped": deduped,
            "canonical_base": canonical_base,
        }
        if deduped_from_job_id is not None:
            payload["deduped_from_job_id"] = deduped_from_job_id
        if source_sha256 is not None:
            payload["source_sha256"] = source_sha256
        return payload

    def _write_output(self, txt_path: Path, json_path: Path, segments: list, text: str,
                      metadata: Dict[str, Any]) -> None:
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        utils.atomic_write(txt_path, text)
        utils.atomic_write(json_path, {"segments": segments, "metadata": metadata})

    def _validate_output_files(self, txt_path: Path, json_path: Path) -> None:
        if not txt_path.exists() or not json_path.exists():
            raise FileNotFoundError("Missing transcript output file after write")
        with open(json_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("Transcript JSON payload is not a dict")
        segments = payload.get("segments")
        if not isinstance(segments, list):
            raise ValueError("Transcript JSON missing segments array")

    def _replay_existing_job(self, job_id: int, canonical_base: str, source_path: Path,
                            canonical_audio_path: Path, txt_path: Path, json_path: Path,
                            duplicate) -> bool:
        prior_txt = Path(duplicate["transcript_txt_path"])
        prior_json = Path(duplicate["transcript_json_path"])
        try:
            if not prior_txt.exists() or not prior_json.exists():
                return False

            with open(prior_json, "r", encoding="utf-8") as handle:
                prior_payload = json.load(handle)
            if not isinstance(prior_payload, dict):
                return False

            segments = prior_payload.get("segments")
            if not isinstance(segments, list):
                return False

            shutil.copy2(prior_txt, txt_path)
            text = "\n".join(
                [
                    str(item.get("text", "")).strip()
                    for item in segments
                    if str(item.get("text", "")).strip()
                ]
            )

            started_at = utils.now_iso()
            ended_at = utils.now_iso()
            metadata = self._metadata(
                canonical_base=canonical_base,
                started_at=started_at,
                ended_at=ended_at,
                preprocess_sec=0.0,
                transcribe_sec=0.0,
                total_sec=0.0,
                orig_inbox_path=str(source_path),
                orig_name=source_path.name,
                canonical_audio_path=str(canonical_audio_path),
                transcript_txt_path=str(txt_path),
                transcript_json_path=str(json_path),
                deduped=True,
                deduped_from_job_id=int(duplicate["id"]),
                source_sha256=duplicate["sha256"],
            )

            self._write_output(txt_path, json_path, segments, text, metadata)
            self._validate_output_files(txt_path, json_path)

            db.update_job(
                self.conn,
                job_id,
                status=STATUS_DONE,
                ended_at=ended_at,
                preprocess_sec=0.0,
                transcribe_sec=0.0,
                total_sec=0.0,
                engine_params=json.dumps(metadata),
                is_deduped=1,
                deduped_from_job_id=int(duplicate["id"]),
                canonical_audio_path=str(canonical_audio_path),
                transcript_txt_path=str(txt_path),
                transcript_json_path=str(json_path),
            )
            return True
        except (OSError, json.JSONDecodeError, ValueError):
            for replay_file in (txt_path, json_path):
                try:
                    replay_file.unlink()
                except FileNotFoundError:
                    pass
            return False

    def _move_to_errors(self, canonical_audio: Path) -> Path:
        self.error_dir.mkdir(parents=True, exist_ok=True)
        target = self.error_dir / canonical_audio.name
        if target.exists():
            target = self.error_dir / f"{canonical_audio.stem}__error__{utils.short_id(6)}{canonical_audio.suffix}"
        utils.safe_move_file(canonical_audio, target)
        return target

    def process_job(self, source_path: Path) -> None:
        paths = self._job_paths(source_path)
        canonical_base = paths["canonical_base"]
        canonical_audio = paths["canonical_audio_path"]
        txt_path = paths["transcript_txt_path"]
        json_path = paths["transcript_json_path"]
        job_ctx = {"job_id": "-", "canonical_base": canonical_base}

        utils.ensure_dir(self.audio_dir)
        utils.ensure_dir(self.transcript_dir)

        job_id = db.create_job(
            self.conn,
            status=STATUS_PENDING,
            orig_inbox_path=str(source_path),
            orig_name=source_path.name,
            canonical_base=canonical_base,
            canonical_audio_path=str(canonical_audio),
            transcript_txt_path=str(txt_path),
            transcript_json_path=str(json_path),
            engine_params={
                "engine": "faster-whisper",
                "model_size": self.params.model_size,
                "device": self.params.device,
                "compute_type": self.params.compute_type,
                "language": self.params.language,
                "task": self.params.task,
                "beam_size": self.params.beam_size,
                "vad_filter": self.params.vad_filter,
                "word_timestamps": self.params.word_timestamps,
            },
        )
        job_ctx["job_id"] = str(job_id)

        canonical_audio_final: Path | None = None
        tmp_wav: Path | None = None

        try:
            if not source_path.exists():
                raise FileNotFoundError(f"Source file disappeared before move: {source_path}")

            utils.safe_move_file(source_path, canonical_audio)
            canonical_audio_final = canonical_audio
            sha256 = utils.compute_sha256(canonical_audio_final)
            db.update_job(
                self.conn,
                job_id,
                canonical_audio_path=str(canonical_audio_final),
                sha256=sha256,
            )

            self._log(logging.INFO, "moved to stable folder", job_ctx)

            duplicate = db.find_done_job_by_sha(self.conn, sha256)
            if duplicate and duplicate["id"] != job_id:
                if self._replay_existing_job(
                    job_id=job_id,
                    canonical_base=canonical_base,
                    source_path=source_path,
                    canonical_audio_path=canonical_audio_final,
                    txt_path=txt_path,
                    json_path=json_path,
                    duplicate=duplicate,
                ):
                    self._log(logging.INFO, "dedupe completed", job_ctx)
                    return

            if not db.claim_job_for_processing(self.conn, job_id):
                raise RuntimeError(f"Failed to claim job {job_id} as PROCESSING")
            started_at = utils.now_iso()
            self._log(logging.INFO, "transcription started", job_ctx)

            segments, transcript_text, preprocess_sec, transcribe_sec, tmp_wav = self.worker.transcribe_file(
                canonical_audio_final,
                canonical_base,
            )
            total_sec = preprocess_sec + transcribe_sec
            ended_at = utils.now_iso()

            metadata = self._metadata(
                canonical_base=canonical_base,
                started_at=started_at,
                ended_at=ended_at,
                preprocess_sec=preprocess_sec,
                transcribe_sec=transcribe_sec,
                total_sec=total_sec,
                orig_inbox_path=str(source_path),
                orig_name=source_path.name,
                canonical_audio_path=str(canonical_audio_final),
                transcript_txt_path=str(txt_path),
                transcript_json_path=str(json_path),
            )
            self._write_output(txt_path, json_path, segments, transcript_text, metadata)
            self._validate_output_files(txt_path, json_path)

            db.update_job(
                self.conn,
                job_id,
                status=STATUS_DONE,
                ended_at=ended_at,
                preprocess_sec=preprocess_sec,
                transcribe_sec=transcribe_sec,
                total_sec=total_sec,
                engine_params=json.dumps(metadata),
            )
            self._log(logging.INFO, "done", job_ctx)

        except Exception as exc:
            tb = traceback.format_exc()
            self.logger.exception("job processing failed")
            error_audio = canonical_audio_final
            if canonical_audio_final and canonical_audio_final.exists():
                error_audio = self._move_to_errors(canonical_audio_final)

            db.update_job(
                self.conn,
                job_id,
                status=STATUS_ERROR,
                ended_at=utils.now_iso(),
                error_message=str(exc),
                error_trace=tb,
                canonical_audio_path=str(error_audio) if error_audio else str(canonical_audio),
            )

            self.notifier.notify_error({
                "job_id": job_id,
                "orig_name": source_path.name,
                "canonical_base": canonical_base,
                "orig_inbox_path": str(source_path),
                "canonical_audio_path": str(error_audio) if error_audio else str(canonical_audio),
                "error_message": str(exc),
            })
            self._log(logging.ERROR, "failed", job_ctx)
        finally:
            if tmp_wav:
                self.worker.cleanup_tmp(tmp_wav)

    def run(self, run_once: bool = False) -> None:
        self.startup_recovery()
        self._log(logging.INFO, "pipeline start", {"job_id": "-", "canonical_base": "-"})

        while True:
            stable_files = self.watcher.scan_stable_files()
            if not stable_files:
                if run_once:
                    return
                time.sleep(self.polling_interval_sec)
                continue

            for path in stable_files:
                self.process_job(path)
                if run_once:
                    return

            if not run_once:
                time.sleep(self.polling_interval_sec)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Always-on lecture STT worker")
    parser.add_argument("--once", action="store_true", help="Process at most one stable file")
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config.yaml",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv("/Users/geonha/lecture_stt/.env", override=False)
    config = _ensure_config_defaults(load_config(args.config))

    logging_cfg = config["logging"]
    logger = setup_logging(
        logging_cfg["file"],
        int(logging_cfg["max_bytes"]),
        int(logging_cfg["backup_count"]),
    )

    logger.info("Loaded configuration")
    STTPipeline(config=config, logger=logger).run(run_once=args.once)


if __name__ == "__main__":
    main()
