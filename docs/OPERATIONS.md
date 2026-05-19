# Lecture STT Operations Runbook

이 문서는 `lecture_stt`의 현재 운영 기준 문서다. 오래된 handoff/checklist 문서는 이력으로 보존하고, 운영 중 실제로 따라야 할 절차는 이 파일과 `docs/WORKLOG.md`를 우선한다.

## 0. 현재 결론

- 운영 repo: `/Users/geonha/DEV/lecture_stt`
- 운영 모델: `large-v3` 유지
- 현재 모델 교체 금지: `large-v3-turbo`, distil, Korean turbo 계열은 canonical STT 기본값으로 쓰지 않는다.
- production `.venv` 업그레이드: 조건부 허용. 단, 모델 교체와 분리된 runtime package update로 다루고, 계획/rollback/검증 후 진행한다.
- remote push, tag, release: 별도 명시 요청 없이는 하지 않는다.
- destructive DB/file migration: dry-run 보고와 명시 승인 없이는 하지 않는다.
- 기존 미커밋 변경: 항상 `git status`/`git diff`로 확인하고 보존한다.

## 1. 빠른 운영 체크리스트

```bash
cd /Users/geonha/DEV/lecture_stt

git status --short --branch
git diff --stat
git diff --check

uid=$(id -u)
launchctl print "gui/$uid/com.geonha.lecture-stt" 2>/dev/null | sed -n '1,80p'
launchctl print "gui/$uid/com.geonha.lecture-stt-distribute" 2>/dev/null | sed -n '1,80p'

PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python -m compileall -q scripts src tests
```

DB 무결성 확인:

```bash
sqlite3 state/jobs.sqlite3 'PRAGMA integrity_check;'
sqlite3 state/jobs.sqlite3 'PRAGMA foreign_key_check;'
```

Downstream 상태 확인:

```bash
bash scripts/distribute_status.sh
bash scripts/distribute_status.sh list --only-problems
```

## 2. Launchd 서비스

현재 repo template 기준 label:

- `com.geonha.lecture-stt`: 메인 STT worker
- `com.geonha.lecture-stt-distribute`: manual correction/summary downstream worker
- `com.geonha.lecture-stt-cleanup`: cleanup worker
- `com.geonha.lecture-stt-webpanel`: web panel

설치/재등록:

```bash
bash scripts/setup_launchd.sh
```

개별 재시작은 운영 영향이 있으므로 먼저 계획을 보고한 뒤 진행한다.

```bash
uid=$(id -u)
launchctl kickstart -k "gui/$uid/com.geonha.lecture-stt"
launchctl kickstart -k "gui/$uid/com.geonha.lecture-stt-distribute"
```

## 3. STT 입력/출력 흐름

기본 흐름:

1. 새 오디오가 iCloud `lecture_recordings/00_inbox`에 들어온다.
2. 안정 시간 이후 worker가 처리 대상으로 인식한다.
3. 원본은 `01_audio`로 이동/보관된다.
4. raw transcript는 `02_transcripts`에 생성된다.
5. 사용자가 검토한 correction은 `03_correction`에 둔다.
6. summary는 `04_summarize`에 둔다.
7. downstream worker가 GH archive/Obsidian 목적지로 배포한다.

현재 구현된 retry/failure 정책:

- retry 횟수: 기본 2회 (`app.transcribe_max_retries`).
- retry 시점: STT 실행 실패 job은 `PENDING` + `전사 재시도 대기 n/2` 상태로 남기고, 현재 진행 중인 작업이 끝난 뒤 다음 scan에서 즉시 재시도한다. 별도 timed backoff는 없다.
- retryable job은 canonical audio를 `01_audio`에 유지하며, startup recovery도 이를 inbox로 되돌리거나 job row를 삭제하지 않는다.
- terminal failed job: retry 한도 초과 후 `ERROR`로 확정되며, 수동 reset 전까지 추가 자동 재시도하지 않는다.
- 최종 실패한 원본 오디오: `99_errors`로 이동한다.
- failure report/log/DB에는 안정적인 실패 사유와 경로를 남기되 secret-like 문자열은 `[REDACTED]`로 마스킹한다.

