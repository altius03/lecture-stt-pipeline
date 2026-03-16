#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="/Users/geonha/lecture_stt"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
PYTHON_BIN="$VENV_PYTHON"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi

"$PYTHON_BIN" "$REPO_ROOT/src/distribute_status.py" --config "$REPO_ROOT/config/config.yaml" "$@"
