# Lecture STT Models

작성 시각: 2026-05-19 KST
목적: `lecture_stt`의 STT 모델/패키지 최신성, 후보 모델, benchmark 기준, rollback 기준을 기록한다.

## 1. 현재 운영 설정

`config/config.yaml` 기준:

```yaml
engine:
  engine: faster-whisper
transcribe:
  model_size: large-v3
  device: cpu
  compute_type: int8
  language: ko
  task: transcribe
  beam_size: 5
  vad_filter: true
  word_timestamps: false
  condition_on_previous_text: false
  keep_model_loaded: false
  repetition_penalty: 1.15
  no_repeat_ngram_size: 4
  vad_threshold: 0.55
  min_silence_duration_ms: 1200
  initial_prompt: 이 강의는 컴퓨터공학 전공 수업입니다.
```

현재 `large-v3` faster-whisper alias는 다음 repo를 가리킨다.

- `large-v3` → `Systran/faster-whisper-large-v3`
- local cached snapshot: `edaa852ec7e145841d8ffdb056a99866b5f0a478`

운영 머신 read-only 확인:

- CPU: Apple M4
- RAM: 32 GB
- 목표 운용 수준: Mac mini 상시 백그라운드 가능

## 2. 현재 설치 패키지와 최신성

2026-05-19 final hardening 이후 live 확인 결과:

| package | production installed | requirements.txt | 판단 |
|---|---:|---:|---|
| `faster-whisper` | 1.2.1 | 1.2.1 | 운영 적용 완료. model switch가 아니라 package-only 변경 |
| `ctranslate2` | 4.7.1 | transitive | 현재 운영에서 확인됨 |
| `huggingface-hub` | 1.4.1 | transitive/tooling | 현재 운영에서 확인됨 |
| `openai-whisper` | not installed | not listed | 현재 runtime 아님. 비교 후보로만 검토 |
| `transformers` | not installed | not listed | 현재 runtime 아님. raw OpenAI/Distil 후보 실험 시 필요 가능 |

주의:

- canonical STT model은 계속 `large-v3`다.
- `faster-whisper==1.2.1` 적용은 명시 승인된 runtime package workstream 결과이며, `transcribe.model_size` 변경이 아니다.
- 1.2 계열 VAD option signature 차이는 compatibility fix로 보정됐다.
- copied canary `260519OOP_2`는 package/VAD fix 이후 `DONE`을 확인했지만, 다음 실제 강의 canary gate는 별도로 남아 있다.
- 향후 package upgrade/downgrade 또는 rollback은 model switch와 분리하고, 계획/rollback/검증 후 별도 승인으로 진행한다.

## 3. faster-whisper alias 확인

기존 확인된 faster-whisper alias map 중 관련 항목. 운영 package는 현재 `faster-whisper==1.2.1`이지만 canonical alias 정책은 계속 `large-v3` 기준이다:

| alias | repo |
|---|---|
| `large-v3` | `Systran/faster-whisper-large-v3` |
| `large-v3-turbo` | `mobiuslabsgmbh/faster-whisper-large-v3-turbo` |
| `turbo` | `mobiuslabsgmbh/faster-whisper-large-v3-turbo` |
| `distil-large-v3` | `Systran/faster-distil-whisper-large-v3` |

## 4. 후보 모델 live 조회

2026-05-19 Hugging Face metadata 조회 결과:

| 후보 | repo / alias | runtime 호환성 | last modified | downloads | likes | 1차 판단 |
|---|---|---|---:|---:|---:|---|
| current baseline | `Systran/faster-whisper-large-v3` / `large-v3` | faster-whisper/CT2 | 2023-11-23 | 867,654 | 575 | 현행 baseline. 신뢰도 높음 |
| turbo CT2 alias | `mobiuslabsgmbh/faster-whisper-large-v3-turbo` / `large-v3-turbo` | faster-whisper/CT2 alias | 2025-11-05 | 813,737 | 54 | 1순위 benchmark 후보. 속도 개선 기대, 정확도 검증 필요 |
| turbo CT2 alt | `deepdml/faster-whisper-large-v3-turbo-ct2` | faster-whisper/CT2 | 2026-02-22 | 95,303 | 204 | 2순위 benchmark 후보. 태그/ASR metadata가 더 명시적 |
| official turbo | `openai/whisper-large-v3-turbo` | transformers/openai runtime | 2024-10-04 | 7,288,973 | 3,013 | 공식성/신뢰도 높음. 현재 pipeline 직접 호환 아님 |
| official large-v3 | `openai/whisper-large-v3` | transformers/openai runtime | 2024-08-12 | 4,818,342 | 5,705 | 기준 reference로만 검토 가능 |
| distil | `distil-whisper/distil-large-v3` | transformers 중심 | 2026-04-21 | 1,358,995 | 376 | 속도 후보. 한국어 강의 정확도 검증 전 운영 부적합 |
| MLX turbo | `mlx-community/whisper-large-v3-turbo` | MLX runtime | 2026-04-12 | 32,376 | 93 | Apple Silicon 최적화 후보. pipeline 변경 필요 |
| MLX large-v3 | `mlx-community/whisper-large-v3-mlx` | MLX runtime | 2026-04-12 | 143,360 | 81 | Apple Silicon reference 후보. pipeline 변경 필요 |

