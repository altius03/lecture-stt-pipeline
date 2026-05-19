"""교정 워커 — 02_transcripts → 03_correction 파이프라인.

자동 LLM 교정 provider는 현재 비활성화되어 있다. scan_once()는
transcript_folder에서 미교정 파일 쌍을 찾아 manual correction 대기 상태로
보고하되, 원본 transcript나 correction_folder를 변경하지 않는다.
"""
from __future__ import annotations

import logging
import re
import time
import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lecture_stt.shared.paths import (
    env_file,
    repo_root,
    resolve_config_path,
    runtime_env,
)
from lecture_stt.shared.utils import is_temporary_file


logger = logging.getLogger(__name__)

# 파일명 앞 6자리가 날짜(YYMMDD), 7번째 이후가 과목 코드
_STEM_SUBJECT_RE = re.compile(r"^\d{6}([A-Za-z]+)")


@dataclass(frozen=True)
class CorrectionConfig:
    transcript_dir: Path
    correction_dir: Path
    prompt_dir: Path
    stable_for_sec: int
    scan_interval_sec: int
    mode: str = "manual"


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


class CorrectionWorker:
    def __init__(self, config: CorrectionConfig):
        self.config = config

    def scan_once(self) -> dict[str, int]:
        """미교정 파일 쌍을 찾아 교정하고 결과를 반환한다."""
        stats = {"corrected": 0, "skipped": 0, "errors": 0}

        pairs = self._collect_pending_pairs()
        if not pairs:
            logger.info("correction scan: nothing to process")
            return stats

        logger.info("correction scan: %d pair(s) pending in %s mode", len(pairs), self.config.mode)

        for pair in pairs:
            logger.info(
                "manual correction pending: %s (subject=%s, txt=%s, json=%s)",
                pair.stem,
                pair.subject_abbr,
                pair.txt_path,
                pair.json_path,
            )
            stats["skipped"] += 1

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

    return CorrectionConfig(
        transcript_dir=transcript_dir,
        correction_dir=correction_dir,
        prompt_dir=prompt_dir,
        stable_for_sec=int(correction_cfg.get("stable_for_sec", 60)),
        scan_interval_sec=int(correction_cfg.get("scan_interval_sec", 60)),
        mode=str(correction_cfg.get("mode", "manual")),
    )
