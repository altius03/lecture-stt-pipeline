# Lecture STT Remaining Hardening Plan

> **For Hermes:** 새 세션에서 이 계획을 로드한 뒤, 구현 단계에서는 `test-driven-development`, `systematic-debugging`, `requesting-code-review`, 필요 시 `github-pr-workflow` skill을 사용한다. 변경 전 반드시 `git status --short --branch`, `git diff --stat`, `git diff --check`를 확인하고 기존 미커밋 변경을 보존한다.

**Goal:** 최초 고도화 계획에서 남은 8개 작업 묶음을 안전하게 마무리해 `lecture_stt`를 운영 가능한 STT + manual correction/summary + downstream 배포 파이프라인으로 안정화한다.

**Current Context:**
- Repo: `/Users/geonha/DEV/lecture_stt`
- Branch: `checkpoint/pre-web-panel-redesign`
- Remote: `origin https://github.com/altius03/lecture-stt-pipeline.git`
- Current HEAD at plan time: `4ec8fd6 [verified] Remove Anthropic correction coupling`
- Current branch was synced with origin at plan time.
- Already completed:
  - downstream log burst mitigation and JSONL rotation for downstream worker
  - DB integrity/foreign key checks
  - model benchmark smoke and `large-v3` keep decision
  - isolated `faster-whisper==1.2.1` check, production `.venv` not upgraded
  - Claude/Anthropic API coupling removed; correction is manual/provider-neutral
  - downstream problem rows read-only triaged in `docs/DOWNSTREAM_TRIAGE_2026-05-19.md`
- Hard constraints:
  - no remote push/tag/release unless explicitly requested in that session
  - no model switch yet; keep `large-v3`
  - production `.venv` upgrade is now conditionally allowed only as a separate runtime package update: inspect live state first, show plan/rollback, run targeted verification, and do not combine with model switching
  - no destructive DB/file migration without explicit approval
  - preserve uncommitted changes
  - do not print secrets

---

## Workstream 0: Session Setup and Safety Gate

**Objective:** Start from live state, not from stale handoff text.

**Read-only steps:**
1. `cd /Users/geonha/DEV/lecture_stt`
2. `git status --short --branch`
3. `git diff --stat`
4. `git diff --check`
5. `git log --oneline --decorate -5`
6. Read:
   - `docs/HANDOFF_RESTRUCTURE_AND_MODEL_UPGRADE.md`
   - `docs/DOWNSTREAM_TRIAGE_2026-05-19.md`
   - `docs/RUNTIME_INVENTORY_2026-05-19.md`
   - `docs/MODELS.md`
   - `docs/WORKLOG.md`
   - `config/config.example.yaml`
   - `src/lecture_stt/downstream/lib.py`
   - `src/lecture_stt/stt/main.py`

**Acceptance criteria:**
- Current repo status is known.
- Any pre-existing uncommitted changes are preserved and not overwritten.
- The implementation session has a fresh task list.

---

## Workstream 1: Downstream Conflict Resolution Plan and Safe Execution

**Objective:** Resolve or queue the 41 downstream problem rows without overwriting user data.

**Known state:**
- `docs/DOWNSTREAM_TRIAGE_2026-05-19.md` classifies 41 problem rows.
- 25 rows appear to be true source/destination hash conflicts.
- 1 row, `260422LC`, appears stale/DB-only or source-missing.
- 8 rows are invalid stem issues.
- 7 rows need subject route/rename confirmation.

**Implementation approach:**
1. Write/read a read-only diagnostic script or add CLI dry-run mode if missing.
2. Recompute current source/destination existence and hashes from live filesystem.
3. Produce a machine-readable conflict table under `state/reports/` or `docs/` only if approved for repo docs.
4. Split rows into:
   - `same-content-now`: can be marked idempotent/delivered if DB stale
   - `source-missing`: DB stale, candidate for row clear after approval
   - `dest-missing`: rerun downstream delivery candidate
   - `hash-conflict`: manual canonical decision required
   - `route/rename-needed`: rename/mapping required
5. Implement only safe, explicit operations:
   - no overwrite by default
   - no source deletion
   - no destination deletion
   - row clear only with `--dry-run` and `--yes`
6. Add tests around any new status/repair CLI behavior.

