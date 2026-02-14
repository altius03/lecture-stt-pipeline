# Lecture STT Pipeline

## 개요

`lecture_stt`는 iPhone/Voice Memo 등으로 업로드된 강의 음성 파일을 수신해 자동으로 텍스트로 변환하는 macOS 상시 동작 파이프라인입니다.

- `00_inbox` 폴더를 감시합니다.
- 파일이 안정 상태(90초 이상 크기/수정 시각 변화 없음)가 되면 처리 대상으로 간주합니다.
- 파일을 `01_audio`로 이동·이름 정규화 후 `faster-whisper`로 전사합니다.
- 결과를 `txt`/`json`으로 저장합니다.
- SQLite(`jobs` 테이블)에 작업 상태를 기록합니다.

실행 모드는 두 가지입니다.

- 항상 실행: `launchd`(KeepAlive)로 백그라운드 데몬화
- 1회 테스트: `--once`

추가 운영 특징:

- 수동 정지/재개: 프로세스를 죽이지 않고 `--pause`, `--resume` 플래그로 제어
- 엄격한 시작 검증: `config/config.yaml`이 유효하지 않으면 즉시 종료(예: 예전처럼 예시 파일로 fallback 없음)

---

## 경로/디렉터리

- 작업 디렉터리(워크트리): `/Users/geonha/lecture_stt`
- 감시 대상(인박스): `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/00_inbox`
- 안정 상태 저장(오디오): `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/01_audio`
- 전사 결과 저장: `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/02_transcripts`
- 실패 처리 저장: `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/99_errors`
- DB: `/Users/geonha/lecture_stt/state/jobs.sqlite3`
- 임시 파일: `/Users/geonha/lecture_stt/tmp`
- ffmpeg: `/opt/homebrew/bin/ffmpeg`
- 가상환경: `/Users/geonha/lecture_stt/.venv`
- 일시정지 플래그: `/Users/geonha/lecture_stt/state/paused`
- launchd 로그:
  - `/Users/geonha/lecture_stt/state/logs/launchd.out.log`
  - `/Users/geonha/lecture_stt/state/logs/launchd.err.log`

---

## STT 기본 파라미터(현재 동작 기준)

파이프라인 기본값은 코드/설정에서 다음과 같이 사용됩니다.

- `faster-whisper`
  - `model_size`: `large-v3`
  - `device`: `cpu`
  - `compute_type`: `int8`
  - `language`: `ko`
  - `task`: `transcribe`
  - `beam_size`: `5`
  - `vad_filter`: `false`
  - `word_timestamps`: `false`

---

## 환경 구성 (config/config.yaml 필수, fail-fast)

`config/config.yaml`은 필수이며, 예시 파일로 대체되지 않습니다.

- `config/config.yaml`이 없으면 즉시 실패
- 비어 있으면(0 bytes) 즉시 실패
- YAML 파싱 실패하면 즉시 실패
- 필수 키 검증 실패(`app`, `paths`, `ffmpeg`, `transcribe`) 시 즉시 실패
- 수치 검증 실패(음수/0/형식 오류) 시 즉시 실패
- 파일시스템 점검:
  - `watch_folder` 존재 확인
  - 출력 디렉터리/임시 디렉터리 생성/쓰기 가능 여부 확인
  - `ffmpeg` 존재 + 실행권한 확인
  - DB 부모 디렉터리 및 `state/logs` 생성/쓰기 가능 여부 확인

최초 설정은 아래와 같이 시작합니다.

```bash
cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

`/Volumes/...` 경로는 외부 볼륨이므로 마운트되지 않으면 검증에서 실패합니다. 실패 상태에서 launchd는 재시작을 반복할 수 있습니다.

```bash
# .env
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

---

## 실행 방법

### A) 1회 실행 (`--once`)

```bash
bash scripts/run_once.sh
```

또는 아래처럼 직접 실행:

```bash
/Users/geonha/lecture_stt/.venv/bin/python /Users/geonha/lecture_stt/src/main.py --once --config /Users/geonha/lecture_stt/config/config.yaml
```

### B) 터미널에서 데몬(수동 실행)

```bash
/Users/geonha/lecture_stt/.venv/bin/python /Users/geonha/lecture_stt/src/main.py --config /Users/geonha/lecture_stt/config/config.yaml
```

### C) launchd (항상 실행 권장)

```bash
plist=/Users/geonha/lecture_stt/launchd/com.geonha.lecture-stt.plist
uid=$(id -u)

launchctl bootout "gui/$uid" "$plist" 2>/dev/null || true
launchctl bootstrap "gui/$uid" "$plist"
launchctl kickstart -k "gui/$uid/com.geonha.lecture-stt"
```

상태 확인:

```bash
launchctl print "gui/$(id -u)/com.geonha.lecture-stt" >/dev/null 2>&1 && echo LOADED || echo NOT
pgrep -af "/Users/geonha/lecture_stt/src/main.py" || echo "not running"
```

로그 확인:

