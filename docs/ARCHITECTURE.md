# Lecture STT Architecture

기준일: 2026-05-20

## 목적
- iCloud inbox에 들어오는 강의 음성 파일을 자동으로 전사한다.
- 전사 결과를 TXT/JSON과 metadata-only quality sidecar로 저장한다.
- 후단 correction/summary 산출물을 별도 배포하고, Hermes operator가 교정/요약 자동화를 앱 외부 계층에서 수행할 수 있게 한다.

## 최상위 디렉터리
- `src/`: 애플리케이션 본체
- `frontend/`: React 웹 패널 소스(Vite 기반)
- `scripts/`: 수동 실행, 설치, 운영 보조 스크립트
- `scripts/hermes_postprocess/`: Hermes cron/operator용 repo-local 후보 discovery, prompt loading, validator, review-only misrecognition queue, staging manifest, explicit promote helper
- `launchd/`: macOS launchd 서비스 정의 템플릿
- `config/`: YAML 설정 템플릿. 운영 `config.yaml`은 gitignore 대상이다.
- `tests/`: Python unittest suite
- `docs/`: 운영 runbook, architecture, worklog, model/operator 문서
- `.github/workflows/`: GitHub Actions CI
- `.codex/agents/`: project-local Codex subagent 설정
- `.hermes/plans/`: 승인/작업 계획 기록
- `state/`: gitignored repo-local SQLite DB, operator staging, reports, lock/pause files
- `tmp/`: gitignored legacy/local fallback tmp. 현재 운영 STT tmp/cache 기본값은 `~/Library/Caches/lecture_stt/tmp`다.

## 현재 소스 구조
- 실제 구현 코드는 `src/lecture_stt/` 패키지 아래에 정리되어 있다.
- `src/lecture_stt/stt/`: 메인 STT 파이프라인
- `src/lecture_stt/shared/`: 공용 DB, 유틸
- `src/lecture_stt/correction/`: 수동/provider-neutral correction 대기 상태 조회와 correction prompt helper
- `src/lecture_stt/downstream/`: correction/summary 배포 파이프라인
- `src/lecture_stt/ui/`: 웹 제어판
- 운영 스크립트는 `PYTHONPATH=<repo>/src python -m lecture_stt...` 방식으로 패키지를 직접 실행한다.

## 메인 STT 파이프라인
- 메인 진입점 모듈은 `src/lecture_stt/stt/main.py`다.
- `load_config()`와 `validate_config()`가 설정 파일을 읽고 경로, ffmpeg, 쓰기 권한을 검증한다.
- `STTPipeline`이 전체 작업을 오케스트레이션한다.
- `PollingWatcher`가 inbox 폴더를 polling하면서 일정 시간 이상 변하지 않은 파일만 안정 파일로 판단한다.
- 안정 파일은 먼저 로컬 `tmp/inbox_staging`으로 선점 이동한 뒤 `01_audio`로 옮겨, iCloud rename/sync 영향이 전사 중간 단계로 번지지 않게 한다.
- 워커 시작 시 `tmp/inbox_staging`에 남아 있던 중단 파일과 `01_audio`에만 남은 pre-claim pending 오디오는 다시 inbox로 되돌려 재처리하고, 대응되는 stale job row도 정리한다.
- 같은 SHA-256의 완료 작업이 있으면 기존 결과를 복제해 dedupe 처리한다.
- 중복이 아니면 `STTWorker`가 ffmpeg로 WAV 전처리 후 faster-whisper 전사를 수행한다.
- 전사 결과는 `postprocess()`로 반복/노이즈/오인식 용어를 정리한다.
- `quality_gate.evaluate()`가 반복도 기반 건강도를 계산해 메타데이터에 포함한다.
- 최종 산출물은 `02_transcripts` 아래 `{base}.txt`, `{base}.json`으로 저장되고, 품질 메타데이터가 있으면 `{base}.quality.json` scorecard sidecar도 함께 저장된다. Scorecard는 transcript 본문과 segment 배열을 제외한 metadata-only artifact다.
- STT 실행 실패는 기본 2회까지 retryable 상태(`전사 재시도 대기 n/2`)로 DB에 남기고, 다음 scan에서 즉시 재시도한다.
- retry 한도 초과 후 terminal failure가 되면 오디오는 `99_errors`로 이동하고 DB 상태는 `ERROR`로 기록된다.
- DB `engine_params`에는 `transcription_failures`, `transcription_max_retries`, `last_error_message`를 secret-redacted 형태로 남긴다.
- 알림 계층은 `notification.provider` 설정과 `.env` secret을 기준으로 Telegram 또는 Discord provider를 선택한다.
- `notification.dual_send_providers`를 사용하면 전환 기간 동안 다중 채널 shadow 전송을 할 수 있다.

