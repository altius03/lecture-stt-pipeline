from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import utils as utils


@dataclass
class _FileState:
    size: int
    mtime: float
    first_unstable_at: float


class PollingWatcher:
    def __init__(self, watch_folder: str, stable_for_sec: int, polling_interval_sec: int):
        self.watch_folder = Path(watch_folder)
        self.stable_for_sec = stable_for_sec
        self.polling_interval_sec = polling_interval_sec
        self._states: Dict[str, _FileState] = {}

    def scan_stable_files(self) -> List[Path]:
        stable_files: List[Path] = []
        if not self.watch_folder.exists():
            return stable_files

        now = utils.now()
        observed = set()

        for path in self.watch_folder.iterdir():
            if not path.is_file():
                continue
            if utils.is_temporary_file(path):
                continue

            observed.add(str(path))
            stat = path.stat()
            state = self._states.get(str(path))

            if state is None or state.size != stat.st_size or state.mtime != stat.st_mtime:
                self._states[str(path)] = _FileState(size=stat.st_size, mtime=stat.st_mtime, first_unstable_at=now)
                continue

            unchanged_for = now - state.first_unstable_at
            if unchanged_for >= self.stable_for_sec:
                stable_files.append(path)
                self._states.pop(str(path), None)

        stale_keys = set(self._states.keys()) - observed
        for key in stale_keys:
            self._states.pop(key, None)

        stable_files.sort(key=lambda p: p.stat().st_mtime)
        return stable_files

