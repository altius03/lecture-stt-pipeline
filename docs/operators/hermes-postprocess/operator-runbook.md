# Hermes Postprocess Operator Runbook

## Purpose

Hermes cron이 `lecture_stt`의 raw transcript 하나를 선택해 Stage 3 correction과 Stage 4 summary를 수행한다. 이 operator는 앱 내부 LLM 기능이 아니라 외부 운영 계층이다.

## Required read order per run

후보가 있는 경우 Hermes agent는 작업 전에 아래 문서를 순서대로 읽는다.

1. `docs/operators/hermes-postprocess/operator-runbook.md`
2. `docs/operators/hermes-postprocess/output-contract.md`
3. `docs/operators/hermes-postprocess/failure-policy.md`
4. `docs/operators/hermes-postprocess/correction-prompt.md`
5. `docs/operators/hermes-postprocess/summary-prompt.md`
6. 후보 JSON의 `prompt_dir` 아래 `00_base_prompt.txt`
7. 후보 JSON의 `prompt_dir` 아래 `01_common_glossary.txt`
8. 후보 subject가 있고 파일이 존재하면 `glossary_{subject}.txt`

`05_prompt/PIPELINE.md`는 있으면 참고하되, deterministic contract보다 우선하지 않는다.

## Invariants

- 한 번에 하나의 stem만 처리한다.
- raw transcript body를 최종 응답, routine report, chat message에 포함하지 않는다.
- final output이 이미 존재하면 overwrite하지 않는다.
- 모든 generated artifact는 `state/hermes_postprocess/staging/{stem}/`에 먼저 쓴다.
- 오인식 후보 누적은 repo-local review queue에만 기록하며, `05_prompt`에는 자동 반영하지 않는다.
- final promote는 validator가 통과한 뒤에만 수행한다.
- promote CLI는 기본 비활성이며 `--allow-promote`가 있어야 final 경로에 쓴다.
- downstream distribution은 기존 downstream worker에게 맡긴다.
- no candidate는 정상 상태이며 조용히 종료한다.
- 활성 cron wrapper는 activation-time backlog를 건너뛰고 baseline 이후 새 후보만 처리한다.
- active claim은 exclusive create로 획득한다. 이미 claim된 stem은 건너뛰며, `claimed`/`running` 상태가 stale threshold를 넘으면 stale claim을 해제한 뒤 다시 후보가 될 수 있다.
- 운영 구현의 source of truth는 repo 안의 `scripts/hermes_postprocess/operator.py`와 `scripts/lecture_stt_postprocess_operator.py`이다. `~/.hermes/scripts/lecture_stt_postprocess_operator.py`는 repo-tracked operator를 호출하는 thin shim이다.

## Active Hermes cron registration

2026-05-20 후속 승인으로 아래 script-only cron job이 등록되어 있다.

- job id: `977667876027`
- name: `lecture_stt_postprocess_operator`
- schedule: `every 30m`
- deliver: `discord:#운영-보안`
- workdir: `/Users/geonha/DEV/lecture_stt`
- script: `~/.hermes/scripts/lecture_stt_postprocess_operator.py` thin shim → repo-tracked `scripts/hermes_postprocess/operator.py`
- mode: `no_agent=true`

## Hardening canary evidence

Latest local evidence file:

- `/Users/geonha/DEV/lecture_stt/state/reports/hermes_postprocess/hardening-canary-20260521.json`

Scope of that evidence:

- local temporary fixture roots only
- no iCloud final writes
- no cron registration or schedule changes
- active shim read/compile only

Observed results:

- empty local fixture `status`: exit 0, metadata-only JSON, `candidate_counts.pending=0`, `raw_transcript_body_included=false`
- empty local fixture `run-once`: exit 0, stdout length 0
- pending local fixture `status`: exit 0, metadata-only JSON, `candidate_counts.pending=1`, raw sentinel absent
- pending local fixture `run-once` with `/bin/false` child: exit 0, short metadata-only failure alert, `failure=child_hermes_failed`, `raw_transcript_body_included=false`, no final entries created
- active shim: `python3 -m py_compile /Users/geonha/.hermes/scripts/lecture_stt_postprocess_operator.py` exit 0

