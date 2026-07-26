"""품질 게이트 — 전사 결과의 건강도를 측정한다.

segments는 dict 리스트: seg["text"] (NOT seg.text)
"""
from __future__ import annotations

import json
import logging
import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping

from lecture_stt.shared import utils

logger = logging.getLogger(__name__)

QUALITY_SCORECARD_SCHEMA_VERSION = 1
QUALITY_SCORECARD_KIND = "lecture_stt_quality_scorecard"


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
    nonempty_segments: int        # 비어 있지 않은 세그먼트 수
    nonspace_chars: int           # 공백 제외 텍스트 문자 수
    last_segment_end_sec: float   # 마지막 세그먼트 종료 시각
    audio_duration_sec: float | None
    chars_per_audio_min: float | None
    coverage_ratio: float | None
    quality_score: int            # 0~100, 높을수록 건강함
    health: str                   # "good" | "warn" | "bad"
    summary: str                  # 사람이 읽을 수 있는 한 줄 요약

    def to_dict(self) -> dict:
        payload = {
            "total_segments": self.total_segments,
            "empty_segments": self.empty_segments,
            "dot_noise_segments": self.dot_noise_segments,
            "short_segments": self.short_segments,
            "rep_mass": round(self.rep_mass, 4),
            "avg_segment_length": round(self.avg_segment_length, 1),
            "empty_ratio": round(self.empty_ratio, 4),
            "dot_noise_ratio": round(self.dot_noise_ratio, 4),
            "short_segment_ratio": round(self.short_segment_ratio, 4),
            "nonempty_segments": self.nonempty_segments,
            "nonspace_chars": self.nonspace_chars,
            "last_segment_end_sec": round(self.last_segment_end_sec, 3),
            "quality_score": self.quality_score,
            "health": self.health,
            "summary": self.summary,
        }
        if self.audio_duration_sec is not None:
            payload["audio_duration_sec"] = round(self.audio_duration_sec, 3)
        if self.chars_per_audio_min is not None:
            payload["chars_per_audio_min"] = round(self.chars_per_audio_min, 2)
        if self.coverage_ratio is not None:
            payload["coverage_ratio"] = round(self.coverage_ratio, 4)
        return payload


def quality_scorecard_path(transcript_json_path: str | Path) -> Path:
    """Return the sidecar path for a transcript JSON quality scorecard."""
    path = Path(transcript_json_path)
    return path.with_name(f"{path.stem}.quality.json")


def _copy_present(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: mapping[key] for key in keys if key in mapping and mapping[key] is not None}


