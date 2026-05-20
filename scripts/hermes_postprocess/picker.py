from __future__ import annotations

import time
from pathlib import Path

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
    transcript_dir = lecture_root / "02_transcripts"
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


def build_action_plan(paths: CandidatePaths) -> tuple[dict[str, dict[str, object]], tuple[str, ...]]:
    correction_txt_exists = paths.correction_txt_path.exists()
    correction_json_exists = paths.correction_json_path.exists()
    correction_complete = correction_txt_exists and correction_json_exists
    correction_partial = correction_txt_exists != correction_json_exists
    summary_exists = paths.summary_md_path.exists()

    blocked: list[str] = []
    if correction_partial:
        blocked.append("partial_correction_final_output")

    correction_needed = not correction_txt_exists and not correction_json_exists
    if correction_partial:
        correction_needed = False

    summary_needed = not summary_exists
    if not summary_needed:
        summary_input_source = None
    elif correction_complete:
        summary_input_source = "final_correction"
    elif correction_needed:
        summary_input_source = "staging_correction"
    else:
        summary_input_source = None
        blocked.append("summary_waiting_for_complete_correction")

    actions: dict[str, dict[str, object]] = {
        "correction": {
            "needed": correction_needed,
            "reason": "missing_final_pair" if correction_needed else "final_pair_exists" if correction_complete else "blocked",
            "final_txt_exists": correction_txt_exists,
            "final_json_exists": correction_json_exists,
        },
        "summary": {
            "needed": summary_needed,
            "reason": "missing_final_markdown" if summary_needed else "final_markdown_exists",
            "input_source": summary_input_source,
            "final_md_exists": summary_exists,
        },
    }
    return actions, tuple(blocked)


def is_complete(paths: CandidatePaths) -> bool:
    return paths.correction_txt_path.exists() and paths.correction_json_path.exists() and paths.summary_md_path.exists()


def find_candidate(
    *,
    lecture_root: str | Path,
    repo_root: str | Path,
    stable_for_sec: int = 60,
    skip_claimed: bool = True,
) -> Candidate | None:
    lecture_root_path = Path(lecture_root).expanduser()
    repo_root_path = Path(repo_root).expanduser()
    for stem in _candidate_stems(lecture_root_path, stable_for_sec=stable_for_sec):
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
        return {"schema_version": 1, "status": "no_candidate", "reason": "no raw transcript without correction/summary"}
    return candidate.to_dict()