## 메인 파이프라인 핵심 모듈
- `src/lecture_stt/stt/main.py`: 설정 로드, 파이프라인 오케스트레이션, 상태 전이, 복구
- `src/lecture_stt/stt/watcher.py`: 안정 파일 감시
- `src/lecture_stt/stt/transcribe.py`: ffmpeg 전처리, Whisper 전사
- `src/lecture_stt/stt/postprocess.py`: 반복/점 노이즈 제거, 용어 교정
- `src/lecture_stt/stt/quality_gate.py`: 품질 보고서와 metadata-only quality scorecard 생성
- `src/lecture_stt/stt/notifier.py`: Telegram/Discord notifier, 중복 방지 마커, provider 팩토리
- `src/lecture_stt/shared/utils.py`: 파일 이동, atomic write, hash, pause flag 등 공용 함수
- 메인 워커는 `state/stt.lock` 파일 락으로 단일 인스턴스를 보장하고, claim 전에 원본이 사라진 경우는 다른 워커 선점 또는 외부 rename 가능성으로 보고 경고 후 skip한다.
- scheduled cleanup은 `tmp/`를 정리하되 `tmp/inbox_staging`은 보존해 복구 대기 중인 claimed input을 삭제하지 않는다.

## 상태 저장
- `src/lecture_stt/shared/db.py`가 SQLite 스키마와 접근 로직을 담당한다.
- `jobs` 테이블은 STT 메인 파이프라인 상태를 저장한다.
- 주요 상태는 `PENDING`, `PROCESSING`, `DONE`, `ERROR`다.
- 진행률, ETA, dedupe 정보, 에러 메시지, 처리 시간까지 함께 저장한다.
- 시작 시 `recover_processing_jobs()`가 중단된 `PROCESSING` 작업을 복구한다.

