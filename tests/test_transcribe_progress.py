from __future__ import annotations

import math
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
import types

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

if "faster_whisper" not in sys.modules:
    sys.modules["faster_whisper"] = types.SimpleNamespace(WhisperModel=object)

from lecture_stt.stt.transcribe import STTWorker


class DummyModel:
    def __init__(self, segments: list[SimpleNamespace]):
        self._segments = segments

    def transcribe(self, *_args, **_kwargs):
        return iter(self._segments), None


class TranscribeProgressTests(unittest.TestCase):
    def _make_worker(self, segments: list[SimpleNamespace]) -> STTWorker:
        worker = object.__new__(STTWorker)
        worker.params = SimpleNamespace(
            language="ko",
            task="transcribe",
            beam_size=5,
            vad_filter=False,
            word_timestamps=False,
            condition_on_previous_text=True,
            repetition_penalty=1.15,
            no_repeat_ngram_size=4,
            initial_prompt="",
        )
        worker.model = DummyModel(segments)
        return worker

    def _write_wav(self, path: Path, duration_sec: float) -> None:
        sample_rate = 16000
        frame_count = int(sample_rate * duration_sec)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(b"\x00\x00" * frame_count)

    def test_transcribe_reports_progress_with_remaining_eta(self) -> None:
        segments = [
            SimpleNamespace(id=0, start=0.0, end=24.0, text="첫 문장"),
            SimpleNamespace(id=1, start=24.0, end=48.0, text="둘째 문장"),
            SimpleNamespace(id=2, start=48.0, end=60.0, text="마지막 문장"),
        ]
        worker = self._make_worker(segments)
        progress_updates: list[tuple[float, float | None, int | None]] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = Path(tmpdir) / "sample.wav"
            self._write_wav(wav_path, duration_sec=60.0)

            payload, transcript = worker.transcribe(
                wav_path,
                progress_callback=lambda processed, total, remaining: progress_updates.append(
                    (processed, total, remaining)
                ),
            )

        self.assertEqual(len(payload), 3)
        self.assertEqual(transcript, "첫 문장\n둘째 문장\n마지막 문장")
        self.assertTrue(progress_updates)
        self.assertAlmostEqual(progress_updates[-1][0], 60.0, delta=0.5)
        self.assertAlmostEqual(progress_updates[-1][1] or 0.0, 60.0, delta=0.5)
        self.assertEqual(progress_updates[-1][2], 0)
        self.assertTrue(any(update[2] is None or update[2] >= 0 for update in progress_updates))
        self.assertTrue(all(math.isfinite(update[0]) for update in progress_updates))
