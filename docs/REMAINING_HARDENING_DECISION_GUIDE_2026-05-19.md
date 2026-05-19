# Lecture STT 남은 고도화 업무 결정 가이드 — 2026-05-19

이 문서는 `lecture_stt` 최초 고도화 계획에서 남은 업무를 사용자가 결정하기 쉽게 정리한 안내서다.

중요: 이 문서를 작성하거나 읽는 것만으로 실제 운영 변경을 승인한 것이 아니다. DB row 삭제, 파일 이동/삭제/덮어쓰기, launchd 재시작, runtime path migration, package upgrade, push/tag/release는 별도 명시 승인 전에는 하지 않는다.

## 0. 현재 상태 요약

Post-final live 확인 기준:

- repo: `/Users/geonha/DEV/lecture_stt`
- branch: `main`
- remote main / HEAD at clean baseline: `48be5a4ddf5614ca5a37865c249b6d642dd0ca3f`
- canonical STT model: `large-v3`
- production model switch: 금지
- production `.venv` / `requirements.txt`: `faster-whisper==1.2.1`
- runtime migration: 적용 완료
  - tmp: `~/Library/Caches/lecture_stt/tmp`
  - logs: `~/Library/Logs/lecture_stt`
  - DB: `/Users/geonha/DEV/lecture_stt/state/jobs.sqlite3` 유지
  - installed LaunchAgents stdout/stderr도 `~/Library/Logs/lecture_stt` 기준
- STT/downstream launchd worker: running
- inbox: 실제 강의 input 없음 (`.DS_Store`만 확인)
- DB integrity: `ok`
- STT job 상태:
  - `DONE=30`
  - `ERROR=1` (`260519OOP_1` 실패 증거 보존)
- downstream delivery row:
  - total `152`
  - problem rows `4`
  - `INVALID_STEM=3`
  - `CONFLICT=1` (`260422LC`, document-only 보호 대상)

이미 완료된 큰 축:

- STT retry/failure policy 구현
- secret redaction 보강
- log retention/rotation 구현
- downstream problem diagnosis/report/clear-stale tooling 보강
- runtime path migration 적용
- production package alignment to `faster-whisper==1.2.1`
- VAD compatibility fix
- copied canary `260519OOP_2` DONE
- downstream safe repair 적용 후 problem rows 4건까지 축소
- `docs/OPERATIONS.md` 운영 runbook 추가 및 post-final 정합성 업데이트

지금 남은 핵심은 “다음 실제 강의 canary”, “downstream invalid stem 3건 파일별 결정”, “cleanup apply 여부”, “문서/commit 정리”다.

---

## 1. 용어 풀이

### canary

운영 환경에서 작은 입력 1개로 전체 파이프라인이 정상인지 확인하는 테스트다.

이 프로젝트에서는 “다음 실제 강의 파일 1개”를 iCloud inbox에 넣고, launchd가 실행 중인 실제 STT worker가 처리하게 둔 뒤 결과를 본다.

확인하는 것:

- DB job이 `DONE`이 되는지
- transcript txt/json이 생성되는지
- notification이 현재 설정대로 동작하는지
- downstream worker가 불필요한 에러나 로그 폭주를 만들지 않는지

왜 필요한가:

- unit test는 코드 단위 검증이다.
- canary는 실제 macOS launchd, iCloud path, DB, log, notification까지 포함한 운영 검증이다.

### immediate pass / stability gate

- immediate pass: 실제 강의 1개가 end-to-end로 완료되면 빠르게 “즉시 smoke 통과”로 본다.
- stability gate: 그 뒤 24시간 idle 상태 또는 다음 실제 강의 2개까지 더 보면서 장기 안정성을 본다.

추천은 둘을 분리하는 것이다. 즉, 1개 성공을 immediate pass로 기록하고, 24h/다음 2개는 별도 안정성 관찰로 남긴다.

### launchd

macOS의 백그라운드 서비스 관리자다. Linux의 systemd와 비슷하게, 지정한 프로그램을 로그인 세션에서 자동 실행하거나 재시작한다.

이 프로젝트의 주요 launchd label:

