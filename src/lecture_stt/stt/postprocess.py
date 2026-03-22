"""후처리 모듈 — 전사 결과의 반복/노이즈를 정리한다.

segments는 dict 리스트: [{"id": int, "start": float, "end": float, "text": str}, ...]
이 인터페이스는 절대 변경하지 않는다.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


# ── 점 노이즈 제거 ──────────────────────────────────────────────

# 마침표/쉼표/공백만으로 구성된 세그먼트를 빈 문자열로 치환한다.
_DOT_NOISE_RE = re.compile(r"^[\s.,。、…·]+$")


def remove_dot_noise(segments: List[dict]) -> List[dict]:
    """점/쉼표만 있는 세그먼트의 text를 비운다. 세그먼트 자체는 삭제하지 않는다."""
    cleaned = 0
    for seg in segments:
        if _DOT_NOISE_RE.match(seg["text"]):
            seg["text"] = ""
            cleaned += 1
    if cleaned:
        logger.info("dot_noise: cleared %d segment(s)", cleaned)
    return segments


# ── 반복 제거 (슬라이딩 윈도우 방식) ────────────────────────────

def remove_repeated_phrases(text: str, min_len: int = 6, min_repeat: int = 3) -> str:
    """슬라이딩 윈도우로 연속 반복 구간을 탐지·축소한다.

    정규식 역참조 대신 문자열 비교를 사용해 카타스트로픽 백트래킹을 방지한다.
    - min_len: 반복으로 간주할 최소 구절 길이 (글자 수)
    - min_repeat: 최소 연속 반복 횟수
    """
    if not text or len(text) < min_len * min_repeat:
        return text

    result = text
    # 긴 패턴부터 탐색해야 짧은 패턴이 긴 반복을 쪼개지 않는다.
    for phrase_len in range(min(60, len(text) // min_repeat), min_len - 1, -1):
        i = 0
        new_parts = []
        while i < len(result):
            phrase = result[i:i + phrase_len]
            if len(phrase) < phrase_len:
                new_parts.append(result[i:])
                break

            # 이 phrase가 몇 번 연속 반복되는지 센다
            count = 1
            j = i + phrase_len
            while j + phrase_len <= len(result) and result[j:j + phrase_len] == phrase:
                count += 1
                j += phrase_len

            if count >= min_repeat:
                # min_repeat회 이상 반복이면 1회만 남긴다
                new_parts.append(phrase)
                i = j  # 반복 구간 전체를 건너뛴다
                logger.debug("repeat removed: '%s' x%d", phrase[:30], count)
            else:
                new_parts.append(result[i])
                i += 1

        result = "".join(new_parts)

    return result


# ── 용어 교정 (오인식만, 한→영 변환은 하지 않음) ──────────────

# key: Whisper가 잘못 인식하는 패턴, value: 올바른 표기
# "깃허브"→"GitHub" 같은 한→영 변환은 포함하지 않는다. 그건 교정이 아니라 변형이다.
DEFAULT_CORRECTIONS: Dict[str, str] = {
    # 예시 (실제 강의에서 수집된 오류 패턴을 추가할 것)
    "모금투": "우분투",
    "유분투": "우분투",
    "린눅스": "리눅스",
    "링눅스": "리눅스",
    "기트": "깃",
    "비쥬얼 스튜디오": "비주얼 스튜디오",
}


def correct_terms(text: str, corrections: Dict[str, str] | None = None) -> str:
    """오인식 용어를 교정한다. 한→영 변환은 수행하지 않는다."""
    if corrections is None:
        corrections = DEFAULT_CORRECTIONS
    for wrong, right in corrections.items():
        if wrong in text:
            text = text.replace(wrong, right)
    return text


# ── 통합 후처리 진입점 ──────────────────────────────────────────

def postprocess(
    segments: List[dict],
    text: str,
    corrections: Dict[str, str] | None = None,
) -> Tuple[List[dict], str]:
    """segments(dict 리스트)와 text를 받아 정제된 결과를 반환한다.

    반환 시그니처는 STTWorker.transcribe()와 동일: (List[dict], str)
    """
    # Step 1: 세그먼트 레벨 점 노이즈 제거
    segments = remove_dot_noise(segments)

    # Step 2: 전체 텍스트에서 반복 제거
    text = remove_repeated_phrases(text)

    # Step 3: 용어 교정
    text = correct_terms(text, corrections)

    # Step 4: 세그먼트별 텍스트에도 용어 교정 적용
    for seg in segments:
        if seg["text"]:
            seg["text"] = correct_terms(seg["text"], corrections)

    # Step 5: 텍스트를 세그먼트 기반으로 재조립 (후처리 후 동기화)
    non_empty = [seg["text"] for seg in segments if seg["text"]]
    if non_empty:
        text = "\n".join(non_empty).strip()

    return segments, text
