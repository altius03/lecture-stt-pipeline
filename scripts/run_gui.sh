#!/usr/bin/env bash
set -euo pipefail

# 웹 기반 제어판을 실행한다. tkinter 의존성은 더 이상 필요 없다.
REPO_ROOT="/Users/geonha/lecture_stt"
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

  if "$candidate" - <<'PY'
import pathlib
import yaml

root = pathlib.Path('/Users/geonha/lecture_stt')
control = root / 'src' / 'web_control_panel.py'
if not control.exists():
    raise SystemExit('web control script not found')
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
  GUI_PYTHON_BIN=/path/to/python3 bash /Users/geonha/lecture_stt/scripts/run_gui.sh

Default URL: http://127.0.0.1:8765
You can change host/port:
  WEB_PANEL_HOST=127.0.0.1 WEB_PANEL_PORT=8765 bash /Users/geonha/lecture_stt/scripts/run_gui.sh
EOF
  exit 1
fi

echo "Web control panel starting: http://$HOST:$PORT"
echo "Use this machine: open browser to http://$HOST:$PORT"
echo "To stop: Ctrl+C"

SCRIPT_PATH="$REPO_ROOT/src/web_control_panel.py"
HOST="$HOST" PORT="$PORT" "$PYTHON_BIN" "$SCRIPT_PATH"