**Likely files:**
- `src/lecture_stt/downstream/status.py`
- `src/lecture_stt/downstream/lib.py`
- `tests/test_distribute_status.py`
- `tests/test_distribute_lib.py`
- `docs/DOWNSTREAM_TRIAGE_2026-05-19.md`
- possible new doc: `docs/DOWNSTREAM_RESOLUTION_PLAN_2026-05-XX.md`

**Validation:**
- `PYTHONPATH=src .venv/bin/python -m unittest tests.test_distribute_status tests.test_distribute_lib -v`
- Dry-run report shows no file/DB mutation.
- If actual repair is approved, before/after row counts and file hashes are logged.

**Decision gate:** Requires user decisions D1-D5 below before destructive or canonical-changing actions.

---

## Workstream 2: STT Retry and `99_errors` Failure Policy

**Objective:** Implement deterministic retry/failure behavior for STT jobs.

**Known draft policy:**
- 2 retries, then `99_errors`.
- Need final decision on whether original audio moves to `99_errors` or remains in `01_audio` with diagnostic copy.

**TDD plan:**
1. Read current job lifecycle in `src/lecture_stt/stt/main.py` and DB schema in `src/lecture_stt/shared/db.py`.
2. Add failing tests for:
   - transient failure increments retry count and returns job to pending/retryable state
   - retry limit reached moves or copies failed input according to chosen policy
   - failure report includes stable reason and paths but no secrets
   - startup recovery does not infinite-loop terminal failures
3. Implement minimal DB metadata or reuse existing fields if enough.
4. Ensure idempotent behavior on restart.
5. Document failure states.

