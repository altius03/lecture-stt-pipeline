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

# User Decision Snapshot

## Resolved or defaulted decisions

D1 = B. Source `03_correction` is canonical for downstream conflicts. Destination replacement still requires backup/dry-run; no blind overwrite.

D2 = recommended conservative default. For DB-only stale rows, first produce a dry-run table. Actual row clear is allowed only for rows that are proven DB-only/stale with no recoverable source action, after DB backup/snapshot and an explicit before/after row list. `260422LC` is excluded by D3 and should not be cleared automatically.

D3 = Leave `260422LC` untouched and document only.

D4 = Invalid stem files require per-file confirmation before rename.

D5 = accepted default. Infer a proposed subject route from filename stem rules, but present ambiguous route/rename rows as a manual table before changing paths. Do not use existing destination folder as the sole source of truth when it conflicts with source naming.

F1 = 2 retries.

F2 = retry on the next scan immediately after currently running work finishes; no timed backoff for now.

F3 = after terminal failure, move the original audio to `99_errors`.

F4 = terminal failed jobs require manual reset before any further retry.

L1 = retention uses both size and date.

L2 = accepted defaults: app logs 10MB x 5, downstream JSONL 10MB x 5, launchd stdout/stderr 10MB x 3, compressed archive retention 30 days.

L3 = routine scan stdout remains disabled by default.

L4 = old log archive/cleanup must ask again at the time; no standing approval.

R1 = move logs/tmp/cache outside repo; keep DB in repo.

R2 = use macOS standard paths by default: logs under `~/Library/Logs/lecture_stt`, cache/tmp under `~/Library/Caches/lecture_stt`; DB remains in repo `state/` unless separately approved.

R3 = legacy `/Users/geonha/lecture_stt` should be inspected again and decided later.

R4 = keep empty repo runtime folders as placeholders.

R5 = stale `tmp/*.wav` should be archived, then deleted.

R6 = launchd restart during migration/canary requires showing the plan first.

M1 = keep `large-v3`; skip model switching for now.

M2 = no more isolated `faster-whisper==1.2.1` benchmark samples for now.

M3 = no MLX/Apple Silicon spike now.

M4 = production `.venv` package upgrade may proceed if checks show no separate issue, but only as a package/runtime update, not as a model switch, and with rollback verification.

B1 = do not expand benchmarks now; revisit later.

B2 = raw benchmark artifacts may remain locally under `state/benchmarks/`.

B3 = docs should include metric summaries only, not raw transcript text.

B4 = long sample `260415LA` only for final candidates.

C1 = canary input should be the next real lecture only.

C2 = accepted default. Use the launchd-running worker for the real canary; reserve `scripts/run_once.sh` for controlled debugging/preflight if launchd canary fails or if the worker must be paused.

C3 = keep current notification config.

C4 = keep downstream worker on during canary.

C5 = accepted default. Define immediate canary pass as 1 real lecture job completed end-to-end without errors/log spam; define operational stability separately as 24h idle observation or next 2 real lectures.

O1 = create `docs/OPERATIONS.md` as the main operator guide.

O2 = keep existing handoff checklist as historical record; update `OPERATIONS`/`WORKLOG` as current truth.

O3 = docs should include both concise command checklist and detailed runbook sections.

---

# Decisions Needed From User Before Implementation

## D. Downstream conflict/canonical decisions

D1. For existing destination conflicts, which side is canonical by default?
- A: destination/GH_archive wins; source in `03_correction` is stale
- B: source in `03_correction` wins; destination should be replaced only after backup
- C: no default; produce per-file table for manual decision

D2. Are DB-only stale rows allowed to be cleared after dry-run confirmation?
- yes / no / ask per row

D3. For `260422LC`, preferred handling?
- clear stale DB row if source is still missing
- recreate source from destination if possible
- leave untouched and document only

D4. For invalid stem files, should we rename to standard stem format if mapping is obvious?
- yes, after dry-run table
- no, document only
- ask per file

D5. For subject route/rename issues, what is the authoritative subject mapping source?
- filename stem rules
- existing destination folder
- manual table from user
- other

## F. Retry/failure policy decisions

F1. Retry count: keep 2 retries?
- yes / choose another number

F2. Retry delay/backoff:
- immediate next scan
- fixed delay, e.g. 10 minutes
- exponential backoff

F3. Final failure handling for original audio:
- move original to `99_errors`
- keep original in `01_audio`, write diagnostic marker/copy to `99_errors`
- copy original to `99_errors`, keep original in `01_audio`

