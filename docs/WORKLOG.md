# Lecture STT Worklog

이 파일은 저장소에 반영된 변경을 날짜순으로 누적 기록한다.
최신 항목을 위에 추가한다.

## 2026-05-19

### Claude/Anthropic 자동 교정 제거와 downstream problem row 전수 분류
- correction 단계는 유지하되 API-backed 자동 교정 provider를 비활성화하고 manual/provider-neutral 모드로 전환했다.
- `requirements.txt`에서 `anthropic` dependency를 제거하고, `config/config.yaml`/`config/config.example.yaml`의 Claude 전용 model/API key 설정을 `correction.mode: manual`로 대체했다.
- `CorrectionConfig`에서 API key/model/max token 필드를 제거하고, `CorrectionWorker`가 pending transcript pair를 correction output으로 쓰지 않고 `skipped`로 보고하도록 바꿨다.
- legacy `corrector.py`는 외부 API 호출 없이 provider-neutral helper와 disabled compatibility shim만 남겼다.
- API key 없이 설정을 로드하고 manual mode에서 source/correction 폴더를 변경하지 않는 회귀 테스트를 추가했다.
- 운영 DB의 downstream problem row 41건을 read-only로 전수 분류해 `docs/DOWNSTREAM_TRIAGE_2026-05-19.md`에 기록했다. 25건은 실제 source/destination hash conflict, 1건은 stale/DB-only conflict 의심, 8건은 invalid stem, 7건은 subject route/rename 확인 대상으로 분리했다.

### downstream 로그 폭주 완화와 모델 benchmark 초안
- downstream worker가 반복 conflict/blocked/error 문제를 매 scan마다 다시 stdout/JSONL에 쓰지 않도록, 프로세스 생애 동안 동일 문제 이벤트를 1회만 기록하는 suppression을 추가했다.
- scan 통계 stdout은 최초/변경/heartbeat 때만 출력하도록 `ScanStatsReporter`를 추가해 `downstream.out.log` 증가량을 줄였다.
- `JsonlLogger`에 opt-in size guard/rotation 기능을 추가했다. 기본 example은 안전하게 비활성(`log_jsonl_max_bytes: 0`)으로 두고, 운영 config에서는 별도 승인 후 크기 제한을 켜는 방식으로 분리했다.
- `downstream status summary`가 problem row 수와 `last_error_code`별 reason count를 함께 보여주도록 개선했다.
- `scripts/benchmark_models.py` 초안을 추가했다. 기본은 현재 config의 `large-v3` baseline plan만 실행 가능하고, 후보 모델은 `--allow-candidate`, 다운로드/cache miss는 `--allow-download` 없이는 진행하지 않는다. 실제 benchmark 실행은 transcript payload가 stdout에 노출되지 않도록 `--output`을 필수로 요구한다.
- 관련 unittest와 benchmark plan smoke check를 추가했다. 모델 다운로드/교체/실제 benchmark 실행은 수행하지 않았다.
- 후속 보강으로 반복 문제 suppression cache를 bounded set으로 바꾸고, `backup_count=0` JSONL rotation도 UUID suffix로 충돌 없이 여러 번 회전되도록 했다.
- `config/config.example.yaml`에는 downstream 로그 관련 안전 기본값을 문서화했고, gitignored 운영 `config/config.yaml`에는 승인된 범위에서 `log_jsonl_max_bytes: 10485760`, `log_jsonl_backup_count: 5`, `log_suppression_max_keys: 4096`, `log_routine_scan_events: false`를 적용했다.
- 기존 대용량 downstream 로그는 삭제하지 않고 `state/logs/archive/20260519T045929Z/` 아래 gzip으로 보존한 뒤 원본을 truncate했으며, `com.geonha.lecture-stt-distribute`를 재시작해 stdout 반복 폭주가 멈춘 것을 확인했다.
- 모델 변경은 baseline freeze → 실사용 sample shadow benchmark → 수동 품질판정 → 제한 canary 순서로 검증하도록 `docs/MODELS.md`에 절차를 추가했다.
- `large-v3-turbo`, `deepdml` turbo, `distil-large-v3`, `ghost613` Korean turbo 후보를 실제 짧은/중간 샘플로 비교했고, 속도 이득은 있었지만 전공 용어 오류·누락·hallucination 징후 때문에 canonical STT 기본값으로는 탈락시켰다.
- isolated `faster-whisper==1.2.1` 환경에서 같은 `large-v3`를 재측정했다. 속도는 개선됐지만 short sample 기준 출력 길이가 2338자에서 1742자로 줄고 도입부 누락/initial-prompt성 문장 삽입 징후가 있어 production `.venv` 업그레이드는 보류한다.

