# lecture_stt 구조 정리 및 모델 고도화 Handoff

작성 시각: 2026-05-19 12:48:09 KST
작성 목적: `lecture_stt` 장기 고도화 작업을 세션 간 이어가기 위한 현황, 사용자 결정사항, 다음 질문, 실행 계획 기록

## 0. 현재 결론 v2

이번 작업은 단순 모델 교체가 아니라 다음 5개 축의 장기 작업이다.

1. `/Users/geonha/DEV/lecture_stt`와 `/Users/geonha/lecture_stt` 중복 경로 정리
2. source repo, runtime state, logs, temp/cache, iCloud lecture artifacts의 책임 분리
3. Claude/Anthropic API 구현 제거
4. downstream/log/retry/failure-reporting 안정화
5. 최신 STT 모델/패키지 버전 live 확인 후 정확도 우선 benchmark 및 교체 판단

사용자 답변으로 확정된 방향:

- 녹음 생성: iPhone 녹음
- 사용자 업로드 위치: iCloud `lecture_recordings/00_inbox`
- 자동화 경계: 현재는 STT 전사까지만 자동화한다.
- 현재 흐름: `00_inbox` 감지 → 업로드 안정화 확인 → audio 폴더로 이동 → STT → 이후 교정/요약은 사용자가 LLM에 수동 명령해서 생성
- raw transcript와 corrected transcript는 둘 다 유지한다.
- Claude는 제거하지만 correction/summary 단계 자체는 유지한다. 다른 LLM을 사용할 예정이고 추후 자동화할 수 있다.
- summary는 계속 필요하다.
- Telegram/Discord 알림은 유지한다. 추후 기존 봇 연동 등 고도화 가능성이 있다.
- Web panel은 개편 대상이지만 이번 작업에서는 제외한다.
- 실패 파일은 재시도 후 실패 시 에러 파일로 이동하고, 로그를 남기며 보고한다.
- 모델 교체 우선순위는 정확도 최우선, 단 사용 중인 컴퓨터가 감당 가능한 범위여야 한다.

따라서 1차 구조 개편의 핵심은 다음과 같다.

- correction/summary 개념을 삭제하지 않는다.
- Claude/Anthropic에 고정된 구현과 설정만 제거하거나 provider-neutral/manual 단계로 바꾼다.
- STT 자동화는 raw transcript 생성까지를 안정화한다.
- downstream은 “자동 교정/요약 생성기”가 아니라, 현재 수동 산출물을 다루는 보조/배포/상태 계층인지 재정의해야 한다.
- 모델 최신성 확인은 필수 gate로 둔다. 실제 benchmark 전에 Hugging Face/GitHub/PyPI 등에서 후보 모델과 패키지 최신 버전을 live 조회하고 `docs/MODELS.md`에 기록한다.

삭제, launchd 재시작, runtime state 이동, DB migration, 모델 교체는 모두 side effect가 크므로 사용자 승인 후 진행한다.

## 1. “canonical output” 설명과 현재 추천

canonical output은 “여러 복사본/동기화본/내보내기본 중 어느 위치를 원본(source of truth)으로 볼 것인가”라는 뜻이다.

예시:

- iCloud `lecture_recordings`에 raw/corrected/summary가 있고 Obsidian에도 복사되어 있다면, 둘 중 어느 쪽이 최종본인지 정해야 한다.
- GH_archive가 단순 백업/게시용이면 iCloud가 canonical이다.
- Obsidian에서 사용자가 수동 수정하는 노트가 최종본이면 Obsidian이 canonical이다.
- canonical이 정해져야 overwrite, conflict, 재배포 정책을 안전하게 만들 수 있다.

현재 추천:

- source code canonical: `/Users/geonha/DEV/lecture_stt`
- lecture artifact canonical: 우선 iCloud `lecture_recordings`로 둔다.
- GH_archive/Obsidian: 사용자가 최종 수정 위치라고 확정하기 전까지는 derived/export copy로 취급한다.
- runtime state canonical: repo 내부가 아니라 `~/Library/Application Support/lecture-stt/` 같은 macOS 표준 위치로 분리하는 방향을 추천한다.
- logs canonical: `~/Library/Logs/lecture-stt/`
- tmp/cache canonical: `~/Library/Caches/lecture-stt/`

