from __future__ import annotations

import io
import json
import os
import time
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

RAW_SENTINEL = "DO_NOT_LEAK_RAW_FIXTURE_BODY"


def _make_operator_fixture(root: Path) -> Path:
    lecture_root = root / "lecture_recordings"
    for name in ("02_transcripts", "03_correction", "04_summarize", "05_prompt"):
        (lecture_root / name).mkdir(parents=True, exist_ok=True)
    prompt_dir = lecture_root / "05_prompt"
    (prompt_dir / "00_base_prompt.txt").write_text("base correction prompt", encoding="utf-8")
    (prompt_dir / "01_common_glossary.txt").write_text("common glossary", encoding="utf-8")
    (prompt_dir / "glossary_DS.txt").write_text("Data Science glossary", encoding="utf-8")
    return lecture_root


def _write_raw_pair(lecture_root: Path, stem: str, body: str = RAW_SENTINEL) -> None:
    transcript_dir = lecture_root / "02_transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / f"{stem}.txt").write_text(body, encoding="utf-8")
    payload = {
        "language": "ko",
        "segments": [
            {"id": 1, "start": 0.0, "end": 1.0, "text": body},
            {"id": 2, "start": 1.0, "end": 2.0, "text": "second segment"},
        ],
    }
    (transcript_dir / f"{stem}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_quality_scorecard(lecture_root: Path, stem: str, *, health: str, malformed: bool = False) -> None:
    transcript_dir = lecture_root / "02_transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"{stem}.quality.json"
    if malformed:
        path.write_text("{not-json", encoding="utf-8")
        return
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "lecture_stt_quality_scorecard",
                "canonical_base": stem,
                "health": health,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_correction_pair(lecture_root: Path, stem: str) -> None:
    correction_dir = lecture_root / "03_correction"
    correction_dir.mkdir(parents=True, exist_ok=True)
    (correction_dir / f"{stem}.txt").write_text("corrected transcript", encoding="utf-8")
    payload = {
        "language": "ko",
        "segments": [
            {"id": 1, "start": 0.0, "end": 1.0, "text": "corrected transcript"},
            {"id": 2, "start": 1.0, "end": 2.0, "text": "second segment"},
        ],
    }
    (correction_dir / f"{stem}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _valid_summary_markdown() -> str:
    return (
        "# 강의\n\n"
        "## 핵심 개요\n본문\n\n"
        "## 주요 개념\n본문\n\n"
        "## 세부 내용\n본문\n\n"
        "## 예시 / 코드 / 수식\n본문\n\n"
        "## 헷갈리기 쉬운 점\n본문\n\n"
        "## 시험·과제·교수 강조사항\n본문\n\n"
        "## 복습 질문\n본문\n"
    )


def _passing_validation_report() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "passed": True,
            "failure_class": None,
            "message": "validation passed",
            "details": {},
            "raw_transcript_body_included": False,
        },
        ensure_ascii=False,
    )


def _write_summary(lecture_root: Path, stem: str) -> None:
    summary_dir = lecture_root / "04_summarize"
    summary_dir.mkdir(parents=True, exist_ok=True)
    (summary_dir / f"{stem}.md").write_text(_valid_summary_markdown(), encoding="utf-8")


