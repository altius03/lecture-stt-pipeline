from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lecture_stt.stt.notifier import DiscordNotifier, TelegramNotifier, build_notifier  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "ok", json_data: dict | None = None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data if json_data is not None else {"ok": True}
        self.content = text.encode("utf-8")

    def json(self) -> dict:
        return self._json_data


class NotifierTests(unittest.TestCase):
    def test_auto_provider_falls_back_to_discord(self) -> None:
        with mock.patch("lecture_stt.stt.notifier.requests.get", return_value=_FakeResponse()):
            notifier = build_notifier(
                config={},
                env={"DISCORD_WEBHOOK_URL": "https://discord.example/webhook"},
                state_dir="/tmp",
            )

        self.assertIsInstance(notifier, DiscordNotifier)

    def test_explicit_telegram_sends_plain_text_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                mock.patch("lecture_stt.stt.notifier.requests.get", return_value=_FakeResponse()),
                mock.patch("lecture_stt.stt.notifier.requests.post", return_value=_FakeResponse()) as post_mock,
            ):
                notifier = build_notifier(
                    config={
                        "notification": {
                            "provider": "telegram",
                        }
                    },
                    env={
                        "TELEGRAM_BOT_TOKEN": "bot-token",
                        "TELEGRAM_CHAT_ID": "123456",
                        "TELEGRAM_MESSAGE_THREAD_ID": "77",
                    },
                    state_dir=tmpdir,
                )
                self.assertIsInstance(notifier, TelegramNotifier)

                notifier.notify_success(
                    {
                        "job_id": 42,
                        "orig_name": "sample.m4a",
                        "elapsed_sec": 12.3,
                        "quality_health": "good",
                        "quality_summary": "반복 없음",
                        "pending_count": 1,
                        "processing_count": 2,
                    }
                )

        self.assertEqual(post_mock.call_count, 1)
        url = post_mock.call_args.kwargs["json"]
        self.assertEqual(url["chat_id"], "123456")
        self.assertEqual(url["message_thread_id"], "77")
        self.assertIn("sample.m4a", url["text"])
        self.assertIn("소요시간: 12.3초", url["text"])

    def test_dual_provider_uses_provider_specific_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            with (
                mock.patch("lecture_stt.stt.notifier.requests.get", return_value=_FakeResponse()),
                mock.patch("lecture_stt.stt.notifier.requests.post", return_value=_FakeResponse()) as post_mock,
            ):
                notifier = build_notifier(
                    config={
                        "notification": {
                            "provider": "telegram",
                            "dual_send_providers": ["discord"],
                        }
                    },
                    env={
                        "TELEGRAM_BOT_TOKEN": "bot-token",
                        "TELEGRAM_CHAT_ID": "123456",
                        "DISCORD_WEBHOOK_URL": "https://discord.example/webhook",
                    },
                    state_dir=state_dir,
                )

                payload = {
                    "job_id": 99,
                    "orig_name": "dual.wav",
                    "error_step": "전사 실행",
                    "error_message": "mocked failure",
                }
                notifier.notify_error(payload)
                notifier.notify_error(payload)

            self.assertEqual(post_mock.call_count, 2)
            self.assertTrue((state_dir / "notified" / "telegram_error_99").exists())
            self.assertTrue((state_dir / "notified" / "discord_error_99").exists())

    def test_dual_provider_review_uses_distinct_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            with (
                mock.patch("lecture_stt.stt.notifier.requests.get", return_value=_FakeResponse()),
                mock.patch("lecture_stt.stt.notifier.requests.post", return_value=_FakeResponse()) as post_mock,
            ):
                notifier = build_notifier(
                    config={
                        "notification": {
                            "provider": "telegram",
                            "dual_send_providers": ["discord"],
                        }
                    },
                    env={
                        "TELEGRAM_BOT_TOKEN": "bot-token",
                        "TELEGRAM_CHAT_ID": "123456",
                        "DISCORD_WEBHOOK_URL": "https://discord.example/webhook",
                    },
                    state_dir=state_dir,
                )

                payload = {
                    "job_id": 77,
                    "orig_name": "review.wav",
                    "review_step": "품질 검사",
                    "review_message": "전사량이 비정상적으로 적습니다.",
                }
                notifier.notify_review(payload)
                notifier.notify_review(payload)

            self.assertEqual(post_mock.call_count, 2)
            self.assertTrue((state_dir / "notified" / "telegram_review_77").exists())
            self.assertTrue((state_dir / "notified" / "discord_review_77").exists())


if __name__ == "__main__":
    unittest.main()