- `com.geonha.lecture-stt`: 메인 STT worker
- `com.geonha.lecture-stt-distribute`: correction/summary downstream worker
- `com.geonha.lecture-stt-cleanup`: cleanup worker
- `com.geonha.lecture-stt-webpanel`: web panel

주의점:

- launchd restart는 실제 운영 worker를 재시작하는 일이다.
- 재시작 중 현재 처리 중인 작업, log file descriptor, 환경변수, config 반영 타이밍에 영향을 줄 수 있다.
- 그래서 migration이나 canary 중 restart는 계획/rollback 보고 후 승인받아야 한다.

### downstream

STT가 만든 raw transcript 이후 단계다.

대략 흐름:

1. 사용자가 `03_correction`에 correction txt/json을 둔다.
2. summary md가 `04_summarize`에 생긴다.
3. downstream worker가 이 산출물을 GH archive나 Obsidian 목적지로 복사/배포한다.

여기서 문제가 생기는 대표 원인:

- destination에 이미 다른 내용의 파일이 있음
- filename stem이 규칙과 맞지 않음
- subject route가 설정에 없음
- DB row는 남아 있지만 source 파일이 없음

### canonical

“정답으로 삼을 기준 원본”이라는 뜻이다.

현재 downstream conflict 정책에서는 `03_correction`의 source 파일을 canonical로 보는 방향이 기본이다.

하지만 canonical을 source로 정했다고 해서 destination을 바로 덮어써도 된다는 뜻은 아니다. 실제 overwrite는 다음 절차가 필요하다.

1. dry-run table
2. destination backup
3. before/after hash 확인
4. 사용자 명시 승인
5. 적용 후 재검증

### hash / sha256

파일 내용을 숫자/문자열 지문처럼 요약한 값이다.

- 같은 파일 내용이면 같은 sha256이 나온다.
- 내용이 1글자라도 다르면 sha256이 달라진다.

이 프로젝트에서 hash는 “source와 destination이 정말 같은 내용인가?”를 판단하는 안전장치다.

### hash-conflict

source 파일과 destination 파일이 둘 다 존재하지만 sha256이 다른 상태다.

뜻:

- 둘 중 하나가 더 최신이거나 더 정확할 수 있다.
- 자동 overwrite하면 사용자가 수동으로 고친 destination을 덮어쓸 위험이 있다.

현재 live 기준 `hash-conflict`는 25건이다.

추천 처리:

- source `03_correction`을 canonical로 보되,
- destination을 바로 덮어쓰지 말고,
- backup/dry-run/before-after hash/명시 승인 후 처리한다.

### route / subject route

파일명을 보고 어느 과목 폴더로 보낼지 정하는 규칙이다.

예:

- `260504DS_1` → `DS` 과목
- `260429LA` → `LA` 과목
- `260428Unix_2` → `Unix` 과목

subject route가 없으면 downstream worker가 목적지 폴더를 결정하지 못한다.

### route/rename-needed

파일명 또는 subject route 문제 때문에 자동 배포하기 어려운 상태다.

현재 live 기준 15건이다.

두 종류가 섞여 있다.

1. invalid stem 8건
   - 파일명이 강의 stem 규칙과 맞지 않음
   - 예: `test123`, `zztest`, `BOSS_SPECIAL_LECTURE`
   - 강의 산출물이면 rename, 아니면 downstream 대상에서 제외해야 한다.

2. unknown subject / route suffix 7건
   - 날짜+과목처럼 보이지만 파일명 뒤에 timestamp/hash suffix가 붙어 있거나 subject route가 없음
   - proposed stem을 계산할 수 있는 경우가 있다.
   - 예: `260407DS_2_20260412_013113_58e799` → proposed stem `260407DS_2`

추천 처리:

- manual table을 만들고 사용자가 확인한다.
- obvious한 proposed stem은 후보로 제시하되, 자동 rename은 하지 않는다.

### source-missing / DB-only stale row

DB에는 row가 남아 있지만, 실제 source 파일이 없거나 더 이상 처리할 수 없는 상태다.

현재 live 기준 source-missing은 1건이고 `260422LC`다.

다만 `260422LC`는 현재 정책상 document-only 보호 대상이다. 즉, 자동 clear하지 않는다.

### clear / row clear

DB의 delivery row를 삭제하는 작업이다.