### runtime inventory 및 모델 최신성 live 조회
- read-only inventory 결과를 `docs/RUNTIME_INVENTORY_2026-05-19.md`에 저장했다.
- iCloud `lecture_recordings` 실제 구조, repo runtime 폴더, legacy `/Users/geonha/lecture_stt`, launchd/process, DB/deliveries 상태를 정리했다.
- 모델/패키지 최신성 조회 결과와 benchmark 후보를 `docs/MODELS.md`에 저장했다.
- 현행 `faster-whisper==1.1.0` 대비 최신 `1.2.1`이 있음을 확인했고, `large-v3-turbo`, CT2 turbo, MLX, Korean fine-tune 후보를 benchmark 후보로 분리했다.
- 삭제/이동/launchd 재시작/DB 변경/모델 변경은 수행하지 않았다.

### lecture workflow 답변 반영 및 handoff 갱신
- 사용자 답변을 바탕으로 iPhone 녹음 → iCloud `lecture_recordings/00_inbox` 업로드 → STT 전사 → 수동 LLM 교정/요약 흐름을 `docs/HANDOFF_RESTRUCTURE_AND_MODEL_UPGRADE.md`에 반영했다.
- Claude/Anthropic 제거 범위는 correction 단계 삭제가 아니라 Claude API 구현 제거로 조정하고, raw/corrected transcript와 summary는 계속 유지하는 방향으로 정리했다.
- Web panel 개편은 이번 작업 범위에서 제외하고, 모델 고도화는 정확도 최우선 + 컴퓨터 수용 가능성 제약 + 최신 후보 live 확인을 필수 gate로 갱신했다.
- canonical output의 의미와 iCloud/GH_archive/Obsidian 역할 확인 질문을 추가했다.

### 구조 정리 및 모델 고도화 handoff 문서 추가
- 장기 작업을 세션 간 이어가기 위해 `docs/HANDOFF_RESTRUCTURE_AND_MODEL_UPGRADE.md`를 추가했다.
- 사용자가 결정해야 할 경로 정책, Claude 제거 범위, downstream/summary 워크플로우, 모델 benchmark 기준을 분리해 기록했다.
- 삭제/이동/launchd 재시작/DB 변경은 승인 후 진행하도록 안전장치를 명시했다.

## 2026-04-29

### launchd 중복 STT worker 재시작 루프 완화
- `com.geonha.lecture-stt` LaunchAgent가 기존 외부 STT worker의 `state/stt.lock`을 만나면 10초마다 재실행 로그를 남기던 문제를 확인했다.
- launchd로 실행된 `scripts/run_worker.sh`는 `LECTURE_STT_LOCK_WAIT=1`을 기본 설정하도록 바꿨다.
- STT main lock은 해당 환경변수가 켜진 경우 non-blocking 실패로 종료하지 않고 lock을 기다리도록 확장했다.
- launchd job이 대기 프로세스 하나로 유지되므로 반복 로그를 멈추고, 기존 worker가 종료되면 새 job이 자연스럽게 lock을 이어받는다.
- lock 대기 모드 회귀 테스트를 추가했다.
- 전체 점검 중 downstream이 같은 invalid/incomplete/blocked 항목을 30초마다 반복 기록해 로그가 커지는 문제를 확인했다.
- downstream worker 프로세스 생애 동안 같은 반복 문제 이벤트는 한 번만 남기도록 줄이고 회귀 테스트를 추가했다.

## 2026-03-29

### 메인 워커 선점 레이스 완화와 로컬 staging 추가
- 메인 STT 워커 시작 시 `state/stt.lock` 파일 락을 잡아 launchd, 수동 실행, 제어판 실행이 겹쳐도 동시에 두 개 이상 돌지 않도록 막았다.
- inbox에서 안정 판정된 파일은 바로 `01_audio`로 가지 않고 `tmp/inbox_staging`으로 먼저 선점 이동한 뒤 canonical audio로 넘기도록 바꿔, iCloud rename/sync가 전사 본 처리 단계에 끼어드는 구간을 줄였다.
- claim 전에 원본이 사라진 경우는 최근 동일 source를 다른 워커가 이미 잡았는지 재확인하고, benign race나 외부 rename 가능성으로 판단되면 ERROR 대신 warning log만 남기고 skip하도록 완화했다.
- 워커 재시작 시 `tmp/inbox_staging`에 남은 파일을 inbox로 되돌리고, 대응되는 stale staging job을 정리하도록 보완했다.
- 추가로 `01_audio`에만 남은 pre-claim `PENDING` 오디오도 시작 복구 시 inbox로 되돌려, canonical move 직후 크래시가 영구 정체로 남지 않게 했다.
- scheduled cleanup이 `tmp/inbox_staging`을 삭제하지 않도록 예외 처리하고, 관련 STT/cleanup 회귀 테스트를 추가했다.

