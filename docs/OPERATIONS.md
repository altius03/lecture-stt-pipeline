# Lecture STT Operations Runbook

이 문서는 `lecture_stt`의 현재 운영 기준 문서다. 오래된 handoff/checklist 문서는 이력으로 보존하고, 운영 중 실제로 따라야 할 절차는 이 파일과 `docs/WORKLOG.md`를 우선한다.

## 0. 현재 결론

- 운영 repo: `/Users/geonha/DEV/lecture_stt`
- 운영 branch: `main`
- GitHub Actions workflow: `.github/workflows/ci.yml`에서 Python unittest/compileall과 web-panel test/build를 실행한다.
- 운영 모델: `large-v3` 유지. 모델 교체 금지.
- runtime package: production `.venv`와 `requirements.txt`는 `faster-whisper==1.2.1`로 정렬되어 있다. 이는 package-only 변경이며 canonical STT model 변경이 아니다.
- runtime path migration: 적용 완료. STT tmp는 `~/Library/Caches/lecture_stt/tmp`, app/downstream/launchd logs는 `~/Library/Logs/lecture_stt`, DB는 repo `state/jobs.sqlite3`에 유지한다.
- installed LaunchAgents stdout/stderr: `~/Library/Logs/lecture_stt/*.out.log`, `~/Library/Logs/lecture_stt/*.err.log` 기준으로 확인됐다.
- Hermes 교정·요약 operator와 cron: 영구 폐기. 2026-08-09 사용자 영역의 `ai.hermes.*` LaunchAgent 4개와 전용 runtime/Web UI/wrapper/app state까지 활성 경로에서 제거했으며 해당 runtime·CI 경로는 다시 활성화하지 않는다. 과거 감사 자료와 비활성 lab은 실행 경로가 아니다.
- transcript postprocess worker: `DONE` 전사를 저장된 ChatGPT 로그인 기반 Codex CLI로 교정·요약한 뒤 활성 학기 course route로 전달한다. 별도 LLM API 키나 직접 API adapter는 사용하지 않는다.
- latest real lecture canary: `260504DS_2` job `203` reached `DONE` under launchd. Operationally pass, but quality metadata is `warn` with score `61/100` due to high repetition ratio.
- quality scorecard sidecar: new jobs with quality metadata write `02_transcripts/<stem>.quality.json`. The sidecar is metadata-only and must not include transcript body or segment arrays. `260504DS_2.quality.json` was later backfilled and validated as metadata-only.
- runtime cleanup: 2026-05-20 approved closeout에서 preserve-first apply 완료. old raw audio/transcript candidates는 영구삭제 대신 iCloud `lecture_recordings/90_cleanup_archive/cleanup-20260520T014307Z`로 이동했고, repo-local stale tmp/log payloads와 legacy root는 `~/Library/Application Support/lecture_stt/cleanup_archives/cleanup-20260520T014307Z`에 보존했다.
- main branch push는 final hardening에서 완료됐지만, tag/release는 하지 않았다. 앞으로도 push/tag/release는 별도 명시 요청 없이는 하지 않는다.
- destructive DB/file/config/launchd/package/model 작업은 plan/rollback 보고 후 명시 승인 없이 하지 않는다.
- 기존 미커밋 변경은 항상 `git status`/`git diff`로 확인하고 보존한다.

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
- `com.geonha.lecture-stt-distribute`: DONE transcript→Codex 교정·요약→Obsidian 전달 worker
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
4. active semester에서 과목이 확정된 raw transcript는 `02_transcripts/<semester>/<course_dir>/{stem}.txt`와 `{stem}.json`으로 생성된다. 기존 평면 artifact와 과목 미확정/비강의 결과는 `02_transcripts` 루트에서 계속 읽는다.
5. 품질 metadata가 있으면 raw pair와 같은 과목 폴더에 `{stem}.quality.json` sidecar도 함께 생성된다. 이 파일은 score/health/metrics/timings/model/artifact path만 담고 transcript 본문이나 segment 배열은 담지 않는다. 운영 패널의 transcript count와 cleanup retention은 중첩 폴더를 재귀적으로 읽고 이 sidecar를 별도 전사 건수로 세지 않는다.
6. quality가 `DONE`이면 활성 학기 snapshot과 activation cutoff를 사용해 `transcript_postprocess_jobs`에 과목, staging/final 경로와 generator provenance를 고정한다. `NEEDS_REVIEW`는 자동 처리하지 않는다.
7. downstream worker가 Codex CLI로 교정 TXT/JSON과 요약 Markdown을 생성한다. 전사 내용은 Codex 서비스로 전송되지만 API key나 직접 HTTP API는 사용하지 않는다.
8. 원본은 그대로 두고 교정본을 `<course>/06_lecture_notes/02_origin`, 요약본을 `<course>/06_lecture_notes/01_summarize`로 전달한다.

