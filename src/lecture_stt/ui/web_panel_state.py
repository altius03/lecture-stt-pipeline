from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from lecture_stt.shared import db
from lecture_stt.shared.paths import env_file, package_env, resolve_config_path, runtime_env, venv_python
from lecture_stt.storage_v2.analytics import (
    disabled_transcription_analytics,
    read_transcription_analytics,
)
from lecture_stt.storage_v2.archive_review import (
    ArchiveReviewWriteDisabledError,
    apply_archive_review_promotion,
    list_archive_review_cases,
    plan_archive_review_promotion,
    read_archive_review_case,
    update_archive_review_status,
)
from lecture_stt.storage_v2.library import (
    RecordingLibraryDisabledError,
    disabled_recording_library_list,
    list_recordings,
    read_recording_detail,
)
from lecture_stt.storage_v2.title_suggestions import (
    TitleSuggestionWriteDisabledError,
    apply_title_suggestion_confirmation,
    list_title_suggestions,
    plan_title_suggestion_confirmation,
    read_title_suggestion,
    reject_title_suggestion,
)
from lecture_stt.storage_v2.timetable import (
    TimetableWriteDisabledError,
    apply_classification_confirmation,
    list_classification_proposals,
    list_timetable_entries,
    plan_classification_confirmation,
    read_classification_proposal,
    update_classification_status,
)
from lecture_stt.storage_v2.unified_review import read_unified_review_feed
import yaml


NOTIFICATION_SELECTIONS = ("telegram", "discord", "both", "disabled")
DEFAULT_CONTROLLER_LABEL = "com.geonha.lecture-stt-controller"
CONTROLLER_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
CONTROLLER_PID_SCHEMA = "lecture-stt/controller-pid@2"


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _prometheus_escape_label(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace('"', '\\"')
    )


def _prometheus_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    payload = ",".join(
        f'{key}="{_prometheus_escape_label(value)}"' for key, value in labels.items()
    )
    return f"{{{payload}}}"