F4. Should terminal failed jobs require manual reset before retrying again?
- yes / no

## L. Log policy decisions

L1. Retention policy:
- size-based only
- date-based only
- both size and date

L2. Recommended default acceptable?
- app logs: 10MB x 5
- downstream JSONL: 10MB x 5
- launchd stdout/stderr: 10MB x 3
- archive compressed logs for 30 days

L3. Should verbose routine scan logs stay disabled by default?
- yes / no

L4. Is one-time cleanup/archive of current old logs allowed in the next session if dry-run looks safe?
- yes / no / ask again then

## R. Runtime structure decisions

R1. Should runtime state remain inside repo for now, or move to macOS standard paths?
- keep in repo for now
- move logs only
- move logs/tmp/cache, keep DB in repo
- move all runtime state outside repo

R2. If moving outside repo, preferred base paths?
- `~/Library/Application Support/lecture_stt`
- `~/Library/Logs/lecture_stt`
- `~/Library/Caches/lecture_stt`
- custom paths

R3. What to do with legacy `/Users/geonha/lecture_stt`?
- archive then delete
- keep as-is
- inspect again and ask later

R4. What to do with empty repo folders `audio/`, `errors/`, `inbox/`, `transcripts/`, `models/`?
- delete if unused
- keep placeholders
- document only

R5. What to do with stale `tmp/*.wav`?
- delete after dry-run
- archive then delete
- leave untouched

R6. Is launchd restart allowed during runtime migration/canary?
- yes, if plan shown first
- only in a specified time window
- no, manual instructions only

## M/B. Model and benchmark decisions

M1. Given current evidence, should `large-v3` remain fixed and skip more model work for now?
- yes, skip model work
- no, run more isolated benchmarks

M2. Should `faster-whisper==1.2.1` get more isolated samples?
- no
- short+medium only
- full representative set

M3. Should MLX/Apple Silicon spike be included now?
- no
- yes, separate spike only

M4. Production `.venv` upgrade remains forbidden until explicit later approval, correct?
- yes / no

B1. If expanding benchmark, which samples are approved?
- use documented 8 candidates
- only short+medium
- user supplies list

B2. Are raw transcript benchmark artifacts allowed to remain under local `state/benchmarks/`?
- yes / no

B3. Should benchmark docs include only metrics summaries, not raw text?
- yes recommended / no

B4. Is long sample `260415LA` allowed only for final candidates?
- yes / no

## C. Canary/live operation decisions

C1. Canary input source:
- next real lecture only
- copied existing sample with test stem
- no canary now

C2. Use launchd-running worker or `run_once` for canary?
- launchd
- `scripts/run_once.sh`
- both in sequence

C3. Should notifications be enabled during canary?
- current config
- disable
- Telegram only
- Discord only

C4. Should downstream worker stay on during canary?
- yes
- pause/disable while testing STT
- only if manual correction output is present

C5. How long should observation run before considering canary passed?
- one completed job
- 24 hours
- next 2 real lectures

## O. Documentation decisions

O1. Create `docs/OPERATIONS.md` as the main operator guide?
- yes / no, keep README only

O2. Should historical handoff checklist be updated to reflect completed items, or left as historical record?
- update it
- leave it and point to OPERATIONS/WORKLOG

O3. Preferred doc style:
- concise command checklist
- detailed runbook
- both: quickstart plus detailed sections

---

## Suggested User Reply Format for New Session

Paste this at the start of the new session after decisions are made:

```text
/Users/geonha/DEV/lecture_stt 고도화 남은 8개 작업을 진행한다.
먼저 skill 로드, git status/diff 확인, 기존 변경 보존.
계획 파일: .hermes/plans/2026-05-19_160615-lecture-stt-remaining-hardening.md

결정사항:
D1=..., D2=..., D3=..., D4=..., D5=...
F1=..., F2=..., F3=..., F4=...
L1=..., L2=..., L3=..., L4=...
R1=..., R2=..., R3=..., R4=..., R5=..., R6=...
M1=..., M2=..., M3=..., M4=...
B1=..., B2=..., B3=..., B4=...
C1=..., C2=..., C3=..., C4=..., C5=...
O1=..., O2=..., O3=...

제약:
- 모델 교체 금지 unless explicitly approved
- production .venv upgrade 금지 unless explicitly approved
- destructive file/DB migration은 dry-run 보고 후 재승인
- push/tag/release 금지 unless explicitly requested
```
