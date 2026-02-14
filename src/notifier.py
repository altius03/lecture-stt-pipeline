from __future__ import annotations
import logging
import socket
from pathlib import Path
from typing import Any, Dict

import requests


logger = logging.getLogger(__name__)


class DiscordNotifier:
    MAX_CONTENT_LEN = 1900

    def __init__(self, webhook_url: str | None, state_dir: str | Path | None = None):
        self.webhook_url = webhook_url
        root = Path(state_dir) if state_dir else Path("/Users/geonha/lecture_stt/state")
        self.state_dir = root
        self._marker_dir = Path(self.state_dir) / "notified"

    def _marker_path(self, kind: str, job_id: Any) -> Path:
        return self._marker_dir / f"{kind}_{job_id}"

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
        if max_len <= len("...(생략)"):
            return "..."[:max_len]
        return value[: max_len - len("...(생략)")] + "...(생략)"

    def _post(self, content: str) -> bool:
        if not self.is_enabled():
            logger.debug("Discord webhook not configured; skipping notification")
            return False

        data = {"content": content}
        try:
            response = requests.post(self.webhook_url, json=data, timeout=10)
            if 200 <= response.status_code < 300:
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
        return bool(self.webhook_url)

    def notify_success(self, payload: Dict[str, Any]) -> None:
        if not self.is_enabled():
            return

        job_id = str(payload.get("job_id", "-"))
        if self._already_notified("success", job_id):
            return

        elapsed_sec = payload.get("elapsed_sec", "-")
        lines = [
            "[lecture_stt] ✅ 변환 성공",
            f"hostname: {socket.gethostname()}",
            f"job_id: {job_id}",
            f"원본 파일: {self._truncate(payload.get('orig_name', '-'), 200)}",
            f"캐노니컬: {self._truncate(payload.get('canonical_base', '-'), 200)}",
            f"inbox 경로: {self._truncate(payload.get('orig_inbox_path', '-'), 200)}",
            f"오디오 경로: {self._truncate(payload.get('canonical_audio_path', '-'), 240)}",
            f"결과(txt): {self._truncate(payload.get('transcript_txt_path', '-'), 240)}",
            f"결과(json): {self._truncate(payload.get('transcript_json_path', '-'), 240)}",
            f"처리시간: {self._truncate(elapsed_sec, 40)}s",
        ]
        content = "\n".join(lines)
        content = self._truncate(content, self.MAX_CONTENT_LEN)
        if self._post(content):
            self._mark_notified("success", job_id)

    def notify_error(self, payload: Dict[str, Any]) -> None:
        self.notify_failure(payload)

    def notify_failure(self, payload: Dict[str, Any]) -> None:
        if not self.is_enabled():
            return

        job_id = str(payload.get("job_id", "-"))
        if self._already_notified("error", job_id):
            return

        error_message = payload.get("error_message", "-")
        if len(str(error_message)) > 1100:
            error_message = self._truncate(error_message, 1100)

        lines = [
            "[lecture_stt] ❌ 변환 실패",
            f"hostname: {socket.gethostname()}",
            f"job_id: {job_id}",
            f"원본 파일: {self._truncate(payload.get('orig_name', '-'), 200)}",
            f"캐노니컬: {self._truncate(payload.get('canonical_base', '-'), 200)}",
            f"inbox 경로: {self._truncate(payload.get('orig_inbox_path', '-'), 200)}",
            f"오디오 경로: {self._truncate(payload.get('canonical_audio_path', '-'), 280)}",
            f"결과(txt): {self._truncate(payload.get('transcript_txt_path', '-'), 220)}",
            f"결과(json): {self._truncate(payload.get('transcript_json_path', '-'), 220)}",
            f"실패 사유: {self._truncate(error_message, 700)}",
        ]
        content = "\n".join(lines)
        content = self._truncate(content, self.MAX_CONTENT_LEN)
        if self._post(content):
            self._mark_notified("error", job_id)
