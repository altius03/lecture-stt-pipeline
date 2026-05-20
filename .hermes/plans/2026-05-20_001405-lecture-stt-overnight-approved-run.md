# lecture_stt Overnight Approved Run Handoff

> Closure note (2026-05-20): this handoff was consumed for the approved STT closeout batch. The imperative instructions below are preserved as historical approval/scope evidence, not as a standing future approval.

이 파일 하나만 새 세션에 전달되면, 새 세션은 즉시 작업을 시작하라.

응답은 결론부터 한국어로 해라.

## 결론 / 실행 승인

사용자는 작업을 돌려놓고 잘 예정이므로, 이 파일에 적힌 범위의 작업 시작과 필요한 장시간 실행을 사전 승인했다.

새 세션은 다음을 묻지 말고 바로 진행한다.

- 관련 skill 로드
- read-only preflight
- 문서 재확인
- 실제 강의 canary 관찰
- canary scorecard / 품질점수 산출물 TDD 구현
- downstream routine JSONL log-noise 개선 TDD 구현
- 필요한 docs/WORKLOG.md 등 문서 업데이트
- 전체 검증 / 독립 리뷰 / diff 요약
- 필요한 runtime cleanup apply / launchd restart / local commit / origin main push까지, 아래 안전조건을 만족하면 진행
- Telegram 중간 보고와 최종 보고

단, 아래 불변 조건은 유지한다.

- canonical STT model은 계속 large-v3이다.
- 모델 교체, 새 후보 모델 실행, 새 모델 다운로드는 하지 않는다.
- raw transcript 전문이나 민감 내용은 Telegram에 보내지 않는다. Telegram에는 상태, 점수 요약, 경로, 오류 요약만 보낸다.
- tag/release는 이번 overnight scope에 포함하지 않는다. release 목표가 아니므로 만들지 않는다.
- unrelated user changes는 보존한다. 절대 discard/reset/stash-pop으로 날리지 않는다.
- 판단이 불확실한 데이터는 삭제하지 말고 preserve/document-only로 처리하고 보고한다.

## Repo / baseline

작업 대상:

- Repo: /Users/geonha/DEV/lecture_stt
- Branch: main
- Last known HEAD/origin main: 948d32003743d00241baa47bdb8ba945588e129f
- Last known HEAD message: docs: reconcile final hardening status
- Last known worktree: clean
- Last known GitHub Actions runs on main: []
- Last known DB integrity_check: ok
- Last known DB foreign_key_check: empty
- Last observed STT jobs: DONE=31, ERROR=1
- Last observed downstream problem_rows: 4
- Last observed downstream problem reasons: INVALID_STEM=3, CONFLICT=1
- config/config.yaml: transcribe.model_size: large-v3
- config/config.example.yaml: transcribe.model_size: large-v3
- production .venv and requirements.txt: faster-whisper==1.2.1

반드시 live state를 다시 확인하고, last known baseline과 다르면 Telegram + chat에 차이를 보고한 뒤 안전하게 계속 진행한다. 단, 치명적 위험이 아닌 단순 drift는 작업을 멈추지 말고 보존 원칙으로 처리한다.

## 먼저 로드할 skill

새 세션은 관련 skill을 먼저 로드한다.

권장:

- systematic-debugging
- test-driven-development
- writing-plans
- writing-plans reference: operational-overnight-approval
- test-driven-development references if needed:
  - operational-daemon-safety-tdd
  - production-repo-hardening-tdd

## 먼저 읽을 문서

반드시 아래 문서를 읽고 source of truth를 정리한다.

- /Users/geonha/DEV/lecture_stt/.hermes/plans/2026-05-19_160615-lecture-stt-remaining-hardening.md
- /Users/geonha/DEV/lecture_stt/docs/WORKLOG.md
- /Users/geonha/DEV/lecture_stt/docs/OPERATIONS.md
- /Users/geonha/DEV/lecture_stt/docs/REMAINING_HARDENING_DECISION_GUIDE_2026-05-19.md
- /Users/geonha/DEV/lecture_stt/docs/RUNTIME_MIGRATION_PLAN_2026-05-19.md
- /Users/geonha/DEV/lecture_stt/docs/MODELS.md

Source of truth:

- docs/WORKLOG.md와 .hermes plan이 최신 source of truth다.
- OPERATIONS/MODELS/RUNTIME_MIGRATION_PLAN은 commit 948d320에서 reconcile 되었지만, live state와 비교 후 신뢰한다.

## Read-only preflight: 즉시 실행

다음은 즉시 실행한다. 모두 read-only이다.

