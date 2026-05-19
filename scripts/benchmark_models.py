#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as importlib_metadata
import inspect
import json
import platform
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


DOC_CANDIDATE_MODELS = [
    "large-v3-turbo",
    "deepdml/faster-whisper-large-v3-turbo-ct2",
    "distil-large-v3",
    "ghost613/faster-whisper-large-v3-turbo-korean",
]


class CandidateApprovalRequired(RuntimeError):
    pass


class BenchmarkModel:
    __slots__ = ("model_id", "is_baseline")

    def __init__(self, model_id: str, *, is_baseline: bool):
        self.model_id = model_id
        self.is_baseline = is_baseline

    def to_dict(self) -> dict[str, object]:
        return {"model_id": self.model_id, "is_baseline": self.is_baseline}


def dedupe_preserving_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def build_benchmark_plan(
    *,
    config_model: str,
    requested_models: Sequence[str] | None,
    allow_candidates: bool,
) -> list[BenchmarkModel]:
    baseline = config_model.strip() or "large-v3"
    model_ids = dedupe_preserving_order(requested_models or [baseline])
    if baseline not in model_ids:
        model_ids.insert(0, baseline)

    candidate_ids = [model_id for model_id in model_ids if model_id != baseline]
    if candidate_ids and not allow_candidates:
        joined = ", ".join(candidate_ids)
        raise CandidateApprovalRequired(
            "Candidate model execution requires explicit approval flag: "
            f"--allow-candidate. Blocked candidate(s): {joined}"
        )

    return [BenchmarkModel(model_id, is_baseline=(model_id == baseline)) for model_id in model_ids]


def _tokens(text: str) -> list[str]:
    return [token for token in text.replace("\n", " ").split(" ") if token]


def _repeated_ngram_ratio(tokens: Sequence[str], n: int) -> float:
    if n <= 0 or len(tokens) < n:
        return 0.0
    grams = [tuple(tokens[index : index + n]) for index in range(len(tokens) - n + 1)]
    counts = Counter(grams)
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    return repeated / float(len(grams)) if grams else 0.0


def summarize_segments(segments: Sequence[dict[str, Any]], transcript_text: str) -> dict[str, object]:
    token_list = _tokens(transcript_text)
    empty_segment_count = 0
    timestamp_regression_count = 0
    zero_or_negative_duration_count = 0
    total_segment_chars = 0
    previous_end: float | None = None

    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            empty_segment_count += 1
        total_segment_chars += len(text)
        start = float(segment.get("start") or 0.0)
        end = float(segment.get("end") or 0.0)
        if previous_end is not None and start < previous_end:
            timestamp_regression_count += 1
        if end <= start:
            zero_or_negative_duration_count += 1
        previous_end = end

    segment_count = len(segments)
    return {
        "segment_count": segment_count,
        "empty_segment_count": empty_segment_count,
        "empty_segment_ratio": empty_segment_count / float(segment_count) if segment_count else 0.0,
        "timestamp_regression_count": timestamp_regression_count,
        "zero_or_negative_duration_count": zero_or_negative_duration_count,
        "transcript_chars": len(transcript_text),
        "token_count": len(token_list),
        "avg_segment_chars": total_segment_chars / float(segment_count) if segment_count else 0.0,
        "repeated_unigram_ratio": _repeated_ngram_ratio(token_list, 1),
        "repeated_bigram_ratio": _repeated_ngram_ratio(token_list, 2),
        "repeated_trigram_ratio": _repeated_ngram_ratio(token_list, 3),
    }


def load_config(path: Path) -> dict[str, Any]:
    import yaml

    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Config must be a YAML mapping: {path}")
    return payload


def parse_config_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
        raise ValueError(f"Invalid boolean value: {value!r}")
    return bool(value)


