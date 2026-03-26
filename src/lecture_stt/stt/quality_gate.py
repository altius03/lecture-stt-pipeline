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
    short_segments: int           # 지나치게 짧은 세그먼트 수
    rep_mass: float               # 0.0~1.0, 높을수록 반복 많음
    avg_segment_length: float     # 평균 세그먼트 글자 수
    empty_ratio: float
    dot_noise_ratio: float
    short_segment_ratio: float
    quality_score: int            # 0~100, 높을수록 건강함
    health: str                   # "good" | "warn" | "bad"
    summary: str                  # 사람이 읽을 수 있는 한 줄 요약

    def to_dict(self) -> dict:
        return {
            "total_segments": self.total_segments,
            "empty_segments": self.empty_segments,
            "dot_noise_segments": self.dot_noise_segments,
            "short_segments": self.short_segments,
            "rep_mass": round(self.rep_mass, 4),
            "avg_segment_length": round(self.avg_segment_length, 1),
            "empty_ratio": round(self.empty_ratio, 4),
            "dot_noise_ratio": round(self.dot_noise_ratio, 4),
            "short_segment_ratio": round(self.short_segment_ratio, 4),
            "quality_score": self.quality_score,
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
    short_segments = sum(
        1
        for seg in segments
        if seg["text"].strip() and len(seg["text"].strip()) <= 2
    )

    seg_lengths = [len(seg["text"].strip()) for seg in segments if seg["text"].strip()]
    avg_len = sum(seg_lengths) / len(seg_lengths) if seg_lengths else 0.0

    rep_mass = _compression_ratio(text)
    empty_ratio = (empty / total) if total else 0.0
    dot_noise_ratio = (dot_noise / total) if total else 0.0
    short_segment_ratio = (short_segments / len(seg_lengths)) if seg_lengths else 0.0

    score = 100.0
    score -= min(55.0, rep_mass * 60.0)
    score -= min(18.0, empty_ratio * 90.0)
    score -= min(20.0, dot_noise_ratio * 80.0)
    score -= min(16.0, short_segment_ratio * 35.0)
    if avg_len < 4.0:
        score -= 12.0
    elif avg_len < 7.0:
        score -= 6.0

    quality_score = max(0, min(100, int(round(score))))
    issues: list[str] = []
    if rep_mass >= bad_threshold:
        issues.append("반복 비율이 매우 높음")
    elif rep_mass >= warn_threshold:
        issues.append("반복 비율이 높음")
    if dot_noise_ratio >= 0.25:
        issues.append("기호성 노이즈가 많음")
    elif dot_noise_ratio >= 0.12:
        issues.append("기호 노이즈가 보임")
    if empty_ratio >= 0.25:
        issues.append("빈 구간이 많음")
    elif empty_ratio >= 0.12:
        issues.append("빈 구간이 다소 있음")
    if short_segment_ratio >= 0.45:
        issues.append("짧은 발화 비율이 높음")
    elif short_segment_ratio >= 0.30:
        issues.append("짧은 발화가 다소 많음")
    if avg_len and avg_len < 4.0:
        issues.append("문장 길이가 지나치게 짧음")

    if rep_mass >= bad_threshold or quality_score < 45 or len(issues) >= 3:
        health = "bad"
        summary = (
            f"품질 위험 (점수 {quality_score}/100). "
            + (", ".join(issues[:3]) if issues else "전사 실패 가능성이 높습니다.")
        )
    elif issues or quality_score < 75:
        health = "warn"
        summary = (
            f"품질 주의 (점수 {quality_score}/100). "
            + (", ".join(issues[:3]) if issues else "결과 검토가 필요합니다.")
        )
    else:
        health = "good"
        summary = f"정상 (점수 {quality_score}/100). 반복/노이즈 징후가 크지 않습니다."

    report = QualityReport(
        total_segments=total,
        empty_segments=empty,
        dot_noise_segments=dot_noise,
        short_segments=short_segments,
        rep_mass=rep_mass,
        avg_segment_length=avg_len,
        empty_ratio=empty_ratio,
        dot_noise_ratio=dot_noise_ratio,
        short_segment_ratio=short_segment_ratio,
        quality_score=quality_score,
        health=health,
        summary=summary,
    )

    logger.info(
        "quality_gate: health=%s score=%d rep_mass=%.4f segments=%d empty=%d dot=%d short=%d avg_len=%.1f",
        health, quality_score, rep_mass, total, empty, dot_noise, short_segments, avg_len,
    )
    return report
