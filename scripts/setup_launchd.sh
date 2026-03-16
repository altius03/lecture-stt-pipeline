#!/usr/bin/env bash
# ============================================================
# lecture-stt 원클릭 설치 스크립트
# 사용법: bash /Users/geonha/lecture_stt/scripts/setup_launchd.sh
# ============================================================
set -euo pipefail

REPO="/Users/geonha/lecture_stt"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
uid=$(id -u)

print_step() { echo ""; echo "▶ $1"; }
print_ok()   { echo "  ✅ $1"; }
print_fail() { echo "  ❌ $1"; }

# ── 1. 가상환경 ──────────────────────────────────────────────
print_step "가상환경 확인"
if [ ! -x "$REPO/.venv/bin/python" ]; then
  echo "  가상환경 생성 중..."
  python3 -m venv "$REPO/.venv"
  print_ok "가상환경 생성 완료"
else
  print_ok "가상환경 이미 존재"
fi

# ── 2. 패키지 설치 ───────────────────────────────────────────
print_step "패키지 설치 확인"
"$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"
print_ok "패키지 설치 완료"

# ── 3. ffmpeg 확인 ───────────────────────────────────────────
print_step "ffmpeg 확인"
if /opt/homebrew/bin/ffmpeg -version &>/dev/null; then
  print_ok "ffmpeg 정상 (/opt/homebrew/bin/ffmpeg)"
else
  print_fail "ffmpeg 없음 → brew install ffmpeg 실행 필요"
  exit 1
fi

# ── 4. Whisper 모델 다운로드 ─────────────────────────────────
print_step "Whisper 모델 확인 (large-v3, 처음이면 수분 소요)"
"$REPO/.venv/bin/python" "$REPO/scripts/download_model.py"
print_ok "모델 준비 완료"

# ── 5. iCloud 폴더 생성 ──────────────────────────────────────
print_step "iCloud 녹음 폴더 생성"
ICLOUD_BASE="$HOME/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings"
for dir in 00_inbox 01_audio 02_transcripts 03_correction 04_summarize 05_prompt 99_errors; do
  mkdir -p "$ICLOUD_BASE/$dir"
  print_ok "$dir"
done

# ── 6. 로그 폴더 생성 ────────────────────────────────────────
print_step "로그 폴더 확인"
mkdir -p "$REPO/state/logs"
print_ok "state/logs 준비 완료"

# ── 7. launchd 등록 ──────────────────────────────────────────
print_step "launchd 서비스 등록"
mkdir -p "$LAUNCH_AGENTS"

PLISTS=(
  "com.geonha.lecture-stt"
  "com.geonha.lecture-stt-cleanup"
  "com.geonha.lecture-stt-distribute"
  "com.geonha.lecture-stt-webpanel"
)

for label in "${PLISTS[@]}"; do
  src="$REPO/launchd/${label}.plist"
  dst="$LAUNCH_AGENTS/${label}.plist"

  # 기존 등록 해제 (오류 무시)
  launchctl bootout "gui/$uid" "$dst" 2>/dev/null || true

  cp "$src" "$dst"
  launchctl bootstrap "gui/$uid" "$dst"
  launchctl kickstart -k "gui/$uid/$label"
  print_ok "$label 등록 완료"
done

# ── 8. 최종 상태 확인 ────────────────────────────────────────
print_step "서비스 상태 확인"
sleep 2
echo ""
printf "  %-50s %s\n" "서비스" "PID"
printf "  %-50s %s\n" "──────────────────────────────────────────────────" "────"
for label in "${PLISTS[@]}"; do
  pid=$(launchctl list "$label" 2>/dev/null | awk '/PID/{print $3}' || echo "-")
  if [ "$pid" != "-" ] && [ "$pid" != "0" ]; then
    printf "  %-50s %s ✅\n" "$label" "$pid"
  else
    printf "  %-50s %s ❌\n" "$label" "(미실행)"
  fi
done

# ── 9. 웹 콘솔 응답 확인 ─────────────────────────────────────
print_step "웹 콘솔 응답 확인"
sleep 3
if curl -s --max-time 5 http://127.0.0.1:8765 | grep -q "."; then
  print_ok "웹 콘솔 정상 → http://127.0.0.1:8765"
else
  echo "  ⚠️  웹 콘솔 아직 미응답 (기동 중일 수 있음)"
  echo "     잠시 후 브라우저에서 http://127.0.0.1:8765 확인하세요"
fi

# ── 10. Tailscale IP 출력 ────────────────────────────────────
print_step "Tailscale IP (노트북에서 SSH 터널 시 사용)"
if command -v tailscale &>/dev/null; then
  TS_IP=$(tailscale ip -4 2>/dev/null || echo "확인 불가")
  echo "  📡 Tailscale IP: $TS_IP"
  echo ""
  echo "  노트북 터미널에서:"
  echo "    ssh -N -L 8765:127.0.0.1:8765 geonha@$TS_IP"
  echo "  그 다음 브라우저에서:"
  echo "    http://127.0.0.1:8765"
else
  echo "  ⚠️  tailscale 명령어를 찾을 수 없습니다"
  echo "     Tailscale 앱 → 내 IP 확인 후 아래 명령어 실행:"
  echo "     ssh -N -L 8765:127.0.0.1:8765 geonha@<Tailscale-IP>"
fi

echo ""
echo "============================================================"
echo "  설치 완료! 맥미니를 재시작해도 자동으로 구동됩니다."
echo "  [주의] 시스템 설정에서 아래 항목은 직접 확인하세요:"
echo "    - 자동 로그인 켜기 (시스템 설정 → 일반 → 로그인 항목)"
echo "    - 절전 방지 설정 (시스템 설정 → 에너지 절약)"
echo "    - 원격 로그인(SSH) 켜기 (시스템 설정 → 일반 → 공유)"
echo "============================================================"
