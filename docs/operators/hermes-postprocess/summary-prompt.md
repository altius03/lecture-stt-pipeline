# Stage 4 Summary Prompt Policy

## Goal

교정 완료 transcript를 바탕으로 복습 가능한 마크다운 강의노트를 만든다.

이 작업은 “짧은 요약”이 아니다. 원문 전체를 그대로 재현하지는 않지만, 시험/복습에 필요한 개념, 정의, 예시, 코드/수식, 교수 강조사항, 헷갈리기 쉬운 점을 충분히 남기는 구조화 노트가 목표다.

## Input

- preferred input: staging 또는 final correction txt
- optional reference: corrected json
- subject glossary: `05_prompt/glossary_{subject}.txt` if available
- common glossary: `05_prompt/01_common_glossary.txt`

Raw transcript보다 corrected transcript를 우선한다. correction과 summary가 충돌하면 correction을 기준으로 한다.

## Required output

Markdown file: `04_summarize/{stem}.md`

권장 구조:

```markdown
# {강의 제목 또는 stem 기반 제목}

## 핵심 개요

## 주요 개념

## 세부 내용

## 예시 / 코드 / 수식

## 헷갈리기 쉬운 점

## 시험·과제·교수 강조사항

## 복습 질문
```

강의에 해당 내용이 없으면 섹션을 생략하지 말고 `- 명시적으로 언급되지 않음`처럼 표시한다. 단, 없는 내용을 만들어내지 않는다.

## Style

- 한국어 학습 노트 스타일로 작성한다.
- English proper nouns, API 이름, 코드 식별자, 명령어는 원 표기를 유지한다.
- 전문 용어는 glossary 표기를 따른다.
- 단순 bullet dump를 피하고, 개념 간 관계를 설명한다.
- “흐름” 같은 표현을 반복적으로 남용하지 않는다. 필요한 경우 “진행 내용”, “전개”, “연결 관계” 등으로 자연스럽게 쓴다.
- 시험 대비에 도움이 되도록 정의, 차이점, 조건, 절차, 예외를 분리한다.

## Forbidden output style

아래 스타일은 실패로 본다.

- 5~10줄짜리 초압축 요약
- 제목과 bullet 몇 개만 있는 형태
- 핵심 개념 정의 없이 “무엇을 배웠다”만 나열
- 원문에 있는 예시/코드/수식/주의사항을 전부 누락
- corrected transcript와 모순되는 내용
- 외부 지식으로 내용을 보강
- hallucinated 시험 포인트

## Detail policy

짧게 만들려고 하지 않는다. 정보량은 입력 transcript의 밀도에 비례해야 한다.

- 긴 강의 transcript에서 summary가 지나치게 짧으면 warning/fail 대상이다.
- 원문 전체 복사는 금지하지만, 중요한 정의와 예시는 충분히 남긴다.
- 교수자가 반복하거나 강조한 내용은 따로 표시한다.
- 불확실한 내용은 단정하지 말고 “전사/교정 기준으로는 불확실”이라고 표시한다.

## Source limitation

summary는 corrected transcript와 provided glossary만 근거로 한다. 외부 지식, 일반 상식, 검색 결과를 이용해 강의 내용을 보강하지 않는다.

## Privacy/reporting

생성된 summary 본문을 Hermes final chat report에 길게 붙이지 않는다. final report에는 stem, output path, validation result만 포함한다.
