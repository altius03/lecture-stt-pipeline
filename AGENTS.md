# Repository Workflow

## Default Workflow
- 저장소 관련 요청을 받으면 항상 이 순서로 진행한다.
- 1. 파일 확인: 관련 코드, 설정, 테스트, 실행 스크립트를 먼저 읽는다.
- 2. 작업: 실제 코드 기준으로 영향 범위를 파악하고 필요한 처리 방식을 정한다.
- 3. 수정: 필요한 파일만 최소 범위로 수정한다.
- 4. 검증: 가능한 범위에서 테스트, 실행, diff 확인으로 결과를 검증한다.

## Documentation Rules
- 구조 변경이나 새 서브시스템이 생기면 `docs/ARCHITECTURE.md`를 갱신한다.
- 저장소에 남는 변경을 하면 `docs/WORKLOG.md`에 날짜 기준으로 누적 기록한다.
- 질문만 답하는 작업이라도 저장소 맥락이 필요하면 관련 파일을 먼저 확인한 뒤 답한다.

## Canonical Docs
- `docs/ARCHITECTURE.md`: 현재 시스템 구조와 실행 흐름
- `docs/WORKLOG.md`: 작업을 통해 누적된 변경 기록

## Project Subagents
- 프로젝트 로컬 서브에이전트는 `.codex/agents/`를 기준으로 사용한다.
- 현재 설치한 기본 세트는 `code-mapper`, `python-pro`, `frontend-developer`, `react-specialist`, `typescript-pro`, `browser-debugger`, `test-automator`, `reviewer`다.
- 리액트 웹 패널 작업은 기본적으로 `code-mapper -> react-specialist -> frontend-developer -> typescript-pro -> test-automator -> reviewer` 순서로 필요한 범위만 호출한다.
- Python 워커, downstream, launchd, 스크립트 수정은 `code-mapper -> python-pro -> test-automator -> reviewer` 순서를 우선한다.
- 브라우저 재현, UI 상태 확인, 네트워크/콘솔 증거 수집이 필요할 때만 `browser-debugger`를 추가한다.
- 구조 파악만 필요한 요청에는 `code-mapper`만 쓰고, 구현 요청에 바로 `reviewer`를 먼저 붙이지 않는다.
- 변경 규모가 작으면 모든 서브에이전트를 동원하지 말고 최소 세트만 사용한다.
