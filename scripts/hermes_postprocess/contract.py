from __future__ import annotations

SCHEMA_VERSION = 1

SUBJECT_CODES: tuple[str, ...] = ("DStr", "Unix", "OOP", "LC", "DS", "LA")

TRANSCRIPT_DIR = "02_transcripts"
CORRECTION_DIR = "03_correction"
SUMMARY_DIR = "04_summarize"
PROMPT_DIR = "05_prompt"

STATE_DIR = "state"
POSTPROCESS_STATE_DIR = "hermes_postprocess"
STAGING_DIR = "staging"
CLAIMS_DIR = "claims"
CRON_LOGS_DIR = "cron_logs"
MISRECOGNITIONS_DIR = "misrecognitions"
BASELINE_FILENAME = "cron-baseline.json"
OPERATOR_DOCS_DIR = ("docs", "operators", "hermes-postprocess")

REQUIRED_SUMMARY_HEADINGS: tuple[str, ...] = (
    "# ",
    "## 핵심 개요",
    "## 주요 개념",
    "## 세부 내용",
    "## 예시 / 코드 / 수식",
    "## 헷갈리기 쉬운 점",
    "## 시험·과제·교수 강조사항",
    "## 복습 질문",
)

SUMMARY_FAILURE_PHRASES: tuple[str, ...] = (
    "요약할 수 없습니다",
    "원문을 제공해 주세요",
    "원문이 제공되지 않았습니다",
    "내용 없음",
    "정보가 부족합니다",
    "I cannot summarize",
    "No transcript provided",
)

CLAIM_ACTIVE_STATUSES: frozenset[str] = frozenset({"claimed", "running"})
CLAIM_TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "blocked"})

FAILURE_CLASSES: frozenset[str] = frozenset(
    {
        "missing_candidate_json",
        "invalid_candidate_json",
        "missing_raw_txt",
        "missing_raw_json",
        "unsafe_raw_artifact",
        "missing_prompt_dir",
        "missing_base_prompt",
        "missing_common_glossary",
        "output_already_exists",
        "partial_correction_final_output",
        "summary_waiting_for_complete_correction",
        "promote_disabled",
        "promote_validation_missing",
        "stale_claim",
        "icloud_unavailable",
        "model_timeout",
        "model_interrupted",
        "operator_wrapper_failed",
        "child_hermes_failed",
        "postprocess_verification_failed",
        "preexisting_final_artifact_unsupported_type",
        "unauthorized_final_output_created_by_child",
        "unauthorized_final_output_modified_by_child",
        "unsafe_staging_artifact",
        "staging_artifact_changed_after_validation",
        "invalid_validation_report",
        "promote_validation_failed",
        "summary_validation_skipped",
        "sandbox_exec_missing",
        "invalid_correction_json",
        "correction_json_structure_changed",
        "correction_non_text_metadata_changed",
        "segment_count_mismatch",
        "segment_metadata_changed",
        "correction_validation_failed",
        "summary_required_heading_missing",
        "summary_too_short",
        "summary_validation_failed",
        "summary_semantic_guard_failed",
        "misrecognition_report_missing",
        "misrecognition_report_invalid",
        "promote_failed",
    }
)
