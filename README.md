# Lecture STT Pipeline

처음 참여하는 사람과 AI 도구는 [시스템·운영 이력 인수인계서](docs/SYSTEM_HANDOFF.md)에서 처리 흐름, 주요 결정, 운영 확인 순서를 볼 수 있습니다.

**[강의 녹음이 학습 노트가 되기까지](docs/SYSTEM_OVERVIEW.md)** — Mac mini의 전사·품질 검사·교정 흐름과 대량 녹음 운영 사례를 정리했습니다. 아래는 `dev` 버전의 실행 안내입니다. Codex CLI 후처리를 사용하는 버전의 설정은 [운영 코드 README](https://github.com/altius03/lecture-stt-pipeline/blob/main/README.md)를 참고하세요.

현재 기준: `/Users/geonha/DEV/lecture_stt`가 운영 저장소입니다. 예전 `/Users/geonha/lecture_stt` 경로는 2026-05-20 runtime cleanup에서 archive로 보존된 legacy root이며 새 작업 기준이 아닙니다.

Remote: `https://github.com/altius03/lecture-stt-pipeline.git`
Branch: `main`
CI: `.github/workflows/ci.yml`에서 Python unittest/compileall과 web-panel test/build를 실행합니다.

## 1. 무엇을 하는 저장소인가

`lecture_stt`는 iCloud `lecture_recordings` 폴더를 기준으로 강의 녹음 파일을 처리하는 로컬 운영 파이프라인입니다.

1. `00_inbox`에 들어온 음성 파일을 감시합니다.
2. 안정 시간이 지난 파일을 `01_audio`로 보관하고 faster-whisper로 전사합니다.
3. raw transcript를 `02_transcripts/{stem}.txt`와 `{stem}.json`으로 저장합니다.
4. 품질 metadata가 있으면 `02_transcripts/{stem}.quality.json` sidecar를 저장합니다. 이 sidecar는 transcript 본문과 segment 배열을 포함하지 않습니다.
5. 사람이 검토한 correction/summary final artifact를 `03_correction`, `04_summarize`에 둡니다.
6. downstream worker가 GH archive와 Obsidian 목적지로 배포합니다.


## 2. 저장소 구조

```text
.
├── README.md                         # 이 파일: 빠른 진입점
├── AGENTS.md                         # 저장소 작업 규칙
├── config/
│   └── config.example.yaml           # 운영 config 템플릿; config.yaml은 gitignore
├── src/lecture_stt/
│   ├── stt/                          # STT 감시/전사/품질/알림 파이프라인
│   ├── correction/                   # manual correction 대기/helper 계층
│   ├── downstream/                   # correction/summary 배포 worker와 상태 CLI
│   ├── shared/                       # DB, path, utility 공용 모듈
│   └── ui/                           # 로컬 web panel backend
├── frontend/web-panel/               # Vite + React + TypeScript 운영 패널
├── scripts/
│   ├── run_worker.sh                 # STT worker 상시 실행
│   ├── run_once.sh                   # STT worker 1회 실행
│   ├── run_gui.sh                    # web panel 실행
│   ├── run_distribute.sh             # downstream worker 상시 실행
│   ├── distribute_once.sh            # downstream worker 1회 실행
│   ├── distribute_status.sh          # downstream 상태 확인
│   ├── cleanup.py                    # audio/transcript/tmp cleanup helper; 기본 dry-run
│   └── rotate_logs.py                # launchd plain log rotation helper; 기본 dry-run
├── docs/
│   ├── OPERATIONS.md                 # 현재 운영 runbook source of truth
│   ├── ARCHITECTURE.md               # 구조/흐름 문서
│   ├── MODELS.md                     # 모델/benchmark 정책
│   └── WORKLOG.md                    # 누적 변경 기록
├── launchd/                          # macOS LaunchAgent 템플릿
├── tests/                            # Python unittest suite
├── .github/workflows/ci.yml          # GitHub Actions CI
└── .codex/agents/                    # project-local Codex subagent 설정
```

Gitignore 대상 로컬 runtime/build artifact:

- `.venv/`, `frontend/web-panel/node_modules/`
- `frontend/web-panel/dist/`, `*.tsbuildinfo`, generated `vite.config.js`/`.d.ts`
- `state/`, `tmp/`, `models/`, `logs/`
- local `config/config.yaml`, `.env`, `.claude/`, `.idea/`
- root-level legacy runtime folders: `audio/`, `inbox/`, `transcripts/`, `errors/`

## 3. Runtime 데이터 위치

운영 config는 `config/config.yaml`에 두며 저장소에는 커밋하지 않습니다. 템플릿은 `config/config.example.yaml`입니다.

기본 운영 경계:

- iCloud lecture root: `${LECTURE_RECORDINGS_ROOT}`
  - `00_inbox`: 입력
  - `01_audio`: canonical audio 보관
  - `02_transcripts`: raw transcript와 quality sidecar
  - `03_correction`: final correction pair
  - `04_summarize`: final summary markdown
  - `05_prompt`: correction prompt source of truth
  - `99_errors`: terminal failure audio
- repo-local SQLite DB: `state/jobs.sqlite3`
- STT tmp/cache: `~/Library/Caches/lecture_stt/tmp`
- app/downstream/launchd logs: `~/Library/Logs/lecture_stt/`

## 4. 처음 설정

```bash
cd /Users/geonha/DEV/lecture_stt
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
```

`config/config.yaml`에는 최소한 아래 외부 경로 환경값이 해석되도록 설정합니다.

```bash
export LECTURE_RECORDINGS_ROOT="$HOME/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings"
export GH_CURRENT_SEMESTER_ROOT="/path/to/GH_archive/current-semester"
export OBSIDIAN_SEMESTER_ROOT="/path/to/Obsidian/current-semester"
```

알림 secret은 `.env`에 둡니다. `.env`는 커밋하지 않습니다.

```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
# TELEGRAM_MESSAGE_THREAD_ID=...
# DISCORD_WEBHOOK_URL=...
```

## 5. 자주 쓰는 명령

검증:

```bash
cd /Users/geonha/DEV/lecture_stt
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q scripts tests src
git diff --check
```

STT worker:

```bash
bash scripts/run_worker.sh          # 상시 실행
bash scripts/run_once.sh            # 1회 실행
bash scripts/run_worker.sh --pause
bash scripts/run_worker.sh --resume
bash scripts/run_worker.sh --status
```

Web panel:

```bash
bash scripts/run_gui.sh
# browser: http://127.0.0.1:8765
```

React panel 개발/검증:

```bash
cd frontend/web-panel
npm install
npm test
npm run build
npm run dev
```

Downstream worker:

```bash
bash scripts/distribute_once.sh --dry-run
bash scripts/distribute_once.sh
bash scripts/run_distribute.sh
bash scripts/distribute_status.sh
bash scripts/distribute_status.sh list --only-problems
bash scripts/distribute_status.sh show 260316LC_1
```

LaunchAgent 등록/재등록은 운영 영향이 있으므로 `docs/OPERATIONS.md`의 절차와 현재 상태를 먼저 확인한 뒤 실행합니다.

```bash
bash scripts/setup_launchd.sh
```

## 6. 운영 문서 우선순위

1. `docs/OPERATIONS.md`: 현재 운영 runbook
2. `docs/ARCHITECTURE.md`: 시스템 구조와 실행 흐름
3. `docs/WORKLOG.md`: 변경 이력
4. 날짜가 붙은 handoff/inventory/decision 문서: 과거 의사결정 기록. 현재 판단은 위 문서를 우선합니다.

## 7. 안전 원칙

- raw transcript 본문을 chat, stdout, report, worklog에 그대로 남기지 않습니다.
- final artifact overwrite는 하지 않습니다. 기존 파일과 내용이 다르면 conflict로 남깁니다.
- cleanup, DB row 삭제, launchd restart, model/provider 변경은 현재 상태와 rollback 계획을 확인한 뒤 수행합니다.
- `.venv/`, `node_modules/`, `state/`, `dist/`, iCloud/GH archive/Obsidian runtime payload는 커밋하지 않습니다.
- 저장소 변경 전후에는 `git status --short --branch`, `git diff --check`, 테스트/compileall을 확인합니다.