class HermesPostprocessCandidateTests(unittest.TestCase):
    def test_skip_candidate_when_raw_pair_is_temporary_or_stale(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_1", body=RAW_SENTINEL)
            _write_raw_pair(lecture_root, "260505DS_2", body=RAW_SENTINEL)

            now = time.time()
            transcript_dir = lecture_root / "02_transcripts"
            os.utime(transcript_dir / "260504DS_1.txt", (now - 1200, now - 1200))
            os.utime(transcript_dir / "260504DS_1.json", (now - 1200, now - 1200))
            os.utime(transcript_dir / "260505DS_2.txt", (now - 1, now - 1))
            os.utime(transcript_dir / "260505DS_2.json", (now - 1, now - 1))
            (transcript_dir / "260506DS_3.txt").write_text("temp", encoding="utf-8")
            (transcript_dir / "260506DS_3.json.tmp").write_text("{}", encoding="utf-8")

            candidate = find_candidate(
                lecture_root=lecture_root, repo_root=root, stable_for_sec=60, skip_claimed=True
            )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.stem, "260504DS_1")

    def test_skip_claimed_stems_by_default(self) -> None:
        from scripts.hermes_postprocess.paths import resolve_candidate_paths
        from scripts.hermes_postprocess.picker import find_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_1")
            _write_raw_pair(lecture_root, "260505DS_2")
            claimed = resolve_candidate_paths("260504DS_1", lecture_root=lecture_root, repo_root=root)
            claimed.claim_path.parent.mkdir(parents=True, exist_ok=True)
            claimed.claim_path.write_text("{}", encoding="utf-8")

            candidate = find_candidate(
                lecture_root=lecture_root, repo_root=root, stable_for_sec=0, skip_claimed=True
            )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.stem, "260505DS_2")

    def test_selects_one_oldest_incomplete_raw_pair_and_skips_complete_outputs(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_1")
            _write_correction_pair(lecture_root, "260504DS_1")
            _write_summary(lecture_root, "260504DS_1")
            _write_raw_pair(lecture_root, "260504DS_2")
            _write_raw_pair(lecture_root, "260505OOP_1")

            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.stem, "260504DS_2")
        self.assertEqual(candidate.subject, "DS")
        self.assertTrue(candidate.actions["correction"]["needed"])
        self.assertTrue(candidate.actions["summary"]["needed"])
        self.assertEqual(candidate.actions["summary"]["input_source"], "staging_correction")

    def test_marks_summary_only_when_final_correction_pair_exists(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            _write_correction_pair(lecture_root, "260504DS_2")

            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertFalse(candidate.actions["correction"]["needed"])
        self.assertTrue(candidate.actions["summary"]["needed"])
        self.assertEqual(candidate.actions["summary"]["input_source"], "final_correction")

    def test_returns_no_candidate_when_every_raw_pair_has_outputs(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            _write_correction_pair(lecture_root, "260504DS_2")
            _write_summary(lecture_root, "260504DS_2")

            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)

        self.assertIsNone(candidate)

    def test_skips_bad_quality_candidate_and_selects_next_stem(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            _write_quality_scorecard(lecture_root, "260504DS_2", health="bad")
            _write_raw_pair(lecture_root, "260505OOP_1")

            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.stem, "260505OOP_1")

    def test_malformed_quality_scorecard_fails_closed_for_that_stem_only(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            _write_quality_scorecard(lecture_root, "260504DS_2", health="warn", malformed=True)
            _write_raw_pair(lecture_root, "260505OOP_1")

            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.stem, "260505OOP_1")

    def test_quality_scorecard_without_valid_health_fails_closed_for_that_stem(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            scorecard_path = lecture_root / "02_transcripts" / "260504DS_2.quality.json"
            scorecard_path.write_text('{"schema_version": 1, "health": 7}', encoding="utf-8")
            _write_raw_pair(lecture_root, "260505OOP_1")

            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.stem, "260505OOP_1")

    def test_resolves_contract_paths_and_longest_subject_abbreviation(self) -> None:
        from scripts.hermes_postprocess.paths import resolve_candidate_paths

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            paths = resolve_candidate_paths("260410DStr_1", lecture_root=lecture_root, repo_root=root)

        self.assertEqual(paths.subject, "DStr")
        self.assertEqual(paths.raw_txt_path, lecture_root / "02_transcripts" / "260410DStr_1.txt")
        self.assertEqual(paths.raw_json_path, lecture_root / "02_transcripts" / "260410DStr_1.json")
        self.assertEqual(paths.correction_txt_path, lecture_root / "03_correction" / "260410DStr_1.txt")
        self.assertEqual(paths.correction_json_path, lecture_root / "03_correction" / "260410DStr_1.json")
        self.assertEqual(paths.summary_md_path, lecture_root / "04_summarize" / "260410DStr_1.md")
        self.assertEqual(paths.prompt_dir, lecture_root / "05_prompt")
        self.assertEqual(paths.staging_dir, root / "state" / "hermes_postprocess" / "staging" / "260410DStr_1")
        self.assertEqual(paths.claim_path, root / "state" / "hermes_postprocess" / "claims" / "260410DStr_1.json")
        self.assertEqual(paths.operator_docs_dir, root / "docs" / "operators" / "hermes-postprocess")

    def test_dry_run_cli_outputs_json_without_raw_transcript_body(self) -> None:
        from scripts.hermes_postprocess.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["dry-run", "--lecture-root", str(lecture_root), "--repo-root", str(root)])
            output = stdout.getvalue()

        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["status"], "candidate")
        self.assertEqual(payload["stem"], "260504DS_2")
        self.assertNotIn(RAW_SENTINEL, output)
        self.assertNotIn("segments", output)

    def test_dry_run_cli_silent_no_candidate_returns_empty_stdout(self) -> None:
        from scripts.hermes_postprocess.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            _write_correction_pair(lecture_root, "260504DS_2")
            _write_summary(lecture_root, "260504DS_2")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        "dry-run",
                        "--lecture-root",
                        str(lecture_root),
                        "--repo-root",
                        str(root),
                        "--silent-no-candidate",
                    ]
                )
            output = stdout.getvalue()

        self.assertEqual(code, 0)
        self.assertEqual(output, "")

    def test_unresolved_env_var_in_config_does_not_silently_scan_literal_path(self) -> None:
        from scripts.hermes_postprocess.cli import _lecture_root_from_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "config.yaml").write_text(
                "paths:\n  transcript_folder: ${MISSING_LECTURE_ROOT}/02_transcripts\n",
                encoding="utf-8",
            )

            resolved = _lecture_root_from_config(root)

        self.assertIsNone(resolved)

    def test_promote_cli_reports_missing_candidate_json_as_metadata_failure(self) -> None:
        from scripts.hermes_postprocess.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-candidate.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["promote", "--candidate-json", str(missing), "--allow-promote"])
            output = stdout.getvalue()

        self.assertEqual(code, 2)
        payload = json.loads(output)
        self.assertEqual(payload["failure_class"], "missing_candidate_json")
        self.assertNotIn(RAW_SENTINEL, output)