## 제어 계층
- 현재 주 제어 UI는 `src/lecture_stt/ui/web_panel.py`다.
- 웹 제어 백엔드 상태/워커 제어 로직은 `src/lecture_stt/ui/web_panel_state.py`로 분리되어 있다.
- 내장 HTTP 서버가 워커 시작, 일시정지, 재개, 중지, 로그 tail, DB 상태 조회를 제공한다.
- 웹 제어 계층은 `config.yaml`의 `notification` 섹션도 수정할 수 있으며, 패널에서 텔레그램/디스코드/둘 다/끄기 선택을 저장한다.
- React 빌드 산출물이 있으면 내장 HTTP 서버가 `/`에서 메인 웹 패널과 정적 자산을 서빙한다.
- React 소스는 `frontend/web-panel/`에 있으며, Vite + React + TypeScript 기반으로 유지한다.
- 프론트 데이터 계층은 `src/lib/panelApi.ts`, `src/lib/panelEvents.ts`, `src/lib/decodePanelState.ts`, `src/lib/logParser.ts`, `src/hooks/usePanelState.ts`, `src/hooks/usePanelLogs.ts`로 나뉜다.
- 패널 UI는 `src/components/panel/`과 `src/components/ui/`의 로컬 재사용 컴포넌트로 나누고, `App.tsx`는 화면 조합만 담당한다.
- panel state 조회와 action 이후 refetch/invalidation은 TanStack Query가 담당한다.
- 상단 알림 토글은 `.env` secret 존재 여부를 기준으로 선택 가능한 알림 채널만 순환 표시하고, 선택 시 `/api/notification`으로 `config.yaml`을 갱신한다.
- 실행 중 워커가 있으면 알림 채널 저장 요청이 같은 API 호출 안에서 워커 재시작까지 이어져 즉시 반영된다.
- 실시간 갱신은 `GET /api/events` SSE를 우선 사용하며, 서버는 `state`, `log_chunk`, `log_reset` 이벤트를 스트리밍한다.
- 클라이언트는 SSE가 연결되어 있는 동안 polling을 멈추고, 스트림이 실패하거나 브라우저가 `EventSource`를 지원하지 않으면 기존 `/api/state`, `/api/logs` polling으로 fallback 한다.
- 최근 작업과 로그는 분리 카드 대신 하나의 Activity 패널로 묶어, 최근 작업 행 선택과 해당 작업 중심의 한국어 운영 로그 확인을 한 흐름으로 제공한다.
- Activity 패널의 로그 영역은 raw log를 그대로 유지하되, `logParser.ts`가 반복 패턴을 파싱해 과목/날짜/요일/교시를 포함한 한국어 운영 로그 뷰와 오류 전용 뷰를 함께 제공한다.
- pause/resume는 별도 IPC 대신 `state/paused` 플래그 파일로 제어한다.
- Tk 기반 구형 GUI는 제거했고, 운영 제어면은 웹 패널로 단일화한다.

## Hermes postprocess operator package
- `scripts/hermes_postprocess/`는 앱 내부 LLM provider를 되살리지 않고 Hermes cron/operator 계층에서 쓸 결정적 보조 기능만 제공한다.
- `dry-run` CLI는 `02_transcripts`의 txt/json pair 중 final correction/summary가 완성되지 않은 stem 하나를 metadata-only JSON으로 반환한다. raw transcript body와 segment 배열은 stdout/report에 싣지 않는다.
- `paths.py`는 `02_transcripts`, `03_correction`, `04_summarize`, `05_prompt`, repo-local `state/hermes_postprocess/{staging,claims}` 경로를 계산하고 subject code를 longest-match로 추출한다.
- `prompts.py`는 existing iCloud `05_prompt/00_base_prompt.txt`, `01_common_glossary.txt`, 선택 subject glossary를 source of truth로 로드한다.
- `validators.py`는 correction JSON의 segment count, `id/start/end`, 전체 JSON key/order/array 구조, non-`text` metadata 보존, final overwrite 금지, summary required headings 및 `summary_too_short` guardrail을 검증한다.
- `misrecognitions.py`는 교정 중 발견한 짧은 오인식 후보 phrase를 repo-local `state/hermes_postprocess/misrecognitions/pending.jsonl`에 dedupe append한다. raw transcript excerpt/context와 `05_prompt` 자동 변경은 금지한다.
- `staging.py`는 metadata-only `manifest.json`과 explicit promote 인터페이스를 제공한다. Promote는 기본적으로 `promote_disabled`를 반환하며, `--allow-promote`가 없으면 final 경로에 쓰지 않는다. 허용된 promote도 candidate destination path를 재계산해 검증하고 validator를 재실행한 뒤 exclusive no-overwrite copy와 hash-checked rollback을 사용한다.
- 이 패키지는 Gate A/B 개발·dry-run 검증 범위에서 시작했으며, 이후 승인된 Gate C+ promote canary와 Hermes cron 등록까지 반영됐다. 현재 활성화 범위와 금지사항은 `docs/operators/hermes-postprocess/README.md`와 `docs/OPERATIONS.md`를 우선한다.
- 2026-05-20 후속 승인으로 live stem `260504DS_1` 1건의 content staging, validation, Gate C+ promote canary를 수행했고, script-only Hermes cron job `lecture_stt_postprocess_operator`를 등록했다. Cron은 `state/hermes_postprocess/cron-baseline.json`의 activation-time backlog skip list를 사용해 기존 backlog를 건너뛰며, no-candidate일 때 stdout/delivery 없이 조용히 종료한다.
- `--lecture-root`는 iCloud가 아니어도 된다. 같은 폴더 구조의 local canary root를 넘기면 postprocess helper는 완전히 로컬에서 동작한다. iCloud는 현재 실제 transcript/prompt 정본 위치라 Gate B에서 read-only inventory 대상으로만 사용한다.