## 2026-03-27

### PyCharm 모듈 루트 설정 수정
- `.idea/lecture_stt.iml`의 module content root가 `.idea/`를 가리키고 있어 프로젝트 파일 트리가 비정상적으로 보일 수 있던 문제를 수정했다.
- module root를 저장소 루트 기준으로 바꾸고 `src/`, `.venv/` 경로도 같은 기준으로 다시 연결했다.

## 2026-03-26

### 경로 설정 정책 정리와 사용자 절대경로 제거
- STT/downstream/cleanup/UI가 공통 경로 해석 규칙을 쓰도록 정리하고, `config.yaml` 경로 값에서 `~`와 `${VAR}` 확장을 지원하도록 맞췄다.
- 코드 기본값에서는 `state/`, `tmp/`, `logs/` 같은 repo 내부 경로만 남기고, iCloud/GH archive/Obsidian 같은 사용자 데이터 경로 fallback은 제거했다.
- `config/config.example.yaml`을 사용자 절대경로 템플릿에서 env-backed 예시로 바꾸고, launchd plist도 repo 절대경로 대신 `__REPO_ROOT__` 템플릿으로 바꿨다.
- `setup_launchd.sh`는 ffmpeg를 고정 Homebrew 경로 대신 `config -> FFMPEG_BINARY -> PATH` 순으로 찾도록 수정했다.
- 경로 해석 회귀를 막기 위해 control command, 웹 패널 placeholder, `.env` 기반 패널 경로 해석 테스트를 추가했다.

### 스크립트 엔트리포인트 회귀 수정
- `scripts/cleanup.py`가 launchd처럼 직접 파일 실행될 때도 `src/`를 import 경로에 올려 정상 동작하도록 부트스트랩을 추가했다.
- `scripts/ab_test.py`는 기본 `ffmpeg` 값을 그대로 넘기지 않고 실제 실행 파일 경로로 해석한 뒤 `STTWorker`에 주입하도록 수정했다.
- cleanup 직접 실행과 `ab_test.py` ffmpeg 경로 해석을 검증하는 회귀 테스트를 추가했다.

## 2026-03-23

### 웹 패널 알림 채널 선택 UI 추가
- 웹 패널 snapshot과 API 계약에 `notification` 상태와 저장 endpoint를 추가하고, `config.yaml`의 `notification` 섹션을 패널에서 수정할 수 있게 했다.
- React 패널 상단에 Notification 카드를 추가해 텔레그램만, 디스코드만, 둘 다, 끄기 중 하나를 선택하고 저장할 수 있게 했으며, `.env` secret이 없는 선택지는 비활성화했다.
- 저장은 기본적으로 안전한 `다음 재시작부터 적용` 모델로 두고, 필요하면 같은 카드에서 `저장 후 재시작`으로 현재 워커에 즉시 반영할 수 있게 했다.

### STT 알림 채널 텔레그램 전환 기반 추가
- `DiscordNotifier` 단일 구현을 provider 기반 notifier 팩토리로 바꾸고, Telegram/Discord/Noop 및 다중 채널 전송을 지원하도록 정리했다.
- `notification` 설정 섹션과 `.env` secret 조합으로 채널을 선택하게 바꾸고, provider별 중복 마커를 따로 써서 전환 기간의 shadow 전송이 막히지 않도록 했다.
- 텔레그램 payload, auto provider 선택, dual-send 중복 방지에 대한 Python 단위 테스트를 추가하고 운영 문서와 예시 설정을 갱신했다.

### STT 품질 경고/진행률/ETA 현실화
- 품질 게이트를 단순 반복도 기준에서 벗어나 반복 비율, 빈 구간, 기호 노이즈, 짧은 발화 비율, 평균 길이를 함께 보는 복합 점수 방식으로 보강했다.
- `faster-whisper` 세그먼트 스트림을 이용해 전사 중간 진행률을 계속 갱신하고, WAV 길이 대비 처리 구간과 실제 처리 속도로 남은 ETA를 재계산하도록 바꿨다.
- 후처리, 품질 검사, 산출물 저장 단계의 진행률/ETA도 별도 단계 값으로 정리하고, 관련 Python 단위 테스트를 추가했다.

