from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lecture_stt.correction.worker import CorrectionWorker, load_correction_config


class CorrectionManualModeTests(unittest.TestCase):
    def test_load_correction_config_does_not_require_anthropic_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.yaml"
            config_path.write_text(
                "paths:\n"
                f"  transcript_folder: {root / '02_transcripts'}\n"
                "downstream:\n"
                f"  correction_folder: {root / '03_correction'}\n"
                "correction:\n"
                f"  prompt_folder: {root / '05_prompt'}\n",
                encoding="utf-8",
            )

            with mock.patch("lecture_stt.correction.worker.env_file", return_value=root / ".env"), \
                 mock.patch.dict("os.environ", {}, clear=True):
                config = load_correction_config(str(config_path))

        self.assertEqual(config.transcript_dir, root / "02_transcripts")
        self.assertEqual(config.correction_dir, root / "03_correction")
        self.assertEqual(config.prompt_dir, root / "05_prompt")
        self.assertFalse(hasattr(config, "api_key"))
        self.assertFalse(hasattr(config, "model"))

    def test_correction_worker_skips_pending_pairs_in_manual_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript_dir = root / "02_transcripts"
            correction_dir = root / "03_correction"
            prompt_dir = root / "05_prompt"
            transcript_dir.mkdir()
            correction_dir.mkdir()
            txt = transcript_dir / "260316LC_1.txt"
            js = transcript_dir / "260316LC_1.json"
            txt.write_text("raw transcript", encoding="utf-8")
            js.write_text('{"segments":[{"id":1,"start":0,"end":1,"text":"raw transcript"}]}', encoding="utf-8")

            from lecture_stt.correction.worker import CorrectionConfig

            config = CorrectionConfig(
                transcript_dir=transcript_dir,
                correction_dir=correction_dir,
                prompt_dir=prompt_dir,
                stable_for_sec=0,
                scan_interval_sec=60,
            )
            stats = CorrectionWorker(config).scan_once()

            self.assertEqual(stats, {"corrected": 0, "skipped": 1, "errors": 0})
            self.assertFalse((correction_dir / "260316LC_1.txt").exists())
            self.assertFalse((correction_dir / "260316LC_1.json").exists())

    def test_requirements_do_not_depend_on_anthropic(self) -> None:
        requirements = Path("requirements.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("anthropic", requirements)


if __name__ == "__main__":
    unittest.main()