## 4. Downstream conflict 처리 기준

확정 기준:

- conflict가 있을 때 canonical source는 `03_correction`이다.
- destination에 다른 내용이 있으면 blind overwrite하지 않는다.
- destination을 바꿔야 하면 dry-run 표, backup, before/after hash를 먼저 확인한다.
- `260422LC`는 현재 그대로 두고 문서화만 한다.
- invalid stem rename은 파일별 확인 후 진행한다.

### D5: subject route/rename 기준 설명

`subject route/rename`은 파일을 어느 과목 폴더로 보낼지 결정하는 문제다. 예를 들어 stem이 `260504DS_1`이면 `DS`를 보고 데이터사이언스 폴더로 보낼지, 이미 destination에 있는 폴더 위치를 믿을지, 사용자가 별도로 매핑을 줄지 정해야 한다.

선택지 의미:

- filename stem rules: `LC`, `DStr`, `DS`, `LA`, `OOP`, `Unix` 같은 stem의 과목 코드를 기준으로 route를 정한다.
  - 장점: 재현 가능하고 자동화하기 좋다.
  - 단점: 파일명 자체가 잘못됐으면 잘못된 폴더로 보낼 수 있다.
- existing destination folder: 이미 배포된 destination 폴더를 기준으로 삼는다.
  - 장점: 과거 수동 정리를 보존할 수 있다.
  - 단점: 기존 오배포가 있으면 오배포를 정답으로 고착할 수 있다.
- manual table: ambiguous row마다 표를 만들고 사용자가 결정한다.
  - 장점: 가장 안전하다.
  - 단점: 자동화가 덜 된다.

추천 기본값:

- obvious한 과목 코드는 filename stem rule로 proposed route를 만든다.
- route/rename이 애매한 7개 row는 manual table로 확인받는다.
- source naming과 destination folder가 충돌하면 destination folder만 믿지 않는다.

현재 구현된 read-only 진단:

```bash
PYTHONPATH=src .venv/bin/python -m lecture_stt.downstream.status diagnose --limit 60
```

machine-readable report가 필요하면 명시 경로를 지정한다. `state/reports/`는 gitignore 대상이므로 운영 snapshot 저장에 적합하다. 이 명령은 DB row와 source/destination 파일을 변경하지 않는다.

```bash
PYTHONPATH=src .venv/bin/python -m lecture_stt.downstream.status diagnose \
  --limit 60 \
  --json \
  --report-path state/reports/downstream-diagnose-YYYYMMDD.json
```

DB-only/source-missing stale row clear는 별도 안전 명령만 사용한다. `260422LC`는 코드상 clear가 거부되며 문서화만 한다. 실제 운영 DB에 적용하려면 먼저 dry-run 출력의 before row, before/after count, backup path를 확인하고 별도 승인을 받아야 한다.

```bash
# dry-run only; DB/file mutation 없음
PYTHONPATH=src .venv/bin/python -m lecture_stt.downstream.status clear-stale STEM \
  --dry-run \
  --backup-path state/backups/jobs.before-clear-STEM.sqlite3

# 승인 후에만 실행; DB backup 생성 후 source-missing row 1개만 삭제
PYTHONPATH=src .venv/bin/python -m lecture_stt.downstream.status clear-stale STEM \
  --yes \
  --backup-path state/backups/jobs.before-clear-STEM.sqlite3
```

## 5. Log policy

확정 정책:

- retention 기준: size + date 둘 다 사용
- app logs: 10MB x 5
- downstream JSONL: 10MB x 5
- launchd stdout/stderr: 10MB x 3
- compressed archive 보존: 30일
- routine scan stdout 로그: 기본 비활성화
- old log archive/cleanup: 그 시점에 다시 확인 후 진행

이미 적용/준비된 운영값:

