# Failure Policy

## Normal no-op

`status: no_candidate` is a normal state. The cron run should produce no user-facing alert unless an explicit inventory/report run was requested.

## Failure classes

Use stable failure class names in reports.

- `missing_candidate_json`
- `invalid_candidate_json`
- `missing_raw_txt`
- `missing_raw_json`
- `missing_prompt_dir`
- `missing_base_prompt`
- `missing_common_glossary`
- `output_already_exists`
- `partial_correction_final_output`
- `summary_waiting_for_complete_correction`
- `promote_disabled`
- `promote_validation_missing`
- `stale_claim`
- `icloud_unavailable`
- `model_timeout`
- `model_interrupted`
- `child_hermes_failed`
- `postprocess_verification_failed`
- `invalid_correction_json`
- `correction_json_structure_changed`
- `correction_non_text_metadata_changed`
- `segment_count_mismatch`
- `segment_metadata_changed`
- `correction_validation_failed`
- `summary_required_heading_missing`
- `summary_too_short`
- `summary_validation_failed`
- `misrecognition_report_missing`
- `misrecognition_report_invalid`
- `promote_failed`

## Retry policy

- Missing prompt/doc/config errors: fail fast; require human/operator fix.
- Output already exists or partial final output: fail fast; never overwrite without explicit user approval.
- Promote disabled: expected Gate A/B behavior; do not retry as failure unless a Gate C promote was explicitly requested.
- Missing promote validation report: fail as `promote_validation_missing`; generate the missing validator report for the action whose `actions.*.needed` is true.
- Blocked candidate (`partial_correction_final_output`, `summary_waiting_for_complete_correction`): fail fast and inspect final correction state; do not promote partial output.
- Model timeout/interruption: release or mark claim retryable according to claim policy.
- Validator failure: do not auto-promote; keep staging for review if safe. Correction JSON structure or non-text metadata changes (`correction_json_structure_changed`, `correction_non_text_metadata_changed`) must be fixed in staging, not manually overridden.
- Promote path validation failure (`invalid_candidate_json`): regenerate candidate JSON from dry-run; do not edit destination paths by hand.
- Promote copy failure: treat as failed and inspect `promote_failed` details. The helper attempts hash-checked rollback for files created earlier in the same promote attempt.
- Misrecognition candidate report missing/invalid: keep correction/summary validation independent; do not modify `05_prompt`, do not echo offending raw values, and keep the failure metadata-only.
- iCloud unavailable: retry later; do not treat as content failure.

## Claim policy

A claim prevents duplicate processing by overlapping cron ticks.

Claim file should include:

```json
{
  "stem": "260504DS_2",
  "status": "claimed",
  "claimed_at": "ISO-8601",
  "operator": "hermes-cron",
  "pid_or_run_id": "..."
}
```

The active cron wrapper writes `claimed`, `failed`, or `completed` claim states and skips existing claims to avoid duplicate processing. Do not manually remove stale claims without inspecting the claim file, staging reports, final output existence, and child log path first.

## Failure report contract

Failure reports are metadata-only.

Allowed:

- stem
- failure class
- paths
- validator error message
- timestamps
- retryable flag
- next action

Forbidden:

- raw transcript body
- full corrected transcript
- full summary body
- prompt secrets or provider credentials

## User-facing failure message

Keep it short.

```text
lecture_stt postprocess failed
stem: {stem}
failure: {failure_class}
retryable: {true|false}
action: {short action}
```

## Escalation

Require explicit approval before:

- overwriting any final output
- deleting or moving source/final artifacts
- running bulk backlog processing
- changing cron schedule
- changing Hermes model/provider/profile
- modifying `05_prompt` source files
