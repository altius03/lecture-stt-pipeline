"""교정 워커 — 02_transcripts → 03_correction 파이프라인.

scan_once()는 transcript_folder에서 미교정 파일 쌍을 찾아
Claude API로 교정 후 correction_folder에 저장한다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from lecture_stt.shared.paths import (
    env_file,
    repo_root,
    resolve_config_path,
    runtime_env,
)
from lecture_stt.shared.utils import is_temporary_file
from lecture_stt.correction.prompt_builder import PromptBuilder
from lecture_stt.correction.corrector import Corrector


logger = logging.getLogger(__name__)

# 파일명 앞 6자리가 날짜(YYMMDD), 7번째 이후가 과목 코드
_STEM_SUBJECT_RE = re.compile(r"^\d{6}([A-Za-z]+)")


@dataclass(frozen=True)
class CorrectionConfig:
    transcript_dir: Path
    correction_dir: Path
    prompt_dir: Path
    api_key: str
    model: str
    max_tokens: int
    stable_for_sec: int
    scan_interval_sec: int


@dataclass
class PendingPair:
    stem: str
    txt_path: Path
    json_path: Path
    subject_abbr: Optional[str]


def _parse_subject(stem: str) -> Optional[str]:
    """파일명 stem에서 과목 코드를 추출한다. 예: '260410LC' → 'LC'"""
    match = _STEM_SUBJECT_RE.match(stem)
    return match.group(1) if match else None


def _write_atomic(path: Path, content: str) -> None:
    """임시 파일로 쓴 뒤 rename해서 원자적으로 저장한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


class CorrectionWorker:
    def __init__(self, config: CorrectionConfig):
        self.config = config
        self._builder = PromptBuilder(config.prompt_dir)
        self._corrector = Corrector(
            api_key=config.api_key,
            model=config.model,
            max_tokens=config.max_tokens,
        )

    def scan_once(self) -> dict[str, int]:
        """미교정 파일 쌍을 찾아 교정하고 결과를 반환한다."""
        stats = {"corrected": 0, "skipped": 0, "errors": 0}

        pairs = self._collect_pending_pairs()
        if not pairs:
            logger.info("correction scan: nothing to process")
            return stats

        logger.info("correction scan: %d pair(s) pending", len(pairs))

        for pair in pairs:
            try:
                self._process_pair(pair)
                stats["corrected"] += 1
            except Exception as exc:
                stats["errors"] += 1
                logger.exception("correction failed for %s: %s", pair.stem, exc)

        logger.info("correction scan done: %s", stats)
        return stats

    # ── 파일 수집 ─────────────────────────────────────────────────

    def _collect_pending_pairs(self) -> list[PendingPair]:
        src = self.config.transcript_dir
        dst = self.config.correction_dir

        if not src.exists():
            logger.warning("transcript_dir does not exist: %s", src)
            return []

        # correction_folder에 이미 있는 stem 집합 (txt, json 둘 다 있어야 완료)
        done_stems = self._done_stems(dst)

        now = time.time()
        pairs: list[PendingPair] = []

        txt_paths: dict[str, Path] = {}
        json_paths: dict[str, Path] = {}

        for path in src.iterdir():
            if not path.is_file():
                continue
            if is_temporary_file(path):
                continue
            # stable_for_sec 이내에 수정된 파일은 아직 쓰는 중일 수 있다
            try:
                if now - path.stat().st_mtime < self.config.stable_for_sec:
                    continue
            except OSError:
                continue
            if path.suffix.lower() == ".txt":
                txt_paths[path.stem] = path
            elif path.suffix.lower() == ".json":
                json_paths[path.stem] = path

        for stem, txt_path in sorted(txt_paths.items()):
            if stem not in json_paths:
                continue
            if stem in done_stems:
                continue
            pairs.append(PendingPair(
                stem=stem,
                txt_path=txt_path,
                json_path=json_paths[stem],
                subject_abbr=_parse_subject(stem),
            ))

        return pairs

    @staticmethod
    def _done_stems(correction_dir: Path) -> set[str]:
        """교정이 완료된 stem 집합 — txt + json 둘 다 있어야 완료로 본다."""
        if not correction_dir.exists():
            return set()
        txt_stems: set[str] = set()
        json_stems: set[str] = set()
        for path in correction_dir.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() == ".txt":
                txt_stems.add(path.stem)
            elif path.suffix.lower() == ".json":
                json_stems.add(path.stem)
        return txt_stems & json_stems

    # ── 교정 처리 ─────────────────────────────────────────────────

    def _process_pair(self, pair: PendingPair) -> None:
        logger.info("correcting: %s (subject=%s)", pair.stem, pair.subject_abbr)

        system_prompt = self._builder.build(pair.subject_abbr)

        # ── TXT 교정 ──
        txt_content = pair.txt_path.read_text(encoding="utf-8")
        txt_result = self._corrector.correct_txt(system_prompt, txt_content)

        # ── JSON 교정 ──
        json_content = pair.json_path.read_text(encoding="utf-8")
        json_result = self._corrector.correct_json(system_prompt, json_content)

        # ── 저장 ──
        dst_txt = self.config.correction_dir / pair.txt_path.name
        dst_json = self.config.correction_dir / pair.json_path.name

        _write_atomic(dst_txt, txt_result.corrected)
        _write_atomic(dst_json, self._format_json(json_result.corrected))

        # [확인 필요] 항목이 있으면 사이드카 파일로 저장
        review_parts: list[str] = []
        if txt_result.review_notes:
            review_parts.append("[TXT 확인 필요]\n" + txt_result.review_notes)
        if json_result.review_notes:
            review_parts.append("[JSON 확인 필요]\n" + json_result.review_notes)
        if review_parts:
            review_path = self.config.correction_dir / f"{pair.stem}.review.txt"
            _write_atomic(review_path, "\n\n".join(review_parts))
            logger.warning("review notes written: %s", review_path)

        logger.info("corrected: %s → %s", pair.stem, self.config.correction_dir)

    @staticmethod
    def _format_json(json_str: str) -> str:
        """JSON을 정규화된 형태로 저장한다."""
        try:
            parsed = json.loads(json_str)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            return json_str


