# Lecture STT 운영 가이드

## 추가 문서
- 현재 운영 runbook: `docs/OPERATIONS.md`
- 구조 문서: `docs/ARCHITECTURE.md`
- 누적 변경 기록: `docs/WORKLOG.md`
- 저장소 작업 워크플로우: `AGENTS.md`

주의: 이 README에는 legacy 경로 예시가 남아 있을 수 있습니다. 현재 운영 기준은 `/Users/geonha/DEV/lecture_stt`와 `docs/OPERATIONS.md`를 우선합니다.

## 1. 시스템 개요
- 업로드된 음성 파일을 자동으로 텍스트로 변환합니다.
- 기본 감시 폴더: `00_inbox`
- 출력 폴더: `02_transcripts`
- 실패 폴더: `99_errors`
- 관리 폴더
  - `01_audio`: 변환 전 임시 원본 보관
  - `03_correction`: 수동/provider-neutral 교정 결과
  - `04_summarize`: 수동/provider-neutral 요약 결과
  - `state/jobs.sqlite3`: 처리 이력 DB
  - `logs/app.log`: 실행 로그

## 2. 기본 폴더 구조(고정)
- 프로젝트: `/Users/geonha/lecture_stt`
- 인박스: `/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/00_inbox`
- 오디오 임시 보관: `/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/01_audio`
- 텍스트: `/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/02_transcripts`
- 교정: `/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/03_correction`
- 요약: `/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/04_summarize`
- 오류: `/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/99_errors`
- 임시 폴더: `/Users/geonha/lecture_stt/tmp`
- DB: `/Users/geonha/lecture_stt/state/jobs.sqlite3`
- 로그: `/Users/geonha/lecture_stt/logs/app.log`

## 3. 최초 준비
1. 가상환경 생성
```bash
cd /Users/geonha/lecture_stt
python3 -m venv .venv
/Users/geonha/lecture_stt/.venv/bin/pip install -r /Users/geonha/lecture_stt/requirements.txt
```

2. 환경 파일 준비
```bash
cp /Users/geonha/lecture_stt/config/config.example.yaml /Users/geonha/lecture_stt/config/config.yaml
```

3. Telegram 알림 사용 시(선택, 권장)
```bash
cat > /Users/geonha/lecture_stt/.env <<'EOF'
TELEGRAM_BOT_TOKEN=123456789:telegram-bot-token
TELEGRAM_CHAT_ID=-1001234567890
# TELEGRAM_MESSAGE_THREAD_ID=10
EOF
```

- `config/config.yaml`의 `notification.provider`를 `telegram`으로 두면 텔레그램으로 전송합니다.
- 전환 기간에 디스코드도 같이 보내려면 `dual_send_providers: [discord]`를 사용합니다.
- 기존 디스코드 webhook을 계속 쓰려면 `.env`에 `DISCORD_WEBHOOK_URL=...`를 두고 `notification.provider: discord`로 설정합니다.

4. 폴더 접근 권한 확인
```bash
mkdir -p "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/00_inbox"
mkdir -p "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/01_audio"
mkdir -p "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/02_transcripts"
mkdir -p "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/99_errors"
```

## 4. 파일 처리 방식
1. 음성 파일은 `00_inbox`에 넣습니다.
2. 파일 복사가 끝나고 안정 시간(기본 90초)이 지나면 처리 대상으로 인식됩니다.
3. 변환 완료 시 텍스트는 `02_transcripts`에 저장됩니다.
4. 실패 시 `99_errors`로 이동되며, 실패 사유는 앱 로그와 알림에 남습니다.

### 4.1 Correction/summary 수동 모드
- API-backed 자동 교정 provider는 현재 비활성화되어 있으며, `ANTHROPIC_API_KEY`나 Claude 전용 모델 설정은 필요하지 않습니다.
- `02_transcripts`의 raw `{stem}.txt + {stem}.json`은 그대로 보존합니다.
- 사람이 검토했거나 외부 도구로 교정한 `{stem}.txt + {stem}.json` pair를 `03_correction`에 넣으면 downstream worker가 GH archive origin으로 배포합니다.
- 요약 `{stem}.md`를 `04_summarize`에 넣으면 correction 전달 완료를 확인한 뒤 GH archive summary와 Obsidian note 경로로 배포합니다.
- destination에 다른 내용이 이미 있으면 overwrite하지 않고 `CONFLICT`로 남깁니다. 현재 problem row 전수 분류는 `docs/DOWNSTREAM_TRIAGE_2026-05-19.md`를 참고합니다.

## 5. 실행 방법

### 5.1 수동 실행(터미널)
- 시작
```bash
bash /Users/geonha/lecture_stt/scripts/run_worker.sh
```

