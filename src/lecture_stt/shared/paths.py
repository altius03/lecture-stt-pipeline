from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
from typing import Mapping

from dotenv import dotenv_values


_ENV_VAR_PATTERN = re.compile(r"\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def src_root() -> Path:
    return repo_root() / "src"


def env_file(root: Path | None = None) -> Path:
    return (root or repo_root()) / ".env"


def state_dir() -> Path:
    return repo_root() / "state"


def default_db_path() -> Path:
    return state_dir() / "jobs.sqlite3"


def pause_flag_path() -> Path:
    return state_dir() / "paused"


def venv_python() -> Path:
    return repo_root() / ".venv" / "bin" / "python"


def runtime_env(
    base: Mapping[str, str] | None = None,
    *,
    dotenv_path: Path | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {}
    if dotenv_path and dotenv_path.exists():
        loaded = dotenv_values(str(dotenv_path))
        for key, value in loaded.items():
            if key and value is not None:
                env[key] = str(value)
    for key, value in (base or os.environ).items():
        env[key] = value
    return env


def _expand_env_tokens(value: str, env: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith("${"):
            name = token[2:-1]
        else:
            name = token[1:]
        return env.get(name, token)

    return _ENV_VAR_PATTERN.sub(replace, value)


def expand_config_value(value: str, *, env: Mapping[str, str] | None = None) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("Config path value must not be empty")
    effective_env = dict(env or os.environ)
    expanded = os.path.expanduser(_expand_env_tokens(text, effective_env))
    unresolved = _ENV_VAR_PATTERN.search(expanded)
    if unresolved:
        raise ValueError(f"Unresolved environment variable in config path: {text}")
    return expanded


def resolve_config_path(
    value: str,
    *,
    base_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    expanded = expand_config_value(value, env=env)
    path = Path(expanded)
    if not path.is_absolute():
        path = (base_dir or repo_root()) / path
    return path


def resolve_executable(
    value: str,
    *,
    base_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    expanded = expand_config_value(value, env=env)
    candidate = Path(expanded)
    has_separator = os.sep in expanded or (os.altsep is not None and os.altsep in expanded)
    if candidate.is_absolute() or has_separator:
        if not candidate.is_absolute():
            candidate = (base_dir or repo_root()) / candidate
        return candidate

    effective_env = dict(env or os.environ)
    resolved = shutil.which(expanded, path=effective_env.get("PATH"))
    if resolved:
        return Path(resolved)
    return candidate


def package_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base or os.environ)
    src = str(src_root())
    existing = env.get("PYTHONPATH", "")
    parts = [part for part in existing.split(os.pathsep) if part]
    if src not in parts:
        env["PYTHONPATH"] = src if not existing else src + os.pathsep + existing
    return env
