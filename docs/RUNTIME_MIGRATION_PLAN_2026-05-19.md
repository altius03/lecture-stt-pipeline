# Runtime Path Migration Plan and Status — 2026-05-19

목적: repo 내부 runtime-like 경로를 macOS 표준 위치로 분리하되, 운영 DB와 사용자 산출물을 안전하게 보존한다.

이 문서는 계획/rollback 및 실행 상태 문서다. 최초 작성만으로는 파일 이동, 삭제, DB migration, launchd restart를 승인한 것이 아니었고, 이후 명시 승인된 범위에서 runtime migration이 적용됐다. 새 cleanup apply, DB/file movement, launchd restart, rollback은 여전히 별도 계획/승인 대상이다.

## 1. 확정 정책

- DB는 repo 내부 `state/jobs.sqlite3`에 유지한다.
- logs/tmp/cache는 repo 밖 macOS 표준 경로를 사용한다.
- 권장 target:
  - STT app log: `~/Library/Logs/lecture_stt/app.log`
  - downstream JSONL: `~/Library/Logs/lecture_stt/downstream.jsonl`
  - launchd stdout/stderr: `~/Library/Logs/lecture_stt/*.out.log`, `*.err.log`로 전환하는 방향. 단, 실제 installed plist 교체와 restart는 별도 승인 후 진행한다.
  - temp/cache: `~/Library/Caches/lecture_stt/tmp`
  - DB: `/Users/geonha/DEV/lecture_stt/state/jobs.sqlite3`
- legacy `/Users/geonha/lecture_stt`는 다시 inspect 후 결정한다.
- repo 내부 empty runtime folders는 placeholder로 유지한다.
- stale `tmp/*.wav`는 archive 후 삭제하되, cleanup 시점에 다시 확인받는다.

## 2. 현재 적용 상태

명시 승인 후 적용된 runtime migration 결과:

- STT tmp: `~/Library/Caches/lecture_stt/tmp`
- STT app log: `~/Library/Logs/lecture_stt/app.log`
- downstream JSONL: `~/Library/Logs/lecture_stt/downstream.jsonl`
- installed LaunchAgents stdout/stderr: `~/Library/Logs/lecture_stt/*.out.log`, `~/Library/Logs/lecture_stt/*.err.log`
- DB: `/Users/geonha/DEV/lecture_stt/state/jobs.sqlite3` 유지
- migration backup: `state/backups/runtime-migration-20260519T114522Z`
- post-migration log rotation: dry-run only, `state/reports/log-rotation-post-migration-dry-run-20260519T114610Z.txt`

현재 cleanup 상태:

- repo-local stale tmp/log/archive 후보는 dry-run/list만 수행했다.
- iCloud `01_audio` / `02_transcripts` bulk cleanup 후보도 dry-run/list만 수행했다.
- legacy `/Users/geonha/lecture_stt`는 read-only inventory만 수행했다.
- broad deletion/prune/truncate/archive apply는 하지 않았다.

현재 runtime path 상태를 다시 확인하려면 `config/config.yaml`, installed plist, `~/Library/Logs/lecture_stt`, `~/Library/Caches/lecture_stt/tmp`를 read-only로 대조한다.

## 3. 승인 전 허용되는 read-only/preflight

아래는 상태 확인용이며 파일/DB/launchd를 변경하지 않는다.

```bash
cd /Users/geonha/DEV/lecture_stt

git status --short --branch
git diff --stat
git diff --check

sqlite3 state/jobs.sqlite3 'PRAGMA integrity_check;'
sqlite3 state/jobs.sqlite3 'PRAGMA foreign_key_check;'

uid=$(id -u)
launchctl print "gui/$uid/com.geonha.lecture-stt" 2>/dev/null | sed -n '1,120p'
launchctl print "gui/$uid/com.geonha.lecture-stt-distribute" 2>/dev/null | sed -n '1,120p'
launchctl print "gui/$uid/com.geonha.lecture-stt-cleanup" 2>/dev/null | sed -n '1,120p'
launchctl print "gui/$uid/com.geonha.lecture-stt-webpanel" 2>/dev/null | sed -n '1,120p'
```

