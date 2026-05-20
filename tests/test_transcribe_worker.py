from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from lecture_stt.stt.transcribe import EngineParams, STTWorker, build_vad_parameters


class TranscribeWorkerVadCompatibilityTests(unittest.TestCase):
    def _params(self) -> EngineParams:
        return EngineParams(
            model_size="large-v3",
            device="cpu",
            compute_type="int8",
            language="ko",
            task="transcribe",
            beam_size=5,
            vad_filter=True,
            word_timestamps=False,
            condition_on_previous_text=False,
            vad_threshold=0.55,
            min_silence_duration_ms=1200,
        )

    def test_build_vad_parameters_supports_faster_whisper_11_and_12_signatures(self) -> None:
        class Vad11:
            def __init__(self, onset=0.5, offset=0.35, min_silence_duration_ms=2000):
                pass

        class Vad12:
            def __init__(self, threshold=0.5, neg_threshold=None, min_silence_duration_ms=2000):
                pass

        params = self._params()

        self.assertEqual(
            build_vad_parameters(params, Vad11),
            {"min_silence_duration_ms": 1200, "onset": 0.55, "offset": 0.4},
        )
        self.assertEqual(
            build_vad_parameters(params, Vad12),
            {"min_silence_duration_ms": 1200, "threshold": 0.55},
        )

    def test_transcribe_passes_signature_compatible_vad_parameters_to_model(self) -> None:
        class Vad12:
            def __init__(self, threshold=0.5, min_silence_duration_ms=2000):
                pass

        class FakeModel:
            def __init__(self) -> None:
                self.kwargs = None

            def transcribe(self, _path: str, **kwargs):
                self.kwargs = kwargs
                return [], object()

        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "sample.wav"
            wav_path.write_bytes(b"not-a-real-wav")
            worker = STTWorker(self._params(), "ffmpeg", tmp, vad_options_cls=Vad12)
            fake_model = FakeModel()
            worker.model = fake_model

            worker.transcribe(wav_path)

        self.assertIsNotNone(fake_model.kwargs)
        assert fake_model.kwargs is not None
        self.assertEqual(
            fake_model.kwargs["vad_parameters"],
            {"min_silence_duration_ms": 1200, "threshold": 0.55},
        )


if __name__ == "__main__":
    unittest.main()