This evidence proves the repo-local wrapper's quiet/no-candidate and metadata-only failure contracts for fixture runs. It does not prove semantic correction/summary quality and does not approve live iCloud promotion.

Wrapper 동작:

1. `state/hermes_postprocess/cron-baseline.json`의 `skip_stems`에 있는 activation-time backlog는 건너뛴다.
2. baseline 이후 새 candidate 하나만 exclusive claim한다. 이미 claim된 stem은 건너뛰며 stale active claim은 threshold 이후 해제된다.
3. child Hermes CLI를 repo root에서 실행해 docs/prompt를 읽고 staging artifact를 만든다. Child stdout/stderr는 raw body 누출 방지를 위해 cron log에 저장하지 않는다.
4. deterministic validator와 `--allow-promote` promote가 모두 통과하면 final paths와 pass/fail metadata만 짧게 출력한다.
5. candidate가 없으면 stdout 없이 0으로 종료한다.

## Repo-local CLI

모든 명령은 repo root(`/Users/geonha/DEV/lecture_stt`)에서 실행한다.

`--lecture-root`가 없으면 helper는 `LECTURE_RECORDINGS_ROOT` 또는 `config/config.yaml`의 `paths.transcript_folder`에서 lecture root를 추론한다. 설정값에 `${LECTURE_RECORDINGS_ROOT}` 같은 미해결 환경변수가 남아 있으면 literal path로 조용히 스캔하지 않고 lecture root를 미해결 상태로 취급한다.

The postprocess helper can run fully local: pass a local directory with the same `lecture_recordings/{02_transcripts,03_correction,04_summarize,05_prompt}` shape to `--lecture-root`. iCloud is not required by the program. We use iCloud read-only in Gate B only because the current real transcripts and prompt source of truth already live there. Code, staging state, review queue, and tests remain repo-local.

Operator status is metadata-only and safe for routine inspection:

```bash
python3 scripts/lecture_stt_postprocess_operator.py status \
  --repo-root /Users/geonha/DEV/lecture_stt \
  --lecture-root "$LECTURE_RECORDINGS_ROOT" \
  --hermes-bin /Users/geonha/.local/bin/hermes
```

Run at most one candidate through the repo-tracked operator. This command prints nothing when there is no candidate; it only prints a short metadata-only completion/failure alert when a candidate is actually processed or blocked.

```bash
python3 scripts/lecture_stt_postprocess_operator.py run-once \
  --repo-root /Users/geonha/DEV/lecture_stt \
  --lecture-root "$LECTURE_RECORDINGS_ROOT" \
  --hermes-bin /Users/geonha/.local/bin/hermes
```

Operator containment rules:

- The child Hermes process is untrusted generation only. The parent owns validation, promotion, claim writes, and final artifact writes.
- On macOS, the child command is wrapped with `sandbox-exec` using deny-default plus explicit file-write allowlist for staging/log/temp/Hermes runtime paths. If `sandbox-exec` is unavailable on macOS, the operator fails closed instead of running the child unsandboxed.
- Before child execution, the parent snapshots pre-existing regular final artifacts with lstat mode and content hash. After every child outcome (success, nonzero exit, timeout, wrapper exception), the parent quarantines newly-created final artifacts, restores modified/deleted/replaced pre-existing regular final artifacts, and writes metadata-only failure reports.
- If a final path already contains a non-regular filesystem entry before the child runs (symlink, directory, device, socket, etc.), the operator fails as `preexisting_final_artifact_unsupported_type` without mutating that entry.
- Quarantined unauthorized final entries live under `state/hermes_postprocess/staging/{stem}/unauthorized-final-artifacts/` and must be reviewed manually before any further action.

## Platform matrix