## Downstream 배포 파이프라인
- API-backed 자동 correction provider는 현재 비활성화되어 있으며, `src/lecture_stt/correction/worker.py`는 `02_transcripts`의 pending pair를 manual correction 대기 상태로만 보고한다.
- `CorrectionConfig`에는 API key/model/max token 필드가 없고, `correction.mode: manual`을 기본 운영 모드로 둔다.
- `src/lecture_stt/correction/corrector.py`는 외부 API 호출 구현을 갖지 않는 compatibility/helper 모듈이며, 자동 correction 시도는 명시적으로 실패한다.
- 진입점은 `src/lecture_stt/downstream/worker.py`다.
- correction 입력은 `03_correction` 폴더의 `{stem}.txt + {stem}.json` pair다.
- summary 입력은 `04_summarize` 폴더의 `{stem}.md`다.
- `src/lecture_stt/downstream/lib.py`가 stem 해석, 과목 라우팅, 무손실 복사, conflict 처리, cleanup, deliveries 상태 갱신을 담당한다.
- correction은 GH archive의 `06_lecture_notes/02_origin`으로 배포된다.
- summary는 GH archive의 `01_summarize`와 Obsidian 노트 경로 둘 다로 배포된다.
- summary는 correction 전달 완료가 확인된 경우에만 배포된다.
- 동일 내용은 hash 비교로 idempotent하게 처리하고, 다른 내용이 있으면 overwrite하지 않고 conflict로 남긴다.
- 반복되는 invalid/incomplete/blocked/conflict/error 이벤트는 같은 worker 프로세스 안에서 bounded suppression cache의 동일 key 기준 1회만 stdout/JSONL에 남겨 로그 폭주를 줄인다.
- scan 통계 로그는 최초, 통계 변화, 설정된 heartbeat 주기 때만 JSONL에 남기고 routine stdout은 기본적으로 끈다.
- `downstream.log_jsonl_max_bytes`를 0보다 크게 설정하면 `state/logs/downstream.jsonl`에 size guard/rotation을 적용한다. `downstream.log_suppression_max_keys`는 장기 실행 중 suppression cache 상한을 정한다. `downstream.log_routine_scan_events: false`이면 routine `scan_started`는 stdout/JSONL 모두 생략하고, routine `scan_completed`는 최초/변경/heartbeat만 JSONL에 남긴다. 기존 `downstream.out.log` truncate/delete나 launchd 재시작은 운영 승인 후 별도 절차로 처리한다.

## Downstream 상태 저장
- downstream 상태는 `deliveries` 테이블에 기록된다.
- correction/summary 각각의 상태, 대상 경로, hash, last error, 완료 플래그를 저장한다.
- `src/lecture_stt/downstream/status.py`가 요약 조회, 목록 조회, 상세 조회, row 삭제 CLI를 제공한다.

