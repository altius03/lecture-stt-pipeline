from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

import requests

from lecture_stt.shared.paths import state_dir as default_state_dir


logger = logging.getLogger(__name__)


class DiscordNotifier:
    MAX_CONTENT_LEN = 1900

    # Discord 웹훅 전송을 담당한다.
    def __init__(self, webhook_url: str | None, state_dir: str | Path | None = None):
        # 웹훅 URL이 없으면 조용히 스킵한다.
        self.webhook_url = webhook_url
        root = Path(state_dir) if state_dir else default_state_dir()
        self.state_dir = root
        self._marker_dir = Path(self.state_dir) / "notified"
        if self.is_enabled():
            logger.info("Discord notifier enabled (webhook URL set)")
            self._test_webhook()
        else:
            logger.warning("Discord notifier disabled: DISCORD_WEBHOOK_URL not set")

    def _test_webhook(self) -> None:
        """시작 시 웹훅 URL 유효성을 확인한다 (실제 메시지는 보내지 않음)."""
        if not self.webhook_url:
            return
        try:
            # Discord webhook URL에 GET 요청으로 유효성 확인
            response = requests.get(self.webhook_url, timeout=5)
            if 200 <= response.status_code < 300:
                logger.info("Discord webhook URL verified successfully")
            else:
                logger.error(
                    "Discord webhook URL verification failed: %s %s",
                    response.status_code,
                    self._truncate(response.text, 200),
                )
        except Exception:
            logger.exception("Discord webhook URL verification failed")

    def _marker_path(self, kind: str, job_id: Any) -> Path:
        # 이벤트별/작업별 1회 알림 마커를 관리한다.
        return self._marker_dir / f"{kind}_{job_id}"

    def _already_notified(self, kind: str, job_id: Any) -> bool:
        try:
            return self._marker_path(kind, job_id).exists()
        except Exception:
            return False

    def _mark_notified(self, kind: str, job_id: Any) -> bool:
        # 중복 알림을 막기 위해 마커 파일을 생성한다.
        marker = self._marker_path(kind, job_id)
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch(exist_ok=True)
            return True
        except Exception:
            logger.warning("Failed to write notification marker %s", marker, exc_info=True)
            return False

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        if text is None:
            return "-"
        value = str(text)
        if len(value) <= max_len:
            return value
        if max_len <= len("...(truncated)"):
            return "...(truncated)"[:max_len]
        return value[: max_len - len("...(truncated)")] + "...(truncated)"

    def _post(self, content: str) -> bool:
        # 전송 실패가 파이프라인 전체를 중단하지 않도록 실패는 로그로만 남긴다.
        if not self.is_enabled():
            return False

        data = {"content": content}
        try:
            response = requests.post(self.webhook_url, json=data, timeout=10)
            if 200 <= response.status_code < 300:
                logger.info("Discord notification sent successfully")
                return True
            logger.error(
                "Discord webhook failed: %s %s",
                response.status_code,
                self._truncate(response.text, 500),
            )
        except Exception:
            logger.exception("Failed to send Discord notification")
        return False

    def is_enabled(self) -> bool:
        # 환경변수에 URL이 있을 때만 알림을 발송한다.
        return bool(self.webhook_url)

    def _send_once(self, kind: str, job_id: Any, lines: list[str]) -> None:
        if not self.is_enabled():
            return

        event_job_id = str(job_id) if job_id is not None else "global"
        if self._already_notified(kind, event_job_id):
            logger.debug("Skipping duplicate notification: kind=%s, job_id=%s", kind, event_job_id)
            return

        content = "\n".join([line for line in lines if line])
        content = self._truncate(content, self.MAX_CONTENT_LEN)
        if self._post(content):
            self._mark_notified(kind, event_job_id)

    def _queue_suffix(self, payload: Dict[str, Any]) -> str:
        # 알림 본문에서 진행중/대기중 큐 상태를 한 줄로 보여준다.
        pending = payload.get("pending_count")
        processing = payload.get("processing_count")
        if pending is None and processing is None:
            return ""
        return f"대기중: {self._truncate(pending, 20)}건, 처리중: {self._truncate(processing, 20)}건"

    # ── 통합 알림: 시작 / 완료 / 실패 (3종) ──

    def notify_started(self, payload: Dict[str, Any]) -> None:
        """파일 감지 + 전사 처리 시작을 하나의 알림으로 통합한다."""
        if not self.is_enabled():
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
        """전사 성공 완료를 알린다."""
        if not self.is_enabled():
            return

        job_id = str(payload.get("job_id", "-"))
        file_name = self._truncate(payload.get("orig_name", "-"), 240)
        elapsed = payload.get("elapsed_sec", "-")
        if isinstance(elapsed, (int, float)):
            elapsed_str = f"{elapsed:.1f}초"
        else:
            elapsed_str = f"{self._truncate(elapsed, 40)}초"

        # v2: 품질 정보
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
        """실패/오류 알림을 전송한다."""
        self.notify_failure(payload)

    def notify_failure(self, payload: Dict[str, Any]) -> None:
        """실패 알림을 한국어 템플릿으로 표시한다."""
        if not self.is_enabled():
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

    # ── 하위 호환용 래퍼 (기존 호출부가 깨지지 않도록) ──

    def notify_detected(self, payload: Dict[str, Any]) -> None:
        """하위 호환: notify_started로 위임한다."""
        self.notify_started(payload)

    def notify_moved(self, payload: Dict[str, Any]) -> None:
        """하위 호환: 파일 이동은 별도 알림 없이 무시한다."""
        pass

    def notify_transcript_generated(self, payload: Dict[str, Any]) -> None:
        """하위 호환: 전사문 생성은 별도 알림 없이 무시한다."""
        pass

    def notify_completed(self, payload: Dict[str, Any]) -> None:
        """하위 호환: notify_success에서 이미 처리되므로 무시한다."""
        pass