| Platform/runtime | Child execution mode | Expected behavior | Operator action if unavailable |
| --- | --- | --- | --- |
| macOS with `sandbox-exec` | Child command is wrapped with `sandbox-exec -f <generated-profile>`. | Child may read repo/prompt/raw inputs, but file writes are denied by default and explicitly allowed only for staging, cron logs, temp, and Hermes runtime paths. Child stdout/stderr go to `subprocess.DEVNULL`. | Continue only after parent-owned validation/promote checks. |
| macOS without `sandbox-exec` | No child run. | Fail closed before running Hermes child. | Treat `sandbox_exec_missing`/wrapper failure as a platform failure. Do not run unsandboxed as a workaround. |
| Non-Darwin without `sandbox-exec` | Child runs without macOS sandbox wrapper. | Still uses parent-owned validation, single-link regular-file staging checks, final snapshot/quarantine/restore, and `subprocess.DEVNULL` stdout/stderr suppression. | Only use for local fixture/CI-style verification unless a separate OS sandbox/container policy is approved. |
| Any platform with unsupported final path entry | No child run. | Symlinks, directories, sockets, devices, or other non-regular final entries fail as `preexisting_final_artifact_unsupported_type`. | Manual cleanup/approval required; operator must not mutate those entries automatically. |
| Any platform with unsafe staged artifact | Parent validation/promote stops before reading/copying the staged artifact. | Symlinks, hardlinks, broken symlinks, directories, FIFOs, sockets, devices, or missing staged files fail as `unsafe_staging_artifact`. | Recreate staging from a clean child run after review. |

Read-only candidate discovery:

```bash
python3 -m scripts.hermes_postprocess dry-run \
  --lecture-root "$LECTURE_RECORDINGS_ROOT" \
  --repo-root /Users/geonha/DEV/lecture_stt
```

If an operator run needs a promote input later, save the metadata-only dry-run JSON as a candidate record after the wrapper has parsed `stem` and created that stem's staging directory. The candidate record may also live in a temporary path passed to `--candidate-json`. Do not include transcript body in this file.

Cron pre-run wrapper에서는 no-candidate가 routine output을 만들지 않도록 `--silent-no-candidate`를 붙인다.

```bash
python3 -m scripts.hermes_postprocess dry-run \
  --lecture-root "$LECTURE_RECORDINGS_ROOT" \
  --repo-root /Users/geonha/DEV/lecture_stt \
  --stable-for-sec 60 \
  --silent-no-candidate
```

Correction validation:

```bash
python3 -m scripts.hermes_postprocess validate-correction \
  --raw-json /path/to/02_transcripts/{stem}.json \
  --correction-txt state/hermes_postprocess/staging/{stem}/correction.txt \
  --correction-json state/hermes_postprocess/staging/{stem}/correction.json \
  --final-txt /path/to/03_correction/{stem}.txt \
  --final-json /path/to/03_correction/{stem}.json \
  --report-path state/hermes_postprocess/staging/{stem}/validation-correction.json
```

Summary validation:

```bash
python3 -m scripts.hermes_postprocess validate-summary \
  --summary-md state/hermes_postprocess/staging/{stem}/summary.md \
  --corrected-txt state/hermes_postprocess/staging/{stem}/correction.txt \
  --final-md /path/to/04_summarize/{stem}.md \
  --report-path state/hermes_postprocess/staging/{stem}/validation-summary.json
```

Optional misrecognition candidate accumulation for manual review:

```bash
python3 -m scripts.hermes_postprocess record-misrecognitions \
  --candidate-json state/hermes_postprocess/staging/{stem}/candidate.json \
  --candidates-json state/hermes_postprocess/staging/{stem}/misrecognitions-candidates.json
```

This writes only repo-local review artifacts and a deduped pending queue under `state/hermes_postprocess/misrecognitions/pending.jsonl`. It does not edit `05_prompt`; applying reviewed items to common or subject glossary is a separate approval gate.

Promote interface exists but is disabled by default. Gate C approval 전에는 `--allow-promote`를 실행하지 않는다.

```bash
python3 -m scripts.hermes_postprocess promote \
  --candidate-json state/hermes_postprocess/staging/{stem}/candidate.json
# returns promote_disabled and writes no final files

python3 -m scripts.hermes_postprocess promote \
  --candidate-json state/hermes_postprocess/staging/{stem}/candidate.json \
  --allow-promote
# Gate C 이후 명시 승인 때만 사용
```

Direct wrapper files are also available for script-style use:

- `python3 scripts/hermes_postprocess/validate_correction.py ...`
- `python3 scripts/hermes_postprocess/validate_summary.py ...`
- `python3 scripts/hermes_postprocess/promote_artifacts.py ...`

