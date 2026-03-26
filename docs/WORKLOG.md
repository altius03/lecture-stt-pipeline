# Lecture STT Worklog

이 파일은 저장소에 반영된 변경을 날짜순으로 누적 기록한다.
최신 항목을 위에 추가한다.

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
