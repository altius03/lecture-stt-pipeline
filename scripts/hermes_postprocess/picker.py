from __future__ import annotations

import json
import time
from pathlib import Path

from .contract import SCHEMA_VERSION, TRANSCRIPT_DIR
from .finals import final_entry_lexists, final_regular_exists, unsupported_final_entries
from .paths import resolve_candidate_paths
from .schemas import Candidate, CandidatePaths


_TEMP_SUFFIXES = (".tmp", ".part", ".download")


def _is_temporary_name(path: Path) -> bool:
    name = path.name
    lower = name.lower()
    return name.startswith(".") or name.startswith("~") or lower.endswith(_TEMP_SUFFIXES)


def _is_stable_pair(txt_path: Path, json_path: Path, *, now: float, stable_for_sec: int) -> bool:
    if stable_for_sec <= 0:
        return True
    try:
        newest_mtime = max(txt_path.stat().st_mtime, json_path.stat().st_mtime)
    except OSError:
        return False
    return now - newest_mtime >= stable_for_sec


def _candidate_stems(lecture_root: Path, *, stable_for_sec: int) -> list[str]:
    transcript_dir = lecture_root / TRANSCRIPT_DIR
    if not transcript_dir.exists():
        return []
    now = time.time()
    stems: list[str] = []
    for txt_path in transcript_dir.glob("*.txt"):
        if _is_temporary_name(txt_path):
            continue
        json_path = transcript_dir / f"{txt_path.stem}.json"
        if not json_path.exists() or _is_temporary_name(json_path):
            continue
        if not _is_stable_pair(txt_path, json_path, now=now, stable_for_sec=stable_for_sec):
            continue
        stems.append(txt_path.stem)
    return sorted(set(stems))


def _should_skip_for_bad_quality(transcript_dir: Path, stem: str) -> bool:
    scorecard_path = transcript_dir / f"{stem}.quality.json"
    if not scorecard_path.exists():
        return False
    try:
        payload = json.loads(scorecard_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    if not isinstance(payload, dict):
        return True
    health = payload.get("health")
    if not isinstance(health, str):
        return True
    normalized_health = health.strip().lower()
    if normalized_health not in {"good", "warn", "bad"}:
        return True
    return normalized_health == "bad"


def build_action_plan(paths: CandidatePaths) -> tuple[dict[str, dict[str, object]], tuple[str, ...]]:
    correction_txt_entry_exists = final_entry_lexists(paths.correction_txt_path)
    correction_json_entry_exists = final_entry_lexists(paths.correction_json_path)
    summary_entry_exists = final_entry_lexists(paths.summary_md_path)
    correction_txt_regular = final_regular_exists(paths.correction_txt_path)
    correction_json_regular = final_regular_exists(paths.correction_json_path)
    summary_regular = final_regular_exists(paths.summary_md_path)
    correction_complete = correction_txt_regular and correction_json_regular
    correction_partial = (correction_txt_entry_exists or correction_json_entry_exists) and not correction_complete

    blocked: list[str] = []
    unsupported = unsupported_final_entries(
        (paths.correction_txt_path, paths.correction_json_path, paths.summary_md_path)
    )
    if unsupported:
        blocked.append("preexisting_final_artifact_unsupported_type")
    if correction_partial and not unsupported:
        blocked.append("partial_correction_final_output")

    correction_needed = not correction_txt_entry_exists and not correction_json_entry_exists
    if correction_partial:
        correction_needed = False

    summary_needed = not summary_entry_exists
    if not summary_needed:
        summary_input_source = None
    elif correction_complete:
        summary_input_source = "final_correction"
    elif correction_needed:
        summary_input_source = "staging_correction"
    else:
        summary_input_source = None
        if "summary_waiting_for_complete_correction" not in blocked and not unsupported:
            blocked.append("summary_waiting_for_complete_correction")

    actions: dict[str, dict[str, object]] = {
        "correction": {
            "needed": correction_needed,
            "reason": "missing_final_pair" if correction_needed else "final_pair_exists" if correction_complete else "blocked",
            "final_txt_exists": correction_txt_entry_exists,
            "final_json_exists": correction_json_entry_exists,
            "final_txt_regular": correction_txt_regular,
            "final_json_regular": correction_json_regular,
        },
        "summary": {
            "needed": summary_needed,
            "reason": "missing_final_markdown" if summary_needed else "final_markdown_exists" if summary_regular else "blocked",
            "input_source": summary_input_source,
            "final_md_exists": summary_entry_exists,
            "final_md_regular": summary_regular,
        },
    }
    return actions, tuple(blocked)


def is_complete(paths: CandidatePaths) -> bool:
    return all(
        final_regular_exists(path)
        for path in (paths.correction_txt_path, paths.correction_json_path, paths.summary_md_path)
    )


def find_candidate(
    *,
    lecture_root: str | Path,
    repo_root: str | Path,
    stable_for_sec: int = 60,
    skip_claimed: bool = True,
) -> Candidate | None:
    lecture_root_path = Path(lecture_root).expanduser()
    repo_root_path = Path(repo_root).expanduser()
    transcript_dir = lecture_root_path / TRANSCRIPT_DIR
    for stem in _candidate_stems(lecture_root_path, stable_for_sec=stable_for_sec):
        if _should_skip_for_bad_quality(transcript_dir, stem):
            continue
        paths = resolve_candidate_paths(stem, lecture_root=lecture_root_path, repo_root=repo_root_path)
        if skip_claimed and paths.claim_path.exists():
            continue
        if is_complete(paths):
            continue
        actions, blocked = build_action_plan(paths)
        return Candidate(paths=paths, actions=actions, blocked_reasons=blocked)
    return None


def dry_run_payload(*, lecture_root: str | Path, repo_root: str | Path, stable_for_sec: int = 0) -> dict[str, object]:
    candidate = find_candidate(lecture_root=lecture_root, repo_root=repo_root, stable_for_sec=stable_for_sec)
    if candidate is None:
        return {"schema_version": SCHEMA_VERSION, "status": "no_candidate", "reason": "no raw transcript without correction/summary"}
    return candidate.to_dict()