def transcribe_config(payload: dict[str, Any]) -> dict[str, Any]:
    transcribe = payload.get("transcribe") or {}
    if not isinstance(transcribe, dict):
        transcribe = {}
    return {
        "model_size": str(transcribe.get("model_size") or "large-v3"),
        "device": str(transcribe.get("device") or "cpu"),
        "compute_type": str(transcribe.get("compute_type") or "int8"),
        "language": str(transcribe.get("language") or "ko"),
        "task": str(transcribe.get("task") or "transcribe"),
        "beam_size": int(transcribe.get("beam_size") or 5),
        "vad_filter": parse_config_bool(transcribe.get("vad_filter"), default=True),
        "word_timestamps": parse_config_bool(transcribe.get("word_timestamps"), default=False),
        "condition_on_previous_text": parse_config_bool(
            transcribe.get("condition_on_previous_text"),
            default=False,
        ),
        "repetition_penalty": float(transcribe.get("repetition_penalty") or 1.15),
        "no_repeat_ngram_size": int(transcribe.get("no_repeat_ngram_size") or 4),
        "vad_threshold": float(transcribe.get("vad_threshold") or 0.55),
        "min_silence_duration_ms": int(transcribe.get("min_silence_duration_ms") or 1200),
        "initial_prompt": str(transcribe.get("initial_prompt") or ""),
    }


def build_vad_parameters(params: dict[str, Any], vad_options_cls: Any) -> dict[str, float | int]:
    """Return faster-whisper VAD kwargs across 1.1.x and 1.2.x APIs."""
    names = set(inspect.signature(vad_options_cls).parameters)
    vad_params: dict[str, float | int] = {
        "min_silence_duration_ms": int(params["min_silence_duration_ms"]),
    }
    threshold = float(params["vad_threshold"])
    if "onset" in names:
        vad_params["onset"] = threshold
    if "offset" in names:
        vad_params["offset"] = threshold - 0.15
    if "threshold" in names:
        vad_params["threshold"] = threshold
    return vad_params


def _segment_to_payload(segment: object, fallback_id: int) -> dict[str, object]:
    segment_id = getattr(segment, "id", fallback_id)
    return {
        "id": int(segment_id) if isinstance(segment_id, int) else fallback_id,
        "start": float(getattr(segment, "start", 0.0) or 0.0),
        "end": float(getattr(segment, "end", 0.0) or 0.0),
        "text": (getattr(segment, "text", "") or "").strip(),
    }


def run_one_benchmark(
    *,
    audio_path: Path,
    model: BenchmarkModel,
    params: dict[str, Any],
    allow_download: bool,
) -> dict[str, object]:
    from faster_whisper import WhisperModel
    from faster_whisper.transcribe import VadOptions

    started = time.perf_counter()
    whisper = WhisperModel(
        model.model_id,
        device=str(params["device"]),
        compute_type=str(params["compute_type"]),
        local_files_only=not allow_download,
    )
    load_sec = time.perf_counter() - started

    vad_params = None
    if params["vad_filter"]:
        vad_params = build_vad_parameters(params, VadOptions)

    transcribe_started = time.perf_counter()
    transcribe_kwargs: dict[str, Any] = {
        "language": str(params["language"]),
        "task": str(params["task"]),
        "beam_size": int(params["beam_size"]),
        "vad_filter": bool(params["vad_filter"]),
        "word_timestamps": bool(params["word_timestamps"]),
        "condition_on_previous_text": bool(params["condition_on_previous_text"]),
        "repetition_penalty": float(params["repetition_penalty"]),
        "no_repeat_ngram_size": int(params["no_repeat_ngram_size"]),
        "initial_prompt": str(params["initial_prompt"]) or None,
    }
    if vad_params:
        transcribe_kwargs["vad_parameters"] = vad_params
    segments_iter, info = whisper.transcribe(str(audio_path), **transcribe_kwargs)
    segments = [_segment_to_payload(segment, index) for index, segment in enumerate(segments_iter)]
    transcript_text = "\n".join(str(segment["text"]) for segment in segments if segment["text"]).strip()
    transcribe_sec = time.perf_counter() - transcribe_started

    duration = float(getattr(info, "duration", 0.0) or 0.0)
    metrics = summarize_segments(segments, transcript_text)
    metrics["audio_duration_sec"] = duration
    metrics["real_time_factor"] = transcribe_sec / duration if duration > 0 else None

    return {
        "audio_path": str(audio_path),
        "model": model.to_dict(),
        "load_sec": load_sec,
        "transcribe_sec": transcribe_sec,
        "metrics": metrics,
        "segments": segments,
        "text": transcript_text,
    }


