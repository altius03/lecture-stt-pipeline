from __future__ import annotations

import logging
from pathlib import Path
import re
from typing import Any, Dict, Mapping

import requests

from lecture_stt.shared.paths import state_dir as default_state_dir


logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = {"auto", "telegram", "noop"}

_SENSITIVE_TEXT_PATTERNS = (
    (re.compile(r"(?i)\b(authorization)\s*[:=]\s*(?:bearer\s+)?([^\s,;\)\]\}\"']+)"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|token|password|passwd|secret)\s*[:=]\s*(?:bearer\s+)?([^\s,;\)\]\}\"']+)"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+"), r"\1[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{3,}\b"), "[REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "[REDACTED]"),
)


def redact_sensitive_text(value: Any) -> str:
    text = str(value)
    for pattern, replacement in _SENSITIVE_TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class BaseNotifier:
    MAX_CONTENT_LEN = 1900

    def __init__(
        self,
        provider_name: str,
        state_dir: str | Path | None = None,
        enabled: bool = True,
        send_start: bool = True,
        send_success: bool = True,
        send_review: bool = True,
        send_failure: bool = True,
    ):
        self.provider_name = provider_name
        self._enabled = enabled
        self.send_start = send_start
        self.send_success = send_success
        self.send_review = send_review
        self.send_failure = send_failure
        root = Path(state_dir) if state_dir else default_state_dir()
        self.state_dir = root
        self._marker_dir = Path(self.state_dir) / "notified"

    def is_enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def _truncate(text: Any, max_len: int) -> str:
        if text is None:
            return "-"
        value = str(text)
        if len(value) <= max_len:
            return value
        if max_len <= len("...(truncated)"):
            return "...(truncated)"[:max_len]
        return value[: max_len - len("...(truncated)")] + "...(truncated)"

    def _marker_path(self, kind: str, job_id: Any) -> Path:
        return self._marker_dir / f"{self.provider_name}_{kind}_{job_id}"

    def _already_notified(self, kind: str, job_id: Any) -> bool:
        try:
            return self._marker_path(kind, job_id).exists()
        except Exception:
            return False

    def _mark_notified(self, kind: str, job_id: Any) -> bool:
        marker = self._marker_path(kind, job_id)
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch(exist_ok=True)
            return True
        except Exception as exc:
            logger.warning("Failed to write notification marker %s: %s", marker, self._safe_error_text(exc))
            return False

    def _safe_error_text(self, value: Any) -> str:
        return redact_sensitive_text(value)

    def _queue_suffix(self, payload: Dict[str, Any]) -> str:
        pending = payload.get("pending_count")
        processing = payload.get("processing_count")
        if pending is None and processing is None:
            return ""
        return f"대기중: {self._truncate(pending, 20)}건, 처리중: {self._truncate(processing, 20)}건"

    def _send_once(self, kind: str, job_id: Any, lines: list[str]) -> None:
        if not self.is_enabled():
            return

        event_job_id = str(job_id) if job_id is not None else "global"
        if self._already_notified(kind, event_job_id):
            logger.debug(
                "Skipping duplicate notification: provider=%s, kind=%s, job_id=%s",
                self.provider_name,
                kind,
                event_job_id,
            )
            return

        content = "\n".join([line for line in lines if line])
        content = self._truncate(self._safe_error_text(content), self.MAX_CONTENT_LEN)
        if self._post(content):
            self._mark_notified(kind, event_job_id)

    def _post(self, content: str) -> bool:
        try:
            if self._transport_send(content):
                logger.info("%s notification sent successfully", self.provider_name.capitalize())
                return True
        except Exception as exc:
            logger.error("Failed to send %s notification (%s)", self.provider_name.capitalize(), type(exc).__name__)
        return False

    def _transport_send(self, content: str) -> bool:
        raise NotImplementedError

    def notify_started(self, payload: Dict[str, Any]) -> None:
        if not self.is_enabled() or not self.send_start:
            return

        job_id = str(payload.get("job_id", "-"))
        file_name = self._truncate(payload.get("orig_name", "-"), 240)
        self._send_once(
            "started",
            job_id,
            [
                f"📥 [{file_name}] 전사 처리 시작",
                f"작업 #{job_id}",
                self._queue_suffix(payload),
            ],
        )

    def notify_success(self, payload: Dict[str, Any]) -> None:
        if not self.is_enabled() or not self.send_success:
            return

        job_id = str(payload.get("job_id", "-"))
        file_name = self._truncate(payload.get("orig_name", "-"), 240)
        elapsed = payload.get("elapsed_sec", "-")
        if isinstance(elapsed, (int, float)):
            elapsed_str = f"{elapsed:.1f}초"
        else:
            elapsed_str = f"{self._truncate(elapsed, 40)}초"

        health = payload.get("quality_health", "")
        quality_line = ""
        if health:
            icon = {"good": "🟢", "warn": "🟡", "bad": "🔴"}.get(health, "⚪")
            summary = payload.get("quality_summary", "")
            quality_line = f"{icon} 품질: {summary}"

        self._send_once(
            "success",
            job_id,
            [
                f"✅ [{file_name}] 전사 완료",
                f"작업 #{job_id} / 소요시간: {elapsed_str}",
                quality_line,
                self._queue_suffix(payload),
            ],
        )

    def notify_error(self, payload: Dict[str, Any]) -> None:
        self.notify_failure(payload)

    def notify_review(self, payload: Dict[str, Any]) -> None:
        if not self.is_enabled() or not self.send_review:
            return

        job_id = str(payload.get("job_id", "-"))
        file_name = self._truncate(payload.get("orig_name", "-"), 240)
        review_step = self._truncate(payload.get("review_step", "검토 필요"), 80)
        review_message = self._truncate(redact_sensitive_text(payload.get("review_message", "-")), 700)

        self._send_once(
            "review_completed" if payload.get("completed") else "review",
            job_id,
            [
                (
                    f"⚠️ [{file_name}] 처리 종료 · 결과 확인 권장"
                    if payload.get("completed")
                    else f"⚠️ [{file_name}] 전사 결과 검토 필요"
                ),
                f"작업 #{job_id}",
                f"검토 단계: {review_step}",
                f"사유: {review_message}",
                self._queue_suffix(payload),
            ],
        )

    def notify_failure(self, payload: Dict[str, Any]) -> None:
        if not self.is_enabled() or not self.send_failure:
            return

        job_id = str(payload.get("job_id", "-"))
        file_name = self._truncate(payload.get("orig_name", "-"), 240)
        error_step = self._truncate(payload.get("error_step", "-"), 80)
        error_message = self._truncate(payload.get("error_message", "-"), 700)

        self._send_once(
            "error",
            job_id,
            [
                f"❌ [{file_name}] 전사 실패",
                f"작업 #{job_id}",
                f"실패 단계: {error_step}",
                f"원인: {error_message}",
                self._queue_suffix(payload),
            ],
        )

    def notify_detected(self, payload: Dict[str, Any]) -> None:
        self.notify_started(payload)

    def notify_moved(self, payload: Dict[str, Any]) -> None:
        pass

    def notify_transcript_generated(self, payload: Dict[str, Any]) -> None:
        pass

    def notify_completed(self, payload: Dict[str, Any]) -> None:
        pass


class TelegramNotifier(BaseNotifier):
    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        state_dir: str | Path | None = None,
        enabled: bool = True,
        message_thread_id: str | None = None,
        send_start: bool = True,
        send_success: bool = True,
        send_review: bool = True,
        send_failure: bool = True,
    ):
        self.bot_token = bot_token
        self.chat_id = str(chat_id) if chat_id else None
        self.message_thread_id = str(message_thread_id) if message_thread_id else None
        super().__init__(
            provider_name="telegram",
            state_dir=state_dir,
            enabled=enabled and bool(bot_token) and bool(chat_id),
            send_start=send_start,
            send_success=send_success,
            send_review=send_review,
            send_failure=send_failure,
        )
        if self.is_enabled():
            logger.info("Telegram notifier enabled (bot token/chat id set)")
            self._test_bot()
        elif enabled:
            logger.warning("Telegram notifier disabled: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")
        else:
            logger.info("Telegram notifier disabled by config")

    @property
    def _api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    @property
    def _safe_api_base(self) -> str:
        return "https://api.telegram.org/bot<REDACTED>"

    def _safe_error_text(self, value: Any) -> str:
        text = str(value)
        if self.bot_token:
            text = text.replace(self._api_base, self._safe_api_base)
            text = text.replace(self.bot_token, "<REDACTED>")
        return redact_sensitive_text(text)

    def _test_bot(self) -> None:
        if not self.bot_token:
            return
        try:
            response = requests.get(f"{self._api_base}/getMe", timeout=5)
            if 200 <= response.status_code < 300:
                body = response.json() if response.content else {}
                if body.get("ok", True):
                    logger.info("Telegram bot token verified successfully")
                    return
            logger.error("Telegram bot verification failed (HTTP %s)", response.status_code)
        except Exception as exc:
            logger.error("Telegram bot verification failed (%s)", type(exc).__name__)

    def _transport_send(self, content: str) -> bool:
        if not self.bot_token or not self.chat_id:
            return False

        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": content,
            "disable_web_page_preview": True,
        }
        if self.message_thread_id:
            payload["message_thread_id"] = self.message_thread_id

        response = requests.post(f"{self._api_base}/sendMessage", json=payload, timeout=10)
        if 200 <= response.status_code < 300:
            body = response.json() if response.content else {}
            if body.get("ok", True):
                return True

        logger.error("Telegram sendMessage failed (HTTP %s)", response.status_code)
        return False


