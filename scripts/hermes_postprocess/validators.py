from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schemas import ValidationResult


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

_ASSISTANT_WRAPPER_PREFIXES = (
    "```",
    "Here is",
    "Here's",
    "아래는",
    "교정본:",
    "교정된 전사",
    "요약",
)


def _failure(failure_class: str, message: str, **details: Any) -> ValidationResult:
    return ValidationResult(False, failure_class=failure_class, message=message, details=details)


def _load_json(path: Path, failure_class: str) -> tuple[Any | None, ValidationResult | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, _failure(failure_class, f"missing JSON file: {path}", path=str(path))
    except json.JSONDecodeError as exc:
        return None, _failure("invalid_correction_json", f"invalid JSON: {exc}", path=str(path))


def _json_path(parts: tuple[str | int, ...]) -> str:
    if not parts:
        return "$"
    rendered = "$"
    for part in parts:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered += f".{part}"
    return rendered


def _compare_correction_json_contract(original: Any, corrected: Any, path: tuple[str | int, ...] = ()) -> ValidationResult | None:
    if isinstance(original, dict):
        if not isinstance(corrected, dict):
            return _failure("correction_json_structure_changed", "JSON object structure changed", path=_json_path(path))
        if list(original.keys()) != list(corrected.keys()):
            return _failure(
                "correction_json_structure_changed",
                "JSON object keys or ordering changed",
                path=_json_path(path),
                original_keys=list(original.keys()),
                corrected_keys=list(corrected.keys()),
            )
        for key in original:
            child_path = (*path, key)
            if key == "text":
                if not isinstance(corrected[key], str):
                    return _failure("invalid_correction_json", "text field must remain a string", path=_json_path(child_path))
                continue
            failure = _compare_correction_json_contract(original[key], corrected[key], child_path)
            if failure:
                return failure
        return None
    if isinstance(original, list):
        if not isinstance(corrected, list) or len(original) != len(corrected):
            return _failure("correction_json_structure_changed", "JSON array structure changed", path=_json_path(path))
        for index, (original_item, corrected_item) in enumerate(zip(original, corrected)):
            failure = _compare_correction_json_contract(original_item, corrected_item, (*path, index))
            if failure:
                return failure
        return None
    if original != corrected:
        return _failure("correction_non_text_metadata_changed", "non-text JSON metadata changed", path=_json_path(path))
    return None


def validate_correction_artifacts(
    *,
    raw_json_path: str | Path,
    correction_txt_path: str | Path,
    correction_json_path: str | Path,
    final_txt_path: str | Path,
    final_json_path: str | Path,
) -> ValidationResult:
    raw_json = Path(raw_json_path)
    correction_txt = Path(correction_txt_path)
    correction_json = Path(correction_json_path)
    final_txt = Path(final_txt_path)
    final_json = Path(final_json_path)

    existing_finals = [str(path) for path in (final_txt, final_json) if path.exists()]
    if existing_finals:
        return _failure("output_already_exists", "final correction output already exists", paths=existing_finals)

    try:
        correction_text = correction_txt.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _failure("correction_validation_failed", f"missing correction txt: {correction_txt}", path=str(correction_txt))
    if not correction_text.strip():
        return _failure("correction_validation_failed", "correction txt is empty", path=str(correction_txt))
    stripped = correction_text.lstrip()
    if any(stripped.startswith(prefix) for prefix in _ASSISTANT_WRAPPER_PREFIXES):
        return _failure("correction_validation_failed", "correction txt contains assistant wrapper text", path=str(correction_txt))

    original, error = _load_json(raw_json, "missing_raw_json")
    if error:
        return error
    corrected, error = _load_json(correction_json, "invalid_correction_json")
    if error:
        return error
    assert original is not None and corrected is not None
    if not isinstance(original, dict) or not isinstance(corrected, dict):
        return _failure("invalid_correction_json", "correction JSON top-level value must be an object")

    original_segments = original.get("segments", [])
    corrected_segments = corrected.get("segments", [])
    if not isinstance(original_segments, list) or not isinstance(corrected_segments, list):
        return _failure("invalid_correction_json", "segments must be arrays")
    if len(original_segments) != len(corrected_segments):
        return _failure(
            "segment_count_mismatch",
            "segment count changed",
            original_count=len(original_segments),
            corrected_count=len(corrected_segments),
        )

    for index, (original_segment, corrected_segment) in enumerate(zip(original_segments, corrected_segments)):
        if not isinstance(original_segment, dict) or not isinstance(corrected_segment, dict):
            return _failure("invalid_correction_json", "segment item must be an object", index=index)
        for key in ("id", "start", "end"):
            if original_segment.get(key) != corrected_segment.get(key):
                return _failure(
                    "segment_metadata_changed",
                    f"segment {index} {key} changed",
                    index=index,
                    key=key,
                    original=original_segment.get(key),
                    corrected=corrected_segment.get(key),
                )

    structure_failure = _compare_correction_json_contract(original, corrected)
    if structure_failure:
        return structure_failure

    return ValidationResult(True, message="correction validation passed")


def validate_summary_artifact(
    *,
    summary_md_path: str | Path,
    corrected_txt_path: str | Path,
    final_md_path: str | Path,
    long_input_threshold: int = 1000,
    min_summary_chars_for_long_input: int = 700,
    min_summary_ratio_for_long_input: float = 0.18,
) -> ValidationResult:
    summary_path = Path(summary_md_path)
    corrected_path = Path(corrected_txt_path)
    final_path = Path(final_md_path)

    if final_path.exists():
        return _failure("output_already_exists", "final summary output already exists", path=str(final_path))
    try:
        summary = summary_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _failure("summary_validation_failed", f"missing summary markdown: {summary_path}", path=str(summary_path))
    if not summary.strip():
        return _failure("summary_validation_failed", "summary markdown is empty", path=str(summary_path))

    missing = [heading for heading in REQUIRED_SUMMARY_HEADINGS if heading not in summary]
    if missing:
        return _failure("summary_required_heading_missing", "required summary headings are missing", missing=missing)

    try:
        corrected = corrected_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _failure("summary_validation_failed", f"missing corrected input: {corrected_path}", path=str(corrected_path))

    corrected_len = len(corrected.strip())
    summary_len = len(summary.strip())
    if corrected_len >= long_input_threshold:
        required_len = max(min_summary_chars_for_long_input, int(corrected_len * min_summary_ratio_for_long_input))
        if summary_len < required_len:
            return _failure(
                "summary_too_short",
                "summary is too short for a long corrected transcript",
                corrected_chars=corrected_len,
                summary_chars=summary_len,
                required_chars=required_len,
            )

    if corrected_len > 500 and corrected.strip()[:500] in summary:
        return _failure("summary_validation_failed", "summary appears to contain a raw transcript dump")

    return ValidationResult(True, message="summary validation passed")