아직 확인 필요:

- GH_archive와 Obsidian이 현재 실제로 어떤 용도인지
- 수동 교정/요약 결과가 어느 위치에서 최종 수정되는지
- iCloud와 Obsidian/GH_archive 간 충돌 시 어느 쪽을 우선할지

## 2. 확인된 workflow v2

### 2.1 입력

1. 사용자가 iPhone으로 강의를 녹음한다.
2. 사용자가 iCloud Drive의 `lecture_recordings/00_inbox`에 오디오 파일을 업로드한다.
3. worker는 `00_inbox`를 감시한다.
4. 업로드가 완료되어 파일 크기/mtime이 안정화되었다고 판단되면 파일을 audio 보관 위치로 이동한다.

정확한 audio 보관 폴더명은 추가 확인 필요. 기존 문서상 `01_audio`일 가능성이 높다.

### 2.2 자동 STT

1. audio 보관 위치에 들어온 파일을 STT 대상으로 삼는다.
2. 현재 사용 모델은 faster-whisper 계열 `large-v3`, CPU/int8, Korean language 설정으로 파악되어 있다.
3. STT 결과로 raw transcript를 생성한다.
4. 자동화는 현재 raw transcript 생성까지가 목표다.

### 2.3 수동 LLM 후처리

1. 사용자가 raw transcript를 바탕으로 LLM에 수동 명령한다.
2. 수동으로 corrected transcript를 만든다.
3. 수동으로 summary를 만든다.
4. raw/corrected/summary는 모두 유지한다.
5. 추후 이 단계도 다른 LLM/provider로 자동화할 수 있다.

중요한 설계 결론:

- Claude 제거는 “후처리 단계 삭제”가 아니다.
- correction/summary 폴더, 상태값, 문서 개념은 유지해야 한다.
- 다만 Claude/Anthropic API key, dependency, worker, config는 제거 또는 neutral interface로 교체한다.

### 2.4 알림

- Telegram/Discord 알림은 유지한다.
- 장기적으로 기존 봇과 연동할 수 있다.
- 이번 작업에서는 알림의 noisy progress chatter를 줄이고 완료/실패/주의 보고 중심으로 정리하는 것이 적절하다.

### 2.5 실패 처리

요구사항:

- 재시도한다.
- 최종 실패 시 에러 파일로 이동한다.
- 로그를 남긴다.
- 사용자에게 보고한다.

추가 확인 필요:

- 재시도 횟수
- 원본 오디오를 `01_audio`에 남겨둘지, `99_errors`로 이동할지
- 실패 시 raw partial transcript가 있으면 보존할지
- 실패 보고를 Telegram/Discord 둘 다 보낼지

### 2.6 Web panel

- Web panel은 이번 작업 범위에서 제외한다.
- 다만 기존 Web panel이 깨지지 않도록 회귀는 피한다.
- Web panel 리디자인/개편은 별도 후순위 작업으로 분리한다.

## 3. 권장 target structure 초안

아직 실제 파일 이동은 하지 않는다. 먼저 문서/설정/audit로 확정한다.

### 3.1 source repo

```text
/Users/geonha/DEV/lecture_stt/
  AGENTS.md
  README.md
  config/
  docs/
  frontend/
  launchd/
  scripts/
  src/
  tests/
```

repo root는 가능하면 source-only로 만든다.

repo root에서 분리 또는 삭제 후보:

```text
audio/
errors/
inbox/
logs/
models/
state/
tmp/
transcripts/
```

### 3.2 lecture artifacts, iCloud

현재/권장 초안:

```text
~/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/
  00_inbox/          # 사용자가 업로드하는 유일한 입력 지점
  01_audio/          # STT 대상/원본 오디오 보관
  02_transcripts/    # raw transcript
  03_correction/     # manual or future LLM corrected transcript
  04_summarize/      # manual or future LLM summary
  99_errors/         # 최종 실패 파일/진단 산출물
```