**Likely files:**
- `src/lecture_stt/stt/main.py`
- `src/lecture_stt/shared/db.py`
- `src/lecture_stt/shared/utils.py`
- `tests/test_stt_main.py`
- possibly `tests/test_failure_policy.py`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`

**Validation:**
- targeted STT tests
- full unittest
- compileall

**Decision gate:** Requires user decisions F1-F4.

**Current implementation note (2026-05-19):** Retry policy implemented with `app.transcribe_max_retries=2` default. Transient transcription failures become `PENDING` rows with `전사 재시도 대기 n/2`; the next scan processes retry rows immediately. Final failure moves canonical audio to `99_errors`, keeps the DB row terminal `ERROR`, and redacts Authorization/Bearer/JWT/`sk-` secret surfaces. PROCESSING retry recovery restores the retry marker from `engine_params` so crash/restart does not lose retry state.

---

## Workstream 3: Log Retention and Rotation Policy Across Workers

**Objective:** Apply a coherent log retention policy to app/downstream/launchd logs.

**Already done:**
- downstream JSONL size guard/rotation
- downstream routine stdout suppression
- old huge downstream logs gzip-archived/truncated once

**Remaining approach:**
1. Inventory current log paths and sizes:
   - `logs/app.log*`
   - `state/logs/downstream.*`
   - launchd stdout/stderr paths from installed plist
2. Decide retention policy: size-based, time-based, or both.
3. Add shared rotation helper if useful, without overengineering.
4. Ensure structured JSONL keeps enough diagnostic info while avoiding repeated problem spam.
5. Add tests for rotation edge cases.
6. Update docs and config example.

**Likely files:**
- `src/lecture_stt/downstream/lib.py`
- `src/lecture_stt/stt/main.py` or logging setup module if present
- `config/config.example.yaml`
- `launchd/*.plist` if path comments/templates need update
- `tests/test_distribute_lib.py`
- possible new `tests/test_logging_policy.py`
- `docs/OPERATIONS.md` or `docs/ARCHITECTURE.md`

**Validation:**
- rotation tests
- diff check
- manual dry-run with small max bytes in temp dir

**Decision gate:** Requires user decisions L1-L4.

**Current implementation note (2026-05-19):** Shared log retention helper and `scripts/rotate_logs.py` added. Plain app/launchd logs use size-bounded rotation and copytruncate semantics so running launchd file descriptors keep writing to the active log. Compressed archive pruning remains explicit/dry-run first and old archive/cleanup remains approval-gated.

---

## Workstream 4: Runtime/State/Tmp/Cache Structure Cleanup and Migration Plan

**Objective:** Separate source repo from runtime data, then clean stale runtime artifacts safely.

**Known inventory:**
- Repo runtime-like dirs: `audio/`, `errors/`, `inbox/`, `logs/`, `models/`, `state/`, `tmp/`, `transcripts/`
- Legacy path `/Users/geonha/lecture_stt` exists and appears stale.
- `tmp/*.wav` stale candidates exist.
- `state/` is active and must not be moved blindly.

**Plan:**
1. Create a migration proposal first, not implementation.
2. Define target paths, e.g. one of:
   - keep `state/` in repo for now
   - move to `~/Library/Application Support/lecture_stt/state`
   - move logs to `~/Library/Logs/lecture_stt`
   - move cache/tmp to `~/Library/Caches/lecture_stt` or repo `tmp/`
3. Add config options if needed, preserving backwards compatibility.
4. Add archive script or documented manual steps.
5. Only after approval:
   - stop affected launchd jobs
   - copy/archive old state/logs
   - update config/plist
   - restart launchd
   - verify DB integrity and worker status
6. Keep rollback instructions.

**Likely files:**
- `config/config.example.yaml`
- `scripts/setup_launchd.sh`
- `launchd/*.plist`
- `src/lecture_stt/shared/paths.py`
- `scripts/cleanup.py`
- `docs/RUNTIME_MIGRATION_PLAN_2026-05-XX.md`
- `docs/OPERATIONS.md`

**Validation:**
- path resolution tests
- setup script dry-run/render check if available
- DB integrity check before/after actual migration

**Decision gate:** Requires user decisions R1-R6.

**Current implementation note (2026-05-19):** Path defaults and example config now target macOS standard log/tmp/cache locations while keeping DB in repo state. Migration/rollback is documented in `docs/RUNTIME_MIGRATION_PLAN_2026-05-19.md`; actual config migration, installed plist changes, log/tmp cleanup, DB/file movement, and launchd restart remain approval-gated.

---

## Workstream 5: Model/Package Operational Policy

**Objective:** Keep `large-v3` as production default while deciding whether any runtime/package experiments proceed.

**Known result:**
- `large-v3` remains canonical production model.
- turbo/distil/Korean turbo candidates failed current quality bar.
- `faster-whisper==1.2.1` was faster but showed output shrink/regression signs on short sample; production `.venv` should not be upgraded yet.

**Plan:**
1. Do not change production model/config by default.
2. Decide whether to run more isolated `faster-whisper==1.2.1` samples.
3. If yes, expand isolated benchmark only; no production `.venv` mutation.
4. Document package update policy separately from model switch policy.
5. Add rollback/canary notes.

**Likely files:**
- `docs/MODELS.md`
- `scripts/benchmark_models.py`
- `tests/test_benchmark_models.py`
- `docs/WORKLOG.md`

**Validation:**
- benchmark artifacts stay under `state/benchmarks/`, not committed unless summary-only
- benchmark script tests

**Decision gate:** Requires user decisions M1-M4.

---

## Workstream 6: Representative Benchmark Expansion

**Objective:** If useful, expand benchmark coverage beyond current short/medium smoke results.

**Candidate samples from docs:**
- `260312OOP_3` short OOP
- `260504DS_1` medium DS
- `260504LC_2`
- `260430Dstr_2`
- `260429LA`
- `260407OOP_1`
- `260429Unix_2`
- `260415LA` long sample, final candidates only

**Plan:**
1. Only run if there is a candidate worth testing.
2. Keep canonical `02_transcripts` untouched.
3. Save raw benchmark outputs only in private `state/benchmarks/`.
4. Commit summary docs only, not raw transcript artifacts.
5. Add a small parser/report command if repeated comparison is needed.

**Validation:**
- `scripts/benchmark_models.py --print-plan`
- candidate guard prevents accidental candidate/download runs
- benchmark output path required

**Decision gate:** Requires user decisions B1-B4.

---

## Workstream 7: Operational Canary and Live Observation

**Objective:** Verify the actual launchd-backed pipeline with 1-2 real or copied sample inputs after code changes.

**Plan:**
1. Define canary input policy:
   - real next lecture only, or
   - copied sample file with clear test stem, or
   - no canary until next actual lecture
2. Before canary:
   - verify launchd status
   - verify DB integrity
   - verify inbox empty or controlled
   - pause/stop downstream if needed
3. Run `run_once` or launchd-backed processing according to chosen path.
4. Observe:
   - job state transition
   - transcript txt/json creation
   - quality report
   - notification behavior if enabled
   - downstream behavior for manual correction/summary
5. Record result in `docs/WORKLOG.md` or `docs/OPERATIONS.md`.

**Likely files:**
- no code change unless bugs found
- `docs/WORKLOG.md`
- `docs/OPERATIONS.md`

**Validation:**
- DB state DONE or expected terminal state
- no unexpected overwrite
- no log spam recurrence

**Decision gate:** Requires user decisions C1-C5.

**Current implementation note (2026-05-19):** Read-only readiness check completed. STT/downstream launchd jobs have PIDs, DB integrity/foreign key checks pass, no PROCESSING/retry/ERROR STT rows are present, inbox is empty, and existing 41 downstream problem rows remain known non-blocking issues as long as canary does not clear DB rows or overwrite destinations. Actual canary waits for the next real lecture input.

---

## Workstream 8: Operations Documentation Consolidation

**Objective:** Convert scattered handoff/worklog/model/runtime notes into a reliable operator-facing guide.

**Plan:**
1. Create `docs/OPERATIONS.md` unless user prefers updating README only.
2. Cover:
   - normal startup/shutdown/status commands
   - launchd labels
   - worker responsibilities
   - pause/resume
   - retry/failure policy
   - log locations/retention
   - downstream conflict triage/repair flow
   - model benchmark and no-upgrade policy
   - canary checklist
   - rollback procedures
3. Update `README.md` to point to `docs/OPERATIONS.md`.
4. Keep `docs/HANDOFF_RESTRUCTURE_AND_MODEL_UPGRADE.md` as historical planning/handoff, not primary ops truth.
5. Fix stale checklist boxes if docs remain active.

**Likely files:**
- `docs/OPERATIONS.md`
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`
- possibly `docs/HANDOFF_RESTRUCTURE_AND_MODEL_UPGRADE.md`

**Validation:**
- commands in docs match actual script names and launchd labels
- no secrets in docs
- docs mention that push/release/tag are separate approvals

**Decision gate:** Requires user decisions O1-O3.

---

## Recommended Execution Order in New Session

1. Workstream 0 — safety/read-only setup.
2. Workstream 8 — create/update `docs/OPERATIONS.md` skeleton early so decisions have a home.
3. Workstream 2 — retry/failure policy, because it affects operational semantics.
4. Workstream 3 — log retention policy, because it affects launchd/runtime operations.
5. Workstream 1 — downstream conflict resolution, after canonical choices are clear.
6. Workstream 4 — runtime migration/cleanup plan; actual migration only after approval.
7. Workstream 7 — canary/live observation after code/log/failure changes.
8. Workstream 5/6 — model/package and expanded benchmark only if user wants more evidence despite current `large-v3` keep decision.

Alternative if user wants lowest risk first:
1. docs-only decisions and operations doc
2. conflict dry-run report
3. retry/failure tests and implementation
4. log policy
5. canary
6. cleanup/migration
7. optional benchmark

---

## Standard Verification Gate for Each Code Batch

Run before any commit:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python -m compileall -q scripts src tests
git diff --check
git status --short --branch
git diff --stat
```

For security-sensitive or broad changes:
- added-line secret/shell/eval/pickle/SQL static scan
- independent code review via subagent

Commit style:
- Small local commits per workstream.
- No push unless user explicitly asks in that session.

---

# User Decision Snapshot — 2026-05-19 user reply

Source of truth for the current decision codes: `docs/REMAINING_HARDENING_DECISION_GUIDE_2026-05-19.md`.
This section supersedes the older D/F/M-style decision snapshot that was produced before the remaining-hardening guide existed.

User-provided decisions:

```text
C1=1, C2=1, C3=1, C4=1, C5=1
D1=1, D2=1, D3=1, D4=1, D5=3, D6=1, D7=1
R1=3, R2=1, R3=1, R4=1, R5=1, R6=1, R7=2
L1=1, L2=1, L3=3, L4=1
P1=2, P2=2, P3=3, P4=1
B1=1, B2=1, B3=1, B4=1
O1=1, O2=1, O3=1
```

## Approval boundary from this decision set

Approved now:

- Update this plan with the user decisions.
- Run read-only checks and non-mutating dry-runs.
- Generate downstream diagnostic/manual-table reports under gitignored `state/reports/` if needed for D7, as long as DB rows and source/destination files are not modified.
- Write/update documentation summaries for already-run read-only/dry-run work.

Not approved without a fresh explicit approval:

- DB row delete/update/clear, including any `clear-stale --yes` operation.
- Source/destination file delete/move/rename/overwrite.
- Runtime path migration apply, operating `config/config.yaml` mutation, installed LaunchAgent mutation, or launchd restart.
- Old log cleanup/archive apply, truncate, or rotation `--apply`.
- Production `.venv` package install/upgrade/downgrade.
- Model download, model switch, or candidate model execution beyond explicit benchmark approval.
- Local commit; before committing, show diff summary and wait for approval.
- Remote push, tag, or release.

Canonical STT model remains `large-v3`.

## Post-decision explicit approvals and execution status — 2026-05-19

Later in the same operational session, the user explicitly approved the previously gated runtime migration, STT worker restart, and copied-sample canary. This section records that later approval without changing the original decision snapshot provenance.

Completed under explicit approval:

- Runtime migration applied: STT tmp now uses `~/Library/Caches/lecture_stt/tmp`; app/downstream/webpanel/cleanup logs and launchd stdout/stderr now use `~/Library/Logs/lecture_stt`; DB remains in repo `state/jobs.sqlite3`.
- Runtime package workstream completed: production `.venv` and `requirements.txt` now align on `faster-whisper==1.2.1`; this did not change `transcribe.model_size`, and canonical STT model remains `large-v3`.
- Package rollback, if later needed, is to restore `requirements.txt` to `faster-whisper==1.1.0`, reinstall that exact version in `.venv`, restart only affected launchd labels after approval, and rerun targeted tests plus the next canary.
- Migration backup: `state/backups/runtime-migration-20260519T114522Z`.
- Post-migration log rotation remained dry-run only: `state/reports/log-rotation-post-migration-dry-run-20260519T114610Z.txt`.
- First launchd copied canary `260519OOP_1` reached job `201` but failed with `VadOptions.__init__() got an unexpected keyword argument 'onset'`; the failed DB row and `99_errors` artifact were preserved as evidence.
- Root cause fix: `STTWorker` now builds `vad_parameters` according to the installed faster-whisper `VadOptions` signature, covering both 1.1-style `onset/offset` and 1.2-style `threshold` APIs. Canonical STT model still remains `large-v3`.
- Second launchd copied canary `260519OOP_2` completed as job `202` with `DONE`, transcript txt/json present, report `state/reports/canary-20260519T120017Z.json`.

Later explicit execution request in this session:

- User requested continuing remaining work through execution and main-branch push, while still keeping `large-v3`, preserving rollback evidence, and reporting through Telegram.
- Downstream repair applied with backup `state/backups/downstream-repair-20260519T123211Z` and manifest `state/reports/downstream-repair-apply-20260519T123211Z.json`: 9 source-canonical rename-needed pairs were repaired and processed by downstream; 3 DB-only stale rows were cleared; `260422LC` remained preserved.
- Canary failure evidence cleanup applied with report `state/reports/stt-error-cleanup-20260519T123547Z.json`: job `201` terminal `ERROR` row and `99_errors/260519OOP_1.m4a` remained preserved; stale cache tmp wav was removed after backup.
- Runtime cleanup remained dry-run/list only because the manifest included broad audio/transcript/log/archive/legacy deletion candidates. Report directory: `state/reports/runtime-cleanup-20260519T123847Z`.
- Main push is approved by the later explicit request; tag and release remain out of scope.

Still not approved unless explicitly requested later:

- Tag or release.
- Broad audio/transcript cleanup apply, old log archive deletion/prune, or legacy root deletion beyond dry-run/list.
- Model switch away from `large-v3`.

## C — Canary / live observation

- C1=1: Canary input is the next real lecture only. Do not copy an old sample into the inbox as a test canary.
- C2=1: One completed real lecture is the immediate pass criterion; 24h idle observation or the next 2 real lectures are a separate stability gate.
- C3=1: Keep the downstream worker running during canary.
- C4=1: Keep the current notification config during canary.
- C5=1: Record canary results in `docs/WORKLOG.md`.

Implication: wait for a real lecture input. `scripts/run_once.sh` remains debugging/preflight only; the actual canary pass is launchd-backed unless a later failure analysis requires otherwise.

## D — Downstream problem rows

- D1=1: Start now with read-only report/manual table only.
- D2=1: Treat source `03_correction` as the default canonical side for hash conflicts.
- D3=1: Later destination overwrite may be allowed only after dry-run table, destination backup, before/after hash check, and explicit approval.
- D4=1: For route/rename-needed rows, create a manual table and ask the user to confirm.
- D5=3: For invalid stem rows, the user decides file-by-file from the table.
- D6=1: Leave `260422LC` in place and document only.
- D7=1: Store downstream report artifacts under gitignored `state/reports/` as JSON/CSV when a file artifact is useful.

Implication: downstream next step is `diagnose`/manual table generation only. No DB clear, no rename, no route config change, no destination overwrite.

## R — Runtime path migration

- R1=3: Do canary first, then runtime path migration.
- R2=1: Actual order is canary first, migration second.
- R3=1: Move logs/tmp/cache outside the repo eventually; keep DB in repo.
- R4=1: launchd restart is allowed later only after exact plan/rollback is shown and approved label-by-label.
- R5=1: Legacy `/Users/geonha/lecture_stt` gets read-only inspection first; decide later.
- R6=1: Keep empty repo runtime folders as placeholders.
- R7=2: For stale `tmp/*.wav`, create a read-only list only.

Implication: do not apply migration in this batch. Read-only inventory and exact plan/rollback updates are okay.

## L — Old logs archive/cleanup

- L1=1: Generate dry-run/manifest only for old log cleanup.
- L2=1: Keep compressed archive retention at 30 days.
- L3=3: Do not apply plain launchd log rotation now.
- L4=1: Summarize cleanup dry-run results in `docs/WORKLOG.md` if work is performed.

Implication: `scripts/rotate_logs.py` may be run without `--apply`; do not truncate/archive/delete logs.

## P — Runtime package update

- P1=2: Address package update by writing an update plan/rollback, not by applying it now.
- P2=2: The package-update scope may include `faster-whisper`, but model switch remains forbidden.
- P3=3: Treat `faster-whisper==1.2.1` as a production `.venv` upgrade candidate to be evaluated in the plan.
- P4=1: If an update is later approved, verification must include targeted tests, compileall, and the next real lecture canary.

Implication: read live package state and draft rollback/verification steps only. Do not mutate `.venv`, `requirements.txt`, lockfiles, model cache, or production config without a new approval.

## B — Benchmark expansion / model experiment

- B1=1: Do not resume benchmark expansion now.
- B2=1: If benchmarks are later approved, raw benchmark artifacts may stay local under `state/benchmarks/`.
- B3=1: Docs should contain metric summaries only; no raw transcript text.
- B4=1: Long sample `260415LA` runs only for final candidates.

Implication: no new model benchmark, no model download, no candidate model run in this batch.

## O — Documentation and work management

- O1=1: Reflect decisions in this plan document.
- O2=1: Manage remaining work as separate workstreams: canary, downstream, migration, package, logs/cleanup, benchmark/docs.
- O3=1: Before local commit, show diff summary and ask for approval. Push only on explicit request.

## Updated execution order

1. Workstream 0: safety/read-only setup, git status/diff/check, relevant docs.
2. Workstream O: record current decisions in this plan and `docs/WORKLOG.md`.
3. Workstream D: run downstream read-only diagnose/manual-table work; save to `state/reports/` only if useful, and never mutate DB/files.
4. Workstream L: run log cleanup/rotation dry-runs only; no `--apply`.
5. Workstream P: inspect package state and write package update plan/rollback for possible `faster-whisper==1.2.1` production candidate; no `.venv` mutation.
6. Workstream C: wait for next real lecture canary under launchd, current notification config, downstream on.
7. Workstream R: after canary, revisit runtime migration plan/rollback and request explicit approval before any apply/restart.
8. Workstream B: keep benchmark expansion paused unless a new explicit benchmark request appears.

## Safe commands for the immediate read-only/dry-run batch

These commands are safe in intent and should not perform destructive/system/external side effects:

```bash
cd /Users/geonha/DEV/lecture_stt

git status --short --branch
git diff --stat
git diff --check

sqlite3 state/jobs.sqlite3 'PRAGMA integrity_check;'
sqlite3 state/jobs.sqlite3 'PRAGMA foreign_key_check;'

PYTHONPATH=src .venv/bin/python -m lecture_stt.downstream.status diagnose --limit 60
.venv/bin/python scripts/rotate_logs.py
.venv/bin/python scripts/benchmark_models.py --config config/config.yaml --print-plan
.venv/bin/python -m compileall -q scripts src tests
```

If a command wants to write an artifact, prefer `state/reports/` or `state/benchmarks/` only when the user decision explicitly allows that artifact type, and report the path in the final summary.