## Inputs

Candidate script는 transcript 본문을 stdout에 넣지 않고 경로와 metadata만 JSON으로 반환한다.

필수 입력:

- `stem`
- `subject`
- `raw_txt_path`
- `raw_json_path`
- `correction_txt_path`
- `correction_json_path`
- `summary_md_path`
- `staging_dir`
- `claim_path`
- `prompt_dir`
- `operator_docs_dir`

## Stage 3 correction procedure

1. `raw_txt_path`와 `raw_json_path`가 둘 다 존재하고 안정화되어 있는지 확인한다.
2. final correction path가 이미 존재하면 실패한다.
3. 기존 `05_prompt` 파일을 읽어 교정 규칙과 glossary를 적용한다.
4. `correction-prompt.md`의 operator guardrail을 적용한다.
5. 교정 txt를 staging에 쓴다.
6. 교정 json을 staging에 쓴다.
7. 교정 중 반복 가능성이 보이는 오인식 패턴이 있으면 `misrecognitions-candidates.json`에 짧은 후보 phrase만 기록한다. raw excerpt, segment array, transcript context는 넣지 않는다. 기록된 후보는 `record-misrecognitions`로 repo-local pending queue에만 누적되며 `05_prompt`에는 자동 반영하지 않는다.
8. `python3 -m scripts.hermes_postprocess validate-correction`로 검증한다.
9. 실패하면 final path에 아무것도 쓰지 않는다.

## Stage 4 summary procedure

1. Stage 3 staged correction validation이 통과했거나, final correction pair가 이미 존재하는 summary-only candidate인 경우에만 시작한다.
2. summary는 corrected transcript를 입력으로 사용한다. `actions.summary.input_source`가 `staging_correction`이면 staging correction txt를, `final_correction`이면 final correction txt를 `--corrected-txt`로 넘긴다.
3. `summary-prompt.md`의 상세 강의노트 스타일을 따른다.
4. 너무 짧은 압축 요약, bullet dump, 주요 개념 누락은 금지한다.
5. summary markdown을 staging에 쓴다.
6. `python3 -m scripts.hermes_postprocess validate-summary`로 검증한다.
7. 실패하면 final path에 아무것도 쓰지 않는다.

## Promote procedure

1. candidate JSON의 `blocked_reasons`가 비어 있어야 한다. `partial_correction_final_output` 같은 blocked candidate는 promote를 거부한다.
2. candidate JSON의 raw/final/prompt paths가 같은 `lecture_root` layout에서 계산된 expected path와 일치해야 한다. tampered path면 `invalid_candidate_json`으로 거부한다.
3. candidate JSON의 `actions.*.needed`에 해당하는 staging artifact와 validation report가 존재해야 한다. 필요 없는 action의 validation report는 요구하지 않는다.
4. validation report가 pass여야 하며, promote 직전에 deterministic validator를 다시 실행해 stale/fake pass report를 거부한다.
5. 필요한 final destination 중 하나라도 이미 존재하면 promote를 중단한다.
6. `python3 -m scripts.hermes_postprocess promote --candidate-json ... --allow-promote`로 promote를 수행한다. 파일 생성은 exclusive no-overwrite 방식이며, multi-artifact copy 중 뒤쪽 단계가 실패하면 이번 promote에서 이미 만든 final file을 hash 확인 후 rollback한다.
7. promote 후 final files의 existence/hash를 확인한다.

## Reporting

성공 시 최종 응답은 짧게 유지한다.

```text
lecture_stt postprocess complete
stem: {stem}
outputs:
- {correction_txt_path}
- {correction_json_path}
- {summary_md_path}
validation: correction=pass, summary=pass, promote=pass
```

실패 시에는 failure class, stem, 필요한 action만 보고한다. transcript 본문이나 긴 generated output을 포함하지 않는다.

## Actions requiring explicit approval

- existing final output overwrite
- bulk backlog run
- correction/summary prompt policy 변경
- iCloud archive/cleanup/delete/move
- cron schedule 변경
- Hermes model/provider/profile/privacy routing 변경
- downstream worker restart
- git commit/push/tag/release
