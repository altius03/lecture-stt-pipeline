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

from lecture_stt.stt.notifier import NoopNotifier, TelegramNotifier, build_notifier  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "ok", json_data: dict | None = None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data if json_data is not None else {"ok": True}
        self.content = text.encode("utf-8")

    def json(self) -> dict:
        return self._json_data


class NotifierTests(unittest.TestCase):
    def test_failed_transports_log_only_safe_diagnostics(self) -> None:
        provider = "telegram"
        for failure in (OSError("private-test-value"), _FakeResponse(401, "private-test-value")):
            with self.subTest(provider=provider, failure=type(failure).__name__), tempfile.TemporaryDirectory() as tmp:
                args = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with (
                    mock.patch("lecture_stt.stt.notifier.requests.get", **args),
                    mock.patch("lecture_stt.stt.notifier.requests.post", **args),
                    self.assertLogs("lecture_stt.stt.notifier", "ERROR") as logs,
                ):
                    notifier = build_notifier(
                        {"notification": {"provider": provider}},
                        env={"TELEGRAM_BOT_TOKEN": "private-test-value", "TELEGRAM_CHAT_ID": "123",
                             "DISCORD_WEBHOOK_URL": "https://example.invalid/private-test-value"},
                        state_dir=tmp,
                    )
                    notifier.notify_success({"job_id": 9})
                self.assertNotIn("private-test-value", "\n".join(logs.output))
                self.assertFalse((Path(tmp) / "notified" / f"{provider}_success_9").exists())

    def test_auto_provider_ignores_discord_only_credentials(self) -> None:
        with mock.patch("lecture_stt.stt.notifier.requests.get") as get_mock:
            notifier = build_notifier(
                config={},
                env={"DISCORD_WEBHOOK_URL": "https://discord.example/webhook"},
                state_dir="/tmp",
            )

        self.assertIsInstance(notifier, NoopNotifier)
        get_mock.assert_not_called()

    def test_legacy_discord_selection_is_noop_even_with_telegram_credentials(self) -> None:
        with mock.patch("lecture_stt.stt.notifier.requests.get") as get_mock:
            notifier = build_notifier(
                config={"notification": {"provider": "discord", "enabled": True}},
                env={"TELEGRAM_BOT_TOKEN": "bot-token", "TELEGRAM_CHAT_ID": "123456"},
            )
        self.assertIsInstance(notifier, NoopNotifier)
        get_mock.assert_not_called()

    def test_telegram_request_errors_never_log_bot_token(self) -> None:
        token = "123456:FAKE_TEST_SECRET_TOKEN"
        api_url = f"https://api.telegram.org/bot{token}"
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                mock.patch("lecture_stt.stt.notifier.requests.get", side_effect=RuntimeError(f"GET {api_url}/getMe failed")),
                mock.patch("lecture_stt.stt.notifier.requests.post", side_effect=RuntimeError(f"POST {api_url}/sendMessage failed")),
                self.assertLogs("lecture_stt.stt.notifier", level="ERROR") as captured,
            ):
                notifier = build_notifier(
                    config={"notification": {"provider": "telegram"}},
                    env={"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": "123"},
                    state_dir=tmpdir,
                )
                notifier.notify_review({"job_id": 1, "orig_name": "test.wav", "review_message": "check"})

        logs = "\n".join(captured.output)
        self.assertNotIn(token, logs)
        self.assertNotIn("api.telegram.org", logs)
        self.assertIn("Telegram bot verification failed", logs)
        self.assertIn("Failed to send Telegram notification", logs)

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

    def test_legacy_dual_provider_sends_only_telegram(self) -> None:
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

            self.assertEqual(post_mock.call_count, 1)
            self.assertTrue((state_dir / "notified" / "telegram_error_99").exists())
            self.assertFalse((state_dir / "notified" / "discord_error_99").exists())

    def test_legacy_dual_review_uses_only_telegram_marker(self) -> None:
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
                    "review_message": "전사량이 적습니다. token=secret-123456789",
                    "completed": True,
                }
                notifier.notify_review(payload)
                notifier.notify_review(payload)

            self.assertEqual(post_mock.call_count, 1)
            self.assertIn("처리 종료", post_mock.call_args.kwargs["json"]["text"])
            self.assertNotIn("secret-123456789", post_mock.call_args.kwargs["json"]["text"])
            self.assertTrue((state_dir / "notified" / "telegram_review_completed_77").exists())
            self.assertFalse((state_dir / "notified" / "discord_review_completed_77").exists())


if __name__ == "__main__":
    unittest.main()
