# Lecture STT Pipeline

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
5. 사람이 검토했거나 Hermes operator가 만든 correction/summary final artifact를 `03_correction`, `04_summarize`에 둡니다.
6. downstream worker가 GH archive와 Obsidian 목적지로 배포합니다.

앱 내부에는 Claude/Anthropic 같은 API-backed correction provider를 다시 넣지 않습니다. 자동 교정/요약은 별도 Hermes cron/operator 계층에서 수행하고, repo 안 코드는 후보 선택, prompt loading, staging, validation, promote safety 같은 결정적 보조 기능만 제공합니다.

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
│   ├── rotate_logs.py                # launchd plain log rotation helper; 기본 dry-run
│   └── hermes_postprocess/           # Hermes postprocess operator helper package
├── docs/
│   ├── OPERATIONS.md                 # 현재 운영 runbook source of truth
│   ├── ARCHITECTURE.md               # 구조/흐름 문서
│   ├── MODELS.md                     # 모델/benchmark 정책
│   ├── WORKLOG.md                    # 누적 변경 기록
│   └── operators/hermes-postprocess/ # Hermes operator runbook/prompt/output contract
├── launchd/                          # macOS LaunchAgent 템플릿
├── tests/                            # Python unittest suite
├── .github/workflows/ci.yml          # GitHub Actions CI
├── .codex/agents/                    # project-local Codex subagent 설정
└── .hermes/plans/                    # 승인/작업 계획 기록
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
- Hermes operator staging/report: `state/hermes_postprocess/`, `state/reports/`

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

## 6. Hermes postprocess operator

Repo-local helper package:

```bash
python3 -m scripts.hermes_postprocess dry-run --help
python3 -m scripts.hermes_postprocess validate-correction --help
python3 -m scripts.hermes_postprocess validate-summary --help
python3 -m scripts.hermes_postprocess record-misrecognitions --help
python3 -m scripts.hermes_postprocess promote --help
```

핵심 안전 규칙:

- `dry-run`은 후보 metadata/action plan만 출력하고 raw transcript body나 segment 배열을 출력하지 않습니다.
- staging manifest와 validation report도 metadata-only입니다.
- `promote`는 기본적으로 비활성화되어 있으며, final write에는 `--allow-promote`가 필요합니다.
- existing final output overwrite, backlog bulk processing, `05_prompt` 자동 수정은 금지합니다.
- 현재 Hermes cron job은 `lecture_stt_postprocess_operator`이며, activation-time backlog는 `state/hermes_postprocess/cron-baseline.json`으로 건너뜁니다. no-candidate tick은 stdout 없이 조용히 종료합니다.

자세한 운영 규칙은 `docs/operators/hermes-postprocess/README.md`와 `operator-runbook.md`를 봅니다.

## 7. 운영 문서 우선순위

1. `docs/OPERATIONS.md`: 현재 운영 runbook
2. `docs/ARCHITECTURE.md`: 시스템 구조와 실행 흐름
3. `docs/operators/hermes-postprocess/`: Hermes operator 규칙
4. `docs/WORKLOG.md`: 변경 이력
5. 날짜가 붙은 handoff/inventory/decision 문서: 과거 의사결정 기록. 현재 판단은 위 문서를 우선합니다.

## 8. 안전 원칙

- raw transcript 본문을 chat, stdout, report, worklog에 그대로 남기지 않습니다.
- final artifact overwrite는 하지 않습니다. 기존 파일과 내용이 다르면 conflict로 남깁니다.
- cleanup, DB row 삭제, launchd restart, cron 변경, model/provider 변경은 현재 상태와 rollback 계획을 확인한 뒤 수행합니다.
- `.venv/`, `node_modules/`, `state/`, `dist/`, iCloud/GH archive/Obsidian runtime payload는 커밋하지 않습니다.
- 저장소 변경 전후에는 `git status --short --branch`, `git diff --check`, 테스트/compileall을 확인합니다.
