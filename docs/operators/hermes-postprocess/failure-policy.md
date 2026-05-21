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
- `preexisting_final_artifact_unsupported_type`
- `unauthorized_final_output_created_by_child`
- `unauthorized_final_output_modified_by_child`
- `unsafe_staging_artifact`
- `invalid_validation_report`
- `promote_validation_failed`
- `summary_validation_skipped`
- `sandbox_exec_missing`
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

## Operator action table

| Failure class | Meaning | Retry? | Operator action |
| --- | --- | --- | --- |
| `missing_candidate_json` / `invalid_candidate_json` | Candidate record is absent, malformed, or path-tampered. | No | Re-run read-only dry-run to regenerate metadata. Do not hand-edit final paths. |
| `missing_raw_txt` / `missing_raw_json` | Raw transcript pair is incomplete. | Later | Wait for ingestion/iCloud sync to finish; do not synthesize raw files. |
| `missing_prompt_dir` / `missing_base_prompt` / `missing_common_glossary` | Prompt source-of-truth is unavailable. | No | Restore or fix `05_prompt`; require human/operator fix before rerun. |
| `output_already_exists` / `partial_correction_final_output` / `summary_waiting_for_complete_correction` | Final output state would cause overwrite or partial downstream state. | No | Inspect final correction/summary state. Never overwrite or delete without explicit approval. |
| `promote_disabled` | Promote was intentionally invoked without Gate C `--allow-promote`. | No | Treat as expected in dry-run/Gate A/B. Only rerun with `--allow-promote` after explicit Gate C approval. |
| `promote_validation_missing` / `invalid_validation_report` / `promote_validation_failed` | Required validator report is missing, stale, invalid, or failed on revalidation. | No | Regenerate staging artifacts and validator reports; do not trust child-written pass reports. |
| `unsafe_staging_artifact` | Child-writable staged artifact is not a single-link regular file. | No | Fail closed. Inspect staging metadata only; remove/recreate staging under approval if needed. Do not follow or copy the artifact. |
| `preexisting_final_artifact_unsupported_type` | A final path already contains a symlink, directory, socket, device, or other non-regular entry. | No | Stop before child execution. Manually inspect and clean up only with explicit approval. |
| `unauthorized_final_output_created_by_child` | Child created a final artifact outside the parent-owned promote path. | No | Use quarantine path in metadata report. Review `unauthorized-final-artifacts/`; do not promote from it directly. |
| `unauthorized_final_output_modified_by_child` | Child modified, deleted, or replaced a pre-existing regular final artifact. | No | Parent restores snapshot when possible and quarantines tampered entry. Manually inspect before any retry. |
| `model_timeout` | Child Hermes run exceeded timeout. | Yes | Retry after checking claim/log metadata and whether any unauthorized final artifact was quarantined. |
| `model_interrupted` / wrapper exception / `sandbox_exec_missing` | Child or wrapper could not run safely. | Usually no | On macOS, install/restore `sandbox-exec` behavior or adjust operator environment; do not run unsandboxed as a workaround. |
| `child_hermes_failed` | Child Hermes exited non-zero. | No by default | Read metadata log path. Fix child prompt/tool/env issue; stdout/stderr are intentionally suppressed. |
| `postprocess_verification_failed` | Child exited zero but parent verification/promote/final checks failed. | No | Inspect `verification.promote_failure_class` and validator reports; fix staging then rerun. |
| `correction_json_structure_changed` / `correction_non_text_metadata_changed` / `segment_count_mismatch` / `segment_metadata_changed` | Correction JSON changed non-text structure or immutable segment metadata. | No | Regenerate correction JSON from raw JSON; do not manually override validator failure. |
| `summary_required_heading_missing` / `summary_too_short` / `summary_validation_failed` / `summary_semantic_guard_failed` / `summary_validation_skipped` | Summary output fails deterministic contract or was skipped because correction did not pass. | No | Regenerate summary after correction validation passes. Semantic/factual quality still needs human/QA review. |
| `misrecognition_report_missing` / `misrecognition_report_invalid` | Optional misrecognition review artifact is absent or contains forbidden/raw-like fields. | No | Keep correction/summary validation independent; regenerate short phrase-only candidate report. |
| `stale_claim` | Existing active claim exceeded stale threshold. | Maybe | Inspect claim, staging reports, final outputs, and child log path before removing/retrying. |
| `icloud_unavailable` | Live lecture root is unavailable. | Later | Retry after iCloud/local volume is available; do not treat as content failure. |

## Retry policy

- Missing prompt/doc/config errors: fail fast; require human/operator fix.
- Output already exists or partial final output: fail fast; never overwrite without explicit user approval.
- Promote disabled: expected Gate A/B behavior; do not retry as failure unless a Gate C promote was explicitly requested.
- Missing promote validation report: fail as `promote_validation_missing`; generate the missing validator report for the action whose `actions.*.needed` is true.
- Unsafe staging artifact (`unsafe_staging_artifact`): fail closed. The parent must not read, follow, hash, copy, or promote symlinks, hardlinks, directories, FIFOs, sockets, devices, or missing staged artifacts. Recreate staging from a clean child run after review.
- Invalid or failed validation report (`invalid_validation_report`, `promote_validation_failed`): fail closed and regenerate reports with parent validators. Child-written pass reports are not trusted.
- macOS sandbox missing (`sandbox_exec_missing`): fail closed on Darwin. Do not rerun the child unsandboxed; fix the local platform/runtime or run only a non-promoting local fixture check.
- Blocked candidate (`partial_correction_final_output`, `summary_waiting_for_complete_correction`): fail fast and inspect final correction state; do not promote partial output.
- Model timeout/interruption: release or mark claim retryable according to claim policy.
- Child final-artifact violation (`unauthorized_final_output_created_by_child`, `unauthorized_final_output_modified_by_child`): fail closed and inspect `state/hermes_postprocess/staging/{stem}/unauthorized-final-artifacts/`. The parent quarantines child-created finals and restores pre-existing regular finals from a pre-child snapshot before writing the failed claim. Do not manually promote from that staging directory until the quarantine entry is reviewed.
- Pre-existing non-regular final artifact (`preexisting_final_artifact_unsupported_type`): fail closed before running the child. Symlinks, directories, devices, sockets, and other non-regular entries in final locations require human cleanup/approval; the operator must not mutate them automatically.
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
