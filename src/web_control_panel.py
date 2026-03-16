from __future__ import annotations

import html
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db
import yaml


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
        self.main_script = self.repo_root / "src" / "main.py"
        self.python_bin = self.repo_root / ".venv" / "bin" / "python"
        if not self.python_bin.exists():
            self.python_bin = Path(sys.executable)

        self.worker_proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.notice = "대기중"
        self._shutdown_handler = None
        self._poll_boost_until = 0.0

        self.config_data = self._load_config()
        paths = self.config_data.get("paths", {})
        self.db_path = Path(paths.get("db_path", self.repo_root / "state" / "jobs.sqlite3"))
        self.pause_flag = self.db_path.parent / "paused"
        self.log_path = self._resolve_log_path()
        self.watch_folder = Path(paths.get("watch_folder", ""))
        self.audio_folder = Path(paths.get("stable_audio_folder", ""))
        self.transcript_folder = Path(paths.get("transcript_folder", ""))
        self.error_folder = Path(paths.get("error_folder", ""))
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

    def _resolve_log_path(self) -> Path:
        logging_cfg = self.config_data.get("logging", {})
        configured = logging_cfg.get("file", "logs/app.log")
        path = Path(configured)
        if not path.is_absolute():
            path = self.repo_root / path
        return path

    def _worker_cmd(self, *extra: str) -> list[str]:
        cmd = [str(self.python_bin), str(self.main_script)]
        cmd.extend(extra)
        return cmd

    def _run_main_cmd(self, *extra: str) -> str:
        cmd = self._worker_cmd(*extra, "--config", str(self.config_path))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise RuntimeError(detail)
        return (result.stdout or "").strip()

    def _find_worker_pids(self) -> list[int]:
        # 관리 상태 확인을 위해 main.py 워커 PID를 찾는다.
        try:
            result = subprocess.run(
                ["pgrep", "-f", str(self.main_script)],
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

    def _clear_runtime_state(self) -> None:
        # 완전 종료 시 UI에 표시되는 로그/상태 기반 데이터 초기화.
        try:
            self._truncate_log()
        except Exception:
            self.notice = "로그 초기화 실패"

        try:
            self._set_paused(False)
        except Exception:
            pass

        try:
            with sqlite3.connect(self.db_path, timeout=3.0) as conn:
                conn.execute("DELETE FROM jobs")
                conn.execute("DELETE FROM sqlite_sequence WHERE name='jobs'")
                conn.commit()
        except Exception as exc:
            self.notice = f"상태 초기화 실패: {exc}"

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

    def snapshot(self, include_log: bool = True) -> dict:
        with self.lock:
            managed_running = self._is_managed_running()
            pids = self._find_worker_pids()
            paused = self._is_paused()

            if managed_running or pids:
                runtime = "일시정지" if paused else "실행중"
                runtime_desc = "일시정지 상태(큐/파일 대기 중)"
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
            processing = self._processing_jobs()
            if processing:
                _, file_name, step, progress, eta_sec = processing[0]
                progress_label = f"{step} ({progress}%)"
                eta_text = (
                    "이 파일 완료예정: -"
                    if not eta_sec
                    else f"이 파일 완료예정: {self._format_eta(eta_sec)}"
                )
            else:
                progress_label = "대기"
                eta_text = "이 파일 완료예정: -"

            return {
                "runtime": runtime,
                "runtime_desc": runtime_desc,
                "origin": origin,
                "pause_button": "재개" if paused else "일시정지",
                "counts": self._db_counts(),
                "folders": self._folder_counts(),
                "jobs": self._recent_jobs(),
            "processing": processing,
            "progress": progress_label,
            "eta_text": eta_text,
            "notice": self.notice,
                "refresh_hint": "활성 구간은 1초, 유휴 구간은 2초로 동기화됩니다. 작업 상태에서 즉시 반영이 필요하면 '상태 동기화'를 눌러주세요.",
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "log_tail": self._recent_log_text() if include_log else "",
                "poll_interval_sec": self._poll_interval_sec(bool(processing), bool(managed_running or pids)),
            }


def render_html(snapshot: dict) -> str:
    jobs_rows: list[str] = []
    for row in snapshot["jobs"]:
        job_id, status, orig_name, updated_at, step, progress, error_msg = row
        jobs_rows.append(
            "<tr>"
            "<td>" + html.escape(str(job_id)) + "</td>"
            "<td>" + html.escape(str(status)) + "</td>"
            "<td>" + html.escape(str(orig_name or "")) + "</td>"
            "<td>" + html.escape(str(updated_at or "")) + "</td>"
            "<td>" + html.escape(str(progress)) + "%</td>"
            "<td>" + html.escape(str(step or "")) + "</td>"
            "<td>" + html.escape(str(error_msg or "")) + "</td>"
            "</tr>"
        )
    jobs_html = "\n".join(jobs_rows) or "<tr><td colspan='7'>작업 없음</td></tr>"

    processing_rows: list[str] = []
    for job_id, file_name, step, progress, eta_sec in snapshot["processing"]:
        try:
            eta_value = int(eta_sec) if eta_sec else 0
        except Exception:
            eta_value = 0

        if eta_value:
            from datetime import datetime as _dt, timedelta as _td
            finish = _dt.now() + _td(seconds=eta_value)
            eta_text = finish.strftime("%H:%M:%S")
        else:
            eta_text = "-"
        processing_rows.append(
            "<li>" +
            "#" + html.escape(str(job_id)) + " " + html.escape(str(file_name)) + " : "
            + html.escape(str(step or "파일 감지")) + " ("
            + html.escape(str(progress)) + "%) / 이 파일 완료예정 " + html.escape(eta_text) +
            "</li>"
        )
    processing_html = "\n".join(processing_rows) or "<li>현재 처리중인 작업 없음</li>"

    style = """
    body { font-family: Arial, Helvetica, sans-serif; margin: 16px; }
    h1 { margin: 0 0 8px 0; font-size: 26px; }
    .row { display: flex; gap: 12px; margin-bottom: 12px; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 10px; flex: 1; }
    .card h3 { margin-top: 0; }
    button { padding: 8px 12px; font-size: 14px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border: 1px solid #ddd; padding: 6px; text-align: left; }
    th { background: #f7f7f7; }
    .meta { color: #333; font-size: 13px; margin: 2px 0; }
    .meta-note { color: #555; font-size: 12px; }
    .guide { background: #f8f8f8; border: 1px solid #ececec; }
    .guide ul { margin: 4px 0 0 18px; padding: 0; }
    .guide li { margin-bottom: 6px; font-size: 12px; color: #333; line-height: 1.35; }
    .notice { color: #0b62a4; }
    .log { background: #111; color: #ddd; padding: 10px; border-radius: 8px; overflow: auto; height: 280px; white-space: pre-wrap; }
    .small { font-size: 12px; color: #666; }
    .controls form { display: inline-block; margin-right: 8px; margin-bottom: 6px; }
    ul { padding-left: 20px; margin: 0; }
    """

    template = """<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>강의 녹음 STT 웹 제어판</title>
  <style>@@STYLE@@</style>
</head>
<body>
  <h1>강의 녹음 STT 웹 제어판</h1>

  <div class=\"meta\">업데이트: <span id=\"updatedAt\">@@UPDATED_AT@@</span></div>
  <div class=\"meta\">상태: <strong id=\"runtime\">@@RUNTIME@@</strong> / <span id=\"runtimeDesc\">@@RUNTIME_DESC@@</span></div>
  <div class=\"meta\">실행원천: <span id=\"origin\">@@ORIGIN@@</span></div>
  <div class=\"meta\">현재 단계: <strong id=\"progressLabel\">@@PROGRESS@@</strong> / <span id=\"etaLabel\">@@ETA@@</span> </div>
  <div class=\"meta-note\">상태 동기화 버튼: 상태/이력은 즉시 새로고침, 로그는 변경분만 이어붙입니다.</div>
  <div class=\"meta\">현재 처리 작업</div>
  <div class=\"card\"><ul id=\"processingList\">@@PROCESSING@@</ul></div>
  <div class=\"notice\" id=\"notice\">@@NOTICE@@</div>
  <div class=\"small\" id=\"refreshHint\">@@REFRESH_HINT@@</div>

  <div class=\"row controls\">
    <div class=\"card\">
      <form method=\"post\" action=\"/action/start\" onsubmit=\"sendAction('start'); return false;\"><button type=\"submit\">시작</button></form>
      <form method=\"post\" action=\"/action/pause\" onsubmit=\"sendAction('@@PAUSE_LABEL@@'); return false;\"><button type=\"submit\" id=\"pauseBtn\">@@PAUSE_LABEL@@</button></form>
      <form method=\"post\" action=\"/action/stop\" onsubmit=\"sendAction('stop'); return false;\"><button type=\"submit\">중지</button></form>
      <form method=\"post\" action=\"/action/refresh\" onsubmit=\"sendAction('refresh'); return false;\"><button type=\"submit\">상태 동기화</button></form>
      <form method=\"post\" action=\"/action/exit\" onsubmit=\"sendAction('exit'); return false;\"><button type=\"submit\">종료</button></form>
      <form method=\"post\" action=\"/action/clear_history\" onsubmit=\"sendAction('clear_history'); return false;\"><button type=\"submit\">이력 초기화</button></form>
      <div class=\"small\">종료: 웹 제어판 서버만 종료됩니다. / 이력 초기화: 완료·오류 이력과 로그를 삭제합니다.</div>
      <label><input type=\"checkbox\" id=\"autoScroll\" checked /> 자동 스크롤</label>
    </div>
    <div class=\"card guide\">
      <h3>웹 제어판 사용 안내</h3>
      <ul>
        <li><b>현재 상태</b>: <span class=\"small\">실행중/일시정지/중지 상태와 원천(웹 시작 또는 외부 실행)을 확인합니다.</span></li>
        <li><b>시작</b>: 워커를 실행(또는 재시작)합니다.</li>
        <li><b>재개</b>: 일시정지된 상태에서 계속 진행합니다.</li>
        <li><b>중지</b>: 실행 중인 워커를 종료하고 즉시 대기 상태로 전환합니다.</li>
        <li><b>로그</b>: 상단 영역은 실시간 로그입니다. 새 로그가 아래로 이어집니다.</li>
        <li><b>이력</b>: 최근 작업 표에서 상태/오류/완료 단계와 파일명을 확인합니다.</li>
        <li><b>자동 스크롤</b>: 켜면 새 로그가 도착할 때 항상 아래로 이동합니다.</li>
      </ul>
    </div>
  </div>

  <div class=\"row\">
    <div class=\"card\">
      <h3>작업 상태 개수</h3>
      <div>대기(DB): <span id=\"countPending\">@@PENDING@@</span></div>
      <div>미등록(inbox): <span id=\"countUnregistered\">@@UNREGISTERED@@</span></div>
      <div>처리중: <span id=\"countProcessing\">@@PROCESSING_COUNT@@</span></div>
      <div>완료: <span id=\"countDone\">@@DONE@@</span></div>
      <div>오류: <span id=\"countError\">@@ERROR@@</span></div>
    </div>
    <div class=\"card\">
      <h3>폴더 현황</h3>
      <div>00_inbox: <span id=\"folderInbox\">@@FOLDER_INBOX@@</span></div>
      <div>01_audio: <span id=\"folderAudio\">@@FOLDER_AUDIO@@</span></div>
      <div>02_transcripts: <span id=\"folderTranscripts\">@@FOLDER_TRANSCRIPTS@@</span> 건 (txt+json=1쌍)</div>
      <div>99_errors: <span id=\"folderErrors\">@@FOLDER_ERRORS@@</span></div>
    </div>
  </div>

  <div class=\"card\">
    <h3>최근 작업</h3>
    <div class=\"small\">최근 작업은 ID/상태/파일명/최종수정/진행률/현재 단계/오류 입니다.</div>
    <table>
      <thead><tr><th>ID</th><th>상태</th><th>파일</th><th>업데이트</th><th>진행률</th><th>단계</th><th>오류</th></tr></thead>
      <tbody id=\"jobRows\">@@JOBS@@</tbody>
    </table>
  </div>

  <div class=\"card\">
    <h3>실행 로그 미리보기</h3>
    <div class=\"log\" id=\"logBox\">@@LOG@@</div>
  </div>

<script>
let logOffset = null;
let isManualScroll = false;
let tickTimer = null;
let nextPollMs = 1000;
let pollBoostUntil = 0;

function text(v) {
  return (v === null || v === undefined) ? '' : String(v);
}

function setActionLabel(label) {
  const button = document.getElementById('pauseBtn');
  if (button) {
    button.textContent = label;
  }
}

function formatProgressValue(value) {
  if (value === null || value === undefined || value === '') {
    return '-';
  }
  return text(value);
}

function trimLogLines(element) {
  const maxLines = 500;
  const lines = element.textContent.split('\n');
  if (lines.length <= maxLines) {
    return;
  }
  element.textContent = lines.slice(lines.length - maxLines).join('\n');
}

function isLogAtBottom(element) {
  return (element.scrollTop + element.clientHeight) >= (element.scrollHeight - 6);
}

function formatEta(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) {
    return '-';
  }
  const finish = new Date(Date.now() + value * 1000);
  const hh = String(finish.getHours()).padStart(2, '0');
  const mm = String(finish.getMinutes()).padStart(2, '0');
  const ss = String(finish.getSeconds()).padStart(2, '0');
  return `${hh}:${mm}:${ss}`;
}

function updateJobs(rows) {
  const target = document.getElementById('jobRows');
  let html = '';
  if (!rows || rows.length === 0) {
    target.innerHTML = '<tr><td colspan="7">작업 없음</td></tr>';
    return;
  }

  for (const row of rows) {
    const [job_id, status, orig_name, updated_at, step, progress, error_msg] = row;
    html += '<tr>' +
      '<td>' + text(job_id) + '</td>' +
      '<td>' + text(status) + '</td>' +
      '<td>' + text(orig_name) + '</td>' +
      '<td>' + text(updated_at) + '</td>' +
      '<td>' + formatProgressValue(progress) + '%</td>' +
      '<td>' + text(step) + '</td>' +
      '<td>' + text(error_msg) + '</td>' +
      '</tr>';
  }
  target.innerHTML = html;
}

function updateProcessing(rows) {
  const target = document.getElementById('processingList');
  if (!rows || rows.length === 0) {
    target.innerHTML = '<li>현재 처리중인 작업 없음</li>';
    return;
  }

  let html = '';
  for (const row of rows) {
    const [job_id, file_name, step, progress, eta_sec] = row;
    const etaText = formatEta(eta_sec);
    html += `<li>#${text(job_id)} ${text(file_name)} : ${text(step)} (${text(progress)}%) / 이 파일 완료예정 ${etaText}</li>`;
  }
  target.innerHTML = html;
}

function applyState(snapshot) {
  document.getElementById('updatedAt').textContent = text(snapshot.updated_at);
  document.getElementById('runtime').textContent = text(snapshot.runtime);
  document.getElementById('runtimeDesc').textContent = text(snapshot.runtime_desc);
  document.getElementById('origin').textContent = text(snapshot.origin);
  document.getElementById('progressLabel').textContent = text(snapshot.progress);
  document.getElementById('etaLabel').textContent = text(snapshot.eta_text);
  document.getElementById('notice').textContent = text(snapshot.notice);
  document.getElementById('countPending').textContent = text(snapshot.counts.PENDING);
  document.getElementById('countUnregistered').textContent = text(snapshot.counts.UNREGISTERED);
  document.getElementById('countProcessing').textContent = text(snapshot.counts.PROCESSING);
  document.getElementById('countDone').textContent = text(snapshot.counts.DONE);
  document.getElementById('countError').textContent = text(snapshot.counts.ERROR);
  document.getElementById('folderInbox').textContent = text(snapshot.folders.inbox);
  document.getElementById('folderAudio').textContent = text(snapshot.folders.audio);
  document.getElementById('folderTranscripts').textContent = text(snapshot.folders.transcripts);
  document.getElementById('folderErrors').textContent = text(snapshot.folders.errors);
  document.getElementById('refreshHint').textContent = text(snapshot.refresh_hint);

  setActionLabel(text(snapshot.pause_button));
  updateJobs(snapshot.jobs || []);
  updateProcessing(snapshot.processing || []);
}

async function refreshState() {
  const response = await fetch('/api/state', { cache: 'no-store' });
  if (!response.ok) {
    return null;
  }
  const snapshot = await response.json();
  applyState(snapshot);
  return snapshot;
}

async function refreshLogs(force) {
  const query = logOffset === null ? '' : `?offset=${encodeURIComponent(logOffset)}`;
  const response = await fetch(`/api/logs${query}`, { cache: 'no-store' });
  if (!response.ok) {
    return;
  }

  const payload = await response.json();
  const payloadText = payload.text || '';
  const autoScroll = document.getElementById('autoScroll');
  const logBox = document.getElementById('logBox');

  if (payloadText) {
    const wasBottom = isLogAtBottom(logBox);
    logBox.textContent += payloadText;
    trimLogLines(logBox);
    if ((autoScroll && autoScroll.checked && (force || wasBottom)) || force) {
      logBox.scrollTop = logBox.scrollHeight;
    }
  }

  logOffset = payload.offset;
  if (autoScroll && autoScroll.checked) {
    isManualScroll = false;
  }
}

function trackScrollState() {
  const logBox = document.getElementById('logBox');
  const autoScroll = document.getElementById('autoScroll');

  logBox.addEventListener('scroll', () => {
    const atBottom = isLogAtBottom(logBox);
    if (!atBottom) {
      isManualScroll = true;
      if (autoScroll) {
        autoScroll.checked = false;
      }
      return;
    }

    isManualScroll = false;
    if (autoScroll) {
      autoScroll.checked = true;
    }
  });

  const checkbox = document.getElementById('autoScroll');
  checkbox.addEventListener('change', () => {
    if (checkbox.checked && !isManualScroll) {
      logBox.scrollTop = logBox.scrollHeight;
    }
  });
}

async function sendAction(name) {
  const knownActions = ['start', 'stop', 'exit', 'refresh', 'clear_history'];
  let endpoint;
  if (name === '재개') {
    endpoint = '/action/resume';
  } else if (name === '일시정지') {
    endpoint = '/action/pause';
  } else if (knownActions.includes(name)) {
    endpoint = '/action/' + name;
  } else {
    endpoint = '/api/' + name;
  }

  await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: ''
  });
  pollBoostUntil = Date.now() + 6000;
  await refreshState();
  await refreshLogs(true);
}

function scheduleNextPoll() {
  if (tickTimer) {
    clearTimeout(tickTimer);
    tickTimer = null;
  }

  const effectiveInterval = pollBoostUntil > Date.now() ? 500 : nextPollMs;
  tickTimer = setTimeout(() => {
    tickTimer = null;
    runTick();
  }, effectiveInterval);
}

async function runTick() {
  const snapshot = await refreshState();
  const forceLog = !!(snapshot && snapshot.poll_interval_sec === 0.5 && pollBoostUntil > Date.now());
  await refreshLogs(forceLog);

  if (snapshot && Number.isFinite(Number(snapshot.poll_interval_sec)) && Number(snapshot.poll_interval_sec) > 0) {
    nextPollMs = Math.max(200, Math.round(Number(snapshot.poll_interval_sec) * 1000));
  }
  if (snapshot && snapshot.runtime_desc) {
    nextPollMs = Math.max(200, Math.round((snapshot.poll_interval_sec || 1) * 1000));
  }
  scheduleNextPoll();
}

window.onload = () => {
  trackScrollState();
  refreshState();
  refreshLogs(true);
  runTick();
};
</script>
</body>
</html>"""

    return (
        template.replace("@@STYLE@@", style)
        .replace("@@UPDATED_AT@@", html.escape(snapshot["updated_at"]))
        .replace("@@RUNTIME@@", html.escape(snapshot["runtime"]))
        .replace("@@RUNTIME_DESC@@", html.escape(snapshot["runtime_desc"]))
        .replace("@@ORIGIN@@", html.escape(snapshot["origin"]))
        .replace("@@PROGRESS@@", html.escape(snapshot["progress"]))
        .replace("@@ETA@@", html.escape(snapshot["eta_text"]))
        .replace("@@PROCESSING@@", processing_html)
        .replace("@@NOTICE@@", html.escape(snapshot["notice"]))
        .replace("@@REFRESH_HINT@@", html.escape(snapshot["refresh_hint"]))
        .replace("@@PAUSE_LABEL@@", html.escape(snapshot["pause_button"]))
        .replace("@@PENDING@@", str(snapshot["counts"]["PENDING"]))
        .replace("@@UNREGISTERED@@", str(snapshot["counts"]["UNREGISTERED"]))
        .replace("@@PROCESSING_COUNT@@", str(snapshot["counts"]["PROCESSING"]))
        .replace("@@DONE@@", str(snapshot["counts"]["DONE"]))
        .replace("@@ERROR@@", str(snapshot["counts"]["ERROR"]))
        .replace("@@FOLDER_INBOX@@", html.escape(snapshot["folders"]["inbox"]))
        .replace("@@FOLDER_AUDIO@@", html.escape(snapshot["folders"]["audio"]))
        .replace("@@FOLDER_TRANSCRIPTS@@", html.escape(snapshot["folders"]["transcripts"]))
        .replace("@@FOLDER_ERRORS@@", html.escape(snapshot["folders"]["errors"]))
        .replace("@@JOBS@@", jobs_html)
        .replace("@@LOG@@", html.escape(snapshot["log_tail"]))
        .replace("@@POLL_INTERVAL@@", str(snapshot["poll_interval_sec"]))
    )


class RequestHandler(BaseHTTPRequestHandler):
    def _write(self, code: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        encoded = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _write_json(self, code: int, payload: dict) -> None:
        self._write(code, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def _action_start(self, api_mode: bool = False) -> None:
        assert STATE is not None
        STATE.start()
        if api_mode:
            self._write_json(200, {"ok": True, "notice": STATE.notice})
        else:
            self._redirect_home()

    def _action_pause(self, api_mode: bool = False) -> None:
        assert STATE is not None
        STATE.pause()
        if api_mode:
            self._write_json(200, {"ok": True, "notice": STATE.notice})
        else:
            self._redirect_home()

    def _action_resume(self, api_mode: bool = False) -> None:
        assert STATE is not None
        STATE.resume()
        if api_mode:
            self._write_json(200, {"ok": True, "notice": STATE.notice})
        else:
            self._redirect_home()

    def _action_stop(self, api_mode: bool = False) -> None:
        assert STATE is not None
        STATE.stop()
        if api_mode:
            self._write_json(200, {"ok": True, "notice": STATE.notice})
        else:
            self._redirect_home()

    def _action_refresh(self, api_mode: bool = False) -> None:
        if api_mode:
            self._write_json(200, {"ok": True})
        else:
            self._redirect_home()

    def _action_clear_history(self, api_mode: bool = False) -> None:
        assert STATE is not None
        STATE.clear_history()
        if api_mode:
            self._write_json(200, {"ok": True, "notice": STATE.notice})
        else:
            self._redirect_home()

    def _action_exit(self, api_mode: bool = False) -> None:
        assert STATE is not None
        STATE.stop()
        STATE._clear_runtime_state()
        STATE.request_shutdown()
        if api_mode:
            self._write_json(200, {"ok": True, "notice": "종료 요청됨"})
        else:
            self._write(303, "종료 요청됨. 창을 닫고 터미널에서 제어판을 종료하세요.")

    def _redirect_home(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _api_error(self, status: int, message: str) -> None:
        self._write_json(status, {"ok": False, "error": message})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/":
            assert STATE is not None
            self._write(200, render_html(STATE.snapshot()))
            return

        if parsed.path == "/api/state":
            assert STATE is not None
            self._write_json(200, STATE.snapshot())
            return

        if parsed.path == "/api/logs":
            assert STATE is not None
            offset_raw = query.get("offset", [None])[0]
            try:
                offset = int(offset_raw) if offset_raw is not None else None
            except ValueError:
                offset = None
            log_offset, text_data = STATE._log_delta(offset)
            self._write_json(200, {"offset": log_offset, "text": text_data})
            return

        self._write(404, "Not Found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path.startswith("/api/"):
            if parsed.path == "/api/start":
                self._action_start(api_mode=True)
                return
            if parsed.path == "/api/pause":
                self._action_pause(api_mode=True)
                return
            if parsed.path == "/api/resume":
                self._action_resume(api_mode=True)
                return
            if parsed.path == "/api/stop":
                self._action_stop(api_mode=True)
                return
            if parsed.path == "/api/refresh":
                self._action_refresh(api_mode=True)
                return
            if parsed.path == "/api/exit":
                self._action_exit(api_mode=True)
                return
            if parsed.path == "/api/clear_history":
                self._action_clear_history(api_mode=True)
                return
            self._api_error(404, "Not Found")
            return

        if parsed.path == "/action/start":
            self._action_start(api_mode=False)
            return
        if parsed.path == "/action/pause":
            self._action_pause(api_mode=False)
            return
        if parsed.path == "/action/resume":
            self._action_resume(api_mode=False)
            return
        if parsed.path == "/action/stop":
            self._action_stop(api_mode=False)
            return
        if parsed.path == "/action/refresh":
            self._action_refresh(api_mode=False)
            return
        if parsed.path == "/action/exit":
            self._action_exit(api_mode=False)
            return
        if parsed.path == "/action/clear_history":
            self._action_clear_history(api_mode=False)
            return

        self._write(404, "Not Found", "text/plain; charset=utf-8")

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


STATE: ControlState | None = None


def main() -> None:
    repo_root = Path("/Users/geonha/lecture_stt")
    host = os.environ.get("WEB_PANEL_HOST", "127.0.0.1")
    port_env = os.environ.get("WEB_PANEL_PORT", "8765")
    try:
        port = int(port_env)
    except ValueError as exc:
        raise RuntimeError(f"Invalid WEB_PANEL_PORT: {port_env}") from exc

    global STATE
    STATE = ControlState(repo_root=repo_root)

    ThreadingHTTPServer.allow_reuse_address = True
    try:
        server = ThreadingHTTPServer((host, port), RequestHandler)
    except PermissionError as exc:
        raise RuntimeError(
            f"Cannot bind web control panel to {host}:{port}. Permission denied: {exc}. "
            "Check sandbox/security policy or use a non-restricted shell/session."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot bind web control panel to {host}:{port}. {exc}") from exc

    STATE.set_shutdown_handler(server.shutdown)
    print(f"Web control panel running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
