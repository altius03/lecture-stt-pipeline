# Downstream Problem Row Triage — 2026-05-19

목적: 운영 DB의 downstream problem rows를 파일 변경 없이 read-only로 분류한다.

## 요약

- problem rows: 41
- `CONFLICT`: 26
- `INVALID_STEM`: 8
- `UNKNOWN_SUBJECT`: 7

## 처리 유형별 분류

- `add_subject_route_or_rename_stem`: 7
- `conflict_may_be_stale_or_db_only`: 1
- `conflict_requires_manual_compare`: 25
- `rename_or_exclude_non-lecture_artifact`: 8

해석:

- `conflict_requires_manual_compare`: source와 destination이 모두 존재하고 hash가 달라 overwrite 금지. 사용자가 어느 쪽이 canonical인지 비교해야 한다.
- `rename_or_exclude_non-lecture_artifact`: 날짜+과목 stem 규칙에 맞지 않는 파일. 강의 산출물이면 rename, 아니면 downstream 대상에서 제외한다.
- `add_subject_route_or_rename_stem`: stem은 날짜+문자 과목 코드처럼 보이나 config subject route에 없다. 새 과목이면 route 추가, 오타면 rename한다.

## 전체 problem rows

| stem | subject | correction | summary | error | source exists txt/json/md | diff destinations | 권장 처리 |
|---|---|---|---|---|---|---|---|
| `260421DS_1` | DS | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260421DS_2` | DS | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260427DS_1` | DS | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260427DS_2` | DS | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260428DS_1` | DS | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260428DS_2` | DS | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260420DStr` | DStr | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260427DStr_1` | DStr | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260427DStr_2` | DStr | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260430DStr_1` | DStr | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260430DStr_2` | DStr | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260428LA_1` | LA | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260429LA` | LA | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260420LC` | LC | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260422LC` | LC | CONFLICT | BLOCKED | CONFLICT | 0/0/0 |  | 수동 확인 |
| `260427LC_1` | LC | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260427LC_2` | LC | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260429LC_1` | LC | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260429LC_2` | LC | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260428OOP_1` | OOP | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260428OOP_2` | OOP | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260422Unix` | Unix | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260428Unix_1` | Unix | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260428Unix_2` | Unix | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260429Unix_1` | Unix | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `260429Unix_2` | Unix | CONFLICT | BLOCKED | CONFLICT | 1/1/1 | origin_txt,origin_json,gh_summary | destination과 source 내용 비교 후 canonical 결정; 자동 overwrite 금지 |
| `2603034LA_2` | 4LA | MISSING | ERROR | INVALID_STEM | 0/0/0 |  | 파일명 규칙에 맞게 rename하거나 downstream 대상에서 제외 |
| `260407_LA` | UNKNOWN | ERROR | MISSING | INVALID_STEM | 0/0/0 |  | 파일명 규칙에 맞게 rename하거나 downstream 대상에서 제외 |
| `260413_LC_2` | UNKNOWN | ERROR | MISSING | INVALID_STEM | 0/0/0 |  | 파일명 규칙에 맞게 rename하거나 downstream 대상에서 제외 |
| `BOSS_SPECIAL_LECTURE` | UNKNOWN | ERROR | ERROR | INVALID_STEM | 0/0/0 |  | 파일명 규칙에 맞게 rename하거나 downstream 대상에서 제외 |
| `test123` | UNKNOWN | ERROR | MISSING | INVALID_STEM | 0/0/0 |  | 파일명 규칙에 맞게 rename하거나 downstream 대상에서 제외 |
| `tmp_260330DStr_2` | UNKNOWN | MISSING | ERROR | INVALID_STEM | 0/0/0 |  | 파일명 규칙에 맞게 rename하거나 downstream 대상에서 제외 |
| `zztest` | UNKNOWN | ERROR | MISSING | INVALID_STEM | 0/0/0 |  | 파일명 규칙에 맞게 rename하거나 downstream 대상에서 제외 |
| `선형대수학_시험출제포인트_전체정리` | UNKNOWN | MISSING | ERROR | INVALID_STEM | 0/0/0 |  | 파일명 규칙에 맞게 rename하거나 downstream 대상에서 제외 |
| `260323DS_1__20260412_020950__3b5301` | DS | ERROR | MISSING | UNKNOWN_SUBJECT | 0/0/0 |  | 과목 route 추가 또는 stem 수정(raw=DS) |
| `260324DS_1__20260324_123825__6e0631` | DS | ERROR | MISSING | UNKNOWN_SUBJECT | 0/0/0 |  | 과목 route 추가 또는 stem 수정(raw=DS) |
| `260407DS_1_20260412_013102_c22194` | DS | ERROR | MISSING | UNKNOWN_SUBJECT | 0/0/0 |  | 과목 route 추가 또는 stem 수정(raw=DS) |
| `260407DS_2_20260412_013113_58e799` | DS | ERROR | MISSING | UNKNOWN_SUBJECT | 0/0/0 |  | 과목 route 추가 또는 stem 수정(raw=DS) |
| `260326DStr_2__20260329_152337__601c7d` | DStr | ERROR | MISSING | UNKNOWN_SUBJECT | 0/0/0 |  | 과목 route 추가 또는 stem 수정(raw=DStr) |
| `260326OOP_1__20260329_152339__674c57` | OOP | ERROR | MISSING | UNKNOWN_SUBJECT | 0/0/0 |  | 과목 route 추가 또는 stem 수정(raw=OOP) |
| `260324Unix_1__20260324_163221__174a51` | Unix | ERROR | MISSING | UNKNOWN_SUBJECT | 0/0/0 |  | 과목 route 추가 또는 stem 수정(raw=Unix) |

## 자동 변경 금지 사항

- conflict 26건 중 25건은 source와 destination이 모두 존재하고 hash가 달라서 현재 정책상 정확히 보호되고 있다.
- `260422LC` 1건은 DB에는 conflict로 남아 있으나 현재 source 파일이 없어 stale/DB-only 여부를 별도로 확인해야 한다.
- DB row 삭제, source 삭제, destination overwrite는 수행하지 않았다.
- 다음 단계는 사용자가 canonical을 정한 뒤 rename/route 추가/수동 merge 계획을 별도 승인받아 실행한다.
