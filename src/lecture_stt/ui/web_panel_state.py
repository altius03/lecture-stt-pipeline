from __future__ import annotations

import os
import signal
import sqlite3
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
import yaml


NOTIFICATION_SELECTIONS = ("telegram", "discord", "both", "disabled")


class ControlState:
    # 폴링은 상태에 따라 동적으로 조정해 불필요한 부하를 줄인다.
    FAST_POLL_INTERVAL_SEC = 0.5
    NORMAL_POLL_INTERVAL_SEC = 1
    IDLE_POLL_INTERVAL_SEC = 2
    LOG_READ_BYTES = 65536
    RECENT_JOB_LIMIT = 20

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.config_path = self.repo_root / "config" / "config.yaml"
        self.worker_module = "lecture_stt.stt.main"
        self.python_bin = venv_python()
        if not self.python_bin.exists():
            self.python_bin = Path(sys.executable)

        self.worker_proc: subprocess.Popen | None = None
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
        can_apply_now = bool(managed_running or external_pids)
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

        if self._is_managed_running() or self._find_worker_pids():
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

        if not (self._is_managed_running() or self._find_worker_pids()):
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
        if self._is_managed_running():
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

    def _worker_cmd(self, *extra: str) -> list[str]:
        cmd = [str(self.python_bin), "-m", self.worker_module]
        cmd.extend(extra)
        return cmd

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
            result = subprocess.run(
                ["pgrep", "-f", self.worker_module],
                capture_output=True,
                text=True,
                check=False,
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
        counts = {"PENDING": 0, "PROCESSING": 0, "DONE": 0, "ERROR": 0, "UNREGISTERED": 0}
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
        """transcript 폴더의 txt+json 2파일을 1쌍으로 세어 실제 전사 건수를 반환한다."""
        if not path:
            return "-"
        if not path.exists() or not path.is_dir():
            return "missing"
        try:
            stems = set()
            for item in path.iterdir():
                if (
                    item.is_file()
                    and not item.name.startswith(".")
                    and not item.name.startswith("~")
                ):
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
        """완료/오류 이력과 로그를 수동 초기화한다. 사용자가 버튼을 눌렀을 때만 실행."""
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

        self.notice = "이력 및 로그가 초기화되었습니다."
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
    ) -> str:
        if paused:
            return "paused"
        if managed_running or external_pids:
            return "running"
        return "stopped"

    @staticmethod
    def _runtime_source_key(*, managed_running: bool, external_pids: list[int]) -> str:
        if managed_running:
            return "web"
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
            runtime_status = self._runtime_status_key(
                managed_running=managed_running,
                external_pids=pids,
                paused=paused,
            )
            runtime_source = self._runtime_source_key(
                managed_running=managed_running,
                external_pids=pids,
            )

            if managed_running or pids:
                runtime = "일시정지" if paused else "실행중"
                runtime_desc = "일시정지 상태(큐/파일 대기 중)" if paused else "큐/파일 대기 중"
            elif paused:
                runtime = "일시정지"
                runtime_desc = "일시정지 상태(워커 미실행)"
            else:
                runtime = "중지"
                runtime_desc = "워커가 실행 중이 아닙니다."

            if not managed_running and not pids and paused:
                runtime = "일시정지"
                runtime_desc = "일시정지 플래그가 적용되어 있습니다."

            origin = "웹에서 실행 중" if managed_running else ("외부 실행 중" if pids else "-")
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