def build_quality_scorecard(
    metadata: Mapping[str, Any],
    *,
    transcript_json_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build a metadata-only quality scorecard without transcript body/segments."""
    quality = metadata.get("quality")
    if not isinstance(quality, Mapping):
        raise ValueError("metadata missing quality report")
    for key in ("quality_score", "health", "summary"):
        if key not in quality:
            raise ValueError(f"metadata quality report missing {key}")

    artifacts = _copy_present(
        metadata,
        ("canonical_audio_path", "transcript_txt_path", "transcript_json_path"),
    )
    if transcript_json_path is not None:
        artifacts["transcript_json_path"] = str(Path(transcript_json_path))
    artifacts = {key: str(value) for key, value in artifacts.items()}

    metrics = _copy_present(
        quality,
        (
            "total_segments",
            "empty_segments",
            "dot_noise_segments",
            "short_segments",
            "rep_mass",
            "avg_segment_length",
            "empty_ratio",
            "dot_noise_ratio",
            "short_segment_ratio",
            "nonempty_segments",
            "nonspace_chars",
            "last_segment_end_sec",
            "audio_duration_sec",
            "chars_per_audio_min",
            "coverage_ratio",
        ),
    )
    model = _copy_present(
        metadata,
        (
            "engine",
            "model_size",
            "device",
            "compute_type",
            "language",
            "task",
            "beam_size",
            "vad_filter",
            "word_timestamps",
            "condition_on_previous_text",
            "keep_model_loaded",
        ),
    )

    scorecard = {
        "schema_version": QUALITY_SCORECARD_SCHEMA_VERSION,
        "kind": QUALITY_SCORECARD_KIND,
        "canonical_base": str(metadata.get("canonical_base") or ""),
        "orig_name": str(metadata.get("orig_name") or ""),
        "health": str(quality["health"]),
        "quality_score": int(quality["quality_score"]),
        "summary": str(quality["summary"]),
        "metrics": metrics,
        "timings": dict(metadata.get("timings") or {}),
        "model": model,
        "artifacts": artifacts,
    }
    profile = metadata.get("profile")
    if isinstance(profile, Mapping):
        profile_snapshot = _copy_present(
            profile,
            ("key", "version", "config_sha256"),
        )
        if profile_snapshot:
            scorecard["profile"] = {
                key: str(value)
                for key, value in profile_snapshot.items()
            }
    return scorecard


def write_quality_scorecard(transcript_json_path: str | Path, metadata: Mapping[str, Any]) -> Path:
    """Write the sidecar quality scorecard and return its path."""
    path = quality_scorecard_path(transcript_json_path)
    utils.atomic_write(path, build_quality_scorecard(metadata, transcript_json_path=transcript_json_path))
    return path


def _contains_forbidden_scorecard_key(value: Any) -> bool:
    forbidden_keys = {"segments", "transcript_text", "transcript_body", "transcript"}
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key) in forbidden_keys:
                return True
            if _contains_forbidden_scorecard_key(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_scorecard_key(item) for item in value)
    return False


def validate_quality_scorecard(transcript_json_path: str | Path, metadata: Mapping[str, Any]) -> None:
    """Validate that the quality scorecard exists and matches transcript metadata."""
    path = quality_scorecard_path(transcript_json_path)
    if not path.exists():
        raise FileNotFoundError("Missing quality scorecard output file after write")

    try:
        with open(path, "r", encoding="utf-8") as handle:
            scorecard = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError("Quality scorecard is not valid JSON") from exc
    if not isinstance(scorecard, dict):
        raise ValueError("Quality scorecard payload is not a dict")
    if _contains_forbidden_scorecard_key(scorecard):
        raise ValueError("Quality scorecard must be metadata-only")

    expected = build_quality_scorecard(metadata, transcript_json_path=transcript_json_path)
    for key in (
        "schema_version",
        "kind",
        "canonical_base",
        "orig_name",
        "health",
        "quality_score",
        "summary",
    ):
        if scorecard.get(key) != expected[key]:
            raise ValueError(f"Quality scorecard {key} does not match transcript metadata")

    if "profile" in expected and scorecard.get("profile") != expected["profile"]:
        raise ValueError("Quality scorecard profile does not match transcript metadata")

    artifacts = scorecard.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("Quality scorecard missing artifacts object")
    for key, expected_value in expected["artifacts"].items():
        if str(artifacts.get(key)) != str(expected_value):
            raise ValueError(f"Quality scorecard artifact {key} does not match transcript metadata")


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
    audio_duration_sec: float | None = None,
) -> QualityReport:
    """segments(dict 리스트)와 text로 품질 보고서를 생성한다.

    audio_duration_sec가 있으면 오늘 260610LC처럼 "길이는 긴데 초반 일부만
    전사된" 결과를 잡기 위해 전사 밀도와 시간 커버리지도 함께 본다.
    """
    dot_re = re.compile(r"^[\s.,。、…·]+$")

    total = len(segments)
    empty = sum(1 for seg in segments if not str(seg.get("text", "")).strip())
    dot_noise = sum(1 for seg in segments if dot_re.match(str(seg.get("text", "")).strip() or " "))
    short_segments = sum(
        1
        for seg in segments
        if str(seg.get("text", "")).strip() and len(str(seg.get("text", "")).strip()) <= 2
    )

    seg_lengths = [len(str(seg.get("text", "")).strip()) for seg in segments if str(seg.get("text", "")).strip()]
    nonempty_segments = len(seg_lengths)
    avg_len = sum(seg_lengths) / nonempty_segments if seg_lengths else 0.0
    nonspace_chars = sum(1 for char in text if not char.isspace())

    segment_ends: list[float] = []
    for seg in segments:
        try:
            end = float(seg.get("end", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if end >= 0:
            segment_ends.append(end)
    last_segment_end_sec = max(segment_ends) if segment_ends else 0.0

    normalized_duration_sec: float | None = None
    if audio_duration_sec is not None:
        try:
            candidate_duration = float(audio_duration_sec)
        except (TypeError, ValueError):
            candidate_duration = 0.0
        if candidate_duration > 0:
            normalized_duration_sec = candidate_duration

    chars_per_audio_min: float | None = None
    coverage_ratio: float | None = None
    if normalized_duration_sec is not None:
        duration_min = normalized_duration_sec / 60.0
        if duration_min > 0:
            chars_per_audio_min = nonspace_chars / duration_min
        coverage_ratio = min(1.0, last_segment_end_sec / normalized_duration_sec)

    rep_mass = _compression_ratio(text)
    empty_ratio = (empty / total) if total else 0.0
    dot_noise_ratio = (dot_noise / total) if total else 0.0
    short_segment_ratio = (short_segments / nonempty_segments) if nonempty_segments else 0.0

    score = 100.0
    score -= min(55.0, rep_mass * 60.0)
    score -= min(18.0, empty_ratio * 90.0)
    score -= min(20.0, dot_noise_ratio * 80.0)
    score -= min(16.0, short_segment_ratio * 35.0)
    if avg_len < 4.0:
        score -= 12.0
    elif avg_len < 7.0:
        score -= 6.0

    issues: list[str] = []
    duration_critical = False
    if normalized_duration_sec is not None and normalized_duration_sec >= 180:
        if chars_per_audio_min is not None:
            if chars_per_audio_min < 15.0:
                score -= 45.0
                issues.append("오디오 길이 대비 전사량이 매우 적음")
                duration_critical = True
            elif chars_per_audio_min < 45.0:
                score -= 25.0
                issues.append("오디오 길이 대비 전사량이 적음")
        if coverage_ratio is not None:
            if coverage_ratio < 0.35:
                score -= 40.0
                issues.append("전사가 오디오 초반부에서 끊김")
                duration_critical = True
            elif coverage_ratio < 0.75:
                score -= 20.0
                issues.append("전사 시간 커버리지가 낮음")
        if normalized_duration_sec >= 600:
            min_expected_segments = max(10, int((normalized_duration_sec / 60.0) * 0.4))
            if nonempty_segments < min_expected_segments:
                score -= 20.0
                issues.append("오디오 길이 대비 세그먼트 수가 적음")

    quality_score = max(0, min(100, int(round(score))))
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

    if duration_critical or rep_mass >= bad_threshold or quality_score < 45 or len(issues) >= 3:
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
        nonempty_segments=nonempty_segments,
        nonspace_chars=nonspace_chars,
        last_segment_end_sec=last_segment_end_sec,
        audio_duration_sec=normalized_duration_sec,
        chars_per_audio_min=chars_per_audio_min,
        coverage_ratio=coverage_ratio,
        quality_score=quality_score,
        health=health,
        summary=summary,
    )

    logger.info(
        "quality_gate: health=%s score=%d rep_mass=%.4f segments=%d empty=%d dot=%d short=%d avg_len=%.1f chars_per_min=%s coverage=%s",
        health,
        quality_score,
        rep_mass,
        total,
        empty,
        dot_noise,
        short_segments,
        avg_len,
        f"{chars_per_audio_min:.2f}" if chars_per_audio_min is not None else "n/a",
        f"{coverage_ratio:.4f}" if coverage_ratio is not None else "n/a",
    )
    return report
