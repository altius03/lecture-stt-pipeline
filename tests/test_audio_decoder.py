"""Exercise the real faster-whisper/PyAV boundary without downloading a model."""
import io
import unittest
import wave

from faster_whisper.audio import decode_audio


class DecoderTests(unittest.TestCase):
    def test_decode_mono_pcm_for_inference(self):
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", ""))
            wav.writeframes(bytes(32000))
        audio.seek(0)
        decoded = decode_audio(audio)
        self.assertEqual(decoded.shape, (16000,))
        self.assertFalse(decoded.any())
