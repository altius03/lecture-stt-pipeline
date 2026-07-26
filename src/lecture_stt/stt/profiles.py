"""Versioned transcription profiles.

Profiles are intentionally small and deterministic.  They select overrides for
the existing faster-whisper configuration, post-processing corrections, and
quality thresholds without changing the legacy configuration path.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping


DEFAULT_WARN_THRESHOLD = 0.55
DEFAULT_BAD_THRESHOLD = 0.70

_PROFILE_ROOT_KEYS = {"active", "definitions"}
_PROFILE_DEFINITION_KEYS = {"version", "transcribe", "postprocess", "quality"}
_TRANSCRIBE_KEYS = {
    "model_size",
    "device",
    "compute_type",
    "language",
    "task",
    "beam_size",
    "vad_filter",
    "word_timestamps",
    "condition_on_previous_text",
    "repetition_penalty",
    "no_repeat_ngram_size",
    "vad_threshold",
    "min_silence_duration_ms",
    "initial_prompt",
    "keep_model_loaded",
}
_TRANSCRIBE_STRING_KEYS = {
    "model_size",
    "device",
    "compute_type",
    "language",
    "task",
    "initial_prompt",
}
_TRANSCRIBE_BOOL_KEYS = {
    "vad_filter",
    "word_timestamps",
    "condition_on_previous_text",
    "keep_model_loaded",
}
_TRANSCRIBE_POSITIVE_INT_KEYS = {"beam_size", "no_repeat_ngram_size", "min_silence_duration_ms"}
_TRANSCRIBE_POSITIVE_NUMBER_KEYS = {"repetition_penalty", "vad_threshold"}


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Config error: {label} must be a mapping")
    return value


def _reject_unknown_keys(mapping: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(str(key) for key in mapping if key not in allowed)
    if unknown:
        raise ValueError(f"Config error: {label} contains unknown keys: {', '.join(unknown)}")


def _validate_transcribe_overrides(value: Any, label: str) -> dict[str, Any]:
    mapping = _require_mapping(value, label)
    _reject_unknown_keys(mapping, _TRANSCRIBE_KEYS, label)
    normalized = dict(mapping)

    for key in _TRANSCRIBE_STRING_KEYS:
        if key not in normalized:
            continue
        candidate = normalized[key]
        if not isinstance(candidate, str):
            raise ValueError(f"Config error: {label}.{key} must be a string")
        if key != "initial_prompt" and not candidate.strip():
            raise ValueError(f"Config error: {label}.{key} must be non-empty")

    for key in _TRANSCRIBE_BOOL_KEYS:
        if key in normalized and not isinstance(normalized[key], bool):
            raise ValueError(f"Config error: {label}.{key} must be a boolean")

    for key in _TRANSCRIBE_POSITIVE_INT_KEYS:
        if key not in normalized:
            continue
        candidate = normalized[key]
        if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate <= 0:
            raise ValueError(f"Config error: {label}.{key} must be a positive integer")

    for key in _TRANSCRIBE_POSITIVE_NUMBER_KEYS:
        if key not in normalized:
            continue
        candidate = normalized[key]
        if isinstance(candidate, bool) or not isinstance(candidate, (int, float)) or candidate <= 0:
            raise ValueError(f"Config error: {label}.{key} must be a positive number")

    return normalized


def _validate_corrections(value: Any, label: str) -> dict[str, str]:
    mapping = _require_mapping(value, label)
    normalized: dict[str, str] = {}
    for wrong, right in mapping.items():
        if not isinstance(wrong, str) or not wrong:
            raise ValueError(f"Config error: {label} keys must be non-empty strings")
        if not isinstance(right, str) or not right:
            raise ValueError(f"Config error: {label}.{wrong} must be a non-empty string")
        normalized[wrong] = right
    return normalized


def _validate_threshold(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Config error: {label} must be a number")
    normalized = float(value)
    if not 0.0 <= normalized <= 1.0:
        raise ValueError(f"Config error: {label} must be between 0 and 1")
    return normalized


@dataclass(frozen=True)
class ResolvedProfile:
    key: str
    version: str
    config_sha256: str
    transcribe_overrides: dict[str, Any]
    corrections: dict[str, str] | None
    warn_threshold: float
    bad_threshold: float
    is_legacy: bool = False

    def snapshot(self) -> dict[str, str]:
        return {
            "key": self.key,
            "version": self.version,
            "config_sha256": self.config_sha256,
        }


def _normalize_definition(key: str, value: Any) -> dict[str, Any]:
    label = f"profiles.definitions.{key}"
    definition = _require_mapping(value, label)
    _reject_unknown_keys(definition, _PROFILE_DEFINITION_KEYS, label)

    if "version" not in definition:
        raise ValueError(f"Config error: {label}.version is required")
    raw_version = definition["version"]
    if isinstance(raw_version, bool) or not isinstance(raw_version, (str, int)):
        raise ValueError(f"Config error: {label}.version must be a string or integer")
    version = str(raw_version).strip()
    if not version:
        raise ValueError(f"Config error: {label}.version must be non-empty")

    transcribe: dict[str, Any] = {}
    if "transcribe" in definition:
        transcribe = _validate_transcribe_overrides(
            definition["transcribe"],
            f"{label}.transcribe",
        )

    corrections: dict[str, str] | None = None
    if "postprocess" in definition:
        postprocess = _require_mapping(definition["postprocess"], f"{label}.postprocess")
        _reject_unknown_keys(postprocess, {"corrections"}, f"{label}.postprocess")
        if "corrections" in postprocess:
            corrections = _validate_corrections(
                postprocess["corrections"],
                f"{label}.postprocess.corrections",
            )

    warn_threshold = DEFAULT_WARN_THRESHOLD
    bad_threshold = DEFAULT_BAD_THRESHOLD
    if "quality" in definition:
        quality = _require_mapping(definition["quality"], f"{label}.quality")
        _reject_unknown_keys(
            quality,
            {"warn_threshold", "bad_threshold"},
            f"{label}.quality",
        )
        if "warn_threshold" in quality:
            warn_threshold = _validate_threshold(
                quality["warn_threshold"],
                f"{label}.quality.warn_threshold",
            )
        if "bad_threshold" in quality:
            bad_threshold = _validate_threshold(
                quality["bad_threshold"],
                f"{label}.quality.bad_threshold",
            )
    if warn_threshold >= bad_threshold:
        raise ValueError(
            f"Config error: {label}.quality.warn_threshold must be less than bad_threshold"
        )

    normalized: dict[str, Any] = {
        "version": version,
        "transcribe": transcribe,
        "quality": {
            "warn_threshold": warn_threshold,
            "bad_threshold": bad_threshold,
        },
    }
    if corrections is not None:
        normalized["postprocess"] = {"corrections": corrections}
    return normalized


def resolve_profile(config: Mapping[str, Any]) -> ResolvedProfile:
    """Resolve and strictly validate the configured active profile.

    Missing or empty ``profiles`` preserves the historical transcription,
    post-processing, and quality behavior while adding a traceable legacy
    snapshot to new job metadata.
    """
    raw_profiles = config.get("profiles")
    if raw_profiles is None or raw_profiles == {}:
        legacy_transcribe = config.get("transcribe")
        fingerprint_input = {
            "key": "legacy",
            "version": "unversioned",
            "transcribe": dict(legacy_transcribe) if isinstance(legacy_transcribe, Mapping) else {},
            "quality": {
                "warn_threshold": DEFAULT_WARN_THRESHOLD,
                "bad_threshold": DEFAULT_BAD_THRESHOLD,
            },
        }
        return ResolvedProfile(
            key="legacy",
            version="unversioned",
            config_sha256=_canonical_sha256(fingerprint_input),
            transcribe_overrides={},
            corrections=None,
            warn_threshold=DEFAULT_WARN_THRESHOLD,
            bad_threshold=DEFAULT_BAD_THRESHOLD,
            is_legacy=True,
        )

    profiles = _require_mapping(raw_profiles, "profiles")
    _reject_unknown_keys(profiles, _PROFILE_ROOT_KEYS, "profiles")
    active = profiles.get("active")
    if not isinstance(active, str) or not active.strip():
        raise ValueError("Config error: profiles.active must be a non-empty string")
    active = active.strip()

    definitions = _require_mapping(profiles.get("definitions"), "profiles.definitions")
    if not definitions:
        raise ValueError("Config error: profiles.definitions must not be empty")

    normalized_definitions: dict[str, dict[str, Any]] = {}
    for raw_key, definition in definitions.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError("Config error: profile keys must be non-empty strings")
        key = raw_key.strip()
        if key in normalized_definitions:
            raise ValueError(f"Config error: duplicate normalized profile key: {key}")
        normalized_definitions[key] = _normalize_definition(key, definition)

    if active not in normalized_definitions:
        raise ValueError(f"Config error: profiles.active '{active}' is not defined")

    selected = normalized_definitions[active]
    corrections: dict[str, str] | None = None
    postprocess = selected.get("postprocess")
    if isinstance(postprocess, Mapping):
        corrections = dict(postprocess.get("corrections") or {})

    return ResolvedProfile(
        key=active,
        version=str(selected["version"]),
        config_sha256=_canonical_sha256({"key": active, "definition": selected}),
        transcribe_overrides=dict(selected["transcribe"]),
        corrections=corrections,
        warn_threshold=float(selected["quality"]["warn_threshold"]),
        bad_threshold=float(selected["quality"]["bad_threshold"]),
    )


def merge_transcribe_config(
    base: Mapping[str, Any],
    profile: ResolvedProfile,
) -> dict[str, Any]:
    merged = dict(base)
    merged.update(profile.transcribe_overrides)
    return merged


def legacy_unknown_snapshot() -> dict[str, str]:
    """Describe pre-profile output without falsely attributing the active profile."""
    return {
        "key": "legacy-unknown",
        "version": "unknown",
        "config_sha256": "unknown",
    }
