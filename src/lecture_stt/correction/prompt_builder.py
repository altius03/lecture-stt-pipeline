"""교정 시스템 프롬프트 조합 모듈.

00_base_prompt.txt + 01_common_glossary.txt + glossary_{subject}.txt 를
순서대로 합쳐 Claude API system prompt를 만든다.
"""
from __future__ import annotations

from pathlib import Path


# 과목 코드 → glossary 파일명 매핑
_GLOSSARY_FILE: dict[str, str] = {
    "LC":   "glossary_LC.txt",
    "DStr": "glossary_DStr.txt",
    "DS":   "glossary_DS.txt",
    "LA":   "glossary_LA.txt",
    "OOP":  "glossary_OOP.txt",
    "Unix": "glossary_Unix.txt",
}


class PromptBuilder:
    def __init__(self, prompt_dir: Path):
        self.prompt_dir = Path(prompt_dir)
        self._base = self._read("00_base_prompt.txt")
        self._common = self._read("01_common_glossary.txt")

    def _read(self, filename: str) -> str:
        path = self.prompt_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Prompt file not found: {path}")
        return path.read_text(encoding="utf-8").strip()

    def build(self, subject_abbr: str | None) -> str:
        """subject_abbr에 맞는 시스템 프롬프트를 반환한다.

        subject_abbr이 None이거나 알 수 없는 코드면 공통 glossary만 포함한다.
        """
        parts = [self._base, "---", "# 공통 glossary", self._common]

        glossary_file = _GLOSSARY_FILE.get(subject_abbr or "")
        if glossary_file:
            path = self.prompt_dir / glossary_file
            if path.exists():
                subject_glossary = path.read_text(encoding="utf-8").strip()
                parts.extend(["---", f"# 과목별 glossary ({subject_abbr})", subject_glossary])

        return "\n\n".join(parts)
