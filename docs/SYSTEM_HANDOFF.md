# Lecture STT 인수인계서

**2026-11-27 기준.** 이 시스템은 강의·회의 녹음에서 시간표시가 있는 전사문을 만들고, 과목이 확인된 강의를 다시 읽을 수 있는 노트로 정리한다. 음성 처리는 Mac mini에서 실행한다. GitHub에는 프로그램과 작업 규칙이 있고, 녹음·전사·운영 DB는 운영자의 저장 공간에 있다. 저장소만 받은 사람도 코드와 테스트를 살펴볼 수 있다. 특정 녹음의 완료 여부를 판단하려면 그 녹음을 처리한 호스트의 기록이 필요하다.

## 처음 5분

1. `git status --short --branch`와 `git rev-parse HEAD`로 현재 작업 위치를 확인한다. GitHub 기본 브랜치는 `dev`다. 아래에 설명하는 학기별 Codex 후처리와 Go 컨트롤러 코드는 `main`에 있다. 두 브랜치는 이력이 갈라져 있어 작업 대상 코드가 어느 쪽에 있는지 먼저 본다.
2. 사람과 도구가 공유하는 [작업 규칙](../AGENTS.md)을 읽는다. Claude용 진입점도 같은 파일을 가리킨다. 다른 도구에는 이 인수인계서와 작업 규칙 두 파일을 건네면 된다.
3. 질문이 **프로그램의 기능**이면 [구조 문서][arch]와 대상 브랜치의 소스를 본다. **실제 녹음의 처리 결과**라면 아래의 읽기 전용 조회부터 시작한다. **이전 결정의 이유**는 [운영 코드의 작업 이력][history]과 [이슈별 기록 양식](work-log/README.md)에서 찾는다.

### 처음 설치하거나 장애를 넘겨받을 때

