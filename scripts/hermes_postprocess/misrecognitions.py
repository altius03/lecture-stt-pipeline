from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import Candidate


MISRECOGNITION_KIND = "hermes_postprocess_misrecognition_candidates"
ALLOWED_SCOPES = {"common", "subject"}
ALLOWED_CONFIDENCES = {"low", "medium", "high"}
MAX_TERM_CHARS = 80
MAX_REASON_CHARS = 240
FORBIDDEN_KEYS = {
    "after_context",
    "before_context",
    "context",
    "excerpt",
    "quote",
    "raw_body",
    "raw_excerpt",
    "raw_text",
    "segments",
    "source_excerpt",
    "source_text",
    "transcript",
    "transcript_body",
    "transcript_text",
}


class MisrecognitionError(RuntimeError):
    def __init__(self, failure_class: str, message: str, details: dict[str, Any] | None = None):
        self.failure_class = failure_class
        self.details = details or {}
        super().__init__(message)


def default_pending_jsonl_path(candidate: Candidate) -> Path:
    staging_dir = Path(candidate.staging_dir)
    hermes_postprocess_dir = staging_dir.parent.parent
    return hermes_postprocess_dir / "misrecognitions" / "pending.jsonl"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MisrecognitionError(
            "misrecognition_report_missing",
            "misrecognition candidates file is missing",
            {"path": str(path)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise MisrecognitionError(
            "misrecognition_report_invalid",
            "misrecognition candidates file is not valid JSON",
            {"path": str(path)},
        ) from exc
    if not isinstance(payload, dict):
        raise MisrecognitionError(
            "misrecognition_report_invalid",
            "misrecognition candidates payload must be an object",
            {"path": str(path)},
        )
    return payload


def _forbidden_keys(value: Any) -> list[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            normalized = key_text.lower()
            if normalized in FORBIDDEN_KEYS:
                found.add(key_text)
            found.update(_forbidden_keys(child))
    elif isinstance(value, list):
        for item in value:
            found.update(_forbidden_keys(item))
    return sorted(found)


def _clean_short_text(
    value: Any,
    *,
    field: str,
    max_chars: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise MisrecognitionError(
            "misrecognition_report_invalid",
            f"{field} must be a string",
            {"field": field},
        )
    text = " ".join(value.strip().split())
    if not text and not allow_empty:
        raise MisrecognitionError(
            "misrecognition_report_invalid",
            f"{field} must not be empty",
            {"field": field},
        )
    if len(text) > max_chars:
        raise MisrecognitionError(
            "misrecognition_report_invalid",
            f"{field} is too long for metadata-only review",
            {"field": field, "max_chars": max_chars},
        )
    return text


def _normalize_candidates(payload: dict[str, Any], candidate: Candidate) -> list[dict[str, Any]]:
    forbidden = _forbidden_keys(payload)
    if forbidden:
        raise MisrecognitionError(
            "misrecognition_report_invalid",
            "misrecognition candidates must not include raw excerpts or transcript context fields",
            {"forbidden_keys": forbidden},
        )
    if payload.get("raw_transcript_body_included") is not False:
        raise MisrecognitionError(
            "misrecognition_report_invalid",
            "raw_transcript_body_included must be false",
            {"field": "raw_transcript_body_included"},
        )
    if payload.get("schema_version") != 1:
        raise MisrecognitionError(
            "misrecognition_report_invalid",
            "schema_version must be 1",
            {"field": "schema_version"},
        )
    if payload.get("kind") != MISRECOGNITION_KIND:
        raise MisrecognitionError(
            "misrecognition_report_invalid",
            "kind must identify a misrecognition candidate report",
            {"field": "kind", "expected": MISRECOGNITION_KIND},
        )
    if payload.get("stem") != candidate.stem:
        raise MisrecognitionError(
            "misrecognition_report_invalid",
            "misrecognition candidate stem does not match candidate JSON",
            {"field": "stem", "expected": candidate.stem},
        )
    if payload.get("subject") != candidate.subject:
        raise MisrecognitionError(
            "misrecognition_report_invalid",
            "misrecognition candidate subject does not match candidate JSON",
            {"field": "subject", "expected": candidate.subject},
        )

    raw_items = payload.get("candidates")
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise MisrecognitionError(
            "misrecognition_report_invalid",
            "candidates must be a list",
            {"field": "candidates"},
        )

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise MisrecognitionError(
                "misrecognition_report_invalid",
                "candidate item must be an object",
                {"index": index},
            )
        scope = _clean_short_text(item.get("scope"), field="scope", max_chars=20)
        if scope not in ALLOWED_SCOPES:
            raise MisrecognitionError(
                "misrecognition_report_invalid",
                "scope must be common or subject",
                {"index": index, "field": "scope", "allowed": sorted(ALLOWED_SCOPES)},
            )
        confidence = _clean_short_text(item.get("confidence"), field="confidence", max_chars=20)
        if confidence not in ALLOWED_CONFIDENCES:
            raise MisrecognitionError(
                "misrecognition_report_invalid",
                "confidence must be low, medium, or high",
                {"index": index, "field": "confidence", "allowed": sorted(ALLOWED_CONFIDENCES)},
            )
        normalized.append(
            {
                "schema_version": 1,
                "kind": "hermes_postprocess_misrecognition_pending_item",
                "stem": candidate.stem,
                "subject": candidate.subject,
                "scope": scope,
                "suspected_wrong": _clean_short_text(
                    item.get("suspected_wrong"),
                    field="suspected_wrong",
                    max_chars=MAX_TERM_CHARS,
                ),
                "suggested_correct": _clean_short_text(
                    item.get("suggested_correct"),
                    field="suggested_correct",
                    max_chars=MAX_TERM_CHARS,
                ),
                "confidence": confidence,
                "reason": _clean_short_text(
                    item.get("reason", ""),
                    field="reason",
                    max_chars=MAX_REASON_CHARS,
                    allow_empty=True,
                ),
                "source_stem": candidate.stem,
                "reviewed": False,
                "raw_transcript_body_included": False,
            }
        )
    return normalized


def _dedupe_key(item: dict[str, Any]) -> tuple[str | None, str | None, str, str, str]:
    return (
        item.get("stem"),
        item.get("subject"),
        str(item.get("scope")),
        str(item.get("suspected_wrong")),
        str(item.get("suggested_correct")),
    )


def _existing_keys(path: Path) -> set[tuple[str | None, str | None, str, str, str]]:
    if not path.exists():
        return set()
    keys: set[tuple[str | None, str | None, str, str, str]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            keys.add(_dedupe_key(payload))
    return keys


def _write_review_markdown(
    path: Path,
    candidate: Candidate,
    items: list[dict[str, Any]],
    appended_count: int,
) -> None:
    lines = [
        f"# Misrecognition review: {candidate.stem}",
        "",
        f"- subject: {candidate.subject}",
        "- raw_transcript_body_included: false",
        "- status: pending manual review",
        f"- appended_count: {appended_count}",
        "",
    ]
    if not items:
        lines.extend(["No misrecognition candidates proposed.", ""])
    else:
        lines.extend(
            [
                "| scope | suspected wrong | suggested correct | confidence | reason |",
                "|---|---|---|---|---|",
            ]
        )
        for item in items:
            lines.append(
                "| {scope} | {wrong} | {correct} | {confidence} | {reason} |".format(
                    scope=item["scope"],
                    wrong=item["suspected_wrong"],
                    correct=item["suggested_correct"],
                    confidence=item["confidence"],
                    reason=item["reason"],
                )
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def record_misrecognition_candidates(
    candidate: Candidate,
    candidates_json_path: str | Path,
    *,
    pending_jsonl_path: str | Path | None = None,
) -> dict[str, Any]:
    candidates_path = Path(candidates_json_path).expanduser()
    pending_path = (
        Path(pending_jsonl_path).expanduser()
        if pending_jsonl_path
        else default_pending_jsonl_path(candidate)
    )
    staging_dir = Path(candidate.staging_dir)
    review_path = staging_dir / "misrecognitions-review.md"

    payload = _read_json(candidates_path)
    items = _normalize_candidates(payload, candidate)
    now = datetime.now(timezone.utc).isoformat()
    existing = _existing_keys(pending_path)
    new_items: list[dict[str, Any]] = []
    duplicate_count = 0
    for item in items:
        key = _dedupe_key(item)
        if key in existing:
            duplicate_count += 1
            continue
        existing.add(key)
        record = dict(item)
        record["created_at"] = now
        new_items.append(record)

    pending_path.parent.mkdir(parents=True, exist_ok=True)
    if new_items:
        with pending_path.open("a", encoding="utf-8") as handle:
            for item in new_items:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")

    staging_dir.mkdir(parents=True, exist_ok=True)
    _write_review_markdown(review_path, candidate, items, len(new_items))

    report = {
        "schema_version": 1,
        "passed": True,
        "failure_class": None,
        "message": "misrecognition candidates recorded for manual review",
        "stem": candidate.stem,
        "subject": candidate.subject,
        "candidates_json_path": str(candidates_path),
        "pending_jsonl_path": str(pending_path),
        "review_md_path": str(review_path),
        "input_count": len(items),
        "appended_count": len(new_items),
        "duplicate_count": duplicate_count,
        "raw_transcript_body_included": False,
    }
    (staging_dir / "misrecognitions-record.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report
