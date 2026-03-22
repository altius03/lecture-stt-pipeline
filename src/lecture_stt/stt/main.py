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

from lecture_stt.shared import db, utils
from lecture_stt.shared.db import (
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_PENDING,
    STATUS_PROCESSING,
)
from lecture_stt.shared.paths import default_db_path, env_file, repo_root
from lecture_stt.stt.notifier import DiscordNotifier
from lecture_stt.stt.postprocess import postprocess
from lecture_stt.stt.quality_gate import evaluate as quality_evaluate
from lecture_stt.stt.transcribe import EngineParams, STTWorker
from lecture_stt.stt.watcher import PollingWatcher


# 설정 파일을 읽고 기본 형식 유효성을 검사한다.
def load_config(config_path: str = "config/config.yaml") -> dict:
    root = repo_root()
    explicit = Path(config_path)
    if not explicit.is_absolute():
        explicit = root / explicit
    if not explicit.exists():
        raise FileNotFoundError(f"Missing required config file: {explicit}")
    if explicit.stat().st_size == 0:
        raise ValueError(f"Config file is empty: {explicit}")

    with open(explicit, "r", encoding="utf-8") as f:
        try:
            loaded = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise ValueError(f"Failed to parse YAML in {explicit}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ValueError(f"Config must be a YAML mapping in {explicit}")
    if not loaded:
        raise ValueError(f"Config file has no content: {explicit}")

    return loaded

def validate_config(config_path: str, config: dict) -> dict:
    # 설정 섹션/타입/값 범위를 검증해 실행 시 실패를 앞당긴다.
    config_path_obj = Path(config_path)

    def require_section(name: str) -> Dict[str, Any]:
        section = config.get(name)
        if not isinstance(section, dict):
            raise ValueError(f"Config error in {config_path_obj}: missing/invalid section '{name}'")
        return section

    def parse_bool(name: str, value: Any) -> bool:
        # 문자열/정수 입력이 섞여도 의도한 불리언으로 안전하게 해석한다.
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "y", "on"}:
                return True
            if normalized in {"0", "false", "no", "n", "off"}:
                return False
        raise ValueError(f"Config error: {name} must be a boolean")

    app = require_section("app")
    paths = require_section("paths")
    ffmpeg_cfg = require_section("ffmpeg")
    transcribe = require_section("transcribe")

    required_app = ["polling_interval_sec", "stable_for_sec", "stale_processing_hours"]
    for key in required_app:
        if key not in app:
            raise ValueError(f"Config error in {config_path_obj}: missing key '{key}' under 'app'")

    try:
        polling_interval_sec = int(app["polling_interval_sec"])
        stable_for_sec = int(app["stable_for_sec"])
        stale_processing_hours = int(app["stale_processing_hours"])
    except Exception as exc:
        raise ValueError("Config error: app polling/stable/stale values must be integers") from exc

    if polling_interval_sec <= 0:
        raise ValueError("Config error: app.polling_interval_sec must be greater than 0")
    if stable_for_sec <= 0:
        raise ValueError("Config error: app.stable_for_sec must be greater than 0")
    if stale_processing_hours <= 0:
        raise ValueError("Config error: app.stale_processing_hours must be greater than 0")

    app["polling_interval_sec"] = polling_interval_sec
    app["stable_for_sec"] = stable_for_sec
    app["stale_processing_hours"] = stale_processing_hours

    required_paths = [
        "watch_folder",
        "stable_audio_folder",
        "transcript_folder",
        "error_folder",
        "tmp_dir",
        "db_path",
    ]
    for key in required_paths:
        if key not in paths:
            raise ValueError(f"Config error in {config_path_obj}: missing key '{key}' under 'paths'")
        value = paths[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Config error: paths.{key} must be a non-empty string")

    watch_folder = Path(paths["watch_folder"])
    if not watch_folder.exists():
        raise FileNotFoundError(
            f"Config error: watch_folder does not exist: {watch_folder} (create it and rerun)"
        )
    if not watch_folder.is_dir():
        raise NotADirectoryError(f"Config error: watch_folder is not a directory: {watch_folder}")

    writable_dirs = [
        ("paths.stable_audio_folder", Path(paths["stable_audio_folder"])),
        ("paths.transcript_folder", Path(paths["transcript_folder"])),
        ("paths.error_folder", Path(paths["error_folder"])),
        ("paths.tmp_dir", Path(paths["tmp_dir"])),
    ]
    for label, directory in writable_dirs:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PermissionError(f"Config error: cannot create {label} directory {directory}") from exc
        probe = directory / f".lecture_stt_write_probe_{os.getpid()}"
        try:
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise PermissionError(f"Config error: cannot write in {label} directory {directory}") from exc

    db_parent = Path(paths["db_path"]).parent
    try:
        db_parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PermissionError(f"Config error: cannot create db_path parent directory {db_parent}") from exc
    state_log_dir = db_parent / "logs"
    try:
        state_log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PermissionError(
            f"Config error: cannot create state logs directory {state_log_dir}"
        ) from exc
    try:
        probe = state_log_dir / f".lecture_stt_state_probe_{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise PermissionError(f"Config error: cannot write in state logs directory {state_log_dir}") from exc

    if "binary_path" not in ffmpeg_cfg:
        raise ValueError(f"Config error in {config_path_obj}: missing key 'binary_path' under 'ffmpeg'")
    ffmpeg_path = Path(ffmpeg_cfg["binary_path"])
    if not ffmpeg_path.exists():
        raise FileNotFoundError(f"Config error: ffmpeg binary not found: {ffmpeg_path}")
    if not os.access(ffmpeg_path, os.X_OK):
        raise PermissionError(f"Config error: ffmpeg binary not executable: {ffmpeg_path}")

    required_transcribe = [
        "model_size",
        "device",
        "compute_type",
        "language",
        "task",
        "beam_size",
        "vad_filter",
        "word_timestamps",
    ]
    for key in required_transcribe:
        if key not in transcribe:
            raise ValueError(f"Config error in {config_path_obj}: missing key '{key}' under 'transcribe'")

    for key in ["model_size", "device", "compute_type", "language", "task"]:
        if not isinstance(transcribe[key], str) or not str(transcribe[key]).strip():
            raise ValueError(f"Config error: transcribe.{key} must be a non-empty string")

    try:
        beam_size = int(transcribe["beam_size"])
        transcribe["beam_size"] = beam_size
    except Exception as exc:
        raise ValueError("Config error: transcribe.beam_size must be an integer") from exc
    if beam_size <= 0:
        raise ValueError("Config error: transcribe.beam_size must be greater than 0")
    transcribe["vad_filter"] = parse_bool("transcribe.vad_filter", transcribe["vad_filter"])
    transcribe["word_timestamps"] = parse_bool("transcribe.word_timestamps", transcribe["word_timestamps"])
    if "condition_on_previous_text" in transcribe:
        transcribe["condition_on_previous_text"] = parse_bool(
            "transcribe.condition_on_previous_text", transcribe["condition_on_previous_text"]
        )

    return _ensure_config_defaults(config)


# 환경설정에서 누락된 값은 기본값으로 채워 코드 실행 안정성을 높인다.
def _ensure_config_defaults(config: dict) -> dict:
    root = repo_root()
    defaults = {
        "app": {
            "polling_interval_sec": 10,
            "stable_for_sec": 90,
            "stale_processing_hours": 6,
        },
        "paths": {
            "watch_folder": "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/00_inbox",
            "stable_audio_folder": "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/01_audio",
            "transcript_folder": "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/02_transcripts",
            "error_folder": "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/99_errors",
            "tmp_dir": str(root / "tmp"),
            "db_path": str(default_db_path()),
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
            "condition_on_previous_text": True,
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
    # 로그 필드 기본값을 보강해 포맷 에러 없이 추적 정보를 일관되게 남긴다.
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "job_id"):
            record.job_id = "-"
        if not hasattr(record, "canonical_base"):
            record.canonical_base = "-"
        return True


# 실행 로그를 파일/콘솔로 동일 형식으로 남기고 핸들러 중복 등록을 방지한다.
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
    # 전체 전사 워크플로우의 오케스트레이션을 담당한다.
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
            condition_on_previous_text=bool(trans["condition_on_previous_text"]),
            # --- v2 추가 ---
            repetition_penalty=float(trans.get("repetition_penalty", 1.15)),
            no_repeat_ngram_size=int(trans.get("no_repeat_ngram_size", 4)),
            vad_threshold=float(trans.get("vad_threshold", 0.55)),
            min_silence_duration_ms=int(trans.get("min_silence_duration_ms", 1200)),
            initial_prompt=str(trans.get("initial_prompt", "")),
        )

        self.worker = STTWorker(self.params, self.config["ffmpeg"]["binary_path"], str(self.tmp_dir))
        self.watcher = PollingWatcher(
            watch_folder=str(self.watch_dir),
            stable_for_sec=self.stable_for_sec,
            polling_interval_sec=self.polling_interval_sec,
        )

        self.pause_sleep_sec = max(30, self.polling_interval_sec)
        self._was_paused = False
        self._last_pause_log_at = 0.0

        self.conn = db.init_db(str(self.db_path))
        self.notifier = DiscordNotifier(
            os.getenv("DISCORD_WEBHOOK_URL"),
            state_dir=self.db_path.parent,
        )

    @staticmethod
    def _to_stage_progress(step: str) -> int:
        """단계 이름 기반 기본 진행률 (시간 기반 계산 불가 시 폴백)."""
        stage_progress = {
            "파일 감지": 5,
            "파일 이동": 10,
            "중복 결과 재사용": 40,
            "전사 시작/진행": 15,
            "후처리(반복/노이즈 제거)": 75,
            "품질 검사": 85,
            "전사문 생성(TXT/JSON)": 90,
            "전체 완료": 100,
            "실패": 0,
        }
        return stage_progress.get(step, 5)

    def _time_based_progress(self, job_id: int, eta_sec: int | None) -> int | None:
        """started_at 기준 경과 시간 / 예상 총 시간으로 진행률을 계산한다."""
        if eta_sec is None or eta_sec <= 0:
            return None
        try:
            row = self.conn.execute(
                "SELECT started_at FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row or not row["started_at"]:
                return None
            from datetime import datetime
            started = datetime.fromisoformat(row["started_at"])
            now = datetime.now().astimezone()
            elapsed = (now - started).total_seconds()
            if elapsed < 0:
                return None
            pct = int((elapsed / eta_sec) * 100)
            # 15~95% 범위로 제한 (시작/완료 단계는 별도 처리)
            return max(15, min(95, pct))
        except Exception:
            return None

    def _queue_status(self) -> dict[str, int]:
        try:
            return db.get_status_counts(self.conn)
        except Exception:
            return {"PENDING": 0, "PROCESSING": 0, "DONE": 0, "ERROR": 0}

    def _update_progress(self, job_id: int, step: str, progress: int | None = None, eta_sec: int | None = None) -> None:
        values = {"current_step": step}
        if progress is not None:
            values["progress_pct"] = max(0, min(100, int(progress)))
        elif step == "전사 시작/진행":
            # 전사 중에는 시간 기반 진행률을 우선 사용한다.
            current_eta = eta_sec
            if current_eta is None:
                try:
                    row = self.conn.execute("SELECT eta_sec FROM jobs WHERE id = ?", (job_id,)).fetchone()
                    current_eta = row["eta_sec"] if row and row["eta_sec"] else None
                except Exception:
                    current_eta = None
            time_pct = self._time_based_progress(job_id, current_eta)
            values["progress_pct"] = time_pct if time_pct is not None else self._to_stage_progress(step)
        else:
            values["progress_pct"] = self._to_stage_progress(step)
        if eta_sec is not None:
            values["eta_sec"] = eta_sec
        db.update_job(self.conn, job_id, **values)

    def _estimate_eta_sec(self) -> int | None:
        rows = self.conn.execute(
            "SELECT total_sec FROM jobs WHERE status = ? AND total_sec IS NOT NULL ORDER BY ended_at DESC LIMIT 3",
            (STATUS_DONE,),
        ).fetchall()
        samples = [float(row["total_sec"]) for row in rows if row["total_sec"] is not None]
        if not samples:
            return None
        return max(1, int(sum(samples) / len(samples)))

    # 시작 시 PROCESSING으로 남은 작업을 정리해 중복 처리/중단 상태를 회복한다.
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

    # 공통 로그 헬퍼: 작업 컨텍스트를 함께 출력한다.
    def _log(self, level: int, message: str, job_ctx: Dict[str, str], *args: Any) -> None:
        self.logger.log(level, message, *args, extra=job_ctx)

    # 소스 파일 기준으로 유일한 base와 경로들을 생성한다.
    # 원본 파일명이 앞에 와서 전사물에서 원본을 쉽게 식별할 수 있다.
    def _job_paths(self, source_path: Path) -> Dict[str, Any]:
        safe_stem = utils.sanitize_stem(source_path.stem)

        # 기본: 원본 파일명(sanitize만 적용) 그대로 사용
        base_candidate = safe_stem

        # 충돌 검사: audio 또는 transcript 폴더에 동명 파일이 이미 있으면 suffix 부착
        audio_target = self.audio_dir / f"{base_candidate}{source_path.suffix.lower()}"
        txt_target = self.transcript_dir / f"{base_candidate}.txt"
        if audio_target.exists() or txt_target.exists():
            base_candidate = f"{safe_stem}__{utils.local_timestamp()}__{utils.short_id(6)}"

        return {
            "canonical_base": base_candidate,
            "canonical_audio_path": self.audio_dir / f"{base_candidate}{source_path.suffix.lower()}",
            "transcript_txt_path": self.transcript_dir / f"{base_candidate}.txt",
            "transcript_json_path": self.transcript_dir / f"{base_candidate}.json",
        }

    def _metadata(self, canonical_base: str, started_at: str, ended_at: str, preprocess_sec: float,
                  transcribe_sec: float, total_sec: float, orig_inbox_path: str,
                  orig_name: str, canonical_audio_path: str, transcript_txt_path: str,
                  transcript_json_path: str, deduped: bool = False,
                  deduped_from_job_id: int | None = None,
                  source_sha256: str | None = None) -> Dict[str, Any]:
        # 결과물과 처리 시간을 묶는 메타데이터를 한 곳에서 구성한다.
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
            "condition_on_previous_text": self.params.condition_on_previous_text,
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
        # 텍스트와 JSON 결과를 원자적 쓰기로 저장한다.
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        utils.atomic_write(txt_path, text)
        utils.atomic_write(json_path, {"segments": segments, "metadata": metadata})

    # 산출물 존재/구조를 최소 검증해 손상된 결과를 바로 감지한다.
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
        # 동일 파일의 기존 결과를 복제해 처리 시간을 절약한다.
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
            self._update_progress(job_id, "전사문 생성(TXT/JSON)")

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
                current_step="전체 완료",
                progress_pct=100,
                eta_sec=0,
            )
            queue = self._queue_status()
            self.notifier.notify_transcript_generated({
                "job_id": job_id,
                "orig_name": source_path.name,
                "canonical_base": canonical_base,
                "pending_count": queue.get("PENDING", 0),
                "processing_count": queue.get("PROCESSING", 0),
            })
            self.notifier.notify_success({
                "job_id": job_id,
                "orig_name": source_path.name,
                "canonical_base": canonical_base,
                "orig_inbox_path": str(source_path),
                "canonical_audio_path": str(canonical_audio_path),
                "transcript_txt_path": str(txt_path),
                "transcript_json_path": str(json_path),
                "elapsed_sec": 0.0,
                "pending_count": queue.get("PENDING", 0),
                "processing_count": queue.get("PROCESSING", 0),
                "updated_at": utils.now_iso(),
            })
            self.notifier.notify_completed({
                "job_id": job_id,
                "orig_name": source_path.name,
                "canonical_base": canonical_base,
                "pending_count": queue.get("PENDING", 0),
                "processing_count": queue.get("PROCESSING", 0),
            })
            return True
        except (OSError, json.JSONDecodeError, ValueError):
            for replay_file in (txt_path, json_path):
                try:
                    replay_file.unlink()
                except FileNotFoundError:
                    pass
            return False

    # 실패한 오디오를 에러 폴더로 이동시켜 후속 추적 대상만 남긴다.
    def _move_to_errors(self, canonical_audio: Path) -> Path:
        self.error_dir.mkdir(parents=True, exist_ok=True)
        target = self.error_dir / canonical_audio.name
        if target.exists():
            target = self.error_dir / f"{canonical_audio.stem}__error__{utils.short_id(6)}{canonical_audio.suffix}"
        utils.safe_move_file(canonical_audio, target)
        return target

    # 하나의 파일에 대해 이동, 중복 처리, 전사, 저장, 알림까지 수행한다.
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
                "condition_on_previous_text": self.params.condition_on_previous_text,
            },
            current_step="파일 감지",
            progress_pct=10,
        )
        job_ctx["job_id"] = str(job_id)
        self._update_progress(job_id, "파일 감지")
        queue = self._queue_status()
        self.notifier.notify_detected({
            "job_id": job_id,
            "orig_name": source_path.name,
            "canonical_base": canonical_base,
            "pending_count": queue.get("PENDING", 0),
            "processing_count": queue.get("PROCESSING", 0),
        })

        canonical_audio_final: Path | None = None
        tmp_wav: Path | None = None
        # 실패 발생 시 어떤 단계에서 중단되었는지 추적해 장애 분석에 바로 활용한다.
        fail_step = "파이프라인 시작"

        try:
            if not source_path.exists():
                # 파일 이동 전에 원본이 존재하는지 먼저 확인한다. 없으면 즉시 실패 처리한다.
                fail_step = "입력 파일 검증"
                self._update_progress(job_id, "파일 감지")
                raise FileNotFoundError(f"Source file disappeared before move: {source_path}")

            # 안정적인 경로로 음원 파일을 옮겨 후속 처리를 시작한다.
            fail_step = "파일 이동"
            self._update_progress(job_id, "파일 이동", 20)
            utils.safe_move_file(source_path, canonical_audio)
            canonical_audio_final = canonical_audio
            sha256 = utils.compute_sha256(canonical_audio_final)
            db.update_job(
                self.conn,
                job_id,
                canonical_audio_path=str(canonical_audio_final),
                sha256=sha256,
            )

            queue = self._queue_status()
            self.notifier.notify_moved({
                "job_id": job_id,
                "orig_name": source_path.name,
                "canonical_base": canonical_base,
                "pending_count": queue.get("PENDING", 0),
                "processing_count": queue.get("PROCESSING", 0),
            })

            self._log(logging.INFO, "moved to stable folder", job_ctx)
            self._update_progress(job_id, "파일 이동", 30)

            duplicate = db.find_done_job_by_sha(self.conn, sha256)
            if duplicate and duplicate["id"] != job_id:
                # 이미 변환 완료된 동일 파일이 있으면 결과를 재사용해 중복 작업 시간을 줄인다.
                fail_step = "중복 결과 재사용"
                self._update_progress(job_id, "중복 결과 재사용", 40)
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
                # 상태를 PROCESSING으로 바꿔 다른 워커가 같은 작업을 중복 처리하지 않게 막는다.
                fail_step = "처리 상태 전환"
                raise RuntimeError(f"Failed to claim job {job_id} as PROCESSING")
            started_at = utils.now_iso()
            eta = self._estimate_eta_sec()
            self._update_progress(job_id, "전사 시작/진행", None, eta)
            self._log(logging.INFO, "transcription started", job_ctx)

            # Whisper 전사 단계: 오디오에서 텍스트를 추출한다.
            fail_step = "전사 실행"
            segments, transcript_text, preprocess_sec, transcribe_sec, tmp_wav = self.worker.transcribe_file(
                canonical_audio_final,
                canonical_base,
            )
            total_sec = preprocess_sec + transcribe_sec
            ended_at = utils.now_iso()

            # ── v2: 후처리 + 품질 게이트 ──
            fail_step = "후처리"
            self._update_progress(job_id, "후처리(반복/노이즈 제거)", 70)
            segments, transcript_text = postprocess(segments, transcript_text)

            fail_step = "품질 검사"
            quality_report = quality_evaluate(segments, transcript_text)
            self._log(
                logging.WARNING if quality_report.health != "good" else logging.INFO,
                f"quality: {quality_report.summary}",
                job_ctx,
            )
            # ── v2 끝 ──

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
            # v2: 품질 보고서를 metadata에 포함
            metadata["quality"] = quality_report.to_dict()

            # 생성된 텍스트/메타데이터를 디스크에 저장하고 구조를 검증한다.
            fail_step = "산출물 저장/검증"
            self._write_output(txt_path, json_path, segments, transcript_text, metadata)
            self._validate_output_files(txt_path, json_path)
            self._update_progress(job_id, "전사문 생성(TXT/JSON)", 85)
            self.notifier.notify_transcript_generated({
                "job_id": job_id,
                "orig_name": source_path.name,
                "canonical_base": canonical_base,
                "pending_count": self._queue_status().get("PENDING", 0),
                "processing_count": self._queue_status().get("PROCESSING", 0),
            })

            # DB 상태를 DONE으로 마무리하고 처리 시간을 기록한다.
            fail_step = "최종 상태 갱신"
            db.update_job(
                self.conn,
                job_id,
                status=STATUS_DONE,
                ended_at=ended_at,
                preprocess_sec=preprocess_sec,
                transcribe_sec=transcribe_sec,
                total_sec=total_sec,
                engine_params=json.dumps(metadata),
                current_step="전체 완료",
                progress_pct=100,
                eta_sec=0,
            )
            # 최종 산출물 생성이 완료되면 성공 알림을 전송한다.
            fail_step = "알림 전송"
            queue = self._queue_status()
            self.notifier.notify_success({
                "job_id": job_id,
                "orig_name": source_path.name,
                "canonical_base": canonical_base,
                "orig_inbox_path": str(source_path),
                "canonical_audio_path": str(canonical_audio_final),
                "transcript_txt_path": str(txt_path),
                "transcript_json_path": str(json_path),
                "elapsed_sec": total_sec,
                "pending_count": queue.get("PENDING", 0),
                "processing_count": queue.get("PROCESSING", 0),
                "updated_at": ended_at,
                # --- v2 추가 ---
                "quality_health": quality_report.health,
                "quality_summary": quality_report.summary,
            })
            self.notifier.notify_completed({
                "job_id": job_id,
                "orig_name": source_path.name,
                "canonical_base": canonical_base,
                "pending_count": queue.get("PENDING", 0),
                "processing_count": queue.get("PROCESSING", 0),
            })
            self._log(logging.INFO, "done", job_ctx)

        except Exception as exc:
            tb = traceback.format_exc()
            self.logger.exception("job processing failed")
            # 예상치 못한 예외는 방어적으로 Unknown으로 남겨 원인 분류가 누락되지 않게 한다.
            fail_step = fail_step or "Unknown"
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
                current_step=f"실패: {fail_step}",
                progress_pct=0,
                eta_sec=None,
            )
            self._update_progress(job_id, f"실패: {fail_step}", 0, None)
            queue = self._queue_status()

            self.notifier.notify_error({
                "job_id": job_id,
                "orig_name": source_path.name,
                "canonical_base": canonical_base,
                "orig_inbox_path": str(source_path),
                "canonical_audio_path": str(error_audio) if error_audio else str(canonical_audio),
                "transcript_txt_path": str(txt_path),
                "transcript_json_path": str(json_path),
                "error_message": str(exc),
                "error_step": fail_step,
                "pending_count": queue.get("PENDING", 0),
                "processing_count": queue.get("PROCESSING", 0),
            })
            self._log(logging.ERROR, "failed", job_ctx)
        finally:
            if tmp_wav:
                self.worker.cleanup_tmp(tmp_wav)

    def run(self, run_once: bool = False) -> None:
        # 파이프라인을 루프 또는 한 번 처리 모드로 실행하고 pause 상태를 반영한다.
        self.startup_recovery()
        self._log(logging.INFO, "pipeline start", {"job_id": "-", "canonical_base": "-"})

        while True:
            if utils.is_paused(self.config):
                now = utils.now()
                if not self._was_paused:
                    self._was_paused = True
                    self._last_pause_log_at = now
                    self._log(logging.INFO, "pipeline paused", {"job_id": "-", "canonical_base": "-"})
                elif now - self._last_pause_log_at >= 60:
                    self._last_pause_log_at = now
                    self._log(logging.INFO, "pipeline paused", {"job_id": "-", "canonical_base": "-"})

                if run_once:
                    msg = "Paused: skipping --once"
                    print(msg)
                    self._log(logging.INFO, msg, {"job_id": "-", "canonical_base": "-"})
                    return

                time.sleep(self.pause_sleep_sec)
                continue
            if self._was_paused:
                self._was_paused = False
                self._last_pause_log_at = 0.0
                self._log(logging.INFO, "pipeline resumed", {"job_id": "-", "canonical_base": "-"})

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
    # CLI 모드를 파싱해 제어 동작과 실행 모드를 구분한다.
    parser = argparse.ArgumentParser(description="Always-on lecture STT worker")
    parser.add_argument("--once", action="store_true", help="Process at most one stable file")
    parser.add_argument("--watch-folder", dest="watch_folder", help="Override paths.watch_folder in runtime")
    control_group = parser.add_mutually_exclusive_group()
    control_group.add_argument("--pause", action="store_true", help="Pause scanning/processing")
    control_group.add_argument("--resume", action="store_true", help="Resume scanning/processing")
    control_group.add_argument("--status", action="store_true", help="Print pause status and exit")
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config.yaml",
    )
    return parser.parse_args()