현재 구현된 retry/failure 정책:

- retry 횟수: 기본 2회 (`app.transcribe_max_retries`).
- retry 시점: STT 실행 실패 job은 `PENDING` + `전사 재시도 대기 n/2` 상태로 남기고, 현재 진행 중인 작업이 끝난 뒤 다음 scan에서 즉시 재시도한다. 별도 timed backoff는 없다.
- retryable job은 canonical audio를 `01_audio`에 유지하며, startup recovery도 이를 inbox로 되돌리거나 job row를 삭제하지 않는다.
- terminal failed job: retry 한도 초과 후 `ERROR`로 확정되며, 수동 reset 전까지 추가 자동 재시도하지 않는다.
- 최종 실패한 원본 오디오: `99_errors`로 이동한다.
- failure report/log/DB에는 안정적인 실패 사유와 경로를 남기되 secret-like 문자열은 `[REDACTED]`로 마스킹한다.

## 4. 학기 activation과 Codex 후처리 기준

학기 변경은 candidate manifest 편집만으로 끝내지 않는다.
Activation 전에는 `jobs`의 `PENDING`/`PROCESSING`/전사 재시도와
`transcript_postprocess_jobs.status=PENDING` row가 모두 0이어야 한다. 실행
중인 한 작업이 이전 raw 경로와 새 downstream snapshot을 섞지 않도록 이 상태를
먼저 확인한다. `apply`는 jobs DB 쓰기 잠금 안에서 같은 조건을 다시 검사한다.

```bash
PYTHONPATH=src .venv/bin/python -m lecture_stt.downstream.semester plan \
  --manifest config/semesters/2026-2.yaml

PYTHONPATH=src .venv/bin/python -m lecture_stt.downstream.semester apply \
  --manifest config/semesters/2026-2.yaml \
  --active-path state/active-semester.json \
  --jobs-db-path state/jobs.sqlite3 \
  --expected-course-count 7 \
  --expected-plan-sha256 <plan 출력값> \
  --allow-write
```

Plan/apply gate:

- `vault_root/.obsidian`과 vault 내부 `semester_root`를 확인한다.
- symlink component를 거부한다.
- 각 course directory와 `06_lecture_notes/02_origin`, `06_lecture_notes/01_summarize`가 이미 있어야 한다. 자동 생성하지 않는다.
- manifest course code/name 집합이 Storage v2의 선택된 해당 학기 timetable과 정확히 같아야 한다.
- alias/course code/course directory 중복을 거부한다.
- apply는 exact course count와 plan SHA-256, `--allow-write`를 모두 요구한다.
- jobs DB와 필수 table이 없거나 STT `PENDING`/`PROCESSING`, postprocess
  `PENDING`이 하나라도 있으면 active snapshot을 바꾸지 않는다.
- 현재 snapshot의 `activated_at` 이후 `DONE`인데 postprocess row가 아직 없는
  작업도 enqueue 직전 구간으로 간주해 전환을 차단한다.
- Queue가 없고 완료 시각도 해석할 수 없는 `DONE`은 cutoff 전후를 증명할 수
  없으므로 `invalid_unqueued_done_jobs`로 세어 전환을 차단한다.

2026-2 filename aliases:

- `CA`: 컴퓨터구조
- `CN`: 컴퓨터네트워크
- `DB`: 데이터베이스
- `IS`: 정보보안개론
- `IT`: IT신기술
- `HMS`: 인간과현대사회
- `WLT`: 서구문학과기술문명

예: `260901CA_1.m4a`. Alias가 없으면 profile이 `lecture`인 경우에만 녹음 시각과 시간표의 유일 후보를 사용한다. 후보 없음·복수이면 `UNROUTED`로 남기며 폴더를 추측하거나 생성하지 않는다.

과목 route가 확정되면 STT와 downstream이 같은 결정 함수를 사용해 아래 상대 경로를 고정한다.

```text
02_transcripts/<semester>/<course_dir>/<canonical_base>.{txt,json,quality.json}
03_correction/<semester>/<course_dir>/<logical_stem>.{txt,json}
04_summarize/<semester>/<course_dir>/<logical_stem>.md
```

중첩 parent는 실제 write 직전에 설정된 root 아래에서만 생성한다. `..`, 절대경로, symlink, 비-directory component는 거부한다. 기존 평면 파일과 기존 queue row는 이동·재작성하지 않는다.

후처리 설정에는 cutoff를 중복 저장하지 않는다.

```yaml
transcript_delivery:
  enabled: true
  active_semester_manifest: state/active-semester.json
  correction_staging_dir: ${LECTURE_RECORDINGS_ROOT}/03_correction
  summary_staging_dir: ${LECTURE_RECORDINGS_ROOT}/04_summarize
  generator:
    backend: codex_cli
    codex_binary: /Users/geonha/.local/bin/codex
    model: gpt-5.6-terra
    reasoning_effort: low
    timeout_sec: 1200
    max_attempts: 3
    max_correction_chars: 100000
    max_summary_chars: 100000
```

Active snapshot의 timezone-aware `activated_at` 이전에 끝난 기존 `DONE` 작업은
reconcile 대상에서 제외한다. 별도 config 값을 맞출 필요가 없으며 유효하지 않거나
timezone이 없는 `activated_at`은 거부한다. 활성화 전에 Codex 로그인과 read-only
상태/dry-run을 확인한다.

```bash
/Users/geonha/.local/bin/codex login status
bash scripts/distribute_status.sh
bash scripts/distribute_once.sh --dry-run
```

Postprocess/Delivery conflict 기준:

- source TXT/JSON은 `02_transcripts/<semester>/<course_dir>` 정본이며 교정·전달 뒤에도 삭제하거나 수정하지 않는다. 기존 평면 source도 계속 유효하다.
- 교정 staging은 `03_correction/<semester>/<course_dir>/{stem}.txt/.json`, 요약 staging은 `04_summarize/<semester>/<course_dir>/{stem}.md`다.
- 교정 destination은 과목별 `02_origin/{stem}.txt/.json`, 요약 destination은 `01_summarize/{stem}.md`다.
- staging 또는 destination 중 하나라도 다른 hash로 존재하면 overwrite하지 않고 `CONFLICT`로 남긴다.
- staging write 전 DB에 output hash intent를 고정한다. `READY` commit 전 worker가 중단되면 pinned correction JSON/TXT 또는 summary Markdown을 재검증해 같은 단계에서 이어간다.
- source hash가 enqueue 이후 달라지면 `ERROR`로 닫고 재활성화나 자동 덮어쓰기를 하지 않는다.
- 같은 hash의 기존 파일은 idempotent success로 처리한다.
- 학기 snapshot이 바뀌어도 기존 queue row의 semester/root/course/destination은 바꾸지 않는다.
- 실시간 enqueue/reconcile source는 `state/jobs.sqlite3`의 `jobs`이며, 별도 Storage v2 DB는 학기 시간표 검증과 시간 기반 fallback 조회에만 사용한다.
- Codex 결과의 segment ID·개수·순서 또는 요약 계약이 틀리면 `NEEDS_REVIEW`로 격리하고 최종 전달하지 않는다.
- Codex/파일시스템 실행 오류는 같은 단계에서 기본 3회까지 재시도하고 한도를 넘으면 `ERROR`로 닫는다.

상태 확인:

```bash
bash scripts/distribute_status.sh
bash scripts/distribute_status.sh list --only-problems
```

기본 summary는 `active_semester`, snapshot `activated_at`과 동일한
`effective_cutoff`, STT 진행/문제, queue 미적재 `DONE`, postprocess 대기/문제
건수와 전체 상태를
출력한다. 문제 row는 `list --only-problems`, 세부 고정 경로와 오류 단계는
`show <source_job_id>`로 확인한다. `HEALTHY`는 대기·문제 없음, `BUSY`는 정상
처리 중, `ATTENTION`은 `UNROUTED`/`CONFLICT`/`NEEDS_REVIEW`/`ERROR` 또는 STT
검토·오류, queue 미적재·완료 시각 불명 `DONE`이 있음을 뜻한다. 완료 시각이
손상된 행은 reconciliation 결과에 `INVALID_COMPLETION_TIMESTAMP`와 job ID를
남기되 뒤의 정상 `DONE` 처리는 계속한다.

`bash scripts/distribute_once.sh --dry-run`은 `state/jobs.sqlite3`를 read-only mode로 열어 activation 이후 미큐잉 `DONE` 건과 기존 `PENDING` 건을 계획만 한다. DB/table, lock, JSONL, 과목별 staging directory, 목적 파일을 생성·갱신하지 않는다.

과거 `transcript_deliveries` 직접 전달 원장과 `deliveries` correction/summary 247개 row는 감사·Storage v2 archive evidence로 보존한다. 현재 worker는 새 `transcript_postprocess_jobs`만 처리하며, 새 row는 course-scoped staging 경로를 pin하고 기존 row의 평면 경로는 그대로 존중한다.

## 5. Log policy

확정 정책:

- retention 기준: size + date 둘 다 사용
- app logs: 10MB x 5
- transcript-delivery JSONL: 10MB x 5
- launchd stdout/stderr: 10MB x 3
- compressed archive 보존: 30일
- routine scan 로그: 기본값에서 `scan_started`는 stdout/JSONL 모두 비활성화하고, `scan_completed`는 최초/변경/heartbeat만 JSONL에 남긴다.
- old log archive/cleanup: 그 시점에 다시 확인 후 진행

이미 적용/준비된 운영값:

- STT app log 기본값: `logging.max_bytes: 10485760`, `logging.backup_count: 5`
- transcript-delivery JSONL: `~/Library/Logs/lecture_stt/transcript-delivery.jsonl`
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

현재 적용 상태:

- DB는 repo 내부 `/Users/geonha/DEV/lecture_stt/state/jobs.sqlite3` 유지.
- STT tmp/cache는 `~/Library/Caches/lecture_stt/tmp` 사용.
- app log는 `~/Library/Logs/lecture_stt/app.log` 사용.
- transcript-delivery JSONL은 `~/Library/Logs/lecture_stt/transcript-delivery.jsonl` 사용.
- 활성 학기 snapshot은 repo-local `state/active-semester.json` 사용.
- 폐기한 AI 후처리 operator의 runtime state는 `state/reports/retired-ai-postprocess-20260807`로 이동해 과거 감사 증거만 보존한다.
- installed LaunchAgents stdout/stderr는 `~/Library/Logs/lecture_stt/*.out.log`, `~/Library/Logs/lecture_stt/*.err.log` 기준으로 확인됐다.
- legacy `/Users/geonha/lecture_stt`는 2026-05-20 cleanup에서 삭제하지 않고 `~/Library/Application Support/lecture_stt/cleanup_archives/cleanup-20260520T014307Z/legacy_root`로 이동 보존했다.
- repo 안 empty runtime folders는 placeholder로 유지한다.
- repo-local stale tmp/log/state-log payloads는 2026-05-20 cleanup에서 `~/Library/Application Support/lecture_stt/cleanup_archives/cleanup-20260520T014307Z`로 이동 보존했다. iCloud audio/transcript cleanup 후보도 영구삭제하지 않고 `lecture_recordings/90_cleanup_archive/cleanup-20260520T014307Z`로 이동 보존했다.
- Transcript cleanup retention은 `{stem}.txt`, `{stem}.json`, `{stem}.quality.json`을 한 transcript set으로 묶어 판단한다. `retain_min_transcripts`는 파일 개수가 아니라 primary transcript set 개수 기준이며, apply 시 sidecar만 남거나 primary transcript만 삭제되는 상태가 되지 않도록 함께 보존/정리한다. Orphan-only `{stem}.quality.json`은 최소 보존 슬롯을 소비하지 않는다.