### React 웹 패널 최근 작업/운영 로그 통합
- 접근성을 높이기 위해 `최근 작업` 테이블과 `운영 로그` 카드를 하나의 Activity 패널로 통합했다.
- 최근 작업 행을 선택하면 같은 파일 기준의 운영 이벤트와 오류를 바로 아래에서 볼 수 있도록 바꿔, 화면 이동 없이 작업 맥락을 유지하게 했다.
- raw log, auto-follow, 이력 초기화, 패널 종료 동작은 유지하고, 통합 패널에 맞는 선택 행/상세 영역 스타일만 추가했다.

### React 웹 패널 운영 로그 파서/한국어 로그 뷰 추가
- 로그 패널에 `운영 뷰`, `오류만`, `원본 로그` 전환을 추가하고, 반복되는 STT 로그 패턴을 한국어 운영 이벤트로 정리해 보여주도록 확장했다.
- 파일명 규칙 `YYMMDD + 과목코드 + 선택적 _N + 확장자`를 파싱해 과목명, 날짜, 요일, 교시를 표시하고 `Dstr`는 `DStr`로 정규화했다.
- raw log 수집 계층은 유지하고, `logParser.ts`와 parser 테스트만 추가해 최소 침습적으로 표시 계층을 개선했다.

### React 웹 패널 다크 모드 추가
- `localStorage`에 저장되는 light/dark 테마 훅을 추가하고, 운영 헤더에서 라이트/다크 모드를 전환할 수 있도록 연결했다.
- 기존 레이아웃과 컴포넌트 구조는 유지한 채 CSS 변수를 dark override로 확장해, 카드/테이블/로그/에러 배너까지 동일한 시각 언어로 다크 모드를 적용했다.
- 테마 초기화와 토글 persist 동작을 검증하는 프론트 훅 테스트를 추가했다.

### React 웹 패널 테마 토글 UI 보정
- 상단의 큰 테마 버튼을 제거하고, 상태 배지와 어울리는 작은 pill 스위치로 바꿨다.
- 라이트/다크 두 옵션이 같은 캡슐 안에 들어가고, 현재 선택값은 좌우로 이동하는 슬라이딩 thumb로 표시되도록 조정했다.

### React 웹 패널 다크모드 카드/토글 미세 조정
- `Queue` 카드의 `muted-card` 배경이 다크모드 override를 타지 않던 문제를 CSS 변수로 정리했다.
- 테마 스위치를 더 작고 조밀한 iOS-style 캡슐 비율로 줄이고, thumb 그림자와 이동감을 보정했다.

### React 웹 패널 테마 스위치 아이콘형 재조정
- 상단 테마 스위치를 텍스트 세그먼트에서 icon-only 토글로 다시 줄이고, `실행중` 상태 배지와 맞는 높이의 캡슐 스위치로 정리했다.
- 라이트/다크 구분은 텍스트 대신 sun/moon SVG 아이콘으로 바꾸고, 슬라이딩 thumb는 한 번 클릭할 때 좌우로 이동하도록 단순화했다.

### React 웹 패널 기본 액션 버튼 팔레트 고정
- `시작`과 `일시정지/재개`가 기본 버튼 색을 써서 라이트/다크 전환 시 반전돼 보이던 문제를 수정했다.
- 기본 액션 버튼 팔레트를 테마와 무관한 slate 계열로 고정해, 운영 액션 색이 모드 전환에 따라 뒤집히지 않도록 맞췄다.

### React 웹 패널 액션 버튼/테마 전환 속도 재보정
- 기본 액션 버튼 팔레트를 다시 slate 계열로 되돌려, 기존 운영 패널 톤을 유지하면서도 라이트/다크 전환 시 색이 뒤집히지 않게 조정했다.
- 테마 전환이 너무 급하게 느껴지지 않도록 body, 카드, 메타 카드, 로그, 에러 배너, 스위치 thumb의 transition 시간을 늘렸다.

### React 웹 패널 런타임 액션 버튼 톤 정렬
- `시작`과 `일시정지/재개`가 여전히 기본 버튼 톤을 써서 `동기화`와 다르게 보이던 문제를 수정했다.
- Runtime 카드의 비파괴 액션 세 개를 모두 `secondary` 톤으로 통일해, `동기화` 버튼 기준의 동일한 시각 규칙으로 맞췄다.

### 변경 파일
- `frontend/web-panel/src/hooks/useThemeMode.ts`
- `frontend/web-panel/src/App.tsx`
- `frontend/web-panel/src/components/panel/PanelHero.tsx`
- `frontend/web-panel/src/styles.css`
- `frontend/web-panel/test/useThemeMode.test.tsx`
- `docs/WORKLOG.md`

