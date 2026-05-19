from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_benchmark_module():
    path = REPO_ROOT / "scripts" / "benchmark_models.py"
    spec = importlib.util.spec_from_file_location("benchmark_models", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BenchmarkModelsScriptTests(unittest.TestCase):
    def test_default_plan_runs_only_current_baseline(self) -> None:
        module = _load_benchmark_module()

        plan = module.build_benchmark_plan(
            config_model="large-v3",
            requested_models=None,
            allow_candidates=False,
        )

        self.assertEqual([item.model_id for item in plan], ["large-v3"])
        self.assertTrue(plan[0].is_baseline)

    def test_candidate_models_require_explicit_approval_flag(self) -> None:
        module = _load_benchmark_module()

        with self.assertRaises(module.CandidateApprovalRequired):
            module.build_benchmark_plan(
                config_model="large-v3",
                requested_models=["large-v3", "large-v3-turbo"],
                allow_candidates=False,
            )

    def test_vad_parameters_support_faster_whisper_11_and_12(self) -> None:
        module = _load_benchmark_module()
        params = {"vad_threshold": 0.55, "min_silence_duration_ms": 1200}

        class Vad11:
            def __init__(self, onset=0.5, offset=0.35, min_silence_duration_ms=2000):
                pass

        class Vad12:
            def __init__(self, threshold=0.5, min_silence_duration_ms=2000):
                pass

        self.assertEqual(
            module.build_vad_parameters(params, Vad11),
            {"min_silence_duration_ms": 1200, "onset": 0.55, "offset": 0.4},
        )
        self.assertEqual(
            module.build_vad_parameters(params, Vad12),
            {"min_silence_duration_ms": 1200, "threshold": 0.55},
        )

    def test_runtime_metadata_records_package_versions_and_config_hash(self) -> None:
        module = _load_benchmark_module()
        metadata = module.runtime_metadata(REPO_ROOT / "config" / "config.example.yaml")

        self.assertIn("python", metadata)
        self.assertIn("platform", metadata)
        self.assertIn("faster-whisper", metadata["packages"])
        self.assertRegex(metadata["config_sha256"], r"^[0-9a-f]{64}$")

    def test_transcribe_config_parses_boolean_strings(self) -> None:
        module = _load_benchmark_module()

        config = module.transcribe_config(
            {
                "transcribe": {
                    "vad_filter": "false",
                    "word_timestamps": "0",
                    "condition_on_previous_text": "yes",
                }
            }
        )

        self.assertFalse(config["vad_filter"])
        self.assertFalse(config["word_timestamps"])
        self.assertTrue(config["condition_on_previous_text"])

    def test_transcribe_config_rejects_invalid_boolean_strings(self) -> None:
        module = _load_benchmark_module()

        with self.assertRaises(ValueError):
            module.transcribe_config({"transcribe": {"vad_filter": "maybe"}})

    def test_actual_benchmark_requires_output_path(self) -> None:
        module = _load_benchmark_module()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("transcribe:\n  model_size: large-v3\n", encoding="utf-8")

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                status = module.main(["--config", str(config_path), "--audio", str(Path(tmp) / "sample.m4a")])

        self.assertEqual(status, 2)
        self.assertIn("require --output", stderr.getvalue())

    def test_segment_quality_summary_flags_repetition_and_timestamp_regression(self) -> None:
        module = _load_benchmark_module()
        segments = [
            {"start": 0.0, "end": 1.0, "text": "자료구조 자료구조 자료구조"},
            {"start": 0.9, "end": 2.0, "text": "그래프 그래프"},
            {"start": 2.0, "end": 2.0, "text": ""},
        ]

        summary = module.summarize_segments(segments, "자료구조 자료구조 자료구조\n그래프 그래프")

        self.assertEqual(summary["segment_count"], 3)
        self.assertEqual(summary["empty_segment_count"], 1)
        self.assertEqual(summary["timestamp_regression_count"], 1)
        self.assertGreater(summary["repeated_bigram_ratio"], 0.0)


if __name__ == "__main__":
    unittest.main()