주의:

- 파일 삭제와는 다르지만 운영 DB를 바꾸는 작업이다.
- 잘못 지우면 이후 상태 추적이 어려워질 수 있다.
- 그래서 `clear-stale`은 dry-run, backup path, `--yes` 같은 안전장치를 요구한다.

현재 `260422LC`는 코드상 clear가 거부되도록 보호되어 있다.

### dry-run

실제로 바꾸지 않고 “바꾼다면 무엇을 하게 되는지”만 보여주는 모드다.

예:

- 어떤 DB row를 지울 예정인지 보여줌
- 어떤 파일을 rotate할지 보여줌
- 어떤 destination을 overwrite할 후보인지 보여줌

중요:

- dry-run은 안전 확인 단계다.
- dry-run 결과가 좋아도 실제 apply는 별도 승인 후 해야 한다.

### apply

dry-run과 반대로 실제 변경을 수행하는 모드다.

예:

- log 파일 truncate
- DB row 삭제
- config 변경
- 파일 move/delete/overwrite

이 프로젝트에서는 apply 성격의 작업은 모두 승인 게이트 뒤에 둔다.

### backup / snapshot

변경 전 상태를 복사해두는 것이다.

예:

- DB 변경 전 `state/jobs.sqlite3`를 `state/backups/...sqlite3`로 복사
- destination overwrite 전 기존 destination 파일을 별도 위치에 보관

목적:

- 잘못되면 되돌릴 수 있게 한다.
- before/after 비교가 가능하다.

### rollback

문제가 생겼을 때 이전 상태로 되돌리는 절차다.

예:

- 변경한 config를 이전 값으로 되돌림
- launchd plist를 이전 파일로 복원
- DB를 backup에서 복원
- launchd job을 다시 재시작

runtime migration이나 package upgrade처럼 운영에 영향을 주는 작업은 rollback 계획이 먼저 있어야 한다.

### runtime path migration

실행 중 생기는 파일들을 repo 밖 표준 위치로 옮기는 작업이다.

현재 목표:

- DB: repo 내부 `state/jobs.sqlite3` 유지
- logs: `~/Library/Logs/lecture_stt`
- tmp/cache: `~/Library/Caches/lecture_stt`

왜 하는가:

- source code repo와 runtime data를 분리한다.
- git status가 runtime 파일로 오염되는 것을 줄인다.
- macOS 표준 위치에 맞춘다.

주의:

- 운영 `config/config.yaml` 변경이 필요할 수 있다.
- installed LaunchAgents plist 변경이 필요할 수 있다.
- launchd restart가 필요할 수 있다.
- 그래서 canary와 분리해서 진행하는 편이 안전하다.

### retention / log rotation

log가 무한히 커지지 않도록 보존 개수/기간을 정하고 오래된 파일을 정리하는 정책이다.

현재 정책:

- app logs: 10MB x 5
- downstream JSONL: 10MB x 5
- launchd stdout/stderr: 10MB x 3
- compressed archive: 30일

### copytruncate

log rotation 방식 중 하나다.

일반 rename rotation은 active log 파일을 `.1`로 rename하고 새 파일을 만든다. 그런데 launchd처럼 이미 열린 file descriptor에 계속 쓰는 프로세스는 rename 후에도 예전 파일에 계속 쓸 수 있다.

copytruncate는:

1. active log 내용을 backup 파일로 복사한다.
2. active log 파일 자체는 그대로 두고 내용만 0 byte로 비운다.

장점:

- launchd가 열어둔 파일 경로가 유지된다.
- restart 없이도 active log 크기를 줄일 수 있다.

주의:

- 실제 truncate는 운영 파일 변경이다.
- 그래서 `scripts/rotate_logs.py`는 기본 dry-run이고, `--apply`가 있어야 실제 변경한다.

### `.venv` / production package update

`.venv`는 Python 패키지가 설치된 가상환경이다.

production `.venv` package update는 실제 운영 worker가 사용하는 Python dependency를 바꾸는 작업이다.

주의:

- 모델을 바꾸지 않아도 package 버전 변화만으로 STT 결과나 성능이 달라질 수 있다.
- 특히 `faster-whisper`는 모델 실행 결과에 영향을 줄 수 있다.
- 그래서 package update는 model switch와 분리해서 별도 계획/rollback/검증 후 해야 한다.