```bash
cd /Users/geonha/DEV/lecture_stt
git status --short --branch
git log -1 --oneline --decorate
git diff --stat
git diff --check
git ls-remote --heads origin main
gh run list --branch main --limit 5 --json databaseId,status,conclusion,workflowName,headSha,createdAt 2>/dev/null || true
sqlite3 state/jobs.sqlite3 'PRAGMA integrity_check;'
sqlite3 state/jobs.sqlite3 'PRAGMA foreign_key_check;'
sqlite3 -header -column state/jobs.sqlite3 "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status ORDER BY status;"
PYTHONPATH=src .venv/bin/python -m lecture_stt.downstream.status summary --limit 20
```

추가 read-only 확인:

```bash
launchctl print gui/$(id -u)/com.geonha.lecture-stt 2>/dev/null || true
launchctl print gui/$(id -u)/com.geonha.lecture-stt-distribute 2>/dev/null || true
launchctl print gui/$(id -u)/com.geonha.lecture-stt-webpanel 2>/dev/null || true
launchctl print gui/$(id -u)/com.geonha.lecture-stt-cleanup 2>/dev/null || true
```

검사 경로:

- iCloud inbox: ~/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/00_inbox
- audio output: ~/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/01_audio
- transcript output: ~/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/02_transcripts
- logs: ~/Library/Logs/lecture_stt
- DB: /Users/geonha/DEV/lecture_stt/state/jobs.sqlite3

## Telegram 보고 규칙

사용자는 Telegram 중간 보고를 명시적으로 승인했다.

Telegram 보고는 `send_message(target="telegram", ...)`를 사용한다.

반드시 보내는 시점:

1. 작업 시작 직후
   - repo, HEAD, 목표, “overnight approved run 시작”
2. read-only preflight 완료 후
   - branch/HEAD/worktree/DB/downstream 요약
3. 실제 canary 파일 감지 시
   - stem/path/관찰 시작 시각
4. canary DONE 또는 ERROR 시
   - job id/status/output 경로/문제 여부
5. scorecard TDD 구현 완료 후
   - 테스트 결과/새 산출물 경로/점수 요약
6. log-noise TDD 구현 완료 후
   - 테스트 결과/변경 요약
7. cleanup apply 또는 launchd restart 전후
   - 전: 대상/rollback 요약
   - 후: 성공/실패/검증 결과
8. 장시간 대기 중 30~60분 이상 milestone이 없을 때
   - 짧은 heartbeat. 예: “대기 중, 새 canary 미감지, DB 문제 증가 없음”
9. 최종 완료 시
   - pass/fail, commit/push 여부, 남은 리스크, evidence paths

Telegram에는 raw transcript 전문을 넣지 않는다.

## 승인된 작업 범위

아래 작업은 이번 overnight run에서 모두 승인되었다. 새 세션은 별도 확인 질문 없이 수행한다.

### 1. Next real lecture canary / stability gate

승인됨.

목표:

- 다음 실제 강의 파일을 canary input으로 감지한다.
- launchd-backed processing을 관찰한다.
- 파일을 임의로 move/copy/delete/rename하지 않는다. 기존 launchd 흐름을 관찰한다.
- DB/logs/output을 poll한다.
- DONE이면 operational canary pass로 기록한다.
- ERROR이면 evidence를 보존하고 실패로 기록한다.

판정:

- C1=1: only next real lecture file counts as canary input.
- C2=1: one real lecture DONE is immediate operational pass.
- 24h idle 또는 다음 2 lectures는 별도 stability gate다. 가능하면 함께 관찰하되, 첫 DONE만으로 immediate pass를 기록한다.
- downstream worker는 켜진 상태 유지.
- notification config는 특별한 이유 없으면 변경하지 않는다.

확인할 evidence:

- `01_audio/<stem>.<source_ext or m4a>`
- `02_transcripts/<stem>.txt`
- `02_transcripts/<stem>.json`
- jobs row status DONE/ERROR
- downstream problem_rows가 known 4에서 예상 밖으로 증가하지 않았는지
- `~/Library/Logs/lecture_stt`에 새 ERROR/log spam이 없는지
- notification behavior가 logs/config상 정상인지

### 2. Canary scorecard / 품질점수 산출물

승인됨. strict TDD로 구현한다.

원칙:

- production code before failing test 금지.
- 먼저 failing test를 추가하고 실제 실패를 확인한다.
- 그 다음 최소 구현으로 pass시킨다.
- side-effect-safe test를 우선한다.
- runtime DB나 iCloud 실제 파일을 직접 변조하는 테스트는 피한다.
- 필요하면 fixture/tempdir를 사용한다.

요구 기능 방향:

- canary 결과를 사람이 보기 쉬운 scorecard로 산출한다.
- operational checks와 transcript quality checks를 분리한다.
- operational pass 기준:
  - job DONE
  - expected audio/transcript/json output 존재
  - downstream problem_rows unexpected increase 없음
  - 새 log error/spam 없음
