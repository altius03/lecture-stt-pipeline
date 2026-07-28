from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Mapping

from lecture_stt.shared import utils


class ControllerGateError(RuntimeError):
    pass


class ControllerKillSwitchActiveError(ControllerGateError):
    pass


def normalize_controller_kill_switch_path(
    value: str | Path | None,
    *,
    operation: str = "controller retry execution",
) -> Path:
    if value is None or not str(value).strip():
        raise ControllerGateError(
            f"{operation} requires --controller-kill-switch"
        )
    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def ensure_controller_kill_switch_inactive(path: Path) -> None:
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ControllerGateError("controller kill-switch path is not safely inspectable") from exc

    if not stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        raise ControllerGateError(
            "controller kill-switch path must be absent or a regular single-link marker"
        )
    if observed.st_nlink != 1:
        raise ControllerGateError(
            "controller kill-switch path must be absent or a regular single-link marker"
        )
    raise ControllerKillSwitchActiveError("controller kill switch is active")


def controller_kill_switch_is_active(path: Path) -> bool:
    try:
        ensure_controller_kill_switch_inactive(path)
    except ControllerKillSwitchActiveError:
        return True
    return False


def ensure_controller_execution_unpaused(config: Mapping[str, Any] | dict[str, Any] | None) -> None:
    if utils.is_paused(dict(config) if isinstance(config, Mapping) else config):
        raise ControllerGateError(
            "controller execution is blocked while the pause flag is active"
        )


__all__ = [
    "ControllerGateError",
    "ControllerKillSwitchActiveError",
    "controller_kill_switch_is_active",
    "ensure_controller_execution_unpaused",
    "ensure_controller_kill_switch_inactive",
    "normalize_controller_kill_switch_path",
]