def main() -> None:
    # 진입점: 제어 커맨드를 우선 처리하고, 기본 실행은 파이프라인 시작이다.
    args = parse_args()
    load_dotenv(str(env_file()), override=False)

    if args.pause or args.resume or args.status:
        control_config = _ensure_config_defaults(load_config(args.config))
        if args.pause:
            utils.set_paused(True, control_config)
            print(f"Paused: pause flag created at {utils.get_pause_flag_path(control_config)}")
            return
        if args.resume:
            utils.set_paused(False, control_config)
            print(f"Resumed: pause flag removed at {utils.get_pause_flag_path(control_config)}")
            return

        paused = utils.is_paused(control_config)
        state = "PAUSED" if paused else "RESUMED"
        print(f"Pipeline status: {state}")
        print(f"Pause flag: {utils.get_pause_flag_path(control_config)}")
        return

    config = load_config(args.config)
    if args.watch_folder:
        config.setdefault("paths", {})["watch_folder"] = args.watch_folder
    config = validate_config(args.config, config)
    logging_cfg = config["logging"]
    logger = setup_logging(
        logging_cfg["file"],
        int(logging_cfg["max_bytes"]),
        int(logging_cfg["backup_count"]),
    )

    logger.info("Loaded and validated configuration from %s", args.config)
    STTPipeline(config=config, logger=logger).run(run_once=args.once)


if __name__ == "__main__":
    main()
