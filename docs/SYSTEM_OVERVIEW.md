# 강의 녹음이 학습 노트가 되기까지

Lecture STT는 iCloud Drive에 넣은 녹음을 Mac mini에서 한국어 텍스트로 바꾸고, 원음을 다시 찾을 수 있도록 발화 구간별 시간을 함께 저장한다. 강의로 분류된 전사는 교정·요약을 거쳐 과목별 Obsidian 폴더로 전달할 수 있다. 아래 자동 처리 흐름은 [운영 코드 README](https://github.com/altius03/lecture-stt-pipeline/blob/main/README.md)을 기준으로 한다.

## 녹음 한 건의 처리 흐름

| 단계 | 시스템이 하는 일 | 확인할 결과 |
| --- | --- | --- |
| 접수 | `00_inbox`를 감시하고 파일 크기 등이 안정된 뒤 작업을 등록한다. 오디오는 `01_audio`에 보관한다. | SQLite 작업 ID, 원래 파일명, 오디오 SHA-256 |
| 전사 | Mac mini의 faster-whisper가 음성을 인식한다. 설정 예시는 한국어와 `large-v3` 모델을 사용한다. | `02_transcripts`의 읽기용 TXT와 구간 ID·시작·종료 시각이 담긴 JSON |
| 품질 검사 | 전사량, 반복 지표, 빈 구간, 마지막으로 인식된 시각 등을 검사한다. | 작업 상태와 별도의 `.quality.json` 품질 기록 |
| 교정·요약 | 전사 상태가 `DONE`이고 활성 학기·과목 및 처리 시작 시점 조건을 충족하면 별도 큐에 등록한다. 워커는 전사 텍스트를 Codex CLI를 통해 Codex 서비스로 보내 교정·요약한다. | `03_correction`의 교정 TXT·JSON, `04_summarize`의 요약 Markdown |
| 전달 | 확정된 과목 경로에 결과물을 복사한다. 기존 파일과 충돌하면 덮어쓰기를 막는다. | 과목별 `06_lecture_notes/02_origin` 교정본과 `06_lecture_notes/01_summarize` 요약본 |

macOS에서는 launchd가 워커를 실행한다. 실행 주체는 설정에 따라 Python 감시기 또는 Go 컨트롤러가 맡는다. 운영 웹 패널은 작업 상태를 보여 주고, SQLite는 전사 작업과 후처리 진행 상태를 따로 기록한다. [아키텍처](https://github.com/altius03/lecture-stt-pipeline/blob/main/docs/ARCHITECTURE.md) · [설정 예시](https://github.com/altius03/lecture-stt-pipeline/blob/main/config/config.example.yaml)

## 완료 여부를 판단하는 방법

- **전사가 끝났는가?** 해당 작업 ID의 상태와 실제 TXT·JSON을 함께 확인한다. `DONE`이어도 품질 경고가 남을 수 있다. `NEEDS_REVIEW`는 자동 교정으로 넘어가기 전에 검토가 필요한 상태다.
- **녹음 중간이 빠졌는가?** `coverage_ratio`는 마지막 인식 시각을 전체 녹음 길이로 나눈 값이다. 예를 들어 60분 녹음에서 마지막 발화가 59분에 잡히면 약 98%가 나온다. 이 값만으로 중간 10분에 발화가 있었는지는 알 수 없다. 연속한 두 구간의 종료·시작 시각 차이를 구해 의심 구간을 찾고 원음과 대조한다. [품질 지표 계산 코드](https://github.com/altius03/lecture-stt-pipeline/blob/main/src/lecture_stt/stt/quality_gate.py)
- **교정과 전달도 끝났는가?** 원래 작업 ID에 연결된 `transcript_postprocess_jobs`에서 교정·요약·전달 상태를 각각 확인한다. 과목을 연결하지 못하면 전사만 남을 수 있다. 교정 결과는 원전사와 구간 ID·개수·순서를 대조하고 원래 시간 정보를 보존한다. [교정 결과 검증 코드](https://github.com/altius03/lecture-stt-pipeline/blob/main/src/lecture_stt/downstream/postprocess.py) · [운영 절차와 상태 조회 명령](https://github.com/altius03/lecture-stt-pipeline/blob/main/docs/OPERATIONS.md)

## 대량 녹음을 정리한 사례

2026년 11월에는 약 42시간 분량의 녹음 67개를 처리했다. 다음은 기존 전사 워커에 녹음을 접수하고, 그 결과를 실제 학습 자료로 정리하면서 별도로 수행한 작업이다.

1. **원본과 업로드 사본을 대조했다.** 음성 메모에서 녹음을 내보내고 AAC 스트림을 재인코딩 없이 추출했다. 오디오 데이터와 전송 파일의 해시를 확인하고, 이미 전사한 녹음은 중복 접수에서 제외했다.
2. **녹음 시각과 내용을 함께 보고 이름을 붙였다.** 시간표상 강의와 겹친 외부 행사는 전사 내용을 보고 따로 분류했다. 강의에는 날짜와 과목 약칭을 붙이고, 같은 수업을 나눠 녹음한 경우 시작 시각별 폴더로 구분했다.
3. **인식량이 적은 녹음은 사본으로 재시도했다.** 한 녹음은 약 45분 중 처음 5분 남짓만 인식됐다. 음량을 보정해 다시 전사한 결과 마지막 인식 지점이 녹음 끝 부근까지 늘어났다. 첫 결과와 보정 결과를 모두 보존했다.
4. **학습용 보완과 발언 기록을 구분했다.** 교안과 앞뒤 발언으로 전문 용어를 복원하고, 결론이 뒤집힐 수 있는 부정 표현이나 학자명은 해당 원음 구간을 다시 인식해 비교했다. 긴 전사 공백에 덧붙인 개념 설명은 교안 쪽수와 함께 학습용 보충으로 표시했다.
5. **기기와 게시 위치에서 결과를 다시 읽었다.** 두 Mac에서 최종 TXT·JSON과 목록 파일의 해시를 비교했다. Notion에는 같은 날짜·과목의 녹음을 시작 시각 순으로 옮겨 본문과 목차 링크를 확인했다.

이 사례에서 음성 메모 제목 변경, 교안 대조 보완, Notion 반영은 별도 배치 작업으로 진행했다. 저장소 워커가 기본적으로 담당하는 범위는 앞 표의 전사·조건부 후처리·과목 폴더 전달까지다.
