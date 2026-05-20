from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - optional fallback for minimal envs
    yaml = None  # type: ignore

from .misrecognitions import MisrecognitionError, record_misrecognition_candidates
from .picker import dry_run_payload
from .schemas import Candidate, candidate_from_dict
from .staging import PromoteError, promote_candidate
from .validators import validate_correction_artifacts, validate_summary_artifact


def _json_dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _repo_root_from_args(raw: str | None) -> Path:
    return Path(raw).expanduser() if raw else Path.cwd()


def _lecture_root_from_config(repo_root: Path) -> Path | None:
    if yaml is None:
        return None
    config_path = repo_root / "config" / "config.yaml"
    if not config_path.exists():
        return None
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    paths = config.get("paths") or {}
    transcript_folder = paths.get("transcript_folder")
    if not isinstance(transcript_folder, str):
        return None
    expanded = os.path.expandvars(os.path.expanduser(transcript_folder))
    if "$" in expanded:
        return None
    transcript_path = Path(expanded)
    if transcript_path.name == "02_transcripts":
        return transcript_path.parent
    return None


def _resolve_lecture_root(raw: str | None, repo_root: Path) -> Path:
    if raw:
        return Path(raw).expanduser()
    env_root = os.environ.get("LECTURE_RECORDINGS_ROOT")
    if env_root:
        return Path(env_root).expanduser()
    config_root = _lecture_root_from_config(repo_root)
    if config_root:
        return config_root
    raise SystemExit("lecture root not found: pass --lecture-root or set LECTURE_RECORDINGS_ROOT")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="lecture_stt Hermes postprocess helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    dry_run = subparsers.add_parser("dry-run", help="Discover at most one candidate and print metadata-only JSON")
    dry_run.add_argument("--lecture-root", help="lecture_recordings root; defaults to LECTURE_RECORDINGS_ROOT/config")
    dry_run.add_argument("--repo-root", help="repository root; defaults to current working directory")
    dry_run.add_argument("--stable-for-sec", type=int, default=0, help="minimum age for transcript pair")
    dry_run.add_argument("--silent-no-candidate", action="store_true", help="print nothing when no candidate is found")

    validate_correction = subparsers.add_parser("validate-correction", help="Validate staged correction artifacts")
    validate_correction.add_argument("--raw-json", required=True)
    validate_correction.add_argument("--correction-txt", required=True)
    validate_correction.add_argument("--correction-json", required=True)
    validate_correction.add_argument("--final-txt", required=True)
    validate_correction.add_argument("--final-json", required=True)
    validate_correction.add_argument("--report-path")

    validate_summary = subparsers.add_parser("validate-summary", help="Validate staged summary artifact")
    validate_summary.add_argument("--summary-md", required=True)
    validate_summary.add_argument("--corrected-txt", required=True)
    validate_summary.add_argument("--final-md", required=True)
    validate_summary.add_argument("--report-path")

    promote = subparsers.add_parser("promote", help="Promote staged artifacts when explicitly enabled")
    promote.add_argument("--candidate-json", required=True)
    promote.add_argument("--allow-promote", action="store_true")
    promote.add_argument("--report-path")

    misrecognitions = subparsers.add_parser(
        "record-misrecognitions",
        help="Append metadata-only misrecognition candidates to the local manual-review queue",
    )
    misrecognitions.add_argument("--candidate-json", required=True)
    misrecognitions.add_argument("--candidates-json", required=True)
    misrecognitions.add_argument("--pending-jsonl")
    misrecognitions.add_argument("--report-path")

    return parser


def _write_optional_report(path: str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_json_dump(payload), encoding="utf-8")


def _candidate_error(failure_class: str, message: str, path: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "passed": False,
        "failure_class": failure_class,
        "message": message,
        "details": {"path": path},
        "stem": None,
        "raw_transcript_body_included": False,
    }


def _load_candidate(path: str) -> tuple[Candidate | None, dict[str, Any] | None]:
    candidate_path = Path(path).expanduser()
    try:
        payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, _candidate_error("missing_candidate_json", "candidate JSON file is missing", str(candidate_path))
    except json.JSONDecodeError:
        return None, _candidate_error("invalid_candidate_json", "candidate JSON file is not valid JSON", str(candidate_path))
    if not isinstance(payload, dict):
        return None, _candidate_error("invalid_candidate_json", "candidate JSON payload must be an object", str(candidate_path))
    try:
        return candidate_from_dict(payload), None
    except (KeyError, TypeError, ValueError):
        return None, _candidate_error("invalid_candidate_json", "candidate JSON payload is missing required metadata fields", str(candidate_path))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "dry-run":
        repo_root = _repo_root_from_args(args.repo_root)
        lecture_root = _resolve_lecture_root(args.lecture_root, repo_root)
        payload = dry_run_payload(lecture_root=lecture_root, repo_root=repo_root, stable_for_sec=args.stable_for_sec)
        if payload.get("status") == "no_candidate" and args.silent_no_candidate:
            return 0
        sys.stdout.write(_json_dump(payload))
        return 0

    if args.command == "validate-correction":
        result = validate_correction_artifacts(
            raw_json_path=args.raw_json,
            correction_txt_path=args.correction_txt,
            correction_json_path=args.correction_json,
            final_txt_path=args.final_txt,
            final_json_path=args.final_json,
        ).to_dict()
        _write_optional_report(args.report_path, result)
        sys.stdout.write(_json_dump(result))
        return 0 if result["passed"] else 2

    if args.command == "validate-summary":
        result = validate_summary_artifact(
            summary_md_path=args.summary_md,
            corrected_txt_path=args.corrected_txt,
            final_md_path=args.final_md,
        ).to_dict()
        _write_optional_report(args.report_path, result)
        sys.stdout.write(_json_dump(result))
        return 0 if result["passed"] else 2

    if args.command == "promote":
        candidate, load_error = _load_candidate(args.candidate_json)
        if load_error:
            _write_optional_report(args.report_path, load_error)
            sys.stdout.write(_json_dump(load_error))
            return 2
        assert candidate is not None
        try:
            result = promote_candidate(candidate, allow_promote=args.allow_promote)
        except PromoteError as exc:
            result = {
                "schema_version": 1,
                "passed": False,
                "failure_class": exc.failure_class,
                "message": str(exc),
                "details": exc.details,
                "stem": candidate.stem,
            }
        _write_optional_report(args.report_path, result)
        sys.stdout.write(_json_dump(result))
        return 0 if result.get("passed") else 2

    if args.command == "record-misrecognitions":
        candidate, load_error = _load_candidate(args.candidate_json)
        if load_error:
            _write_optional_report(args.report_path, load_error)
            sys.stdout.write(_json_dump(load_error))
            return 2
        assert candidate is not None
        try:
            result = record_misrecognition_candidates(
                candidate,
                args.candidates_json,
                pending_jsonl_path=args.pending_jsonl,
            )
        except MisrecognitionError as exc:
            result = {
                "schema_version": 1,
                "passed": False,
                "failure_class": exc.failure_class,
                "message": str(exc),
                "details": exc.details,
                "stem": candidate.stem,
                "raw_transcript_body_included": False,
            }
        _write_optional_report(args.report_path, result)
        sys.stdout.write(_json_dump(result))
        return 0 if result.get("passed") else 2

    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
