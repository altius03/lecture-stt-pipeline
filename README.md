# Lecture STT Pipeline

현재 기준: `/Users/geonha/DEV/lecture_stt`가 운영 저장소입니다. 예전 `/Users/geonha/lecture_stt` 경로는 2026-05-20 runtime cleanup에서 archive로 보존된 legacy root이며 새 작업 기준이 아닙니다.

Remote: `https://github.com/altius03/lecture-stt-pipeline.git`
Branch: `main`
CI: `.github/workflows/ci.yml`에서 Python unittest/compileall과 web-panel test/build를 실행합니다.

## 1. 무엇을 하는 저장소인가

`lecture_stt`는 iCloud `lecture_recordings` 폴더를 기준으로 강의 녹음 파일을 처리하는 로컬 운영 파이프라인입니다.

1. `00_inbox`에 들어온 음성 파일을 감시합니다.
2. 안정 시간이 지난 파일을 `01_audio`로 보관하고 faster-whisper로 전사합니다.
3. 과목이 확정된 raw transcript를 `02_transcripts/<학기>/<과목폴더>/{stem}.txt`와 `{stem}.json`으로 저장합니다.
4. 품질 metadata가 있으면 같은 과목 폴더에 `{stem}.quality.json` sidecar를 저장합니다. 이 sidecar는 transcript 본문과 segment 배열을 포함하지 않습니다.
5. 품질 상태가 `DONE`인 전사만 활성 학기 manifest의 과목 route와 활성화 cutoff를 고정해 후처리 큐에 넣습니다. `NEEDS_REVIEW`는 자동 처리하지 않습니다.
6. downstream worker가 저장된 ChatGPT 로그인으로 `codex exec`를 호출해 교정 TXT/JSON과 요약 Markdown을 생성합니다.
7. 교정본은 과목의 `06_lecture_notes/02_origin`, 요약본은 `06_lecture_notes/01_summarize`로 무덮어쓰기 전달합니다.

Hermes와 그 cron/operator 경로는 영구 폐기했습니다. 2026-08-09에는 사용자 영역의 `ai.hermes.*` LaunchAgent 4개, `~/.hermes`, Web UI clone, 실행 wrapper와 앱 상태도 활성 경로에서 제거했습니다. 과거 worklog·감사 report·비활성 lab 자료에 남은 Hermes 문자열은 실행 경로가 아닙니다. 현재 후처리는 별도 LLM API 키나 직접 API 호출 없이 Codex CLI를 사용하지만, 교정·요약을 위해 전사 내용은 Codex 서비스로 전송됩니다. STT 원본은 정본으로 보존하며, 후처리 실패가 전사 완료 상태를 되돌리지 않도록 별도 SQLite 큐와 launchd worker를 사용합니다.

## 2. 저장소 구조

```text
.
├── README.md                         # 이 파일: 빠른 진입점
├── AGENTS.md                         # 저장소 작업 규칙
├── config/
│   ├── config.example.yaml           # 운영 config 템플릿; config.yaml은 gitignore
│   └── semesters/                    # 학기별 검증 대상 manifest
├── src/lecture_stt/
│   ├── stt/                          # STT 감시/전사/품질/알림 파이프라인
│   ├── correction/                   # 과거 manual correction helper
│   ├── downstream/                   # 학기 activation, 전사 전달 worker와 상태 CLI
│   ├── storage_v2/                   # record manifest, 비파괴 importer, snapshot/verifier
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
│   ├── import_storage_v2.py          # legacy read-only plan / 명시적 v2 copy importer
│   └── rotate_logs.py                # launchd plain log rotation helper; 기본 dry-run
├── docs/
│   ├── OPERATIONS.md                 # 현재 운영 runbook source of truth
│   ├── ARCHITECTURE.md               # 구조/흐름 문서
│   ├── STORAGE_V2.md                 # record-centric 보관소/manifest 설계
│   ├── MODELS.md                     # 모델/benchmark 정책
│   └── WORKLOG.md                    # 누적 변경 기록
├── launchd/                          # macOS LaunchAgent 템플릿
├── migrations/v2/                    # 운영 DB와 분리된 additive storage v2 migration
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
  - `02_transcripts/<학기>/<과목폴더>`: 과목이 확정된 raw transcript와 quality sidecar
  - `02_transcripts` 루트: 기존 평면 artifact와 과목 미확정/비강의 transcript의 호환 위치
  - `99_errors`: terminal failure audio
- Obsidian/GH archive: 활성 학기 manifest의 `vault_root`, `semester_root`, course route
- 교정 staging: `${LECTURE_RECORDINGS_ROOT}/03_correction/<학기>/<과목폴더>/{stem}.{txt,json}`
- 요약 staging: `${LECTURE_RECORDINGS_ROOT}/04_summarize/<학기>/<과목폴더>/{stem}.md`
- 교정 최종본: `<course>/06_lecture_notes/02_origin/{stem}.{txt,json}`
- 요약 최종본: `<course>/06_lecture_notes/01_summarize/{stem}.md`
- repo-local SQLite DB: `state/jobs.sqlite3`
- 활성 학기 snapshot: `state/active-semester.json`
- STT tmp/cache: `~/Library/Caches/lecture_stt/tmp`
- app/transcript-delivery/launchd logs: `~/Library/Logs/lecture_stt/`

`02_transcripts`, `03_correction`, `04_summarize`의 새 강의 artifact는 active semester의 `semester/course_dir` 아래에 함께 정렬됩니다. 기존 루트 직속 파일과 이미 pin된 queue row는 이동하지 않고 계속 읽습니다. 과목을 확정할 수 없는 원본은 루트에 보존하고 `UNROUTED`로 격리하며, 기존 파일은 어느 단계에서도 덮어쓰지 않습니다. 과거 `05_prompt`와 Hermes 상태는 감사용 역사 자료일 뿐 현재 런타임에서 사용하지 않습니다.

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
```

