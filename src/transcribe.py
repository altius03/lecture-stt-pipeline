from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from faster_whisper import WhisperModel

from utils import ensure_dir

logger = logging.getLogger(__name__)


@dataclass
class EngineParams:
    # Faster-Whisper 실행 파라미터를 구조체로 묶어 전달한다.
    model_size: str
    device: str
    compute_type: str
    language: str
    task: str
    beam_size: int
    vad_filter: bool
    word_timestamps: bool
    condition_on_previous_text: bool = True
    # --- v2 추가 ---
    repetition_penalty: float = 1.15
    no_repeat_ngram_size: int = 4
    vad_threshold: float = 0.55
    min_silence_duration_ms: int = 1200
    initial_prompt: str = ""


class STTWorker:
    # 음성 전처리부터 전사, 임시 파일 정리에 이르는 실제 작업 실행기
    def __init__(self, params: EngineParams, ffmpeg_path: str, tmp_dir: str):
        self.params = params
        self.ffmpeg_path = ffmpeg_path
        self.tmp_dir = Path(tmp_dir)
        ensure_dir(self.tmp_dir)
        self.model = WhisperModel(
            params.model_size,
            device=params.device,
            compute_type=params.compute_type,
        )

    # ffmpeg로 wav 형식으로 변환해 모델 입력 형식으로 정규화한다.
    def preprocess(self, src_audio: Path, canonical_base: str) -> Path:
        if not os.path.exists(self.ffmpeg_path):
            raise FileNotFoundError(f"ffmpeg not found at {self.ffmpeg_path}")

        wav_path = self.tmp_dir / f"{canonical_base}.wav"
        if wav_path.exists():
            wav_path.unlink()

        cmd = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostats",
            "-y",
            "-i",
            str(src_audio),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-acodec",
            "pcm_s16le",
            "-f",
            "wav",
            str(wav_path),
        ]
        logger.info("Preprocessing audio %s -> %s", src_audio, wav_path)
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            error_output = (result.stderr or "").strip()
            if len(error_output) > 2000:
                error_output = error_output[-2000:]
            raise RuntimeError(
                f"ffmpeg preprocessing failed (exit={result.returncode}): {error_output or 'unknown error'}"
            )
        return wav_path

    def transcribe(self, wav_path: Path) -> Tuple[List[dict], str]:
        # whisper 세그먼트를 dict 리스트와 결합 텍스트로 변환한다.
        segments_payload: List[dict] = []
        texts = []

        # v2: VAD 세부 설정 구성
        vad_params = None
        if self.params.vad_filter:
            vad_params = {
                "onset": self.params.vad_threshold,
                "offset": self.params.vad_threshold - 0.15,
                "min_silence_duration_ms": self.params.min_silence_duration_ms,
            }

        segments, _ = self.model.transcribe(
            str(wav_path),
            language=self.params.language,
            task=self.params.task,
            beam_size=self.params.beam_size,
            vad_filter=self.params.vad_filter,
            word_timestamps=self.params.word_timestamps,
            condition_on_previous_text=self.params.condition_on_previous_text,
            # --- v2 추가 ---
            repetition_penalty=self.params.repetition_penalty,
            no_repeat_ngram_size=self.params.no_repeat_ngram_size,
            initial_prompt=self.params.initial_prompt or None,
            **({"vad_parameters": vad_params} if vad_params else {}),
        )

        for idx, seg in enumerate(segments):
            text = (getattr(seg, "text", "") or "").strip()
            start = float(getattr(seg, "start", 0.0) or 0.0)
            end = float(getattr(seg, "end", 0.0) or 0.0)
            seg_id = getattr(seg, "id", idx)
            segments_payload.append({
                "id": int(seg_id) if isinstance(seg_id, int) else idx,
                "start": start,
                "end": end,
                "text": text,
            })
            if text:
                texts.append(text)

        return segments_payload, "\n".join(texts).strip()

    def transcribe_file(self, src_audio: Path, canonical_base: str) -> Tuple[List[dict], str, float, float, Path]:
        # 전처리+전사 시간을 함께 반환해 처리 성능 추적에 사용한다.
        preprocess_started = time.perf_counter()
        wav_path = self.preprocess(src_audio, canonical_base)
        preprocess_sec = time.perf_counter() - preprocess_started

        transcribe_started = time.perf_counter()
        segments, transcript_text = self.transcribe(wav_path)
        transcribe_sec = time.perf_counter() - transcribe_started

        return segments, transcript_text, preprocess_sec, transcribe_sec, wav_path

    def cleanup_tmp(self, wav_path: Path) -> None:
        # 임시 WAV 생성 파일을 정리해 디스크 사용을 회수한다.
        try:
            if wav_path.exists():
                wav_path.unlink()
        except Exception:
            logger.warning("Failed to clean temp file %s", wav_path, exc_info=True)
