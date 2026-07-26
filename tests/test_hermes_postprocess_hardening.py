from __future__ import annotations

import concurrent.futures
import datetime as dt
import io
import json
import os
import socket
import subprocess
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
FIXED_NOW = dt.datetime(2026, 5, 21, 12, 0, 0, tzinfo=dt.UTC)


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
        "duration": 2.0,
        "segments": [
            {"id": 1, "start": 0.0, "end": 1.0, "text": body},
            {"id": 2, "start": 1.0, "end": 2.0, "text": "second segment"},
        ],
    }
    (transcript_dir / f"{stem}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_quality_scorecard(lecture_root: Path, stem: str, *, health: str, malformed: bool = False) -> None:
    transcript_dir = lecture_root / "02_transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    scorecard_path = transcript_dir / f"{stem}.quality.json"
    if malformed:
        scorecard_path.write_text("{not-json", encoding="utf-8")
        return
    scorecard_path.write_text(
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
        "duration": 2.0,
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


def _write_valid_staging(candidate: object, lecture_root: Path) -> None:
    staging = Path(candidate.staging_dir)  # type: ignore[attr-defined]
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "correction.txt").write_text("corrected transcript", encoding="utf-8")
    raw = json.loads((lecture_root / "02_transcripts" / f"{candidate.stem}.json").read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    raw["segments"][0]["text"] = "corrected transcript"
    (staging / "correction.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    (staging / "summary.md").write_text(_valid_summary_markdown(), encoding="utf-8")


def _run_cli(args: list[str], *, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "scripts.hermes_postprocess", *args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def _assert_payload_metadata_only(case: unittest.TestCase, payload: object) -> None:
    assert isinstance(payload, dict)
    case.assertIs(payload.get("raw_transcript_body_included"), False)
    text = json.dumps(payload, ensure_ascii=False)
    case.assertNotIn(RAW_SENTINEL, text)
    case.assertNotIn("segments", text)


class HermesPostprocessOperatorHardeningTests(unittest.TestCase):
    def test_complete_nonregular_final_outputs_are_not_treated_as_complete(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, build_status, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            outside = root / "outside-final-targets"
            outside.mkdir()
            for relative in (
                "03_correction/260504DS_2.txt",
                "03_correction/260504DS_2.json",
                "04_summarize/260504DS_2.md",
            ):
                target = outside / relative.replace("/", "-")
                target.write_text("outside target", encoding="utf-8")
                final_path = lecture_root / relative
                final_path.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(target, final_path)
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)
            called = False

            def child_runner(*_args: object, **_kwargs: object) -> ChildRunResult:
                nonlocal called
                called = True
                raise AssertionError("child must not run when final outputs are non-regular entries")

            status_before = build_status(config, now=FIXED_NOW)
            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")

        self.assertEqual(status_before["candidate_counts"]["complete"], 0)
        self.assertEqual(status_before["candidate_counts"]["blocked"], 1)
        self.assertFalse(called)
        self.assertEqual(outcome["kind"], "failed")
        self.assertIn("preexisting_final_artifact_unsupported_type", claim_text)
        self.assertNotIn(RAW_SENTINEL, json.dumps(outcome, ensure_ascii=False) + claim_text)

    def test_promote_preserves_preexisting_broken_symlink_destination(self) -> None:
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
            raw = json.loads(Path(candidate.raw_json_path).read_text(encoding="utf-8"))
            raw["segments"][0]["text"] = "corrected transcript"
            (staging / "correction.txt").write_text("corrected transcript", encoding="utf-8")
            (staging / "correction.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            (staging / "summary.md").write_text(_valid_summary_markdown(), encoding="utf-8")
            (staging / "validation-correction.json").write_text(_passing_validation_report(), encoding="utf-8")
            (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")
            final_txt = Path(candidate.correction_txt_path)
            final_txt.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(root / "missing-final-target.txt", final_txt)

            with self.assertRaises(PromoteError) as caught:
                promote_candidate(candidate, allow_promote=True)
            symlink_still_exists = os.path.lexists(final_txt)
            symlink_still_symlink = final_txt.is_symlink()

        self.assertIn(caught.exception.failure_class, {"output_already_exists", "promote_failed"})
        self.assertTrue(symlink_still_exists)
        self.assertTrue(symlink_still_symlink)

    def test_promote_rollback_does_not_delete_replaced_symlink_destination(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess import staging as staging_mod
        from scripts.hermes_postprocess.staging import PromoteError, promote_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            staging = Path(candidate.staging_dir)
            staging.mkdir(parents=True, exist_ok=True)
            raw = json.loads(Path(candidate.raw_json_path).read_text(encoding="utf-8"))
            raw["segments"][0]["text"] = "corrected transcript"
            (staging / "correction.txt").write_text("corrected transcript", encoding="utf-8")
            (staging / "correction.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            (staging / "summary.md").write_text(_valid_summary_markdown(), encoding="utf-8")
            (staging / "validation-correction.json").write_text(_passing_validation_report(), encoding="utf-8")
            (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")
            original_copy = staging_mod._atomic_copy_no_overwrite
            calls: list[Path] = []
            replacement_target = root / "replacement-target.txt"

            def replacing_copy(src: Path, dst: Path, expected_source=None) -> dict[str, str]:
                calls.append(dst)
                if len(calls) == 1:
                    metadata = original_copy(src, dst, expected_source)
                    replacement_target.write_bytes(dst.read_bytes())
                    dst.unlink()
                    os.symlink(replacement_target, dst)
                    return metadata
                raise OSError("forced copy failure")

            with patch.object(staging_mod, "_atomic_copy_no_overwrite", side_effect=replacing_copy):
                with self.assertRaises(PromoteError) as caught:
                    promote_candidate(candidate, allow_promote=True)
            replaced_destination = calls[0]
            symlink_still_exists = os.path.lexists(replaced_destination)
            symlink_still_symlink = replaced_destination.is_symlink()
            details = caught.exception.details

        self.assertEqual(caught.exception.failure_class, "promote_failed")
        self.assertTrue(symlink_still_exists)
        self.assertTrue(symlink_still_symlink)
        self.assertIn("rollback_errors", details)
        self.assertNotIn(RAW_SENTINEL, json.dumps(details, ensure_ascii=False))

    def test_cli_subprocess_end_to_end_validate_promote_flow_is_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")

            dry = _run_cli([
                "dry-run",
                "--lecture-root",
                str(lecture_root),
                "--repo-root",
                str(root),
            ])
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertNotIn(RAW_SENTINEL, dry.stdout)
            candidate = json.loads(dry.stdout)
            staging = Path(candidate["staging_dir"])
            staging.mkdir(parents=True, exist_ok=True)
            candidate_path = staging / "candidate.json"
            candidate_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")

            raw = json.loads(Path(candidate["raw_json_path"]).read_text(encoding="utf-8"))
            raw["segments"][0]["text"] = "corrected transcript"
            (staging / "correction.txt").write_text("corrected transcript", encoding="utf-8")
            (staging / "correction.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            (staging / "summary.md").write_text(_valid_summary_markdown(), encoding="utf-8")

            validation_correction = _run_cli([
                "validate-correction",
                "--raw-json",
                candidate["raw_json_path"],
                "--correction-txt",
                str(staging / "correction.txt"),
                "--correction-json",
                str(staging / "correction.json"),
                "--final-txt",
                candidate["correction_txt_path"],
                "--final-json",
                candidate["correction_json_path"],
                "--report-path",
                str(staging / "validation-correction.json"),
            ])
            validation_summary = _run_cli([
                "validate-summary",
                "--summary-md",
                str(staging / "summary.md"),
                "--corrected-txt",
                str(staging / "correction.txt"),
                "--final-md",
                candidate["summary_md_path"],
                "--report-path",
                str(staging / "validation-summary.json"),
            ])
            promote = _run_cli([
                "promote",
                "--candidate-json",
                str(candidate_path),
                "--allow-promote",
                "--report-path",
                str(staging / "promote.json"),
            ])

            combined = dry.stdout + validation_correction.stdout + validation_summary.stdout + promote.stdout
            self.assertEqual(validation_correction.returncode, 0, validation_correction.stderr)
            self.assertEqual(validation_summary.returncode, 0, validation_summary.stderr)
            self.assertEqual(promote.returncode, 0, promote.stderr)
            self.assertNotIn(RAW_SENTINEL, combined)
            self.assertTrue(Path(candidate["correction_txt_path"]).exists())
            self.assertTrue(Path(candidate["correction_json_path"]).exists())
            self.assertTrue(Path(candidate["summary_md_path"]).exists())

    def test_operator_selects_after_baseline_and_claim_filters_and_releases_stale_claim(self) -> None:
        from scripts.hermes_postprocess.operator import OperatorConfig, select_candidate
        from scripts.hermes_postprocess.paths import resolve_candidate_paths

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_1")
            _write_raw_pair(lecture_root, "260504DS_2")
            _write_raw_pair(lecture_root, "260504DS_3")
            baseline = root / "state" / "hermes_postprocess" / "cron-baseline.json"
            baseline.parent.mkdir(parents=True, exist_ok=True)
            baseline.write_text(json.dumps({"schema_version": 1, "skip_stems": ["260504DS_1"]}), encoding="utf-8")
            active_claim = resolve_candidate_paths("260504DS_2", lecture_root=lecture_root, repo_root=root).claim_path
            active_claim.parent.mkdir(parents=True, exist_ok=True)
            active_claim.write_text(
                json.dumps({"schema_version": 1, "stem": "260504DS_2", "status": "claimed", "updated_at": FIXED_NOW.isoformat().replace("+00:00", "Z")}),
                encoding="utf-8",
            )
            stale_claim = resolve_candidate_paths("260504DS_3", lecture_root=lecture_root, repo_root=root).claim_path
            stale_claim.parent.mkdir(parents=True, exist_ok=True)
            stale_claim.write_text(
                json.dumps({"schema_version": 1, "stem": "260504DS_3", "status": "claimed", "updated_at": "2026-05-21T09:00:00Z"}),
                encoding="utf-8",
            )
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0, stale_claim_after_sec=3600)

            candidate = select_candidate(config, now=FIXED_NOW)

            self.assertIsNotNone(candidate)
            assert candidate is not None
            self.assertEqual(candidate.stem, "260504DS_3")
            self.assertTrue(active_claim.exists())
            self.assertFalse(stale_claim.exists())

    def test_operator_select_candidate_skips_bad_quality_and_selects_next_stem(self) -> None:
        from scripts.hermes_postprocess.operator import OperatorConfig, select_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_1")
            _write_quality_scorecard(lecture_root, "260504DS_1", health="bad")
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            candidate = select_candidate(config, now=FIXED_NOW)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.stem, "260504DS_2")

    def test_operator_acquires_claim_atomically_without_overwriting_existing_claim(self) -> None:
        from scripts.hermes_postprocess.operator import OperatorConfig, acquire_claim, select_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)
            candidate = select_candidate(config, now=FIXED_NOW)
            assert candidate is not None

            first = acquire_claim(candidate, config, now=FIXED_NOW)
            second = acquire_claim(candidate, config, now=FIXED_NOW + dt.timedelta(seconds=1))
            claim_text = Path(candidate.claim_path).read_text(encoding="utf-8")

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertIn("260504DS_2", claim_text)
        self.assertIn('"status": "claimed"', claim_text)
        self.assertNotIn(RAW_SENTINEL, claim_text)

    def test_operator_run_once_skips_malformed_quality_and_runs_next_candidate(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_1")
            _write_quality_scorecard(lecture_root, "260504DS_1", health="warn", malformed=True)
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)
            seen_stems: list[str] = []

            def child_runner(candidate: object, _candidate_path: Path, _config: object) -> ChildRunResult:
                seen_stems.append(candidate.stem)  # type: ignore[attr-defined]
                return ChildRunResult(exit_code=1, log_path=root / "child.log")

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            skipped_claim_exists = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_1.json").exists()
            selected_claim_exists = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").exists()

        self.assertEqual(seen_stems, ["260504DS_2"])
        self.assertEqual(outcome["kind"], "failed")
        self.assertEqual(outcome["stem"], "260504DS_2")
        self.assertFalse(skipped_claim_exists)
        self.assertTrue(selected_claim_exists)

    def test_run_once_resets_stale_staging_before_child_attempt(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once, select_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)
            stale_candidate = select_candidate(config, now=FIXED_NOW)
            assert stale_candidate is not None
            _write_valid_staging(stale_candidate, lecture_root)
            staging = Path(stale_candidate.staging_dir)
            stale_marker = staging / "stale-only.txt"
            stale_marker.write_text(RAW_SENTINEL, encoding="utf-8")

            def child_runner(*_args: object, **_kwargs: object) -> ChildRunResult:
                return ChildRunResult(exit_code=0, log_path=root / "child.log")

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            claim_text_after = Path(stale_candidate.claim_path).read_text(encoding="utf-8")
            promote_text = (staging / "promote.json").read_text(encoding="utf-8")

        self.assertEqual(outcome["kind"], "failed")
        self.assertFalse(stale_marker.exists())
        self.assertFalse(Path(stale_candidate.correction_txt_path).exists())
        self.assertIn("postprocess_verification_failed", claim_text_after)
        self.assertIn("unsafe_staging_artifact", promote_text)
        self.assertIn('"reason": "missing"', promote_text)
        self.assertNotIn(RAW_SENTINEL, json.dumps(outcome, ensure_ascii=False) + claim_text_after + promote_text)

    def test_operator_concurrent_claim_attempts_allow_exactly_one_winner(self) -> None:
        from scripts.hermes_postprocess.operator import OperatorConfig, acquire_claim, select_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)
            candidate = select_candidate(config, now=FIXED_NOW)
            assert candidate is not None

            def try_claim(offset: int) -> bool:
                return acquire_claim(candidate, config, now=FIXED_NOW + dt.timedelta(seconds=offset))

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(try_claim, range(8)))
            claim_payload = json.loads(Path(candidate.claim_path).read_text(encoding="utf-8"))

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 7)
        self.assertEqual(claim_payload["stem"], "260504DS_2")
        self.assertEqual(claim_payload["status"], "claimed")
        self.assertNotIn(RAW_SENTINEL, json.dumps(claim_payload, ensure_ascii=False))

    def test_run_child_hermes_uses_macos_sandbox_to_deny_final_writes(self) -> None:
        from scripts.hermes_postprocess.operator import OperatorConfig, run_child_hermes, select_candidate, write_candidate_and_manifest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            (root / ".env").write_text("REPO_SECRET=do-not-read\n", encoding="utf-8")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/opt/hermes/bin/hermes"), stable_for_sec=0)
            candidate = select_candidate(config, now=FIXED_NOW)
            self.assertIsNotNone(candidate)
            assert candidate is not None
            candidate_path, _manifest_path = write_candidate_and_manifest(candidate)
            captured: dict[str, object] = {}

            class FakeCompletedProcess:
                returncode = 0

            def fake_run(cmd: list[str], **kwargs: object) -> FakeCompletedProcess:
                captured["cmd"] = cmd
                captured["cwd"] = kwargs.get("cwd")
                captured["env"] = kwargs.get("env")
                captured["stdout"] = kwargs.get("stdout")
                captured["stderr"] = kwargs.get("stderr")
                captured["text"] = kwargs.get("text")
                return FakeCompletedProcess()

            with (
                patch("scripts.hermes_postprocess.operator.shutil.which", return_value="/usr/bin/sandbox-exec"),
                patch("scripts.hermes_postprocess.operator.subprocess.run", side_effect=fake_run),
            ):
                result = run_child_hermes(candidate, candidate_path, config)

            cmd = captured["cmd"]
            assert isinstance(cmd, list)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(cmd[0], "/usr/bin/sandbox-exec")
            self.assertEqual(cmd[1], "-f")
            sandbox_profile = Path(cmd[2])
            self.assertEqual(cmd[3], str(config.hermes_bin))
            self.assertIn("-t", cmd)
            self.assertEqual(cmd[cmd.index("-t") + 1], "file,no_mcp")
            self.assertNotIn("terminal,file", cmd)
            profile_text = sandbox_profile.read_text(encoding="utf-8")
            self.assertIn("(deny file-write*)", profile_text)
            self.assertIn("(allow file-write*", profile_text)
            self.assertNotIn("(allow default)", profile_text)
            self.assertIn(str(Path(candidate.staging_dir)), profile_text)
            self.assertIn('(allow file-write* (literal "/dev/null"))', profile_text)
            self.assertIn('/private/var/select', profile_text)
            self.assertNotIn(f'(allow file-read* (subpath "{config.cron_log_dir}"))', profile_text)
            self.assertNotIn(f'(allow file-read* (literal "{root / ".env"}"))', profile_text)
            self.assertIn(f'(deny file-read* (literal "{root / ".env"}"))', profile_text)
            runtime_home = Path(candidate.staging_dir) / ".hermes-runtime"
            self.assertNotIn(f'(allow file-read* (literal "{runtime_home / ".env"}"))', profile_text)
            self.assertIn(f'(deny file-read* (literal "{runtime_home / ".env"}"))', profile_text)
            self.assertNotIn(f'(allow file-write* (subpath "{config.cron_log_dir}"))', profile_text)
            self.assertNotIn(f'(allow file-read* (subpath "{lecture_root / "02_transcripts"}"))', profile_text)
            self.assertNotIn(f'(allow file-read* (subpath "{lecture_root / "03_correction"}"))', profile_text)
            self.assertNotIn(f'(allow file-read* (subpath "{lecture_root / "04_summarize"}"))', profile_text)
            self.assertIn(str(lecture_root / "05_prompt"), profile_text)
            self.assertIn(str(Path(candidate.staging_dir) / "validation-correction.json"), profile_text)
            self.assertIn(str(Path(candidate.staging_dir) / "validation-summary.json"), profile_text)
            self.assertIn(str(Path(candidate.staging_dir) / "promote.json"), profile_text)
            env = captured["env"]
            assert isinstance(env, dict)
            self.assertEqual(env["LECTURE_RECORDINGS_ROOT"], str(lecture_root))
            self.assertIs(captured["stdout"], subprocess.DEVNULL)
            self.assertIs(captured["stderr"], subprocess.DEVNULL)
            self.assertIs(captured["text"], True)

    def test_run_child_hermes_uses_isolated_hermes_home_inside_staging(self) -> None:
        from scripts.hermes_postprocess.operator import OperatorConfig, run_child_hermes, select_candidate, write_candidate_and_manifest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            real_hermes_home = root / "real-hermes-home"
            real_hermes_home.mkdir()
            (real_hermes_home / "config.yaml").write_text("model: test-model\n", encoding="utf-8")
            (real_hermes_home / "auth.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "active_provider": "openai-codex",
                        "updated_at": "2026-05-21T12:00:00Z",
                        "providers": {
                            "openai-codex": {
                                "tokens": {
                                    "access_token": "codex-access-token",
                                    "refresh_token": "codex-refresh-token",
                                },
                                "auth_mode": "chatgpt",
                            },
                            "copilot": {
                                "tokens": {
                                    "access_token": "other-provider-token",
                                }
                            },
                        },
                        "credential_pool": {
                            "openai-codex": [
                                {
                                    "label": "primary",
                                    "access_token": "pool-access-token",
                                    "refresh_token": "pool-refresh-token",
                                }
                            ],
                            "copilot": [
                                {
                                    "label": "other",
                                    "access_token": "other-pool-token",
                                }
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (real_hermes_home / ".env").write_text("TEST_PROVIDER_KEY=secret\n", encoding="utf-8")
            (real_hermes_home / "models_dev_cache.json").write_text("{}\n", encoding="utf-8")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/opt/hermes/bin/hermes"), stable_for_sec=0)
            candidate = select_candidate(config, now=FIXED_NOW)
            self.assertIsNotNone(candidate)
            assert candidate is not None
            candidate_path, _manifest_path = write_candidate_and_manifest(candidate)
            captured: dict[str, object] = {}

            class FakeCompletedProcess:
                returncode = 0

            def fake_run(cmd: list[str], **kwargs: object) -> FakeCompletedProcess:
                env = kwargs.get("env")
                assert isinstance(env, dict)
                runtime_home = Path(env["HERMES_HOME"])
                self.assertEqual(runtime_home, Path(candidate.staging_dir) / ".hermes-runtime")
                self.assertTrue((runtime_home / "config.yaml").is_file())
                self.assertTrue((runtime_home / "auth.json").is_file())
                self.assertFalse((runtime_home / ".env").exists())
                self.assertTrue((runtime_home / "logs").is_dir())
                self.assertTrue((runtime_home / "sessions").is_dir())
                self.assertTrue((runtime_home / "cache").is_dir())
                import yaml

                copied_config = yaml.safe_load((runtime_home / "config.yaml").read_text(encoding="utf-8"))
                copied_auth = json.loads((runtime_home / "auth.json").read_text(encoding="utf-8"))
                self.assertEqual(copied_config["model"], "test-model")
                self.assertEqual(copied_config["mcp_servers"], {})
                self.assertEqual(copied_config["security"]["tirith_enabled"], False)
                self.assertEqual(copied_auth["active_provider"], "openai-codex")
                self.assertEqual(sorted(copied_auth["providers"]), ["openai-codex"])
                self.assertEqual(sorted(copied_auth["credential_pool"]), ["openai-codex"])
                self.assertNotIn("copilot", json.dumps(copied_auth, ensure_ascii=False))
                self.assertNotIn("TEST_PROVIDER_KEY", json.dumps(copied_auth, ensure_ascii=False))
                self.assertNotIn("UNRELATED_SECRET", env)
                self.assertEqual(env["PATH"], os.environ["PATH"])
                captured["cmd"] = cmd
                captured["env"] = env
                captured["runtime_home"] = runtime_home
                return FakeCompletedProcess()

            with (
                patch.dict(os.environ, {"HERMES_HOME": str(real_hermes_home), "UNRELATED_SECRET": "do-not-forward"}, clear=False),
                patch("scripts.hermes_postprocess.operator.shutil.which", return_value="/usr/bin/sandbox-exec"),
                patch("scripts.hermes_postprocess.operator.subprocess.run", side_effect=fake_run),
            ):
                result = run_child_hermes(candidate, candidate_path, config)

            cmd = captured["cmd"]
            assert isinstance(cmd, list)
            sandbox_profile = Path(cmd[2]).read_text(encoding="utf-8")
            runtime_home = captured["runtime_home"]
            assert isinstance(runtime_home, Path)
            self.assertEqual(result.exit_code, 0)
            self.assertIn(str(runtime_home), sandbox_profile)
            self.assertFalse(runtime_home.exists())

    def test_run_child_hermes_suppresses_child_stdout_and_stderr_from_log(self) -> None:
        from scripts.hermes_postprocess.operator import OperatorConfig, run_child_hermes, select_candidate, write_candidate_and_manifest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            child = root / "fake-hermes"
            child.write_text(f"#!/bin/sh\necho {RAW_SENTINEL}\necho {RAW_SENTINEL} >&2\nexit 0\n", encoding="utf-8")
            child.chmod(0o755)
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=child, stable_for_sec=0)
            candidate = select_candidate(config, now=FIXED_NOW)
            self.assertIsNotNone(candidate)
            assert candidate is not None
            candidate_path, _manifest_path = write_candidate_and_manifest(candidate)

            with patch("scripts.hermes_postprocess.operator._wrap_child_command_with_sandbox", side_effect=lambda cmd, **_kwargs: cmd):
                result = run_child_hermes(candidate, candidate_path, config)
            log_text = result.log_path.read_text(encoding="utf-8")
            log_mode = result.log_path.stat().st_mode & 0o777

        self.assertEqual(result.exit_code, 0)
        self.assertNotIn(RAW_SENTINEL, log_text)
        self.assertIn("child stdout/stderr suppressed", log_text)
        self.assertEqual(log_mode, 0o600)

    def test_run_child_hermes_fails_closed_when_macos_sandbox_is_missing(self) -> None:
        from scripts.hermes_postprocess.operator import OperatorConfig, run_child_hermes, select_candidate, write_candidate_and_manifest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/opt/hermes/bin/hermes"), stable_for_sec=0)
            candidate = select_candidate(config, now=FIXED_NOW)
            self.assertIsNotNone(candidate)
            assert candidate is not None
            candidate_path, _manifest_path = write_candidate_and_manifest(candidate)

            with (
                patch("scripts.hermes_postprocess.operator.shutil.which", return_value=None),
                patch("scripts.hermes_postprocess.operator.sys.platform", "darwin"),
            ):
                with self.assertRaisesRegex(RuntimeError, "sandbox_exec_missing"):
                    run_child_hermes(candidate, candidate_path, config)

    def test_run_child_hermes_runs_unsandboxed_only_on_non_darwin_without_sandbox_exec(self) -> None:
        from scripts.hermes_postprocess.operator import OperatorConfig, run_child_hermes, select_candidate, write_candidate_and_manifest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/opt/hermes/bin/hermes"), stable_for_sec=0)
            candidate = select_candidate(config, now=FIXED_NOW)
            self.assertIsNotNone(candidate)
            assert candidate is not None
            candidate_path, _manifest_path = write_candidate_and_manifest(candidate)
            captured: dict[str, object] = {}

            class FakeCompletedProcess:
                returncode = 0

            def fake_run(cmd: list[str], **kwargs: object) -> FakeCompletedProcess:
                captured["cmd"] = cmd
                captured["stdout"] = kwargs.get("stdout")
                captured["stderr"] = kwargs.get("stderr")
                return FakeCompletedProcess()

            with (
                patch("scripts.hermes_postprocess.operator.shutil.which", return_value=None),
                patch("scripts.hermes_postprocess.operator.sys.platform", "linux"),
                patch("scripts.hermes_postprocess.operator.subprocess.run", side_effect=fake_run),
            ):
                result = run_child_hermes(candidate, candidate_path, config)

            cmd = captured["cmd"]
            assert isinstance(cmd, list)

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(cmd[0], str(config.hermes_bin))
        self.assertNotIn("sandbox-exec", cmd)
        self.assertIs(captured["stdout"], subprocess.DEVNULL)
        self.assertIs(captured["stderr"], subprocess.DEVNULL)

    def test_operator_run_once_no_candidate_is_silent_and_does_not_run_child(self) -> None:
        from scripts.hermes_postprocess.operator import OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)
            called = False

            def child_runner(*_args: object, **_kwargs: object) -> object:
                nonlocal called
                called = True
                raise AssertionError("child runner must not run without a candidate")

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)

        self.assertEqual(outcome["kind"], "no_candidate")
        self.assertIsNone(outcome.get("alert"))
        self.assertFalse(called)
        _assert_payload_metadata_only(self, outcome)

    def test_operator_run_once_outcomes_declare_metadata_containment(self) -> None:
        import scripts.hermes_postprocess.operator as operator_module
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        outcomes: list[dict[str, object]] = []

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)
            outcomes.append(run_once(config, child_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no child")), now=FIXED_NOW))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            partial_final = lecture_root / "03_correction" / "260504DS_2.txt"
            partial_final.write_text("partial correction", encoding="utf-8")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)
            outcomes.append(run_once(config, child_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("blocked child")), now=FIXED_NOW))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)
            with patch.object(operator_module, "acquire_claim", return_value=False):
                outcomes.append(run_once(config, child_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("race child")), now=FIXED_NOW))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            def failing_child(_candidate: object, _candidate_path: Path, child_config: OperatorConfig) -> ChildRunResult:
                log_path = Path(child_config.cron_log_dir) / "failed.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("metadata-only child failure", encoding="utf-8")
                return ChildRunResult(exit_code=9, log_path=log_path)

            outcomes.append(run_once(config, child_runner=failing_child, now=FIXED_NOW))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            def successful_child(candidate: object, _candidate_path: Path, child_config: OperatorConfig) -> ChildRunResult:
                _write_valid_staging(candidate, lecture_root)
                log_path = Path(child_config.cron_log_dir) / "success.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("metadata-only child success", encoding="utf-8")
                return ChildRunResult(exit_code=0, log_path=log_path)

            outcomes.append(run_once(config, child_runner=successful_child, now=FIXED_NOW))

        self.assertEqual([outcome["kind"] for outcome in outcomes], ["no_candidate", "blocked", "claim_race_lost", "failed", "completed"])
        for outcome in outcomes:
            _assert_payload_metadata_only(self, outcome)
            alert = outcome.get("alert")
            if alert is not None:
                _assert_payload_metadata_only(self, alert)

    def test_operator_run_once_completes_with_metadata_only_alert_and_claim(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            def child_runner(candidate: object, _candidate_path: Path, config: OperatorConfig) -> ChildRunResult:
                _write_valid_staging(candidate, lecture_root)
                log_path = Path(config.cron_log_dir) / "child.log"  # type: ignore[attr-defined]
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("metadata-only child log", encoding="utf-8")
                return ChildRunResult(exit_code=0, log_path=log_path)

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            alert_text = json.dumps(outcome.get("alert"), ensure_ascii=False)
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")
            candidate_text = (root / "state" / "hermes_postprocess" / "staging" / "260504DS_2" / "candidate.json").read_text(encoding="utf-8")
            manifest_text = (root / "state" / "hermes_postprocess" / "staging" / "260504DS_2" / "manifest.json").read_text(encoding="utf-8")

        self.assertEqual(outcome["kind"], "completed")
        self.assertIn("lecture_stt_postprocess_complete", alert_text)
        self.assertIn('"status": "completed"', claim_text)
        for text in (alert_text, claim_text, candidate_text, manifest_text):
            self.assertNotIn(RAW_SENTINEL, text)
            self.assertNotIn("segments", text)

    def test_operator_run_once_reports_sandbox_exec_missing_as_stable_failure_class(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            def child_runner(*_args: object, **_kwargs: object) -> ChildRunResult:
                raise RuntimeError("sandbox_exec_missing")

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            alert_text = json.dumps(outcome.get("alert"), ensure_ascii=False)
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")

        self.assertEqual(outcome["kind"], "failed")
        self.assertIn("sandbox_exec_missing", alert_text)
        self.assertIn("sandbox_exec_missing", claim_text)
        self.assertNotIn('"failure": "RuntimeError"', alert_text + claim_text)
        self.assertNotIn(RAW_SENTINEL, alert_text + claim_text)
        self.assertNotIn("segments", alert_text + claim_text)

    def test_operator_run_once_child_success_without_promote_report_marks_failed_metadata_only(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            def child_runner(candidate: object, _candidate_path: Path, config: OperatorConfig) -> ChildRunResult:
                log_path = Path(config.cron_log_dir) / "child.log"  # type: ignore[attr-defined]
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("child returned without promote report", encoding="utf-8")
                return ChildRunResult(exit_code=0, log_path=log_path)

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            alert_text = json.dumps(outcome.get("alert"), ensure_ascii=False)
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")

        self.assertEqual(outcome["kind"], "failed")
        self.assertIn("postprocess_verification_failed", alert_text)
        self.assertIn('"status": "failed"', claim_text)
        self.assertNotIn(RAW_SENTINEL, alert_text + claim_text)
        self.assertNotIn("segments", alert_text + claim_text)

    def test_operator_restores_existing_final_outputs_modified_by_child(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            _write_correction_pair(lecture_root, "260504DS_2")
            correction_txt = lecture_root / "03_correction" / "260504DS_2.txt"
            correction_json = lecture_root / "03_correction" / "260504DS_2.json"
            original_txt = correction_txt.read_text(encoding="utf-8")
            original_json = correction_json.read_text(encoding="utf-8")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            def child_runner(candidate: object, _candidate_path: Path, config: OperatorConfig) -> ChildRunResult:
                staging = Path(candidate.staging_dir)  # type: ignore[attr-defined]
                staging.mkdir(parents=True, exist_ok=True)
                (staging / "summary.md").write_text(_valid_summary_markdown(), encoding="utf-8")
                correction_txt.write_text("child tampered correction", encoding="utf-8")
                correction_json.write_text(json.dumps({"segments": []}), encoding="utf-8")
                log_path = Path(config.cron_log_dir) / "child.log"  # type: ignore[attr-defined]
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("metadata-only child log", encoding="utf-8")
                return ChildRunResult(exit_code=0, log_path=log_path)

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            staging = root / "state" / "hermes_postprocess" / "staging" / "260504DS_2"
            quarantine_paths = sorted((staging / "unauthorized-final-artifacts").glob("*"))
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")
            restored_txt = correction_txt.read_text(encoding="utf-8")
            restored_json = correction_json.read_text(encoding="utf-8")
            summary_exists = (lecture_root / "04_summarize" / "260504DS_2.md").exists()

        self.assertEqual(outcome["kind"], "failed")
        self.assertEqual(restored_txt, original_txt)
        self.assertEqual(restored_json, original_json)
        self.assertFalse(summary_exists)
        self.assertEqual(len(quarantine_paths), 2)
        self.assertIn("unauthorized_final_output_modified_by_child", claim_text)
        self.assertNotIn(RAW_SENTINEL, json.dumps(outcome, ensure_ascii=False) + claim_text)

    def test_operator_fails_closed_before_child_for_preexisting_broken_symlink_final(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            final_txt = lecture_root / "03_correction" / "260504DS_2.txt"
            final_txt.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(root / "missing-before-child.txt", final_txt)
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)
            called = False

            def child_runner(*_args: object, **_kwargs: object) -> ChildRunResult:
                nonlocal called
                called = True
                raise AssertionError("child must not run with unsupported pre-existing final artifact")

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")
            final_lexists = os.path.lexists(final_txt)
            final_is_symlink = final_txt.is_symlink()
            quarantine_dir = root / "state" / "hermes_postprocess" / "staging" / "260504DS_2" / "unauthorized-final-artifacts"
            quarantine_entries = list(quarantine_dir.glob("*")) if quarantine_dir.exists() else []

        self.assertFalse(called)
        self.assertEqual(outcome["kind"], "failed")
        self.assertTrue(final_lexists)
        self.assertTrue(final_is_symlink)
        self.assertEqual(quarantine_entries, [])
        self.assertIn("preexisting_final_artifact_unsupported_type", claim_text)
        self.assertNotIn("created_by_child", claim_text)
        self.assertNotIn(RAW_SENTINEL, json.dumps(outcome, ensure_ascii=False) + claim_text)

    def test_operator_fails_closed_before_child_for_preexisting_directory_final(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            final_txt = lecture_root / "03_correction" / "260504DS_2.txt"
            final_txt.mkdir(parents=True, exist_ok=True)
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)
            called = False

            def child_runner(*_args: object, **_kwargs: object) -> ChildRunResult:
                nonlocal called
                called = True
                raise AssertionError("child must not run with unsupported pre-existing final artifact")

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")
            still_directory = final_txt.is_dir()

        self.assertFalse(called)
        self.assertEqual(outcome["kind"], "failed")
        self.assertTrue(still_directory)
        self.assertIn("preexisting_final_artifact_unsupported_type", claim_text)
        self.assertNotIn(RAW_SENTINEL, json.dumps(outcome, ensure_ascii=False) + claim_text)

    def test_operator_fails_closed_before_child_for_symlinked_final_parent_directory(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, build_status, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            outside_parent = root / "outside-correction-parent"
            outside_parent.mkdir()
            correction_dir = lecture_root / "03_correction"
            correction_dir.rmdir()
            os.symlink(outside_parent, correction_dir)
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)
            called = False

            def child_runner(*_args: object, **_kwargs: object) -> ChildRunResult:
                nonlocal called
                called = True
                raise AssertionError("child must not run with symlinked final parent directory")

            status_before = build_status(config, now=FIXED_NOW)
            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")
            outside_entries = list(outside_parent.iterdir())
            parent_still_symlink = correction_dir.is_symlink()

        self.assertEqual(status_before["candidate_counts"]["complete"], 0)
        self.assertEqual(status_before["candidate_counts"]["blocked"], 1)
        self.assertFalse(called)
        self.assertEqual(outcome["kind"], "failed")
        self.assertTrue(parent_still_symlink)
        self.assertEqual(outside_entries, [])
        self.assertIn("preexisting_final_artifact_unsupported_type", claim_text)
        self.assertIn("unsupported_preexisting_final_parent_type", claim_text)
        self.assertNotIn(RAW_SENTINEL, json.dumps(outcome, ensure_ascii=False) + claim_text)

    def test_operator_does_not_follow_final_parent_symlink_created_by_child_on_failure(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            outside_parent = root / "outside-correction-parent"
            outside_parent.mkdir()
            outside_file = outside_parent / "260504DS_2.txt"
            outside_file.write_text("outside file must not be moved by quarantine", encoding="utf-8")
            correction_dir = lecture_root / "03_correction"
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            def child_runner(candidate: object, _candidate_path: Path, config: OperatorConfig) -> ChildRunResult:
                correction_dir.rmdir()
                os.symlink(outside_parent, correction_dir)
                log_path = Path(config.cron_log_dir) / "child.log"  # type: ignore[attr-defined]
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("metadata-only child log", encoding="utf-8")
                return ChildRunResult(exit_code=7, log_path=log_path)

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            outside_file_exists = os.path.lexists(outside_file)
            outside_file_text = outside_file.read_text(encoding="utf-8") if outside_file_exists else None
            parent_still_symlink = correction_dir.is_symlink()
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")

        self.assertEqual(outcome["kind"], "failed")
        self.assertTrue(parent_still_symlink)
        self.assertTrue(outside_file_exists)
        self.assertEqual(outside_file_text, "outside file must not be moved by quarantine")
        self.assertIn("unsafe_final_parent_after_child", claim_text)
        self.assertNotIn(RAW_SENTINEL, json.dumps(outcome, ensure_ascii=False) + claim_text)

    def test_operator_refuses_symlinked_quarantine_directory_without_moving_outside(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            outside_quarantine = root / "outside-quarantine"
            outside_quarantine.mkdir()
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            def child_runner(candidate: object, _candidate_path: Path, config: OperatorConfig) -> ChildRunResult:
                staging = Path(candidate.staging_dir)  # type: ignore[attr-defined]
                symlinked_quarantine = staging / "unauthorized-final-artifacts"
                os.symlink(outside_quarantine, symlinked_quarantine)
                final_txt = Path(candidate.correction_txt_path)  # type: ignore[attr-defined]
                final_txt.parent.mkdir(parents=True, exist_ok=True)
                final_txt.write_text("unauthorized final correction", encoding="utf-8")
                log_path = Path(config.cron_log_dir) / "child.log"  # type: ignore[attr-defined]
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("metadata-only child log", encoding="utf-8")
                return ChildRunResult(exit_code=7, log_path=log_path)

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            final_txt = lecture_root / "03_correction" / "260504DS_2.txt"
            outside_entries = list(outside_quarantine.iterdir())
            final_still_exists = final_txt.exists()
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")

        self.assertEqual(outcome["kind"], "failed")
        self.assertEqual(outside_entries, [])
        self.assertTrue(final_still_exists)
        self.assertIn("quarantine_failed_unsafe_quarantine_dir", claim_text)
        self.assertNotIn(RAW_SENTINEL, json.dumps(outcome, ensure_ascii=False) + claim_text)

    def test_operator_restores_existing_final_output_mode_modified_by_child(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            _write_correction_pair(lecture_root, "260504DS_2")
            correction_txt = lecture_root / "03_correction" / "260504DS_2.txt"
            original_mode = correction_txt.stat().st_mode & 0o777
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            def child_runner(candidate: object, _candidate_path: Path, config: OperatorConfig) -> ChildRunResult:
                staging = Path(candidate.staging_dir)  # type: ignore[attr-defined]
                staging.mkdir(parents=True, exist_ok=True)
                (staging / "summary.md").write_text(_valid_summary_markdown(), encoding="utf-8")
                correction_txt.chmod(0o600 if original_mode != 0o600 else 0o644)
                log_path = Path(config.cron_log_dir) / "child.log"  # type: ignore[attr-defined]
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("metadata-only child log", encoding="utf-8")
                return ChildRunResult(exit_code=0, log_path=log_path)

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            restored_mode = correction_txt.stat().st_mode & 0o777
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")

        self.assertEqual(outcome["kind"], "failed")
        self.assertEqual(restored_mode, original_mode)
        self.assertIn("mode_modified_by_child", claim_text)
        self.assertNotIn(RAW_SENTINEL, json.dumps(outcome, ensure_ascii=False) + claim_text)

    def test_operator_restores_existing_final_outputs_replaced_by_same_content_symlink(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            _write_correction_pair(lecture_root, "260504DS_2")
            correction_txt = lecture_root / "03_correction" / "260504DS_2.txt"
            original_txt = correction_txt.read_text(encoding="utf-8")
            same_content_target = root / "same-content-target.txt"
            same_content_target.write_text(original_txt, encoding="utf-8")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            def child_runner(candidate: object, _candidate_path: Path, config: OperatorConfig) -> ChildRunResult:
                staging = Path(candidate.staging_dir)  # type: ignore[attr-defined]
                staging.mkdir(parents=True, exist_ok=True)
                (staging / "summary.md").write_text(_valid_summary_markdown(), encoding="utf-8")
                correction_txt.unlink()
                os.symlink(same_content_target, correction_txt)
                log_path = Path(config.cron_log_dir) / "child.log"  # type: ignore[attr-defined]
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("metadata-only child log", encoding="utf-8")
                return ChildRunResult(exit_code=0, log_path=log_path)

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            staging = root / "state" / "hermes_postprocess" / "staging" / "260504DS_2"
            quarantine_paths = sorted((staging / "unauthorized-final-artifacts").glob("*"))
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")
            restored_txt = correction_txt.read_text(encoding="utf-8")
            restored_is_symlink = correction_txt.is_symlink()

        self.assertEqual(outcome["kind"], "failed")
        self.assertEqual(restored_txt, original_txt)
        self.assertFalse(restored_is_symlink)
        self.assertEqual(len(quarantine_paths), 1)
        self.assertIn("unauthorized_final_output_modified_by_child", claim_text)
        self.assertNotIn(RAW_SENTINEL, json.dumps(outcome, ensure_ascii=False) + claim_text)

    def test_operator_quarantines_broken_symlink_final_created_by_child_on_failure(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            def child_runner(candidate: object, _candidate_path: Path, config: OperatorConfig) -> ChildRunResult:
                final_txt = Path(candidate.correction_txt_path)  # type: ignore[attr-defined]
                final_txt.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(root / "missing-target.txt", final_txt)
                log_path = Path(config.cron_log_dir) / "child.log"  # type: ignore[attr-defined]
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("metadata-only child log", encoding="utf-8")
                return ChildRunResult(exit_code=7, log_path=log_path)

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            final_txt = lecture_root / "03_correction" / "260504DS_2.txt"
            staging = root / "state" / "hermes_postprocess" / "staging" / "260504DS_2"
            quarantine_paths = sorted((staging / "unauthorized-final-artifacts").glob("*"))
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")
            final_lexists = os.path.lexists(final_txt)
            quarantined_lexists = os.path.lexists(quarantine_paths[0]) if quarantine_paths else False

        self.assertEqual(outcome["kind"], "failed")
        self.assertFalse(final_lexists)
        self.assertEqual(len(quarantine_paths), 1)
        self.assertTrue(quarantined_lexists)
        self.assertIn("unauthorized_final_output_created_by_child", claim_text)
        self.assertNotIn(RAW_SENTINEL, json.dumps(outcome, ensure_ascii=False) + claim_text)

    def test_operator_quarantines_child_written_final_artifacts_on_child_failure_paths(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        cases = {
            "nonzero_exit": lambda _log_path: ChildRunResult(exit_code=7, log_path=_log_path),
            "timeout": lambda _log_path: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="hermes", timeout=1)),
            "wrapper_exception": lambda _log_path: (_ for _ in ()).throw(RuntimeError("child wrapper failed")),
        }
        for name, result_factory in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                lecture_root = _make_operator_fixture(root)
                _write_raw_pair(lecture_root, "260504DS_2")
                config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

                def child_runner(candidate: object, _candidate_path: Path, config: OperatorConfig) -> ChildRunResult:
                    Path(candidate.correction_txt_path).parent.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
                    Path(candidate.summary_md_path).parent.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
                    Path(candidate.correction_txt_path).write_text("unauthorized final correction", encoding="utf-8")  # type: ignore[attr-defined]
                    Path(candidate.correction_json_path).write_text(json.dumps({"segments": []}), encoding="utf-8")  # type: ignore[attr-defined]
                    Path(candidate.summary_md_path).write_text("unauthorized summary", encoding="utf-8")  # type: ignore[attr-defined]
                    log_path = Path(config.cron_log_dir) / f"{name}.log"  # type: ignore[attr-defined]
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text("metadata-only child log", encoding="utf-8")
                    return result_factory(log_path)

                outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
                final_paths = [
                    lecture_root / "03_correction" / "260504DS_2.txt",
                    lecture_root / "03_correction" / "260504DS_2.json",
                    lecture_root / "04_summarize" / "260504DS_2.md",
                ]
                final_exists_after_run = [path.exists() for path in final_paths]
                staging = root / "state" / "hermes_postprocess" / "staging" / "260504DS_2"
                quarantine_paths = sorted((staging / "unauthorized-final-artifacts").glob("*"))
                claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")

            self.assertEqual(outcome["kind"], "failed")
            self.assertEqual(final_exists_after_run, [False, False, False])
            self.assertEqual(len(quarantine_paths), 3)
            self.assertIn('"status": "failed"', claim_text)
            self.assertNotIn(RAW_SENTINEL, json.dumps(outcome, ensure_ascii=False) + claim_text)

    def test_operator_run_once_rejects_child_written_final_artifacts_and_fake_reports(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            def child_runner(candidate: object, _candidate_path: Path, config: OperatorConfig) -> ChildRunResult:
                staging = Path(candidate.staging_dir)  # type: ignore[attr-defined]
                staging.mkdir(parents=True, exist_ok=True)
                (staging / "validation-correction.json").write_text(_passing_validation_report(), encoding="utf-8")
                (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")
                (staging / "promote.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "passed": True,
                            "failure_class": None,
                            "message": "fake promote passed",
                            "artifacts": [],
                            "raw_transcript_body_included": False,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                Path(candidate.correction_txt_path).parent.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
                Path(candidate.summary_md_path).parent.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
                Path(candidate.correction_txt_path).write_text("```invalid child final correction", encoding="utf-8")  # type: ignore[attr-defined]
                Path(candidate.correction_json_path).write_text(json.dumps({"segments": []}), encoding="utf-8")  # type: ignore[attr-defined]
                Path(candidate.summary_md_path).write_text("invalid summary", encoding="utf-8")  # type: ignore[attr-defined]
                log_path = Path(config.cron_log_dir) / "child.log"  # type: ignore[attr-defined]
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("metadata-only fake child log", encoding="utf-8")
                return ChildRunResult(exit_code=0, log_path=log_path)

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            alert_text = json.dumps(outcome.get("alert"), ensure_ascii=False)
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")
            staging = root / "state" / "hermes_postprocess" / "staging" / "260504DS_2"
            validation_text = (staging / "validation-correction.json").read_text(encoding="utf-8")
            promote_text = (staging / "promote.json").read_text(encoding="utf-8")
            final_paths = [
                lecture_root / "03_correction" / "260504DS_2.txt",
                lecture_root / "03_correction" / "260504DS_2.json",
                lecture_root / "04_summarize" / "260504DS_2.md",
            ]
            final_exists_after_run = [path.exists() for path in final_paths]
            quarantine_paths = sorted((staging / "unauthorized-final-artifacts").glob("*"))

        self.assertEqual(outcome["kind"], "failed")
        self.assertIn("postprocess_verification_failed", alert_text)
        self.assertIn("unauthorized_final_output_created_by_child", validation_text + promote_text)
        self.assertIn('"passed": false', promote_text)
        self.assertNotIn("fake promote passed", promote_text)
        self.assertEqual(final_exists_after_run, [False, False, False])
        self.assertEqual(len(quarantine_paths), 3)
        self.assertIn('"status": "failed"', claim_text)
        self.assertNotIn(RAW_SENTINEL, alert_text + claim_text + validation_text + promote_text)
        self.assertNotIn("segments", alert_text + claim_text)

    def test_operator_status_reports_metadata_counts_only(self) -> None:
        from scripts.hermes_postprocess.operator import OperatorConfig, build_status
        from scripts.hermes_postprocess.paths import resolve_candidate_paths

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_1")
            _write_raw_pair(lecture_root, "260504DS_2")
            _write_correction_pair(lecture_root, "260504DS_2")
            (lecture_root / "04_summarize" / "260504DS_2.md").write_text(_valid_summary_markdown(), encoding="utf-8")
            claim = resolve_candidate_paths("260504DS_1", lecture_root=lecture_root, repo_root=root).claim_path
            claim.parent.mkdir(parents=True, exist_ok=True)
            claim.write_text(
                json.dumps({"schema_version": 1, "stem": "260504DS_1", "status": "claimed", "updated_at": FIXED_NOW.isoformat().replace("+00:00", "Z")}),
                encoding="utf-8",
            )
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            status = build_status(config, now=FIXED_NOW)
            status_text = json.dumps(status, ensure_ascii=False)

        self.assertEqual(status["schema_version"], 1)
        self.assertEqual(status["candidate_counts"]["claimed"], 1)
        self.assertEqual(status["candidate_counts"]["complete"], 1)
        self.assertEqual(status["candidate_counts"]["pending"], 0)
        self.assertNotIn(RAW_SENTINEL, status_text)
        self.assertNotIn("segments", status_text)

    def test_operator_status_reports_last_terminal_claims_and_no_noise_contract(self) -> None:
        from scripts.hermes_postprocess.operator import OperatorConfig, build_status
        from scripts.hermes_postprocess.paths import resolve_candidate_paths

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_1")
            _write_raw_pair(lecture_root, "260504DS_2")
            for stem, payload in {
                "260504DS_1": {
                    "schema_version": 1,
                    "stem": "260504DS_1",
                    "status": "completed",
                    "updated_at": "2026-05-21T10:00:00Z",
                    "completed_at": "2026-05-21T10:00:00Z",
                    "final_paths": [str(lecture_root / "04_summarize" / "260504DS_1.md")],
                    "raw_transcript_body_included": False,
                },
                "260504DS_2": {
                    "schema_version": 1,
                    "stem": "260504DS_2",
                    "status": "failed",
                    "updated_at": "2026-05-21T11:00:00Z",
                    "failure": "child_hermes_failed",
                    "log_path": str(root / "state" / "hermes_postprocess" / "cron_logs" / "failed.log"),
                    "raw_transcript_body_included": False,
                },
            }.items():
                claim_path = resolve_candidate_paths(stem, lecture_root=lecture_root, repo_root=root).claim_path
                claim_path.parent.mkdir(parents=True, exist_ok=True)
                claim_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            status = build_status(config, now=FIXED_NOW)
            status_text = json.dumps(status, ensure_ascii=False)

        self.assertTrue(status["no_noise_mode"])
        self.assertEqual(status["last_completed"]["stem"], "260504DS_1")
        self.assertEqual(status["last_failed"]["stem"], "260504DS_2")
        self.assertEqual(status["last_failed"]["failure"], "child_hermes_failed")
        self.assertNotIn(RAW_SENTINEL, status_text)
        self.assertNotIn("segments", status_text)

    def test_repo_tracked_operator_script_runs_without_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "lecture_stt_postprocess_operator.py"),
                    "status",
                    "--repo-root",
                    str(root),
                    "--lecture-root",
                    str(lecture_root),
                    "--hermes-bin",
                    "/bin/false",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"kind": "lecture_stt_postprocess_status"', proc.stdout)
        self.assertNotIn(RAW_SENTINEL, proc.stdout + proc.stderr)

    def test_operator_cli_status_and_run_once_no_candidate_are_metadata_only(self) -> None:
        from scripts.hermes_postprocess.operator import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                status_code = main([
                    "status",
                    "--repo-root",
                    str(root),
                    "--lecture-root",
                    str(lecture_root),
                    "--hermes-bin",
                    "/bin/false",
                ])
            status_output = stdout.getvalue()
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                run_code = main([
                    "run-once",
                    "--repo-root",
                    str(root),
                    "--lecture-root",
                    str(lecture_root),
                    "--hermes-bin",
                    "/bin/false",
                    "--stable-for-sec",
                    "0",
                ])
            run_output = stdout.getvalue()

        self.assertEqual(status_code, 0)
        self.assertEqual(run_code, 0)
        self.assertIn('"kind": "lecture_stt_postprocess_status"', status_output)
        self.assertEqual(run_output, "")
        self.assertNotIn(RAW_SENTINEL, status_output + run_output)

    def test_operator_run_once_reports_missing_lecture_root_as_icloud_unavailable(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_lecture_root = root / "missing_lecture_recordings"
            config = OperatorConfig(repo_root=root, lecture_root=missing_lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)
            called = False

            def child_runner(*_args: object, **_kwargs: object) -> ChildRunResult:
                nonlocal called
                called = True
                raise AssertionError("child must not run when lecture root is unavailable")

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            outcome_text = json.dumps(outcome, ensure_ascii=False)

        self.assertFalse(called)
        self.assertEqual(outcome["kind"], "failed")
        self.assertEqual(outcome["alert"]["failure"], "icloud_unavailable")
        self.assertNotIn(RAW_SENTINEL, outcome_text)
        self.assertNotIn("segments", outcome_text)

    def test_operator_run_once_reports_missing_prompt_dir_before_no_candidate(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            prompt_dir = lecture_root / "05_prompt"
            for path in sorted(prompt_dir.iterdir()):
                path.unlink()
            prompt_dir.rmdir()
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)
            called = False

            def child_runner(*_args: object, **_kwargs: object) -> ChildRunResult:
                nonlocal called
                called = True
                raise AssertionError("child must not run when prompt source is unavailable")

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            outcome_text = json.dumps(outcome, ensure_ascii=False)

        self.assertFalse(called)
        self.assertEqual(outcome["kind"], "failed")
        self.assertEqual(outcome["alert"]["failure"], "missing_prompt_dir")
        self.assertNotIn(RAW_SENTINEL, outcome_text)
        self.assertNotIn("segments", outcome_text)

    def test_operator_run_once_rejects_symlinked_raw_transcript_before_child(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            transcript_dir = lecture_root / "02_transcripts"
            outside_raw = root / "outside-raw.txt"
            outside_raw.write_text(RAW_SENTINEL, encoding="utf-8")
            os.symlink(outside_raw, transcript_dir / "260504DS_2.txt")
            (transcript_dir / "260504DS_2.json").write_text(
                json.dumps({"segments": [{"id": 1, "start": 0.0, "end": 1.0, "text": RAW_SENTINEL}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)
            called = False

            def child_runner(*_args: object, **_kwargs: object) -> ChildRunResult:
                nonlocal called
                called = True
                raise AssertionError("child must not read symlinked raw transcript input")

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")
            combined = json.dumps(outcome, ensure_ascii=False) + claim_text

        self.assertFalse(called)
        self.assertEqual(outcome["kind"], "failed")
        self.assertIn("unsafe_raw_artifact", combined)
        self.assertNotIn(RAW_SENTINEL, combined)
        self.assertNotIn("segments", json.dumps(outcome, ensure_ascii=False))

    def test_operator_verify_success_preflights_unsafe_staging_before_validator_reads_it(self) -> None:
        from scripts.hermes_postprocess.operator import verify_success
        from scripts.hermes_postprocess.picker import find_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            _write_valid_staging(candidate, lecture_root)
            staging = Path(candidate.staging_dir)
            outside = root / "outside-correction.txt"
            outside.write_text("corrected transcript", encoding="utf-8")
            (staging / "correction.txt").unlink()
            os.symlink(outside, staging / "correction.txt")

            with patch("scripts.hermes_postprocess.operator.validate_correction_artifacts", side_effect=AssertionError("validator read unsafe staging artifact")):
                status = verify_success(candidate)
            promote_report = json.loads((staging / "promote.json").read_text(encoding="utf-8"))

        self.assertFalse(status["promote_passed"])
        self.assertEqual(status["promote_failure_class"], "unsafe_staging_artifact")
        self.assertEqual(promote_report["failure_class"], "unsafe_staging_artifact")
        self.assertNotIn(RAW_SENTINEL, json.dumps(promote_report, ensure_ascii=False))

    def test_promote_rejects_staging_changed_after_validation_before_copy(self) -> None:
        from scripts.hermes_postprocess import staging as staging_module
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import PromoteError, promote_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            _write_valid_staging(candidate, lecture_root)
            staging = Path(candidate.staging_dir)
            (staging / "validation-correction.json").write_text(_passing_validation_report(), encoding="utf-8")
            (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")
            original_rerun = staging_module._rerun_required_validators

            def mutate_after_validation(candidate_arg) -> None:
                original_rerun(candidate_arg)
                (staging / "correction.txt").write_text("tampered after validation", encoding="utf-8")

            with patch.object(staging_module, "_rerun_required_validators", side_effect=mutate_after_validation):
                with self.assertRaises(PromoteError) as ctx:
                    promote_candidate(candidate, allow_promote=True)
            final_txt_exists = os.path.lexists(Path(candidate.correction_txt_path))

        self.assertEqual(ctx.exception.failure_class, "staging_artifact_changed_after_validation")
        self.assertFalse(final_txt_exists)
        self.assertNotIn(RAW_SENTINEL, json.dumps(ctx.exception.details, ensure_ascii=False))

    def test_unknown_child_wrapper_exception_uses_stable_failure_class(self) -> None:
        from scripts.hermes_postprocess.operator import ChildRunResult, OperatorConfig, run_once

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/bin/false"), stable_for_sec=0)

            def child_runner(*_args: object, **_kwargs: object) -> ChildRunResult:
                raise ValueError("unexpected wrapper failure")

            outcome = run_once(config, child_runner=child_runner, now=FIXED_NOW)
            alert_text = json.dumps(outcome.get("alert"), ensure_ascii=False)
            claim_text = (root / "state" / "hermes_postprocess" / "claims" / "260504DS_2.json").read_text(encoding="utf-8")

        self.assertEqual(outcome["kind"], "failed")
        self.assertIn("operator_wrapper_failed", alert_text + claim_text)
        self.assertNotIn("ValueError", alert_text + claim_text)
        self.assertNotIn(RAW_SENTINEL, alert_text + claim_text)

    def test_child_sandbox_profile_does_not_allow_global_file_read(self) -> None:
        from scripts.hermes_postprocess.operator import OperatorConfig, _child_sandbox_profile, select_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            (root / ".env").write_text("REPO_SECRET=do-not-read\n", encoding="utf-8")
            config = OperatorConfig(repo_root=root, lecture_root=lecture_root, hermes_bin=Path("/opt/hermes/bin/hermes"), stable_for_sec=0)
            candidate = select_candidate(config, now=FIXED_NOW)
            assert candidate is not None

            profile_text = _child_sandbox_profile(candidate, config)

        self.assertNotIn("(allow file-read*)\n", profile_text)
        self.assertIn("(allow file-read* (subpath", profile_text)
        self.assertIn(str(Path(candidate.raw_txt_path)), profile_text)
        self.assertIn(str(Path(candidate.raw_json_path)), profile_text)
        self.assertIn(str(Path(candidate.operator_docs_dir)), profile_text)
        runtime_home = Path(candidate.staging_dir) / ".hermes-runtime"
        self.assertNotIn(f'(allow file-read* (literal "{root / ".env"}"))', profile_text)
        self.assertIn(f'(deny file-read* (literal "{root / ".env"}"))', profile_text)
        self.assertNotIn(f'(allow file-read* (literal "{runtime_home / ".env"}"))', profile_text)
        self.assertIn(f'(deny file-read* (literal "{runtime_home / ".env"}"))', profile_text)
        self.assertNotIn(f'(allow file-read* (subpath "{Path(candidate.raw_txt_path).parent}"))', profile_text)
        self.assertNotIn(f'(allow file-read* (subpath "{Path(candidate.correction_txt_path).parent}"))', profile_text)
        self.assertNotIn(f'(allow file-read* (subpath "{Path(candidate.summary_md_path).parent}"))', profile_text)

    def test_promote_rejects_staging_changed_after_snapshot_before_copy(self) -> None:
        from scripts.hermes_postprocess import staging as staging_module
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import PromoteError, promote_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            _write_valid_staging(candidate, lecture_root)
            staging = Path(candidate.staging_dir)
            (staging / "validation-correction.json").write_text(_passing_validation_report(), encoding="utf-8")
            (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")
            original_verify = staging_module._verify_staging_sources_unchanged

            def mutate_after_snapshot_check(snapshots) -> None:
                original_verify(snapshots)
                (staging / "correction.txt").write_text("tampered after snapshot check", encoding="utf-8")

            with patch.object(staging_module, "_verify_staging_sources_unchanged", side_effect=mutate_after_snapshot_check):
                with self.assertRaises(PromoteError) as ctx:
                    promote_candidate(candidate, allow_promote=True)
            final_txt_exists = os.path.lexists(Path(candidate.correction_txt_path))

        self.assertEqual(ctx.exception.failure_class, "staging_artifact_changed_after_validation")
        self.assertFalse(final_txt_exists)
        self.assertNotIn(RAW_SENTINEL, json.dumps(ctx.exception.details, ensure_ascii=False))


class HermesPostprocessValidatorHardeningTests(unittest.TestCase):
    def test_summary_validator_rejects_placeholder_failure_language(self) -> None:
        from scripts.hermes_postprocess.validators import validate_summary_artifact

        bad_phrases = ["요약할 수 없습니다", "원문을 제공해 주세요", "내용 없음"]
        for phrase in bad_phrases:
            with self.subTest(phrase=phrase):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    corrected_path = root / "correction.txt"
                    summary_path = root / "summary.md"
                    final_path = root / "final" / "summary.md"
                    corrected_path.write_text("corrected transcript", encoding="utf-8")
                    summary_path.write_text(_valid_summary_markdown() + f"\n{phrase}\n", encoding="utf-8")

                    result = validate_summary_artifact(
                        summary_md_path=summary_path,
                        corrected_txt_path=corrected_path,
                        final_md_path=final_path,
                    )

                self.assertFalse(result.passed)
                self.assertEqual(result.failure_class, "summary_semantic_guard_failed")

    def test_misrecognition_candidates_reject_sentence_like_term_fields_without_leaking_body(self) -> None:
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
                                "suspected_wrong": "첫 문장입니다. 두 번째 문장입니다.",
                                "suggested_correct": "database",
                                "scope": "subject",
                                "confidence": "medium",
                                "reason": "검토 필요",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main([
                    "record-misrecognitions",
                    "--candidate-json",
                    str(candidate_json),
                    "--candidates-json",
                    str(candidates_path),
                ])
            output = stdout.getvalue()

        self.assertEqual(code, 2)
        payload = json.loads(output)
        self.assertEqual(payload["failure_class"], "misrecognition_report_invalid")
        self.assertNotIn(RAW_SENTINEL, output)
        self.assertNotIn("첫 문장입니다", output)

    def test_promote_does_not_delete_externally_modified_destination_during_rollback(self) -> None:
        from scripts.hermes_postprocess import staging as staging_module
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import PromoteError, promote_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            _write_valid_staging(candidate, lecture_root)
            staging = Path(candidate.staging_dir)
            (staging / "validation-correction.json").write_text(_passing_validation_report(), encoding="utf-8")
            (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")
            original_copy = staging_module._atomic_copy_no_overwrite
            first_destination: Path | None = None
            calls = 0

            def fail_after_external_destination_change(src: Path, dst: Path, expected_source=None) -> dict[str, str]:
                nonlocal calls, first_destination
                calls += 1
                if calls == 1:
                    metadata = original_copy(src, dst, expected_source)
                    first_destination = dst
                    return metadata
                assert first_destination is not None
                first_destination.write_text("external mutation must survive rollback", encoding="utf-8")
                raise OSError("simulated later copy failure")

            with patch.object(staging_module, "_atomic_copy_no_overwrite", side_effect=fail_after_external_destination_change):
                with self.assertRaises(PromoteError):
                    promote_candidate(candidate, allow_promote=True)

            assert first_destination is not None
            self.assertTrue(first_destination.exists())
            self.assertEqual(first_destination.read_text(encoding="utf-8"), "external mutation must survive rollback")

    def test_promote_rejects_staging_artifact_symlink_sources(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import PromoteError, promote_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            _write_valid_staging(candidate, lecture_root)
            staging = Path(candidate.staging_dir)
            outside = root / "outside-correction.txt"
            outside.write_text("corrected transcript", encoding="utf-8")
            (staging / "correction.txt").unlink()
            os.symlink(outside, staging / "correction.txt")
            (staging / "validation-correction.json").write_text(_passing_validation_report(), encoding="utf-8")
            (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")

            with self.assertRaises(PromoteError) as ctx:
                promote_candidate(candidate, allow_promote=True)
            final_txt_exists = os.path.lexists(Path(candidate.correction_txt_path))

        self.assertEqual(ctx.exception.failure_class, "unsafe_staging_artifact")
        self.assertFalse(final_txt_exists)
        self.assertNotIn(RAW_SENTINEL, json.dumps(ctx.exception.details, ensure_ascii=False))

    def test_promote_rejects_staging_artifact_hardlink_sources(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import PromoteError, promote_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            _write_valid_staging(candidate, lecture_root)
            staging = Path(candidate.staging_dir)
            outside = root / "outside-correction.txt"
            outside.write_text("corrected transcript", encoding="utf-8")
            (staging / "correction.txt").unlink()
            os.link(outside, staging / "correction.txt")
            (staging / "validation-correction.json").write_text(_passing_validation_report(), encoding="utf-8")
            (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")

            with self.assertRaises(PromoteError) as ctx:
                promote_candidate(candidate, allow_promote=True)
            final_txt_exists = os.path.lexists(Path(candidate.correction_txt_path))

        self.assertEqual(ctx.exception.failure_class, "unsafe_staging_artifact")
        self.assertFalse(final_txt_exists)
        self.assertNotIn(RAW_SENTINEL, json.dumps(ctx.exception.details, ensure_ascii=False))

    def test_promote_rejects_staging_artifact_nonregular_type_matrix(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import PromoteError, promote_candidate

        def replace_correction_source(staging_path: Path, kind: str, root: Path) -> socket.socket | None:
            staging_path.unlink()
            if kind == "broken_symlink":
                os.symlink(root / "missing-staging-source.txt", staging_path)
                return None
            if kind == "directory":
                staging_path.mkdir()
                return None
            if kind == "fifo":
                os.mkfifo(staging_path)
                return None
            if kind == "socket":
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.bind(str(staging_path))
                return sock
            raise AssertionError(f"unknown fixture kind: {kind}")

        for kind in ("broken_symlink", "directory", "fifo", "socket"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(dir="/tmp") as tmp:
                root = Path(tmp)
                lecture_root = _make_operator_fixture(root)
                _write_raw_pair(lecture_root, "260504DS_2")
                candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
                assert candidate is not None
                _write_valid_staging(candidate, lecture_root)
                staging = Path(candidate.staging_dir)
                sock = replace_correction_source(staging / "correction.txt", kind, root)
                (staging / "validation-correction.json").write_text(_passing_validation_report(), encoding="utf-8")
                (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")

                try:
                    with self.assertRaises(PromoteError) as ctx:
                        promote_candidate(candidate, allow_promote=True)
                    final_txt_exists = os.path.lexists(Path(candidate.correction_txt_path))
                finally:
                    if sock is not None:
                        sock.close()

                self.assertEqual(ctx.exception.failure_class, "unsafe_staging_artifact")
                details_text = json.dumps(ctx.exception.details, ensure_ascii=False)
                self.assertIn(kind if kind != "broken_symlink" else "symlink", details_text)
                self.assertFalse(final_txt_exists)
                self.assertNotIn(RAW_SENTINEL, details_text)

    def test_promote_disabled_report_declares_metadata_containment(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import promote_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None

            result = promote_candidate(candidate, allow_promote=False)

        self.assertFalse(result["passed"])
        self.assertEqual(result["failure_class"], "promote_disabled")
        _assert_payload_metadata_only(self, result)

    def test_promote_rejects_malformed_validation_report_with_metadata_only_contract(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import PromoteError, promote_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            _write_valid_staging(candidate, lecture_root)
            staging = Path(candidate.staging_dir)
            (staging / "validation-correction.json").write_text("{not-json", encoding="utf-8")
            (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")

            with self.assertRaises(PromoteError) as ctx:
                promote_candidate(candidate, allow_promote=True)

        self.assertEqual(ctx.exception.failure_class, "invalid_validation_report")
        self.assertNotIn(RAW_SENTINEL, str(ctx.exception) + json.dumps(ctx.exception.details, ensure_ascii=False))

    def test_cli_promote_error_report_declares_metadata_containment(self) -> None:
        from scripts.hermes_postprocess.cli import main
        from scripts.hermes_postprocess.picker import find_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            _write_valid_staging(candidate, lecture_root)
            staging = Path(candidate.staging_dir)
            candidate_json = staging / "candidate.json"
            candidate_json.write_text(json.dumps(candidate.to_dict(), ensure_ascii=False), encoding="utf-8")
            (staging / "validation-correction.json").write_text("{not-json", encoding="utf-8")
            (staging / "validation-summary.json").write_text(_passing_validation_report(), encoding="utf-8")
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                code = main(["promote", "--candidate-json", str(candidate_json), "--allow-promote"])
            output = stdout.getvalue()

        self.assertEqual(code, 2)
        payload = json.loads(output)
        self.assertEqual(payload["failure_class"], "invalid_validation_report")
        _assert_payload_metadata_only(self, payload)

    def test_promote_rejects_validation_report_without_schema_version(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import PromoteError, promote_candidate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            _write_valid_staging(candidate, lecture_root)
            staging = Path(candidate.staging_dir)
            (staging / "validation-correction.json").write_text('{"passed": true}', encoding="utf-8")
            (staging / "validation-summary.json").write_text('{"passed": true}', encoding="utf-8")

            with self.assertRaises(PromoteError) as ctx:
                promote_candidate(candidate, allow_promote=True)

        self.assertEqual(ctx.exception.failure_class, "invalid_validation_report")
        self.assertNotIn(RAW_SENTINEL, str(ctx.exception))

    def test_promote_rejects_invalid_summary_input_source_without_leaking_body(self) -> None:
        from scripts.hermes_postprocess.picker import find_candidate
        from scripts.hermes_postprocess.staging import PromoteError, promote_candidate
        from scripts.hermes_postprocess.validators import validate_correction_artifacts, validate_summary_artifact

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture_root = _make_operator_fixture(root)
            _write_raw_pair(lecture_root, "260504DS_2")
            candidate = find_candidate(lecture_root=lecture_root, repo_root=root, stable_for_sec=0)
            assert candidate is not None
            _write_valid_staging(candidate, lecture_root)
            candidate.actions["summary"]["input_source"] = "unknown_source"
            staging = Path(candidate.staging_dir)
            correction_result = validate_correction_artifacts(
                raw_json_path=candidate.raw_json_path,
                correction_txt_path=staging / "correction.txt",
                correction_json_path=staging / "correction.json",
                final_txt_path=candidate.correction_txt_path,
                final_json_path=candidate.correction_json_path,
            )
            summary_result = validate_summary_artifact(
                summary_md_path=staging / "summary.md",
                corrected_txt_path=staging / "correction.txt",
                final_md_path=candidate.summary_md_path,
            )
            (staging / "validation-correction.json").write_text(json.dumps(correction_result.to_dict(), ensure_ascii=False), encoding="utf-8")
            (staging / "validation-summary.json").write_text(json.dumps(summary_result.to_dict(), ensure_ascii=False), encoding="utf-8")

            with self.assertRaises(PromoteError) as ctx:
                promote_candidate(candidate, allow_promote=True)

        self.assertEqual(ctx.exception.failure_class, "summary_validation_failed")
        self.assertNotIn(RAW_SENTINEL, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
