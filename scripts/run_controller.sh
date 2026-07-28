#!/usr/bin/env bash
set -uo pipefail

if [[ "${XPC_SERVICE_NAME:-}" != "com.geonha.lecture-stt-controller" ]]; then
  echo "controller wrapper requires the expected launchd service" >&2
  exit 2
fi
if (( $# < 1 )) || [[ ! -x "$1" ]]; then
  echo "controller wrapper requires an executable controller binary" >&2
  exit 2
fi

"$@" &
controller_pid=$!

forward_stop() {
  kill -TERM "$controller_pid" 2>/dev/null || true
}

trap forward_stop INT TERM
wait "$controller_pid"
exit_code=$?
wait "$controller_pid" 2>/dev/null || true
exit "$exit_code"
