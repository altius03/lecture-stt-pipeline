from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from .contract import (
    CLAIMS_DIR,
    CORRECTION_DIR,
    OPERATOR_DOCS_DIR,
    POSTPROCESS_STATE_DIR,
    PROMPT_DIR,
    STAGING_DIR,
    STATE_DIR,
    SUBJECT_CODES,
    SUMMARY_DIR,
    TRANSCRIPT_DIR,
)
from .schemas import CandidatePaths


DEFAULT_SUBJECT_CODES: tuple[str, ...] = SUBJECT_CODES


def parse_subject_from_stem(stem: str, subject_codes: Iterable[str] = DEFAULT_SUBJECT_CODES) -> str | None:
    """Extract the subject code from stems like 260504DS_2.

    Codes are matched longest-first so DStr is not truncated to DS.
    Unknown or malformed stems return None rather than raising; downstream
    validators decide whether that should block processing.
    """
    if len(stem) < 8 or not stem[:6].isdigit():
        return None
    suffix = stem[6:]
    for code in sorted(subject_codes, key=len, reverse=True):
        if suffix == code:
            return code
        if suffix.startswith(f"{code}_"):
            remainder = suffix[len(code) + 1 :]
            if remainder.isdigit():
                return code
    return None


def resolve_candidate_paths(
    stem: str,
    *,
    lecture_root: str | Path,
    repo_root: str | Path,
    subject_codes: Iterable[str] = DEFAULT_SUBJECT_CODES,
) -> CandidatePaths:
    lecture_root_path = Path(lecture_root).expanduser()
    repo_root_path = Path(repo_root).expanduser()
    subject = parse_subject_from_stem(stem, subject_codes=subject_codes)
    return CandidatePaths(
        stem=stem,
        subject=subject,
        raw_txt_path=lecture_root_path / TRANSCRIPT_DIR / f"{stem}.txt",
        raw_json_path=lecture_root_path / TRANSCRIPT_DIR / f"{stem}.json",
        correction_txt_path=lecture_root_path / CORRECTION_DIR / f"{stem}.txt",
        correction_json_path=lecture_root_path / CORRECTION_DIR / f"{stem}.json",
        summary_md_path=lecture_root_path / SUMMARY_DIR / f"{stem}.md",
        staging_dir=repo_root_path / STATE_DIR / POSTPROCESS_STATE_DIR / STAGING_DIR / stem,
        claim_path=repo_root_path / STATE_DIR / POSTPROCESS_STATE_DIR / CLAIMS_DIR / f"{stem}.json",
        prompt_dir=lecture_root_path / PROMPT_DIR,
        operator_docs_dir=repo_root_path.joinpath(*OPERATOR_DOCS_DIR),
    )


def lecture_root_from_env() -> Path | None:
    raw = os.environ.get("LECTURE_RECORDINGS_ROOT")
    if not raw:
        return None
    return Path(raw).expanduser()