- quality score는 pass/warn/fail 또는 점수+reason으로 표현한다.
- transcript 내용 검토가 필요하면 로컬 repo/test fixture 또는 실제 canary transcript를 읽어도 된다. 단 Telegram에는 원문을 보내지 않는다.
- 산출물 경로는 `state/reports/` 아래 timestamped JSON/MD/CSV 중 적절히 둔다.
- CLI entrypoint 또는 기존 status/report 계열과 자연스럽게 통합한다.

검증:

- targeted tests
- broader unittest suite
- compileall
- git diff --check

### 3. Downstream routine JSONL log-noise 개선

승인됨. strict TDD로 구현한다.

목표:

- 정상/routine JSONL 로그가 불필요하게 noisy하지 않도록 개선한다.
- 문제 상황은 숨기지 않는다.
- downstream worker 동작 의미는 유지한다.
- launchd restart는 필요할 때만 수행하되, 이번 overnight run에서는 필요 시 승인된 것으로 본다.

원칙:

- failing test 먼저.
- 로그 출력/레벨/중복 억제 등 동작을 테스트로 고정.
- 실제 LaunchAgent 수정 전후에는 plist/status/log path를 확인한다.
- restart가 필요하면 named service만 restart한다:
  - com.geonha.lecture-stt
  - com.geonha.lecture-stt-distribute
  - com.geonha.lecture-stt-webpanel
  - com.geonha.lecture-stt-cleanup
- restart 전 Telegram 보고, restart 후 launchctl/log/DB로 확인.
- restart 실패 시 rollback 또는 이전 상태 보고.

### 4. docs / WORKLOG / operations 문서 업데이트

승인됨.

업데이트 대상 예시:

- docs/WORKLOG.md
- docs/OPERATIONS.md
- docs/MODELS.md
- docs/RUNTIME_MIGRATION_PLAN_2026-05-19.md
- .hermes/plans/2026-05-19_160615-lecture-stt-remaining-hardening.md

원칙:

- live-verified 내용만 완료로 기록한다.
- planned/approved/remaining을 구분한다.
- canary 결과, scorecard 결과, log-noise 개선, cleanup 결과, launchd restart 여부, commit/push 여부를 기록한다.
- 문서만으로 stale 상태가 생기지 않게 WORKLOG와 plan을 우선 reconcile한다.

### 5. Downstream problem row 4 cleanup

승인됨. 단, guessing으로 데이터 손상하지 않는다.

Known remaining:

- INVALID_STEM 3:
  - NFD-looking Korean stem 1
  - BOSS_SPECIAL_LECTURE
  - 2603034LA_2
- CONFLICT 1:
  - 260422LC

정책:

- 260422LC는 document-only/preserved 기본값을 유지한다.
- INVALID_STEM 3은 deterministic 1:1 repair가 가능한 경우에만 backup/manifest 후 처리한다.
- deterministic하지 않으면 preserve/document-only로 남기고 report한다.
- DB update/delete/clear가 필요하면 사전 backup을 만들고 manifest를 남긴다.
- 파일 rename/move/delete가 필요하면 원본 보존 backup 또는 reversible archive를 먼저 만든다.
- 문제 row를 0으로 만들기 위해 stem을 추측하지 않는다.

승인된 산출물:

- state/backups/<timestamped-backup>
- state/reports/<timestamped-downstream-cleanup-report>.json
- state/reports/<timestamped-downstream-cleanup-report>.csv 또는 .md

### 6. Runtime/log/legacy cleanup

승인됨. 단, dry-run/list -> manifest -> backup/rollback -> apply 순서를 지킨다.

검토 대상:

- /Users/geonha/lecture_stt legacy root
- repo-local stale tmp files
- repo/state/runtime logs and old archive
- iCloud 01_audio / 02_transcripts bulk cleanup candidates
- scripts/rotate_logs.py candidates

적용 원칙:

- 삭제보다 archive/preseve 우선.
- raw lecture source/audio/transcript는 확실한 duplicate/stale이 아니면 삭제하지 않는다.
- cleanup target list를 report로 남긴다.
- backup/rollback path를 남긴다.
- apply 후 동일한 검증을 다시 실행한다.
- `scripts/rotate_logs.py --apply`는 이번 overnight run에서 승인된 것으로 본다. 단, 실행 전 dry-run 결과와 rollback 가능성을 report하고 Telegram에 짧게 알린 뒤 진행한다.

### 7. Verification / independent review

승인됨. 오래 걸려도 수행한다.

최소 검증:

```bash
cd /Users/geonha/DEV/lecture_stt
git diff --check
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
PYTHONPATH=src .venv/bin/python -m compileall -q src tests scripts
```