- STT app log 기본값: `logging.max_bytes: 10485760`, `logging.backup_count: 5`
- downstream JSONL 기본값: `downstream.log_jsonl_max_bytes: 10485760`, `downstream.log_jsonl_backup_count: 5`
- downstream 반복 문제 suppression: `downstream.log_suppression_max_keys: 4096`
- downstream routine scan stdout: `downstream.log_routine_scan_events: false`
- launchd stdout/stderr plain log rotation helper: `scripts/rotate_logs.py` (`--apply` 없이는 dry-run)

Launchd stdout/stderr rotation dry-run:

```bash
.venv/bin/python scripts/rotate_logs.py
```

적용은 old log cleanup/archive와 같은 운영 side effect로 취급한다. 실제 적용 전 출력된 대상/크기/backup plan을 확인하고 승인받은 뒤 실행한다.

```bash
.venv/bin/python scripts/rotate_logs.py --apply
```

## 6. Runtime/state/tmp/cache 구조

확정 방향:

- DB는 repo 내부 `state/jobs.sqlite3` 유지
- logs/tmp/cache는 repo 밖 macOS 표준 경로로 이동하는 방향
- 권장 target:
  - STT app log: `~/Library/Logs/lecture_stt/app.log`
  - downstream JSONL: `~/Library/Logs/lecture_stt/downstream.jsonl`
  - launchd stdout/stderr: `~/Library/Logs/lecture_stt/*.out.log`, `*.err.log`로 전환하는 방향. 단, installed plist 교체/restart는 별도 승인 후 진행
  - cache/tmp: `~/Library/Caches/lecture_stt/tmp`
  - DB: `/Users/geonha/DEV/lecture_stt/state/jobs.sqlite3`
- legacy `/Users/geonha/lecture_stt`는 다시 inspect 후 결정
- repo 안 empty runtime folders는 placeholder로 유지
- stale `tmp/*.wav`는 archive 후 삭제하되, cleanup 시점에 다시 확인
- migration/canary 중 launchd restart는 계획 보고 후 허용

현재 문서화된 migration/rollback 계획:

- `docs/RUNTIME_MIGRATION_PLAN_2026-05-19.md`

현재 코드/config default는 새 log/tmp 기본값을 사용할 수 있게 준비되어 있지만, 운영 `config/config.yaml`, installed LaunchAgents, 기존 repo log/tmp 파일은 별도 승인 없이 변경하지 않는다.

## 7. Model/package policy

현재 운영 모델:

- `large-v3`
- `cpu / int8`
- benchmark상 turbo 계열은 속도는 빠르지만 OOP/전공 용어 품질 regression과 누락/hallucination 문제가 있어 canonical 기본값으로 쓰지 않는다.

Package update 정책:

- `faster-whisper==1.2.1` 추가 isolated benchmark는 지금은 하지 않는다.
- production `.venv` 업그레이드는 가능하지만, 아래를 만족해야 한다.
  1. model switch와 분리한다.
  2. 현재 package/version 상태를 먼저 기록한다.
  3. rollback 명령을 문서화한다.
  4. targeted tests와 short canary를 통과한다.
  5. 문제 생기면 즉시 이전 package set으로 되돌린다.

Benchmark artifact 정책:

- raw benchmark artifact는 local `state/benchmarks/`에 둘 수 있다.
- docs에는 metric summary만 남기고 raw transcript text는 넣지 않는다.
- 긴 샘플 `260415LA`는 final candidate에만 실행한다.

## 8. Canary/live observation

확정 기준:

- canary input: 다음 실제 강의만 사용
- notification: 현재 config 유지
- downstream worker: 켜둠

실제 canary 대기 조건:

1. `git status --short --branch`, `git diff --stat`, `git diff --check`가 확인되어 있고, commit 전 변경 요약이 준비되어 있을 것.
2. DB read-only check가 통과할 것.
   - `PRAGMA integrity_check` = `ok`
   - `PRAGMA foreign_key_check` = empty
   - `PROCESSING` row = 0
   - retry 대기 row가 있다면 새 강의보다 먼저 처리될 수 있음을 명시할 것