### React 웹 패널 리뷰 지적 후속 수정
- `refresh/start/pause/resume/stop` 같은 일반 action 이후 로그 버퍼가 지워지던 회귀를 수정하고, `clear_history`일 때만 로그 reset 키가 증가하도록 고쳤다.
- SSE decode 실패가 한 번 발생하면 realtime이 영구 중단되던 문제를 수정해, 스트림을 정리한 뒤 자동 재연결을 시도하도록 바꿨다.
- 로그 reset 이후에도 이전 세션의 hidden-line 수치가 남던 문제를 수정하고 관련 프론트 테스트를 추가했다.

### 변경 파일
- `frontend/web-panel/src/hooks/usePanelState.ts`
- `frontend/web-panel/src/hooks/usePanelLogs.ts`
- `frontend/web-panel/src/lib/panelEvents.ts`
- `frontend/web-panel/test/usePanelState.test.tsx`
- `frontend/web-panel/test/usePanelLogs.test.tsx`
- `frontend/web-panel/test/panelEvents.test.ts`
- `src/lecture_stt/ui/web_panel.py`
- `docs/WORKLOG.md`

### React 웹 패널 상단 메타 카드 보정
- 상단 헤더의 `동기화 정책` 카드를 제거하고, 클라이언트 기준 `현재 시간`과 `실시간 연결됨/fallback polling 중` 상태를 보여주도록 바꿨다.
- `업데이트 시간`은 마지막 상태 변경 시각으로 유지하고, 현재 시각은 별도 카드로 분리해 의미 혼선을 줄였다.

### 변경 파일
- `frontend/web-panel/src/hooks/usePanelState.ts`
- `frontend/web-panel/src/App.tsx`
- `frontend/web-panel/src/components/panel/PanelHero.tsx`
- `frontend/web-panel/src/styles.css`
- `docs/WORKLOG.md`

### React 웹 패널 SSE 실시간 갱신 도입
- `/api/events` SSE 스트림을 추가해 React 패널이 `state`, `log_chunk`, `log_reset` 이벤트를 서버 push로 받도록 확장했다.
- 프론트에 `panelEvents` adapter/service를 추가하고, `usePanelState`와 `usePanelLogs`가 SSE 연결 시 polling을 멈추고 끊기면 기존 polling으로 자동 fallback 하도록 정리했다.
- 로그 truncate/reset 안전성을 위해 `ControlState._log_stream_delta()`를 추가하고, 관련 Python/React 테스트를 보강했다.

