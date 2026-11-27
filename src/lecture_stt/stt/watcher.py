from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat
import time
from typing import Dict, List

from lecture_stt.shared import utils


@dataclass
class _FileState:
    # 파일이 변경되었는지 판단할 수 있도록 마지막 상태를 저장한다.
    size: int
    mtime: float
    first_unstable_at: float


class PollingWatcher:
    # 폴더를 주기적으로 스캔해 일정 시간 이상 변경 없는 파일만 안정 파일로 간주한다.
    def __init__(self, watch_folder: str, stable_for_sec: int, polling_interval_sec: int):
        self.watch_folder = Path(watch_folder)
        self.stable_for_sec = stable_for_sec
        self.polling_interval_sec = polling_interval_sec
        self._states: Dict[str, _FileState] = {}

    def scan_once(self) -> List[Path]:
        ready = self.scan_stable_files()
        if ready or not self._states:
            return ready
        time.sleep(self.stable_for_sec)
        return self.scan_stable_files()

    # 파일을 스캔해 완성으로 판단되는 항목만 정렬해 반환한다.
    def scan_stable_files(self) -> List[Path]:
        stable_files: List[Path] = []
        now = utils.now()
        observed = set()

        try:
            entries = list(self.watch_folder.iterdir())
        except OSError:
            return stable_files

        for path in entries:
            if utils.is_temporary_file(path):
                continue
            try:
                info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            observed.add(str(path))
            state = self._states.get(str(path))

            if state is None or state.size != info.st_size or state.mtime != info.st_mtime:
                self._states[str(path)] = _FileState(size=info.st_size, mtime=info.st_mtime, first_unstable_at=now)
                continue

            unchanged_for = now - state.first_unstable_at
            if unchanged_for >= self.stable_for_sec:
                stable_files.append(path)
                self._states.pop(str(path), None)

        stale_keys = set(self._states.keys()) - observed
        for key in stale_keys:
            self._states.pop(key, None)

        def _safe_mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        stable_files.sort(key=_safe_mtime)
        return stable_files