Obsidian 및 학기 경로는 환경변수 두 곳에 중복 작성하지 않고 `config/semesters/<학기>.yaml` 하나에서 관리합니다.

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
bash scripts/distribute_status.sh show <source_job_id>
```

`distribute_once.sh --dry-run`은 운영 DB를 SQLite read-only mode로 열어 새 `DONE` 건의 예상 과목·파일명과 기존 `PENDING` 전달 상태만 계산합니다. 큐/스키마, lock, JSONL, 목적 파일은 만들거나 갱신하지 않습니다.

Storage v2 비파괴 계획:

```bash
.venv/bin/python scripts/import_storage_v2.py plan \
  --legacy-db /path/to/standalone/jobs.snapshot.sqlite3 \
  --legacy-root "$LECTURE_RECORDINGS_ROOT" \
  --json
```

이 명령은 legacy DB와 파일을 읽기만 하며 v2 DB나 `records`를 만들지 않습니다. 활성 WAL/journal이 있는 운영 DB는 열지 않으므로, 먼저 SQLite backup API로 만든 독립 snapshot을 사용해야 합니다. 실제 copy에는 legacy root 밖의 별도 v2 DB/root, plan 출력의 `--expected-count`와 `--expected-plan-sha256`, `--allow-write`가 필요합니다. 자세한 계약은 `docs/STORAGE_V2.md`를 따릅니다.

Ownerless delivery와 현재 archive의 경로·해시를 변경 없이 대조:

```bash
.venv/bin/python scripts/import_storage_v2.py reconcile-archive \
  --legacy-db /path/to/standalone/jobs.snapshot.sqlite3 \
  --legacy-root "$LECTURE_RECORDINGS_ROOT" \
  --config config/config.yaml \
  --json
```

과거 recorded 절대경로가 현재 archive root 밖이면 직접 열지 않고, 현재 과목 route에서 계산한 경로만 검증합니다. 저장 SHA와 현재 파일이 다르거나 `MISSING` 상태인데 현재 파일이 발견되면 non-zero로 종료해 자동 cut-over를 막습니다.

정본을 고르지 않은 채 ownerless archive revision을 별도 evidence store에 보존:

```bash
.venv/bin/python scripts/import_storage_v2.py plan-archive-evidence \
  --legacy-db /path/to/standalone/jobs.snapshot.sqlite3 \
  --legacy-root "$LECTURE_RECORDINGS_ROOT" \
  --config config/config.yaml \
  --historical-root 'old_obsidian=/explicitly/allowed/historical/root' \
  --logical-stem 260430DStr_2 \
  --json

.venv/bin/python scripts/import_storage_v2.py apply-archive-evidence \
  --legacy-db /path/to/standalone/jobs.snapshot.sqlite3 \
  --legacy-root "$LECTURE_RECORDINGS_ROOT" \
  --config config/config.yaml \
  --historical-root 'old_obsidian=/explicitly/allowed/historical/root' \
  --logical-stem 260430DStr_2 \
  --v2-db /separate/path/storage-v2.sqlite3 \
  --evidence-root /separate/path/archive-evidence \
  --expected-count 1 \
  --expected-plan-sha256 <plan 출력값> \
  --allow-write \
  --json
