# Hermes Postprocess Operator

이 문서는 `lecture_stt`의 외부 Hermes cron/operator 계층 설계 문서다.

## 결론

`lecture_stt` 앱 내부에는 LLM provider/API를 다시 넣지 않는다. 앱은 STT, metadata, downstream distribution, validation, dashboard를 담당하고, 교정/요약 자동화는 Hermes cron이 수행하는 별도 operator phase로 둔다.

## Runtime source of truth

교정 작업은 기존 iCloud prompt surface를 우선 사용한다.

- `${LECTURE_RECORDINGS_ROOT}/05_prompt/00_base_prompt.txt`
- `${LECTURE_RECORDINGS_ROOT}/05_prompt/01_common_glossary.txt`
- `${LECTURE_RECORDINGS_ROOT}/05_prompt/glossary_{subject}.txt`
- `${LECTURE_RECORDINGS_ROOT}/05_prompt/PIPELINE.md`는 참고 자료로만 사용한다.

Repo-local operator docs는 기존 지시문을 대체하지 않는다. 역할은 Hermes cron이 안전하게 후보 선택, staging, validation, promote를 수행하도록 운영 규칙과 산출물 계약을 고정하는 것이다.

## Prompt lineage note

Obsidian에는 여러 prompt 계열이 있다.

- `GH_archive/01_TUK/06_others/lecture_notes_prompt/README.md`
- `GH_archive/01_TUK/06_others/lecture_notes_prompt/prompt_{subject}.md`
- `GH_archive/01_TUK/06_others/lecture_notes_prompt/prompt_review_report_v2.md`
- `GH_archive/01_TUK/06_others/lecture_notes_prompt/자료 요청 방법/...`
- `GH_archive/01_TUK/06_others/lecture_notes_prompt/전사파이프라인/...`

현재 iCloud `05_prompt/00_base_prompt.txt`는 교정 전용 최종본 계열로 보인다. 이 파일의 핵심은 “교정은 하되 요약, 재구성, 해설, 내용 추가는 금지”다.

요약 문서는 별도로 만든다. 다만 잘못된 old style처럼 너무 짧게 압축하는 방향은 금지한다. Stage 4의 목표는 짧은 bullet summary가 아니라 복습 가능한 구조화 강의노트다.

## Pipeline shape

한 cron tick은 stem 하나만 처리한다.

```text
02_transcripts/{stem}.txt + {stem}.json
  -> scripts.hermes_postprocess dry-run candidate discovery
  -> staging correction
  -> optional metadata-only misrecognition candidates for manual review
  -> validate correction
  -> staging summary
  -> validate summary
  -> explicit promote to 03_correction and/or 04_summarize
  -> downstream worker distributes existing final artifacts
```

## Repo-local implementation

Phase 2~4 개발 범위의 결정적/비LLM 부분은 `scripts/hermes_postprocess/` 패키지에 있다.

- `python3 -m scripts.hermes_postprocess dry-run ...`: 후보 1개와 필요한 작업을 metadata-only JSON으로 출력한다.
- `scripts/hermes_postprocess/picker.py`: raw transcript pair 중 complete final output이 없는 stem 하나만 고른다.
- `scripts/hermes_postprocess/paths.py`: parameterized `lecture_root` 아래 raw/final/prompt 경로와 repo-local staging/claim/docs 경로를 계산한다.
- `scripts/hermes_postprocess/prompts.py`: `05_prompt`의 base/common/subject glossary를 로드한다.
- `scripts/hermes_postprocess/validators.py`: correction/summary 산출물 계약을 검증한다.
- `scripts/hermes_postprocess/misrecognitions.py`: 짧은 오인식 후보 phrase를 repo-local manual-review queue에 누적한다. raw excerpt/context와 `05_prompt` 자동 변경은 금지한다.
- `scripts/hermes_postprocess/staging.py`: metadata-only manifest 작성과 explicit promote 인터페이스를 제공한다.
- `scripts/hermes_postprocess/cli.py`: dry-run, validation, misrecognition recording, promote subcommand를 제공한다.

이 패키지는 후보 발견, 경로 계산, staging manifest, validator, review-only 오인식 후보 누적, promote safety만 담당한다. transcript를 교정/요약하는 LLM content generation은 Hermes operator가 문서를 읽고 수행하는 별도 단계다. 초기 개발 범위에서는 cron 등록이나 iCloud final 쓰기를 활성화하지 않았고, 2026-05-20 후속 승인으로 아래 activation status에 적힌 제한된 canary/promote/cron만 별도로 수행했다.

## Local vs iCloud runtime boundary

프로그램은 iCloud에 고정되어 있지 않다. `--lecture-root`로 같은 폴더 구조를 가진 로컬 디렉터리를 넘기면 완전히 로컬 fixture/canary root에서 동작한다. 현재 운영 문서에서 iCloud를 read-only로 보는 이유는 실제 raw transcript와 `05_prompt` source of truth가 이미 iCloud `lecture_recordings`에 있기 때문이다. 개발/테스트/오인식 후보 review queue는 repo-local `state/hermes_postprocess/`에 둬서 iCloud final 산출물과 prompt 파일을 승인 없이 건드리지 않는다.

## 2026-05-20 activation status

후속 승인으로 live stem `260504DS_1` 1건에 대해 content staging, deterministic validation, Gate C+ promote canary를 수행했다. Final iCloud 산출물은 `03_correction/260504DS_1.txt`, `03_correction/260504DS_1.json`, `04_summarize/260504DS_1.md` 3개이며, staged artifact와 final artifact hash가 일치함을 metadata-only report로 확인했다. Raw transcript body와 generated body는 worklog/report/chat에 싣지 않는다.

Hermes cron은 script-only job `lecture_stt_postprocess_operator`로 등록되어 있다.

- job id: `977667876027`
- schedule: `every 30m`
- workdir: `/Users/geonha/DEV/lecture_stt`
- script: `~/.hermes/scripts/lecture_stt_postprocess_operator.py`
- mode: `no_agent=true`; script stdout이 비어 있으면 delivery도 조용하다.
- backlog guard: activation-time backlog는 `state/hermes_postprocess/cron-baseline.json`의 `skip_stems`로 건너뛰고, cron은 baseline 이후 새 후보만 처리한다.

## Documents

- `operator-runbook.md`: Hermes cron agent 행동 규칙
- `correction-prompt.md`: Stage 3 교정 지침. 기존 `05_prompt`를 우선 사용한다.
- `summary-prompt.md`: Stage 4 요약/강의노트 생성 지침. 짧은 요약 금지.
- `output-contract.md`: 후보 JSON, staging, final output, validator 계약
- `failure-policy.md`: 실패 분류, 재시도, 보고 정책
- `cron-prompt.md`: cronjob에 넣을 짧은 실행 prompt 원본

## Privacy boundary

Hermes cron operator가 cloud LLM provider를 쓰면 transcript content가 해당 provider로 전송될 수 있다. “외부 provider 전송 없음”이 요구되면 cronjob 생성 전에 Hermes model/provider/profile을 local/private 경로로 별도 승인받아야 한다.

## Approval boundary

초기 Phase 2~4 승인 범위는 repo-local CLI, validators, fixture tests, docs 정합성까지만 포함했다. 2026-05-20 후속 승인으로 `260504DS_1` 1건 promote와 위 cron 등록을 수행했다.

아래는 계속 별도 승인 전 금지한다.

- existing final output overwrite
- cleanup/delete/move
- bulk backlog processing 또는 activation-time backlog 처리
- `05_prompt` 자동 수정
- Hermes model/provider/privacy routing 변경
- cron schedule/delivery/script 변경
