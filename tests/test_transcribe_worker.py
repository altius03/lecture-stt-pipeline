from __future__ import annotations

from pathlib import Path
import errno
import os
import shutil
import tempfile
import wave
import unittest
from unittest import mock

from lecture_stt.shared.temporary_wav import TemporaryWav, WORKSPACE_PREFIX
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

    def test_failed_model_or_decoder_removes_partial_wav(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = STTWorker(self._params(), "/usr/bin/true", tmp)
            source = root / "input.m4a"
            wav = root / "example.wav"
            with mock.patch.object(
                worker, "transcribe", side_effect=RuntimeError("engine interrupted")
            ), self.assertRaises(RuntimeError):
                worker.transcribe_file(source, "example")
            self.assertFalse(wav.exists())
            self.assertEqual(list(root.glob(WORKSPACE_PREFIX + "*")), [])
            self.assertFalse(worker._temporary_wavs)

            def failed_decoder(cmd, **kwargs):
                Path(cmd[-1]).write_bytes(b"partial")
                return mock.Mock(returncode=1, stderr="decode error")

            with mock.patch("subprocess.run", side_effect=failed_decoder), self.assertRaises(RuntimeError):
                worker.preprocess(source, "example")
            self.assertFalse(wav.exists())
            self.assertEqual(list(root.glob(WORKSPACE_PREFIX + "*")), [])
            self.assertFalse(worker._temporary_wavs)

            wav.write_bytes(b"original WAV recording")
            with mock.patch("subprocess.run", side_effect=failed_decoder), self.assertRaises(RuntimeError):
                worker.preprocess(wav, "example")
            self.assertEqual(wav.read_bytes(), b"original WAV recording")
            self.assertEqual(list(root.glob("*.wav")), [wav])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
    def test_ffmpeg_publishes_seekable_pcm_in_the_reserved_inode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input.wav"
            with wave.open(str(source), "wb") as fixture:
                fixture.setparams((1, 2, 16000, 0, "NONE", ""))
                fixture.writeframes(bytes(32000))
            worker = STTWorker(self._params(), shutil.which("ffmpeg"), tmp)
            wav = worker.preprocess(source, "input")
            self.addCleanup(worker.cleanup_tmp, wav)
            self.assertEqual(worker._wav_duration_sec(wav), 1.0)
            witness, = wav.parent.glob("owner-*")
            self.assertTrue(witness.samefile(wav))
            self.assertFalse(source.samefile(wav))
            worker.cleanup_tmp(wav)
            self.assertFalse(wav.parent.exists())
            self.assertEqual(source.stat().st_nlink, 1)

    def test_no_hardlink_support_uses_live_handle_for_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = STTWorker(self._params(), "/usr/bin/true", tmp)
            with mock.patch.object(os, "link", side_effect=OSError(errno.ENOTSUP, "unsupported")):
                wav = worker.preprocess(root / "input.m4a", "input")
            self.assertEqual(list(wav.parent.glob("owner-*")), [])
            worker.cleanup_tmp(wav)
            self.assertFalse(wav.parent.exists())

    def test_unsupported_lock_preserves_orphan_without_stopping_retention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = STTWorker(self._params(), "/usr/bin/true", tmp)
            with mock.patch("fcntl.flock", side_effect=OSError(errno.ENOTSUP, "unavailable")):
                wav = worker.preprocess(root / "input.m4a", "input")
            self.assertEqual(list(wav.parent.glob("owner-*")), [])
            worker.cleanup_tmp(wav)
            self.assertFalse(wav.parent.exists())

            wav = worker.preprocess(root / "another.m4a", "another")
            witness, = wav.parent.glob("owner-*")
            with mock.patch("fcntl.flock", side_effect=OSError(errno.EPERM, "unavailable")):
                self.assertIsNone(TemporaryWav.reopen(wav.parent, witness))
            self.assertTrue(wav.exists())
            worker.cleanup_tmp(wav)

    def test_worker_cleanup_keeps_replacement_and_unowned_wavs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = STTWorker(self._params(), "/usr/bin/true", tmp)
            source = root / "source.m4a"
            source.write_bytes(b"source")
            wav = worker.preprocess(source, "lecture")
            replacement = root / "replacement.wav"
            replacement.write_bytes(b"independent")
            replacement.replace(wav)
            worker.cleanup_tmp(wav)
            self.assertEqual(wav.read_bytes(), b"independent")
            worker.cleanup_tmp(source)
            self.assertEqual(source.read_bytes(), b"source")

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