조회 실패/주의:

- `Systran/faster-whisper-large-v3-turbo`는 public repo로 확인되지 않았다. 현 alias는 `mobiuslabsgmbh/faster-whisper-large-v3-turbo`를 사용한다.

## 5. Korean fine-tune 후보

사용자 선호: 정확도가 좋으면 되지만 모델 신뢰도 문제 때문에 Whisper 계열을 활용하고 싶음.

따라서 추천 정책:

1. 운영 1차 후보는 Whisper 계열 중 신뢰도 높은 official/base 또는 CT2 변환본으로 제한한다.
2. Korean fine-tune은 “실험 후보”로만 둔다.
3. 다운로드/like가 낮은 fine-tune은 benchmark에서 유의미하게 좋아도 바로 운영 반영하지 않고 추가 샘플을 더 본다.

조회된 Korean 후보 예시:

| 후보 | repo | downloads | likes | 판단 |
|---|---|---:|---:|---|
| Korean turbo fine-tune | `ghost613/whisper-large-v3-turbo-korean` | 636 | 13 | raw transformers 계열. 실험 후보 |
| Korean turbo CT2 | `ghost613/faster-whisper-large-v3-turbo-korean` | 243 | 9 | faster-whisper 호환 가능성이 있어 실험 후보 |
| Zeroth KO v2 | `o0dimplz0o/Whisper-Large-v3-turbo-STT-Zeroth-KO-v2` | 823 | 11 | raw transformers 계열. 실험 후보 |
| medium Korean | `seastar105/whisper-medium-komixv2` | 1,571 | 6 | large-v3 대비 체급이 낮아 참고용 |

## 6. 추천 benchmark 순서

정확도 최우선 + Mac mini 상시 백그라운드 기준으로 다음 순서를 추천한다.

### Stage A. 같은 모델, package update 영향 분리

역사적 benchmark 기준:

1. 당시 운영 환경: `faster-whisper==1.1.0` + `large-v3`
2. 임시/분리 환경: `faster-whisper==1.2.1` + `large-v3`

현재 운영 상태:

- production `.venv`와 `requirements.txt`는 이후 명시 승인된 runtime package workstream에서 `faster-whisper==1.2.1`로 정렬됐다.
- 이 변경은 model switch가 아니며, canonical model은 계속 `large-v3`다.
- 향후 1.2.1 regression이 확인되면 rollback 후보는 `faster-whisper==1.1.0`이다.

목적:

- 모델이 아니라 runtime package만 바꿨을 때 결과/속도/안정성이 달라지는지 확인한다.

### Stage B. CT2/faster-whisper 호환 후보

1. `large-v3` baseline
2. `large-v3-turbo` alias → `mobiuslabsgmbh/faster-whisper-large-v3-turbo`
3. `deepdml/faster-whisper-large-v3-turbo-ct2`
4. `distil-large-v3` alias → `Systran/faster-distil-whisper-large-v3`
5. Korean CT2 fine-tune: `ghost613/faster-whisper-large-v3-turbo-korean`은 낮은 신뢰도 실험 후보로만 측정

목적:

- 현재 pipeline 변경 없이 넣을 수 있는 후보 중 정확도 regression이 없는지 확인한다.

### Stage C. Apple Silicon runtime 후보, 별도 spike

1. `mlx-community/whisper-large-v3-mlx`
2. `mlx-community/whisper-large-v3-turbo`

목적:

- Mac mini M4에서 CPU/int8보다 MLX가 정확도 유지 + 속도/전력 개선을 줄 수 있는지 확인한다.
- pipeline 변경량이 커서 바로 운영 교체하지 말고 spike로 분리한다.

## 7. Benchmark 샘플 후보