### 변경 파일
- `src/lecture_stt/ui/web_panel.py`
- `src/lecture_stt/ui/web_panel_state.py`
- `frontend/web-panel/src/types.ts`
- `frontend/web-panel/src/lib/panelApi.ts`
- `frontend/web-panel/src/lib/panelEvents.ts`
- `frontend/web-panel/src/hooks/usePanelState.ts`
- `frontend/web-panel/src/hooks/usePanelLogs.ts`
- `frontend/web-panel/test/usePanelState.test.tsx`
- `frontend/web-panel/test/usePanelLogs.test.tsx`
- `tests/test_web_panel.py`
- `tests/test_web_panel_state.py`
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`

### React 웹 패널 운영형 대시보드 디자인 개편
- `shadcn/ui`의 dashboard/card/badge/alert/table 레퍼런스를 참고하되, Tailwind나 컴포넌트 스택 교체 없이 기존 로컬 React/CSS 구조만으로 운영형 대시보드 톤으로 재정렬했다.
- 소개형 hero를 얇은 운영 헤더로 바꾸고, 상단 요약 카드와 본문 2열 레이아웃, 경고 alert, 상태 pill, 진행률 bar, 터미널형 로그 패널을 적용했다.
- 기능 경계나 데이터 흐름은 건드리지 않고 `App.tsx`, panel/ui 컴포넌트, `styles.css`만으로 시각 계층과 밀도를 조정했다.

### 변경 파일
- `frontend/web-panel/src/App.tsx`
- `frontend/web-panel/src/components/panel/PanelHero.tsx`
- `frontend/web-panel/src/components/panel/RuntimeCard.tsx`
- `frontend/web-panel/src/components/panel/ProcessingCard.tsx`
- `frontend/web-panel/src/components/panel/JobsTableCard.tsx`
- `frontend/web-panel/src/components/panel/LogPanelCard.tsx`
- `frontend/web-panel/src/components/ui/SectionCard.tsx`
- `frontend/web-panel/src/components/ui/ActionBar.tsx`
- `frontend/web-panel/src/components/ui/ErrorBanner.tsx`
- `frontend/web-panel/src/components/ui/LoadingBlock.tsx`
- `frontend/web-panel/src/components/ui/ErrorState.tsx`
- `frontend/web-panel/src/styles.css`
- `docs/WORKLOG.md`

### React 웹 패널 최종 검증 준비와 소형 테스트 보강
- `vitest + jsdom + @testing-library/react` 기반의 최소 프론트 테스트 러너를 추가했다.
- `decodePanelState`, `usePanelLogs`, `usePanelState`에 대해 decoder 검증, 로그 polling/trim/auto-follow, action 후 refetch를 확인하는 소형 단위 테스트를 보강했다.
- `README.md`에 `/app` 수동 검증 체크리스트와 `package-lock.json`/`dist/` 커밋 기준을 명시했다.

### 변경 파일
- `frontend/web-panel/package.json`
- `frontend/web-panel/package-lock.json`
- `frontend/web-panel/vitest.config.ts`
- `frontend/web-panel/test/setup.ts`
- `frontend/web-panel/test/decodePanelState.test.ts`
- `frontend/web-panel/test/usePanelLogs.test.tsx`
- `frontend/web-panel/test/usePanelState.test.tsx`
- `README.md`
- `docs/WORKLOG.md`

## 2026-03-22

### React 웹 패널 1차 최소 침습 리팩터링
- `App.tsx`에 몰려 있던 상태 조회, action 실행, logs polling, 화면 렌더링을 `panelApi`, decoder, hooks, local UI/panel components로 분리했다.
- panel state 조회와 action 이후 refetch/invalidation에 TanStack Query를 적용하고, logs는 기존 byte-offset polling을 custom hook으로 유지했다.
- React 패널이 백엔드 `actions.endpoints` 계약을 실제로 소비하도록 바꾸고, 로그 auto-follow, manual scroll 감지, trim 메타 표시를 추가했다.
- Vite dev server에 `/api`, `/action` 프록시를 추가하고, 문서와 backend contract 테스트를 보강했다.

### 변경 파일
- `frontend/web-panel/package.json`
- `frontend/web-panel/vite.config.ts`
- `frontend/web-panel/src/main.tsx`
- `frontend/web-panel/src/App.tsx`
- `frontend/web-panel/src/api.ts`
- `frontend/web-panel/src/types.ts`
- `frontend/web-panel/src/styles.css`
- `frontend/web-panel/src/lib/panelApi.ts`
- `frontend/web-panel/src/lib/decodePanelState.ts`
- `frontend/web-panel/src/hooks/usePanelState.ts`
- `frontend/web-panel/src/hooks/usePanelLogs.ts`
- `frontend/web-panel/src/components/panel/PanelHero.tsx`
- `frontend/web-panel/src/components/panel/RuntimeCard.tsx`
- `frontend/web-panel/src/components/panel/ProcessingCard.tsx`
- `frontend/web-panel/src/components/panel/JobsTableCard.tsx`
- `frontend/web-panel/src/components/panel/LogPanelCard.tsx`
- `frontend/web-panel/src/components/ui/SectionCard.tsx`
- `frontend/web-panel/src/components/ui/StatusBadge.tsx`
- `frontend/web-panel/src/components/ui/DefinitionGrid.tsx`
- `frontend/web-panel/src/components/ui/MetricList.tsx`
- `frontend/web-panel/src/components/ui/ActionBar.tsx`
- `frontend/web-panel/src/components/ui/LoadingBlock.tsx`
- `frontend/web-panel/src/components/ui/EmptyState.tsx`
- `frontend/web-panel/src/components/ui/ErrorState.tsx`
- `frontend/web-panel/src/components/ui/ErrorBanner.tsx`
- `tests/test_web_panel.py`
- `tests/test_web_panel_state.py`
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`

### React 웹 패널 0단계 착수
- `web_panel_state.snapshot()`에 React 친화형 구조화 응답(`schema_version`, `runtime_state`, `actions`, `jobs_v2`, `processing_v2`, `summary`)을 추가했다.
- 기존 레거시 SSR 패널은 `/`에서 유지하고, React 빌드가 있으면 `/app`에서 정적 자산을 서빙하도록 `web_panel.py`를 확장했다.
- `frontend/web-panel/`에 Vite + React + TypeScript 골격과 API 클라이언트, 기본 화면, 스타일 파일을 추가했다.
- React 빌드가 없을 때 `/app`에서 안내 placeholder를 보여주도록 했다.
- React용 snapshot 계약과 `/app` placeholder 동작에 대한 테스트를 추가했다.