## 4. 적용된 migration 절차와 재실행 원칙

이미 적용된 범위:

1. preflight 기록
   - git status/diff/check
   - DB integrity/foreign key check
   - launchd status
2. backup/snapshot
   - migration backup: `state/backups/runtime-migration-20260519T114522Z`
3. target directory 사용
   - `~/Library/Logs/lecture_stt`
   - `~/Library/Caches/lecture_stt/tmp`
4. config/template update 적용
   - 운영 `config/config.yaml`의 tmp/log 경로는 macOS 표준 path 기준
   - installed LaunchAgents stdout/stderr도 `~/Library/Logs/lecture_stt` 기준
5. launchd 재시작/검증
   - 승인된 범위에서 affected labels를 재시작하고 log path를 확인했다.
6. verification
   - DB integrity/foreign key check
   - unittest/compileall/diff check
   - copied canary와 VAD compatibility fix 후 `260519OOP_2` DONE 확인

재실행 또는 추가 migration/rollback 원칙:

- 같은 종류의 변경이라도 새 DB/file/config/launchd side effect가 있으면 현재 상태를 다시 read-only로 확인한다.
- exact path list, backup/snapshot, rollback command, affected launchd labels를 먼저 보고한다.
- 사용자의 명시 승인 전에는 cleanup apply, installed plist 변경, launchd restart, DB restore를 하지 않는다.
- stale repo runtime 정리, old archive prune, iCloud audio/transcript cleanup은 migration과 분리된 별도 cleanup 작업으로 취급한다.

## 5. Rollback 절차 초안

문제 발생 시 즉시 아래 순서로 되돌린다. 실제 rollback도 launchd restart와 파일 변경을 포함하므로 실행 전 현재 상태를 짧게 확인한다.

1. affected launchd job을 중지/재시작해야 하는지 확인한다.
2. 운영 `config/config.yaml`의 runtime path를 이전 값으로 되돌린다.
   - tmp: repo `tmp` 또는 이전 운영값
   - app log: 이전 `logs/app.log` 또는 이전 운영값
   - downstream JSONL: 이전 `state/logs/downstream.jsonl` 또는 이전 운영값
3. installed plist를 변경했다면 이전 plist backup으로 복원한다.
4. DB 문제라면 `state/jobs.sqlite3`를 migration 직전 backup에서 복원한다. 복원 전 현재 DB를 별도 보존한다.
5. launchd job을 label별로 재시작하고 상태를 확인한다.
6. 검증:
   - `sqlite3 state/jobs.sqlite3 'PRAGMA integrity_check;'`
   - `sqlite3 state/jobs.sqlite3 'PRAGMA foreign_key_check;'`
   - `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v`
   - `.venv/bin/python -m compileall -q scripts src tests`
   - launchd stdout/stderr가 다시 기록되는지 확인

## 6. 명시적으로 아직 하지 않는 것

- 기존 repo `logs/`, `state/logs/`, `tmp/` 삭제 또는 archive/truncate apply
- iCloud `01_audio` / `02_transcripts` bulk cleanup apply
- legacy `/Users/geonha/lecture_stt` 삭제 또는 archive apply
- `state/jobs.sqlite3` 이동
- source/destination 산출물 overwrite/delete
- 추가 launchd restart 또는 installed plist 변경
- production `.venv` package upgrade/downgrade/rollback
- 모델 변경
- remote push/tag/release

## 7. Acceptance criteria

완료된 기준:

- runtime log/tmp/cache 경로가 macOS 표준 위치로 적용됐다.
- DB는 repo-local `state/jobs.sqlite3`에 유지된다.
- installed LaunchAgents stdout/stderr가 `~/Library/Logs/lecture_stt` 기준으로 확인됐다.
- migration backup과 post-migration dry-run report가 남아 있다.

계속 유지할 기준:

- cleanup apply는 dry-run/plan/rollback 보고 후 별도 승인 없이는 실행하지 않는다.
- rollback도 DB/file/config/launchd side effect가 있으므로 별도 승인 없이는 실행하지 않는다.
- canonical STT model은 `large-v3`로 유지한다.
- 다음 실제 강의 canary 결과는 `docs/WORKLOG.md`에 기록한다.