read-only 후보 추출 결과, 실제 audio는 147개, `01_audio` 크기는 약 7.7GB다.
`02_transcripts`는 `.txt` 158개 + `.json` 158개가 존재해 txt/json 병행 정책은 이미 잘 작동 중이다.

1차 benchmark 후보:

| stem | subject | duration | 기존 품질 | 기존 transcribe sec | path |
|---|---|---:|---|---:|---|
| `260504DS_1` | DS | 39.0m | warn: 반복 비율 높음 | 375.7 | `01_audio/260504DS_1.m4a` |
| `260504LC_2` | LC | 47.7m | warn: 반복 비율 높음 | 369.8 | `01_audio/260504LC_2.m4a` |
| `260430Dstr_2` | DStr | 47.6m | warn: 반복 비율 높음 | 525.3 | `01_audio/260430Dstr_2.m4a` |
| `260429LA` | LA | 23.3m | warn: 반복 비율 높음 | 269.7 | `01_audio/260429LA.m4a` |
| `260407OOP_1` | OOP | 41.6m | warn: 반복 비율 높음 | 2320.8 | `01_audio/260407OOP_1.m4a` |
| `260429Unix_2` | Unix | 45.0m | warn: 반복 비율 높음 | 922.4 | `01_audio/260429Unix_2.m4a` |
| `260312OOP_3` | OOP | 6.8m | warn: 반복 주의 | 228.6 | `01_audio/260312OOP_3.m4a` |
| `260415LA` | LA | 94.6m | warn: 반복 주의 | 2504.7 | `01_audio/260415LA.m4a` |

추천:

- 첫 benchmark는 전체 8개를 바로 돌리지 말고 `260312OOP_3` 짧은 파일 + `260504DS_1` 중간 파일로 smoke test.
- 그다음 6~8개 전체 비교.
- 최종 후보만 긴 파일 `260415LA`까지 돌린다.

## 8. Benchmark script 초안

초안 script:

- `scripts/benchmark_models.py`

안전장치:

- 기본 실행 plan은 현재 config의 baseline `large-v3`만 포함한다.
- `--include-doc-candidates` 또는 `--model large-v3-turbo`처럼 baseline 외 후보를 넣으면 `--allow-candidate` 없이는 종료한다.
- Hugging Face cache miss/download는 `--allow-download` 없이는 허용하지 않는다.
- 실제 benchmark 실행은 `--output`을 필수로 요구해 transcript text/segments가 터미널 stdout이나 수집 로그에 직접 출력되지 않게 한다.
- `--output` artifact에는 전사문과 segment text가 포함되므로 `state/benchmarks/` 같은 비공개 로컬 state 경로에 저장하고, 공유/커밋 전에는 내용을 확인한다.
- 따라서 후보 모델 다운로드/실행, package upgrade, 운영 config 교체는 별도 승인 전에는 하지 않는다.

side-effect 없는 plan 확인:

```bash
.venv/bin/python scripts/benchmark_models.py --config config/config.yaml --print-plan
```

baseline 실제 측정 예시. 실행 전 후보 파일과 시간대를 다시 확인한다.

```bash
.venv/bin/python scripts/benchmark_models.py \
  --config config/config.yaml \
  --audio "${LECTURE_RECORDINGS_ROOT}/01_audio/260312OOP_3.m4a" \
  --output state/benchmarks/large-v3-baseline.json
```

후보 모델 비교 예시. 실행 전 반드시 최신 package/model metadata를 live로 재확인하고 승인받는다.

```bash
.venv/bin/python scripts/benchmark_models.py \
  --config config/config.yaml \
  --include-doc-candidates \
  --allow-candidate \
  --allow-download \
  --audio "${LECTURE_RECORDINGS_ROOT}/01_audio/260312OOP_3.m4a" \
  --output state/benchmarks/model-candidates-smoke.json
```

초안 metric:

- load/transcribe seconds, real-time factor
- segment count, empty segment ratio
- timestamp regression count, zero/negative duration count
- transcript char/token count, 평균 segment 길이
- repeated unigram/bigram/trigram ratio

## 8.1 2026-05-19 실측 benchmark 결과

모든 측정은 canonical `02_transcripts`를 덮어쓰지 않고 `state/benchmarks/` 아래 shadow artifact로만 저장했다.
운영 모델/config 교체는 수행하지 않았다.

### Short sample: `260312OOP_3.m4a`

오디오 길이: 약 405.4초.

