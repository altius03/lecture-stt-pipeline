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
    model_size: str
    device: str
    compute_type: str
    language: str
    task: str
    beam_size: int
    vad_filter: bool
    word_timestamps: bool


class STTWorker:
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

    def preprocess(self, src_audio: Path, canonical_base: str) -> Path:
        if not os.path.exists(self.ffmpeg_path):
            raise FileNotFoundError(f"ffmpeg not found at {self.ffmpeg_path}")

        wav_path = self.tmp_dir / f"{canonical_base}.wav"
        if wav_path.exists():
            wav_path.unlink()

        cmd = [
            self.ffmpeg_path,
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
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
        return wav_path

    def transcribe(self, wav_path: Path) -> Tuple[List[dict], str]:
        segments_payload: List[dict] = []
        texts = []

        segments, _ = self.model.transcribe(
            str(wav_path),
            language=self.params.language,
            task=self.params.task,
            beam_size=self.params.beam_size,
            vad_filter=self.params.vad_filter,
            word_timestamps=self.params.word_timestamps,
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
        preprocess_started = time.perf_counter()
        wav_path = self.preprocess(src_audio, canonical_base)
        preprocess_sec = time.perf_counter() - preprocess_started

        transcribe_started = time.perf_counter()
        segments, transcript_text = self.transcribe(wav_path)
        transcribe_sec = time.perf_counter() - transcribe_started

        return segments, transcript_text, preprocess_sec, transcribe_sec, wav_path

    def cleanup_tmp(self, wav_path: Path) -> None:
        try:
            if wav_path.exists():
                wav_path.unlink()
        except Exception:
            logger.warning("Failed to clean temp file %s", wav_path, exc_info=True)