class ControlState:
    # 폴링은 상태에 따라 동적으로 조정해 불필요한 부하를 줄인다.
    FAST_POLL_INTERVAL_SEC = 0.5
    NORMAL_POLL_INTERVAL_SEC = 1
    IDLE_POLL_INTERVAL_SEC = 2
    LOG_READ_BYTES = 65536
    RECENT_JOB_LIMIT = 20
    SUBPROCESS_TIMEOUT_SEC = 8.0

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.config_path = self.repo_root / "config" / "config.yaml"
        self.worker_module = "lecture_stt.stt.main"
        self.python_bin = venv_python()
        if not self.python_bin.exists():
            self.python_bin = Path(sys.executable)

        self.worker_proc: subprocess.Popen | None = None
        self.controller_proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.notice = "대기중"
        self._shutdown_handler = None
        self._poll_boost_until = 0.0
        self._notification_restart_required = False

        self.config_data = self._load_config()
        paths = self.config_data.get("paths", {})
        resolved_env = runtime_env(dotenv_path=env_file(self.repo_root))
        self.db_path = resolve_config_path(
            str(paths.get("db_path", "state/jobs.sqlite3")),
            base_dir=self.repo_root,
            env=resolved_env,
        )
        self.pause_flag = self.db_path.parent / "paused"
        self.log_path = self._resolve_log_path()
        self.watch_folder = self._resolve_optional_path(paths.get("watch_folder"))
        self.audio_folder = self._resolve_optional_path(paths.get("stable_audio_folder"))
        self.transcript_folder = self._resolve_optional_path(paths.get("transcript_folder"))
        self.error_folder = self._resolve_optional_path(paths.get("error_folder"))
        self._ensure_db_schema()

    def _ensure_db_schema(self) -> None:
        # 구버전 DB에서도 제어판이 바로 동작하도록 최신 컬럼을 보정한다.
        try:
            db.init_db(str(self.db_path))
        except Exception as exc:
            self.notice = f"DB 초기화 실패: {exc}"

    def _legacy_recent_jobs(self) -> list[tuple]:
        # 단계/진행률 컬럼이 없는 DB용 폴백 조회 쿼리.
        with sqlite3.connect(self.db_path, timeout=3.0) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 3000")
            rows = conn.execute(
                "SELECT id, status, orig_name, updated_at, '' AS step, 0 AS progress, '' AS error_message "
                "FROM jobs ORDER BY id DESC LIMIT ?",
                (self.RECENT_JOB_LIMIT,),
            ).fetchall()
        return [
            (
                row["id"],
                row["status"],
                row["orig_name"],
                row["updated_at"],
                row["step"],
                int(row["progress"] or 0),
                row["error_message"],
            )
            for row in rows
        ]

    def _legacy_processing_jobs(self) -> list[tuple]:
        # 처리중 작업 조회용 호환 경로.
        with sqlite3.connect(self.db_path, timeout=3.0) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 3000")
            rows = conn.execute(
                "SELECT id, orig_name, '' AS current_step, 0 AS progress_pct, 0 AS eta_sec, updated_at "
                "FROM jobs WHERE status = 'PROCESSING' ORDER BY updated_at ASC"
            ).fetchall()
        return [
            (
                row["id"],
                row["orig_name"],
                row["current_step"] or "",
                int(row["progress_pct"] or 0),
                row["eta_sec"],
            )
            for row in rows
        ]

    def _load_config(self) -> dict:
        if not self.config_path.exists():
            raise FileNotFoundError(f"Missing config file: {self.config_path}")
        with self.config_path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Invalid config format: {self.config_path}")
        return loaded

    def _save_config(self, config_data: dict[str, Any]) -> None:
        with self.config_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(config_data, handle, allow_unicode=True, sort_keys=False)
        self.config_data = config_data

    def _runtime_env(self) -> dict[str, str]:
        return runtime_env(dotenv_path=env_file(self.repo_root))

    @staticmethod
    def _notification_defaults() -> dict[str, Any]:
        return {
            "provider": "auto",
            "enabled": True,
            "send_start": True,
            "send_success": True,
            "send_review": True,
            "send_failure": True,
            "dual_send_providers": [],
        }

    def _notification_config(self) -> dict[str, Any]:
        current = self.config_data.get("notification")
        merged = self._notification_defaults()
        if isinstance(current, dict):
            merged.update(current)
        return merged

    def _notification_availability(self) -> dict[str, bool]:
        runtime_env = self._runtime_env()
        return {
            "telegram": bool(runtime_env.get("TELEGRAM_BOT_TOKEN") and runtime_env.get("TELEGRAM_CHAT_ID")),
            "discord": bool(runtime_env.get("DISCORD_WEBHOOK_URL")),
        }

    @staticmethod
    def _normalize_dual_send(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            items = [item.strip().lower() for item in value.split(",")]
            return [item for item in items if item]
        if isinstance(value, list):
            return [str(item).strip().lower() for item in value if str(item).strip()]
        return []

    def _notification_selection(self) -> str:
        notification_cfg = self._notification_config()
        if not bool(notification_cfg.get("enabled", True)):
            return "disabled"

        provider = str(notification_cfg.get("provider", "auto") or "auto").strip().lower()
        dual_send = set(self._normalize_dual_send(notification_cfg.get("dual_send_providers")))
        availability = self._notification_availability()

        if provider == "noop":
            return "disabled"
        if {"telegram", "discord"}.issubset(dual_send | {provider}):
            return "both"
        if provider == "telegram":
            return "telegram"
        if provider == "discord":
            return "discord"
        if provider == "auto":
            if availability["telegram"]:
                return "telegram"
            if availability["discord"]:
                return "discord"
        return "disabled"

    @staticmethod
    def _notification_label(selection: str) -> str:
        labels = {
            "telegram": "텔레그램만",
            "discord": "디스코드만",
            "both": "둘 다",
            "disabled": "끄기",
        }
        return labels.get(selection, "끄기")

    def _notification_options(self) -> list[dict[str, object]]:
        availability = self._notification_availability()
        return [
            {
                "id": "telegram",
                "label": "텔레그램만",
                "description": "TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID가 필요합니다.",
                "available": availability["telegram"],
            },
            {
                "id": "discord",
                "label": "디스코드만",
                "description": "DISCORD_WEBHOOK_URL이 필요합니다.",
                "available": availability["discord"],
            },
            {
                "id": "both",
                "label": "둘 다",
                "description": "텔레그램과 디스코드 secret이 모두 필요합니다.",
                "available": availability["telegram"] and availability["discord"],
            },
            {
                "id": "disabled",
                "label": "끄기",
                "description": "알림 전송을 중단합니다.",
                "available": True,
            },
        ]

    def _notification_state(self, managed_running: bool, external_pids: list[int]) -> dict[str, object]:
        selection = self._notification_selection()
        can_apply_now = self._has_active_runtime(
            managed_running=managed_running,
            external_pids=external_pids,
        )
        apply_label = (
            "변경하면 실행 중 워커에 즉시 적용됩니다."
            if can_apply_now
            else "다음 시작부터 적용됩니다."
        )
        return {
            "selection": selection,
            "selected_label": self._notification_label(selection),
            "apply_label": apply_label,
            "restart_required": self._notification_restart_required,
            "can_apply_now": can_apply_now,
            "options": self._notification_options(),
        }

    def update_notification_selection(self, selection: str) -> None:
        normalized = str(selection or "").strip().lower()
        if normalized not in NOTIFICATION_SELECTIONS:
            raise RuntimeError("지원하지 않는 알림 채널 선택입니다.")

        availability = self._notification_availability()
        if normalized == "telegram" and not availability["telegram"]:
            raise RuntimeError("텔레그램 secret이 없어 선택할 수 없습니다.")
        if normalized == "discord" and not availability["discord"]:
            raise RuntimeError("디스코드 webhook이 없어 선택할 수 없습니다.")
        if normalized == "both" and not (availability["telegram"] and availability["discord"]):
            raise RuntimeError("둘 다를 선택하려면 텔레그램과 디스코드 설정이 모두 필요합니다.")

        config_data = self._load_config()
        notification_cfg = self._notification_defaults()
        current_notification = config_data.get("notification")
        if isinstance(current_notification, dict):
            notification_cfg.update(current_notification)

        if normalized == "telegram":
            notification_cfg.update({"provider": "telegram", "enabled": True, "dual_send_providers": []})
        elif normalized == "discord":
            notification_cfg.update({"provider": "discord", "enabled": True, "dual_send_providers": []})
        elif normalized == "both":
            notification_cfg.update({"provider": "telegram", "enabled": True, "dual_send_providers": ["discord"]})
        else:
            notification_cfg.update({"provider": "noop", "enabled": False, "dual_send_providers": []})

        config_data["notification"] = notification_cfg
        self._save_config(config_data)

        if self._has_active_runtime():
            self._notification_restart_required = True
            self.notice = "알림 채널이 저장되었습니다. 실행 중 워커에 즉시 적용합니다."
        else:
            self._notification_restart_required = False
            self.notice = "알림 채널이 저장되었습니다. 다음 시작부터 적용됩니다."
        self._set_poll_boost()

    def _wait_for_worker_shutdown(self, timeout_sec: float = 30.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_sec)
        while time.monotonic() < deadline:
            if not self._is_managed_running() and not self._find_worker_pids():
                return True
            time.sleep(0.5)
        return not self._is_managed_running() and not self._find_worker_pids()

    def restart_worker_for_notification(self) -> None:
        if not self._notification_restart_required:
            self.notice = "재시작이 필요한 알림 변경이 없습니다."
            self._set_poll_boost()
            return

        if not self._has_active_runtime():
            self._notification_restart_required = False
            self.notice = "알림 채널이 저장되었습니다. 다음 시작부터 적용됩니다."
            self._set_poll_boost()
            return

        self.stop()
        if "실패" in self.notice:
            return
        if not self._wait_for_worker_shutdown():
            # 타임아웃: 전사 중일 수 있음. config는 이미 저장됐으므로
            # 새 관리 워커는 시작하지 않고 기존 워커 자연 종료를 기다린다.
            self._notification_restart_required = False
            self.notice = "알림 채널 저장 완료. 현재 전사가 끝나면 반영됩니다 (워커를 재시작하면 즉시 적용)."
            self._set_poll_boost()
            return
        self.start()
        if self._has_active_runtime():
            self._notification_restart_required = False
            self.notice = "알림 채널 변경을 즉시 적용했습니다."

    def _resolve_log_path(self) -> Path:
        logging_cfg = self.config_data.get("logging", {})
        configured = str(logging_cfg.get("file", "logs/app.log"))
        return resolve_config_path(configured, base_dir=self.repo_root, env=self._runtime_env())

    def _resolve_optional_path(self, configured: Any) -> Path:
        if not isinstance(configured, str) or not configured.strip():
            return Path()
        return resolve_config_path(configured, base_dir=self.repo_root, env=self._runtime_env())

    def _transcription_analytics_settings(self) -> dict[str, Any]:
        storage_config = self.config_data.get("storage_v2")
        if not isinstance(storage_config, dict):
            storage_config = {}
        analytics_config = storage_config.get("analytics")
        if not isinstance(analytics_config, dict):
            analytics_config = {}
        enabled = analytics_config.get("enabled") is True
        if not enabled:
            return {"enabled": False}

        configured_records_root = storage_config.get("records_root")
        if (
            not isinstance(configured_records_root, str)
            or not configured_records_root.strip()
        ):
            raise RuntimeError(
                "storage_v2.records_root must be configured when transcription analytics is enabled"
            )
        configured_db_path = storage_config.get(
            "db_path",
            "state/storage-v2.sqlite3",
        )
        return {
            "enabled": True,
            "db_path": resolve_config_path(
                str(configured_db_path),
                base_dir=self.repo_root,
                env=self._runtime_env(),
            ),
            "records_root": resolve_config_path(
                configured_records_root,
                base_dir=self.repo_root,
                env=self._runtime_env(),
            ),
        }

    def transcription_analytics(
        self,
        *,
        period: str = "week",
    ) -> dict[str, Any]:
        settings = self._transcription_analytics_settings()
        if not settings["enabled"]:
            return disabled_transcription_analytics(period)
        return read_transcription_analytics(
            settings["db_path"],
            settings["records_root"],
            period=period,
        )

    def transcription_analytics_prometheus(self, *, period: str = "week") -> str:
        payload = self.transcription_analytics(period=period)
        period_label = str(payload.get("period", period))
        available_label = (
            "true"
            if payload.get("available") is True
            else "false"
        )
        lines: list[str] = []
        emitted_families: set[str] = set()

        def push_metric(
            name: str,
            doc: str,
            value: float | int | None,
            labels: dict[str, str],
            metric_type: str = "gauge",
        ) -> None:
            if value is None:
                return
            safe_value = float(value)
            if not math.isfinite(safe_value):
                return
            if name not in emitted_families:
                lines.append(f"# HELP {name} {doc}")
                lines.append(f"# TYPE {name} {metric_type}")
                emitted_families.add(name)
            rendered_value = (
                str(int(safe_value))
                if safe_value.is_integer()
                else format(safe_value, ".15g")
            )
            lines.append(f"{name}{_prometheus_labels(labels)} {rendered_value}")

        base_labels = {
            "period": period_label,
            "available": available_label,
        }

        coverage = payload.get("coverage", {})
        totals = payload.get("totals", {})
        status_distribution = payload.get("status_distribution", [])
        quality_distribution = payload.get("quality_distribution", [])
        classification_distribution = payload.get("classification_distribution", [])
        freshness = payload.get("freshness", {})
        window = payload.get("window", {})

        for row in status_distribution:
            status = str(row.get("status", "unknown"))
            count = row.get("count", 0)
            push_metric(
                "lecture_stt_transcriptions_jobs_in_window",
                "분석 윈도우의 상태별 전사 작업 수",
                int(count),
                {**base_labels, "status": status},
            )

        for row in quality_distribution:
            band = str(row.get("band", "unscored"))
            count = row.get("count", 0)
            push_metric(
                "lecture_stt_transcriptions_quality_band_jobs",
                "분석 윈도우의 품질 점수 구간별 전사 작업 수",
                int(count),
                {**base_labels, "band": band},
            )

        for row in classification_distribution:
            context_type = row.get("context_type")
            source = row.get("source")
            count = row.get("count", 0)
            push_metric(
                "lecture_stt_transcriptions_classification_jobs",
                "분석 윈도우의 분류별 전사 작업 수",
                int(count),
                {
                    **base_labels,
                    "context_type": (
                        "미분류"
                        if context_type is None
                        else str(context_type)
                    ),
                    "source": "미분류" if source is None else str(source),
                },
            )

        push_metric(
            "lecture_stt_transcriptions_average_quality_score",
            "전사 품질 점수 평균",
            None
            if totals.get("average_quality_score") is None
            else float(totals["average_quality_score"]),
            base_labels,
        )
        push_metric(
            "lecture_stt_transcriptions_audio_duration_seconds",
            "전사 오디오 시간 총합(초)",
            None if totals.get("audio_duration_sec") is None else float(totals["audio_duration_sec"]),
            base_labels,
        )
        push_metric(
            "lecture_stt_transcriptions_processing_seconds",
            "전사 처리 시간 총합(초)",
            None
            if totals.get("total_processing_sec") is None
            else float(totals["total_processing_sec"]),
            base_labels,
        )

        push_metric(
            "lecture_stt_transcriptions_jobs_in_window_overall",
            "분석 윈도우의 전체 전사 작업 수",
            int(totals.get("jobs", 0)),
            base_labels,
            metric_type="gauge",
        )

        push_metric(
            "lecture_stt_transcriptions_quality_missing_jobs",
            "분석 윈도우의 품질 점수 누락 작업 수",
            int(coverage.get("quality_missing", 0)),
            base_labels,
        )
        push_metric(
            "lecture_stt_transcriptions_quality_invalid_jobs",
            "분석 윈도우의 품질 점수 무효 작업 수",
            int(coverage.get("quality_invalid", 0)),
            base_labels,
        )
        push_metric(
            "lecture_stt_transcriptions_quality_scored_jobs",
            "분석 윈도우의 품질 점수 보유 작업 수",
            int(coverage.get("quality_scored", 0)),
            base_labels,
        )

        generated_at = freshness.get("generated_at")
        latest_event_at = freshness.get("latest_event_at")
        if isinstance(generated_at, str):
            try:
                generated_unix = datetime.fromisoformat(generated_at).timestamp()
                push_metric(
                    "lecture_stt_transcriptions_generated_epoch_seconds",
                    "분석 데이터 생성 시각(UNIX epoch)",
                    float(generated_unix),
                    base_labels,
                    metric_type="gauge",
                )
            except ValueError:
                pass
        if isinstance(latest_event_at, str):
            try:
                latest_event_unix = datetime.fromisoformat(latest_event_at).timestamp()
                push_metric(
                    "lecture_stt_transcriptions_latest_event_epoch_seconds",
                    "최신 이벤트 시각(UNIX epoch)",
                    float(latest_event_unix),
                    base_labels,
                    metric_type="gauge",
                )
            except ValueError:
                pass

        start_at = window.get("start_at")
        end_at = window.get("end_at")
        if isinstance(start_at, str):
            try:
                start_unix = datetime.fromisoformat(start_at).timestamp()
                push_metric(
                    "lecture_stt_transcriptions_window_start_epoch_seconds",
                    "분석 윈도우 시작 시각(UNIX epoch)",
                    float(start_unix),
                    base_labels,
                    metric_type="gauge",
                )
            except ValueError:
                pass
        if isinstance(end_at, str):
            try:
                end_unix = datetime.fromisoformat(end_at).timestamp()
                push_metric(
                    "lecture_stt_transcriptions_window_end_epoch_seconds",
                    "분석 윈도우 종료 시각(UNIX epoch)",
                    float(end_unix),
                    base_labels,
                    metric_type="gauge",
                )
            except ValueError:
                pass

        return "\n".join(lines) + "\n"

    def _recording_library_settings(self) -> dict[str, Any]:
        storage_config = self.config_data.get("storage_v2")
        if not isinstance(storage_config, dict):
            storage_config = {}
        library_config = storage_config.get("library")
        if not isinstance(library_config, dict):
            library_config = {}
        configured_db_path = storage_config.get(
            "db_path",
            "state/storage-v2.sqlite3",
        )
        return {
            "enabled": library_config.get("enabled") is True,
            "db_path": resolve_config_path(
                str(configured_db_path),
                base_dir=self.repo_root,
                env=self._runtime_env(),
            ),
        }

    def recording_library_list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        settings = self._recording_library_settings()
        if not settings["enabled"]:
            return disabled_recording_library_list(
                limit=limit,
                offset=offset,
            )
        return list_recordings(
            settings["db_path"],
            limit=limit,
            offset=offset,
        )

    def recording_library_detail(
        self,
        storage_key: str,
    ) -> dict[str, Any]:
        settings = self._recording_library_settings()
        if not settings["enabled"]:
            raise RecordingLibraryDisabledError(
                "Recording library API is disabled"
            )
        return read_recording_detail(
            settings["db_path"],
            storage_key,
        )

    def unified_review_feed(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        archive_settings = self._archive_review_settings()
        timetable_settings = self._timetable_settings()
        title_settings = self._title_review_settings()
        recording_settings = self._recording_library_settings()
        return read_unified_review_feed(
            archive_settings["db_path"],
            archive_enabled=bool(archive_settings["enabled"]),
            timetable_enabled=bool(timetable_settings["enabled"]),
            title_enabled=bool(title_settings["enabled"]),
            recording_enabled=bool(recording_settings["enabled"]),
            limit=limit,
            offset=offset,
        )

    def _title_review_settings(self) -> dict[str, Any]:
        storage_config = self.config_data.get("storage_v2")
        if not isinstance(storage_config, dict):
            storage_config = {}
        title_config = storage_config.get("title_review")
        if not isinstance(title_config, dict):
            title_config = {}
        configured_db_path = storage_config.get(
            "db_path",
            "state/storage-v2.sqlite3",
        )
        configured_records_root = storage_config.get("records_root")
        enabled = title_config.get("enabled") is True
        return {
            "enabled": enabled,
            "confirmations_enabled": (
                title_config.get("confirmations_enabled") is True
            ),
            "status_writes_enabled": (
                title_config.get("status_writes_enabled") is True
            ),
            "db_path": (
                resolve_config_path(
                    str(configured_db_path),
                    base_dir=self.repo_root,
                    env=self._runtime_env(),
                )
                if enabled
                else None
            ),
            "records_root_value": configured_records_root,
        }

    @staticmethod
    def _disabled_title_review_list(
        *,
        status: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": "storage-v2/title-suggestion-list@1",
            "available": False,
            "disabled_reason": "title_review_disabled",
            "counts": {
                "suggested": 0,
                "confirmed": 0,
                "rejected": 0,
            },
            "total": 0,
            "proposals": [],
            "capabilities": {
                "confirmations_enabled": False,
                "status_writes_enabled": False,
            },
        }

    def _require_title_review_records_root(
        self,
        settings: dict[str, Any],
    ) -> Path:
        records_root_value = settings.get("records_root_value")
        if records_root_value is None:
            raise ValueError("Title review requires storage_v2.records_root")
        if not isinstance(records_root_value, str):
            records_root_value = str(records_root_value)
        return resolve_config_path(
            records_root_value,
            base_dir=self.repo_root,
            env=self._runtime_env(),
        )

    def title_review_list(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        settings = self._title_review_settings()
        if not settings["enabled"]:
            return self._disabled_title_review_list(
                status=status,
                limit=limit,
                offset=offset,
            )
        payload = list_title_suggestions(
            settings["db_path"],
            status=status,
            limit=limit,
            offset=offset,
        )
        if not payload.get("available", False):
            payload["disabled_reason"] = "storage_v2_db_unavailable"
            payload["capabilities"] = {
                "confirmations_enabled": False,
                "status_writes_enabled": False,
            }
            return payload
        payload["capabilities"] = {
            "confirmations_enabled": settings["confirmations_enabled"],
            "status_writes_enabled": settings["status_writes_enabled"],
        }
        return payload

    def title_review_detail(
        self,
        proposal_id: int,
    ) -> dict[str, Any]:
        settings = self._title_review_settings()
        if not settings["enabled"]:
            raise TitleSuggestionWriteDisabledError(
                "Title review API is disabled"
            )
        payload = read_title_suggestion(
            settings["db_path"],
            proposal_id,
        )
        payload["capabilities"] = {
            "confirmations_enabled": settings["confirmations_enabled"],
            "status_writes_enabled": settings["status_writes_enabled"],
        }
        return payload

    def title_review_status_update(
        self,
        proposal_id: int,
        *,
        status: str,
        allow_write: bool = False,
    ) -> dict[str, Any]:
        settings = self._title_review_settings()
        if not settings["enabled"]:
            raise TitleSuggestionWriteDisabledError(
                "Title review API is disabled"
            )
        normalized_status = str(status or "").strip().lower()
        if normalized_status != "rejected":
            raise ValueError("status must be rejected")
        return reject_title_suggestion(
            settings["db_path"],
            proposal_id,
            status_writes_enabled=bool(
                settings["status_writes_enabled"]
            ),
            allow_write=allow_write,
        )

    def title_review_confirmation_plan(
        self,
        proposal_id: int,
    ) -> dict[str, Any]:
        settings = self._title_review_settings()
        if not settings["enabled"]:
            raise TitleSuggestionWriteDisabledError(
                "Title review API is disabled"
            )
        return plan_title_suggestion_confirmation(
            settings["db_path"],
            self._require_title_review_records_root(settings),
            proposal_id,
        )

    def title_review_confirmation_apply(
        self,
        proposal_id: int,
        *,
        expected_count: int,
        expected_plan_sha256: str,
        allow_write: bool = False,
    ) -> dict[str, Any]:
        settings = self._title_review_settings()
        if not settings["enabled"]:
            raise TitleSuggestionWriteDisabledError(
                "Title review API is disabled"
            )
        return apply_title_suggestion_confirmation(
            settings["db_path"],
            self._require_title_review_records_root(settings),
            proposal_id,
            expected_count=expected_count,
            expected_plan_sha256=expected_plan_sha256,
            confirmations_enabled=bool(
                settings["confirmations_enabled"]
            ),
            allow_write=allow_write,
        )

    def _archive_review_settings(self) -> dict[str, Any]:
        storage_config = self.config_data.get("storage_v2")
        if not isinstance(storage_config, dict):
            storage_config = {}
        review_config = storage_config.get("archive_review")
        if not isinstance(review_config, dict):
            review_config = {}
        configured_db_path = storage_config.get(
            "db_path",
            "state/storage-v2.sqlite3",
        )
        return {
            # Only a YAML boolean true enables these gates. Strings such as
            # "true" or "false" remain fail-closed.
            "enabled": review_config.get("enabled") is True,
            "status_writes_enabled": (
                review_config.get("status_writes_enabled") is True
            ),
            "promotions_enabled": (
                review_config.get("promotions_enabled") is True
            ),
            "db_path": resolve_config_path(
                str(configured_db_path),
                base_dir=self.repo_root,
                env=self._runtime_env(),
            ),
        }

    @staticmethod
    def _disabled_archive_review_list(
        *,
        review_status: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": "storage-v2/archive-review-list@1",
            "available": False,
            "disabled_reason": "archive_review_disabled",
            "filters": {
                "review_status": review_status,
                "limit": limit,
                "offset": offset,
            },
            "counts": {
                "open": 0,
                "triaged": 0,
                "resolved": 0,
                "dismissed": 0,
            },
            "total": 0,
            "cases": [],
            "capabilities": {
                "status_writes_enabled": False,
                "promotions_enabled": False,
            },
        }

    def archive_review_list(
        self,
        *,
        review_status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        settings = self._archive_review_settings()
        if not settings["enabled"]:
            return self._disabled_archive_review_list(
                review_status=review_status,
                limit=limit,
                offset=offset,
            )
        payload = list_archive_review_cases(
            settings["db_path"],
            review_status=review_status,
            limit=limit,
            offset=offset,
        )
        payload["capabilities"] = {
            "status_writes_enabled": settings["status_writes_enabled"],
            "promotions_enabled": settings["promotions_enabled"],
        }
        return payload

    def archive_review_detail(self, case_key: str) -> dict[str, Any]:
        settings = self._archive_review_settings()
        if not settings["enabled"]:
            raise ArchiveReviewWriteDisabledError(
                "Archive evidence review API is disabled"
            )
        payload = read_archive_review_case(settings["db_path"], case_key)
        payload["capabilities"] = {
            "status_writes_enabled": settings["status_writes_enabled"],
            "promotions_enabled": settings["promotions_enabled"],
        }
        return payload

    def archive_review_update_status(
        self,
        case_key: str,
        review_status: str,
        *,
        allow_write: bool = False,
    ) -> dict[str, Any]:
        settings = self._archive_review_settings()
        if not settings["enabled"]:
            raise ArchiveReviewWriteDisabledError(
                "Archive evidence review API is disabled"
            )
        return update_archive_review_status(
            settings["db_path"],
            case_key,
            review_status,
            allow_write=bool(
                settings["status_writes_enabled"] and allow_write
            ),
        )

    def archive_review_plan_promotion(
        self,
        case_key: str,
        *,
        target_storage_key: str,
        selected_revisions: dict[str, int],
    ) -> dict[str, Any]:
        settings = self._archive_review_settings()
        if not settings["enabled"]:
            raise ArchiveReviewWriteDisabledError(
                "Archive evidence review API is disabled"
            )
        return plan_archive_review_promotion(
            settings["db_path"],
            case_key,
            target_storage_key=target_storage_key,
            selected_revisions=selected_revisions,
        )

    def archive_review_apply_promotion(
        self,
        case_key: str,
        *,
        target_storage_key: str,
        selected_revisions: dict[str, int],
        expected_count: int,
        expected_plan_sha256: str,
        allow_write: bool = False,
    ) -> dict[str, Any]:
        settings = self._archive_review_settings()
        if not settings["enabled"]:
            raise ArchiveReviewWriteDisabledError(
                "Archive evidence review API is disabled"
            )
        return apply_archive_review_promotion(
            settings["db_path"],
            case_key,
            target_storage_key=target_storage_key,
            selected_revisions=selected_revisions,
            expected_count=expected_count,
            expected_plan_sha256=expected_plan_sha256,
            promotions_enabled=bool(settings["promotions_enabled"]),
            allow_write=allow_write,
        )

    def _timetable_settings(self) -> dict[str, Any]:
        storage_config = self.config_data.get("storage_v2")
        if not isinstance(storage_config, dict):
            storage_config = {}
        timetable_config = storage_config.get("timetable")
        if not isinstance(timetable_config, dict):
            timetable_config = {}
        configured_db_path = storage_config.get(
            "db_path",
            "state/storage-v2.sqlite3",
        )
        return {
            "enabled": timetable_config.get("enabled") is True,
            "confirmations_enabled": (
                timetable_config.get("confirmations_enabled") is True
            ),
            "status_writes_enabled": (
                timetable_config.get("status_writes_enabled") is True
            ),
            "db_path": resolve_config_path(
                str(configured_db_path),
                base_dir=self.repo_root,
                env=self._runtime_env(),
            ),
        }

    def timetable_list(
        self,
        *,
        semester: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        settings = self._timetable_settings()
        if not settings["enabled"]:
            return {
                "schema_version": "storage-v2/timetable-list@1",
                "available": False,
                "disabled_reason": "timetable_api_disabled",
                "semester": semester,
                "total": 0,
                "entries": [],
                "capabilities": {
                    "confirmations_enabled": False,
                    "status_writes_enabled": False,
                },
            }
        payload = list_timetable_entries(
            settings["db_path"],
            semester=semester,
            limit=limit,
            offset=offset,
        )
        payload["capabilities"] = {
            "confirmations_enabled": settings["confirmations_enabled"],
            "status_writes_enabled": settings["status_writes_enabled"],
        }
        return payload

    def timetable_classification_list(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        settings = self._timetable_settings()
        if not settings["enabled"]:
            return {
                "schema_version": "storage-v2/classification-proposal-list@1",
                "available": False,
                "disabled_reason": "timetable_api_disabled",
                "counts": {
                    "suggested": 0,
                    "confirmed": 0,
                    "rejected": 0,
                },
                "total": 0,
                "proposals": [],
                "capabilities": {
                    "confirmations_enabled": False,
                    "status_writes_enabled": False,
                },
            }
        payload = list_classification_proposals(
            settings["db_path"],
            status=status,
            limit=limit,
            offset=offset,
        )
        payload["capabilities"] = {
            "confirmations_enabled": settings["confirmations_enabled"],
            "status_writes_enabled": settings["status_writes_enabled"],
        }
        return payload

    def timetable_classification_detail(
        self,
        proposal_id: int,
    ) -> dict[str, Any]:
        settings = self._timetable_settings()
        if not settings["enabled"]:
            raise TimetableWriteDisabledError(
                "Timetable review API is disabled"
            )
        payload = read_classification_proposal(
            settings["db_path"],
            proposal_id,
        )
        payload["capabilities"] = {
            "confirmations_enabled": settings["confirmations_enabled"],
            "status_writes_enabled": settings["status_writes_enabled"],
        }
        return payload

    def timetable_classification_status_update(
        self,
        proposal_id: int,
        *,
        status: str,
        allow_write: bool = False,
    ) -> dict[str, Any]:
        settings = self._timetable_settings()
        if not settings["enabled"]:
            raise TimetableWriteDisabledError(
                "Timetable review API is disabled"
            )
        return update_classification_status(
            settings["db_path"],
            proposal_id,
            status=status,
            status_writes_enabled=bool(
                settings["status_writes_enabled"]
            ),
            allow_write=allow_write,
        )

    def timetable_confirmation_plan(
        self,
        proposal_id: int,
    ) -> dict[str, Any]:
        settings = self._timetable_settings()
        if not settings["enabled"]:
            raise TimetableWriteDisabledError(
                "Timetable review API is disabled"
            )
        return plan_classification_confirmation(
            settings["db_path"],
            proposal_id,
        )

    def timetable_confirmation_apply(
        self,
        proposal_id: int,
        *,
        expected_count: int,
        expected_plan_sha256: str,
        allow_write: bool = False,
    ) -> dict[str, Any]:
        settings = self._timetable_settings()
        if not settings["enabled"]:
            raise TimetableWriteDisabledError(
                "Timetable review API is disabled"
            )
        return apply_classification_confirmation(
            settings["db_path"],
            proposal_id,
            expected_count=expected_count,
            expected_plan_sha256=expected_plan_sha256,
            confirmations_enabled=bool(settings["confirmations_enabled"]),
            allow_write=allow_write,
        )

    def _worker_cmd(self, *extra: str) -> list[str]:
        cmd = [str(self.python_bin), "-m", self.worker_module]
        cmd.extend(extra)
        return cmd

    def _app_config(self) -> dict[str, Any]:
        config_data = self.config_data if isinstance(getattr(self, "config_data", None), dict) else {}
        app_cfg = config_data.get("app")
        return app_cfg if isinstance(app_cfg, dict) else {}

    def _execution_owner(self) -> str:
        owner = str(self._app_config().get("execution_owner", "python") or "python").strip().lower()
        return "controller" if owner == "controller" else "python"

    def _is_controller_owner(self) -> bool:
        return self._execution_owner() == "controller"

    def _controller_label(self) -> str:
        raw = str(self._app_config().get("controller_label") or DEFAULT_CONTROLLER_LABEL).strip()
        if not raw or not CONTROLLER_LABEL_RE.fullmatch(raw):
            raise RuntimeError("controller label 구성이 올바르지 않습니다.")
        return raw

    def _controller_runtime(self) -> str:
        raw = str(self._app_config().get("controller_runtime", "launchd") or "launchd").strip().lower()
        if raw not in {"launchd", "console"}:
            raise RuntimeError("controller runtime 구성이 올바르지 않습니다.")
        return raw

    def _is_console_controller(self) -> bool:
        return self._is_controller_owner() and self._controller_runtime() == "console"

    @staticmethod
    def _console_runtime_supported() -> bool:
        return sys.platform == "darwin"

    @staticmethod
    def _configured_absolute_path(value: Any, *, label: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"{label} 구성이 필요합니다.")
        expanded = Path(value).expanduser()
        if not expanded.is_absolute():
            raise RuntimeError(f"{label}는 절대경로여야 합니다.")
        return Path(os.path.abspath(os.fspath(expanded)))

    def _console_controller_settings(self) -> dict[str, Path]:
        if not self._console_runtime_supported():
            raise RuntimeError("controller console runtime은 Darwin에서만 지원합니다.")
        app = self._app_config()
        expected_sha256 = str(app.get("controller_binary_sha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise RuntimeError("controller binary SHA-256 구성이 올바르지 않습니다.")
        binary = self._configured_absolute_path(
            app.get("controller_binary"),
            label="controller binary",
        )
        state_dir = self._configured_absolute_path(
            app.get("controller_state_dir"),
            label="controller state directory",
        )
        pid_path = self._configured_absolute_path(
            app.get("controller_pid_path"),
            label="controller PID path",
        )
        stdout_path = self._configured_absolute_path(
            app.get(
                "controller_stdout_path",
                "~/Library/Logs/lecture_stt/controller.out.jsonl",
            ),
            label="controller stdout",
        )
        stderr_path = self._configured_absolute_path(
            app.get(
                "controller_stderr_path",
                "~/Library/Logs/lecture_stt/controller.err.log",
            ),
            label="controller stderr",
        )
        self._validate_console_binary(binary, expected_sha256)
        self._validate_console_state_dir(state_dir)
        self._validate_console_state_dir(pid_path.parent)
        if pid_path.parent == state_dir:
            raise RuntimeError(
                "controller PID path는 recovery state directory 밖에 있어야 합니다."
            )
        return {
            "binary": binary,
            "state_dir": state_dir,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "pid_path": pid_path,
        }

    @staticmethod
    def _validate_console_binary(path: Path, expected_sha256: str) -> None:
        if path.parent.name != expected_sha256:
            raise RuntimeError("controller binary version directory가 올바르지 않습니다.")
        for directory in (path.parent, path.parent.parent):
            try:
                directory_state = directory.lstat()
            except OSError as exc:
                raise RuntimeError(
                    "controller binary version directory를 안전하게 확인할 수 없습니다."
                ) from exc
            if (
                not stat.S_ISDIR(directory_state.st_mode)
                or stat.S_ISLNK(directory_state.st_mode)
                or directory_state.st_uid != os.geteuid()
                or stat.S_IMODE(directory_state.st_mode) != 0o500
                or not ControlState._console_binary_path_is_immutable(
                    directory_state
                )
            ):
                raise RuntimeError(
                    "controller binary version directory 상태가 올바르지 않습니다."
                )
        try:
            observed = path.lstat()
        except OSError as exc:
            raise RuntimeError("controller binary를 안전하게 확인할 수 없습니다.") from exc
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_ISLNK(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != os.geteuid()
            or stat.S_IMODE(observed.st_mode) != 0o500
            or not ControlState._console_binary_path_is_immutable(observed)
        ):
            raise RuntimeError("controller binary 상태가 올바르지 않습니다.")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise RuntimeError("controller binary를 안전하게 열 수 없습니다.") from exc
        try:
            opened = os.fstat(fd)
            if (
                opened.st_dev != observed.st_dev
                or opened.st_ino != observed.st_ino
                or opened.st_nlink != 1
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != 0o500
                or not ControlState._console_binary_path_is_immutable(opened)
            ):
                raise RuntimeError("controller binary identity가 변경되었습니다.")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        finally:
            os.close(fd)
        if digest.hexdigest() != expected_sha256:
            raise RuntimeError("controller binary SHA-256이 일치하지 않습니다.")

    @staticmethod
    def _console_binary_path_is_immutable(observed: os.stat_result) -> bool:
        if sys.platform != "darwin":
            return True
        immutable_flag = getattr(stat, "UF_IMMUTABLE", 0)
        return bool(immutable_flag and observed.st_flags & immutable_flag)

    @staticmethod
    def _validate_console_state_dir(path: Path) -> None:
        try:
            observed = path.lstat()
        except OSError as exc:
            raise RuntimeError("controller state directory를 안전하게 확인할 수 없습니다.") from exc
        if (
            not stat.S_ISDIR(observed.st_mode)
            or stat.S_ISLNK(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or stat.S_IMODE(observed.st_mode) != 0o700
        ):
            raise RuntimeError("controller state directory 상태가 올바르지 않습니다.")

    def _console_controller_cmd(self, settings: dict[str, Path]) -> list[str]:
        return [
            str(settings["binary"]),
            "--python-bin",
            str(self.python_bin),
            "--repo-root",
            str(self.repo_root),
            "--config",
            str(self.config_path),
            "--kill-switch",
            str(self._controller_kill_switch_path()),
            "--state-dir",
            str(settings["state_dir"]),
            "--cycle-interval-sec",
            "10",
            "--max-fresh-jobs",
            "1",
            "--enable-execution",
            "--allow-write",
        ]

    @staticmethod
    def _secure_pid_file_fd(path: Path, *, create: bool) -> int:
        flags = os.O_RDWR
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        observed = os.fstat(fd)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != os.geteuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            os.close(fd)
            raise RuntimeError("controller PID file 상태가 올바르지 않습니다.")
        return fd

    def _read_console_controller_identity(
        self,
        settings: dict[str, Path],
    ) -> tuple[int, tuple[int, int]] | None:
        path = settings["pid_path"]
        try:
            fd = self._secure_pid_file_fd(path, create=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RuntimeError("controller PID file을 안전하게 확인할 수 없습니다.") from exc
        try:
            payload = os.read(fd, 512)
            if os.read(fd, 1):
                raise RuntimeError("controller PID file 내용이 올바르지 않습니다.")
        finally:
            os.close(fd)
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("controller PID file 내용이 올바르지 않습니다.") from exc
        if (
            type(decoded) is not dict
            or set(decoded) != {"schema_version", "pid", "process_birth"}
            or decoded.get("schema_version") != CONTROLLER_PID_SCHEMA
            or type(decoded.get("pid")) is not int
            or decoded["pid"] <= 1
            or type(decoded.get("process_birth")) is not dict
            or set(decoded["process_birth"]) != {"tv_sec", "tv_usec"}
            or type(decoded["process_birth"].get("tv_sec")) is not int
            or decoded["process_birth"]["tv_sec"] <= 0
            or type(decoded["process_birth"].get("tv_usec")) is not int
            or not 0 <= decoded["process_birth"]["tv_usec"] < 1_000_000
        ):
            raise RuntimeError("controller PID file 내용이 올바르지 않습니다.")
        return decoded["pid"], (
            decoded["process_birth"]["tv_sec"],
            decoded["process_birth"]["tv_usec"],
        )

    @staticmethod
    def _console_controller_process_identity(
        pid: int,
    ) -> tuple[int, int, int] | None:
        if sys.platform != "darwin":
            raise RuntimeError(
                "controller process kernel identity는 Darwin에서만 지원합니다."
            )
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        except OSError as exc:
            raise RuntimeError(
                "controller process kernel identity를 확인할 수 없습니다."
            ) from exc
        libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        libproc.proc_pidinfo.restype = ctypes.c_int
        info = _DarwinProcBsdInfo()
        result = libproc.proc_pidinfo(
            pid,
            3,  # PROC_PIDTBSDINFO
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if result != ctypes.sizeof(info) or info.pbi_pid != pid:
            return None
        if (
            info.pbi_start_tvsec <= 0
            or info.pbi_start_tvusec >= 1_000_000
            or info.pbi_pgid <= 1
        ):
            return None
        return (
            int(info.pbi_start_tvsec),
            int(info.pbi_start_tvusec),
            int(info.pbi_pgid),
        )

    def _console_controller_process_matches(
        self,
        pid: int,
        process_birth: tuple[int, int],
        settings: dict[str, Path],
    ) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            raise RuntimeError("controller process identity를 확인할 수 없습니다.") from exc
        kernel_identity = self._console_controller_process_identity(pid)
        if (
            kernel_identity is None
            or kernel_identity[:2] != process_birth
            or kernel_identity[2] != pid
        ):
            return False
        result = self._run_control_cmd(["/bin/ps", "-p", str(pid), "-o", "comm="])
        if result.returncode != 0:
            return False
        if (result.stdout or "").strip() != str(settings["binary"]):
            return False
        command_result = self._run_control_cmd(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="]
        )
        if command_result.returncode != 0:
            return False
        return (command_result.stdout or "").strip() == " ".join(
            self._console_controller_cmd(settings)
        )

    def _console_controller_pid(self, settings: dict[str, Path] | None = None) -> int | None:
        resolved = self._console_controller_settings() if settings is None else settings
        identity = self._read_console_controller_identity(resolved)
        if identity is None:
            return None
        pid, process_birth = identity
        return (
            pid
            if self._console_controller_process_matches(
                pid,
                process_birth,
                resolved,
            )
            else None
        )

    def _remove_stale_console_pid_file(self, settings: dict[str, Path]) -> None:
        identity = self._read_console_controller_identity(settings)
        if identity is None:
            return
        pid, process_birth = identity
        if self._console_controller_process_matches(
            pid,
            process_birth,
            settings,
        ):
            raise RuntimeError("controller가 이미 실행 중입니다.")
        try:
            settings["pid_path"].unlink()
            self._fsync_directory(settings["pid_path"].parent)
        except OSError as exc:
            raise RuntimeError("stale controller PID file 정리에 실패했습니다.") from exc

    @staticmethod
    def _open_console_log(path: Path) -> int:
        try:
            parent = path.parent.lstat()
        except OSError as exc:
            raise RuntimeError("controller log directory를 확인할 수 없습니다.") from exc
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            raise RuntimeError("controller log directory 상태가 올바르지 않습니다.")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise RuntimeError("controller log file을 열 수 없습니다.") from exc
        observed = os.fstat(fd)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != os.geteuid()
        ):
            os.close(fd)
            raise RuntimeError("controller log file 상태가 올바르지 않습니다.")
        os.fchmod(fd, 0o600)
        return fd

    def _controller_service_target(self) -> str:
        return f"gui/{os.getuid()}/{self._controller_label()}"

    def _controller_kill_switch_path(self) -> Path:
        configured = self._app_config().get("controller_kill_switch")
        if configured is None or not str(configured).strip():
            raise RuntimeError("controller kill-switch가 설정되지 않았습니다.")
        return Path(os.path.abspath(os.fspath(Path(str(configured)).expanduser())))

    def _run_control_cmd(
        self,
        cmd: list[str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=self.SUBPROCESS_TIMEOUT_SEC if timeout is None else timeout,
            cwd=str(self.repo_root),
        )

    def _run_main_cmd(self, *extra: str) -> str:
        cmd = self._worker_cmd(*extra, "--config", str(self.config_path))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=package_env(),
            cwd=str(self.repo_root),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise RuntimeError(detail)
        return (result.stdout or "").strip()

    def _find_worker_pids(self) -> list[int]:
        # 관리 상태 확인을 위해 STT 워커 모듈 PID를 찾는다.
        try:
            result = self._run_control_cmd(
                ["pgrep", "-f", self.worker_module],
            )
        except Exception:
            return []

        pids: list[int] = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pid = int(line)
            except ValueError:
                continue
            if pid != os.getpid():
                pids.append(pid)
        return sorted(set(pids))

    def _is_managed_running(self) -> bool:
        return self.worker_proc is not None and self.worker_proc.poll() is None

    def _is_paused(self) -> bool:
        return self.pause_flag.exists()

    def _controller_kill_switch_state(self) -> str:
        path = self._controller_kill_switch_path()
        try:
            observed = path.lstat()
        except FileNotFoundError:
            return "absent"
        except OSError as exc:
            raise RuntimeError("controller kill-switch marker를 안전하게 확인할 수 없습니다.") from exc
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_ISLNK(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != os.geteuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            return "invalid"
        return "active"

    def _controller_kill_switch_active(self) -> bool:
        return self._controller_kill_switch_state() == "active"

    def _fsync_directory(self, directory: Path) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        fd = os.open(directory, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _remove_controller_kill_switch_marker(self) -> None:
        path = self._controller_kill_switch_path()
        state = self._controller_kill_switch_state()
        if state == "absent":
            return
        if state != "active":
            raise RuntimeError("controller kill-switch marker 상태가 올바르지 않습니다.")
        try:
            path.unlink()
            self._fsync_directory(path.parent)
        except OSError as exc:
            raise RuntimeError("controller kill-switch marker 제거에 실패했습니다.") from exc

    def _write_controller_kill_switch_marker(self) -> None:
        path = self._controller_kill_switch_path()
        try:
            parent = path.parent.lstat()
        except OSError as exc:
            raise RuntimeError(
                "controller kill-switch 디렉터리를 안전하게 확인할 수 없습니다."
            ) from exc
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            raise RuntimeError(
                "controller kill-switch 디렉터리 상태가 올바르지 않습니다."
            )
        state = self._controller_kill_switch_state()
        if state == "invalid":
            raise RuntimeError("controller kill-switch marker 상태가 올바르지 않습니다.")
        if state == "active":
            return
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags, 0o600)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            observed = path.lstat()
            if not stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode) or observed.st_nlink != 1:
                raise RuntimeError("controller kill-switch marker 상태가 올바르지 않습니다.")
            self._fsync_directory(path.parent)
        except FileExistsError:
            if self._controller_kill_switch_state() != "active":
                raise RuntimeError(
                    "controller kill-switch marker 상태가 올바르지 않습니다."
                )
        except RuntimeError:
            raise
        except OSError as exc:
            raise RuntimeError("controller kill-switch marker 생성에 실패했습니다.") from exc

    def _controller_service_loaded(self) -> bool:
        try:
            result = self._run_control_cmd(["launchctl", "print", self._controller_service_target()])
        except Exception:
            return False
        return result.returncode == 0

    def _run_launchctl(self, *args: str) -> None:
        result = self._run_control_cmd(["launchctl", *args])
        if result.returncode != 0:
            raise RuntimeError("controller 서비스 제어 명령이 실패했습니다.")

    def _cleanup_console_pid_file(self, settings: dict[str, Path]) -> None:
        path = settings["pid_path"]
        try:
            if path.exists():
                fd = self._secure_pid_file_fd(path, create=False)
                os.close(fd)
                path.unlink()
                self._fsync_directory(settings["pid_path"].parent)
        except OSError as exc:
            raise RuntimeError("controller PID file 정리에 실패했습니다.") from exc

    def _start_console_controller(self) -> None:
        settings = self._console_controller_settings()
        existing = self._console_controller_pid(settings)
        if existing is not None:
            self._remove_controller_kill_switch_marker()
            self._set_paused(False)
            self.notice = "controller 실행을 재개했습니다."
            return

        self._remove_stale_console_pid_file(settings)
        self._write_controller_kill_switch_marker()
        pid_fd: int | None = None
        stdout_fd: int | None = None
        stderr_fd: int | None = None
        proc: subprocess.Popen[bytes] | None = None
        try:
            pid_fd = self._secure_pid_file_fd(settings["pid_path"], create=True)
            stdout_fd = self._open_console_log(settings["stdout_path"])
            stderr_fd = self._open_console_log(settings["stderr_path"])
            env = package_env()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            self._validate_console_binary(
                settings["binary"],
                settings["binary"].parent.name,
            )
            proc = subprocess.Popen(
                self._console_controller_cmd(settings),
                cwd=str(self.repo_root),
                env=env,
                stdout=stdout_fd,
                stderr=stderr_fd,
                start_new_session=True,
            )
            kernel_identity = self._console_controller_process_identity(proc.pid)
            if kernel_identity is None or kernel_identity[2] != proc.pid:
                raise RuntimeError("controller process 시작 identity를 확인할 수 없습니다.")
            process_birth = kernel_identity[:2]
            pid_payload = (
                json.dumps(
                    {
                        "schema_version": CONTROLLER_PID_SCHEMA,
                        "pid": proc.pid,
                        "process_birth": {
                            "tv_sec": process_birth[0],
                            "tv_usec": process_birth[1],
                        },
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("ascii")
            if os.write(pid_fd, pid_payload) != len(pid_payload):
                raise RuntimeError("controller PID file 기록이 완료되지 않았습니다.")
            os.fsync(pid_fd)
            self._fsync_directory(settings["pid_path"].parent)
            time.sleep(0.05)
            if proc.poll() is not None:
                raise RuntimeError(
                    f"controller가 즉시 종료되었습니다. (코드={proc.returncode})"
                )
            if not self._console_controller_process_matches(
                proc.pid,
                process_birth,
                settings,
            ):
                raise RuntimeError("controller process 실행 identity가 올바르지 않습니다.")
            self._remove_controller_kill_switch_marker()
            self._set_paused(False)
            self.controller_proc = proc
            self.notice = "controller 실행을 시작했습니다."
        except Exception:
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except Exception:
                        pass
            try:
                self._write_controller_kill_switch_marker()
            except Exception:
                pass
            try:
                self._cleanup_console_pid_file(settings)
            except Exception:
                pass
            raise
        finally:
            for fd in (pid_fd, stdout_fd, stderr_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def _console_process_stopped(
        self,
        pid: int,
        process_birth: tuple[int, int],
        settings: dict[str, Path],
    ) -> bool:
        try:
            reaped, _ = os.waitpid(pid, os.WNOHANG)
            if reaped == pid:
                return True
        except ChildProcessError:
            pass
        return not self._console_controller_process_matches(
            pid,
            process_birth,
            settings,
        )

    def _stop_console_controller(self) -> None:
        settings = self._console_controller_settings()
        self._write_controller_kill_switch_marker()
        identity = self._read_console_controller_identity(settings)
        if identity is None:
            self.notice = "controller kill-switch가 적용되었습니다."
            return
        pid, process_birth = identity
        if not self._console_controller_process_matches(
            pid,
            process_birth,
            settings,
        ):
            self._cleanup_console_pid_file(settings)
            self.notice = "controller kill-switch가 적용되었습니다."
            return
        try:
            if os.getpgid(pid) != pid:
                raise RuntimeError("controller process group 상태가 올바르지 않습니다.")
            os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if self._console_process_stopped(
                    pid,
                    process_birth,
                    settings,
                ):
                    break
                time.sleep(0.1)
            else:
                if not self._console_controller_process_matches(
                    pid,
                    process_birth,
                    settings,
                ):
                    self._cleanup_console_pid_file(settings)
                    self.notice = "controller 중지 요청을 완료했습니다."
                    return
                os.killpg(pid, signal.SIGKILL)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if self._console_process_stopped(
                        pid,
                        process_birth,
                        settings,
                    ):
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("controller process group을 종료하지 못했습니다.")
            self._cleanup_console_pid_file(settings)
            if self.controller_proc is not None and self.controller_proc.pid == pid:
                try:
                    self.controller_proc.wait(timeout=0)
                except Exception:
                    pass
                self.controller_proc = None
            self.notice = "controller 중지 요청을 완료했습니다."
        except ProcessLookupError:
            self._cleanup_console_pid_file(settings)
            self.notice = "controller kill-switch가 적용되었습니다."

    def _controller_runtime_active(self, external_pids: list[int] | None = None) -> bool:
        if not self._is_controller_owner():
            return False
        if self._is_console_controller():
            return (
                self._console_controller_pid() is not None
                and not self._controller_kill_switch_active()
            )
        pids = self._find_worker_pids() if external_pids is None else external_pids
        if pids:
            return True
        if not self._controller_service_loaded():
            return False
        return not self._controller_kill_switch_active()

    def _has_active_runtime(
        self,
        *,
        managed_running: bool | None = None,
        external_pids: list[int] | None = None,
    ) -> bool:
        running = self._is_managed_running() if managed_running is None else managed_running
        pids = self._find_worker_pids() if external_pids is None else external_pids
        if self._is_controller_owner():
            return running or self._controller_runtime_active(pids)
        return running or bool(pids)

    def _set_poll_boost(self, seconds: float = 6.0) -> None:
        # 사용자 동작 직후에는 빠르게 반영되도록 폴링을 잠깐 촉진한다.
        self._poll_boost_until = max(self._poll_boost_until, time.time() + seconds)

    def _is_poll_boost_active(self) -> bool:
        return time.time() < self._poll_boost_until

    def _poll_interval_sec(self, has_processing: bool, is_busy: bool) -> float:
        if self._is_poll_boost_active():
            return self.FAST_POLL_INTERVAL_SEC
        if is_busy or has_processing:
            return self.NORMAL_POLL_INTERVAL_SEC
        return self.IDLE_POLL_INTERVAL_SEC

    @staticmethod
    def _format_eta(seconds: int | float | None) -> str:
        """ETA 초를 현재 시각 기준 완료 예정 시각(HH:MM:SS)으로 표시한다."""
        if seconds is None:
            return "-"
        try:
            total = int(seconds)
        except Exception:
            return "-"
        if total <= 0:
            return "곧 완료"

        from datetime import datetime, timedelta
        finish_time = datetime.now() + timedelta(seconds=total)
        return finish_time.strftime("%H:%M:%S")

    def _set_paused(self, paused: bool) -> None:
        # 일시정지 플래그는 파이프라인 제어의 단일 기준으로 사용한다.
        try:
            self.pause_flag.parent.mkdir(parents=True, exist_ok=True)
            if paused:
                self.pause_flag.touch(exist_ok=True)
            else:
                self.pause_flag.unlink(missing_ok=True)
        except Exception as exc:
            raise RuntimeError(f"일시정지 설정 처리 실패: {exc}") from exc

    def start(self) -> None:
        with self.lock:
            if self._is_controller_owner():
                try:
                    if self._is_console_controller():
                        self._start_console_controller()
                        self._notification_restart_required = False
                        self._set_poll_boost()
                        return
                    if not self._controller_service_loaded():
                        self.notice = "controller 서비스가 load되지 않아 시작할 수 없습니다."
                        return
                    self._remove_controller_kill_switch_marker()
                    self._set_paused(False)
                    self._run_launchctl("kickstart", "-k", self._controller_service_target())
                    self._notification_restart_required = False
                    self.notice = "controller 실행을 재개했습니다."
                    self._set_poll_boost()
                except Exception as exc:
                    self.notice = f"시작 실패: {exc}"
                return

            if self._is_managed_running():
                if self._is_paused():
                    try:
                        self._set_paused(False)
                    except Exception as exc:
                        self.notice = f"일시정지 해제 실패: {exc}"
                        return
                    self.notice = "일시정지 해제 후 파이프라인이 재개됩니다."
                else:
                    self.notice = "이미 실행 중입니다."
                self._set_poll_boost()
                return

            if self._find_worker_pids() and not self._is_managed_running():
                self.notice = "다른 경로에서 실행 중인 워커가 있어 시작할 수 없습니다."
                return

            try:
                self._set_paused(False)
            except Exception as exc:
                self.notice = f"시작 전 초기화 실패: {exc}"
                return
            cmd = self._worker_cmd("--config", str(self.config_path))
            try:
                self.worker_proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.repo_root),
                    env=package_env(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                if self.worker_proc.poll() is not None:
                    # 즉시 종료되면 실제로는 실행되지 않은 상태다.
                    code = self.worker_proc.returncode
                    self.notice = f"파이프라인 시작 실패: 워커가 즉시 종료되었습니다. (코드={code})"
                    self.worker_proc = None
                    return

                self._notification_restart_required = False
                self.notice = "파이프라인이 실행중입니다."
                self._set_poll_boost()
            except Exception as exc:
                self.notice = f"시작 실패: {exc}"

    def pause(self) -> None:
        with self.lock:
            if self._is_paused():
                self.notice = "이미 일시정지 상태입니다."
                return
            try:
                self._set_paused(True)
                self.notice = "파이프라인이 일시정지되었습니다."
                self._set_poll_boost()
            except Exception as exc:
                self.notice = f"일시정지 처리 실패: {exc}"

    def resume(self) -> None:
        with self.lock:
            if not self._is_paused():
                self.notice = "일시정지 상태가 아닙니다."
                return
            try:
                self._set_paused(False)
                if not self._is_managed_running() and not self._find_worker_pids():
                    self.notice = "재개 요청됨. 워커가 없으면 시작 버튼을 눌러 주세요."
                    self._set_poll_boost()
                    return
                self.notice = "파이프라인이 재개되었습니다."
                self._set_poll_boost()
            except Exception as exc:
                self.notice = f"재개 처리 실패: {exc}"

    def stop(self) -> None:
        with self.lock:
            if self._is_controller_owner():
                try:
                    if self._is_console_controller():
                        self._stop_console_controller()
                        self._set_poll_boost()
                        return
                    self._write_controller_kill_switch_marker()
                    self._run_launchctl("kill", "SIGTERM", self._controller_service_target())
                    self.notice = "controller 중지 요청을 보냈습니다."
                    self._set_poll_boost()
                except Exception as exc:
                    self.notice = f"중지 실패: {exc}"
                    self._set_poll_boost()
                return

            stopped: list[int] = []
            errors: list[str] = []
            pause_clear_failed: str | None = None
            try:
                self._set_paused(False)
            except Exception as exc:
                pause_clear_failed = f"일시정지 해제 실패: {exc}"

            if self._is_managed_running():
                assert self.worker_proc is not None
                pid = self.worker_proc.pid
                try:
                    self.worker_proc.terminate()
                    self.worker_proc.wait(timeout=8)
                    stopped.append(pid)
                except Exception as exc:
                    try:
                        self.worker_proc.kill()
                        self.worker_proc.wait(timeout=3)
                        stopped.append(pid)
                    except Exception as kill_exc:
                        errors.append(f"managed pid={pid}: {exc or kill_exc}")
                finally:
                    self.worker_proc = None

            for pid in self._find_worker_pids():
                try:
                    os.kill(pid, signal.SIGTERM)
                    stopped.append(pid)
                except ProcessLookupError:
                    continue
                except Exception as exc:
                    errors.append(f"pid={pid}: {exc}")

            if errors:
                if pause_clear_failed:
                    errors.append(pause_clear_failed)
                self.notice = "중지 중 일부 실패: " + "; ".join(errors)
                self._set_poll_boost()
            elif stopped:
                base_msg = "중지 완료: PID " + ", ".join(str(pid) for pid in sorted(set(stopped)))
                self.notice = base_msg if pause_clear_failed is None else f"{base_msg}, {pause_clear_failed}"
                self._set_poll_boost()
            else:
                if pause_clear_failed is not None:
                    self.notice = pause_clear_failed
                else:
                    self.notice = "실행 중인 워커 없음"
                self._set_poll_boost()

    def set_shutdown_handler(self, callback) -> None:
        self._shutdown_handler = callback

    def request_shutdown(self) -> None:
        self.notice = "웹 제어판 종료 요청됨"
        callback = self._shutdown_handler
        if callback is not None:
            threading.Thread(target=callback, daemon=True).start()

    def _db_counts(self) -> dict[str, int]:
        counts = {
            "PENDING": 0,
            "PROCESSING": 0,
            "NEEDS_REVIEW": 0,
            "DONE": 0,
            "ERROR": 0,
            "UNREGISTERED": 0,
        }
        try:
            with sqlite3.connect(self.db_path, timeout=3.0) as conn:
                conn.execute("PRAGMA busy_timeout = 3000")
                rows = conn.execute(
                    "SELECT status, count(*) FROM jobs GROUP BY status ORDER BY status"
                ).fetchall()
            for status, count in rows:
                if status in counts:
                    counts[status] = int(count)
        except Exception as exc:
            self.notice = f"DB 카운트 갱신 실패: {exc}"

        # inbox에 물리적으로 존재하지만 DB에 아직 등록되지 않은 파일 수
        try:
            inbox_names: set[str] = set()
            if self.watch_folder.exists() and self.watch_folder.is_dir():
                for item in self.watch_folder.iterdir():
                    if (
                        item.is_file()
                        and not item.name.startswith(".")
                        and not item.name.startswith("~")
                    ):
                        inbox_names.add(item.name)
            if inbox_names:
                with sqlite3.connect(self.db_path, timeout=3.0) as conn:
                    conn.execute("PRAGMA busy_timeout = 3000")
                    rows = conn.execute(
                        "SELECT orig_name FROM jobs WHERE status IN ('PENDING', 'PROCESSING')"
                    ).fetchall()
                    registered = {row[0] for row in rows}
                counts["UNREGISTERED"] = len(inbox_names - registered)
            else:
                counts["UNREGISTERED"] = 0
        except Exception:
            counts["UNREGISTERED"] = 0

        return counts

    def _recent_jobs(self) -> list[tuple]:
        try:
            with sqlite3.connect(self.db_path, timeout=3.0) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout = 3000")
                rows = conn.execute(
                    "SELECT id, status, orig_name, updated_at, "
                    "COALESCE(current_step, '') AS step, COALESCE(progress_pct, 0) AS progress, COALESCE(error_message, '') AS error_message "
                    "FROM jobs ORDER BY id DESC LIMIT ?",
                    (self.RECENT_JOB_LIMIT,),
                ).fetchall()
            return [
                (
                    row["id"],
                    row["status"],
                    row["orig_name"],
                    row["updated_at"],
                    row["step"],
                    int(row["progress"] or 0),
                    row["error_message"],
                )
                for row in rows
            ]
        except sqlite3.OperationalError as exc:
            if "no such column" in str(exc):
                return self._legacy_recent_jobs()
            self.notice = f"최근 작업 조회 실패: {exc}"
            return []
        except Exception as exc:
            self.notice = f"최근 작업 조회 실패: {exc}"
            return []

    def _processing_jobs(self) -> list[tuple]:
        try:
            with sqlite3.connect(self.db_path, timeout=3.0) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout = 3000")
                rows = conn.execute(
                    "SELECT id, orig_name, COALESCE(current_step, '') AS current_step, "
                    "COALESCE(progress_pct, 0) AS progress_pct, COALESCE(eta_sec, 0) AS eta_sec, updated_at "
                    "FROM jobs WHERE status = 'PROCESSING' ORDER BY updated_at ASC"
                ).fetchall()
            return [
                (
                    row["id"],
                    row["orig_name"],
                    row["current_step"] or "",
                    int(row["progress_pct"] or 0),
                    row["eta_sec"],
                )
                for row in rows
            ]
        except sqlite3.OperationalError as exc:
            if "no such column" in str(exc):
                return self._legacy_processing_jobs()
            self.notice = f"진행률 조회 실패: {exc}"
            return []
        except Exception as exc:
            self.notice = f"진행률 조회 실패: {exc}"
            return []

    @staticmethod
    def _count_files(path: Path) -> str:
        if not path:
            return "-"
        if not path.exists() or not path.is_dir():
            return "missing"
        try:
            return str(
                sum(
                    1
                    for item in path.iterdir()
                    if item.is_file()
                    and not item.name.startswith(".")
                    and not item.name.startswith("~")
                )
            )
        except Exception:
            return "오류"

    @staticmethod
    def _count_transcript_sets(path: Path) -> str:
        """transcript 폴더의 주 전사 산출물(txt/json)을 기준으로 실제 전사 건수를 반환한다."""
        if not path:
            return "-"
        if not path.exists() or not path.is_dir():
            return "missing"
        try:
            stems = set()
            for item in path.iterdir():
                if (
                    not item.is_file()
                    or item.name.startswith(".")
                    or item.name.startswith("~")
                    or item.name.endswith(".quality.json")
                    or item.suffix not in {".txt", ".json"}
                ):
                    continue
                stems.add(item.stem)
            return str(len(stems))
        except Exception:
            return "오류"

    def _truncate_log(self) -> None:
        if not self.log_path.exists():
            return
        try:
            self.log_path.write_text("", encoding="utf-8")
        except Exception:
            try:
                self.log_path.unlink()
                self.log_path.touch()
            except Exception:
                pass

    def clear_history(self) -> None:
        """완료/오류 이력과 로그만 정리하고 확인 필요 작업은 보존한다."""
        try:
            with sqlite3.connect(self.db_path, timeout=3.0) as conn:
                conn.execute("PRAGMA busy_timeout = 3000")
                # PROCESSING 작업은 건드리지 않는다 — 워커가 실제 처리 중일 수 있음
                conn.execute("DELETE FROM jobs WHERE status IN ('DONE', 'ERROR')")
                conn.commit()
        except Exception as exc:
            self.notice = f"이력 초기화 실패: {exc}"
            return

        try:
            self._truncate_log()
        except Exception:
            pass

        self.notice = "완료/오류 이력과 로그를 정리했습니다. 확인 필요 항목은 보존됩니다."
        self._set_poll_boost()

    def _folder_counts(self) -> dict[str, str]:
        return {
            "inbox": self._count_files(self.watch_folder),
            "audio": self._count_files(self.audio_folder),
            "transcripts": self._count_transcript_sets(self.transcript_folder),
            "errors": self._count_files(self.error_folder),
        }

    def _recent_log_text(self) -> str:
        if not self.log_path.exists():
            return f"log file not found: {self.log_path}\n"
        try:
            with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = deque(handle, maxlen=120)
            return "".join(lines)
        except Exception as exc:
            return f"failed to read log: {exc}\n"

    def _log_delta(self, since: int | None) -> tuple[int, str]:
        # since는 바이트 오프셋. 변경분만 추적해서 델타 전송한다.
        if not self.log_path.exists():
            msg = f"log file not found: {self.log_path}\n"
            return (0, msg)

        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                end = handle.tell()
                if since is None:
                    handle.seek(max(0, end - self.LOG_READ_BYTES))
                elif since < 0 or since > end:
                    handle.seek(0)
                else:
                    handle.seek(since)
                chunk = handle.read().decode("utf-8", errors="replace")
            return (end, chunk)
        except Exception as exc:
            msg = f"failed to read log: {exc}\n"
            return (0, msg)

    def _log_stream_delta(self, since: int | None) -> tuple[int, str, bool]:
        # SSE는 로그 truncate/rotate 이후 reset 여부를 명시적으로 알아야 한다.
        if not self.log_path.exists():
            msg = f"log file not found: {self.log_path}\n"
            return (0, msg, True)

        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                end = handle.tell()
                if since is None:
                    reset = True
                    handle.seek(max(0, end - self.LOG_READ_BYTES))
                elif since < 0 or since > end:
                    reset = True
                    handle.seek(0)
                else:
                    reset = False
                    handle.seek(since)
                chunk = handle.read().decode("utf-8", errors="replace")
            return (end, chunk, reset)
        except Exception as exc:
            msg = f"failed to read log: {exc}\n"
            return (0, msg, True)

    @staticmethod
    def _runtime_status_key(
        *,
        managed_running: bool,
        external_pids: list[int],
        paused: bool,
        controller_ready: bool = False,
    ) -> str:
        if paused:
            return "paused"
        if managed_running or external_pids or controller_ready:
            return "running"
        return "stopped"

    @staticmethod
    def _runtime_source_key(
        *,
        managed_running: bool,
        external_pids: list[int],
        controller_ready: bool = False,
    ) -> str:
        if managed_running:
            return "web"
        if controller_ready:
            return "controller"
        if external_pids:
            return "external"
        return "none"

    @staticmethod
    def _serialize_job_row(row: tuple) -> dict[str, object]:
        job_id, status, orig_name, updated_at, step, progress, error_message = row
        return {
            "id": int(job_id),
            "status": str(status or ""),
            "file_name": str(orig_name or ""),
            "updated_at": str(updated_at or ""),
            "step": str(step or ""),
            "progress_pct": int(progress or 0),
            "error_message": str(error_message or ""),
        }

    @staticmethod
    def _serialize_processing_row(row: tuple) -> dict[str, object]:
        job_id, file_name, step, progress, eta_sec = row
        eta_value = None
        if eta_sec not in (None, "", 0):
            try:
                eta_value = int(eta_sec)
            except Exception:
                eta_value = None
        return {
            "id": int(job_id),
            "file_name": str(file_name or ""),
            "step": str(step or ""),
            "progress_pct": int(progress or 0),
            "eta_sec": eta_value,
            "eta_label": ControlState._format_eta(eta_value),
        }

    def snapshot(self, include_log: bool = True) -> dict:
        with self.lock:
            managed_running = self._is_managed_running()
            pids = self._find_worker_pids()
            paused = self._is_paused()
            controller_ready = False
            controller_source = False
            controller_idle_kill_switch = False
            if self._is_controller_owner():
                if self._is_console_controller():
                    controller_loaded = True
                    controller_process_active = self._console_controller_pid() is not None
                else:
                    controller_loaded = self._controller_service_loaded()
                    controller_process_active = controller_loaded
                kill_switch_active = self._controller_kill_switch_active()
                controller_ready = controller_process_active and not kill_switch_active
                controller_source = controller_loaded
                controller_idle_kill_switch = controller_loaded and kill_switch_active and not pids
            runtime_status = self._runtime_status_key(
                managed_running=managed_running,
                external_pids=pids,
                paused=paused,
                controller_ready=controller_ready,
            )
            runtime_source = self._runtime_source_key(
                managed_running=managed_running,
                external_pids=pids,
                controller_ready=controller_source,
            )

            if controller_idle_kill_switch:
                runtime = "중지"
                runtime_desc = "controller kill-switch가 적용되어 대기 중입니다."
            elif managed_running or pids or controller_ready:
                runtime = "일시정지" if paused else "실행중"
                if controller_source and not managed_running and not pids:
                    runtime_desc = "일시정지 상태(controller 대기 중)" if paused else "controller 대기 중"
                elif controller_source and pids:
                    runtime_desc = "일시정지 상태(controller apply worker 실행 중)" if paused else "controller apply worker 실행 중"
                else:
                    runtime_desc = "일시정지 상태(큐/파일 대기 중)" if paused else "큐/파일 대기 중"
            elif paused:
                runtime = "일시정지"
                runtime_desc = "일시정지 상태(워커 미실행)"
            else:
                runtime = "중지"
                runtime_desc = "워커가 실행 중이 아닙니다."

            if not managed_running and not pids and not controller_ready and paused:
                runtime = "일시정지"
                runtime_desc = "일시정지 플래그가 적용되어 있습니다."

            origin = (
                "웹에서 실행 중"
                if managed_running
                else ("controller에서 실행 중" if controller_source else ("외부 실행 중" if pids else "-"))
            )
            jobs = self._recent_jobs()
            processing = self._processing_jobs()
            jobs_v2 = [self._serialize_job_row(row) for row in jobs]
            processing_v2 = [self._serialize_processing_row(row) for row in processing]
            current_processing = processing_v2[0] if processing_v2 else None
            if current_processing is not None:
                step = str(current_processing["step"] or "파일 감지")
                progress = int(current_processing["progress_pct"] or 0)
                eta_sec = current_processing["eta_sec"]
                progress_label = f"{step} ({progress}%)"
                eta_text = (
                    "이 파일 완료예정: -"
                    if not eta_sec
                    else f"이 파일 완료예정: {self._format_eta(eta_sec)}"
                )
            else:
                progress_label = "대기"
                eta_text = "이 파일 완료예정: -"

            poll_interval_sec = self._poll_interval_sec(bool(processing), bool(managed_running or pids))
            updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            refresh_hint = "활성 구간은 1초, 유휴 구간은 2초로 동기화됩니다. 작업 상태에서 즉시 반영이 필요하면 '상태 동기화'를 눌러주세요."
            notification_state = self._notification_state(managed_running=managed_running, external_pids=pids)

            return {
                "schema_version": 2,
                "runtime": runtime,
                "runtime_desc": runtime_desc,
                "origin": origin,
                "pause_button": "재개" if paused else "일시정지",
                "counts": self._db_counts(),
                "folders": self._folder_counts(),
                "jobs": jobs,
                "processing": processing,
                "progress": progress_label,
                "eta_text": eta_text,
                "notice": self.notice,
                "refresh_hint": refresh_hint,
                "updated_at": updated_at,
                "log_tail": self._recent_log_text() if include_log else "",
                "poll_interval_sec": poll_interval_sec,
                "runtime_state": {
                    "status": runtime_status,
                    "source": runtime_source,
                    "paused": paused,
                    "managed_running": managed_running,
                    "external_worker_pids": pids,
                    "label": runtime,
                    "description": runtime_desc,
                },
                "notification": notification_state,
                "actions": {
                    "pause_action": "resume" if paused else "pause",
                    "pause_label": "재개" if paused else "일시정지",
                    "endpoints": {
                        "state": "/api/state",
                        "logs": "/api/logs",
                        "notification": "/api/notification",
                        "start": "/api/start",
                        "pause": "/api/pause",
                        "resume": "/api/resume",
                        "stop": "/api/stop",
                        "refresh": "/api/refresh",
                        "exit": "/api/exit",
                        "clear_history": "/api/clear_history",
                    },
                },
                "jobs_v2": jobs_v2,
                "processing_v2": processing_v2,
                "summary": {
                    "current_job": current_processing,
                    "progress_label": progress_label,
                    "eta_label": eta_text,
                    "notice": self.notice,
                    "refresh_hint": refresh_hint,
                    "updated_at": updated_at,
                    "poll_interval_sec": poll_interval_sec,
                },
            }