주의:

- 위 폴더명은 기존 코드/문서 추정과 사용자 답변을 조합한 초안이다.
- 실제 iCloud 폴더명/현재 config가 맞는지 read-only audit 후 확정한다.
- 폴더 rename/move/delete는 승인 전 금지한다.

### 3.3 runtime state/log/cache

추천:

```text
~/Library/Application Support/lecture-stt/   # sqlite DB, lock, durable state
~/Library/Logs/lecture-stt/                  # app/downstream/launchd logs
~/Library/Caches/lecture-stt/                # tmp wav, model scratch, transient cache
~/.cache/huggingface/hub/                    # HF model snapshots, 기본 유지 가능
```

## 4. 결정사항 갱신

### D1. source/runtime 경로 정책

추천 유지:

- source: `/Users/geonha/DEV/lecture_stt`
- lecture artifacts: iCloud `lecture_recordings`
- state/log/tmp/cache: macOS 표준 위치로 분리

아직 결정 필요:

- `/Users/geonha/lecture_stt`를 archive 후 삭제할지, runtime 전용으로 남길지

### D2. `/Users/geonha/lecture_stt` 처리

추천 유지:

1. 먼저 archive 생성
2. launchd/config/process가 참조하지 않는지 확인
3. DB/log/state가 중복 또는 stale인지 확인
4. 문제 없으면 삭제

### D3. repo root runtime성 폴더 처리

추천 유지:

- repo root는 source/config/docs/scripts/tests/frontend/launchd 중심으로 정리
- runtime 산출물은 repo 밖으로 이동
- 단 실제 이동/삭제는 승인 후 진행

### D4. `models/` 폴더 정책

추천 유지:

- repo 내부 `models/`는 삭제하거나 README placeholder만 둔다.
- 실제 모델 cache는 Hugging Face cache 또는 별도 명시 cache를 사용한다.
- `docs/MODELS.md`에 model id, package version, snapshot/revision, benchmark 결과, rollback 기준을 기록한다.

### D5. Claude/Anthropic 제거 범위

사용자 답변 반영 후 추천 확정:

- A안으로 진행: Claude/Anthropic API 구현만 제거한다.
- correction/summary 단계는 유지한다.
- future LLM 자동화를 고려해 provider-neutral/manual 상태를 문서화한다.

제거 후보:

- `anthropic` dependency
- `ANTHROPIC_API_KEY` 설정/문서
- Claude API 호출 worker
- Claude 전용 model/config 필드

보존 후보:

- `03_correction` 산출물 개념
- corrected transcript 상태값
- summary 산출물 개념
- 수동/외부 LLM 산출물 감지 또는 배포 구조

### D6. summary/downstream 워크플로우

사용자 답변 반영:

- summary는 계속 필요하다.
- raw transcript와 corrected transcript는 모두 유지한다.
- 현재 자동화 범위는 STT까지만이다.

아직 결정 필요:

- downstream이 지금 raw transcript만 배포해야 하는지
- corrected/summary가 수동으로 생성된 뒤 downstream이 이를 감지/배포해야 하는지
- GH_archive/Obsidian으로 복사하는 경우 어느 쪽이 canonical인지
- summary가 correction을 기다려야 하는지, 아니면 raw 기반 summary도 허용할지

### D7. 최신 모델 교체 기준

사용자 답변 반영:

- 정확도 최우선
- 사용 중인 컴퓨터의 수용 가능성은 필수 제약

추천 benchmark 기준:

- 기존 `large-v3` baseline보다 한국어 강의 정확도가 악화되면 탈락
- 전공 용어 오인식이 늘면 탈락 또는 보류
- 반복 hallucination/짧은 발화 노이즈가 줄면 가점
- 속도는 2차 지표지만, 처리 시간이 실사용 불가능하면 탈락
- RAM/발열/CPU 점유율이 상시 운용에 무리면 탈락
- 5~8개 대표 샘플에서 치명적 regression이 없어야 함

### D8. 모델 benchmark 샘플

아직 결정 필요:

- 어떤 실제 강의 파일을 benchmark에 쓸지
- 품질 warn이 났던 파일을 샘플에 포함할지
- 전공 용어가 많은 과목을 어떤 파일로 대표할지

추천:

- 짧은 강의 1개
- 긴 강의 1개
- quality warn 파일 2개
- 전공 용어 많은 과목별 샘플 2~4개
- 총 5~8개

### D9. launchd/운영 중단

아직 결정 필요:

- 작업 중 STT/downstream launchd 재시작 허용 시간대
- inbox가 비어 있을 때만 작업할지
- DB/log 백업 후 state migration 진행 가능 여부

### D10. 로그 보존 정책

추천:

- app/downstream/launchd 로그 모두 rotation
- 30일 보존 또는 크기 기준 보존
- problem row 자체는 DB/status로 보고, 반복 JSONL은 억제

### D11. 테스트 체계

추천:

- 당장은 기존 unittest 유지
- 장기적으로 `requirements-dev.txt` 또는 `pyproject.toml`로 dev tools 명시
- `scripts/check.sh` 추가
- 모델 benchmark는 별도 script와 결과 artifact로 관리

### D12. branch/commit 운영

현재 branch:

- `checkpoint/pre-web-panel-redesign`

현재 미커밋 변경:

- downstream 로그 suppression/JSONL rotation/status summary 관련 코드와 테스트
- STT launchd lock wait 관련 코드와 테스트
- 모델 benchmark script와 benchmark 테스트
- runtime/model 문서: `docs/HANDOFF_RESTRUCTURE_AND_MODEL_UPGRADE.md`, `docs/RUNTIME_INVENTORY_2026-05-19.md`, `docs/MODELS.md`, `docs/WORKLOG.md`, `docs/ARCHITECTURE.md`
- 예시 설정: `config/config.example.yaml`

추천:

- 기존 미커밋 변경을 먼저 검토 후 별도 commit
- 이후 큰 작업은 분리
  - `cleanup/runtime-structure`
  - `remove/claude-correction`
  - `bench/stt-models`

## 5. 남은 질문

아래 질문에 답하면 바로 실행 계획을 더 구체화할 수 있다.

### A. output/canonical 관련

1. 현재 iCloud `lecture_recordings` 하위 실제 폴더명은 아래와 맞아?
   - `00_inbox`
   - `01_audio`
   - `02_transcripts`
   - `03_correction`
   - `04_summarize`
   - `99_errors`

2. raw transcript 파일은 정확히 어느 폴더에 저장되는 게 맞아?
   - 예: `02_transcripts`
   - 파일 형식: `.txt`, `.md`, `.json`, `.srt`, 기타?

3. 수동 교정본과 요약본은 어디에 저장할 예정이야?
   - iCloud의 `03_correction`/`04_summarize`?
   - Obsidian?
   - GH_archive?
   - 아직 매번 수동으로 다른 위치?

4. Obsidian/GH_archive는 현재 어떤 역할이야?
   - 단순 백업/검색용 export
   - 최종 공부 노트 위치
   - 배포/버전관리 위치
   - 아직 미정

5. iCloud와 Obsidian/GH_archive에 같은 파일이 있을 때, 사용자가 나중에 수정하는 최종본은 보통 어디야?

### B. downstream 관련

6. 이번 작업에서 downstream worker는 계속 켜둘까, 아니면 STT까지만 안정화할 동안 잠시 비활성화하는 방향이 좋아?

7. raw transcript가 생성되면 downstream이 자동으로 어딘가에 복사/배포해야 해?
   - 아니면 raw transcript를 iCloud에 저장하는 것까지만 하면 돼?

8. 수동 correction/summary 파일이 생겼을 때 downstream이 이를 감지해서 상태/알림/배포를 해주면 좋을까?

### C. 모델 benchmark 관련

9. benchmark에 실제 강의 오디오를 사용해도 돼? 사용 가능하다면 내가 read-only로 후보 파일 목록을 뽑아도 될까?