### benchmark

모델이나 package 후보를 샘플 오디오로 비교 측정하는 작업이다.

확인 항목:

- 처리 시간
- transcript 길이
- 누락/반복/hallucination 여부
- 전공 용어 품질

현재 결론:

- `large-v3` 유지
- turbo/distil/Korean turbo 후보는 현재 품질 기준 탈락
- `faster-whisper==1.2.1`은 이후 명시 승인으로 production package alignment가 적용됐고, canonical model은 계속 `large-v3`

### artifact

작업 중 생성되는 결과 파일이다.

예:

- benchmark raw transcript
- JSON report
- dry-run report
- backup DB

정책:

- raw benchmark artifact는 local `state/benchmarks/`에 둘 수 있다.
- docs에는 raw transcript text를 넣지 않고 metric summary만 넣는다.

---

## 2. 남은 업무와 결정 질문

아래 질문에는 코드 형태로 답하면 된다.

예:

```text
C1=1, C2=1, C3=1, C4=1, C5=1
D1=1, D2=1, D3=1, D4=1, D5=3, D6=1, D7=1
R1=3, R2=1, R3=1, R4=1, R5=1, R6=1, R7=2
L1=1, L2=1, L3=3, L4=1
P1=3, P2=1, P3=1, P4=1
B1=1, B2=1, B3=1, B4=1
O1=1, O2=1, O3=1
```

`추천대로`라고 답해도 된다. 특정 항목만 바꾸고 싶으면 `추천대로, 단 D5=1`처럼 답하면 된다.

현재 실행 세션에서 사용자가 지정한 결정값은 아래 snapshot을 따른다.

```text
C1=1, C2=1, C3=1, C4=1, C5=1
D1=1, D2=1, D3=1, D4=1, D5=3, D6=1, D7=1
R1=3, R2=1, R3=1, R4=1, R5=1, R6=1, R7=2
L1=1, L2=1, L3=3, L4=1
P1=2, P2=2, P3=3, P4=1
B1=1, B2=1, B3=1, B4=1
O1=1, O2=1, O3=1
```

이후 별도 명시 요청으로 main branch push는 이번 세션에 한해 승인됐지만, tag/release와 모델 교체는 계속 제외한다.

---

## 3. C — Canary / live observation

### 남은 일

- 다음 실제 강의 파일 1개를 canary input으로 사용한다.
- launchd-running worker 기준으로 관찰한다.
- `scripts/run_once.sh`는 debugging/preflight 보조로만 사용한다.
- downstream worker는 켜둔 상태로 판단한다.
- notification은 현재 config를 유지한다.
- 결과를 WORKLOG에 기록할 수 있다.

### 결정 질문

#### C1. canary input은?

추천: `1`

1. 다음 실제 강의만 사용
   - 장점: 실제 운영 흐름 검증에 가장 정확하다.
   - 단점: 다음 강의가 들어올 때까지 기다려야 한다.
2. 기존 샘플 복사본으로 test stem canary도 허용
   - 장점: 바로 테스트 가능하다.
   - 단점: 실제 강의 흐름과 다를 수 있고, test artifact 정리가 필요하다.
3. canary 보류
   - 장점: 지금 운영에 아무 영향이 없다.
   - 단점: 실제 end-to-end 검증이 미뤄진다.

#### C2. canary pass 기준은?

추천: `1`

1. 실제 강의 1개 완료면 immediate pass, 이후 24h/다음 2개는 stability gate
   - 빠른 판단과 장기 관찰을 분리한다.
2. 24h idle까지 끝나야 pass
   - 더 보수적이지만 오래 걸린다.
3. 다음 실제 강의 2개까지 끝나야 pass
   - 실제 입력 다양성을 더 보지만 시간이 걸린다.

#### C3. canary 중 downstream worker는?

추천: `1`

1. 켜둔 상태 유지
   - 실제 운영 환경과 같다.
2. STT만 보려고 일시 중지
   - STT만 분리 검증하기 좋지만 downstream 운영 검증은 빠진다.
3. 상황 보고 다시 결정

#### C4. canary 중 notification은?

