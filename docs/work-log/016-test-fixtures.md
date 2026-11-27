# 컨트롤러 픽스처와 웹 패널 테스트 정리

## 배경

컨트롤러 테스트 세 곳에서 같은 launchd 템플릿과 설치 스크립트를 만들었다. 웹 패널에서는 기능 하나를 고칠 때 한 디렉터리에서 API·디코더·훅·화면 테스트를 찾아야 했고, JSON 응답이나 지연된 요청을 만드는 코드도 여러 파일에 흩어져 있었다.

## 무엇을 할 것인가

테스트 준비 코드를 공유하고 웹 패널 테스트를 검증 대상별로 묶는다. 기존 케이스의 이름과 수, 테스트별 격리, CI의 개별 모듈 호출을 보존한다.

## 대안과 트레이드오프

모든 컨트롤러 테스트에 전체 번들을 만들면 topology 검사가 런타임 파일 없이도 동작하는지 확인하기 어렵다. React Query 클라이언트를 한 번만 만들면 앞선 테스트의 캐시가 다음 테스트 결과에 영향을 줄 수 있다.

## 결정 근거

topology에는 launchd 파일만 만들고, bundle과 activation에서만 런타임 소스와 번들 입력을 추가한다. 웹 훅은 테스트마다 새 클라이언트를 만들고, 같은 캐시·재시도 설정을 쓰는 훅끼리 생성 함수를 공유한다. fetch 스파이와 모듈 모형의 등록·복원은 각 테스트 파일에서 관리한다.

## 작업 내용

- `tests/fixtures/controller_repo.py`에 launchd 저장소와 번들 픽스처를 모았다. activation의 전용 디렉터리와 bundle의 kill switch 정리 확인은 해당 테스트에서 준비한다.
- `tests/fixtures/controller_files.py`에서 링크 자체를 교체하는 쓰기, 파일 모드 지정, 읽기 전용 트리 정리를 공유한다. 실행 파일은 `0700`, 설정 파일은 `0600`으로 만든다.
- `frontend/web-panel/test/` 아래에 `api/`, `decoders/`, `hooks/`, `panels/`를 두고 도메인별 응답 데이터는 `fixtures/`에 모았다. API의 JSON 응답과 훅의 지연 Promise·QueryClient 도우미는 사용하는 디렉터리에 둔다. 전체 앱 테스트와 공통 `setup.ts`는 기존 위치에서 실행한다.

## 결과

| 로컬 전체 테스트 | 변경 전 수집 / 통과 | 변경 후 수집 / 통과 | 추가·누락된 이름 |
| --- | ---: | ---: | ---: |
| Python unittest | 866 / 866 | 866 / 866 | 0 |
| 웹 패널 Vitest | 268 / 268 | 268 / 268 | 0 |

## 확인 사항

- 기준 커밋 `3a9245b`와 변경 후 트리에서 `unittest.defaultTestLoader.discover("tests", pattern="test_*.py")`로 수집한 `TestCase.id()` 전체를 정렬해 비교했다. Python 3.12.8에서 `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v`로 전후 전체를 실행했다.
- Node 22.23.5에서 `vitest list --no-staticParse --json`으로 수집하고 `npm test -- --reporter=json`으로 실행했다. 전후 모두 32개 파일이며 그중 31개가 이동했다. 파일명이 각 시점에서 유일한지 확인하고 **파일명 + 상위 describe 전체 + 케이스 이름**의 정렬 목록을 대조했다. 수집 목록과 실제 통과 목록도 일치했다.
- 컨트롤러 세 테스트의 초기 디렉터리·파일 경로, 모드, 파일 내용 해시를 비교했다. 설정 파일의 임시 루트 경로만 통일했을 때 topology 10개, bundle 23개, activation 24개 항목이 각각 일치했다.
- 컨트롤러 테스트 메서드 43개의 Python 구문 트리와 웹 테스트 파일 32개의 `describe` 본문을 이전 버전과 비교했다. 픽스처 참조와 이동한 경로의 표기를 맞추면 모두 일치했다.
- CI의 컨트롤러 Python 모듈 9개를 `python -m unittest`에 이름으로 지정해 125개가 통과했다. `python -m compileall -q scripts src tests`와 웹 패널 `npm run build`도 통과했다. [모듈별 CI 호출](../../.github/workflows/ci.yml)
- GitHub CI 결과는 날짜별 인덱스에 연결한 PR의 Checks에서 확인한다. 운영 Mac mini 배포와 실동작 확인은 수행하지 않았다.

## 앞으로 할 일

없음.