```bash
tail -f /Users/geonha/lecture_stt/state/logs/launchd.out.log
tail -f /Users/geonha/lecture_stt/state/logs/launchd.err.log
```

`launchd`는 PATH가 제한될 수 있으므로 `python`, `config`, `ffmpeg` 경로는 plist에서 모두 절대 경로로 지정되어 있습니다.

---

## Pause / Resume / Status

CLI 제어:

```bash
/Users/geonha/lecture_stt/.venv/bin/python /Users/geonha/lecture_stt/src/main.py --pause
/Users/geonha/lecture_stt/.venv/bin/python /Users/geonha/lecture_stt/src/main.py --resume
/Users/geonha/lecture_stt/.venv/bin/python /Users/geonha/lecture_stt/src/main.py --status
```

동작 설명:

- `--pause`는 `/Users/geonha/lecture_stt/state/paused` 파일을 생성합니다.
  - 파이프라인은 실행 중인 프로세스를 종료하지 않고 스캔/처리를 멈춥니다.
  - 처리 중 작업이 있다면 마무리하고 그 후 유휴 상태에서 멈춥니다.
- `--resume`은 플래그 파일을 삭제해 재개합니다.
- `--status`는 현재 상태(`PAUSED/RESUMED`)를 출력하고 종료합니다.
- 파이프라인이 `--paused` 상태일 때 `--once`를 호출하면 다음 메시지를 출력하고 즉시 종료합니다.

```text
Paused: skipping --once
```

---

## Discord 알림 (성공/실패)

`discord` 알림은 `.env`의 `DISCORD_WEBHOOK_URL`이 설정되어 있을 때만 전송됩니다.
웹훅 URL이 비어 있으면 알림은 건너뛰고 파이프라인은 계속 동작합니다.

- 실패 알림: 작업이 `ERROR` 상태가 되면 전송됩니다.
  - 파일명/캐노니컬명/경로/실패 사유를 포함합니다.
- 성공 알림: 작업이 `DONE` 상태로 전환되고 `txt`, `json` 출력이 저장된 뒤 전송됩니다.
- 알림 실패(네트워크/웹훅 에러)는 파이프라인을 중단하지 않고 로그만 남기고 계속 동작합니다.
- 중복 전송 방지 marker:
  - `/Users/geonha/lecture_stt/state/notified/success_<job_id>`
  - `/Users/geonha/lecture_stt/state/notified/error_<job_id>`

컨텐츠가 길 경우(예: 긴 예외 메시지)에는 전송 전에 길이 제한(최대 1900자, `...(생략)` 포함)으로 잘라서 보냅니다.

---

## 운영 모니터링

- launchd 로드/프로세스 확인
- 로그 추적
- DB 상태 확인

```bash
launchctl print "gui/$(id -u)/com.geonha.lecture-stt" >/dev/null 2>&1 && echo LOADED || echo NOT
pgrep -af "/Users/geonha/lecture_stt/src/main.py" || echo "not running"

tail -f /Users/geonha/lecture_stt/state/logs/launchd.out.log
tail -f /Users/geonha/lecture_stt/state/logs/launchd.err.log

tail -f /Users/geonha/lecture_stt/logs/app.log
```

SQLite 최근 상태 조회:

```bash
sqlite3 /Users/geonha/lecture_stt/state/jobs.sqlite3 "select id,status,orig_name,updated_at,error_message from jobs order by id desc limit 5;"
```

watch 대체 루프:

```bash
while true; do
  clear
  date
  sqlite3 /Users/geonha/lecture_stt/state/jobs.sqlite3 "select id,status,orig_name,updated_at from jobs order by id desc limit 20;"
  sleep 2
done
```

폴더 기반 단계 확인:

- inbox: `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/00_inbox`
- audio: `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/01_audio`
- transcripts: `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/02_transcripts`
- errors: `/Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/99_errors`

---

## 문제 해결(트러블슈팅)

### 1) `config/config.yaml`이 0바이트

```bash
cp -f /Users/geonha/lecture_stt/config/config.example.yaml /Users/geonha/lecture_stt/config/config.yaml
```

### 2) 외부 볼륨 미마운트

`watch_folder`가 없으면 시작 검증에서 실패합니다.

```bash
ls -la /Volumes/geonha/GH_archive/01_TUK/06_lecture_recordings/00_inbox
```

### 3) ffmpeg 경로/권한

```bash
ls -l /opt/homebrew/bin/ffmpeg
/opt/homebrew/bin/ffmpeg -version
```

### 4) launchd PATH 이슈

`launchd`는 로그인 셸과 PATH가 다를 수 있으므로 plist는 python/config/ffmpeg 경로를 절대 경로로 사용하고 있습니다.
필요 시 plist의 `ProgramArguments`(python), `WorkingDirectory`, 환경변수를 재확인하세요.

### 5) launchd가 반복 재시작하는 경우

`config` 검증 실패가 원인일 수 있습니다. 최근 로그를 먼저 확인하세요.

```bash
tail -n 100 /Users/geonha/lecture_stt/state/logs/launchd.err.log
```
