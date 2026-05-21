from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class FinalEntryState:
    path: Path
    exists: bool
    is_regular: bool
    file_type: str | None = None
    symlink_target: str | None = None
    error: str | None = None


def final_type_name(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "character_device"
    if stat.S_ISBLK(mode):
        return "block_device"
    return "unknown"


def _symlink_target(path: Path, mode: int) -> str | None:
    if not stat.S_ISLNK(mode):
        return None
    try:
        return os.readlink(path)
    except OSError:
        return None


def final_parent_state(path: str | Path) -> FinalEntryState:
    parent = Path(path).parent
    if not os.path.lexists(parent):
        return FinalEntryState(path=parent, exists=False, is_regular=False)
    try:
        mode = parent.lstat().st_mode
    except OSError as exc:
        return FinalEntryState(
            path=parent,
            exists=True,
            is_regular=False,
            file_type="unknown",
            error=type(exc).__name__,
        )
    return FinalEntryState(
        path=parent,
        exists=True,
        is_regular=False,
        file_type=final_type_name(mode),
        symlink_target=_symlink_target(parent, mode),
    )


def final_parent_is_real_dir(path: str | Path) -> bool:
    state = final_parent_state(path)
    return state.exists and state.file_type == "directory"


def final_entry_lexists(path: str | Path) -> bool:
    return final_parent_is_real_dir(path) and os.path.lexists(Path(path))


def final_entry_state(path: str | Path) -> FinalEntryState:
    final_path = Path(path)
    parent = final_parent_state(final_path)
    if parent.exists and parent.file_type != "directory":
        return FinalEntryState(path=final_path, exists=False, is_regular=False)
    if not os.path.lexists(final_path):
        return FinalEntryState(path=final_path, exists=False, is_regular=False)
    try:
        mode = final_path.lstat().st_mode
    except OSError as exc:
        return FinalEntryState(
            path=final_path,
            exists=True,
            is_regular=False,
            file_type="unknown",
            error=type(exc).__name__,
        )
    file_type = final_type_name(mode)
    symlink_target: str | None = None
    error: str | None = None
    if stat.S_ISLNK(mode):
        try:
            symlink_target = os.readlink(final_path)
        except OSError as exc:
            error = type(exc).__name__
    return FinalEntryState(
        path=final_path,
        exists=True,
        is_regular=stat.S_ISREG(mode),
        file_type=file_type,
        symlink_target=symlink_target,
        error=error,
    )


def final_regular_exists(path: str | Path) -> bool:
    return final_parent_is_real_dir(path) and final_entry_state(path).is_regular


def all_final_regular(paths: Iterable[str | Path]) -> bool:
    return all(final_regular_exists(path) for path in paths)


def _unsupported_entry_from_state(state: FinalEntryState, *, reason: str, key: str = "final_path") -> dict[str, str]:
    entry = {
        key: str(state.path),
        "reason": reason,
        "file_type": state.file_type or "unknown",
    }
    if state.error is not None:
        entry["error"] = state.error
    if state.symlink_target is not None:
        entry["symlink_target"] = state.symlink_target
    return entry


def unsupported_final_entries(paths: Iterable[str | Path]) -> list[dict[str, str]]:
    unsupported: list[dict[str, str]] = []
    seen_parents: set[Path] = set()
    path_list = [Path(path) for path in paths]

    for raw_path in path_list:
        parent_state = final_parent_state(raw_path)
        if not parent_state.exists or parent_state.file_type == "directory" or parent_state.path in seen_parents:
            continue
        seen_parents.add(parent_state.path)
        reason = "lstat_failed" if parent_state.file_type == "unknown" and parent_state.error is not None else "unsupported_preexisting_final_parent_type"
        unsupported.append(_unsupported_entry_from_state(parent_state, reason=reason, key="final_parent_path"))

    for raw_path in path_list:
        state = final_entry_state(raw_path)
        if not state.exists or state.is_regular:
            continue
        reason = "lstat_failed" if state.file_type == "unknown" and state.error is not None else "unsupported_preexisting_final_artifact_type"
        unsupported.append(_unsupported_entry_from_state(state, reason=reason))
    return unsupported
