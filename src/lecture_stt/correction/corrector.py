"""Claude API를 사용한 ASR 전사문 교정 모듈.

txt 교정과 json 교정을 각각 처리한다.
json은 segments[].text 값만 교정하고 나머지 구조는 그대로 반환한다.
"""
from __future__ import annotations

import json
import logging
import re
from typing import NamedTuple

import anthropic


logger = logging.getLogger(__name__)

# JSON 응답에서 ```json ... ``` 블록을 걷어낸다
_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]+?)\s*```")

# Claude가 출력 끝에 붙이는 "---" 구분선 이후 [확인 필요] 섹션 분리
_REVIEW_SEP_RE = re.compile(r"\n---\s*\n", re.MULTILINE)


class CorrectionResult(NamedTuple):
    corrected: str          # 교정된 본문 (txt) 또는 교정된 JSON 문자열
    review_notes: str | None  # [확인 필요] 섹션 (있을 때만)


class Corrector:
    def __init__(self, api_key: str, model: str, max_tokens: int = 8192):
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens

    # ── txt 교정 ────────────────────────────────────────────────

    def correct_txt(self, system_prompt: str, txt_content: str) -> CorrectionResult:
        """TXT 전사문을 교정한다. 교정된 본문과 [확인 필요] 섹션을 반환한다."""
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": txt_content}],
        )
        raw = response.content[0].text
        return self._split_review(raw)

    # ── json 교정 ────────────────────────────────────────────────

    def correct_json(self, system_prompt: str, json_content: str) -> CorrectionResult:
        """JSON 전사문(segments 배열)의 text 값만 교정한다.

        반환값의 corrected는 유효한 JSON 문자열이어야 한다.
        Claude가 구조를 망가뜨린 경우 ValueError를 발생시킨다.
        """
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": json_content}],
        )
        raw = response.content[0].text
        corrected_raw, review_notes = self._split_review(raw)

        json_str = self._extract_json(corrected_raw)
        self._validate_json_structure(json_content, json_str)

        return CorrectionResult(json_str, review_notes)

    # ── 내부 유틸리티 ────────────────────────────────────────────

    @staticmethod
    def _split_review(raw: str) -> tuple[str, str | None]:
        """응답에서 [확인 필요] 섹션을 분리한다."""
        parts = _REVIEW_SEP_RE.split(raw, maxsplit=1)
        main = parts[0].strip()
        review = parts[1].strip() if len(parts) > 1 else None
        return main, review

    @staticmethod
    def _extract_json(text: str) -> str:
        """응답에서 JSON 문자열을 추출한다. 코드 블록 래핑을 제거한다."""
        match = _CODE_BLOCK_RE.search(text)
        if match:
            return match.group(1).strip()
        return text.strip()

    @staticmethod
    def _validate_json_structure(original_json: str, corrected_json: str) -> None:
        """교정 전후 segments 수와 id/start/end가 유지됐는지 검증한다."""
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

        for i, (o, c) in enumerate(zip(orig_segs, corr_segs)):
            for key in ("id", "start", "end"):
                if o.get(key) != c.get(key):
                    raise ValueError(
                        f"Segment {i}: '{key}' changed from {o.get(key)!r} to {c.get(key)!r}"
                    )
