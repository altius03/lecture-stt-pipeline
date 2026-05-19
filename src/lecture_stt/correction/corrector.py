"""Provider-neutral helpers for transcript correction outputs.

Automatic API-backed correction is intentionally disabled in this repository state.
The correction stage remains as a manual/provider-neutral workflow: raw transcript
pairs stay in 02_transcripts until a human or future provider writes reviewed
outputs to 03_correction.
"""
from __future__ import annotations

import json
import re
from typing import NamedTuple


_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]+?)\s*```")
_REVIEW_SEP_RE = re.compile(r"\n---\s*\n", re.MULTILINE)


class CorrectionResult(NamedTuple):
    corrected: str
    review_notes: str | None


class CorrectionProviderUnavailable(RuntimeError):
    """Raised when legacy code tries to use automatic correction."""


class Corrector:
    """Disabled compatibility shim for the removed automatic correction provider."""

    def __init__(self, *args: object, **kwargs: object):
        raise CorrectionProviderUnavailable(
            "Automatic transcript correction is disabled; use the manual correction workflow."
        )

    @staticmethod
    def split_review(raw: str) -> CorrectionResult:
        """Split corrected content and optional review notes from provider output text."""
        corrected, review = _split_review(raw)
        return CorrectionResult(corrected, review)

    @staticmethod
    def extract_json(text: str) -> str:
        """Extract JSON text from a plain response or fenced code block."""
        return _extract_json(text)

    @staticmethod
    def validate_json_structure(original_json: str, corrected_json: str) -> None:
        """Validate that segment metadata is preserved after correction."""
        _validate_json_structure(original_json, corrected_json)


def _split_review(raw: str) -> tuple[str, str | None]:
    parts = _REVIEW_SEP_RE.split(raw, maxsplit=1)
    main = parts[0].strip()
    review = parts[1].strip() if len(parts) > 1 else None
    return main, review


def _extract_json(text: str) -> str:
    match = _CODE_BLOCK_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _validate_json_structure(original_json: str, corrected_json: str) -> None:
    try:
        orig = json.loads(original_json)
        corr = json.loads(corrected_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Corrected output is not valid JSON: {exc}") from exc

    orig_segs = orig.get("segments", [])
    corr_segs = corr.get("segments", [])

    if len(orig_segs) != len(corr_segs):
        raise ValueError(
            f"Segment count changed after correction: {len(orig_segs)} → {len(corr_segs)}"
        )

    for i, (original, corrected) in enumerate(zip(orig_segs, corr_segs)):
        for key in ("id", "start", "end"):
            if original.get(key) != corrected.get(key):
                raise ValueError(
                    f"Segment {i}: '{key}' changed from {original.get(key)!r} "
                    f"to {corrected.get(key)!r}"
                )
