#!/usr/bin/env bash
set -euo pipefail

# 실행용 가상환경 파이썬 경로를 우선 사용하고, 없으면 시스템 python3로 폴백한다.
REPO_ROOT="/Users/geonha/lecture_stt"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
PYTHON_BIN="$VENV_PYTHON"
if [ ! -x "$PYTHON_BIN" ]; then
  # 로컬 가상환경이 없으면 시스템 Python으로 대체 실행
  PYTHON_BIN="$(command -v python3)"
fi

# 메인 워커를 1회 실행해 새 파일이 있으면 변환을 시도한다.
"$PYTHON_BIN" "$REPO_ROOT/src/main.py" --once
