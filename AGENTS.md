# Repository Workflow

## Default Workflow
- 저장소 관련 요청을 받으면 항상 이 순서로 진행한다.
- 1. 파일 확인: 관련 코드, 설정, 테스트, 실행 스크립트를 먼저 읽는다.
- 2. 작업: 실제 코드 기준으로 영향 범위를 파악하고 필요한 처리 방식을 정한다.
- 3. 수정: 필요한 파일만 최소 범위로 수정한다.
- 4. 검증: 가능한 범위에서 테스트, 실행, diff 확인으로 결과를 검증한다.

## 이슈에서 PR까지
- 저장소 변경은 GitHub 이슈에 이유와 완료 조건을 적고 시작한다. 코드가 있는 기준 브랜치를 확인한 뒤 최신 원격 기준에서 이슈별 작업 브랜치를 만든다. PR은 같은 기준 브랜치로 보낸다.
- 작업을 시작하거나 rebase할 때 새로 들어온 관련 작업 기록의 배경과 결정 근거를 읽는다. 충돌은 양쪽 작업의 의도와 현재 코드의 계약을 확인한 뒤 해결한다.
- 이슈와 PR은 한국어로 변경 이유부터 설명한다. PR 제목은 `feat:`, `fix:`, `docs:`, `chore:` 등 변경 종류로 시작한다. 이슈를 마치는 PR은 본문에 `Closes #번호`, 후속 PR이 남아 있으면 `Refs #번호`를 쓴다.
- PR에는 상세 기록 링크와 실제 검증 결과를 적는다. 로컬 검증, GitHub CI, Mac mini 운영 확인은 각각 수행한 범위로 보고한다.

## Documentation Rules
- 구조 변경이나 새 서브시스템이 생기면 `docs/ARCHITECTURE.md`를 갱신한다.
- 저장소 변경은 [이슈별 작업 기록 양식](docs/work-log/README.md)을 따른다. 새 기록은 [날짜별 인덱스](docs/WORKLOG.md)에 연결한다. 작은 변경의 예외 기준도 양식 문서에 있다.
- 질문만 답하는 작업이라도 저장소 맥락이 필요하면 관련 파일을 먼저 확인한 뒤 답한다.

## Canonical Docs
- 처음 참여하거나 작업을 이어받을 때 [시스템·운영 이력 인수인계서](docs/SYSTEM_HANDOFF.md)에서 코드 기준, 단계별 완료 조건, 증거 위치를 확인한다.
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