관련 이력/rollback 문서:

- `docs/RUNTIME_MIGRATION_PLAN_2026-05-19.md`
- migration backup: `state/backups/runtime-migration-20260519T114522Z`
- runtime cleanup dry-run/list report: `state/reports/runtime-cleanup-20260519T123847Z`
- runtime cleanup apply report: `state/reports/runtime-cleanup-apply-20260520T014307Z`

추가 runtime cleanup 또는 rollback은 운영 파일 변경/launchd 영향이 있으므로 다음을 먼저 보고한 뒤 승인받는다.

1. exact target list
2. backup/snapshot 위치
3. rollback command
4. 예상 영향 범위
5. 적용 후 verification gate

## 7. Model/package policy

현재 운영 모델:

- `large-v3`
- `cpu / int8`
- benchmark상 turbo 계열은 속도는 빠르지만 OOP/전공 용어 품질 regression과 누락/hallucination 문제가 있어 canonical 기본값으로 쓰지 않는다.

현재 runtime package 상태:

- production `.venv`: `faster-whisper==1.2.1`
- `requirements.txt`: `faster-whisper==1.2.1`
- 이 변경은 명시 승인된 runtime package workstream에서 적용됐다.
- package-only 변경이며 `transcribe.model_size`는 계속 `large-v3`다.
- faster-whisper 1.2 계열의 VAD parameter signature 차이는 compatibility fix로 보정됐다.

Package rollback 정책:

- 다음 실제 강의 canary 또는 운영 관찰에서 1.2.1 regression이 확인되면 rollback 후보는 `faster-whisper==1.1.0`이다.
- rollback도 production `.venv` 변경과 launchd restart가 필요할 수 있으므로, 실행 전 계획/rollback/검증 범위를 다시 보고하고 승인받는다.
- rollback 후에는 targeted tests, compileall, DB integrity check, 다음 실제 강의 canary를 다시 본다.

Benchmark artifact 정책:

- raw benchmark artifact는 local `state/benchmarks/`에 둘 수 있다.
- docs에는 metric summary만 남기고 raw transcript text는 넣지 않는다.
- 긴 샘플 `260415LA`는 final candidate에만 실행한다.
- B1=1에 따라 현재 benchmark expansion은 보류한다.

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
5. 기존 downstream problem rows는 canary blocker가 아니다. 2026-05-20 기준 known problem rows는 `0`이며, canary 중에는 destination overwrite/DB clear 같은 별도 cleanup 작업을 섞지 않는다.
6. runtime path migration은 이미 적용 완료 상태다. canary 중에는 새 config 변경, cleanup apply, launchd restart를 하지 않고 현재 운영 기준으로 관찰한다.

Canary 실행/판정은 자동으로 강의를 넣는 것이 아니라 다음 실제 강의 입력을 기다리는 방식이다.

- immediate pass: 실제 강의 1개가 launchd worker로 end-to-end 완료되고, DB `DONE`, transcript txt/json 생성, 알림 동작, error/log spam 없음.
- stability gate: 이후 24h idle 관찰 또는 다음 실제 강의 2개까지 문제 없음.

현재 기록된 latest immediate pass:

- `260504DS_2` / job `203` / `DONE`
- outputs: `01_audio/260504DS_2.m4a`, `02_transcripts/260504DS_2.txt`, `02_transcripts/260504DS_2.json`, `02_transcripts/260504DS_2.quality.json`
- quality: `warn`, `61/100`, high repetition ratio
- note: job `203` completed before quality sidecar support was deployed, so `260504DS_2.quality.json` was later backfilled and validated from existing metadata; it remains metadata-only.

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
