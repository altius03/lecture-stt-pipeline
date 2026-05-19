# Runtime Path Migration Plan — 2026-05-19

목적: repo 내부 runtime-like 경로를 macOS 표준 위치로 분리하되, 운영 DB와 사용자 산출물을 안전하게 보존한다.

이 문서는 계획/rollback 문서다. 이 문서를 작성하는 것만으로 파일 이동, 삭제, DB migration, launchd restart를 승인한 것이 아니다.

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

## 2. 현재 코드/config 상태

이미 안전하게 반영 가능한 path default만 코드/config에 반영한다.

- `lecture_stt.shared.paths.default_log_dir()` → `~/Library/Logs/lecture_stt`
- `lecture_stt.shared.paths.default_cache_dir()` → `~/Library/Caches/lecture_stt`
- `lecture_stt.shared.paths.default_tmp_dir()` → `~/Library/Caches/lecture_stt/tmp`
- STT 기본 `logging.file` → `~/Library/Logs/lecture_stt/app.log`
- STT 기본 `paths.tmp_dir` → `~/Library/Caches/lecture_stt/tmp`
- downstream 기본 `log_jsonl_path` → `~/Library/Logs/lecture_stt/downstream.jsonl`
- `config/config.example.yaml`도 위 기본값을 문서화한다.

운영 `config/config.yaml`, installed LaunchAgents, 기존 log/tmp 파일은 이 문서만으로 변경하지 않는다.

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

## 4. 승인 후 migration 절차 초안

다음 절차는 아직 실행하지 않는다. 실행 전 dry-run 결과와 exact path list를 다시 보고하고 승인을 받는다.

1. preflight 기록
   - git status/diff/check
   - DB integrity/foreign key check
   - launchd status
   - inbox가 다음 실제 강의 canary를 방해하지 않는지 확인
2. backup/snapshot
   - `state/jobs.sqlite3`를 timestamped backup으로 copy
   - 기존 repo log와 tmp stale wav 목록을 timestamped manifest로 저장
3. target directory 생성
   - `~/Library/Logs/lecture_stt`
   - `~/Library/Caches/lecture_stt/tmp`
4. config/template update 적용
   - 운영 `config/config.yaml`에서 tmp/log 경로를 macOS 표준 path로 변경
   - launchd stdout/stderr template 또는 installed plist 교체가 필요한 경우 변경 내역을 별도 diff로 확인
5. launchd 재시작
   - 사용자에게 label별 restart plan을 다시 보여준 뒤 승인받는다.
   - restart는 STT/downstream/webpanel/cleanup 영향이 있으므로 한 번에 하지 말고 label별로 상태를 확인한다.
6. verification
   - DB integrity/foreign key check
   - unit tests + compileall + diff check
   - launchd status/log path 확인
   - 다음 실제 강의 canary 대기
7. stale repo runtime 정리
   - repo `tmp/*.wav` archive 후 삭제는 별도 승인 후 진행한다.
   - old logs archive/cleanup도 별도 승인 후 진행한다.

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

- 기존 repo `logs/`, `state/logs/`, `tmp/` 삭제 또는 archive/truncate
- `state/jobs.sqlite3` 이동
- source/destination 산출물 overwrite/delete
- launchd restart
- production `.venv` package upgrade
- 모델 변경
- remote push/tag/release

## 7. Acceptance criteria

- path default tests가 macOS 표준 위치와 repo-local DB를 검증한다.
- `config/config.example.yaml`이 같은 정책을 문서화한다.
- 운영 runbook에서 migration/rollback 문서를 찾을 수 있다.
- 실제 migration은 dry-run/plan/rollback 보고 후 별도 승인 없이는 실행하지 않는다.
