from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from lecture_stt.stt.profiles import (
    legacy_unknown_snapshot,
    merge_transcribe_config,
    resolve_profile,
)


class TranscriptionProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base_transcribe = {
            "model_size": "large-v3",
            "device": "cpu",
            "compute_type": "int8",
            "language": "ko",
            "task": "transcribe",
            "beam_size": 5,
            "vad_filter": False,
            "word_timestamps": False,
            "condition_on_previous_text": True,
            "initial_prompt": "기존 강의 프롬프트",
        }

    def test_missing_profiles_preserves_legacy_behavior_and_is_traceable(self) -> None:
        profile = resolve_profile({"transcribe": dict(self.base_transcribe)})

        self.assertTrue(profile.is_legacy)
        self.assertEqual(profile.key, "legacy")
        self.assertEqual(profile.corrections, None)
        self.assertEqual(
            merge_transcribe_config(self.base_transcribe, profile),
            self.base_transcribe,
        )
        self.assertEqual(len(profile.config_sha256), 64)

    def test_active_general_profile_overrides_prompt_corrections_and_thresholds(self) -> None:
        config = {
            "transcribe": dict(self.base_transcribe),
            "profiles": {
                "active": "general",
                "definitions": {
                    "general": {
                        "version": "2026-07-23.1",
                        "transcribe": {
                            "initial_prompt": "",
                            "beam_size": 3,
                        },
                        "postprocess": {
                            "corrections": {},
                        },
                        "quality": {
                            "warn_threshold": 0.6,
                            "bad_threshold": 0.8,
                        },
                    },
                    "lecture": {
                        "version": 2,
                        "transcribe": {
                            "initial_prompt": "한국어 대학 수업 녹음입니다.",
                        },
                    },
                },
            },
        }

        profile = resolve_profile(config)
        merged = merge_transcribe_config(self.base_transcribe, profile)

        self.assertFalse(profile.is_legacy)
        self.assertEqual(profile.snapshot()["key"], "general")
        self.assertEqual(profile.snapshot()["version"], "2026-07-23.1")
        self.assertEqual(len(profile.snapshot()["config_sha256"]), 64)
        self.assertEqual(merged["initial_prompt"], "")
        self.assertEqual(merged["beam_size"], 3)
        self.assertEqual(merged["model_size"], "large-v3")
        self.assertEqual(profile.corrections, {})
        self.assertEqual(profile.warn_threshold, 0.6)
        self.assertEqual(profile.bad_threshold, 0.8)

    def test_hash_is_stable_for_equivalent_mapping_order(self) -> None:
        first = {
            "transcribe": dict(self.base_transcribe),
            "profiles": {
                "active": "general",
                "definitions": {
                    "general": {
                        "version": "1",
                        "transcribe": {"beam_size": 3, "initial_prompt": ""},
                    }
                },
            },
        }
        second = {
            "profiles": {
                "definitions": {
                    "general": {
                        "transcribe": {"initial_prompt": "", "beam_size": 3},
                        "version": "1",
                    }
                },
                "active": "general",
            },
            "transcribe": dict(self.base_transcribe),
        }

        self.assertEqual(
            resolve_profile(first).config_sha256,
            resolve_profile(second).config_sha256,
        )

    def test_rejects_unknown_keys_in_inactive_profile(self) -> None:
        config = {
            "transcribe": dict(self.base_transcribe),
            "profiles": {
                "active": "general",
                "definitions": {
                    "general": {"version": "1"},
                    "meeting": {
                        "version": "1",
                        "transcribe": {"unknown_decoder_flag": True},
                    },
                },
            },
        }

        with self.assertRaisesRegex(ValueError, "unknown keys"):
            resolve_profile(config)

    def test_rejects_invalid_threshold_order(self) -> None:
        config = {
            "transcribe": dict(self.base_transcribe),
            "profiles": {
                "active": "general",
                "definitions": {
                    "general": {
                        "version": "1",
                        "quality": {
                            "warn_threshold": 0.8,
                            "bad_threshold": 0.7,
                        },
                    }
                },
            },
        }

        with self.assertRaisesRegex(ValueError, "must be less"):
            resolve_profile(config)

    def test_pre_profile_replay_uses_explicit_unknown_snapshot(self) -> None:
        self.assertEqual(
            legacy_unknown_snapshot(),
            {
                "key": "legacy-unknown",
                "version": "unknown",
                "config_sha256": "unknown",
            },
        )

    def test_example_config_uses_general_without_a_global_lecture_prompt(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config = yaml.safe_load(
            (repo_root / "config" / "config.example.yaml").read_text(encoding="utf-8")
        )

        profile = resolve_profile(config)
        merged = merge_transcribe_config(config["transcribe"], profile)

        self.assertEqual(profile.key, "general")
        self.assertEqual(merged["initial_prompt"], "")
        self.assertNotIn("initial_prompt", config["transcribe"])
        self.assertEqual(profile.corrections, {})


if __name__ == "__main__":
    unittest.main()