10. 모델 후보는 faster-whisper/ctranslate2 계열 안에서만 비교할까, 아니면 아래도 후보에 넣어도 될까?
    - `mlx-whisper`/MLX 계열
    - `whisper.cpp`/Core ML 계열
    - OpenAI whisper 원본 구현
    - Korean fine-tuned Whisper 계열

11. “사용 중인 컴퓨터의 수용 가능성” 기준은 어느 쪽이 중요해?
    - Mac mini에서 상시 백그라운드 운용 가능
    - 노트북에서도 가끔 돌릴 수 있어야 함
    - 속도는 느려도 정확하면 됨
    - 발열/전력/팬소음도 중요

12. 모델 평가 시 timestamp/segment 품질도 중요해?
    - 텍스트 정확도만 중요
    - 문장 단위 끊김도 중요
    - 타임스탬프도 중요
    - 나중에 요약/교정에 쓸 정도면 충분

### D. 실패/보존 정책

13. 실패 재시도 횟수는 몇 번이 좋아?
    - 추천: 2회 재시도 후 `99_errors`

14. 최종 실패 시 원본 오디오는 `01_audio`에 남기고 진단 복사본만 `99_errors`에 둘까, 아니면 원본 자체를 `99_errors`로 이동할까?

15. 원본 오디오 장기 보관은 이번 작업에서 정책만 문서화하고 실제 압축/이동은 나중에 해도 될까?

## 6. 갱신된 실행 계획

### Phase 0. 안전장치와 read-only inventory

- [ ] `git status --short --branch` 확인
- [ ] 기존 미커밋 변경 diff 확인
- [ ] launchd plist/config가 참조하는 실제 경로 확인
- [ ] iCloud `lecture_recordings` 하위 폴더 구조 read-only 확인
- [ ] `/Users/geonha/DEV/lecture_stt`와 `/Users/geonha/lecture_stt` 비교
- [ ] DB/log/state 위치와 크기 확인
- [ ] 삭제/이동 후보는 표로만 작성

### Phase 1. workflow/docs/config 정합성 정리

- [ ] README/ARCHITECTURE에 workflow v2 반영
- [ ] config.example의 path 설명 갱신
- [ ] `docs/RUNTIME.md` 또는 `docs/OPERATIONS.md` 작성 검토
- [ ] `docs/MODELS.md` 작성
- [ ] Web panel은 이번 scope out이라고 명시

### Phase 2. 모델 최신성 live 확인 및 benchmark 설계

- [ ] 현재 faster-whisper/ctranslate2/openai-whisper 등 package version 확인
- [ ] Hugging Face/GitHub/PyPI에서 후보 모델/패키지 최신 버전 live 조회
- [ ] 후보군을 `docs/MODELS.md`에 기록
- [ ] benchmark 샘플 선정
- [ ] benchmark script 작성
- [ ] current `large-v3` baseline 측정
- [ ] 후보 모델 측정
- [ ] 정확도/반복/전공용어/시간/RAM 기준으로 교체 판단

### Phase 3. Claude/Anthropic 제거

- [x] `anthropic`, `claude`, `ANTHROPIC_API_KEY` 참조 검색
- [x] Claude API 호출부 제거 또는 neutral manual/provider abstraction으로 교체
- [x] correction/summary 산출물 개념은 유지
- [x] config/docs/tests 갱신
- [x] Python unittest 실행

### Phase 4. retry/failure/log 안정화

- [ ] retry 정책 확정
- [ ] 최종 실패 시 `99_errors` 처리 구현/검증
- [ ] failure report 메시지 정리
- [ ] log rotation 정책 구현
- [ ] 반복 problem JSONL 억제 보강

### Phase 5. runtime 구조 정리

- [ ] state/log/tmp/cache target path 확정
- [ ] migration plan 작성
- [ ] archive plan 작성
- [ ] 사용자 승인 후 실제 이동/삭제
- [ ] launchd 재시작은 승인 후 진행

### Phase 6. 운영 반영

- [ ] 선택 모델 config 반영은 사용자 승인 후 진행
- [ ] 모델 pre-download/cache 확인
- [ ] run_once 검증
- [ ] 실제 강의 1~2건 관찰
- [ ] rollback 절차 문서화

