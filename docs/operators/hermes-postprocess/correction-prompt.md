# Stage 3 Correction Prompt Policy

## Goal

ASR transcript를 “교정”한다. 목표는 원래 의미와 발화 순서를 유지하면서 명백한 전사 오류만 고치는 것이다.

교정은 요약이 아니다. 교정 단계에서 강의노트, 개요, 제목, 해설, 재구성, 내용 추가를 만들지 않는다.

## Primary prompt source

교정 품질/용어 지시는 기존 iCloud `05_prompt`를 우선한다.

1. `${LECTURE_RECORDINGS_ROOT}/05_prompt/00_base_prompt.txt`
2. `${LECTURE_RECORDINGS_ROOT}/05_prompt/01_common_glossary.txt`
3. `${LECTURE_RECORDINGS_ROOT}/05_prompt/glossary_{subject}.txt`

이 문서는 Hermes operator가 기존 prompt를 자동화 환경에서 안전하게 적용하기 위한 보강 규칙이다.

## Input

- raw txt: `02_transcripts/{stem}.txt`
- raw json: `02_transcripts/{stem}.json`
- subject code: filename에서 추출. 예: `260504DS_2` -> `DS`
- prompt/glossary files from `05_prompt`

## Output

- corrected txt: `03_correction/{stem}.txt`
- corrected json: `03_correction/{stem}.json`
- optional review-only misrecognition candidates: `state/hermes_postprocess/staging/{stem}/misrecognitions-candidates.json`

Final location에는 직접 쓰지 않는다. 먼저 staging에 쓴 뒤 validator와 promote를 거친다. Misrecognition candidates는 prompt 개선을 위한 수동 검토 큐에만 들어가며, 교정 artifact나 `05_prompt`를 자동 변경하지 않는다.

## Preserve exactly

JSON에서는 아래를 변경하지 않는다.

- top-level schema
- key names
- array ordering
- non-text scalar values
- `segments` length
- each segment `id`
- each segment `start`
- each segment `end`
- speaker/timestamp metadata

TXT에서는 줄바꿈과 segment boundary 의미를 가능한 한 보존한다.

## Allowed edits

- 명백한 ASR 오인식 수정
- 맞춤법, 띄어쓰기, 조사, 어미, 문장부호의 자연스러운 보정
- glossary 기반 전공 용어 복원
- 음운/문맥상 glossary 용어의 변형으로 확실한 경우 복원
- 영어 기술 용어, 약어, 함수명, 클래스명, 명령어, 파일명, 경로, 코드, 수식, 기호의 표준 표기 복원
- ASR 오류가 명백한 반복/추임새/잡음성 토큰 정리

## Forbidden edits

- 요약
- 짧게 압축
- 강의노트화
- 제목 생성
- 목록화
- 설명/해설 추가
- 원문에 없는 내용 추가
- 수식, 코드, 명령어, 고유명사 추측 생성
- segment 병합/분할
- 의미 변경
- 구어체를 과하게 문어체로 변경

## Uncertainty policy

불확실하면 원문을 유지한다.

문장의 절반 이상이 붕괴되어 의미를 확신하기 어려운 경우, 억지로 “그럴듯하게” 고치지 않는다. 원문을 유지하고 review note에 `[확인 필요]`를 남긴다.

JSON final artifact에는 review note를 섞지 않는다. review note가 필요하면 staging 내부 별도 파일에 둔다.

## Glossary precedence

1. subject glossary
2. common glossary
3. `00_base_prompt.txt`
4. transcript context
5. uncertainty -> preserve original

## Operator-specific guardrail

현재 iCloud `05_prompt/00_base_prompt.txt`는 Obsidian의 최종 교정 프롬프트 계열과 일치한다. 이 계열의 핵심은 “post-editing”이다. 과거 또는 다른 자료 요청 prompt의 짧은 요약 스타일을 correction 단계에 섞지 않는다.

## Optional misrecognition candidate notes

교정 중 추후 glossary/prompt 개선에 도움이 될 반복 오인식 후보가 보이면 별도 `misrecognitions-candidates.json`에만 기록한다.

- 짧은 phrase pair만 기록한다: `suspected_wrong`, `suggested_correct`.
- `scope`는 `common` 또는 `subject` 중 하나다.
- `confidence`는 `low`, `medium`, `high` 중 하나다.
- `reason`은 짧은 메타 설명만 쓴다.
- raw transcript excerpt, surrounding context, segment array, 긴 인용문은 기록하지 않는다.
- 이 후보는 수동 검토용이며 correction output이나 prompt 파일을 자동 변경하지 않는다.