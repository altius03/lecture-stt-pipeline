#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
PYTHON_BIN="$VENV_PYTHON"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi

if [ "${XPC_SERVICE_NAME:-}" = "com.geonha.lecture-stt" ]; then
  export LECTURE_STT_LOCK_WAIT="${LECTURE_STT_LOCK_WAIT:-1}"
fi

export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

exec "$PYTHON_BIN" -m lecture_stt.stt.main --config "$REPO_ROOT/config/config.yaml" "$@"
