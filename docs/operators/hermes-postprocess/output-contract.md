# Output Contract

## Canonical directories

환경 변수 기준:

- `${LECTURE_RECORDINGS_ROOT}/02_transcripts`: raw transcript txt/json
- `${LECTURE_RECORDINGS_ROOT}/03_correction`: corrected transcript txt/json
- `${LECTURE_RECORDINGS_ROOT}/04_summarize`: summary markdown
- `${LECTURE_RECORDINGS_ROOT}/05_prompt`: existing correction prompt/glossary surface

Repo-local operator state:

- `state/hermes_postprocess/claims/`
- `state/hermes_postprocess/staging/`
- `state/hermes_postprocess/reports/`
- `state/hermes_postprocess/cron-baseline.json`: activation-time backlog skip list for the active Hermes cron wrapper
- `state/hermes_postprocess/cron_logs/`: child Hermes run logs for cron-triggered content generation
- `state/hermes_postprocess/misrecognitions/pending.jsonl`: metadata-only manual-review queue for suspected ASR misrecognitions. This is local repo state, not an iCloud prompt file.

The helper is not hard-coded to iCloud. `LECTURE_RECORDINGS_ROOT` may point to the existing iCloud `lecture_recordings` root or to a fully local fixture/canary root with the same `02_transcripts`/`03_correction`/`04_summarize`/`05_prompt` layout. Gate A/B uses read-only discovery against iCloud only because the current live transcript and prompt source of truth already live there; generated artifacts still go through repo-local staging first.

## Candidate JSON contract

Candidate script stdout must be JSON and must not contain transcript body.

Candidate example:

```json
{
  "schema_version": 1,
  "status": "candidate",
  "stem": "260504DS_2",
  "subject": "DS",
  "raw_txt_path": "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/02_transcripts/260504DS_2.txt",
  "raw_json_path": "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/02_transcripts/260504DS_2.json",
  "correction_txt_path": "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/03_correction/260504DS_2.txt",
  "correction_json_path": "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/03_correction/260504DS_2.json",
  "summary_md_path": "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/04_summarize/260504DS_2.md",
  "staging_dir": "/Users/geonha/DEV/lecture_stt/state/hermes_postprocess/staging/260504DS_2",
  "claim_path": "/Users/geonha/DEV/lecture_stt/state/hermes_postprocess/claims/260504DS_2.json",
  "prompt_dir": "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/05_prompt",
  "operator_docs_dir": "/Users/geonha/DEV/lecture_stt/docs/operators/hermes-postprocess",
  "actions": {
    "correction": {
      "needed": true,
      "reason": "missing_final_pair",
      "final_txt_exists": false,
      "final_json_exists": false
    },
    "summary": {
      "needed": true,
      "reason": "missing_final_markdown",
      "input_source": "staging_correction",
      "final_md_exists": false
    }
  },
  "blocked_reasons": []
}
```

No candidate example:

```json
{
  "schema_version": 1,
  "status": "no_candidate",
  "reason": "no raw transcript without correction/summary"
}
```

## Staging layout

For stem `{stem}`:

```text
state/hermes_postprocess/staging/{stem}/
  candidate.json             # optional saved dry-run payload; metadata-only
  manifest.json              # metadata-only staging manifest
  correction.txt
  correction.json
  summary.md
  validation-correction.json
  validation-summary.json
  misrecognitions-candidates.json # optional operator-produced candidate list; metadata phrases only
  misrecognitions-review.md       # generated manual-review view; metadata phrases only
  misrecognitions-record.json     # record-misrecognitions report; metadata-only
  promote.json
  review-notes.md            # optional, metadata/review only
```

Staging files may contain transcript-derived content. Do not copy staging content into chat/report output.

`manifest.json` is metadata-only and must not include transcript body, timed chunk arrays, full corrected transcript, or full summary. It records schema version, stem, subject, candidate paths, action plan, blocked reasons, and privacy flags only.

## Misrecognition candidate review contract

Misrecognition accumulation is review-only. The operator may propose suspected repeated ASR mistakes, but the helper records them only in repo-local state; it never modifies `${LECTURE_RECORDINGS_ROOT}/05_prompt` or any final artifact.

Input file: `state/hermes_postprocess/staging/{stem}/misrecognitions-candidates.json`

```json
{
  "schema_version": 1,
  "kind": "hermes_postprocess_misrecognition_candidates",
  "stem": "260504DS_2",
  "subject": "DS",
  "raw_transcript_body_included": false,
  "candidates": [
    {
      "suspected_wrong": "데이터 베이스",
      "suggested_correct": "database",
      "scope": "subject",
      "confidence": "medium",
      "reason": "DS glossary 후보로 수동 검토 필요"
    }
  ]
}
```