def _package_version(name: str) -> str:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return "NOT_INSTALLED"


def runtime_metadata(config_path: Path) -> dict[str, object]:
    config_bytes = config_path.read_bytes() if config_path.exists() else b""
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "packages": {
            "faster-whisper": _package_version("faster-whisper"),
            "ctranslate2": _package_version("ctranslate2"),
            "huggingface-hub": _package_version("huggingface-hub"),
            "mlx-whisper": _package_version("mlx-whisper"),
        },
        "config_sha256": hashlib.sha256(config_bytes).hexdigest() if config_bytes else None,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark lecture_stt faster-whisper model candidates")
    parser.add_argument("--config", default="config/config.yaml", help="Path to lecture_stt config.yaml")
    parser.add_argument("--audio", action="append", default=[], help="Audio file to benchmark; repeatable")
    parser.add_argument("--model", action="append", default=[], help="Model alias/repo/path to run; repeatable")
    parser.add_argument(
        "--include-doc-candidates",
        action="store_true",
        help="Append candidate models documented in docs/MODELS.md; requires --allow-candidate",
    )
    parser.add_argument("--allow-candidate", action="store_true", help="Permit non-baseline candidate model execution")
    parser.add_argument("--allow-download", action="store_true", help="Permit Hugging Face download/cache misses")
    parser.add_argument("--print-plan", action="store_true", help="Print benchmark plan without loading models")
    parser.add_argument("--output", help="Write JSON result to this path")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = Path(args.config)
    config = load_config(config_path)
    params = transcribe_config(config)

    requested_models = list(args.model)
    if args.include_doc_candidates:
        requested_models.extend(DOC_CANDIDATE_MODELS)
    requested = requested_models or None

    try:
        plan = build_benchmark_plan(
            config_model=str(params["model_size"]),
            requested_models=requested,
            allow_candidates=bool(args.allow_candidate),
        )
    except CandidateApprovalRequired as exc:
        print(str(exc), file=sys.stderr)
        return 2

    plan_payload = [item.to_dict() for item in plan]
    if args.print_plan:
        print(json.dumps({"plan": plan_payload, "allow_download": bool(args.allow_download)}, ensure_ascii=False, indent=2))
        return 0

    if not args.output:
        print(
            "Actual benchmark runs require --output so transcript payloads are not written to stdout.",
            file=sys.stderr,
        )
        return 2

    audio_paths = [Path(value).expanduser() for value in args.audio]
    if not audio_paths:
        print("At least one --audio path is required unless --print-plan is used.", file=sys.stderr)
        return 2
    missing = [str(path) for path in audio_paths if not path.exists()]
    if missing:
        print(f"Missing audio file(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    results: list[dict[str, object]] = []
    for model in plan:
        for audio_path in audio_paths:
            results.append(
                run_one_benchmark(
                    audio_path=audio_path,
                    model=model,
                    params=params,
                    allow_download=bool(args.allow_download),
                )
            )

    payload = {
        "config": str(config_path),
        "runtime": runtime_metadata(config_path),
        "params": params,
        "allow_download": bool(args.allow_download),
        "plan": plan_payload,
        "results": results,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    output_path = Path(args.output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered + "\n", encoding="utf-8")
    print(f"Wrote benchmark result: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
