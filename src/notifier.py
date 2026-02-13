from __future__ import annotations

import json
import logging
import socket
from typing import Any, Dict

import requests


logger = logging.getLogger(__name__)


class DiscordNotifier:
    def __init__(self, webhook_url: str | None):
        self.webhook_url = webhook_url

    def is_enabled(self) -> bool:
        return bool(self.webhook_url)

    def notify_error(self, payload: Dict[str, Any]) -> None:
        if not self.is_enabled():
            logger.debug("Discord webhook not configured; skipping failure notification")
            return

        lines = [
            "[lecture_stt] ERROR job failed",
            f"hostname: {socket.gethostname()}",
            f"job_id: {payload.get('job_id', '-')}",
            f"original_file: {payload.get('orig_name', '-')}",
            f"canonical_file: {payload.get('canonical_base', '-')}",
            f"orig_inbox_path: {payload.get('orig_inbox_path', '-')}",
            f"canonical_audio_path: {payload.get('canonical_audio_path', '-')}",
            f"error: {payload.get('error_message', '-')}",
        ]
        content = "\n".join(lines)
        data = {"content": content}

        try:
            response = requests.post(self.webhook_url, json=data, timeout=10)
            if response.status_code >= 300:
                logger.error("Discord webhook failed: %s %s", response.status_code, response.text[:500])
        except Exception:
            logger.exception("Failed to send Discord notification")