```

`blocked` reconciliation은 복사 금지가 아니라 정본 자동 선택 금지다. 안전하게 다시 검증된 현재본과 명시적으로 허용한 과거 root의 파일은 content-addressed revision으로 함께 보존하고, case는 녹음에 귀속하지 않은 `open` review 상태로 남긴다. 운영 DB/root에는 아직 적용하지 않는다.
Capture snapshot과 observation metadata는 닫힌 allowlist를 사용한다. 해시는 canonical SHA-256, artifact 경로는 절대경로·확장자 계약, stat은 고정 정수 필드만 허용하며 자유 형식 issue message나 transcript/correction/summary 본문은 저장하지 않는다.

LaunchAgent 등록/재등록은 운영 영향이 있으므로 `docs/OPERATIONS.md`의 절차와 현재 상태를 먼저 확인한 뒤 실행합니다.

```bash
bash scripts/setup_launchd.sh
```

## 6. 학기 전환과 자동 전달

학기 snapshot을 바꾸기 전에는 진행 중인 STT(`PENDING`/`PROCESSING`/재시도)
와 미완료 postprocess가 0인지 확인합니다. `apply`도 jobs DB에
`BEGIN IMMEDIATE` 잠금을 잡고 같은 조건과 현재 cutoff 이후 `DONE`이지만 아직
후처리 queue에 들어가지 않은 좁은 완료 구간을 다시 검사하므로, 확인 뒤 새
작업이 끼어드는 경우에는 snapshot을 바꾸지 않고 실패합니다.

후보 manifest를 먼저 읽기 전용 검증합니다.

```bash
PYTHONPATH=src .venv/bin/python -m lecture_stt.downstream.semester plan \
  --manifest config/semesters/2026-2.yaml
```

출력된 course count와 plan SHA-256을 그대로 넣어 활성 snapshot을 원자 교체합니다.

```bash
PYTHONPATH=src .venv/bin/python -m lecture_stt.downstream.semester apply \
  --manifest config/semesters/2026-2.yaml \
  --active-path state/active-semester.json \
  --jobs-db-path state/jobs.sqlite3 \
  --expected-course-count 7 \
  --expected-plan-sha256 <plan 출력값> \
  --allow-write
```

activation은 Obsidian vault marker, root containment, symlink 부재, 7개 과목 폴더의 `01_summarize`와 `02_origin`, 선택된 Storage v2 시간표를 모두 확인합니다. 누락 폴더는 자동 생성하지 않습니다.

학기마다 manifest를 새 시간표와 과목 폴더에 맞게 갱신하고 plan/apply하면 됩니다.
별도 cutoff 설정은 없으며 새 snapshot의 `activated_at`이 과거 `DONE` backlog를
차단하는 단일 기준입니다. 새 raw/교정/요약 artifact의 iCloud 하위 경로도 같은
snapshot의 `<학기>/<과목폴더>`에서 파생됩니다. Codex 로그인과 dry-run을
확인하면 실행 중인 워커는 다음 scan부터 새 snapshot을 사용합니다.

```bash
/Users/geonha/.local/bin/codex login status
bash scripts/distribute_status.sh
bash scripts/distribute_once.sh --dry-run
```

파일명 route는 `YYMMDDAlias[_N]` 형식입니다. 2026-2 별칭은 `CA`, `CN`, `DB`, `IS`, `IT`, `HMS`, `WLT`입니다. 별칭이 없으면 `lecture` 프로필에서만 녹음 시각과 시간표의 유일한 수업을 비교합니다.

## 7. 운영 문서 우선순위

1. `docs/OPERATIONS.md`: 현재 운영 runbook
2. `docs/ARCHITECTURE.md`: 시스템 구조와 실행 흐름
3. `docs/STORAGE_V2.md`: 차기 보관소·manifest·무삭제 이관 계약
4. `docs/WORKLOG.md`: 변경 이력
5. 날짜가 붙은 handoff/inventory/decision 문서: 과거 의사결정 기록. 현재 판단은 위 문서를 우선합니다.

## 8. 안전 원칙

- raw transcript 본문을 chat, stdout, report, worklog에 그대로 남기지 않습니다.
- final artifact overwrite는 하지 않습니다. 기존 파일과 내용이 다르면 conflict로 남깁니다.
- cleanup, DB row 삭제, launchd restart, 학기 activation, model 변경은 현재 상태와 rollback 계획을 확인한 뒤 수행합니다.
- `.venv/`, `node_modules/`, `state/`, `dist/`, iCloud/GH archive/Obsidian runtime payload는 커밋하지 않습니다.
- 저장소 변경 전후에는 `git status --short --branch`, `git diff --check`, 테스트/compileall을 확인합니다.
