from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract import SCHEMA_VERSION


@dataclass(frozen=True)
class CandidatePaths:
    stem: str
    subject: str | None
    raw_txt_path: Path
    raw_json_path: Path
    correction_txt_path: Path
    correction_json_path: Path
    summary_md_path: Path
    staging_dir: Path
    claim_path: Path
    prompt_dir: Path
    operator_docs_dir: Path

    def to_dict(self) -> dict[str, str | None]:
        return {
            "stem": self.stem,
            "subject": self.subject,
            "raw_txt_path": str(self.raw_txt_path),
            "raw_json_path": str(self.raw_json_path),
            "correction_txt_path": str(self.correction_txt_path),
            "correction_json_path": str(self.correction_json_path),
            "summary_md_path": str(self.summary_md_path),
            "staging_dir": str(self.staging_dir),
            "claim_path": str(self.claim_path),
            "prompt_dir": str(self.prompt_dir),
            "operator_docs_dir": str(self.operator_docs_dir),
        }


@dataclass(frozen=True)
class Candidate:
    paths: CandidatePaths
    actions: dict[str, dict[str, Any]]
    blocked_reasons: tuple[str, ...] = ()

    @property
    def stem(self) -> str:
        return self.paths.stem

    @property
    def subject(self) -> str | None:
        return self.paths.subject

    @property
    def raw_txt_path(self) -> Path:
        return self.paths.raw_txt_path

    @property
    def raw_json_path(self) -> Path:
        return self.paths.raw_json_path

    @property
    def correction_txt_path(self) -> Path:
        return self.paths.correction_txt_path

    @property
    def correction_json_path(self) -> Path:
        return self.paths.correction_json_path

    @property
    def summary_md_path(self) -> Path:
        return self.paths.summary_md_path

    @property
    def staging_dir(self) -> Path:
        return self.paths.staging_dir

    @property
    def claim_path(self) -> Path:
        return self.paths.claim_path

    @property
    def prompt_dir(self) -> Path:
        return self.paths.prompt_dir

    @property
    def operator_docs_dir(self) -> Path:
        return self.paths.operator_docs_dir

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "candidate",
            **self.paths.to_dict(),
            "actions": self.actions,
            "blocked_reasons": list(self.blocked_reasons),
        }


@dataclass(frozen=True)
class PromptBundle:
    prompt_dir: Path
    subject: str | None
    base_prompt: str
    common_glossary: str
    subject_glossary: str | None
    base_prompt_path: Path
    common_glossary_path: Path
    subject_glossary_path: Path | None

    def metadata(self) -> dict[str, str | None]:
        return {
            "prompt_dir": str(self.prompt_dir),
            "subject": self.subject,
            "base_prompt_path": str(self.base_prompt_path),
            "common_glossary_path": str(self.common_glossary_path),
            "subject_glossary_path": str(self.subject_glossary_path) if self.subject_glossary_path else None,
        }


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    failure_class: str | None = None
    message: str = ""
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "passed": self.passed,
            "failure_class": self.failure_class,
            "message": self.message,
            "details": self.details or {},
            "raw_transcript_body_included": False,
        }


def candidate_from_dict(payload: dict[str, Any]) -> Candidate:
    paths = CandidatePaths(
        stem=str(payload["stem"]),
        subject=payload.get("subject"),
        raw_txt_path=Path(payload["raw_txt_path"]),
        raw_json_path=Path(payload["raw_json_path"]),
        correction_txt_path=Path(payload["correction_txt_path"]),
        correction_json_path=Path(payload["correction_json_path"]),
        summary_md_path=Path(payload["summary_md_path"]),
        staging_dir=Path(payload["staging_dir"]),
        claim_path=Path(payload["claim_path"]),
        prompt_dir=Path(payload["prompt_dir"]),
        operator_docs_dir=Path(payload["operator_docs_dir"]),
    )
    actions = payload.get("actions")
    if not isinstance(actions, dict):
        actions = {
            "correction": {"needed": True, "reason": "candidate_json_missing_actions"},
            "summary": {"needed": True, "input_source": "staging_correction"},
        }
    blocked = payload.get("blocked_reasons") or []
    return Candidate(paths=paths, actions=actions, blocked_reasons=tuple(str(item) for item in blocked))