| runtime / model | transcribe sec | RTF | segments | chars | 판단 |
|---|---:|---:|---:|---:|---|
| faster-whisper 1.1.0 / `large-v3` | 192.93~195.80 | 0.476~0.483 | 77 | 2338 | 현재 baseline |
| faster-whisper 1.1.0 / `large-v3-turbo` | 82.25~82.54 | 0.203~0.204 | 81 | 2275 | 약 2.34배 빠르지만 OOP 전공 용어 오류가 커서 탈락 |
| faster-whisper 1.1.0 / `deepdml/faster-whisper-large-v3-turbo-ct2` | 81.73 | 0.202 | 81 | 2275 | turbo alias와 거의 같은 오류 패턴으로 탈락 |
| faster-whisper 1.1.0 / `distil-large-v3` | 133.22 | 0.329 | 127 | 2698 | 한국어 강의 출력 품질이 기준 미달이라 탈락 |
| faster-whisper 1.1.0 / `ghost613/faster-whisper-large-v3-turbo-korean` | 60.07 | 0.148 | 13 | 483 | 심한 누락으로 탈락 |
| isolated faster-whisper 1.2.1 / `large-v3` | 151.18 | 0.373 | 71 | 1742 | 같은 모델에서도 출력 축소/도입부 누락 징후가 있어 운영 upgrade 보류 |

`faster-whisper==1.2.1` isolated 결과는 `1.1.0 + large-v3` baseline보다 약 1.30배 빨랐다.
하지만 text similarity가 약 0.649에 그쳤고, transcript 길이가 596자 줄었으며, 도입부가 `컴퓨터공학 전공 수업입니다.`처럼 initial prompt에 가까운 문장으로 시작하는 차이가 확인됐다.
이 결과 때문에 당시에는 production 반영을 보류했지만, 이후 명시 승인된 runtime package workstream에서 production `.venv`와 `requirements.txt`를 `faster-whisper==1.2.1`로 정렬했다. 적용 후 1.2 계열 VAD signature 차이를 보정했고, copied canary `260519OOP_2`가 `DONE`이 된 것을 확인했다. 다만 canonical model은 계속 `large-v3`이며, 다음 실제 강의 canary gate는 별도로 남아 있다.

### Medium sample: `260504DS_1.m4a`

| model | transcribe sec | RTF | segments | chars | 판단 |
|---|---:|---:|---:|---:|---|
| `large-v3` | 392.47 | 0.168 | 212 | 4144 | baseline |
| `large-v3-turbo` | 156.83 | 0.067 | 151 | 3559 | 약 2.5배 빠르지만 도입부 누락/요약성 변형 및 hallucination성 문장 징후로 탈락 |

### 현재 운영 판단

- canonical STT 기본값은 `large-v3` 유지.
- `large-v3-turbo`와 CT2 turbo 계열은 빠른 draft 모드 후보로만 보류하고 기본 모델로 교체하지 않는다.
- `distil-large-v3`와 `ghost613` Korean turbo 후보는 현재 sample 기준 탈락.
- production `.venv`는 현재 `faster-whisper==1.2.1`로 정렬되어 있다. 이는 package-only 상태이며 model switch가 아니다.
- 1.2.1에서 실제 강의 품질 regression이 확인되면 `faster-whisper==1.1.0` rollback을 검토한다. rollback도 package/config/launchd 영향이 있으므로 계획/승인/검증 후 진행한다.

## 9. 평가 기준

정량/정성 혼합 기준:

- txt 전체 반복률, 중복 n-gram, 짧은 발화 비율, 빈 segment 비율
- JSON segment 수, 평균 segment 길이, timestamp 단조성
- 기존 correction/summary에 중요한 전공 용어 보존 여부
- 기존 raw transcript 대비 누락/반복/hallucination 변화
- 처리 시간, real-time factor, peak memory
- Mac mini 상시 백그라운드 가능성

운영 교체 기준:

- 기존 `large-v3`보다 명백한 정확도 regression이 없어야 한다.
- 반복 hallucination이 줄면 강한 가점.
- 전공 용어 오인식이 늘면 탈락.
- 처리 시간이 baseline보다 빨라도 정확도 악화 시 탈락.
- Mac mini에서 장시간 처리 중 메모리/발열/CPU 점유가 운영에 무리면 탈락.

## 10. 실사용 환경 모델 변경 검증 절차

모델 변경은 운영 `02_transcripts`를 바로 덮어쓰지 않고 다음 단계로 검증한다.

1. Baseline freeze
   - benchmark 직전 `faster-whisper`, `ctranslate2`, Python, macOS/host, current config를 다시 기록한다.
   - 현행 `large-v3` + 현재 transcribe config를 baseline으로 먼저 측정한다.
   - baseline output은 `state/benchmarks/<run_id>/baseline/`에 저장하고 canonical `02_transcripts`는 건드리지 않는다.