### 변경 파일
- `src/lecture_stt/ui/web_panel_state.py`
- `src/lecture_stt/ui/web_panel.py`
- `frontend/web-panel/package.json`
- `frontend/web-panel/tsconfig.json`
- `frontend/web-panel/tsconfig.app.json`
- `frontend/web-panel/tsconfig.node.json`
- `frontend/web-panel/vite.config.ts`
- `frontend/web-panel/index.html`
- `frontend/web-panel/src/main.tsx`
- `frontend/web-panel/src/App.tsx`
- `frontend/web-panel/src/api.ts`
- `frontend/web-panel/src/types.ts`
- `frontend/web-panel/src/styles.css`
- `tests/test_web_panel.py`
- `tests/test_web_panel_state.py`
- `.gitignore`
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`

### 웹 패널 제어 계층 분리
- React 전환 전 준비로 웹 패널의 상태/워커 제어 로직을 `web_panel_state.py`로 분리했다.
- `web_panel.py`는 HTML 렌더링, HTTP 핸들러, 서버 시작에 집중하는 엔트리포인트로 정리했다.
- 구조 문서에 새 UI 계층 분리와 현재 테스트 범위를 반영했다.

### 변경 파일
- `src/lecture_stt/ui/web_panel.py`
- `src/lecture_stt/ui/web_panel_state.py`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`

### STT 제어/웹 패널 운영 버그 수정
- `lecture_stt.stt.main`의 `--pause`, `--resume`, `--status`가 `--config`의 `paths.db_path`를 기준으로 pause 플래그를 보도록 수정했다.
- 웹 제어판의 `종료` 버튼이 워커 중지와 전체 이력 삭제를 수행하던 동작을 제거하고, 제어판 서버 종료만 하도록 고쳤다.
- STT control command와 웹 패널 종료 동작에 대한 회귀 테스트를 추가했다.

### 변경 파일
- `src/lecture_stt/stt/main.py`
- `src/lecture_stt/ui/web_panel.py`
- `tests/test_stt_main.py`
- `tests/test_web_panel.py`
- `docs/WORKLOG.md`

### Codex 서브에이전트 도입
- 프로젝트 로컬 `.codex/agents/` 디렉터리를 만들고 작업 성격에 맞는 기본 서브에이전트 8종을 추가했다.
- 리액트 웹 패널 전환을 대비해 `react-specialist`, `frontend-developer`, `typescript-pro`, `browser-debugger`를 기본 세트에 포함했다.
- 저장소 작업 흐름 문서 `AGENTS.md`에 서브에이전트 호출 기준과 추천 사용 순서를 추가했다.

### 변경 파일
- `.codex/agents/react-specialist.toml`
- `.codex/agents/typescript-pro.toml`
- `.codex/agents/frontend-developer.toml`
- `.codex/agents/browser-debugger.toml`
- `.codex/agents/reviewer.toml`
- `.codex/agents/test-automator.toml`
- `.codex/agents/code-mapper.toml`
- `.codex/agents/python-pro.toml`
- `AGENTS.md`
- `docs/WORKLOG.md`

### 리뷰 지적 수정
- `lecture_stt.downstream.status`가 `--db-path` 사용 시 worker/yaml을 선행 import하지 않도록 지연 import로 수정했다.
- `tests/test_distribute_status.py`에 `--db-path`가 worker config import를 건너뛰는 회귀 테스트를 추가했다.
- `scripts/setup_launchd.sh`가 launchd plist를 설치 시점의 저장소 경로로 렌더링하도록 수정했다.

### 변경 파일
- `src/lecture_stt/downstream/status.py`
- `tests/test_distribute_status.py`
- `scripts/setup_launchd.sh`
- `docs/WORKLOG.md`

### 2차 전면 전환
- 운영 진입점을 flat `src/*.py` 직접 실행에서 패키지 모듈 실행 방식으로 바꿨다.
- 새 메인 스크립트 `scripts/run_worker.sh`를 추가했다.
- `run_gui.sh`, `run_distribute.sh`, `distribute_once.sh`, `distribute_status.sh`, `run_once.sh`를 새 패키지 경로 기준으로 갱신했다.
- 웹/Tk 제어판이 워커를 모듈 이름 기준으로 시작하고 PID를 찾도록 수정했다.
- 테스트 import를 새 패키지 경로로 갱신했다.
- 기존 flat `src/*.py` wrapper를 제거했다.

