from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import json
from collections import deque
from datetime import datetime
from pathlib import Path

_YAML_IMPORT_ERROR: Exception | None = None
try:
    import yaml
except Exception as exc:
    yaml = None  # type: ignore[assignment]
    _YAML_IMPORT_ERROR = exc

_TK_IMPORT_ERROR: Exception | None = None
try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except Exception as exc:
    # Tk 런타임이 없는 파이썬에서도 모듈 import 자체는 가능하게 두고, 실행 시점에 안내한다.
    _TK_IMPORT_ERROR = exc

    class _TkShim:
        class Tk:
            pass

    tk = _TkShim()  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]


class STTControlPanel(tk.Tk):
    # UI 자동 갱신 주기(ms)
    REFRESH_MS = 2000
    # 로그 패널에 표시할 최대 라인 수
    LOG_TAIL_LINES = 120
    # 최근 작업 테이블에 표시할 최대 행 수
    RECENT_JOB_LIMIT = 20

    def __init__(self, repo_root: Path):
        super().__init__()
        self.repo_root = repo_root
        self.config_path = self.repo_root / "config" / "config.yaml"
        self.main_script = self.repo_root / "src" / "main.py"
        self.python_bin = self.repo_root / ".venv" / "bin" / "python"
        if not self.python_bin.exists():
            self.python_bin = Path(sys.executable)

        self.worker_proc: subprocess.Popen | None = None
        self._status_text = tk.StringVar(value="Status: initializing")
        self._meta_text = tk.StringVar(value="-")
        self._updated_text = tk.StringVar(value="-")
        self._notice_text = tk.StringVar(value="Ready")

        self.count_vars = {
            "PENDING": tk.StringVar(value="0"),
            "PROCESSING": tk.StringVar(value="0"),
            "DONE": tk.StringVar(value="0"),
            "ERROR": tk.StringVar(value="0"),
        }
        self.folder_vars = {
            "inbox": tk.StringVar(value="-"),
            "audio": tk.StringVar(value="-"),
            "transcripts": tk.StringVar(value="-"),
            "errors": tk.StringVar(value="-"),
        }

        self.config_data = self._load_config()
        self.paths = self.config_data.get("paths", {})
        self.db_path = Path(self.paths.get("db_path", self.repo_root / "state" / "jobs.sqlite3"))
        self.log_path = self._resolve_log_path()
        self.pause_flag = self.db_path.parent / "paused"

        self.title("Lecture STT Control Panel")
        self.geometry("1200x830")
        self.minsize(1024, 700)

        self._build_ui()
        self._apply_tree_tags()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._refresh_loop)

    def _load_config(self) -> dict:
        # GUI 시작 시 설정 파일을 읽어 경로와 DB 위치를 동기화한다.
        if not self.config_path.exists():
            raise FileNotFoundError(f"Missing config file: {self.config_path}")

        if yaml is not None:
            with self.config_path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
        else:
            loaded = self._load_config_via_helper_python()

        if not isinstance(loaded, dict):
            raise ValueError(f"Invalid config format: {self.config_path}")
        return loaded

    def _load_config_via_helper_python(self) -> dict:
        # 현재 인터프리터에 PyYAML이 없으면, 별도 파이썬에서 YAML을 JSON으로 변환해 읽는다.
        helper_code = (
            "import json,sys,yaml\n"
            "from pathlib import Path\n"
            "path = Path(sys.argv[1])\n"
            "loaded = yaml.safe_load(path.read_text(encoding='utf-8')) or {}\n"
            "print(json.dumps(loaded, ensure_ascii=False))\n"
        )
        candidates = [
            self.repo_root / ".venv" / "bin" / "python",
            Path(sys.executable),
            Path("/usr/bin/python3"),
        ]
        errors: list[str] = []
        for candidate in candidates:
            if not candidate.exists() or not os.access(candidate, os.X_OK):
                continue
            try:
                result = subprocess.run(
                    [str(candidate), "-c", helper_code, str(self.config_path)],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except Exception as exc:
                errors.append(f"{candidate}: {exc}")
                continue

            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown").strip()
                errors.append(f"{candidate}: {detail}")
                continue

            try:
                loaded = json.loads(result.stdout.strip())
            except Exception as exc:
                errors.append(f"{candidate}: failed to parse helper output: {exc}")
                continue
            if isinstance(loaded, dict):
                return loaded
            errors.append(f"{candidate}: helper output is not a dict")

        detail = "; ".join(errors) if errors else "no usable helper interpreter"
        raise RuntimeError(
            "Failed to load config.yaml without local PyYAML. "
            f"Original import error: {_YAML_IMPORT_ERROR}. Helper errors: {detail}"
        )

    def _resolve_log_path(self) -> Path:
        # 로그 파일 경로는 config.logging.file을 우선 사용하고 상대경로면 repo 기준으로 해석한다.
        logging_cfg = self.config_data.get("logging", {})
        configured = logging_cfg.get("file", "logs/app.log")
        path = Path(configured)
        if not path.is_absolute():
            path = self.repo_root / path
        return path

    def _build_ui(self) -> None:
        container = ttk.Frame(self, padding=12)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(3, weight=1)
        container.rowconfigure(4, weight=1)

        header = ttk.Label(container, text="Lecture STT Control Panel", font=("Helvetica", 18, "bold"))
        header.grid(row=0, column=0, sticky="w")

        top_frame = ttk.Frame(container, padding=(0, 10, 0, 6))
        top_frame.grid(row=1, column=0, sticky="ew")
        top_frame.columnconfigure(0, weight=1)
        top_frame.columnconfigure(1, weight=1)

        status_frame = ttk.LabelFrame(top_frame, text="Runtime Status", padding=10)
        status_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(status_frame, textvariable=self._status_text, font=("Helvetica", 13, "bold")).pack(anchor="w")
        ttk.Label(status_frame, textvariable=self._meta_text).pack(anchor="w", pady=(4, 0))
        ttk.Label(status_frame, textvariable=self._updated_text).pack(anchor="w", pady=(2, 0))
        ttk.Label(status_frame, textvariable=self._notice_text, foreground="#005a9e").pack(anchor="w", pady=(6, 0))

        control_frame = ttk.LabelFrame(top_frame, text="Control", padding=10)
        control_frame.grid(row=0, column=1, sticky="nsew")
        self.start_btn = ttk.Button(control_frame, text="Start", command=self.start_worker)
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.pause_btn = ttk.Button(control_frame, text="Pause", command=self.toggle_pause)
        self.pause_btn.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.stop_btn = ttk.Button(control_frame, text="Stop", command=self.stop_worker)
        self.stop_btn.grid(row=0, column=2, sticky="ew", padx=(0, 6))
        self.refresh_btn = ttk.Button(control_frame, text="Refresh", command=self.refresh_now)
        self.refresh_btn.grid(row=0, column=3, sticky="ew")
        for col in range(4):
            control_frame.columnconfigure(col, weight=1)

        metrics_frame = ttk.Frame(container)
        metrics_frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        metrics_frame.columnconfigure(0, weight=1)
        metrics_frame.columnconfigure(1, weight=1)

        db_frame = ttk.LabelFrame(metrics_frame, text="Job Counts", padding=10)
        db_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(db_frame, text="PENDING").grid(row=0, column=0, sticky="w")
        ttk.Label(db_frame, textvariable=self.count_vars["PENDING"]).grid(row=0, column=1, sticky="e")
        ttk.Label(db_frame, text="PROCESSING").grid(row=1, column=0, sticky="w")
        ttk.Label(db_frame, textvariable=self.count_vars["PROCESSING"]).grid(row=1, column=1, sticky="e")
        ttk.Label(db_frame, text="DONE").grid(row=2, column=0, sticky="w")
        ttk.Label(db_frame, textvariable=self.count_vars["DONE"]).grid(row=2, column=1, sticky="e")
        ttk.Label(db_frame, text="ERROR").grid(row=3, column=0, sticky="w")
        ttk.Label(db_frame, textvariable=self.count_vars["ERROR"]).grid(row=3, column=1, sticky="e")
        db_frame.columnconfigure(0, weight=1)
        db_frame.columnconfigure(1, weight=1)

        folder_frame = ttk.LabelFrame(metrics_frame, text="Folder Queue", padding=10)
        folder_frame.grid(row=0, column=1, sticky="nsew")
        ttk.Label(folder_frame, text="00_inbox").grid(row=0, column=0, sticky="w")
        ttk.Label(folder_frame, textvariable=self.folder_vars["inbox"]).grid(row=0, column=1, sticky="e")
        ttk.Label(folder_frame, text="01_audio").grid(row=1, column=0, sticky="w")
        ttk.Label(folder_frame, textvariable=self.folder_vars["audio"]).grid(row=1, column=1, sticky="e")
        ttk.Label(folder_frame, text="02_transcripts").grid(row=2, column=0, sticky="w")
        ttk.Label(folder_frame, textvariable=self.folder_vars["transcripts"]).grid(row=2, column=1, sticky="e")
        ttk.Label(folder_frame, text="99_errors").grid(row=3, column=0, sticky="w")
        ttk.Label(folder_frame, textvariable=self.folder_vars["errors"]).grid(row=3, column=1, sticky="e")
        folder_frame.columnconfigure(0, weight=1)
        folder_frame.columnconfigure(1, weight=1)

        jobs_frame = ttk.LabelFrame(container, text="Recent Jobs", padding=8)
        jobs_frame.grid(row=3, column=0, sticky="nsew", pady=(0, 6))
        jobs_frame.columnconfigure(0, weight=1)
        jobs_frame.rowconfigure(0, weight=1)
        columns = ("id", "status", "orig_name", "updated_at", "error_message")
        self.jobs_tree = ttk.Treeview(jobs_frame, columns=columns, show="headings", height=12)
        self.jobs_tree.heading("id", text="ID")
        self.jobs_tree.heading("status", text="Status")
        self.jobs_tree.heading("orig_name", text="File")
        self.jobs_tree.heading("updated_at", text="Updated")
        self.jobs_tree.heading("error_message", text="Error")
        self.jobs_tree.column("id", width=70, stretch=False, anchor="center")
        self.jobs_tree.column("status", width=110, stretch=False, anchor="center")
        self.jobs_tree.column("orig_name", width=270, stretch=True)
        self.jobs_tree.column("updated_at", width=220, stretch=False)
        self.jobs_tree.column("error_message", width=420, stretch=True)
        self.jobs_tree.grid(row=0, column=0, sticky="nsew")
        jobs_scroll = ttk.Scrollbar(jobs_frame, orient="vertical", command=self.jobs_tree.yview)
        jobs_scroll.grid(row=0, column=1, sticky="ns")
        self.jobs_tree.configure(yscrollcommand=jobs_scroll.set)

        log_frame = ttk.LabelFrame(container, text="App Log (tail)", padding=8)
        log_frame.grid(row=4, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="none", height=12, font=("Menlo", 11))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set, state="disabled")

    def _apply_tree_tags(self) -> None:
        # 상태별 색상을 지정해 실패/진행 중 항목을 빠르게 구분한다.
        self.jobs_tree.tag_configure("PENDING", foreground="#8a6d3b")
        self.jobs_tree.tag_configure("PROCESSING", foreground="#006699")
        self.jobs_tree.tag_configure("DONE", foreground="#1f6f43")
        self.jobs_tree.tag_configure("ERROR", foreground="#9f1d1d")

    def _worker_cmd(self, *extra: str) -> list[str]:
        cmd = [str(self.python_bin), str(self.main_script)]
        cmd.extend(extra)
        return cmd

    def _run_main_cmd(self, *extra: str) -> str:
        # pause/resume/status 같은 제어 커맨드를 동기 호출한다.
        cmd = self._worker_cmd(*extra, "--config", str(self.config_path))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            detail = stderr or stdout or "unknown error"
            raise RuntimeError(detail)
        return (result.stdout or "").strip()

    def _find_worker_pids(self) -> list[int]:
        # 외부에서 실행된 워커도 감지할 수 있도록 process 목록에서 main.py를 찾는다.
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

    def start_worker(self) -> None:
        if self._is_managed_running():
            self._notice_text.set("Managed worker is already running.")
            return
        if self._find_worker_pids():
            self._notice_text.set("Another worker process is already running.")
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
            self._notice_text.set("Worker started.")
        except Exception as exc:
            messagebox.showerror("Start failed", str(exc))
            self._notice_text.set(f"Start failed: {exc}")

    def toggle_pause(self) -> None:
        paused = self.pause_flag.exists()
        try:
            if paused:
                self._run_main_cmd("--resume")
                self._notice_text.set("Pipeline resumed.")
            else:
                self._run_main_cmd("--pause")
                self._notice_text.set("Pipeline paused.")
        except Exception as exc:
            messagebox.showerror("Pause/Resume failed", str(exc))
            self._notice_text.set(f"Pause/Resume failed: {exc}")
        finally:
            self.refresh_now()

    def stop_worker(self) -> None:
        # GUI가 실행한 워커 우선 종료 후, 남아있는 동일 main.py 프로세스도 정리한다.
        stopped_pids: list[int] = []
        errors: list[str] = []

        if self._is_managed_running():
            assert self.worker_proc is not None
            pid = self.worker_proc.pid
            try:
                self.worker_proc.terminate()
                self.worker_proc.wait(timeout=8)
                stopped_pids.append(pid)
            except Exception:
                try:
                    self.worker_proc.kill()
                    self.worker_proc.wait(timeout=3)
                    stopped_pids.append(pid)
                except Exception as exc:
                    errors.append(f"Failed to stop managed worker(pid={pid}): {exc}")
            finally:
                self.worker_proc = None

        for pid in self._find_worker_pids():
            try:
                os.kill(pid, signal.SIGTERM)
                stopped_pids.append(pid)
            except ProcessLookupError:
                continue
            except Exception as exc:
                errors.append(f"Failed to stop pid={pid}: {exc}")

        if errors:
            messagebox.showwarning("Stop result", "\n".join(errors))
        if stopped_pids:
            self._notice_text.set(f"Stopped worker PID(s): {', '.join(str(pid) for pid in sorted(set(stopped_pids)))}")
        else:
            self._notice_text.set("No running worker process found.")
        self.refresh_now()

    def refresh_now(self) -> None:
        self._refresh_runtime_status()
        self._refresh_db_counts()
        self._refresh_recent_jobs()
        self._refresh_folder_counts()
        self._refresh_log_tail()

    def _refresh_loop(self) -> None:
        self.refresh_now()
        self.after(self.REFRESH_MS, self._refresh_loop)

    def _refresh_runtime_status(self) -> None:
        # 실행 여부 + pause 플래그 조합으로 현재 동작 상태를 계산한다.
        managed = self._is_managed_running()
        managed_pid = self.worker_proc.pid if managed and self.worker_proc else None
        pids = self._find_worker_pids()
        paused = self.pause_flag.exists()

        state = "STOPPED"
        if pids and paused:
            state = "PAUSED"
        elif pids:
            state = "RUNNING"
        elif paused:
            state = "PAUSED (idle)"

        self._status_text.set(f"Status: {state}")
        origin = "managed-by-gui" if managed else ("external" if pids else "-")
        pid_info = ",".join(str(pid) for pid in pids) if pids else "-"
        self._meta_text.set(
            f"origin={origin} | managed_pid={managed_pid or '-'} | worker_pids={pid_info} | pause_flag={self.pause_flag}"
        )
        self._updated_text.set(f"last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.pause_btn.configure(text="Resume" if paused else "Pause")

    def _refresh_db_counts(self) -> None:
        counts = {"PENDING": 0, "PROCESSING": 0, "DONE": 0, "ERROR": 0}
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
            self._notice_text.set(f"DB count refresh failed: {exc}")

        for key in counts:
            self.count_vars[key].set(str(counts[key]))

    def _refresh_recent_jobs(self) -> None:
        rows = []
        try:
            with sqlite3.connect(self.db_path, timeout=3.0) as conn:
                conn.execute("PRAGMA busy_timeout = 3000")
                rows = conn.execute(
                    "SELECT id, status, orig_name, updated_at, COALESCE(error_message, '') "
                    "FROM jobs ORDER BY id DESC LIMIT ?",
                    (self.RECENT_JOB_LIMIT,),
                ).fetchall()
        except Exception as exc:
            self._notice_text.set(f"Recent job refresh failed: {exc}")

        for item in self.jobs_tree.get_children():
            self.jobs_tree.delete(item)
        for row in rows:
            status = str(row[1])
            self.jobs_tree.insert(
                "",
                "end",
                values=(row[0], status, row[2], row[3], row[4]),
                tags=(status,),
            )

    def _count_files(self, path_value: str | None) -> str:
        if not path_value:
            return "-"
        path = Path(path_value)
        if not path.exists() or not path.is_dir():
            return "missing"
        try:
            count = sum(1 for item in path.iterdir() if item.is_file())
            return str(count)
        except Exception:
            return "error"

    def _refresh_folder_counts(self) -> None:
        self.folder_vars["inbox"].set(self._count_files(self.paths.get("watch_folder")))
        self.folder_vars["audio"].set(self._count_files(self.paths.get("stable_audio_folder")))
        self.folder_vars["transcripts"].set(self._count_files(self.paths.get("transcript_folder")))
        self.folder_vars["errors"].set(self._count_files(self.paths.get("error_folder")))

    def _refresh_log_tail(self) -> None:
        if not self.log_path.exists():
            content = f"log file not found: {self.log_path}\n"
        else:
            try:
                with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
                    lines = deque(handle, maxlen=self.LOG_TAIL_LINES)
                content = "".join(lines)
            except Exception as exc:
                content = f"failed to read log: {exc}\n"

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", content)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        # GUI 종료 시 워커가 떠 있으면 사용자 확인 후 함께 정리한다.
        if self._is_managed_running():
            should_stop = messagebox.askyesno(
                "Exit",
                "A worker started by this GUI is running.\nStop it and exit?",
            )
            if should_stop:
                self.stop_worker()
        self.destroy()


def main() -> None:
    if _TK_IMPORT_ERROR is not None:
        raise RuntimeError(
            "Tkinter runtime is not available in this Python interpreter. "
            "Use a Python build with tkinter support."
        ) from _TK_IMPORT_ERROR

    repo_root = Path("/Users/geonha/lecture_stt")
    try:
        app = STTControlPanel(repo_root=repo_root)
    except Exception as exc:
        raise RuntimeError(f"Failed to initialize GUI: {exc}") from exc
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