3. launchd 상태가 canary 기준에 맞을 것.
   - `com.geonha.lecture-stt`가 running
   - `com.geonha.lecture-stt-distribute`가 running
   - cleanup/webpanel은 보조 서비스이며, cleanup이 실행 중이 아니어도 canary 자체의 blocker는 아님
4. inbox가 비어 있거나, 다음 실제 강의 파일 1개만 들어오는 controlled 상태일 것.
5. 기존 downstream problem rows는 canary blocker가 아니다. 단, known 41 problem rows는 별도 conflict/rename 작업으로 남겨두고, canary 중 destination overwrite/DB clear를 하지 않는다.
6. runtime path migration은 canary와 분리한다. installed LaunchAgents나 운영 `config/config.yaml`가 아직 repo-local log/tmp path를 쓰고 있으면 그 상태를 기록하고, migration/restart 없이 현재 운영 기준으로 canary를 기다린다.

Canary 실행/판정은 자동으로 강의를 넣는 것이 아니라 다음 실제 강의 입력을 기다리는 방식이다.

- immediate pass: 실제 강의 1개가 launchd worker로 end-to-end 완료되고, DB `DONE`, transcript txt/json 생성, 알림 동작, error/log spam 없음.
- stability gate: 이후 24h idle 관찰 또는 다음 실제 강의 2개까지 문제 없음.

### C2: launchd vs `run_once` 설명

- launchd canary
  - 실제 운영 worker, 실제 launchd 환경, 실제 log/notification 경로를 검증한다.
  - 운영 환경 검증에는 가장 의미가 크다.
  - 단점은 제어가 덜 되고, inbox가 정리되지 않았으면 예상 외 파일도 처리될 수 있다.
- `scripts/run_once.sh`
  - foreground 1회 실행이라 debugging이 쉽고, 실패 원인을 바로 보기 좋다.
  - 단점은 launchd 환경 자체를 검증하지는 못한다.
- both in sequence
  - 먼저 `run_once`로 제어된 preflight를 하고, 이후 launchd로 실제 운영 검증을 한다.
  - 가장 안전하지만 절차가 길다.

추천 기본값:

- 실제 canary pass 판정은 launchd-running worker 기준으로 한다.
- `run_once`는 launchd canary가 실패했거나 worker를 pause한 상태에서 원인 확인이 필요할 때 사용한다.

### C5: pass 기준 설명

- one completed job
  - 가장 빠른 smoke 기준이다.
  - STT, DB state, transcript 생성, notification, downstream idle 동작을 한 번 확인한다.
- 24 hours
  - log spam, idle scan, rotation, background worker 안정성까지 보기 좋다.
  - 단점은 기다리는 시간이 길다.
- next 2 real lectures
  - 실제 강의 variability를 한 번 더 확인한다.
  - canonical 운영 안정성 기준으로 가장 신뢰도가 높다.

추천 기본값:

- immediate canary pass: 실제 강의 1개가 end-to-end 완료되고 error/log spam이 없으면 pass.
- operational stability: 이후 24시간 idle 관찰 또는 다음 실제 강의 2개까지 문제 없으면 안정화로 기록.

## 9. Verification gate

코드 변경 batch마다 최소:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python -m compileall -q scripts src tests
git diff --check
git status --short --branch
git diff --stat
```

넓은 변경이나 DB/file migration 전:

- dry-run report
- before/after path/hash/row count
- DB backup/snapshot
- rollback command
- 필요 시 independent code review

## 10. Approval boundaries

명시 승인 없이 하지 않는 것:

- remote push
- tag/release
- destructive DB migration
- source/destination 파일 삭제 또는 overwrite
- launchd restart가 포함된 migration
- old log cleanup/archive
- broad home-directory scan

조건부로 가능한 것:

- plan 문서 업데이트
- read-only inventory/status check
- tests/compile/diff check
- local-only benchmark artifact 생성
- production `.venv` package update, 단 별도 계획/rollback/검증 포함