추천: `1`

1. 현재 config 유지
2. 임시 비활성화
3. Telegram만
4. Discord만

#### C5. canary 결과 문서화는?

추천: `1`

1. `docs/WORKLOG.md`에 기록
2. `docs/OPERATIONS.md`에도 canary 결과 섹션 추가
3. 채팅 보고만 하고 문서 기록 안 함

---

## 4. D — Downstream problem rows 정리

### 남은 일

현재 problem rows는 4건이다.

- `INVALID_STEM`: 3건
  - `선형대수학_시험출제포인트_전체정리`
  - `BOSS_SPECIAL_LECTURE`
  - `2603034LA_2`
- `CONFLICT`: 1건
  - `260422LC`, document-only 보호 대상

가능한 작업 단계:

1. read-only report/manual table 생성
2. invalid stem 3건의 rename/exclude/document-only 여부 파일별 결정
3. `260422LC`는 그대로 두고 문서화만 유지
4. 승인 후에만 DB row clear, destination overwrite, rename, route change 진행

### 결정 질문

#### D1. downstream 정리를 지금 착수할까?

추천: `1`

1. read-only report/manual table까지만 지금 생성
2. report 생성 후, 승인 가능한 일부 DB-only clear까지 검토
3. 전체 정리 계획까지 만들되 실제 변경은 보류
4. downstream 정리 자체를 나중으로 미룸

#### D2. hash-conflict 25건의 기본 canonical은?

추천: `1`

1. source `03_correction`이 canonical
2. destination/GH_archive가 canonical
3. 파일별 manual compare 후 결정

#### D3. destination overwrite를 나중에 허용할 조건은?

추천: `1`

1. dry-run table + destination backup + before/after hash 확인 + 명시 승인 후 허용
2. 파일별로 매번 승인
3. overwrite는 금지, 수동 처리만 허용

#### D4. route/rename-needed 15건은?

추천: `1`

1. manual table 생성 후 사용자가 확인
2. obvious한 7건은 filename stem rule로 proposed route를 만들고, invalid 8건만 수동 확인
3. 모두 document-only, 실제 rename/route 변경 금지

#### D5. invalid stem 8건 처리 기준은?

추천: `3`

1. 강의 산출물인지 파일별 확인 후 rename 또는 exclude 결정
2. 전부 exclude 후보로 두고 자동 배포 대상에서 제외
3. 사용자가 파일별 표를 보고 하나씩 결정

#### D6. `260422LC`는?

추천: `1`

1. 그대로 두고 문서화만
2. destination에서 source 재구성 가능성만 read-only 검토
3. DB-only stale clear 후보로 다시 검토

#### D7. downstream report 저장 위치는?

추천: `1`

1. gitignored `state/reports/`에 JSON/CSV 생성
   - 운영 snapshot으로 적합하고 repo commit에는 포함되지 않는다.
2. repo 문서 `docs/`에 요약 markdown만 생성
   - 의사결정 기록으로 남기 좋지만 민감한 path/hash가 들어갈 수 있다.
3. 둘 다 생성
4. 파일 생성 없이 채팅 보고만

---

## 5. R — Runtime path migration

### 남은 일

runtime path migration은 적용 완료됐다.

- tmp/cache: `~/Library/Caches/lecture_stt/tmp`
- app/downstream/launchd logs: `~/Library/Logs/lecture_stt`
- DB: repo 내부 `state/jobs.sqlite3` 유지
- installed LaunchAgents stdout/stderr: `~/Library/Logs/lecture_stt/*.out.log`, `*.err.log`

현재 남은 runtime 작업은 migration 자체가 아니라 cleanup 검토다.

- legacy `/Users/geonha/lecture_stt` read-only inventory 후 결정
- repo-local stale tmp/log/archive 후보는 dry-run/list만 유지
- iCloud `01_audio` / `02_transcripts` bulk cleanup 후보는 broad deletion이므로 별도 exact target list + backup/rollback + 승인 필요

### 결정 질문

#### R1. runtime path migration을 지금 준비할까?

추천: `3`

1. read-only preflight + exact plan/rollback만 작성
2. plan 작성 후 승인하면 바로 적용까지 진행
3. canary 먼저 끝낸 뒤 migration
4. migration 보류