Constraints:

- `suspected_wrong` and `suggested_correct` are short phrases only, max 80 characters each.
- `reason` is metadata only, max 240 characters.
- `scope` is `common` or `subject`.
- `confidence` is `low`, `medium`, or `high`.
- The report must not contain `raw_excerpt`, transcript context, `segments`, or long quote fields.
- If the report includes raw transcript body/context fields, `record-misrecognitions` fails as `misrecognition_report_invalid` and does not echo the raw value.

Record command:

```bash
python3 -m scripts.hermes_postprocess record-misrecognitions \
  --candidate-json state/hermes_postprocess/staging/{stem}/candidate.json \
  --candidates-json state/hermes_postprocess/staging/{stem}/misrecognitions-candidates.json
```

Outputs:

- `state/hermes_postprocess/staging/{stem}/misrecognitions-review.md`
- `state/hermes_postprocess/staging/{stem}/misrecognitions-record.json`
- append-only/deduped queue: `state/hermes_postprocess/misrecognitions/pending.jsonl`

Promotion from pending review items into `${LECTURE_RECORDINGS_ROOT}/05_prompt/01_common_glossary.txt` or `glossary_{subject}.txt` is a separate approval gate and is intentionally not implemented as an automatic write.

## Validation report schema

Validator commands write JSON reports with this shape:

```json
{
  "schema_version": 1,
  "passed": false,
  "failure_class": "summary_too_short",
  "message": "summary is too short for a long corrected transcript",
  "details": {}
}
```

`passed: true` reports have `failure_class: null`. Reports are metadata-only and must not include raw/corrected/summary body.

## Correction txt contract

- UTF-8 text
- corrected transcript body only
- no assistant preface
- no fenced code block wrapping
- no summary
- no explanation

## Correction json contract

- Valid JSON
- Preserve raw JSON top-level structure
- Preserve `segments` count
- Preserve each segment's `id`, `start`, `end`
- Preserve ordering
- Only text-bearing values intended for correction may change. Current primary target key is `text`.
- Do not append `[확인 필요]` notes after JSON. Review notes must be separate staging metadata.

Correction validation uses `scripts.hermes_postprocess.validators.validate_correction_artifacts()`, which preserves the segment count and `id/start/end` metadata and then recursively verifies that JSON keys, ordering, array lengths, and non-`text` values did not change. Only string values under keys named `text` may be corrected.

## Summary markdown contract

- UTF-8 Markdown
- Filename: `{stem}.md`
- Required headings:
  - `# ...`
  - `## 핵심 개요`
  - `## 주요 개념`
  - `## 세부 내용`
  - `## 예시 / 코드 / 수식`
  - `## 헷갈리기 쉬운 점`
  - `## 시험·과제·교수 강조사항`
  - `## 복습 질문`
- No transcript body dump
- No hallucinated external information
- Not an ultra-short summary

## Validation policy

Correction validation fails if:

- txt missing or empty
- json missing or invalid
- JSON segment count changed
- any segment `id`, `start`, or `end` changed
- final output path already exists
- correction output contains obvious assistant wrapper text

Summary validation fails if:

- md missing or empty
- required headings missing
- final output path already exists
- output is a tiny bullet summary despite long corrected input
- deterministic raw-dump guardrail detects a copied corrected-transcript prefix

Semantic checks such as contradiction detection, unsupported external information, and hallucinated exam points are operator/QA review obligations; the deterministic helper documents those constraints but does not claim full semantic proof.

## Promote policy

Promote is all-or-nothing for the candidate stem and the action plan.

- By default `python3 -m scripts.hermes_postprocess promote` returns `promote_disabled` and writes no final files.
- `--allow-promote` is required for any final write and is Gate C+ only.
- Candidate paths must match the expected `lecture_root` layout derived from `raw_txt_path`; tampered final paths fail as `invalid_candidate_json`.
- Candidates with non-empty `blocked_reasons` are refused before any final write.
- Only artifacts whose `actions.*.needed` is true are promoted, and only those actions require validation reports. A summary-only candidate may promote `summary.md` without rewriting an existing correction pair.
- Promote re-runs the required deterministic validators immediately before copying so stale/fake `passed: true` reports cannot bypass validation.
- If any required final destination exists, do not overwrite. The final file create path uses exclusive no-overwrite creation.
- If validation did not pass, do not promote.
- Multi-artifact promote is rollback-protected: if a later copy fails, files created earlier by the same promote attempt are removed only after hash verification.
- After promote, verify final files exist and record hashes in a metadata-only promote report.