- **코드만 검토할 때**는 테스트용 임시 폴더와 SQLite를 사용한다. 새 Mac에서 서비스를 띄우려면 [설치 절차](https://github.com/altius03/lecture-stt-pipeline/blob/main/README.md)에 따라 Python 가상환경과 운영 설정을 준비한다. 실제 녹음 루트와 활성 학기 설정은 운영자에게 확인한다.
- **학기가 바뀌면** 시간표, 과목 코드, 과목별 목적지와 활성화 시작 시점이 함께 맞아야 한다. 큐에 이미 등록한 항목은 등록 당시 경로를 보존하므로 설정을 수정한 뒤 기존 작업 하나와 새 작업 하나를 구분해 확인한다.
- **실패가 반복되면** 작업 ID로 오류·재시도 상태와 보관된 오디오부터 본다. 전사 실패는 설정된 횟수만큼 재시도하며, 한도 초과 시 오류 기록과 최종 실패 오디오를 확인한다. 모델 변경·서비스 재시작·원본 이동이 필요하면 먼저 해당 호스트의 상태와 복구 방법을 기록한다.
- **동시간대 녹음은 별도 자료일 수 있다.** 11월 배치에는 42분 이상 겹치는 녹음 두 건이 있었다. 시간이 겹쳐도 해시가 다른 원본은 각각 보존하고, 강의별 노트에서 합칠 때 같은 발언인지 확인한다.

## 한 건이 지나가는 경로

| 단계 | 입력과 처리 | 다음 단계에서 사용할 근거 |
|---|---|---|
| 1. 접수 | iCloud Drive의 `lecture_recordings/00_inbox`에 음성 파일이 도착한다. 감시기가 파일의 안정화를 확인한 뒤 작업을 등록하고 오디오를 `01_audio`에 보관한다. | 녹음의 내용 해시(SHA-256), SQLite의 작업 ID, 보관된 오디오 경로 |
| 2. 전사 | Python 워커가 faster-whisper로 음성을 텍스트로 바꾼다. 운영 설정 예시는 한국어 `large-v3`이며 실제 사용 모델과 설정은 해당 작업의 메타데이터를 확인한다. | `02_transcripts`의 읽기용 TXT, 각 구간의 ID·시작·끝·본문을 담은 JSON, 품질 기록 `.quality.json` |
| 3. 품질 판정 | 생성한 파일과 전사량·반복·인식 시간 지표를 검사한다. 낮은 품질로 판정된 전사에도 검토 가능한 파일이 남을 수 있다. | 전사 상태 `DONE`, `NEEDS_REVIEW`, `ERROR`와 품질 지표를 함께 읽는다. |
| 4. 교정·요약 | 전사 완료, 학기 활성화 시점, 과목 연결 조건을 충족하면 별도 후처리 작업을 등록한다. Codex CLI로 전사 텍스트를 보내 교정 TXT·JSON과 요약 Markdown을 생성한다. 이 단계에서 전사 내용이 Codex 서비스로 전달된다. | `transcript_postprocess_jobs.source_job_id`가 전사 작업 ID를 가리킨다. 교정·요약·전달 상태와 산출물 경로가 각각 기록된다. |
| 5. 전달 | 학기 설정에 지정된 과목 폴더로 결과를 전달한다. 기존 파일과 내용이 다르면 충돌로 남겨 확인할 수 있게 한다. | 교정본은 `06_lecture_notes/02_origin`, 요약은 `06_lecture_notes/01_summarize`에서 실제 파일과 해시를 확인한다. |

과목이 정해진 새 전사 파일은 `02_transcripts/<학기>/<과목>/` 아래에 생긴다. 과목 미확정 녹음이나 이전 버전의 파일은 `02_transcripts` 바로 아래에도 있다. 파일명이 같을 수 있으므로 DB가 기록한 경로와 오디오 해시로 찾는다.

## 어디를 읽으면 되는가

| 대상 | 역할 |
|---|---|
| [전사 워커][stt] | 감시, 파일 선점, 전사, 품질 검사, 실패 복구를 담당한다. 기본 설정은 Python 실행 주체를 사용하며, Go 컨트롤러를 선택할 수도 있다. |
| [Go 컨트롤러][controller] | 설정에 따라 새 파일과 재시도 대상을 판정하고 Python 단일 작업 실행을 조율한다. 컨트롤러 바이너리·LaunchAgent 상태와 실제 실행 주체는 운영 호스트에서 확인한다. |
| [후처리 워커][downstream] | 활성 학기, 과목별 경로, 교정·요약·전달을 다룬다. 교정 JSON의 구간 구조를 검증하고 원전사의 시간표시를 보존한다. |
| [Storage v2][storage] | 녹음, 파일, 버전, 검토·분류 근거를 별도로 보존하는 계층이다. 과거 파일을 가져오거나 정본을 바꾸는 작업은 먼저 계획과 대상 해시를 확인한다. 오늘 전사한 작업의 상태는 우선 `state/jobs.sqlite3`에서 찾는다. |
| [웹 패널][panel] | React 화면에서 작업·품질·검토 상태를 살펴본다. 다른 Mac의 같은 포트에서 HTTP 응답을 받았다는 사실만으로 운영 워커의 상태를 판단할 수 없다. |
| [설정 예시][config]와 [운영 절차][ops] | 녹음 루트, 활성 학기, 모델, 알림, 서비스 구동 및 복구 절차를 확인한다. 실제 경로와 자격 증명은 해당 호스트의 로컬 설정을 사용한다. |

## 이 시스템이 바뀌어 온 과정

아래 개발 이력은 Git 커밋과 저장소 작업 기록을 기준으로 했다. 작업 기록에 적힌 서비스 상태와 파일 개수는 그 날짜의 관찰값이다.

| 시기 | 변경과 그 이유 |
|---|---|
| 2026년 2월 | 초기 파이프라인에 설정 검증, 일시정지·재개, 성공·실패 알림과 중복 알림 방지를 도입했다. 장시간 돌아가는 전사를 운영자가 통제하고 결과를 알 수 있게 했다. [초기 신뢰성 개선](https://github.com/altius03/lecture-stt-pipeline/commit/3b397f0) |
| 3~4월 | 전사 이후의 배포 워커와 상태 조회 CLI를 추가하고 React 운영 패널을 정비했다. 파일 선점 경합과 중단 복구를 보강해 재시작 시 같은 입력을 다시 처리하는 위험을 줄였다. [배포 워커](https://github.com/altius03/lecture-stt-pipeline/commit/4cc4661) · [파일 선점 복구](https://github.com/altius03/lecture-stt-pipeline/commit/5f66e53) |
| 5월 | 재시도·최종 실패 처리, 운영 경로 이전 절차, 품질 기록 파일을 정비했다. 원전사와 품질 기록이 함께 보존되므로 파일 생성 여부와 품질을 각각 점검할 수 있다. [재시도 정책](https://github.com/altius03/lecture-stt-pipeline/commit/06a78d2) · [품질 기록](https://github.com/altius03/lecture-stt-pipeline/commit/8312890) |
| 7월 | Storage v2에 원본 보존·검토·정본 반영 경로를 추가했다. Go 컨트롤러와 Python 실행기를 연결하고 실제 macOS 파일 동작을 점검하는 테스트를 마련했다. [저장소 변경](https://github.com/altius03/lecture-stt-pipeline/commit/6d3d077) · [컨트롤러 변경](https://github.com/altius03/lecture-stt-pipeline/commit/6d75cf3) |
| 8~9월 | 8월 작업 기록에 학기별 시간표와 과목 경로, Codex CLI 교정·요약이 등장한다. 9월 커밋에서 학기별 후처리와 전사 미리보기를 통합하고 복구·중복 처리·운영 초기화를 보강했다. 9월 28일에는 당시 자료를 보관하고 런타임 상태를 초기화한 기록이 있다. 이후 실제 처리 여부는 이후 날짜의 작업으로 판단한다. [학기별 후처리](https://github.com/altius03/lecture-stt-pipeline/commit/b6dfed8) · [복구와 초기화](https://github.com/altius03/lecture-stt-pipeline/commit/8c8c40f) |
| 10~11월 | 중간고사와 이후 녹음을 대량 처리하면서 같은 제목의 녹음, 시간표와 실제 내용의 불일치, 낮은 음량, 교정 누락을 운영 단계에서 발견했다. 아래 두 사례가 그때 확립한 확인 방법이다. |
| 11월 27일 | 공개 저장소에 시스템 개요와 이슈별 작업 기록·PR 검증 규칙을 도입했다. 두 브랜치에 각각 병합했으며, 문서 병합과 운영 Mac 배포는 별개의 작업이다. [개요 PR](https://github.com/altius03/lecture-stt-pipeline/pull/3) · [작업 규칙 이슈](https://github.com/altius03/lecture-stt-pipeline/issues/4) |

### 운영 사례 1 · 중간고사 녹음 71개

10월 14일에는 6과목 녹음 60개(약 40시간 29분)를 올렸고 10월 20일에 11개를 추가했다. 같은 날짜·과목의 여러 녹음은 같은 제목을 쓰므로 녹음 ID, 시작 시각, 해시를 따로 관리했다. 같은 파일명을 접수할 때는 앞 파일의 등록을 확인한 다음 뒤 파일을 넣었다. 첫 60개의 원본 작업은 58개가 완료, 2개가 검토 필요였다. 문제 녹음의 원본과 기존 전사를 보존하고 보정 사본으로 다시 시도했다. 10월 추가분 11개는 날짜·과목별 다섯 묶음으로 정리해 기존 수업 필기에 전달했다.

### 운영 사례 2 · 중간고사 이후 녹음 67개

1. **범위 확정.** 음성 메모에 새 녹음 70개가 있었다. 별도로 전사한 3개를 대조해 제외하고 67개(약 41시간 58분)를 접수했다. 강의 62개, 회의 1개, 포럼 4개였다.
2. **전사 확인.** 첫 실행에서 66개가 완료, 1개가 검토 필요였다. 완료된 것 중에도 인식량이나 시간 범위가 낮은 녹음이 있어 총 3개를 음량 보정 사본으로 재전사했다. 최종 선택된 67개 모두 TXT와 구간 JSON을 확인했다.
3. **과목 확인.** 음성 메모의 위치 기반 제목만으로는 강의를 식별하기 어려웠다. 시간표와 전사 내용을 대조하고, 정규 수업과 시간이 겹친 외부 행사는 운영자 확인을 받았다. 같은 과목·날짜에는 같은 제목을 적용하고 사본은 시작 시각별 폴더로 구분했다.
4. **교정 확인.** 장소 이름으로 접수한 최종 전사 67개의 후처리 기록은 `SKIPPED`, 분류 사유는 `not_lecture`였다. 제목을 나중에 바꾼 사실만으로 교정이 실행됐다고 보면 안 된다. 이 배치에서는 확정된 분류와 교안을 사용해 교정본을 별도로 만들고 24,769개 구간의 ID·순서·시간을 원전사와 대조했다.
5. **읽기용 보완과 검토.** 문맥으로 복원한 요지, 원음 확인이 필요한 표현, 5분 이상 전사가 빈 시간대의 학습 보충을 구분했다. 최종본에도 원음 확인 표시가 148구간 남았고, 긴 공백 7곳의 설명에는 교안 쪽수를 붙였다. 의미가 뒤집힌 문장 등 네 대목을 추가 검토해 고쳤다.
6. **전달 검증.** 두 Mac에서 최종 TXT·JSON 134개와 목록 1개의 SHA-256이 일치했다. Notion에서 강의는 기존 날짜별 필기 35곳, 회의·포럼은 별도 페이지 2곳에 반영하고 본문과 목차 링크를 다시 읽었다. 게시용 문안으로 다듬은 일부 문단이 있어 Notion은 전송한 문안과 대조했다.

음성 메모 추출·제목 변경, 교안 대조를 거친 읽기용 보완, Notion 게시는 이 사례에서 별도로 수행한 일이다. 새 설치에서 워커를 시작하면 이 과정까지 자동으로 실행된다고 가정하지 않는다. 공개 기록은 집계와 검증 방법을 제공하며 개별 원음·강의 내용은 운영자가 관리한다.

## 운영자가 처음 받는 세 가지 질문

**“파일을 넣었는데 끝났나요?”** 운영 Mac의 저장소 루트에서 다음 조회를 실행한다. 예시 번호 `123`은 앞선 조회에서 찾은 실제 작업 ID로 바꾼다. 같은 이름의 파일이 여러 개면 해시로 구분한다.

```bash
git status --short --branch
git rev-parse --short HEAD
sqlite3 -readonly state/jobs.sqlite3 'SELECT id, orig_name, status, sha256 FROM jobs ORDER BY id DESC LIMIT 5;'
sqlite3 -readonly state/jobs.sqlite3 'SELECT source_job_id, status, route_method, correction_status, summary_status, delivery_status FROM transcript_postprocess_jobs WHERE source_job_id = 123;'
```

첫 조회는 전사 단계, 두 번째는 `main`의 후처리 단계를 보여 준다. 후처리 행이 없거나 건너뛰었다면 과목 분류와 학기 활성화 기준을 확인한다. 아직 원래 작업이 등록되지 않은 파일을 다시 제출하면 중복될 수 있다. DB 행의 상태에 더해 기록된 TXT·JSON 경로에서 파일을 읽어야 완료를 확인할 수 있다.

**“DONE이면 공부에 쓸 만큼 정확한가요?”** 마지막으로 인식된 시각이 녹음 길이와 가까워도 중간에 긴 공백이 있을 수 있다. 예를 들어 60분 녹음에서 마지막 발화가 59분에 잡히면 끝 지점 비율은 약 98%다. 앞·중간의 공백은 이웃한 구간의 시간 차이로 찾는다. 반복 경고도 같은 문장이 그 비율만큼 반복됐다는 뜻이 아니다. 의심 구간의 원음·발언 문맥·교안을 대조한 후 교정하고, 공백에 넣은 학습 설명을 발언 인용과 구분한다.

**“두 기기나 Notion에도 반영됐나요?”** 로컬 파일을 만들었다면 로컬 생성까지 확인한 것이다. 기기 간 동일성을 확인할 때는 양쪽에서 같은 대상 목록을 읽어 바이트 해시를 비교한다. Notion은 게시 위치·과목·날짜·수록한 녹음 수를 맞추고 저장한 본문을 다시 읽는다. 운영 호스트에 연결되지 않으면 마지막으로 관찰한 시점과 아직 확인할 수 없는 단계를 함께 보고한다.

## 다음 담당자에게 넘기는 형식

아래 항목을 인수인계 메시지에 채우면 IDE, 터미널, AI 도구가 바뀌어도 같은 지점에서 시작할 수 있다. [운영 런북][ops]은 실행 절차를, [모델 문서][models]는 모델 변경·벤치마크 기준을 다룬다.

```text
목표와 완료 조건:
대상 브랜치 / 커밋 / 작업 호스트:
관련 이슈 / 이전 결정 기록:
대상 녹음 또는 작업 ID와 선택 기준:
확인한 단계와 증거 (전사·교정·요약·전달):
남은 불확실성 / 다음으로 확인할 파일이나 상태:
```

코드를 바꿀 때는 해당 브랜치에서 이슈별 작업을 시작하고 같은 브랜치로 PR을 보낸다. Python 전사·후처리는 단위 테스트, 웹 패널은 프런트엔드 테스트와 빌드, 컨트롤러는 Go 테스트와 macOS 파일시스템 검증이 관련된다. 실제 실행된 검사 이름은 그 PR의 Checks에서 확인한다. 로컬 검증·GitHub CI·운영 호스트 실동작은 각각 수행한 범위로 기록한다. [운영 코드의 CI][ci]

[arch]: https://github.com/altius03/lecture-stt-pipeline/blob/main/docs/ARCHITECTURE.md
[history]: https://github.com/altius03/lecture-stt-pipeline/blob/main/docs/WORKLOG.md
[ops]: https://github.com/altius03/lecture-stt-pipeline/blob/main/docs/OPERATIONS.md
[storage]: https://github.com/altius03/lecture-stt-pipeline/blob/main/docs/STORAGE_V2.md
[models]: https://github.com/altius03/lecture-stt-pipeline/blob/main/docs/MODELS.md
[config]: https://github.com/altius03/lecture-stt-pipeline/blob/main/config/config.example.yaml
[stt]: https://github.com/altius03/lecture-stt-pipeline/tree/main/src/lecture_stt/stt
[controller]: https://github.com/altius03/lecture-stt-pipeline/tree/main/controller
[downstream]: https://github.com/altius03/lecture-stt-pipeline/tree/main/src/lecture_stt/downstream
[panel]: https://github.com/altius03/lecture-stt-pipeline/tree/main/frontend/web-panel
[ci]: https://github.com/altius03/lecture-stt-pipeline/blob/main/.github/workflows/ci.yml
