# Lecture STT Architecture

기준일: 2026-03-23

## 목적
- iCloud inbox에 들어오는 강의 음성 파일을 자동으로 전사한다.
- 전사 결과를 TXT/JSON으로 저장하고, 후단 correction/summary 산출물을 별도 배포한다.

## 최상위 디렉터리
- `src/`: 애플리케이션 본체
- `frontend/`: React 웹 패널 소스(Vite 기반)
- `scripts/`: 수동 실행, 설치, 운영 보조 스크립트
- `launchd/`: macOS launchd 서비스 정의
- `config/`: YAML 설정
- `tests/`: downstream 배포 계층 테스트
- `state/`: SQLite DB, pause flag, 구조화 로그
- `tmp/`: ffmpeg 전처리 산출물 등 임시 파일

## 현재 소스 구조
- 실제 구현 코드는 `src/lecture_stt/` 패키지 아래에 정리되어 있다.
- `src/lecture_stt/stt/`: 메인 STT 파이프라인
- `src/lecture_stt/shared/`: 공용 DB, 유틸
- `src/lecture_stt/downstream/`: correction/summary 배포 파이프라인
- `src/lecture_stt/ui/`: 웹/Tk 제어판
- 운영 스크립트는 `PYTHONPATH=<repo>/src python -m lecture_stt...` 방식으로 패키지를 직접 실행한다.

## 메인 STT 파이프라인
- 메인 진입점 모듈은 `src/lecture_stt/stt/main.py`다.
- `load_config()`와 `validate_config()`가 설정 파일을 읽고 경로, ffmpeg, 쓰기 권한을 검증한다.
- `STTPipeline`이 전체 작업을 오케스트레이션한다.
- `PollingWatcher`가 inbox 폴더를 polling하면서 일정 시간 이상 변하지 않은 파일만 안정 파일로 판단한다.
- 안정 파일은 `01_audio`로 이동한 뒤 SHA-256을 계산한다.
- 같은 SHA-256의 완료 작업이 있으면 기존 결과를 복제해 dedupe 처리한다.
- 중복이 아니면 `STTWorker`가 ffmpeg로 WAV 전처리 후 faster-whisper 전사를 수행한다.
- 전사 결과는 `postprocess()`로 반복/노이즈/오인식 용어를 정리한다.
- `quality_gate.evaluate()`가 반복도 기반 건강도를 계산해 메타데이터에 포함한다.
- 최종 산출물은 `02_transcripts` 아래 `{base}.txt`, `{base}.json`으로 저장된다.
- 실패 시 오디오는 `99_errors`로 이동하고 DB 상태는 `ERROR`로 기록된다.
- 알림 계층은 `notification.provider` 설정과 `.env` secret을 기준으로 Telegram 또는 Discord provider를 선택한다.
- `notification.dual_send_providers`를 사용하면 전환 기간 동안 다중 채널 shadow 전송을 할 수 있다.

## 메인 파이프라인 핵심 모듈
- `src/lecture_stt/stt/main.py`: 설정 로드, 파이프라인 오케스트레이션, 상태 전이, 복구
- `src/lecture_stt/stt/watcher.py`: 안정 파일 감시
- `src/lecture_stt/stt/transcribe.py`: ffmpeg 전처리, Whisper 전사
- `src/lecture_stt/stt/postprocess.py`: 반복/점 노이즈 제거, 용어 교정
- `src/lecture_stt/stt/quality_gate.py`: 품질 보고서 생성
- `src/lecture_stt/stt/notifier.py`: Telegram/Discord notifier, 중복 방지 마커, provider 팩토리
- `src/lecture_stt/shared/utils.py`: 파일 이동, atomic write, hash, pause flag 등 공용 함수

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
- 레거시 SSR 패널은 `/`에서 제공하고, React 빌드 산출물이 있으면 `/app`에서 정적 자산을 서빙한다.
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
- `src/lecture_stt/ui/tk_panel.py`는 Tk 기반 구형 GUI이며, 현재 운영 문서와 스크립트는 웹 패널 중심이다.

## Downstream 배포 파이프라인
- 진입점은 `src/lecture_stt/downstream/worker.py`다.
- correction 입력은 `03_correction` 폴더의 `{stem}.txt + {stem}.json` pair다.
- summary 입력은 `04_summarize` 폴더의 `{stem}.md`다.
- `src/lecture_stt/downstream/lib.py`가 stem 해석, 과목 라우팅, 무손실 복사, conflict 처리, cleanup, deliveries 상태 갱신을 담당한다.
- correction은 GH archive의 `06_lecture_notes/02_origin`으로 배포된다.
- summary는 GH archive의 `01_summarize`와 Obsidian 노트 경로 둘 다로 배포된다.
- summary는 correction 전달 완료가 확인된 경우에만 배포된다.
- 동일 내용은 hash 비교로 idempotent하게 처리하고, 다른 내용이 있으면 overwrite하지 않고 conflict로 남긴다.

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
- `scripts/setup_launchd.sh`: venv, 의존성, 모델, 폴더, launchd를 한 번에 설정
- `scripts/cleanup.py`: 오래된 audio/transcript/tmp 정리
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
- 현재 자동 테스트는 downstream 계층에 집중되어 있다.
- `tests/test_distribute_lib.py`: pair 처리, block/unblock, idempotency, conflict, cleanup failure
- `tests/test_distribute_status.py`: deliveries CLI 출력과 삭제 동작
- `tests/test_stt_main.py`: pause/resume/status control command 회귀 테스트
- `tests/test_web_panel.py`: 웹 제어판 종료 동작 회귀 테스트
- `tests/test_web_panel_state.py`: React 친화형 snapshot 계약과 로그 stream reset 회귀 테스트
- `src/lecture_stt/stt/transcribe.py`는 아직 자동 테스트가 없다.

## 유지 규칙
- 구조가 바뀌면 이 문서를 먼저 최신 구조에 맞게 수정한다.
- 저장소 변경 이력은 `docs/WORKLOG.md`에 누적한다.
