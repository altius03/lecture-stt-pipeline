#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 메인 워커를 1회 실행해 새 파일이 있으면 변환을 시도한다.
bash "$REPO_ROOT/scripts/run_worker.sh" --once "$@"