- 1회 실행(테스트)
```bash
bash /Users/geonha/lecture_stt/scripts/run_once.sh
```

- 일시정지/재개/상태 확인
```bash
bash /Users/geonha/lecture_stt/scripts/run_worker.sh --pause
bash /Users/geonha/lecture_stt/scripts/run_worker.sh --resume
bash /Users/geonha/lecture_stt/scripts/run_worker.sh --status
```

### 5.2 자동 실행(launchd, 선택)
```bash
mkdir -p /Users/geonha/Library/LaunchAgents
cp /Users/geonha/lecture_stt/launchd/com.geonha.lecture-stt.plist /Users/geonha/Library/LaunchAgents/com.geonha.lecture-stt.plist
uid=$(id -u)
launchctl bootout "gui/$uid" /Users/geonha/Library/LaunchAgents/com.geonha.lecture-stt.plist 2>/dev/null || true
launchctl bootstrap "gui/$uid" /Users/geonha/Library/LaunchAgents/com.geonha.lecture-stt.plist
launchctl kickstart -k "gui/$uid/com.geonha.lecture-stt"
```

- 중지
```bash
uid=$(id -u)
launchctl bootout "gui/$uid" /Users/geonha/Library/LaunchAgents/com.geonha.lecture-stt.plist
```

### 5.3 웹 컨트롤 패널(권장)
- 실행
```bash
bash /Users/geonha/lecture_stt/scripts/run_gui.sh
```
- 브라우저 접속: `http://127.0.0.1:8765`
- 기본 동작: 시작 / 일시정지(재개) / 중지 / 새로고침
- React 패널 상단 알림 토글에서 `텔레그램만 / 디스코드만 / 둘 다 / 끄기`를 순환 선택할 수 있습니다.
- 이 변경은 `config/config.yaml`의 `notification` 섹션에 반영되며, 실행 중 워커가 있으면 즉시 재시작 적용됩니다.
- 포트 변경
```bash
WEB_PANEL_HOST=127.0.0.1 WEB_PANEL_PORT=8765 bash /Users/geonha/lecture_stt/scripts/run_gui.sh
```

- 원격 사용은 SSH 터널을 권장합니다.
- React 프론트엔드 소스는 `/Users/geonha/lecture_stt/frontend/web-panel`에 있다.
- React 패널 첫 빌드
```bash
cd /Users/geonha/lecture_stt/frontend/web-panel
npm install
npm run build
```
- React 프론트 구조
  - `src/lib/panelApi.ts`: fetch/action adapter
  - `src/lib/panelEvents.ts`: SSE event stream adapter/service
  - `src/lib/decodePanelState.ts`: runtime decoder
  - `src/hooks/usePanelState.ts`: panel state query + action mutation + SSE cache sync
  - `src/hooks/usePanelLogs.ts`: logs SSE + polling fallback + auto-follow UX
  - `src/components/panel`, `src/components/ui`: 운영 패널용 로컬 컴포넌트
- 개발 중에는 `npm run dev`를 띄우면 `/api` 요청이 `127.0.0.1:8765`로 프록시됩니다.
- React 패널은 기본적으로 `GET /api/events` SSE를 우선 사용하고, 브라우저/네트워크 사정으로 스트림이 끊기면 기존 `/api/state`, `/api/logs` polling으로 자동 fallback 합니다.
- 프론트 테스트
```bash
cd /Users/geonha/lecture_stt/frontend/web-panel
npm test
```
- React 패널 수동 검증 체크리스트
  1. `/` 초기 진입 시 loading block 뒤에 runtime, queue, folders, jobs, logs 카드가 모두 렌더링되는지 확인합니다.
  2. `새로고침` 액션 실행 시 버튼이 pending 상태를 거친 뒤 `updated_at` 또는 summary 정보가 다시 갱신되는지 확인합니다.
  3. 운영 중인 워커 제어가 안전한 환경이면 `시작 -> 일시정지/재개 -> 중지`를 순서대로 눌러 각 액션 후 상태 카드와 버튼 라벨이 refetch 결과로 바뀌는지 확인합니다.
  4. 다른 터미널에서 `logs/app.log`에 테스트 마커를 추가해 로그 패널에 새 줄이 append 되는지 확인합니다.
  5. `이력 초기화` 실행 후 로그 패널이 reset 되고 이후 새 로그가 다시 처음부터 쌓이는지 확인합니다.
  6. 자동 따라가기를 끄고 로그 패널을 위로 스크롤한 뒤 새 로그를 추가해 `최신으로 이동` 버튼이 나타나는지 확인합니다.
  7. `최신으로 이동` 클릭 후 스크롤이 맨 아래로 이동하고 자동 따라가기가 다시 켜지는지 확인합니다.