#### R2. 실제 적용 순서는?

추천: `1`

1. canary 먼저, 그 다음 migration
2. migration 먼저, 그 다음 canary
3. migration과 canary를 같은 작업 묶음으로 처리

#### R3. 이동 범위는?

추천: `1`

1. logs/tmp/cache만 repo 밖으로, DB는 repo 내부 유지
2. logs만 repo 밖으로
3. tmp/cache만 repo 밖으로
4. 전부 repo 안 유지

#### R4. launchd restart는?

추천: `1`

1. exact plan/rollback 보고 후 승인하면 label별 restart 허용
2. restart는 직접 하지 말고 명령만 안내
3. restart 금지

#### R5. legacy `/Users/geonha/lecture_stt`는?

추천: `1`

1. read-only inspect 후 나중에 결정
2. archive 후 delete 계획 작성
3. 계속 보존

#### R6. repo 안 empty runtime folders는?

추천: `1`

1. placeholder로 유지
2. 삭제 계획 작성
3. 문서화만 하고 그대로 둠

#### R7. stale `tmp/*.wav`는?

추천: `2`

1. archive 후 delete 계획만 작성, 실제 처리 전 재승인
2. read-only 목록만 생성
3. 건드리지 않음

---

## 6. L — Old logs archive/cleanup

### 남은 일

log rotation 기능은 구현되어 있다. 직전 dry-run 기준 launchd plain log는 threshold 10MB 이하라 rotate 필요가 없었다.

old logs cleanup은 별도 작업이다.

- compressed archive prune dry-run
- old logs manifest
- apply 여부 결정

### 결정 질문

#### L1. old logs cleanup을 지금 볼까?

추천: `1`

1. dry-run/manifest만 생성
2. dry-run 보고 후 승인하면 apply 가능
3. cleanup 보류

#### L2. compressed archive retention은?

추천: `1`

1. 30일 유지
2. 60일
3. 90일
4. 삭제/prune 금지

#### L3. plain launchd log rotation apply는?

추천: `3`

1. threshold 초과 시 자동 apply 허용
2. dry-run마다 승인 후 apply
3. 지금은 apply 안 함

#### L4. cleanup 결과 문서화는?

추천: `1`

1. `docs/WORKLOG.md`에 요약 기록
2. `docs/OPERATIONS.md`에 절차만 유지
3. 기록하지 않음

---

## 7. P — Runtime package update

### 남은 일

모델 교체는 하지 않는다. canonical STT는 `large-v3` 유지다.

package update workstream은 이미 명시 승인 후 적용되어 production `.venv`와 `requirements.txt`가 `faster-whisper==1.2.1`로 정렬됐다. 남은 package 관련 작업은 “추가 update”가 아니라 운영 관찰과 rollback 기준 유지다.

필요 단계:

1. 다음 실제 강의 canary에서 1.2.1 + `large-v3` 품질/안정성 확인
2. regression이 있으면 `faster-whisper==1.1.0` rollback 계획 수립
3. rollback은 package/config/launchd 영향이 있으므로 별도 승인 후 진행
4. targeted tests, compileall, DB integrity, launchd status, canary로 검증

### 결정 질문

#### P1. runtime package update를 지금 다룰까?

추천: `3`

1. live package 상태 read-only 확인만
2. package update plan/rollback까지 작성
3. 지금은 보류
4. 승인 후 실제 update까지 진행

#### P2. package update 범위는?

추천: `1`

1. 보안/버그픽스 목적의 runtime dependency만
2. `faster-whisper` 포함 가능, 단 모델 교체 없음
3. requirements 전체 최신화 검토
4. package update 금지

#### P3. `faster-whisper==1.2.1` 관련 작업은?

추천: `1`

1. 지금 안 함
2. isolated benchmark만 나중에
3. production `.venv` upgrade 후보로 검토

#### P4. update 후 canary 조건은?

추천: `1`

1. targeted tests + compileall + 다음 실제 강의 canary
2. tests만
3. 별도 샘플 `run_once`까지 포함

---

## 8. B — Benchmark 확대 / 모델 실험

### 남은 일

현재는 재개할 필요가 낮다.

현재 결론:

