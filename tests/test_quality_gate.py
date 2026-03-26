from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.stt.quality_gate import evaluate


class QualityGateTests(unittest.TestCase):
    def test_repetitive_text_is_flagged_as_bad(self) -> None:
        segments = [{"text": "안녕하세요 안녕하세요 안녕하세요"} for _ in range(8)]
        text = "안녕하세요 " * 60

        report = evaluate(segments, text)

        self.assertEqual(report.health, "bad")
        self.assertLess(report.quality_score, 60)
        self.assertIn("반복", report.summary)

    def test_noise_and_short_segments_raise_warning(self) -> None:
        segments = [
            {"text": "."},
            {"text": "음"},
            {"text": "아"},
            {"text": ""},
            {"text": "네"},
            {"text": "..."},  # dot noise
            {"text": "테스트"},
        ]

        report = evaluate(segments, "음 아 네 테스트")

        self.assertIn(report.health, {"warn", "bad"})
        self.assertGreater(report.short_segments, 0)
        self.assertGreater(report.dot_noise_segments, 0)

    def test_normal_text_is_reported_as_good(self) -> None:
        segments = [
            {"text": "오늘은 선형대수학의 기저와 차원에 대해 설명하겠습니다."},
            {"text": "먼저 벡터 공간의 정의를 다시 확인해 보겠습니다."},
            {"text": "이후 예제를 통해 선형 독립과 생성 집합을 비교하겠습니다."},
        ]
        text = "\n".join(segment["text"] for segment in segments)

        report = evaluate(segments, text)

        self.assertEqual(report.health, "good")
        self.assertGreaterEqual(report.quality_score, 75)
        self.assertIn("정상", report.summary)