- 프론트 산출물 커밋 기준
  - `frontend/web-panel/package-lock.json`은 커밋합니다. 설치, 테스트, 빌드 재현성을 고정하는 역할이 있습니다.
  - `frontend/web-panel/dist/`는 기본적으로 커밋하지 않습니다. 현재 웹 패널 서버는 로컬 `dist`가 있으면 `/`에서 메인 패널로 서빙하고, 없으면 빌드 안내 화면을 보여주도록 되어 있어 빌드 산출물을 저장소 기본 소스로 취급하지 않습니다.
  - 배포 대상 장비에서 Node 빌드를 수행할 수 없다면 `dist`를 VCS가 아니라 릴리스 산출물로 관리할지 별도 정책을 정합니다.

### 5.4 Stage 5 downstream 배포 worker
- 1회 실행
```bash
bash /Users/geonha/lecture_stt/scripts/distribute_once.sh
```

- dry-run
```bash
bash /Users/geonha/lecture_stt/scripts/distribute_once.sh --dry-run
```

- 상시 실행
```bash
bash /Users/geonha/lecture_stt/scripts/run_distribute.sh
```

- launchd 등록 시
  - `/Users/geonha/lecture_stt/launchd/com.geonha.lecture-stt-distribute.plist`
  - `bash /Users/geonha/lecture_stt/scripts/setup_launchd.sh` 실행 시 함께 등록됩니다.

- 동작 규칙
  - correction은 `{stem}.txt` + `{stem}.json` pair 단위로만 배포됩니다.
  - summary는 correction 이력이 확인될 때만 TUK/Obsidian으로 배포됩니다.
  - overwrite는 하지 않으며, 동일 파일은 hash 비교 후 idempotent하게 처리합니다.
- 구조화 로그는 `/Users/geonha/lecture_stt/state/logs/downstream.jsonl`에 기록됩니다.

- 상태 확인 CLI
```bash
bash /Users/geonha/lecture_stt/scripts/distribute_status.sh
bash /Users/geonha/lecture_stt/scripts/distribute_status.sh list --only-problems
bash /Users/geonha/lecture_stt/scripts/distribute_status.sh show 260316LC_1
bash /Users/geonha/lecture_stt/scripts/distribute_status.sh clear 260316LC_1 --dry-run
bash /Users/geonha/lecture_stt/scripts/distribute_status.sh clear 260316LC_1 --yes
```
  - 기본값은 aggregate summary입니다.
  - `list --only-problems`는 `INCOMPLETE`, `BLOCKED`, `CONFLICT`, `ERROR` 위주로 최근 항목을 보여줍니다.
  - `show <stem>`은 `deliveries` row 전체를 JSON으로 출력합니다.
  - `clear <stem>`은 `deliveries` row만 삭제합니다. source/destination 파일은 건드리지 않습니다.
  - `clear`는 기본적으로 거부되며, 실제 삭제에는 `--yes`가 필요합니다.

## 6. 운영 확인
1. 폴더 파일 수 확인
```bash
ls -1 "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/00_inbox" | wc -l
ls -1 "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/01_audio" | wc -l
ls -1 "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/02_transcripts" | wc -l
ls -1 "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/99_errors" | wc -l
```

2. 최근 작업 확인
```bash
sqlite3 /Users/geonha/lecture_stt/state/jobs.sqlite3 "select id,status,orig_name,updated_at,error_message from jobs order by id desc limit 20;"
```

3. 상태 집계
```bash
sqlite3 /Users/geonha/lecture_stt/state/jobs.sqlite3 "select status,count(*) from jobs group by status order by status;"
```

4. 로그 확인
```bash
tail -f /Users/geonha/lecture_stt/logs/app.log
```

## 7. 운영 주의사항
- `launchd` 자동 실행과 웹 패널의 Start/Stop을 동시에 쓰면 중복 제어가 생깁니다.
- 웹 패널은 Tkinter를 사용하지 않습니다.
- `00_inbox`만 감시 대상이므로 다른 폴더를 쓰면 변환되지 않습니다.

## 8. 자주 발생하는 문제
- `watch_folder does not exist`  
  - `/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/00_inbox` 경로 확인
- `cannot write in paths.*`  
  - iCloud 권한/동기화 상태 확인
- `ffmpeg binary not found`  
  - `which ffmpeg`, `ffmpeg -version` 확인
- 웹 화면이 안 열릴 때  
  - 실행 터미널에 `Web control panel running at` 메시지 확인  
  - 브라우저에서 `127.0.0.1:8765` 접속
  - 포트 충돌이면 `WEB_PANEL_PORT` 변경
