#!/usr/bin/env bash
set -euo pipefail

# 웹 기반 제어판을 실행한다. tkinter 의존성은 더 이상 필요 없다.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${WEB_PANEL_HOST:-127.0.0.1}"
PORT="${WEB_PANEL_PORT:-8765}"

CANDIDATES=(
  "${GUI_PYTHON_BIN:-}"
  "$REPO_ROOT/.venv/bin/python"
  "$(command -v python3 || true)"
  "/usr/bin/python3"
)

PYTHON_BIN=""
for candidate in "${CANDIDATES[@]}"; do
  if [ -z "$candidate" ] || [ ! -x "$candidate" ]; then
    continue
  fi

  if REPO_ROOT="$REPO_ROOT" "$candidate" - <<'PY'
import os
import yaml
import sys

root = os.environ["REPO_ROOT"]
sys.path.insert(0, os.path.join(root, 'src'))
import lecture_stt.ui.web_panel  # noqa: F401
yaml.safe_load('a: 1')
PY
  then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  cat <<'EOF'
No suitable Python interpreter for web control panel was found.
Install dependencies including pyyaml and set interpreter explicitly:
  GUI_PYTHON_BIN=/path/to/python3 bash "$REPO_ROOT/scripts/run_gui.sh"

Default URL: http://127.0.0.1:8765
You can change host/port:
  WEB_PANEL_HOST=127.0.0.1 WEB_PANEL_PORT=8765 bash "$REPO_ROOT/scripts/run_gui.sh"
EOF
  exit 1
fi

echo "Web control panel starting: http://$HOST:$PORT"
echo "Use this machine: open browser to http://$HOST:$PORT"
echo "To stop: Ctrl+C"

export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

exec env WEB_PANEL_HOST="$HOST" WEB_PANEL_PORT="$PORT" "$PYTHON_BIN" -m lecture_stt.ui.web_panel
