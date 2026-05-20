from __future__ import annotations

from pathlib import Path

from .schemas import PromptBundle


class PromptLoadError(RuntimeError):
    def __init__(self, failure_class: str, path: Path):
        self.failure_class = failure_class
        self.path = path
        super().__init__(f"{failure_class}: {path}")


def _read_required(path: Path, failure_class: str) -> str:
    if not path.exists():
        raise PromptLoadError(failure_class, path)
    return path.read_text(encoding="utf-8").rstrip("\n")


def load_prompt_bundle(prompt_dir: str | Path, *, subject: str | None) -> PromptBundle:
    prompt_dir_path = Path(prompt_dir).expanduser()
    if not prompt_dir_path.exists():
        raise PromptLoadError("missing_prompt_dir", prompt_dir_path)

    base_path = prompt_dir_path / "00_base_prompt.txt"
    common_path = prompt_dir_path / "01_common_glossary.txt"
    subject_path = prompt_dir_path / f"glossary_{subject}.txt" if subject else None

    base = _read_required(base_path, "missing_base_prompt")
    common = _read_required(common_path, "missing_common_glossary")
    subject_glossary = None
    existing_subject_path = None
    if subject_path and subject_path.exists():
        subject_glossary = subject_path.read_text(encoding="utf-8").rstrip("\n")
        existing_subject_path = subject_path

    return PromptBundle(
        prompt_dir=prompt_dir_path,
        subject=subject,
        base_prompt=base,
        common_glossary=common,
        subject_glossary=subject_glossary,
        base_prompt_path=base_path,
        common_glossary_path=common_path,
        subject_glossary_path=existing_subject_path,
    )