- `large-v3` 유지
- turbo/distil/Korean turbo 후보는 품질 기준 탈락
- `faster-whisper==1.2.1` production package alignment 적용 완료, 다음 실제 강의 canary 대기
- 긴 샘플 `260415LA`는 final candidate에만 실행

### 결정 질문

#### B1. benchmark 확대를 지금 재개할까?

추천: `1`

1. 지금 안 함
2. short+medium isolated benchmark만
3. 대표 샘플 전체 확대
4. 새 후보가 생길 때만

#### B2. raw benchmark artifact 저장은?

추천: `1`

1. local `state/benchmarks/` 허용
2. 저장 금지
3. metrics만 저장

#### B3. docs 기록 방식은?

추천: `1`

1. metric summary만, raw transcript text 금지
2. raw 일부 포함 허용
3. 문서 기록 안 함

#### B4. `260415LA` 긴 샘플은?

추천: `1`

1. final candidate에만 실행
2. benchmark 확대 시 포함
3. 실행 금지

---

## 9. O — 문서/작업 관리 방식

### 남은 일

사용자 결정사항을 계획 문서에 반영하고, 실제 작업을 하면 WORKLOG에 기록할 수 있다.

### 결정 질문

#### O1. 답변한 결정사항을 plan 문서에 반영할까?

추천: `1`

1. 반영한다
2. 채팅에만 남긴다
3. 실제 작업 착수할 때 반영한다

#### O2. 남은 업무 관리 방식은?

추천: `1`

1. canary/downstream/migration/package를 별도 workstream으로 쪼개 진행
2. 한 번에 큰 batch로 진행
3. canary만 하고 나머지는 보류

#### O3. commit 정책은?

추천: `1`

1. local commit 전 diff 요약 + 승인, push는 명시 요청 시만
2. local commit은 agent 판단으로 가능, push만 승인
3. commit/push 모두 금지

---

## 10. 추천 기본 답안

가장 보수적이고 안전한 기본값은 아래와 같다.

```text
C1=1, C2=1, C3=1, C4=1, C5=1
D1=1, D2=1, D3=1, D4=1, D5=3, D6=1, D7=1
R1=3, R2=1, R3=1, R4=1, R5=1, R6=1, R7=2
L1=1, L2=1, L3=3, L4=1
P1=3, P2=1, P3=1, P4=1
B1=1, B2=1, B3=1, B4=1
O1=1, O2=1, O3=1
```

이 기본값의 의미:

- 다음 실제 강의 canary를 먼저 기다린다.
- downstream은 read-only report/manual table까지만 진행한다.
- runtime path migration은 canary 이후로 미룬다.
- old log cleanup은 dry-run/manifest만 본다.
- package update와 benchmark 확대는 지금 보류한다.
- 사용자 결정사항은 plan 문서에 반영한다.
- commit 전에는 diff 요약과 명시 승인을 받는다.
- push/tag/release는 명시 요청 없이는 하지 않는다.

---

## 11. 답변 템플릿

아래 중 하나로 답하면 된다.

### 전체 추천 수락

```text
추천대로 진행.
```

### 일부만 변경

```text
추천대로 진행. 단, D5=1, R1=1, L1=3.
```

### 직접 전체 지정

```text
C1=1, C2=1, C3=1, C4=1, C5=1
D1=1, D2=1, D3=1, D4=1, D5=3, D6=1, D7=1
R1=3, R2=1, R3=1, R4=1, R5=1, R6=1, R7=2
L1=1, L2=1, L3=3, L4=1
P1=3, P2=1, P3=1, P4=1
B1=1, B2=1, B3=1, B4=1
O1=1, O2=1, O3=1
```

---

## 12. 승인 경계 재확인

아래 작업은 이 문서에 선택지가 있어도, 실제 실행 직전 다시 승인받아야 한다.

- DB row 삭제/수정
- source/destination 파일 삭제/이동/덮어쓰기
- runtime path migration 적용
- 운영 `config/config.yaml` 변경
- installed LaunchAgents 변경
- launchd restart
- old log cleanup/archive apply
- production `.venv` package update
- benchmark에서 새 모델 다운로드 또는 후보 모델 실행
- local commit
- remote push
- tag/release