# ── 설정 로딩 ──────────────────────────────────────────────────────

def load_correction_config(config_path: str = "config/config.yaml") -> CorrectionConfig:
    root = repo_root()
    path = Path(config_path)
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        raise FileNotFoundError(f"Missing required config file: {path}")

    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}

    env = runtime_env(dotenv_path=env_file())

    paths_cfg = loaded.get("paths") or {}
    correction_cfg = loaded.get("correction") or {}

    transcript_dir = resolve_config_path(
        str(paths_cfg.get("transcript_folder", "${LECTURE_RECORDINGS_ROOT}/02_transcripts")),
        env=env,
    )
    correction_dir = resolve_config_path(
        str((loaded.get("downstream") or {}).get(
            "correction_folder", "${LECTURE_RECORDINGS_ROOT}/03_correction"
        )),
        env=env,
    )
    prompt_dir = resolve_config_path(
        str(correction_cfg.get("prompt_folder", "${LECTURE_RECORDINGS_ROOT}/05_prompt")),
        env=env,
    )

    api_key = correction_cfg.get("api_key") or env.get("ANTHROPIC_API_KEY") or ""
    if not api_key:
        raise ValueError(
            "ANTHROPIC_API_KEY not set. Add it to .env or set correction.api_key in config.yaml"
        )

    return CorrectionConfig(
        transcript_dir=transcript_dir,
        correction_dir=correction_dir,
        prompt_dir=prompt_dir,
        api_key=api_key,
        model=str(correction_cfg.get("model", "claude-haiku-4-5-20251001")),
        max_tokens=int(correction_cfg.get("max_tokens", 8192)),
        stable_for_sec=int(correction_cfg.get("stable_for_sec", 60)),
        scan_interval_sec=int(correction_cfg.get("scan_interval_sec", 60)),
    )
