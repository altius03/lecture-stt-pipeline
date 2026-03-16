# Lecture STT 운영 가이드

## 1. 시스템 개요
- 업로드된 음성 파일을 자동으로 텍스트로 변환합니다.
- 기본 감시 폴더: `00_inbox`
- 출력 폴더: `02_transcripts`
- 실패 폴더: `99_errors`
- 관리 폴더
  - `01_audio`: 변환 전 임시 원본 보관
  - `03_correction`: LLM 교정 결과
  - `04_summarize`: LLM 요약 결과
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

3. Discord 알림 사용 시(선택)
```bash
cat > /Users/geonha/lecture_stt/.env <<'EOF'
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
EOF
```

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

## 5. 실행 방법

### 5.1 수동 실행(터미널)
- 시작
```bash
/Users/geonha/lecture_stt/.venv/bin/python /Users/geonha/lecture_stt/src/main.py --config /Users/geonha/lecture_stt/config/config.yaml
```

- 1회 실행(테스트)
```bash
bash /Users/geonha/lecture_stt/scripts/run_once.sh
```

- 일시정지/재개/상태 확인
```bash
/Users/geonha/lecture_stt/.venv/bin/python /Users/geonha/lecture_stt/src/main.py --pause
/Users/geonha/lecture_stt/.venv/bin/python /Users/geonha/lecture_stt/src/main.py --resume
/Users/geonha/lecture_stt/.venv/bin/python /Users/geonha/lecture_stt/src/main.py --status
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
- 포트 변경
```bash
WEB_PANEL_HOST=127.0.0.1 WEB_PANEL_PORT=8765 bash /Users/geonha/lecture_stt/scripts/run_gui.sh
```

- 원격 사용은 SSH 터널을 권장합니다.

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
```
  - 기본값은 aggregate summary입니다.
  - `list --only-problems`는 `INCOMPLETE`, `BLOCKED`, `CONFLICT`, `ERROR` 위주로 최근 항목을 보여줍니다.
  - `show <stem>`은 `deliveries` row 전체를 JSON으로 출력합니다.

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