class NoopNotifier(BaseNotifier):
    def __init__(self):
        super().__init__(provider_name="noop", enabled=False)

    def _transport_send(self, content: str) -> bool:
        return False


def _bool_value(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def build_notifier(
    config: Mapping[str, Any] | None,
    env: Mapping[str, str] | None = None,
    state_dir: str | Path | None = None,
) -> BaseNotifier:
    resolved_env = env or {}
    notification_cfg = dict((config or {}).get("notification") or {})
    provider = str(notification_cfg.get("provider", "auto") or "auto").strip().lower()
    if provider in {"noop", "discord"} or not _bool_value(notification_cfg.get("enabled"), True):
        return NoopNotifier()
    if provider not in {"auto", "telegram"}:
        raise ValueError(f"Unsupported notification provider: {provider}")
    notifier = TelegramNotifier(
        bot_token=resolved_env.get("TELEGRAM_BOT_TOKEN"),
        chat_id=resolved_env.get("TELEGRAM_CHAT_ID"),
        message_thread_id=resolved_env.get("TELEGRAM_MESSAGE_THREAD_ID"),
        state_dir=state_dir,
        send_start=_bool_value(notification_cfg.get("send_start"), True),
        send_success=_bool_value(notification_cfg.get("send_success"), True),
        send_review=_bool_value(notification_cfg.get("send_review"), True),
        send_failure=_bool_value(notification_cfg.get("send_failure"), True),
    )
    return notifier if notifier.is_enabled() else NoopNotifier()
