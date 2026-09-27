from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.downstream import postprocess  # noqa: E402
from lecture_stt.downstream.postprocess import (  # noqa: E402
    GENERATOR_BACKEND_CODEX,
    GeneratorSettings,
    PostprocessValidationError,
    generate_correction,
    generate_summary,
)

CANONICAL_TEMP_ROOT = "/private/tmp" if sys.platform == "darwin" else tempfile.gettempdir()


class CodexPostprocessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(
            tempfile.mkdtemp(prefix="codex-postprocess-", dir=CANONICAL_TEMP_ROOT)
        )
        self.addCleanup(shutil.rmtree, self.root, True)
        self.settings = GeneratorSettings(
            backend=GENERATOR_BACKEND_CODEX,
            codex_binary=Path("/usr/bin/true"),
            model="test-model",
            reasoning_effort="low",
            timeout_sec=30,
            max_attempts=3,
            temp_root=self.root,
            max_correction_chars=100_000,
            max_summary_chars=100_000,
        )

    @staticmethod
    def _source() -> dict[str, object]:
        return {
            "text": "원문입니다.",
            "language": "ko",
            "segments": [
                {"id": 10, "start": 0.0, "end": 1.0, "text": "원문입니다."}
            ],
        }

    def test_codex_exec_is_ephemeral_read_only_and_does_not_forward_api_keys(self) -> None:
        observed: dict[str, object] = {}

        def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
            output_index = command.index("--output-last-message") + 1
            output_path = Path(command[output_index])
            output_path.write_text('{"value":"ok"}', encoding="utf-8")
            observed.update(command=command, **kwargs)
            return SimpleNamespace(returncode=0)

        with (
            mock.patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "must-not-be-forwarded",
                    "UNRELATED_SECRET": "must-not-be-forwarded",
                },
            ),
            mock.patch.object(postprocess.subprocess, "run", side_effect=fake_run),
        ):
            result = postprocess._run_codex_json(
                self.settings,
                schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["value"],
                    "properties": {"value": {"type": "string"}},
                },
                prompt="untrusted transcript body",
            )

        command = observed["command"]
        child_env = observed["env"]
        self.assertEqual(result, {"value": "ok"})
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--skip-git-repo-check", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        disabled = {
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "--disable"
        }
        self.assertEqual(disabled, set(postprocess.CODEX_DISABLED_FEATURES))
        self.assertEqual(command[-1], "-")
        self.assertNotIn("OPENAI_API_KEY", child_env)
        self.assertNotIn("UNRELATED_SECRET", child_env)
        self.assertFalse(Path(observed["cwd"]).exists())

    def test_correction_rejects_changed_segment_id(self) -> None:
        with mock.patch.object(
            postprocess,
            "_run_codex_json",
            return_value={"segments": [{"id": 11, "text": "교정문입니다."}]},
        ):
            with self.assertRaisesRegex(PostprocessValidationError, "id mismatch"):
                generate_correction(
                    self.settings,
                    logical_stem="260101CA_2",
                    course_name="Data Structures",
                    source_json_payload=self._source(),
                )

    def test_correction_preserves_non_text_json_fields(self) -> None:
        with mock.patch.object(
            postprocess,
            "_run_codex_json",
            return_value={"segments": [{"id": 10, "text": "교정된 문장입니다."}]},
        ):
            result = generate_correction(
                self.settings,
                logical_stem="260101CA_2",
                course_name="Data Structures",
                source_json_payload=self._source(),
            )

        corrected = json.loads(result.transcript_json_bytes)
        self.assertEqual(corrected["language"], "ko")
        self.assertEqual(corrected["segments"][0]["start"], 0.0)
        self.assertEqual(corrected["segments"][0]["id"], 10)
        self.assertEqual(corrected["segments"][0]["text"], "교정된 문장입니다.")
        self.assertEqual(result.transcript_text, "교정된 문장입니다.")

    def test_summary_rejects_missing_contract_field(self) -> None:
        payload = {
            "title": "자료 구조",
            "overview": "개요",
            "key_points": [],
            "details": [],
            "examples": [],
            "cautions": [],
            "professor_points": [],
        }
        with mock.patch.object(postprocess, "_run_codex_json", return_value=payload):
            with self.assertRaisesRegex(
                PostprocessValidationError,
                "review_questions must be an array",
            ):
                generate_summary(
                    self.settings,
                    logical_stem="260101CA_2",
                    course_name="Data Structures",
                    corrected_text="교정된 전사",
                )

    def test_summary_collapses_generated_newlines_before_markdown_render(self) -> None:
        payload = {
            "title": "자료 구조\n## injected-title",
            "overview": "개요\n## injected-overview",
            "key_points": ["핵심\n## injected-list"],
            "details": [],
            "examples": [],
            "cautions": [],
            "professor_points": [],
            "review_questions": [],
        }
        with mock.patch.object(postprocess, "_run_codex_json", return_value=payload):
            result = generate_summary(
                self.settings,
                logical_stem="260101CA_2",
                course_name="Data Structures",
                corrected_text="교정된 전사",
            )

        headings = [
            line for line in result.markdown.splitlines() if line.startswith("#")
        ]
        self.assertEqual(
            headings[1:],
            [f"## {heading}" for heading in postprocess.SUMMARY_SECTION_HEADINGS],
        )
        self.assertNotIn("\n## injected", result.markdown)
        self.assertEqual(
            postprocess.summary_from_staged_markdown(result.markdown_bytes),
            result,
        )


if __name__ == "__main__":
    unittest.main()
