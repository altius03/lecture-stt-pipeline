from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def src_root() -> Path:
    return repo_root() / "src"


def env_file() -> Path:
    return repo_root() / ".env"


def state_dir() -> Path:
    return repo_root() / "state"


def default_db_path() -> Path:
    return state_dir() / "jobs.sqlite3"


def pause_flag_path() -> Path:
    return state_dir() / "paused"


def venv_python() -> Path:
    return repo_root() / ".venv" / "bin" / "python"


def package_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base or os.environ)
    src = str(src_root())
    existing = env.get("PYTHONPATH", "")
    parts = [part for part in existing.split(os.pathsep) if part]
    if src not in parts:
        env["PYTHONPATH"] = src if not existing else src + os.pathsep + existing
    return env
