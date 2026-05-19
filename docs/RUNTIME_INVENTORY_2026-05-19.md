# Lecture STT Runtime Inventory

작성 시각: 2026-05-19 KST
범위: read-only inventory. 파일 삭제/이동/DB migration/launchd restart/model change 없음.

## 1. 경로/환경

`.env`/환경에서 확인한 path 변수:

- `LECTURE_RECORDINGS_ROOT=/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings`
- `GH_CURRENT_SEMESTER_ROOT=/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/GH_archive/01_TUK/01_current_semester`
- `OBSIDIAN_SEMESTER_ROOT=/Users/geonha/Library/Mobile Documents/iCloud~md~obsidian/Documents/99_obsidian/StudyVaults/2-1`

secret류 환경변수는 값 출력하지 않음.

## 2. iCloud lecture_recordings

Root:

- `/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings`
- exists: yes
- shallow dirs: 9
- shallow files: 1

확인된 주요 폴더:

| folder | exists | recursive files | size | 비고 |
|---|---:|---:|---:|---|
| `00_inbox` | yes | 1 | 6KB | `.DS_Store`만 있음. 현재 inbox 비어 있음 |
| `01_audio` | yes | 158 | 7.7GB | `.m4a` 147, `.qta` 10, `.DS_Store` 1 |
| `02_transcripts` | yes | 317 | 14.5MB | `.txt` 158 + `.json` 158 + `.DS_Store` 1 |
| `03_correction` | yes | 70 | 4.3MB | `.txt` 35 + `.json` 35 |
| `04_summarize` | yes | 28 | 169.2KB | `.md` 28 |
| `05_prompt` | yes | 9 | 43.6KB | glossary/prompt 자료 존재 |
| `99_errors` | yes | 1 | 6KB | `.DS_Store`만 있음 |

추가로 root에 `99_unused_audio`가 존재한다.

해석:

- 사용자가 말한 iCloud 폴더 구조와 실제 config/runtime이 일치한다.
- raw transcript는 이미 txt/json 병행으로 저장되고 있다.
- correction/summary는 raw transcript 전체 개수에 비해 일부만 존재하므로 현재 수동 후처리 흐름과 맞다.
- 현재 `00_inbox`는 비어 있어 작업/benchmark에는 안전한 시점이다.

## 3. repo runtime성 폴더

`/Users/geonha/DEV/lecture_stt` 기준:

| folder | exists | 현황 | 판단 |
|---|---:|---|---|
| `audio/` | yes | empty | 삭제/정리 후보 |
| `errors/` | yes | empty | 삭제/정리 후보 |
| `inbox/` | yes | empty | 삭제/정리 후보 |
| `logs/` | yes | `app.log`, `app.log.1`, 약 6MB | repo 밖 로그 이동 후보 |
| `models/` | yes | `.DS_Store`, empty `whisper/` | 삭제 또는 README placeholder 후보 |
| `state/` | yes | DB, locks, notified, logs | 현재 실제 운영 state. migration 필요 |
| `tmp/` | yes | stale wav 3개, 약 196MB; `inbox_staging` empty | cleanup 대상. 삭제는 승인 후 |
| `transcripts/` | yes | empty | 삭제/정리 후보 |

중요:

- `state/logs/downstream.jsonl`: 약 1.6GB
- `state/logs/downstream.out.log`: 약 588MB
- `state/logs` 총합: 약 2.2GB

해석:

- downstream 반복 conflict/blocked/error 로그가 계속 쌓이는 상태다.
- log rotation과 반복 이벤트 억제는 우선순위가 높다.
- 단, 로그 삭제/압축/truncate는 승인 후 진행한다.

## 4. legacy path

`/Users/geonha/lecture_stt`:

- exists: yes
- shallow: `.DS_Store`, `state/`
- `state/jobs.sqlite3`: 0B
- `state/logs/downstream.jsonl`: 약 4.6MB, 마지막 수정 2026-04-24

해석:

- 현재 running process는 `/Users/geonha/DEV/lecture_stt`를 사용한다.
- legacy path는 stale로 보이지만, archive 후 삭제가 안전하다.
- 즉시 삭제 금지. 승인 후 archive/delete.

## 5. launchd / process 상태

`launchctl list`에서 확인:

- `com.geonha.lecture-stt-cleanup`: loaded, pid 없음/exit 0 상태
- `com.geonha.lecture-stt-webpanel`: pid 2595
- `com.geonha.lecture-stt-distribute`: pid 2608
- `com.geonha.lecture-stt`: pid 2602

실행 process:

- `/Users/geonha/DEV/lecture_stt/.venv/bin/python -m lecture_stt.ui.web_panel`
- `/Users/geonha/DEV/lecture_stt/.venv/bin/python -m lecture_stt.stt.main --config /Users/geonha/DEV/lecture_stt/config/config.yaml`
- `/Users/geonha/DEV/lecture_stt/.venv/bin/python -m lecture_stt.downstream.worker --config /Users/geonha/DEV/lecture_stt/config/config.yaml`

launchd plist template은 `__REPO_ROOT__` placeholder 상태지만 설치된 실제 launchd job은 `/Users/geonha/DEV/lecture_stt`를 사용 중이다.

해석:

- 현재 운영 기준 source/runtime root는 `/Users/geonha/DEV/lecture_stt`다.
- Web panel은 이번 scope에서 제외하지만 프로세스는 계속 떠 있다.
- downstream worker도 계속 떠 있고, 현재 로그 증가의 직접 원인이다.

## 6. DB 상태

현재 DB:

- `/Users/geonha/DEV/lecture_stt/state/jobs.sqlite3`
- tables: `jobs`, `deliveries`, `sqlite_sequence`
- jobs status: `DONE` 29
- 최근 job은 모두 `DONE`, 마지막 처리 2026-05-04

`deliveries` 상태 요약:

- correction_status:
  - `DELIVERED`: 123
  - `CONFLICT`: 26
  - `ERROR`: 12
  - `MISSING`: 3
- summary_status:
  - `DELIVERED`: 101
  - `MISSING`: 33
  - `BLOCKED`: 26
  - `ERROR`: 4

해석:

- STT jobs 자체는 현재 안정적으로 DONE 상태다.
- downstream/correction/summary 배포 상태는 conflict/blocked/error가 꽤 많다.
- downstream worker가 이 상태를 반복 스캔하면서 `downstream.jsonl`/`downstream.out.log`를 계속 키우는 중이다.

## 7. 현재 코드/config에서 확인한 구조

`config/config.yaml`:

- `paths.watch_folder`: `${LECTURE_RECORDINGS_ROOT}/00_inbox`
- `paths.stable_audio_folder`: `${LECTURE_RECORDINGS_ROOT}/01_audio`
- `paths.transcript_folder`: `${LECTURE_RECORDINGS_ROOT}/02_transcripts`
- `paths.error_folder`: `${LECTURE_RECORDINGS_ROOT}/99_errors`
- `downstream.correction_folder`: `${LECTURE_RECORDINGS_ROOT}/03_correction`
- `downstream.summary_folder`: `${LECTURE_RECORDINGS_ROOT}/04_summarize`
- `paths.tmp_dir`: `tmp`
- `paths.db_path`: `state/jobs.sqlite3`
- `downstream.log_jsonl_path`: `state/logs/downstream.jsonl`
- `downstream.lock_path`: `state/downstream.lock`

Claude/Anthropic 제거 결과:

- `requirements.txt`: `anthropic` dependency 제거 완료
- `config/config.yaml`: Claude 전용 `correction.model`/`max_tokens` 제거 후 `correction.mode: manual` 적용
- `config/config.example.yaml`: Claude model/API key 예시 제거 후 manual/provider-neutral correction 문서화
- `src/lecture_stt/correction/corrector.py`: 외부 API 호출 구현 제거, disabled compatibility/helper 모듈로 전환
- `src/lecture_stt/correction/worker.py`: API key 요구 제거, pending transcript pair를 manual correction 대기 상태로 보고

## 8. 우선순위 제안

1. downstream 폭주 완화
   - 반복 conflict/blocked/error 로그 억제
   - log rotation
   - 필요하면 downstream을 일시 pause/disable하는 운영 옵션 추가
2. Claude/Anthropic 제거
   - correction 단계는 유지
   - Claude API worker/dependency/config 제거
   - manual/provider-neutral 상태로 문서화
3. runtime 경로 분리
   - DB/log/tmp/cache를 macOS 표준 위치로 이동하는 migration plan 작성
   - 기존 state/log/tmp는 archive 후 정리
4. 모델 benchmark
   - `docs/MODELS.md` 기준으로 package/model 후보 benchmark
   - benchmark script와 결과 artifact 생성

## 9. 금지/주의

- 현재 downstream 로그가 매우 크지만 삭제/압축/truncate는 승인 후 진행.
- `/Users/geonha/lecture_stt`는 stale로 보이나 archive 전 삭제 금지.
- `tmp/*.wav` 3개는 stale로 보이나 삭제는 승인 후 진행.
- launchd restart/unload/load는 별도 승인 후 진행.