### 변경 파일
- `scripts/run_worker.sh`
- `scripts/run_once.sh`
- `scripts/run_gui.sh`
- `scripts/run_distribute.sh`
- `scripts/distribute_once.sh`
- `scripts/distribute_status.sh`
- `scripts/ab_test.py`
- `scripts/cleanup.py`
- `scripts/setup_launchd.sh`
- `launchd/com.geonha.lecture-stt.plist`
- `src/lecture_stt/shared/paths.py`
- `src/lecture_stt/shared/utils.py`
- `src/lecture_stt/shared/db.py`
- `src/lecture_stt/stt/main.py`
- `src/lecture_stt/stt/watcher.py`
- `src/lecture_stt/stt/transcribe.py`
- `src/lecture_stt/stt/notifier.py`
- `src/lecture_stt/downstream/lib.py`
- `src/lecture_stt/downstream/worker.py`
- `src/lecture_stt/downstream/status.py`
- `src/lecture_stt/ui/web_panel.py`
- `src/lecture_stt/ui/tk_panel.py`
- `tests/test_distribute_lib.py`
- `tests/test_distribute_status.py`
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`

### 1차 구조 개편
- 실제 구현 코드를 `src/lecture_stt/` 패키지로 재배치했다.
- 하위 구조를 `stt`, `shared`, `downstream`, `ui`로 분리했다.
- 기존 `src/*.py` 경로는 launchd, 스크립트, 테스트 호환성을 위해 wrapper로 유지했다.
- `main.py`, `distribute_worker.py`의 repo root 계산을 새 위치 기준으로 보정했다.
- 런타임 경로 호환성을 유지하면서 소스 구조만 정리한 1차 개편이다.

### 변경 파일
- `src/lecture_stt/__init__.py`
- `src/lecture_stt/stt/__init__.py`
- `src/lecture_stt/stt/main.py`
- `src/lecture_stt/stt/watcher.py`
- `src/lecture_stt/stt/transcribe.py`
- `src/lecture_stt/stt/postprocess.py`
- `src/lecture_stt/stt/quality_gate.py`
- `src/lecture_stt/stt/notifier.py`
- `src/lecture_stt/shared/__init__.py`
- `src/lecture_stt/shared/db.py`
- `src/lecture_stt/shared/utils.py`
- `src/lecture_stt/downstream/__init__.py`
- `src/lecture_stt/downstream/lib.py`
- `src/lecture_stt/downstream/worker.py`
- `src/lecture_stt/downstream/status.py`
- `src/lecture_stt/ui/__init__.py`
- `src/lecture_stt/ui/web_panel.py`
- `src/lecture_stt/ui/tk_panel.py`
- `src/main.py`
- `src/watcher.py`
- `src/transcribe.py`
- `src/postprocess.py`
- `src/quality_gate.py`
- `src/notifier.py`
- `src/db.py`
- `src/utils.py`
- `src/distribute_lib.py`
- `src/distribute_worker.py`
- `src/distribute_status.py`
- `src/web_control_panel.py`
- `src/control_panel.py`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`

### 구조 문서화 및 작업 워크플로우 도입
- 저장소 구조와 실행 흐름을 정리한 `docs/ARCHITECTURE.md`를 추가했다.
- 누적 변경 기록 파일 `docs/WORKLOG.md`를 추가했다.
- 앞으로의 작업 순서를 고정하기 위해 `AGENTS.md`를 추가했다.
- `README.md`에 구조 문서와 변경 기록 링크를 추가했다.
- 런타임 동작 변경은 없다.

### 2026-03-23
- 웹 패널 알림 설정 UI를 카드형에서 상단 인라인 순환 토글로 바꿨다.
- 토글 버튼을 누를 때 `둘 다 -> 디스코드 -> 텔레그램 -> 끄기` 순으로 즉시 저장되게 정리했다.
- 저장 후 별도 `적용` 버튼을 누르지 않아도, 실행 중 워커가 있으면 같은 요청 안에서 즉시 재시작 적용되도록 바꿨다.
- 구형 웹 패널 백엔드와 연결된 경우에도 `미지원` 상태로 안전하게 보이도록 라벨을 보정했다.
- 토글 칩 라벨에서 `알림` 접두어를 제거해 더 짧게 보이도록 정리했다.
- 알림 토글은 텍스트 대신 Telegram/Discord 마크 중심의 인라인 SVG 칩으로 바꾸고, `둘 다` 상태는 두 아이콘을 함께 보여주도록 정리했다.

### 변경 파일
- `AGENTS.md`
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`
