#!/usr/bin/env python3
"""A/B 테스트 — 동일 오디오에 대해 기존 설정 vs v2 설정 품질을 비교한다.

사용법:
    python scripts/ab_test.py --audio /path/to/lecture.m4a
    python scripts/ab_test.py --audio /path/to/lecture.m4a --baseline-config config/config_baseline.yaml

기본 동작:
    A (baseline): repetition_penalty=1.0, no_repeat_ngram_size=0, vad_filter=false
    B (v2):       config.yaml의 현재 값
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# 프로젝트 src를 import 경로에 추가
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lecture_stt.stt.postprocess import postprocess
from lecture_stt.stt.quality_gate import evaluate as quality_evaluate
from lecture_stt.stt.transcribe import EngineParams, STTWorker


def build_params(
    repetition_penalty: float = 1.0,
    no_repeat_ngram_size: int = 0,
    vad_filter: bool = False,
    vad_threshold: float = 0.5,
    min_silence_duration_ms: int = 2000,
    initial_prompt: str = "",
) -> EngineParams:
    return EngineParams(
        model_size="large-v3",
        device="cpu",
        compute_type="int8",
        language="ko",
        task="transcribe",
        beam_size=5,
        vad_filter=vad_filter,
        word_timestamps=False,
        condition_on_previous_text=True,
        repetition_penalty=repetition_penalty,
        no_repeat_ngram_size=no_repeat_ngram_size,
        vad_threshold=vad_threshold,
        min_silence_duration_ms=min_silence_duration_ms,
        initial_prompt=initial_prompt,
    )


def run_test(label: str, worker: STTWorker, wav_path: Path) -> dict:
    print(f"\n{'='*60}")
    print(f"  [{label}] 전사 시작...")
    print(f"{'='*60}")

    t0 = time.perf_counter()
    segments, text = worker.transcribe(wav_path)
    transcribe_sec = time.perf_counter() - t0

    # 후처리 (v2만 적용해도 되지만, 비교를 위해 둘 다 적용)
    segments_pp, text_pp = postprocess(segments, text)
    report = quality_evaluate(segments_pp, text_pp)

    result = {
        "label": label,
        "transcribe_sec": round(transcribe_sec, 1),
        "raw_text_len": len(text),
        "pp_text_len": len(text_pp),
        "segments": len(segments),
        "quality": report.to_dict(),
    }

    print(f"  소요시간: {transcribe_sec:.1f}s")
    print(f"  원본 텍스트 길이: {len(text)} → 후처리 후: {len(text_pp)}")
    print(f"  품질: {report.health} (rep_mass={report.rep_mass:.4f})")
    print(f"  요약: {report.summary}")

    return result


def main():
    parser = argparse.ArgumentParser(description="A/B 테스트")
    parser.add_argument("--audio", required=True, help="테스트할 오디오 파일 경로")
    parser.add_argument("--ffmpeg", default="/opt/homebrew/bin/ffmpeg")
    parser.add_argument("--tmp", default=str(Path(__file__).resolve().parents[1] / "tmp"))
    args = parser.parse_args()

    audio_path = Path(args.audio)
    if not audio_path.exists():
        print(f"파일 없음: {audio_path}")
        sys.exit(1)

    # A: 기존 설정 (baseline)
    params_a = build_params(
        repetition_penalty=1.0,
        no_repeat_ngram_size=0,
        vad_filter=False,
    )
    worker_a = STTWorker(params_a, args.ffmpeg, args.tmp)

    # B: v2 설정
    params_b = build_params(
        repetition_penalty=1.15,
        no_repeat_ngram_size=4,
        vad_filter=True,
        vad_threshold=0.55,
        min_silence_duration_ms=1200,
        initial_prompt="이 강의는 컴퓨터공학 전공 수업입니다.",
    )
    worker_b = STTWorker(params_b, args.ffmpeg, args.tmp)

    # 전처리는 1회만
    canonical_base = "ab_test"
    wav_path = worker_a.preprocess(audio_path, canonical_base)

    result_a = run_test("A (baseline)", worker_a, wav_path)
    result_b = run_test("B (v2)", worker_b, wav_path)

    print(f"\n{'='*60}")
    print(f"  비교 결과")
    print(f"{'='*60}")
    print(f"  {'항목':<20} {'A (baseline)':<20} {'B (v2)':<20}")
    print(f"  {'-'*60}")
    print(f"  {'소요시간':<20} {result_a['transcribe_sec']:<20} {result_b['transcribe_sec']:<20}")
    print(f"  {'텍스트 길이(원본)':<20} {result_a['raw_text_len']:<20} {result_b['raw_text_len']:<20}")
    print(f"  {'텍스트 길이(후처리)':<20} {result_a['pp_text_len']:<20} {result_b['pp_text_len']:<20}")
    print(f"  {'세그먼트 수':<20} {result_a['segments']:<20} {result_b['segments']:<20}")
    print(f"  {'rep_mass':<20} {result_a['quality']['rep_mass']:<20} {result_b['quality']['rep_mass']:<20}")
    print(f"  {'건강도':<20} {result_a['quality']['health']:<20} {result_b['quality']['health']:<20}")

    # 결과 JSON 저장
    output = {"A_baseline": result_a, "B_v2": result_b}
    out_path = Path(args.tmp) / "ab_test_result.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  결과 저장: {out_path}")

    # 정리
    worker_a.cleanup_tmp(wav_path)


if __name__ == "__main__":
    main()