## 7. 금지/주의사항

- 사용자 승인 없이 `/Users/geonha/lecture_stt` 삭제 금지
- 사용자 승인 없이 iCloud `lecture_recordings` 내부 파일 삭제/이동 금지
- 사용자 승인 없이 launchd unload/load/restart 금지
- 사용자 승인 없이 DB migration 또는 row 삭제 금지
- 사용자 승인 없이 모델 교체 config 반영 금지
- API key/token/secret 값 출력 금지
- 기존 미커밋 변경 덮어쓰기 금지
- Web panel 개편은 이번 작업 범위에서 제외

## 8. 다음 세션 시작 시 바로 볼 것

파일:

- `docs/HANDOFF_RESTRUCTURE_AND_MODEL_UPGRADE.md`
- `AGENTS.md`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`
- `config/config.yaml`
- `config/config.example.yaml`
- `requirements.txt`
- `src/lecture_stt/stt/transcribe.py`
- `src/lecture_stt/stt/main.py`
- `src/lecture_stt/downstream/lib.py`
- `src/lecture_stt/downstream/status.py`
- `launchd/*.plist`

명령:

```bash
cd /Users/geonha/DEV/lecture_stt
git status --short --branch
git diff --stat
```

첫 질문:

- 남은 질문 A~D 중 무엇이 확정됐는가?
- read-only inventory와 모델 최신성 live 조회를 시작해도 되는가?
- 삭제/이동/launchd 재시작/DB migration 승인 범위가 있는가?

## 9. 진행 로그

### 2026-05-19

- 사용자 답변을 반영해 workflow v2를 기록했다.
- iPhone → iCloud `lecture_recordings/00_inbox` → STT → 수동 LLM correction/summary 흐름으로 정리했다.
- Claude 제거 범위는 correction 단계 삭제가 아니라 Claude/Anthropic API 구현 제거로 조정했다.
- summary와 raw/corrected transcript 보존 요구를 반영했다.
- Web panel은 이번 작업에서 제외했다.
- 모델 benchmark 기준을 정확도 최우선 + 컴퓨터 수용 가능성 제약으로 갱신했다.
- canonical output 의미와 현재 추천을 문서화했다.
- 추가 사용자 답변을 반영했다: iCloud `00_inbox/01_audio/02_transcripts/03_correction/04_summarize/99_errors` 구조가 맞고, txt/json 병행 유지, GH_archive는 모든 파일 저장소/작업 후 이동 용도, Mac mini 상시 백그라운드 수준을 모델 수용 기준으로 삼는다.
- read-only runtime inventory를 수행하고 `docs/RUNTIME_INVENTORY_2026-05-19.md`에 저장했다.
- 모델/패키지 최신성 live 조회를 수행하고 `docs/MODELS.md`에 저장했다.
- 추천 방향을 확정했다: txt/json은 유지하되 JSON을 machine-readable canonical artifact로 강화하고, downstream은 수동 correction/summary 산출물을 감지·검증·배포하는 보조 계층으로 유지하되 반복 로그 폭주부터 완화한다.
- retry 기본안은 사용자 승인대로 2회 재시도 후 `99_errors` 이동으로 둔다. 실제 구현 전에는 원본 이동/복사 정책을 코드 기준으로 다시 검증한다.
- downstream 운영 로그는 gzip archive 후 truncate했고, 승인된 config 적용 및 launchd 재시작 후 stdout 반복 폭주는 멈춘 상태다.
- Claude/Anthropic API-backed correction 구현과 dependency/config coupling을 제거하고, correction worker를 manual/provider-neutral pending reporter로 전환했다.
- downstream problem row 41건을 read-only로 분류해 `docs/DOWNSTREAM_TRIAGE_2026-05-19.md`에 기록했다. conflict는 overwrite 없이 수동 canonical 결정 대상으로 남겨 두었다.
- 모델 benchmark 결과 canonical 기본값은 `large-v3` 유지로 판단했다. turbo/distil/Korean turbo 후보는 탈락했고, `faster-whisper==1.2.1`은 isolated 환경에서만 보류 후보로 남긴다.