2. 실사용 sample set
   - 짧은 파일: `260312OOP_3` 같은 5~10분 파일로 smoke.
   - 중간 파일: `260504DS_1`, `260504LC_2` 같은 40~50분 파일.
   - 긴 파일: `260415LA` 같은 90분+ 파일은 최종 후보만 실행.
   - 과목별 용어 차이를 보기 위해 OOP/DS/LC/DStr/LA/Unix를 최소 1개 이상 포함한다.

3. Shadow benchmark
   - 후보 모델 산출물은 `state/benchmarks/<run_id>/<model_id>/` 아래 별도 txt/json으로만 저장한다.
   - downstream/correction/summary worker 입력 폴더로 자동 투입하지 않는다.
   - candidate 실행은 `--allow-candidate`와, cache miss가 예상되면 `--allow-download` 승인 후에만 한다.

4. 자동 metric
   - wall time, model load time, transcribe time, real-time factor.
   - segment count, empty segment ratio, 평균 segment 길이.
   - timestamp regression/zero-duration count.
   - repeated unigram/bigram/trigram ratio와 반복 hallucination 후보.
   - transcript char/token count 변화율.
   - 가능하면 peak RSS/CPU sample을 추가해 Mac mini 백그라운드 운용 가능성을 본다.

5. 사람 기준 품질판정
   - 기존 수동 correction txt가 있는 파일은 raw transcript와 correction 차이를 비교해 후보가 수동 수정량을 줄이는지 본다.
   - correction이 없는 파일은 과목별 3~5개 구간을 blind side-by-side로 비교한다.
   - 전공 용어, 숫자/기호, 한국어 조사/어미, 반복 hallucination, 누락, timestamp 유용성을 체크한다.
   - 처리 시간이 빨라도 수동 교정 부담이 늘면 탈락한다.

6. 제한 canary
   - offline/shadow 결과가 통과한 뒤에만 운영 config 변경을 검토한다.
   - canary는 사용자가 승인한 1~2개 신규 강의 또는 복제 sample로 시작한다.
   - canary 중에도 자동 overwrite 금지, txt/json 쌍 생성 여부와 downstream 호환성을 확인한다.
   - 문제가 보이면 즉시 `large-v3` rollback target으로 되돌린다.

7. 최종 채택 조건
   - baseline 대비 명백한 정확도 regression 없음.
   - 반복 hallucination 또는 수동 교정량이 감소.
   - timestamp/JSON schema/downstream compatibility 유지.
   - Mac mini 실사용 시간대에서 CPU/RAM/발열이 허용 가능.
   - 최소 짧은+중간+긴 sample에서 결과가 일관됨.

## 11. txt/json 산출물 정책

현재 txt + json 병행은 유지하는 것이 좋다.

추천 개선:

- `.txt`: 사람이 읽고 LLM에 바로 넣는 raw transcript.
- `.json`: segment, timestamp, model metadata, quality report, glossary/prompt metadata, benchmark metadata를 담는 canonical machine-readable artifact.
- 추가로 필요하면 `.md`를 raw human-readable layer로 만들 수 있지만, 현재 correction/summary와 혼동될 수 있어 당장은 비추천.
- 향후 LLM 교정에는 txt 본문만 던지기보다 json의 segment/time/quality metadata와 과목별 glossary를 함께 사용하도록 prompt builder를 개선한다.

## 12. Rollback 기준

모델 변경 후 다음 중 하나라도 발생하면 즉시 baseline으로 되돌린다.

- 특정 과목에서 기존보다 전공 용어 인식이 눈에 띄게 악화
- 반복 hallucination 증가
- transcript JSON 구조 손상 또는 downstream/correction 입력 호환성 깨짐
- Mac mini 상시 백그라운드 운용 중 과도한 RAM/CPU/발열
- 2개 이상 대표 샘플에서 수동 교정 부담 증가

기본 rollback target:

```yaml
transcribe:
  model_size: large-v3
  device: cpu
  compute_type: int8
```

Package rollback target:

```text
faster-whisper==1.1.0
```

Package rollback은 production `.venv` 변경, `requirements.txt` 변경, launchd restart가 필요할 수 있으므로 실행 전 별도 계획/rollback/검증 보고와 명시 승인을 받는다. 적용 후에는 targeted tests, compileall, DB integrity check, launchd 상태 확인, 다음 실제 강의 canary로 검증한다.