class HermesPostprocessPromptAndStagingTests(unittest.TestCase):
    def test_prompt_loader_uses_base_common_and_optional_subject_glossary(self) -> None:
        from scripts.hermes_postprocess.prompts import load_prompt_bundle

        with tempfile.TemporaryDirectory() as tmp:
            lecture_root = _make_operator_fixture(Path(tmp))
            bundle = load_prompt_bundle(lecture_root / "05_prompt", subject="DS")
            missing_subject_bundle = load_prompt_bundle(lecture_root / "05_prompt", subject="OOP")

        self.assertEqual(bundle.base_prompt, "base correction prompt")
        self.assertEqual(bundle.common_glossary, "common glossary")
        self.assertEqual(bundle.subject_glossary, "Data Science glossary")
        self.assertIsNone(missing_subject_bundle.subject_glossary)

    def test_staging_manifest_is_metadata_only(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import write_staging_manifest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            manifest_path = write_staging_manifest(candidate)
            manifest_text = manifest_path.read_text(encoding="utf-8")

        manifest = json.loads(manifest_text)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["stem"], "260504DS_2")
        self.assertNotIn(RAW_SENTINEL, manifest_text)
        self.assertNotIn("segments", manifest_text)

    def test_promote_is_disabled_by_default_and_final_overwrite_is_blocked_when_enabled(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import PromoteError, promote_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            staging = Path(candidate.staging_dir)
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "correction.txt").write_text("new correction", encoding="utf-8")
            (staging / "correction.json").write_text((lecture_root / "02_transcripts" / "260504DS_2.json").read_text(encoding="utf-8"), encoding="utf-8")
            (staging / "summary.md").write_text(_valid_summary_markdown(), encoding="utf-8")
            (staging / "validation-correction.json").write_text(_passing_validation_report(), encoding="utf-8")
            (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")

            disabled = promote_candidate(candidate)
            self.assertFalse(disabled["passed"])
            self.assertEqual(disabled["failure_class"], "promote_disabled")
            self.assertFalse((lecture_root / "03_correction" / "260504DS_2.txt").exists())

            existing_path = lecture_root / "03_correction" / "260504DS_2.txt"
            existing_path.write_text("existing final must remain", encoding="utf-8")
            with self.assertRaises(PromoteError) as raised:
                promote_candidate(candidate, allow_promote=True)

            self.assertEqual(raised.exception.failure_class, "output_already_exists")
            self.assertEqual(existing_path.read_text(encoding="utf-8"), "existing final must remain")

    def test_summary_only_promote_requires_only_summary_validation(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import promote_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            _write_correction_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            self.assertFalse(candidate.actions["correction"]["needed"])
            self.assertTrue(candidate.actions["summary"]["needed"])

            staging = Path(candidate.staging_dir)
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "summary.md").write_text(_valid_summary_markdown(), encoding="utf-8")
            (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")
            correction_before = (lecture_root / "03_correction" / "260504DS_2.txt").read_text(encoding="utf-8")

            result = promote_candidate(candidate, allow_promote=True)

            self.assertTrue(result["passed"])
            self.assertTrue((lecture_root / "04_summarize" / "260504DS_2.md").exists())
            self.assertEqual((lecture_root / "03_correction" / "260504DS_2.txt").read_text(encoding="utf-8"), correction_before)

    def test_promote_refuses_blocked_partial_correction_candidate(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import PromoteError, promote_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            (lecture_root / "03_correction" / "260504DS_2.txt").write_text("partial final", encoding="utf-8")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            self.assertIn("partial_correction_final_output", candidate.blocked_reasons)
            self.assertIsNone(candidate.actions["summary"]["input_source"])

            staging = Path(candidate.staging_dir)
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "summary.md").write_text(_valid_summary_markdown(), encoding="utf-8")
            (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")

            with self.assertRaises(PromoteError) as raised:
                promote_candidate(candidate, allow_promote=True)

            self.assertEqual(raised.exception.failure_class, "partial_correction_final_output")
            self.assertFalse((lecture_root / "04_summarize" / "260504DS_2.md").exists())

    def test_promote_rolls_back_first_final_when_later_copy_fails(self) -> None:
        from scripts.hermes_postprocess import staging as staging_module
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import PromoteError, promote_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            staging = Path(candidate.staging_dir)
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "correction.txt").write_text("new correction", encoding="utf-8")
            (staging / "correction.json").write_text((lecture_root / "02_transcripts" / "260504DS_2.json").read_text(encoding="utf-8"), encoding="utf-8")
            (staging / "summary.md").write_text(_valid_summary_markdown(), encoding="utf-8")
            (staging / "validation-correction.json").write_text(_passing_validation_report(), encoding="utf-8")
            (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")
            original_copy = staging_module._atomic_copy_no_overwrite
            calls = 0

            def fail_after_first_copy(src: Path, dst: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    original_copy(src, dst)
                    return
                raise OSError("simulated later copy failure")

            with patch.object(staging_module, "_atomic_copy_no_overwrite", side_effect=fail_after_first_copy):
                with self.assertRaises(PromoteError) as raised:
                    promote_candidate(candidate, allow_promote=True)

            self.assertEqual(raised.exception.failure_class, "promote_failed")
            self.assertFalse((lecture_root / "03_correction" / "260504DS_2.txt").exists())
            self.assertFalse((lecture_root / "03_correction" / "260504DS_2.json").exists())
            self.assertFalse((lecture_root / "04_summarize" / "260504DS_2.md").exists())

    def test_promote_rejects_tampered_candidate_destination_path(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.schemas import candidate_from_dict
        from scripts.hermes_postprocess.staging import PromoteError, promote_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            staging = Path(candidate.staging_dir)
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "correction.txt").write_text("new correction", encoding="utf-8")
            (staging / "correction.json").write_text((lecture_root / "02_transcripts" / "260504DS_2.json").read_text(encoding="utf-8"), encoding="utf-8")
            (staging / "summary.md").write_text(_valid_summary_markdown(), encoding="utf-8")
            (staging / "validation-correction.json").write_text(_passing_validation_report(), encoding="utf-8")
            (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")
            tampered = candidate.to_dict()
            tampered["correction_txt_path"] = str(root / "outside-final.txt")

            with self.assertRaises(PromoteError) as raised:
                promote_candidate(candidate_from_dict(tampered), allow_promote=True)

            self.assertEqual(raised.exception.failure_class, "invalid_candidate_json")
            self.assertFalse((root / "outside-final.txt").exists())

    def test_promote_reruns_validation_and_rejects_stale_pass_report(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import PromoteError, promote_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            staging = Path(candidate.staging_dir)
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "correction.txt").write_text("new correction", encoding="utf-8")
            changed = {
                "language": "ko",
                "segments": [
                    {"id": 99, "start": 0.0, "end": 1.0, "text": "changed"},
                    {"id": 2, "start": 1.0, "end": 2.0, "text": "second segment"},
                ],
            }
            (staging / "correction.json").write_text(json.dumps(changed), encoding="utf-8")
            (staging / "summary.md").write_text(_valid_summary_markdown(), encoding="utf-8")
            (staging / "validation-correction.json").write_text(_passing_validation_report(), encoding="utf-8")
            (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")

            with self.assertRaises(PromoteError) as raised:
                promote_candidate(candidate, allow_promote=True)

            self.assertEqual(raised.exception.failure_class, "segment_metadata_changed")
            self.assertFalse((lecture_root / "03_correction" / "260504DS_2.txt").exists())


class HermesPostprocessMisrecognitionTests(unittest.TestCase):
    def test_record_misrecognition_candidates_appends_local_pending_queue_and_review_only(self) -> None:
        from scripts.hermes_postprocess.misrecognitions import record_misrecognition_candidates
        from scripts.hermes_postprocess.picker import find_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            staging = Path(candidate.staging_dir)
            staging.mkdir(parents=True, exist_ok=True)
            candidates_path = staging / "misrecognitions-candidates.json"
            candidates_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "hermes_postprocess_misrecognition_candidates",
                        "stem": "260504DS_2",
                        "subject": "DS",
                        "raw_transcript_body_included": False,
                        "candidates": [
                            {
                                "suspected_wrong": "데이터 베이스",
                                "suggested_correct": "database",
                                "scope": "subject",
                                "confidence": "medium",
                                "reason": "DS glossary 후보로 수동 검토 필요",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = record_misrecognition_candidates(candidate, candidates_path)
            second_report = record_misrecognition_candidates(candidate, candidates_path)
            pending_text = (root / "state" / "hermes_postprocess" / "misrecognitions" / "pending.jsonl").read_text(encoding="utf-8")
            review_text = (staging / "misrecognitions-review.md").read_text(encoding="utf-8")

        self.assertTrue(report["passed"])
        self.assertEqual(report["appended_count"], 1)
        self.assertEqual(second_report["appended_count"], 0)
        self.assertIn("데이터 베이스", pending_text)
        self.assertIn("database", review_text)
        self.assertNotIn(RAW_SENTINEL, pending_text)
        self.assertNotIn(RAW_SENTINEL, review_text)
        self.assertNotIn("segments", pending_text)
        self.assertNotIn("segments", review_text)

    def test_misrecognition_candidates_reject_raw_excerpt_without_leaking_it(self) -> None:
        from scripts.hermes_postprocess.cli import main
        from scripts.hermes_postprocess.picker import find_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            staging = Path(candidate.staging_dir)
            staging.mkdir(parents=True, exist_ok=True)
            candidate_json = staging / "candidate.json"
            candidate_json.write_text(json.dumps(candidate.to_dict(), ensure_ascii=False), encoding="utf-8")
            candidates_path = staging / "misrecognitions-candidates.json"
            candidates_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "hermes_postprocess_misrecognition_candidates",
                        "stem": "260504DS_2",
                        "subject": "DS",
                        "raw_transcript_body_included": False,
                        "candidates": [
                            {
                                "suspected_wrong": "데이터 베이스",
                                "suggested_correct": "database",
                                "scope": "subject",
                                "confidence": "medium",
                                "reason": "검토 필요",
                                "raw_excerpt": RAW_SENTINEL,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        "record-misrecognitions",
                        "--candidate-json",
                        str(candidate_json),
                        "--candidates-json",
                        str(candidates_path),
                    ]
                )
            output = stdout.getvalue()

        self.assertEqual(code, 2)
        payload = json.loads(output)
        self.assertEqual(payload["failure_class"], "misrecognition_report_invalid")
        self.assertNotIn(RAW_SENTINEL, output)
        self.assertNotIn("segments", output)


class HermesPostprocessValidatorTests(unittest.TestCase):
    def test_correction_validator_rejects_non_text_metadata_changes(self) -> None:
        from scripts.hermes_postprocess.validators import validate_correction_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            staging = root / "state" / "hermes_postprocess" / "staging" / "260504DS_2"
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "correction.txt").write_text("corrected", encoding="utf-8")
            original = json.loads((lecture_root / "02_transcripts" / "260504DS_2.json").read_text(encoding="utf-8"))
            original["duration"] = 10.0
            (lecture_root / "02_transcripts" / "260504DS_2.json").write_text(json.dumps(original), encoding="utf-8")
            changed = dict(original)
            changed["duration"] = 11.0
            changed["segments"] = [dict(segment) for segment in original["segments"]]
            changed["segments"][0]["text"] = "corrected text may change"
            (staging / "correction.json").write_text(json.dumps(changed), encoding="utf-8")

            result = validate_correction_artifacts(
                raw_json_path=lecture_root / "02_transcripts" / "260504DS_2.json",
                correction_txt_path=staging / "correction.txt",
                correction_json_path=staging / "correction.json",
                final_txt_path=lecture_root / "03_correction" / "260504DS_2.txt",
                final_json_path=lecture_root / "03_correction" / "260504DS_2.json",
            )

        self.assertFalse(result.passed)
        self.assertEqual(result.failure_class, "correction_non_text_metadata_changed")

    def test_correction_validator_rejects_changed_segment_metadata(self) -> None:
        from scripts.hermes_postprocess.validators import validate_correction_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            staging = root / "state" / "hermes_postprocess" / "staging" / "260504DS_2"
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "correction.txt").write_text("corrected", encoding="utf-8")
            changed = {
                "language": "ko",
                "segments": [
                    {"id": 99, "start": 0.0, "end": 1.0, "text": "changed"},
                    {"id": 2, "start": 1.0, "end": 2.0, "text": "second segment"},
                ],
            }
            (staging / "correction.json").write_text(json.dumps(changed), encoding="utf-8")

            result = validate_correction_artifacts(
                raw_json_path=lecture_root / "02_transcripts" / "260504DS_2.json",
                correction_txt_path=staging / "correction.txt",
                correction_json_path=staging / "correction.json",
                final_txt_path=lecture_root / "03_correction" / "260504DS_2.txt",
                final_json_path=lecture_root / "03_correction" / "260504DS_2.json",
            )

        self.assertFalse(result.passed)
        self.assertEqual(result.failure_class, "segment_metadata_changed")

    def test_summary_validator_rejects_too_short_summary_for_long_input(self) -> None:
        from scripts.hermes_postprocess.validators import validate_summary_artifact

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corrected_path = root / "correction.txt"
            summary_path = root / "summary.md"
            final_path = root / "final" / "summary.md"
            corrected_path.write_text("긴 강의 내용입니다. " * 250, encoding="utf-8")
            summary_path.write_text(
                "# 강의\n\n"
                "## 핵심 개요\n- 짧음\n\n"
                "## 주요 개념\n- 짧음\n\n"
                "## 세부 내용\n- 짧음\n\n"
                "## 예시 / 코드 / 수식\n- 짧음\n\n"
                "## 헷갈리기 쉬운 점\n- 짧음\n\n"
                "## 시험·과제·교수 강조사항\n- 짧음\n\n"
                "## 복습 질문\n- 짧음\n",
                encoding="utf-8",
            )

            result = validate_summary_artifact(
                summary_md_path=summary_path,
                corrected_txt_path=corrected_path,
                final_md_path=final_path,
            )

        self.assertFalse(result.passed)
        self.assertEqual(result.failure_class, "summary_too_short")


if __name__ == "__main__":
    unittest.main()