가능하면 추가:

- targeted tests for scorecard
- targeted tests for log-noise
- downstream status summary
- DB integrity_check / foreign_key_check 재실행
- launchctl status 재확인
- latest logs error scan
- static added-line scan, if existing project command/report pattern exists
- GitHub Actions 확인, push 후에는 run status 확인

독립 리뷰:

- 가능하면 delegate_task로 diff/code review를 수행한다.
- subagent에게는 “external side effects 금지, read-only diff review만” 지시한다.
- subagent 결과는 self-report이므로 주요 지적은 직접 확인한다.

### 8. Git commit / push

이번 overnight run에서는 local commit과 origin main push가 승인되었다.

조건:

- worktree에 unrelated user changes가 있으면 보존한다.
- 변경 파일이 이번 scope와 관련 있는지 확인한다.
- full verification이 통과해야 commit한다.
- commit 전 `git status --short`, `git diff --stat`, 주요 diff 요약을 chat과 Telegram에 남긴다.
- commit message는 명확하게 쓴다.
- commit 후 origin/main에 push한다.
- push 후 `git status --short --branch`, `git log -1 --oneline --decorate`, `git ls-remote --heads origin main`, 가능하면 `gh run list`로 확인한다.
- push 후 GitHub Actions가 생기면 완료/실패까지 가능한 범위에서 관찰하고 Telegram 보고한다.

금지:

- tag/release 생성은 하지 않는다.
- force push 금지.
- history rewrite 금지.

## Known completed work

- main branch fast-forward merge/push completed before this handoff.
- Telegram final report completed in prior session.
- Runtime migration applied.
- production .venv and requirements.txt aligned to faster-whisper==1.2.1.
- VAD compatibility fix completed.
- copied canary:
  - 260519OOP_1: job 201 ERROR, failure evidence preserved.
  - 260519OOP_2: job 202 DONE.
- downstream safe repair applied:
  - backup: state/backups/downstream-repair-20260519T123211Z
  - manifest: state/reports/downstream-repair-apply-20260519T123211Z.json
- job 201 / 99_errors failure evidence preserved:
  - report: state/reports/stt-error-cleanup-20260519T123547Z.json
- runtime cleanup dry-run/list only:
  - report dir: state/reports/runtime-cleanup-20260519T123847Z
- final verification:
  - report dir: state/reports/final-verification-20260519T124121Z
  - unittest 108 OK
  - compileall OK
  - git diff --check OK
  - static added-line scan 0 findings
- downstream manual table report:
  - state/reports/downstream-manual-table-20260519T131110Z.json
  - state/reports/downstream-manual-table-20260519T131110Z.csv

## Suggested execution order

새 세션은 아래 순서로 진행한다.

1. Telegram: overnight run 시작 보고.
2. Load skills.
3. Run read-only preflight.
4. Read source-of-truth docs.
5. Report live baseline in chat + Telegram.
6. Observe iCloud inbox / launchd-backed canary.
7. If real lecture canary appears, poll DB/logs until DONE/ERROR or long timeout.
8. Build canary scorecard with strict TDD.
9. Build downstream routine JSONL log-noise improvement with strict TDD.
10. Run targeted tests.
11. If code/config changes require runtime reload, Telegram pre-report -> named launchd restart -> verify.
12. Apply approved deterministic downstream cleanup if safe.
13. Apply approved runtime/log/legacy cleanup if dry-run+backup+rollback are ready.
14. Update docs/WORKLOG and related docs.
15. Run full verification.
16. Independent review.
17. Commit if verification passes.
18. Push origin main if commit succeeds.
19. Observe GitHub Actions if any.
20. Telegram final report + chat final report.

## Failure handling

Do not wake the user for normal recoverable issues. Use conservative defaults and continue where safe.

Stop and report only if:

- DB integrity_check fails.
- foreign_key_check returns rows after changes.
- launchd restart fails and rollback also fails.
- tests fail and cannot be fixed without broad unrelated rewrite.
- unexpected data-loss risk appears.
- model switch/download seems necessary. In that case, do not do it.
- credentials/auth failures prevent push or GitHub status check. In that case, leave local commit and report.

If a step fails:

- preserve evidence under state/reports or state/backups as appropriate.
- Telegram concise failure report.
- continue independent safe tasks if possible.
- final report must include what succeeded, what failed, what was not attempted, and paths.

## Final response shape

Final response must be Korean, conclusion first.

Include:

- overall pass/fail
- canary result and evidence paths
- scorecard result and artifact paths
- log-noise result
- cleanup result
- DB/downstream counts before/after
- tests/verification results
- commit SHA and push status if committed/pushed
- GitHub Actions status if any
- remaining risks / deferred items