## 운영 스크립트와 서비스
- `scripts/run_worker.sh`: 메인 워커 상시 실행
- `scripts/run_once.sh`: 메인 워커 1회 실행
- `scripts/run_gui.sh`: 웹 제어판 실행
- `scripts/run_distribute.sh`: downstream 워커 실행
- `scripts/distribute_status.sh`: deliveries 상태 CLI 래퍼
- `scripts/benchmark_models.py`: baseline/current model과 승인된 후보 STT 모델을 비교하는 benchmark CLI 초안
- `scripts/setup_launchd.sh`: venv, 의존성, 모델, 폴더, launchd를 한 번에 설정
- `scripts/cleanup.py`: 오래된 audio/transcript/tmp 정리
- `scripts/hermes_postprocess/`: Hermes postprocess operator CLI. `python3 -m scripts.hermes_postprocess dry-run`으로 metadata-only 후보 discovery를 수행하고, `validate-correction`, `validate-summary`, `record-misrecognitions`, `promote` subcommand를 제공한다. Promote는 `--allow-promote` 없이는 final write를 하지 않는다.
- `launchd/com.geonha.lecture-stt.plist`: 메인 워커 상시 실행
- `launchd/com.geonha.lecture-stt-webpanel.plist`: 웹 제어판 상시 실행
- `launchd/com.geonha.lecture-stt-distribute.plist`: downstream 워커 상시 실행
- `launchd/com.geonha.lecture-stt-cleanup.plist`: 정리 작업 스케줄 실행

## 경로/설정 규칙
- 프로젝트 내부 리소스(`config/`, `state/`, `tmp/`, `frontend/web-panel/dist`)는 `repo_root()` 기준 상대경로로 해석한다.
- 사용자 데이터 경로(iCloud inbox, correction/summary, GH archive, Obsidian vault)는 코드 기본값으로 두지 않고 로컬 `config/config.yaml`에서 지정한다.
- `config/config.yaml`의 경로 값은 `~`와 `${VAR}` 환경변수 치환을 지원하고, 상대경로는 저장소 루트 기준으로 해석한다.
- `.env`는 secret뿐 아니라 운영 환경별 경로 override에도 사용할 수 있지만, 실제 외부 저장소 위치의 정본은 `config/config.yaml`이다.
- launchd plist는 저장소에 절대경로를 고정하지 않고 `scripts/setup_launchd.sh`가 현재 repo 위치로 템플릿을 렌더링해 등록한다.

## 테스트 범위
- 현재 자동 테스트는 STT pipeline, downstream, web panel state/backend, script entrypoints, cleanup/log retention, Hermes postprocess operator를 함께 검증한다.
- `tests/test_stt_main.py`: pause/resume/status control command, STT retry/failure, dedupe/replay, quality scorecard sidecar 회귀 테스트
- `tests/test_transcribe_worker.py`, `tests/test_transcribe_progress.py`, `tests/test_quality_gate.py`: transcribe worker/progress/quality scoring 회귀 테스트
- `tests/test_distribute_lib.py`: pair 처리, block/unblock, idempotency, conflict, cleanup failure, 반복 문제 로그 suppression, JSONL rotation
- `tests/test_downstream_worker.py`: scan stats heartbeat/suppression 회귀 테스트
- `tests/test_correction_manual.py`: 자동 correction provider 제거, API key 불필요, manual mode pending skip 회귀 테스트
- `tests/test_distribute_status.py`: deliveries CLI 출력과 삭제 동작
- `tests/test_web_panel.py`, `tests/test_web_panel_state.py`: 웹 제어판 종료 동작, React 친화형 snapshot 계약, 로그 stream reset 회귀 테스트
- `tests/test_hermes_postprocess.py`: Hermes postprocess candidate discovery, action plan, path resolution, raw-body leak prevention, prompt loader, staging manifest, review-only misrecognition queue, promote safety/rollback/path validation, correction/summary validators
- `tests/test_script_entrypoints.py`, `tests/test_rotate_logs_script.py`, `tests/test_log_retention.py`, `tests/test_paths.py`, `tests/test_runtime_migration_config.py`, `tests/test_notifier.py`, `tests/test_benchmark_models.py`: entrypoint portability, log cleanup, path/default config, notification, benchmark safety 회귀 테스트

## 유지 규칙
- 구조가 바뀌면 이 문서를 먼저 최신 구조에 맞게 수정한다.
- 저장소 변경 이력은 `docs/WORKLOG.md`에 누적한다.
