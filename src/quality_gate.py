"""품질 게이트 — 전사 결과의 건강도를 측정한다.

segments는 dict 리스트: seg["text"] (NOT seg.text)
"""
from __future__ import annotations

import logging
import re
import zlib
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)


@dataclass
class QualityReport:
    """품질 측정 결과를 담는다."""
    total_segments: int
    empty_segments: int
    dot_noise_segments: int       # 점 노이즈 세그먼트 수 (후처리 전 기준)
    rep_mass: float               # 0.0~1.0, 높을수록 반복 많음
    avg_segment_length: float     # 평균 세그먼트 글자 수
    health: str                   # "good" | "warn" | "bad"
    summary: str                  # 사람이 읽을 수 있는 한 줄 요약

    def to_dict(self) -> dict:
        return {
            "total_segments": self.total_segments,
            "empty_segments": self.empty_segments,
            "dot_noise_segments": self.dot_noise_segments,
            "rep_mass": round(self.rep_mass, 4),
            "avg_segment_length": round(self.avg_segment_length, 1),
            "health": self.health,
            "summary": self.summary,
        }


def _compression_ratio(text: str) -> float:
    """zlib 압축률로 반복도를 측정한다.

    정상 한국어 텍스트: 0.3~0.5
    심한 반복: 0.7 이상 (압축이 잘 됨 = 반복이 많음)
    """
    if not text or len(text) < 20:
        return 0.0
    encoded = text.encode("utf-8")
    compressed = zlib.compress(encoded, level=9)
    return 1.0 - (len(compressed) / len(encoded))


def evaluate(
    segments: List[dict],
    text: str,
    warn_threshold: float = 0.55,
    bad_threshold: float = 0.70,
) -> QualityReport:
    """segments(dict 리스트)와 text로 품질 보고서를 생성한다."""
    dot_re = re.compile(r"^[\s.,。、…·]+$")

    total = len(segments)
    empty = sum(1 for seg in segments if not seg["text"].strip())
    dot_noise = sum(1 for seg in segments if dot_re.match(seg["text"].strip() or " "))

    seg_lengths = [len(seg["text"].strip()) for seg in segments if seg["text"].strip()]
    avg_len = sum(seg_lengths) / len(seg_lengths) if seg_lengths else 0.0

    rep_mass = _compression_ratio(text)

    if rep_mass >= bad_threshold:
        health = "bad"
        summary = f"반복 심각 (rep_mass={rep_mass:.2f}). 전사 실패 가능성 높음."
    elif rep_mass >= warn_threshold:
        health = "warn"
        summary = f"반복 주의 (rep_mass={rep_mass:.2f}). 결과 검토 권장."
    else:
        health = "good"
        summary = f"정상 (rep_mass={rep_mass:.2f})."

    report = QualityReport(
        total_segments=total,
        empty_segments=empty,
        dot_noise_segments=dot_noise,
        rep_mass=rep_mass,
        avg_segment_length=avg_len,
        health=health,
        summary=summary,
    )

    logger.info(
        "quality_gate: health=%s rep_mass=%.4f segments=%d empty=%d dot=%d avg_len=%.1f",
        health, rep_mass, total, empty, dot_noise, avg_len,
    )
    return report
