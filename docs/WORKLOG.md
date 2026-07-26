# Lecture STT Worklog

이 파일은 저장소에 반영된 변경을 날짜순으로 누적 기록한다.
최신 항목을 위에 추가한다.

## 2026-07-27

### Storage v2 운영 보존 반영과 read-only 웹 패널 전환
- 운영 v1 SQLite를 backup API로 `state/backups/storage-v2-rollout-20260727T012255+0900/jobs.snapshot.sqlite3`에 일관되게 보존했다. Snapshot은 `quick_check=ok`, jobs 132건, deliveries 247건이며 SHA-256은 `c6a8d9611a67f57591be8a0a70bdec0e566bcc94609a016236db0c87a7eb4603`이다. 기존 설정도 같은 rollback evidence 디렉터리에 권한 `0600`으로 복사했고 snapshot/config manifest를 함께 남겼다.
- 전체 132건 import plan은 applicable 132, blocker 0, missing source 91, needs-review 27이었고 exact plan SHA-256 `25ea41ea8a805b1635115d597956928f3f8b0bbb7183df010c761dec53e982fc`를 고정했다. `expected_count=132`, exact digest, `--allow-write`, `--allow-missing-source` guard로 별도 운영 DB `state/storage-v2.sqlite3`와 별도 root `~/Library/Application Support/lecture_stt/storage-v2/records`에만 적용했다.
- 적용 결과는 imported 132, skipped 0이며 verifier는 issue 0이었다. 동일 plan replay는 imported 0, skipped 132, recovered 0이고 DB SHA-256도 바뀌지 않았다. Plan/apply/replay/verifier JSON과 metadata-only adapter preflight 결과는 rollback evidence 디렉터리에 보존했다.
- Machine-local `config/config.yaml`에서는 analytics/library/archive/timetable/title review의 읽기만 활성화하고 status write, confirmation, promotion, materialization을 포함한 모든 쓰기 gate는 계속 `false`로 유지했다. `com.geonha.lecture-stt-webpanel`만 재시작했으며 STT와 downstream PID는 그대로 유지했다. Live HTTP에서 library 132건, unified review 92건, archive 0건과 각 source availability를 확인했고 현재 probe 동안 webpanel error log에 새 오류가 추가되지 않았다. 전후 PID, endpoint status/capability와 log size/mtime/SHA-256은 rollback evidence의 `runtime-validation.json`에 함께 기록했다.
- Legacy v1 DB/root와 iCloud 원본에는 migration, rename, move, delete를 하지 않았다. STT/worker cutover, canonical title 자동 승격, 웹 materialization/upload도 수행하지 않았다. 남은 open review 92건은 사람이 확인할 queue이며 자동 처리 대상이 아니다.

### GitHub Actions 교차 플랫폼 저장소 검증 보강
- 첫 공개 커밋 뒤 Ubuntu CI에서 SQLite가 read-only/writable reopen 중 quiescent `-wal`/`-journal`과 coordination-only `-shm`을 재생성하는 동작을 writable target 변조로 오인하는 것을 확인했다. 메인 DB inode·metadata·SHA-256과 sidecar regular-file/single-link 검증은 그대로 유지하고 durable WAL/journal SHA-256도 snapshot에 추가했다. Absent/0-byte identity churn, content·identity·size·mtime이 같은 active WAL/journal의 ctime-only churn, WAL identity churn에 수반되는 SHM 재생성만 reopen 경계에서 허용한다. SHM 단독 identity churn과 durable sidecar content/mtime 변경은 계속 fail-closed한다.
- 별도 dependency를 설치하지 않는 macOS smoke에서 테스트 본문이 PyYAML을 import하던 문제도 제거했다. Dependency-free fake YAML module이 sanitizer의 nested payload와 exact serialized config를 함께 검증하며 인증·secret allowlist와 runtime isolation 검증은 바꾸지 않았다.
- 격리 Linux Python 3.12에서 Storage v2 283개, dependency-free Python 3.12에서 Hermes smoke 54개, 로컬 전체 Python 563개가 통과했다. `compileall`, `git diff --check`, 운영 read-only verifier의 recordings 132/artifacts 540/issue 0과 운영 DB snapshot 복사본의 writable reopen 2회 canary도 통과했다.

### Storage v2 운영 반영 preflight
- Git 공개와 운영용 Storage v2 환경 구축을 시작하기 전 `origin/main`, 누적 dirty worktree, 운영 설정과 importer root 분리 계약을 다시 대조했다.
- `config/config.example.yaml`의 기존 `${LECTURE_RECORDINGS_ROOT}/storage-v2` 예시는 importer가 의도대로 거부하는 legacy root 내부 target이어서, 원본과 물리적으로 분리된 `~/Library/Application Support/lecture_stt/storage-v2/records`로 바로잡았다. 운영 v1 DB/root와 iCloud 원본은 이 preflight에서 변경하지 않았다.

## 2026-07-26

### Confirmed content-title canonical materialization CLI와 recovery journal
- `src/lecture_stt/storage_v2/title_materialization.py`를 추가해 confirmed/resolved content-title proposal 하나만 명시적으로 정본 제목에 반영하는 `plan-title-materialization`/`apply-title-materialization` CLI를 만들었다. Apply는 기본 비활성 `--enable-materialization`, `--allow-write`, `expected_count=1`, exact private-evidence plan SHA-256을 모두 요구하며 웹 materialization endpoint/UI나 자동 승격은 추가하지 않았다.
- Public plan은 storage key, 이전/목표 표시 제목, count와 digest만 반환한다. Journal의 closed private plan은 confirmation digest, proposal/review identity와 exact resolved 시각, transcript artifact path/hash/bytes/revision을 보존하고, apply 직전과 finalize 직전에 current title, exact transcript bytes, 현재 recorded time/context/classification inference, proposal과 linked review를 다시 계산한다. Stale proposal, current-title drift, transcript drift, linked-review 변경은 canonical selection 전에 fail-closed한다.
- 기존 classification materialization을 복사하지 않고 records-root lock, canonical manifest read/bytes/replace, same-directory temp `fsync`·`os.replace`·directory `fsync`, DB integrity와 verifier issue helper를 재사용했다. Title 전용 `recording_title_materializations` journal은 이전/새 title revision, confirmation/materialization digest, old/new manifest digest와 `prepared`/`applied` 상태를 recording composite FK로 묶고 identity update와 delete를 막는다.
- 첫 transaction은 기존 current title을 유지한 채 새 `system` title revision과 `prepared` journal을 commit한다. Manifest의 title field만 원자 교체한 뒤 두 번째 transaction에서 이전 title을 비-current 이력으로 남기고 새 title과 journal을 `applied`로 전환한다. Old/new manifest 어느 시점에서 중단돼도 같은 guarded plan으로만 forward recovery하며 applied replay는 원래 current-title evidence를 재구성한 뒤 실제 target title/manifest를 별도 검증해 `skipped`로 끝난다. 일반 confirmation replay는 materialization 뒤 current-title 변경을 stale로 보고 거부한다.
- `verify_library()`는 title journal의 closed plan/digest, proposal-review-transcript inference replay, title revision/selection과 manifest digest를 교차 확인하고 `prepared`를 recovery-required로 보고한다. 안전한 첫 revision에서는 한 recording당 content-title materialization 하나만 허용하고 timetable materialization 이력과의 혼합 chain은 양쪽 CLI에서 거부한다.
- 새 migration은 운영 DB에 적용하지 않고 pre-release canonical `migrations/v2/0001_recording_store.sql`을 `TemporaryDirectory` DB에서만 새로 생성해 검증했다. Focused Python 138개, Storage v2 278개, 전체 Python 557개, `compileall`, `git diff --check`가 통과했다. Recovery 테스트는 manifest 교체 전과 교체 후/finalize 전 crash를 각각 `prepared`로 남긴 뒤 `recovered`로 닫는 경로를 포함한다.
- 별도 `TemporaryDirectory` canary에서 `not_prepared → materialized → skipped`, 이전 `filename_inference` title 이력과 새 current `system` title, journal `applied`, verifier issue 0을 확인했다. Manifest는 title 외 필드가 같았고 storage key/source path, context, artifact path, source/transcript bytes도 불변이었다. 유효한 linked-review resolved 시각 변조와 transcript byte 변조 canary는 모두 journal 0, manifest/current title 불변 상태로 거부됐다.
- 운영 Storage v2/legacy DB와 root, launchd, worker, iCloud 원본에는 migration/cutover/restart/read/write를 수행하지 않았다. 파일 rename/move, web upload, frontend 변경과 Git stage/commit/push도 수행하지 않았다.

### Content title proposal metadata API와 네 source 검토 workbench
- `recording_title_proposals`에 이미 보존한 제안을 웹에서 검토할 수 있도록 `/api/storage-v2/title-suggestions` 목록·상세와 explicit reject, confirmation plan/apply endpoint를 추가했다. 목록·상세는 proposed title, storage key, transcript revision, proposal/review lifecycle, 제안 당시 classification 상태와 현재 linked classification만 반환하며 transcript 원문·발췌, artifact path/digest, recording/job/artifact numeric id는 반환하지 않는다. Public proposal id와 통합 feed identity는 JS-safe positive integer까지만 허용한다.
- `storage_v2.title_review.enabled`, `status_writes_enabled`, `confirmations_enabled`를 실제 YAML boolean gate로 분리하고 example config는 모두 `false`로 두었다. 비활성 목록은 DB/root를 해석하지 않고 zero-count unavailable payload를 반환하며 상세·write는 닫힌다. DB가 없는 활성 구성도 capability를 모두 내린 `storage_v2_db_unavailable`로 fail-closed한다. Query/path/body는 canonical positive id, exact key, lowercase SHA-256과 boolean `allow_write`를 검증하고 내부 오류는 경로를 숨긴 503으로 반환한다.
- 제목 보류는 별도 2단계 UI 확인 뒤 proposal을 rejected, review를 dismissed로 바꾸며, confirmation은 read-only plan의 `expected_count=1`과 exact digest를 표시한 뒤 별도 capability와 `allow_write`가 모두 참일 때만 proposal/review를 confirmed/resolved로 바꾼다. 둘 다 current title, selected context, immutable storage key와 manifest를 바꾸지 않는다. Proposal 생성, canonical content-title materialization, web upload endpoint는 추가하지 않았다.
- Server-canonical unified feed를 `storage-v2/unified-review-feed@2`로 올려 archive, timetable, title, recording 네 source를 고정 순서로 제공한다. Timetable/title proposal이 연결한 review ID의 full-table 합집합을 `DISTINCT` 차감해 recording fallback 중복을 제거하고, 한 recording에 서로 다른 미연결 review가 여러 건이면 각각 집계한다. `title:<proposal_id>`는 exact `#review/title/<proposal_id>`로 연결한다.
- React+TypeScript의 기존 Grafana/Falcon식 source ledger를 유지하면서 `TitleSuggestionPanel`과 `useTitleSuggestionReview`를 추가했다. Mount 뒤 fresh title 목록이 `available=true`를 증명한 경우에만 상세를 읽고 warm cache와 기본 비활성 deep link는 숨긴다. 첫 8건 밖 exact deep link도 별도 metadata detail로 유지하고, 제안 당시 classification과 현재 linked classification을 분리 표시한다. Plan 응답은 선택 proposal의 updated metadata와 다시 대조하며 request ID와 abort로 늦은 응답이 새 선택을 덮지 못하게 한다. Reject/confirmation 이후 title 목록과 unified feed를 함께 invalidate한다.
- 선택 항목이 있으면 source ledger → 선택 workbench → unified queue의 실제 DOM 순서와 전폭 한 열 layout을 사용하고, 선택이 없을 때만 desktop ledger/queue 두 열을 유지한다. 900px 이하 제목 panel은 상세를 bounded 8건 목록보다 먼저 렌더링하며 560px 이하 목록을 한 열로 고정했다. API 비활성, Storage v2 DB 미가용, 기타 closed reason도 raw unknown reason을 노출하지 않는 별도 안내로 구분한다.
- 격리 `TemporaryDirectory`의 새 migration DB/root canary에서 content title proposal 1건을 생성한 뒤 state adapter의 목록·상세·네 source unified feed·confirmation plan/apply를 통과시켰다. 기본 비활성 confirmation이 거부되고, gate를 켠 격리 apply는 `confirmed`로 끝났다. Plan count 1, digest 64자리, title source 1건, integrity issue 0, public detail의 path/hash/internal id 노출 0, records tree byte digest 불변, canonical title 0건, selected meeting context 1건과 storage key 불변을 확인했다.
- 자동 검증은 focused web/title 110개, Storage v2 262개, 전체 Python 541개, frontend Vitest 31개 파일 239개, `compileall`, 독립 `tsc -b`, Vite production build와 `git diff --check`를 통과했다. 격리 Chromium에서 1280×900과 390×844의 실제 DOM·computed grid를 확인해 선택된 세 outer block이 각각 root 전폭이고, 모바일 상세가 목록보다 앞서며 목록도 한 열이고 page 수평 overflow가 없음을 확인했다. Plan preview는 body `{}`인 POST 1건만 보냈고 count 1·64자리 digest·audit-only·disabled apply를 표시했으며, 비활성 deep link는 state/title-list/unified GET 외 detail·write 요청을 만들지 않았다. 웹 업로드 control과 console error/warning은 0건이었다. 최종 스크린샷은 `output/playwright/title-review/desktop-title-workbench-final-1280x900.png`, `mobile-title-workbench-final-390x844.png`, `mobile-title-disabled-final-390x844.png`에 남겼다.
- 독립 reviewer의 backend/API/unified feed와 최종 React layout 재검토에서 High/Medium 잔존 항목은 없었다. 향후 content title canonical materialization을 추가할 때는 confirmed replay의 현재 inference 재현 계약을 별도 문서·회귀 테스트로 다시 고정한다.
- 이 단계에서는 격리 DB/root와 fake API만 사용했다. 운영 Storage v2/legacy DB와 root, launchd/worker, iCloud 원본에는 migration/cutover/restart를 수행하지 않았고 Git stage/commit/push도 수행하지 않았다.

## 2026-07-25

### Transcript content 기반 범용 제목 제안 원장과 guarded CLI
- 학기·schedule entry가 필수인 `recording_classification_proposals`를 회의·대화·개인 메모 제목에 재사용하지 않고, `recording_title_proposals`를 별도 추가했다. Exact transcript artifact, 선택적인 내용-confirmed timetable proposal, 전용 review item을 recording composite FK로 묶고 suggested/confirmed/rejected 상태와 recording당 active proposal 하나를 강제한다.
- `src/lecture_stt/storage_v2/title_suggestions.py`는 records root flock 아래 current job의 latest `transcript_raw_text`를 component별 `O_NOFOLLOW`로 연다. Regular single-link, bounded stable read, DB bytes/SHA-256, strict UTF-8/NFC를 모두 확인하고 symlink/hardlink/hash/size/read-race/encoding 불일치는 fail-closed한다. V2 DB가 records root 내부인 구성도 거부한다.
- Current manual/schedule title은 보호한다. Exactly one active unique-time timetable proposal의 course name/code가 transcript에 명시적으로 존재할 때만 기존 날짜·수업명·교시 제목을 `schedule_content_match`로 제안한다. 그렇지 않으면 외부 모델 없이 bounded keyword 2~3개와 valid recorded date, 이미 selected된 context label/type으로 `content_topic`을 제안한다. 모든 결과는 review-only이며 자동 정본 확정은 없다.
- Public plan/snapshot/detail에는 transcript 원문·발췌, keyword 배열, artifact path/content digest, recording/job/artifact numeric id를 넣지 않는다. Bounded proposed title만 내용 유래 값으로 노출한다. 반환하지 않는 artifact id/path/hash/bytes와 recorded timestamp, current title, selected context, active classification metadata는 plan digest에 포함해 표시 제목이 같아도 source/inference 입력이 바뀌면 stale plan으로 막는다. Course code는 더 긴 코드의 부분 문자열이 아니라 영숫자·한글 경계가 일치할 때만 exact signal로 인정한다.
- `plan/apply/snapshot-title-suggestions`, audit-only confirmation plan/apply, explicit reject CLI를 추가했다. Apply는 기본 비활성 enable, allow-write, expected count, exact digest를 요구하고 read-only preflight, `BEGIN IMMEDIATE` 재계산, insert 뒤 commit 직전 재계산을 모두 통과해야 한다. 동일 proposal의 멱등 skip도 linked review의 recording/job/artifact/status/severity/reason/detail/lifecycle을 다시 대조한다. Confirmation plan/apply는 `--records-root`를 필수로 받고 현재 manual/schedule title 보호, recorded timestamp, selected context, active classification, exact transcript로 원래 제안을 다시 계산한다. 이 current-plan digest와 lifecycle 전이 후에도 재구성 가능한 stable evidence digest를 read-only/exclusive/pre-commit 및 confirmed 멱등 replay에서 대조하므로 같은 결과를 내는 inference metadata 변경, 사후 proposal 변조, commit 직전 transcript 변조도 rollback/conflict로 닫는다. Confirm은 review resolved, reject는 dismissed만 기록하며 `recording_titles`, `recording_contexts`, manifest는 바꾸지 않는다.
- Verifier는 proposal-review-artifact/job 소속, current/latest transcript, active timetable link, closed detail JSON/NFC와 상태별 review lifecycle을 검사한다. 이 단계에는 title proposal HTTP API, React workbench, canonical title materialization, 웹 업로드를 추가하지 않았다.
- 독립 reviewer가 confirmed proposal의 non-current transcript를 verifier가 놓치던 경계와, 같은 recording의 unrelated review로 relink된 proposal을 reject가 dismiss할 수 있던 경계를 Medium으로 재현했다. Suggested/confirmed 모두 current/latest를 요구하도록 verifier를 바로잡고 review severity/resolved lifecycle도 포함했다. Reject는 suggested/rejected 최초·멱등·전이 후마다 proposal↔review job/artifact/severity/reason/detail/lifecycle 전체를 검증하며, relink 변조는 어떤 review 상태도 바꾸지 않고 rollback한다. 두 재현 테스트를 추가한 뒤 같은 reviewer가 재검토했고 title-suggestion 범위의 High/Medium 잔존 이슈는 없었다.
- 격리 `TemporaryDirectory` canary에서 meeting context와 transcript 한 건을 plan→apply→snapshot→confirmation으로 통과시켰다. `2026-07-25 주간 보안 회의 보안 · 대시보드 · 품질`을 제안했고 proposal 1건 생성, confirmed 전이, integrity issue 0, public path/hash/artifact id 노출 0을 확인했다. Canonical title row는 0건이었고 기존 selected context 1건은 그대로였다.
- 자동 검증은 title 전용 27개, Storage v2 전체 257개, 전체 Python 527개, frontend Vitest 27개 파일 197개, TypeScript/Vite production build, `compileall`, `git diff --check`를 통과했다. 제안/confirmation commit 직전 transcript 변경 rollback, root/file symlink·hardlink, hash/UTF-8 mismatch, hidden artifact/inference-evidence digest, linked review 변조와 reject rollback, manual-title stale confirmation, confirmed replay 사후 변조/current drift verifier, rejected classification 이력, course-code 부분 문자열 배제, reject 뒤 재제안, confirm/reject review lifecycle, snapshot exact public key와 verifier 변조 검출을 포함한다.
- 모든 write 검증은 `TemporaryDirectory`의 새 migration DB/root에서만 수행했다. 운영 DB/root, launchd, worker, iCloud 원본에는 migration/cutover/restart/read를 수행하지 않았고 Git stage/commit/push도 수행하지 않았다.

## 2026-07-24

### Storage v2 통합 검토 index와 source별 guarded workbench
- 이 2026-07-24 client-only bounded 합성은 2026-07-25의 server-canonical `/api/storage-v2/review-feed`로 대체됐다. 아래 항목은 교체 전 단계의 이력이다.
- 새 backend aggregate endpoint를 만들지 않고 React `useUnifiedReviewFeed`가 검토 화면 mount 시에만 기존 archive evidence case 최대 100건, `suggested` timetable proposal 최대 200건, recording library summary 최대 200건을 독립적으로 읽어 합성한다. 각 query는 retry·window focus·reconnect refetch를 끄고 mount 뒤 fresh 성공/실패가 settle되기 전에는 warm success/error cache를 숨긴다.
- Source ledger는 archive/classification/recording의 ready·disabled·error·loading, bounded visible/total, truncation을 따로 표시한다. 한 source가 비활성화되거나 실패해도 healthy source 항목은 유지한다. 같은 `storage_key`에서 현재 bounded timetable 응답에 보이는 suggested proposal 수만 recording `open_review_count`에서 0 아래로 내려가지 않게 차감하며, 잘린 범위를 전역 dedupe로 주장하지 않는다.
- `#review/archive/<case_key>`, `#review/timetable/<canonical-positive-id>`, `#review/recording/<storage_key>` deep link를 추가했다. Malformed percent encoding, encoded slash, extra segment, unknown source, leading zero·plus·unsafe timetable id는 검토 기본 화면으로 fail-closed한다. Archive/timetable 초기 선택은 첫 목록 page 밖에서도 detail query로 유지하고 hash back/forward 전환도 복원한다.
- 통합 화면은 Grafana/Falcon식 source health ledger와 scan-first review runway로 정리하되 기존 `ArchiveReviewPanel`, `TimetablePanel`, `RecordingLibraryPanel`을 workbench로 그대로 사용한다. 상태 변경·reject·confirmation·canonical promotion의 기본 비활성 설정과 기존 plan/count/digest/allow-write guard는 변경하지 않았다. 1280px에서는 ledger 3개 compact 행과 queue 2열 경계, 390px에서는 ledger·queue 1열로 표시하고 긴 한국어 제목·storage key가 page overflow를 만들지 않게 했다.
- TypeScript 독립 점검에서 이전 mount의 error cache가 새 mount의 fresh refetch 전 노출되던 경계를 찾아 fresh-after-mount error gate와 회귀 테스트를 추가했다. 테스트 전담 점검은 복수 visible proposal 차감과 0 clamp, bounded ledger truncation, workbench/route target 전환, 비정규 timetable hash를 보강했다.
- 실제 Chromium 격리 canary에서 홈에는 검토 API 요청이 없고 `#review`에서 세 list GET만 발생하는 것을 확인했다. Exact archive/timetable/recording detail, 첫 page 밖 archive/timetable 선택, malformed route, back/forward, timetable source 비활성 상태의 partial queue, 한국어 NFC, 웹 업로드 control 부재, write POST 0건, console error/warning 0건을 확인했다. 1280×900과 390×844 모두 page 수평 overflow가 없었고 스크린샷은 `output/playwright/unified-review/desktop-review-1280x900.png`, `mobile-review-390x844.png`에 남겼다.
- 최종 독립 reviewer는 on-demand/fresh cache gate, partial source, bounded 중복 차감, canonical hash, 첫 page 밖 선택과 기존 mutation guard를 다시 확인했으며 통합 검토 범위의 High/Medium 잔존 이슈를 찾지 못했다.
- 최종 자동 검증은 프론트 Vitest 25개 파일 183개, 독립 `tsc -b`, Vite production build, Storage v2 unittest 204개, 전체 Python unittest 465개를 통과했다. 운영 DB/root, launchd, worker, iCloud 원본에는 접근·migration·cutover·restart하지 않았고 Git stage/commit/push도 수행하지 않았다.

### Storage v2 녹음 보관함과 revision 상세 연결
- `src/lecture_stt/storage_v2/library.py`와 `/api/storage-v2/library/recordings` 목록·상세 endpoint를 추가했다. 목록은 표시 이름/NFC 원본명, current title/selected context/current job, 상태·open review·artifact count만 반환한다. 상세는 non-archived job 50개, artifact 500개, review 100개로 제한하고 실제 count와 truncation을 함께 반환한다. Archived job/artifact correlation은 review에서 `null`로 닫는다.
- `storage_v2.library.enabled`는 실제 YAML boolean `true`일 때만 열고 example config는 기본 `false`로 뒀다. 목록 query의 key·중복·빈 값·canonical decimal과 detail의 단일 ASCII storage key segment를 fail-closed로 검사한다. Numeric DB id, source/artifact path, digest, transcript·교정·요약·scorecard 본문, review detail, job config/error는 API에 반환하지 않는다. DB는 SQLite `mode=ro`로만 연다.
- React `RecordingLibraryPanel`을 실제 Storage v2 API에 연결했다. 목록과 선택 detail을 별도 TanStack Query로 유지해 첫 페이지 밖 storage key도 직접 조회하며, list gate가 `available=true`인 경우에만 detail query를 연다. 원본·전사·교정·요약·지원 artifact를 job별 revision lane으로 표시하고 내부 storage key와 사용자 표시 이름을 분리한다. 물리 폴더와 웹 업로드 UI는 노출하지 않는다.
- 홈의 일간·주간·월간 attention 행을 `#library/<storage_key>` 상세로 연결했다. Hash는 malformed percent encoding, percent-encoded slash, extra segment를 거부하며 browser back/forward에서도 선택 상세를 복원한다. 프론트 decoder는 backend 상한, NFC, enum, job/artifact/review 소속, stage, latest/current 유일성, count/truncation 교차 불변성을 재검증하고 list가 비활성화되면 cache된 detail도 숨긴다.
- 격리 fake API와 실제 Chromium에서 dashboard attention → 목록 첫 페이지 밖 상세가 list 1회와 선택 detail 1회로 이어지는 것을 확인했다. 1280×900은 목록/상세 2열 비율, 390×844는 한 열과 page 무수평 overflow를 확인했고, 350px table 영역 안에서 780px table만 내부 스크롤했다. 한국어/NFC, 전사 r1/r2와 교정·요약·지원 lane, 웹 업로드·본문·경로·digest 부재, console error/warning 0건을 확인했다. 기본 비활성 deep link는 list gate만 조회하고 detail 요청을 만들지 않았다. 스크린샷은 `output/playwright/recording-library/desktop-library-final-1280x900.png`, `mobile-library-revisions-final-390x844.png`, `mobile-library-disabled-message-final-390x844.png`에 남겼다.
- 독립 reviewer가 같은 브라우저 세션의 warm cache가 비활성 전환 직후 stale list/detail을 노출할 수 있는 경계와, 전역 artifact 정렬에서 오래된 job이 500개 상한을 먼저 소비할 수 있는 경계를 지적했다. 화면 mount 뒤 fresh list 성공을 다시 요구하고 refetch 중에도 cache를 숨기도록 보정했으며, artifact query는 visible current/newest job rank를 첫 정렬키로 사용한다. Warm-cache remount와 old-job 501 revisions 회귀 테스트를 추가했다.
- 최종 자동 검증은 프론트 Vitest 23개 파일 160개, 독립 `tsc -b`, Vite production build, Storage v2 unittest 204개, 전체 Python unittest 465개, `compileall`, `git diff --check`를 통과했다.
- 같은 독립 reviewer의 재검토에서 두 Medium이 모두 닫혔고, 이번 녹음 보관함 범위의 High/Medium/Low 잔존 항목은 없었다.
- 이 단계에서는 격리 fake API와 임시 DB fixture만 사용했다. 운영 Storage v2/legacy DB와 root, launchd/worker, iCloud 원본에는 migration/cutover/restart/read를 수행하지 않았고 Git stage/commit/push도 수행하지 않았다.

### Storage v2 전사 관제 API와 일간·주간·월간 홈 대시보드
- `src/lecture_stt/storage_v2/analytics.py`를 추가해 archive되지 않은 recording의 current transcription job을 오늘/최근 7일/최근 30일로 bounded 집계한다. 상태별 처리량, 품질 점수 구간, 선택된 context/source 분류, coverage/freshness와 최근 attention 항목을 같은 cohort에서 만들며 `recorded_at` → `received_at` → `queued_at` fallback 사용량도 노출한다.
- 품질은 latest non-archived `quality_scorecard`만 사용한다. 명시적 records root dirfd 아래 storage key/상대경로를 component별 no-follow open하고 regular single-link, 최대 bytes, stable stat, exact integer schema version, kind/score/health와 optional numeric duration을 검증한다. 실제 bytes/SHA-256을 DB artifact metadata와 대조하고 null/malformed/mismatch, 깊은 JSON, 1년을 넘는 비현실적 duration은 해당 row만 invalid로 격리한다. Missing/invalid는 전체 요청을 실패시키거나 평균 분모에서 숨기지 않고 별도 coverage와 `unscored` 구간으로 표시하며, duration 부분합은 known/jobs 표본 수를 함께 노출한다.
- `/api/storage-v2/analytics/transcriptions?period=day|week|month`를 `/api/state`·SSE와 분리된 read-only endpoint로 연결했다. `storage_v2.analytics.enabled`가 실제 YAML boolean `true`이고 별도 `records_root`가 명시된 경우에만 열리며 example config 기본값은 `false`다. 빈 값·알 수 없는 값·반복 period는 400으로 거부한다. Timestamp fallback parser를 SQLite UDF와 공유해 기간 필터를 row limit보다 먼저 적용하므로 미래·invalid·극단 offset 행이 정상 기간 행을 밀어내지 않는다.
- DB는 SQLite `mode=ro`로 schema/table write를 금지하면서 WAL-visible row를 읽는다. 격리 WAL fixture에서 main DB와 WAL의 bytes·size·mtime·ctime 불변을 검증했다. SQLite read-lock coordination이 `-shm` bytes/mtime/ctime을 바꿀 수 있는 표준 동작은 숨기지 않고 SHM identity·mode·single-link·size 불변 계약으로 좁혔다.
- API는 source/artifact path, transcript/correction/summary 본문, scorecard summary, error message와 engine parameter를 반환하지 않는다. 현재 title 또는 NFC original name을 표시 이름으로 쓰고 내부 `storage_key`는 별도 필드로 유지한다. Suggested classification은 확정 분류에 섞지 않고 selected context가 없는 recording은 `미분류`로 남긴다.
- React 홈을 개인 STT 관제 화면으로 개편했다. 기간 탭은 한 번에 하나의 on-demand query만 열고, 전사량·완료율·검토/오류·평균 품질 KPI, 처리량 추이, 상태/품질 histogram, 확정 분류/source, duration 표본 coverage, 최근 attention table을 모바일 우선 layout으로 표시한다. Timeline은 bucket별 수치를 읽을 수 있는 semantic list로 제공한다. 현재 archive evidence `검토 큐`/legacy `처리 현황`에는 storage key 상세 계약이 없으므로 잘못된 링크를 만들지 않고 attention 후속은 `상세 연결 전`으로 명시한다. 새 chart/runtime 의존성, 웹 업로드, background analytics polling은 추가하지 않았다.
- 프론트 decoder는 고정 schema/기간/bucket/점수 구간/status/health/표시 label과 모든 분모·분포·duration 표본 합계를 fail-closed로 검증한다. Attention 품질 점수는 backend 계약대로 정수만 허용하고 health와 함께 존재하거나 함께 비어야 하며, 분류 context/source도 쌍으로 존재해야 한다. Status·timeline·품질·분류 분포는 전체 합계뿐 아니라 대응하는 totals/coverage 값과 정확히 일치해야 하고 분류 context/source pair는 유일해야 한다. Query는 stale/abort 가능한 경계를 사용하며 기간 tab은 roving `tabIndex`, 좌우 방향키와 Home/End, `aria-controls`로 키보드·보조기술 흐름도 고정했다. 프론트 Vitest 19개 파일 123개 테스트와 TypeScript/Vite production build가 통과했다.
- 격리된 `TemporaryDirectory` DB/root 테스트는 설정 gate, API/SSE 분리, 기간 경계, current row 선택, 시간 fallback, 합계/coverage, NFC, privacy, scorecard의 exact schema type/metadata mismatch/deep JSON/extreme duration/malformed/oversize/symlink/hardlink/non-regular/change 방어, main/WAL 불변·SHM coordination 계약과 truncation을 검증했다. Analytics 전용 22개, web panel 연동 47개, Storage v2 198개, 전체 Python unittest 454개가 통과했다.
- `output/playwright/analytics-observability/fake_analytics_server.py`의 fake state/API와 실제 Chromium에서 day/week/month가 선택한 기간 요청만 1회씩 만드는 것을 확인했다. 1280×900과 390×844에서 page 무수평 overflow, 월간 chart desktop fit과 mobile 내부 scroll, attention table의 실제 `overflow-x:auto`(350px viewport 영역/720px table), semantic 30 bucket, 잘못된 attention link 0개, backend 고정 품질/분류 label, 한국어/NFC, 밝은/어두운 모드, 방향키 tab 이동, 웹 업로드·절대경로·본문 필드 부재, console error/warning 0건을 확인했다. Strict decoder 보강 뒤 최종 스크린샷은 `output/playwright/analytics-observability/desktop-month-dark-contract-final.png`, `mobile-month-dark-contract-final.png`에 남겼다.
- 독립 reviewer가 SQL cohort, scorecard 무결성/예외 격리, duration coverage, WAL read-only 경계, decoder 교차 불변성, 모바일/접근성/링크를 재검토했으며 최종 High/Medium/Low 잔존 항목은 없었다.
- 이 단계에서는 격리 임시 DB/root fixture와 fake API만 사용했으며 운영 Storage v2 DB/root, legacy 운영 DB, launchd/worker, iCloud 원본에는 migration/cutover/restart/read를 수행하지 않았다. Git stage/commit/push도 수행하지 않았다.

### Dashboard 화면 경계와 유휴 log I/O 절감
- `frontend/web-panel/src/App.tsx`를 hash 기반 `홈`, `처리 현황`, `녹음 보관함`, `검토 큐`, `시간표`, `설정` 화면으로 나눴다. 각 화면은 현재 실제 state/API가 제공하는 범위만 표시하고, 아직 연결되지 않은 recording revision library와 archive evidence 통합 UI는 추정값 대신 연결 전 상태를 명시한다.
- `ActivityPanel`과 `usePanelLogs`는 처리 현황 화면에서만 활성화한다. `TimetablePanel`도 시간표 화면에서만 mount하므로 홈 유휴 상태에서는 `/api/logs`, timetable entries/classifications 요청과 해당 React update를 만들지 않는다.
- `src/lecture_stt/ui/web_panel.py`의 기존 bare `/api/events`는 state+logs 호환 계약을 유지한다. 새 `/api/events?streams=state`는 state/heartbeat만 보내고 서버의 log delta 읽기를 생략한다. 빈 값·중복·반복 query·알 수 없는 stream은 400으로 fail-closed 처리한다.
- 처리 현황에서는 state/log 훅이 기존 combined SSE singleton을 공유하고, 다른 화면에서는 state-only SSE를 우선 사용한다. 새 endpoint와 연결되지 않는 구형 backend에는 기존 combined SSE 또는 HTTP polling으로 돌아가는 호환 경계를 유지한다.
- 처리 화면 전환 시 endpoint를 현재 화면 상태에서 동기적으로 계산하고 log 훅은 같은 combined singleton에만 합류하도록 해 state-only와 combined 연결이 한 render 동안 겹치던 경쟁 조건을 막았다. 명시적 대기 구간에는 log HTTP fallback만 허용하고, 로그를 끄면 stale realtime 상태도 함께 초기화한다.
- 모바일 화면 index는 수평 swipe에 숨기지 않고 2열 grid로 바꿔 여섯 화면과 현재 선택을 한 번에 보이게 했다. 900px 이하에서는 3열로 전환하며 긴 storage path도 카드 안에서 줄바꿈한다.
- 화면 hash·조건부 panel mount·stream endpoint/fallback·back/forward 복원·이전 SSE 해제·비활성 log I/O를 회귀 테스트로 고정했다. 반복 `streams` query와 중복 stream token의 400 거부도 직접 검증한다. 최종 자동 검증은 프론트 Vitest 59개, TypeScript/Vite production build, 전체 Python unittest 427개, `compileall`, `git diff --check`를 통과했다.
- `/private/tmp/lecture-stt-dashboard-browser.2tqPSc`의 격리 fake API와 실제 Chromium에서 홈은 `/api/state` + `/api/events?streams=state`만, 처리 화면은 추가 `/api/logs` + 단일 `/api/events`, 시간표 화면은 다시 state-only SSE와 timetable on-demand query만 여는 순서를 확인했다. Back/forward hash 복원, 1280px/390px 무수평 overflow, 한국어 NFC, 웹 업로드 UI 부재, console error/warning 0건도 확인했다. 스크린샷은 `output/playwright/dashboard-home-desktop-1280x900.png`, `dashboard-timetable-mobile-390x844.png`에 남겼다.
- 독립 reviewer가 지적한 두 Low 테스트 공백을 위 반복/중복 query, back/forward, unsubscribe 회귀로 보완했고 재검토에서 High/Medium/Low 잔존 항목이 없음을 확인했다.

### Web panel timetable 액션 guard 고정과 stale plan 차단
- `frontend/web-panel/src/hooks/useTimetableReview.ts`에서 confirmation plan 요청마다 local request id를 발급하고, 선택 proposal이 바뀌거나 transient state를 비우면 이전 request id를 폐기하도록 보강했다. 늦게 도착한 이전 proposal의 plan 응답은 현재 선택 상태를 다시 덮지 않는다.
- 같은 훅의 reject/apply 성공 후 선택 reset은 현재 선택이 해당 proposal일 때만 수행하도록 좁혔다. 사용자가 pending 중 다른 proposal을 선택했을 때 이전 action 성공이 새 선택을 강제로 지우지 않으며, 두 late-success 경로를 각각 회귀 테스트로 고정했다.
- `rejectProposal`, `loadConfirmationPlan`, `applyConfirmationPlan`은 mutation error를 내부 `actionError`로만 반영하고 `Promise<boolean>`으로 수렴시켜, 컴포넌트에서 `void ...`로 호출해도 unhandled rejection이 남지 않도록 정리했다. `TimetablePanel.tsx`의 reject 확인 UI도 성공한 경우에만 닫히게 맞췄다.
- `frontend/web-panel/src/types.ts`, `decodeTimetable.ts`, `panelApi.ts`는 backend audit-only 계약을 더 좁게 반영한다. confirmation plan의 `expected_count`는 정확히 `1`, `canonical_metadata_changed`는 항상 `false`, digest는 소문자 SHA-256 hex만 허용한다. apply 요청도 같은 guard 값 외에는 보내지 않는다.
- Timetable action 응답은 요청한 proposal id와 다시 대조하고 confirmation apply 결과의 digest도 실제 적용한 plan digest와 일치해야만 성공으로 처리한다. 계획 화면에는 축약하지 않은 64자리 digest 전체를 카드 내부 줄바꿈으로 표시한다.
- 최종 자동 검증은 프론트 Vitest 47개, 독립 `tsc -b`, TypeScript/Vite production build, 전체 Python unittest 421개, `compileall`, `git diff --check`를 통과했다.
- `/private/tmp/lecture-stt-timetable-action-browser-20260724.2ilRoV`의 격리 fake API와 390×844 Playwright viewport에서 기본 비활성 capability의 plan preview, 64자리 digest 전체, disabled apply/reject를 확인했다. Canary 내부 capability만 켠 뒤 confirmation apply와 2단계 reject/cancel/retry를 실행했고, 요청 body는 각각 exact `expected_count=1`·plan digest·`allow_write=true`, `status=rejected`·`allow_write=true`였다. 최종 집계는 confirmed 1/rejected 1, document/body scroll width는 390, 한국어 NFC 표시는 정상, console error/warning은 0이었다. 스크린샷은 `output/playwright/timetable-actions-disabled-plan-mobile-390x844.png`, `timetable-actions-lifecycle-complete-mobile-390x844.png`에 남겼다.
- 리뷰 보정 후 같은 격리 서버를 새 프로세스로 초기화해 최종 브라우저 회귀를 반복했다. 접근성 snapshot에서 proposal selector가 실제 `button`과 pressed 상태로 노출됐고, 두 번째 제안을 선택한 뒤 `Shift+Tab`·`Enter`로 첫 제안을 다시 선택해 이전 reject 확인이 닫히는 것을 확인했다. 다시 plan/apply/reject 전 과정을 통과했고 exact request guard, confirmed 1/rejected 1, 390px 무수평 overflow, console 0건을 재확인했다. 최종 스크린샷은 `output/playwright/timetable-actions-reviewer-closeout-disabled-mobile-390x844.png`, `timetable-actions-reviewer-closeout-complete-mobile-390x844.png`에 남겼다.
- 이 검증은 운영 DB, launchd/worker, iCloud 원본에 연결하지 않았고 Git stage/commit/push도 수행하지 않았다. Playwright transient snapshot은 canary 임시 경로로 옮겼다.

### Web panel timetable 액션 UX 보정
- `frontend/web-panel/src/components/panel/TimetablePanel.tsx`의 검토 목록 선택을 interactive table row에서 네이티브 `<button>`과 `aria-pressed` 상태로 바꿨다. 키보드 기본 동작과 보조기술 시맨틱을 브라우저에 맡기고, 선택 proposal이 바뀌면 이전 proposal의 reject 확인 박스를 항상 닫는다.
- action notice/error 영역에 `aria-live`와 `role=status|alert`를 추가해 reject/plan/apply 결과가 보조기술에 즉시 전달되도록 했다.
- `frontend/web-panel/src/styles.css`의 confirmation plan digest는 390px급 폭에서도 카드 밖으로 넘치지 않도록 wrapping으로 바꿨다.
- `frontend/web-panel/test/TimetablePanel.test.tsx`에 네이티브 선택 버튼과 `aria-pressed` 전환, reject 확인 UI reset 회귀 테스트를 추가했다. `useTimetableReview.test.tsx`는 늦게 성공한 reject/apply가 새 선택을 지우지 않는 두 경로를, `panelApi.timetable.test.ts`는 잘못 상관된 proposal/digest 응답 거부를 직접 검증한다.

### Web panel timetable 검토 액션 연결
- `frontend/web-panel/src/lib/panelApi.ts`, `decodeTimetable.ts`, `types.ts`에 시간표 검토 action payload decoder와 안전한 API error 해석을 추가했다. `/status`는 explicit `rejected`만, confirmation은 plan/apply를 분리해 exact `expected_count`와 `expected_plan_sha256`만 전달한다.
- `frontend/web-panel/src/hooks/useTimetableReview.ts`는 read-only query 위에 reject/plan/apply mutation을 얹고, 선택 변경이나 성공 후 stale confirmation plan을 비우며 suggested list/detail query를 invalidate하도록 확장했다. confirmation apply는 plan에서 받은 guard 값만 재사용하고 임의 값을 만들지 않는다.
- `frontend/web-panel/src/components/panel/TimetablePanel.tsx`는 proposal 상세에 두 단계 보류 확인 UI와 confirmation plan preview/apply UI를 추가했다. `status_writes_enabled=false`면 보류 버튼을 막고, `confirmations_enabled=false`여도 plan preview는 허용하되 최종 apply는 막는다. 유일 시간 일치가 아닌 제안은 로컬에서 먼저 확정 비활성화 문구를 보여 준다.
- UI copy는 현재 단계가 audit-only confirmation이며 `canonical_metadata_changed=false`, title/context/manifest를 바꾸지 않는다는 사실을 그대로 노출한다. monochrome 모바일 우선 레이아웃과 키보드 선택 흐름은 유지했다.
- 검증: `npm --prefix frontend/web-panel test -- --run test/useTimetableReview.test.tsx test/decodeTimetable.test.ts test/TimetablePanel.test.tsx`, `npm --prefix frontend/web-panel run build`

## 2026-07-23

### Web panel read-only timetable/classification 연결
- 현재 Vite + React + TypeScript client는 typed decoder, TanStack Query, Vitest와 Python static-serving 경계가 이미 있어 그대로 유지하기로 했다. 프레임워크 재작성보다 controller/worker 경계와 idle I/O 개선을 먼저 증명한다.
- `frontend/web-panel/src/lib/decodeTimetable.ts`, `panelApi.ts`, `hooks/useTimetableReview.ts`를 추가/확장해 기존 runtime SSE `PanelState`와 별개로 Storage v2 시간표 entry 및 classification proposal을 on-demand query로 읽도록 했다. `PanelState` 숫자 schema 계약은 그대로 두고, timetable/classification은 별도 `"storage-v2/...@1"` decoder로 분리했다.
- `frontend/web-panel/src/components/panel/TimetablePanel.tsx`를 추가하고 `App.tsx`에 연결해 학기별 active 시간표와 suggested 분류 검토 큐를 표시한다. 여러 학기를 조회하면 학기 열과 중립적인 제목으로 구분하며, confirmed/rejected는 집계에만 남기고 actionable row에는 섞지 않는다. proposal 상세에서는 candidate entry key, explicit reject, confirmation plan/apply를 guard 상태와 함께 보여 준다.
- timetable capability decoder/type을 backend payload와 맞춰 `confirmations_enabled`와 `status_writes_enabled`를 함께 검증하도록 보강했고, 검토 큐 선택 행에 `tabIndex`, `aria-selected`, Enter/Space 키 선택을 추가해 마우스 없이도 proposal 상세를 열 수 있게 했다.
- 스타일과 테스트를 최소 범위로 보강했다. 고정 1320/1280px page 폭을 제거하고 900/560px responsive 경계에서 header·요약·시간표를 단일 열로 전환하며 표만 카드 내부에서 가로 스크롤한다. `test/TimetablePanel.test.tsx`, `useTimetableReview.test.tsx`, decoder와 `App.test.tsx` mock을 갱신했다.
- 프론트 Vitest 전체 28개와 TypeScript/Vite production build가 통과했다. Playwright 격리 fake-state 서버의 390×844 viewport에서 `innerWidth`, document/body `scrollWidth`가 모두 390으로 일치했고 한국어/NFC 시간표·suggested proposal 상세·키보드 선택 행·웹 업로드 부재를 확인했다. Status HTTP smoke는 정상 요청 200, malformed JSON 400, unknown action 404였다. 앱 오류는 없었고 기존 favicon 부재 404 한 건만 console에 남았다. 운영 DB migration/cutover, launchd/worker 재시작, iCloud 원본 변경, Git stage/commit/push는 수행하지 않았다.

### 학기별 시간표 import와 suggested/confirmed 분류 검토 코어
- `src/lecture_stt/storage_v2/timetable.py`를 추가해 UTF-8 CSV/JSON 시간표를 NFC 정규화하고 학기·과목명·선택 과목 코드·요일·시작/종료 시각·교시·강의실의 closed metadata plan으로 변환한다. Source는 single-link regular file로 bounded read하며 source path와 raw CSV/JSON body는 plan/DB/API에 저장하지 않는다.
- `schedule_imports`, `schedule_entries`, `schedule_semester_selections`, `recording_classification_proposals`를 pre-release Storage v2 canonical `0001`에 추가했다. Import/entry는 update 불가이고 normalized entry-set digest와 row count를 verifier가 재계산한다. 학기별 selection은 과거 import를 삭제하지 않고 active import만 교체하며 목록/분류는 active entry만 사용한다. Proposal은 schedule semester와 review recording 소속을 composite FK로 묶고, partial unique index로 recording+semester당 active suggested/confirmed 결정 하나와 rejected 이력 보존을 함께 보장한다.
- `plan-timetable`, `apply-timetable`, `snapshot-timetable` CLI를 추가했다. Apply는 현재 source에서 다시 만든 `expected_count`, exact plan SHA-256, `allow_write`를 모두 요구하며 같은 학기+entry-set 재적용은 row metadata를 검증하고 `skipped`로 끝난다.
- `plan/apply/snapshot-timetable-classification`과 `plan/apply-timetable-confirmation` CLI를 추가했다. 분류는 recording의 `recorded_at` 요일/시각만 보수적으로 사용한다. 유일 후보도 자동 확정하지 않고 suggested proposal+open review를 만들며, 후보 없음·복수·시간 부재/오류도 검토 큐로 보낸다.
- Explicit confirmation은 기본 비활성화된 독립 guard, `allow_write`, `expected_count=1`, exact plan digest를 요구한다. Proposal이 참조한 import가 더 이상 해당 학기의 active selection이 아니어도 stale conflict로 거부한다. 이번 단계의 confirmation은 status/plan SHA/confirmed time과 resolved review를 남기는 audit-only 동작이며, `recording_titles`, `recording_contexts`, record manifest는 변경하지 않는다. Manifest와 DB를 함께 원자적으로 갱신하는 materialization은 후속 단계로 남겼다.
- 내장 backend에 timetable entry/proposal 목록·상세, explicit reject/dismiss 상태 전이와 confirmation plan/apply on-demand API를 추가했다. Status write는 별도 `status_writes_enabled`와 요청별 `allow_write`가 모두 필요하다. Rejected 이력 뒤에는 새 active suggestion을 만들 수 있고, 동일한 confirmed proposal을 포함한 classification 재실행은 멱등하게 건너뛴다. 웹 업로드와 classification generation endpoint는 만들지 않았고, 모든 API/write capability는 example config에서 실제 YAML boolean `true`일 때만 열리도록 기본 비활성화했다. 이 데이터는 평시 SSE snapshot에 포함하지 않는다.
- 현재까지 timetable 전용 18개, Storage v2 전체 176개, web panel backend/state 36개, 전체 Python 421개 회귀가 통과했다. `/private/tmp/lecture-stt-timetable-lifecycle-canary-20260724.BrGe78`에서 2-entry import의 `imported`/재실행 `skipped`, verifier issue 0, unique match confirmation, confirmed 재분류 2건 skip, no-match reject 뒤 새 suggestion 생성, SQLite quick check와 FK violation 0을 확인했다. 운영 DB migration/cutover, launchd/worker 재시작, iCloud 원본 변경, Git stage/commit/push는 수행하지 않았다.

### Archive evidence 검토 큐 API와 metadata-only canonical promotion
- `src/lecture_stt/storage_v2/archive_review.py`를 추가해 archive evidence case의 bounded 목록/상세 조회, 상태 필터, revision별 SHA-256·크기·MIME·observation role/relationship 비교 metadata를 제공한다. Capture snapshot의 절대경로, source relpath와 transcript/correction/summary 본문은 응답하지 않는다.
- Artifact kind별 canonical 제안은 legacy ledger 일치 revision이 유일할 때 우선하고, 그렇지 않으면 최신 capture에서 current GH/Obsidian revision이 유일할 때만 제안한다. 후보가 여러 개면 `unresolved`로 남기며 자동 확정하지 않는다.
- `archive_evidence_canonical_selections`를 Storage v2 canonical schema에 추가했다. `(revision_id, case_id, artifact_kind)` composite FK와 immutable update trigger로 다른 case/kind revision 선택과 확정 row 변경을 막고, verifier가 promoted case의 resolved 상태·selection 존재·단일 plan digest를 교차 확인한다.
- 상태 변경은 명시적 `allow_write`와 설정의 `status_writes_enabled`가 모두 있어야 한다. Canonical promotion은 기존 Storage v2 recording을 사용자가 지정하고 case의 모든 artifact kind revision을 명시하는 metadata-only 연결이며, evidence/records 파일을 복사·이동·삭제하거나 recording을 자동 생성하지 않는다.
- Promotion은 read-only plan에서 `expected_count=1`과 stable SHA-256을 생성한다. Apply는 기본 비활성화된 `promotions_enabled`, 요청의 `allow_write`, 정확한 count·plan digest를 모두 요구하고 `BEGIN IMMEDIATE` 안에서 plan을 재검증한 뒤 selection audit row, `promoted_recording_id`, resolved 상태를 함께 commit한다. Stale/conflicting plan은 거부하고 정확히 같은 재적용만 `skipped`다.
- 내장 HTTP backend에 `/api/storage-v2/archive-evidence/cases` 목록/상세, status, promotion plan/apply endpoint를 추가했다. Archive review 조회는 유휴 SSE snapshot에 포함하지 않아 평시 부하를 늘리지 않으며 example config는 API·상태 write·promotion을 모두 기본 비활성화한다.
- 이 단계는 임시 Storage v2 DB 기반 회귀 테스트만 수행했다. 운영 DB migration, archive evidence/records root 변경, launchd/worker 재시작, iCloud 원본 변경, Git stage/commit/push는 수행하지 않았다.
- 최종 검증은 전체 Python unittest 394개, archive-review 관련 78개, 프론트 Vitest 24개, TypeScript/Vite production build, compileall, diff/whitespace check를 통과했다. Fake state를 사용한 격리 localhost 브라우저에서 목록·상세 200, 한국어 NFC, 절대경로/본문 미노출, plan 200, disabled apply 403, malformed JSON 400, unknown API 404를 확인했다. 독립 reviewer의 High/Medium 지적은 없었고, 비활성 상세 API가 503을 반환하던 Low 항목은 명시적 403과 회귀 테스트로 수정 후 재검토를 통과했다.

### Recording-independent archive evidence/revision queue
- `archive_evidence_cases`, `archive_evidence_captures`, `archive_evidence_revisions`, `archive_evidence_observations`를 Storage v2 canonical schema에 추가했다. Ownerless delivery는 recording FK 없이 open review case로 보존하고, capture 시점 metadata, 서로 다른 실제 바이트, current/historical 관측 provenance를 분리한다.
- `plan-archive-evidence`, `apply-archive-evidence`, `snapshot-archive-evidence`, `verify-archive-evidence` CLI를 추가했다. Reconciliation의 `blocked`는 canonical 자동 선택만 막고, root-bound single-link regular source는 현재본과 과거본을 content-addressed revision으로 함께 보존할 수 있다.
- 과거 recorded 절대경로는 `--historical-root LABEL=PATH`로 명시한 root 아래에 있을 때만 I/O 대상으로 삼는다. Plan과 apply는 legacy DB snapshot, source inode/link/size/mtime/ctime/hash, root overlap, count, plan SHA-256, explicit write guard를 확인한다.
- evidence root는 exclusive lock, closed inventory, no-replace staging/fsync promote, `BEGIN IMMEDIATE`, DB quick/FK/canonical path, metadata-only capture manifest, revision hash의 pre/post verifier를 사용한다. Capture snapshot은 status/classification/flag/token, canonical SHA-256, absolute artifact path·확장자를 닫힌 allowlist로 검증하고 observation metadata도 고정 stat 7개만 허용한다. 자유 형식 issue message/expected/actual은 보존하지 않는다. 파일만 promote된 crash window는 exact manifest/hash가 일치할 때 DB를 recovery하며, 같은 plan은 `skipped`다.
- 전체 125건을 대상으로 한 read-only evidence plan은 source safety blocking 0건, canonical review 대상 124건, revision 362개, observation 474개였다. `summary_status=MISSING`인데 현재 summary가 존재한 9개 파일은 GH/Obsidian의 `unexpected_current` observation 18개로 나타났으며 전체 apply는 수행하지 않았다.
- `/private/tmp/lecture-stt-archive-evidence-canary-final3-20260724.DbCe3d`에서 batch plan SHA-256 `4f5db6f29dac82009817b3ce17a5d474caf924e5913ec8fab34cd045ae3cead0`인 `260430DStr_2` 한 건을 격리 적용했다. current correction txt/json, current summary, legacy SHA와 일치하는 과거 Obsidian summary를 revision 4개와 observation 5개로 보존했고 verifier issue 0, open case 1, 재적용 `skipped`를 확인했다.
- 운영 Storage v2 DB/launchd/worker와 current·historical archive 원본은 수정하지 않았고, 전체 125건 evidence apply도 수행하지 않았다.
- DB와 manifest를 함께 맞춘 snapshot unknown field/허위 hash, observation metadata unknown field 변조 회귀를 추가했다. 독립 reviewer의 최종 재검토에서 archive-evidence 범위 High/Medium 잔존 항목은 없었고, Storage v2 unittest 142개와 전체 Python unittest 370개를 통과했다.

### Ownerless archive reconciliation과 Storage v2 isolated canary
- `src/lecture_stt/storage_v2/archive_reconciliation.py`와 `reconcile-archive` CLI를 추가했다. 독립 legacy snapshot에서 `source_job_id IS NULL`이고 어떤 legacy job과도 매칭되지 않는 delivery만 골라, source artifact 부재와 현재 GH/Obsidian route의 correction/summary 경로·SHA-256을 읽기 전용으로 대조한다.
- 과거 delivery에 기록된 iCloud GH/Obsidian 절대경로는 현재 configured root 밖이면 열지 않고 `destination_relocated` 정보성 evidence로만 남긴다. 실제 검증은 deterministic current route에서 수행하며, root 이탈·symlink component·비정규 파일·hardlink를 거부하고 같은 열린 FD에서 stat과 hash를 확인한다.
- DB snapshot은 archive probe가 끝난 뒤에도 본체·WAL·journal·SHM 안정성을 다시 검사한다. delivery/job SQL read와 candidate/output row 상한, fixed metadata-only output을 두었고 transcript/correction/summary 본문은 report에 포함하지 않는다.
- Phase 0 snapshot의 ownerless/unmatched 125건을 현재 archive와 대조한 결과 `verified_delivered` 1건, `blocked` 124건이었다. 현재 correction txt/json은 모두 존재하지만 123건이 저장 SHA와 달랐고, `DELIVERED` summary 103건 중 56건이 달랐다. `MISSING` summary 22건 중 9건은 현재 route에 파일이 존재했다.
- 과거 Obsidian recorded path에 남은 파일 2개도 별도 read-only로 확인했다. `260422Unix`는 현재본과 동일했고, `260430DStr_2`는 저장 SHA와 일치하지만 현재본과 다른 이전 revision이므로 과거 root 정리 전 보존 대상이다.
- `/private/tmp/lecture-stt-storage-v2-canary-20260724.4yWhwa`에 legacy job 290 한 건을 target-only로 import했다. recording 1건과 원본·transcript txt/json·quality·summary 총 artifact 5개를 만들었고 verifier는 issue 0이었다. 같은 plan 재적용은 `skipped`로 끝나 멱등성을 확인했다.
- legacy snapshot DB와 canary 원본 오디오 SHA-256은 계획 시점 값과 같았다. 운영 v2 DB, launchd, worker, iCloud v1 보관소는 수정하지 않았다.
- archive reconciliation을 포함한 Storage v2 회귀 123개와 전체 Python unittest 351개를 통과했다. `src/scripts/tests` compileall과 `git diff --check`도 통과했다.

### Storage v2 preserve-first importer와 read-only library projection
- `src/lecture_stt/storage_v2/`에 manifest validator/writer, 별도 v2 DB migration repository, legacy read-only candidate discovery, no-overwrite staging importer, dashboard용 library snapshot, 전체 artifact verifier를 추가했다.
- importer는 legacy DB와 `01_audio`~`04_summarize`를 수정하지 않는다. CLI에서 명시한 single-link regular SQLite snapshot만 immutable read-only로 열어 `quick_check`, 본체 stat/hash, WAL/journal/SHM 상태를 연결 전후·조회 뒤에 확인한다. 이 snapshot은 plan digest에 포함하며 records-root lock 획득 뒤와 commit 직전에도 다시 검증한다.
- 선택 필터보다 먼저 모든 job/delivery/source/artifact의 lineage와 canonical path/inode claim을 조사한다. 같은 파일의 source-vs-transcript/correction/summary 역할 충돌, cross-base 공유, symlink/legacy-root 이탈은 fail-closed다. `deliveries.source_job_id`만 명시 소유권으로 사용하고 duplicate base의 비소유 job에서는 실제 공유 artifact만 제외한다.
- `DELIVERED` artifact의 저장 SHA 불일치는 차단한다. legacy가 SHA를 정본으로 갱신하지 않는 `BLOCKED`/`ERROR` 등 비성공 artifact는 현재 파일을 보존하되 stale-hash warning과 open review를 남기며, malformed digest는 상태와 관계없이 차단한다.
- 신규 v2 DB/root는 legacy root 밖의 별도 경로만 허용한다. 기존 DB는 read-only schema/checksum preflight 뒤에만 writable로 열며, main pathname↔SQLite opened inode와 `-wal/-shm/-journal`의 symlink/hardlink/special-file 상태를 전후 검증한다. DB↔records-root binding, exclusive root lock, `BEGIN IMMEDIATE`, closed root inventory로 서로 다른 root의 동시/혼합 import를 차단한다.
- 실제 파일은 plan 시점과 copy/skip/recovery 직전에 inode/크기/hash 또는 부재 probe를 다시 확인한다. no-overwrite `.staging` copy의 SHA-256/byte 수와 file/directory/root `fsync` 뒤 `records/<storage_key>`로 promote하며, recovery commit과 신규 DB parent directory도 `fsync`한다. commit 결과가 불명확하면 promote한 record를 지우지 않고 다음 실행이 exact manifest/tree/DB provenance를 검증해 복구한다.
- `legacy_import_map` fingerprint만 같다고 skip하지 않는다. recording/title/context/job/selected-engine/artifact/review/import-map row, deterministic timestamp/log provenance, manifest와 실제 tree가 모두 일치해야 한다. record 내부 hardlink/duplicate inode도 skip/recovery에서 거부한다. CLI apply는 `--allow-write`, 현재 plan 수와 일치하는 `--expected-count`, canonical plan의 `--expected-plan-sha256`을 요구한다.
- manifest 전체에서 transcript/content 계열 key를 재귀적으로 금지한다. Verifier는 exclusive root lock과 단일 SQLite read transaction에서 canonical schema/checksum, `quick_check`, FK, 모든 non-archived job/selected engine/artifact 소유권, recording-level source와 ingest metadata를 교차 검증한다. Dirfd/openat+`O_NOFOLLOW`로 같은 FD에서 link/size/hash를 확인하고 root swap, orphan/unindexed/special entry, stale/과대 staging도 bounded scan 오류로 보고한다.
- migration 적용 시 현재 SQL SHA-256을 저장하고, 현재 SQL로 만든 canonical table/index/trigger 서명과 실제 DB를 비교한다. 약한 기존 table, checksum 변조, index/trigger drift는 import와 read-only snapshot 모두에서 차단한다.
- phase0 독립 snapshot과 iCloud legacy root를 다시 읽기만 한 inventory에서 legacy jobs 132건 모두 적용 가능했고 blocking plan은 0건이었다. 실제 원본은 41건에 남아 있고 91건은 기존 cleanup으로 부재해 `source_state=missing`과 `source/unavailable.json` marker로 파생 이력을 보존한다. 불완전한 transcript pair 26건과 stale `BLOCKED` summary hash 1건을 합쳐 `needs_review` 27건이며, 중복 비소유 job 1건에서는 공유 artifact를 제외한다.
- delivery 원장 247건 중 현재 job을 명시적으로 소유한 행은 122건이다. source file이 모두 사라진 ownerless/unmatched 125건은 임의 귀속하지 않았으며, 교정+요약 완료 103건과 교정 완료/요약 미완료 22건은 목적지 아카이브 reconciliation 후 cut-over 여부를 정한다.
- Storage v2 타깃 회귀 107개와 전체 Python unittest 335개를 통과했다. 프론트엔드 Vitest 24개, TypeScript/Vite production build, Storage v2 `compileall`, `git diff --check`도 통과했다.
- 실제 운영 v2 DB 생성, iCloud `records` 복사, launchd 변경, worker cut-over는 수행하지 않았다.

### 전면 리팩터링 Phase 0 기준선 및 Phase 1 호환성 안정화
- 운영 전환 전에 SQLite backup API와 config 복사본을 `state/backups/phase0-20260723T174053+0900`에 보존했다. 복사 DB의 `PRAGMA quick_check`는 `ok`였고, backup 시점 집계는 jobs `132`, deliveries `247`, `NEEDS_REVIEW` `0`이었다.
- macOS/iCloud NFD 한글 파일명이 `audio`로 붕괴하던 문제를 막기 위해 전사 stem을 NFC로 정규화하고 Unicode 문자·숫자를 보존하도록 수정했다.
- 후처리 단계에서 전체 반복 제거 결과가 세그먼트 재조립으로 되돌려지던 경로를 수정하고, 모든 세그먼트가 점 노이즈로 비워질 때 최종 텍스트도 빈 문자열로 유지되게 정리했다.
- 버전 전사 프로필을 추가했다. 기존 config는 `legacy/unversioned`로 같은 처리 동작을 유지하고, 새 `general/lecture/meeting/memo` 프로필은 transcribe override, 후처리 교정표, 품질 임계값을 선택하며 결과 metadata·engine params·quality scorecard에 `key/version/config_sha256` snapshot을 기록한다.
- machine-local `config/config.yaml`의 활성 프로필을 `general`로 설정해 기존 컴퓨터공학 강의 전용 initial prompt와 강의 용어 교정을 범용 녹음 경로에서 제거했다. 강의·회의·메모 규칙은 별도 프로필로 남겼다.
- 품질 점검에서 `bad`지만 산출물은 유효한 전사 결과를 `ERROR` 대신 `NEEDS_REVIEW`로 기록하고 실패 알림과 분리된 review 알림을 추가했다. 현행 웹 패널도 `확인 필요`를 오류와 분리해 집계·표시한다.
- 산출물 저장 직후 프로세스가 중단된 복구 경로와 과거 `DONE`/`NEEDS_REVIEW` dedupe 재사용 경로도 persisted `quality.health=bad`를 다시 `DONE` 성공으로 바꾸지 않고 `NEEDS_REVIEW`로 보존하도록 보강했다. Transcript metadata는 있으나 scorecard만 기록되지 않은 crash window에서는 metadata-only sidecar를 재생성해 검증하고, malformed sidecar는 산출물을 삭제하지 않은 채 검토 상태로 격리한다.
- cleanup apply는 미해결 `NEEDS_REVIEW`의 원본·전사·quality 세트를 보존하고, 보호 목록을 DB에서 읽을 수 없으면 정리를 거부한다. 웹 패널의 이력 정리는 검토 항목을 삭제하지 않는 정책을 유지하면서 버튼·안내 문구를 `완료/오류` 범위로 명확히 했다.
- Hermes picker와 실제 cron `operator.select_candidate()`가 모두 `02_transcripts/*.quality.json`의 `health=bad` 후보를 선택하지 않도록 차단하고, malformed/invalid scorecard는 해당 stem만 fail-closed로 건너뛰되 다음 정상 후보 처리는 계속하도록 보강했다.
- Hermes child는 terminal/MCP를 제공하지 않고 `file,no_mcp`로 제한했다. 부모 환경과 repo `.env`를 복제하지 않으며 활성 provider 변수만 allowlist하고, 격리 `HERMES_HOME`에는 해당 provider의 OAuth credential entry만 포함한 권한 `0600`의 최소 `auth.json` snapshot을 전달한다. 실제 설치된 Hermes file tool로 repo `.env`와 child `auth.json` 읽기가 모두 거부되는 것도 확인했다.
- 복잡한 종류별 폴더를 대체할 record-centric storage v2 foundation을 추가했다. `records/<storage_key>/` 하나가 원본과 job별 전사·교정·요약 revision을 소유하고 제목/과목/날짜는 metadata로 분리한다. `migrations/v2/0001_recording_store.sql`은 운영 DB에 적용하지 않고 임시 SQLite에서만 immutable key, 상대경로, JSON, partial unique, composite FK와 재적용 idempotency를 검증했다. 기존 `jobs` 또는 `deliveries` DB에 잘못 적용하면 persistent DDL 전에 중단하며, review/job/artifact가 서로 다른 소속으로 연결되는 것도 FK로 차단한다.
- 최종 검증은 `PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests` 232개, 프론트엔드 Vitest 24개, `npm run build`, Python `compileall`을 통과했다. UNIX socket 파일 유형 테스트 때문에 Python 전체 검증만 샌드박스 밖에서 실행했다.
- 이 단계에서는 실행 중 launchd 서비스 재시작, 운영 DB schema migration, 실제 녹음 canary, Git commit/push를 수행하지 않았다.

## 2026-07-05

### iCloud inbox EDEADLK recovery for ad-hoc transcription
- `00_inbox/gwangmyeongdong_audio.m4a` 처리 중 iCloud Drive 원본을 local staging으로 옮기는 단계에서 macOS `shutil.copy2`/`fcopyfile`이 `OSError: [Errno 11] Resource deadlock avoided`를 반복해, `safe_move_file()`에 errno 11 발생 시 streaming copy fallback을 추가했다.
- `com.geonha.lecture-stt`를 kickstart 재시작해 변경된 코드를 로드했고, job `301`이 `DONE`에 도달했다. 산출물은 `02_transcripts/gwangmyeongdong_audio.txt` 및 `.json`이며 raw transcript body는 이 worklog에 기록하지 않았다.
- 검증: `PYTHONPATH=src .venv/bin/python -m py_compile src/lecture_stt/shared/utils.py`, monkeypatched errno 11 fallback smoke test, DB row `DONE`, txt/json 존재 및 non-empty, JSON segment count `515`, Telegram 전송 API 응답 `ok=true`.
- 사용자의 직접 요청에 따라 전사 본문은 Telegram chat `1074160202`로 3개 chunk(message_id `288`, `289`, `290`)로 전송했다.

## 2026-05-25

### Hermes postprocess cron timeout alignment
- Investigated repeated `lecture_stt_postprocess_operator` cron failures where Hermes scheduler killed the repo wrapper after the scheduler default 120s script timeout while the child Hermes correction/summary process was still running, leaving `cron-lecture-stt-postprocess` child processes orphaned under PID 1.
- Updated the active Hermes default profile config to `cron.script_timeout_seconds=1800` and restarted the default Hermes gateway so the scheduler loads the new value. The active machine-local cron shim now passes `--child-timeout-sec 1500`, leaving a 5 minute parent-owned validation/promotion margin before the scheduler script timeout.
- Reaped the stale orphaned child process that was still running for a postprocess candidate. No final iCloud artifacts were modified by this cleanup step.
- Verification passed with active shim `py_compile`, `python3 -m compileall -q scripts tests`, `python3 -m unittest tests.test_hermes_postprocess_hardening -q` (`52` tests), `PYTHONPATH=src:. python3 -m unittest discover -s tests -q` (`191` tests), `git diff --check`, `hermes cron status`, and repo-local metadata-only operator `status`.
- Current cron job `last_status` can still show the pre-restart 120s timeout until the next scheduled run overwrites it; new scheduler processes resolve `script_timeout_seconds` as `1800`.

## 2026-05-21

### Hermes postprocess operator live canary, sandbox/runtime hardening, scheduler closeout
- Completed a one-stem live postprocess canary for `260504DS_1` under the approved operator boundary. Final iCloud artifacts now exist at `03_correction/260504DS_1.txt`, `03_correction/260504DS_1.json`, and `04_summarize/260504DS_1.md`; staged/final SHA-256 values matched in the metadata-only promote verification. Raw transcript/generated bodies were not copied into this worklog or closeout report.
- Fixed the activation blocker where a later child run could see stale staged artifacts after an earlier failed/timeout attempt by resetting `state/hermes_postprocess/staging/{stem}` before each parent-owned candidate/manifest write. Added regression coverage that a successful child exit with missing fresh artifacts fails as `unsafe_staging_artifact` and does not promote stale bodies.
- Hardened the child Hermes execution envelope by using a per-attempt isolated `HERMES_HOME` under staging, cleaning it after the child exits, and extending the macOS sandbox profile with required Hermes/Python runtime read paths plus explicit `/dev/null` write allowance while keeping final artifact writes parent-owned.
- Resumed the existing script-only Hermes cron job `lecture_stt_postprocess_operator` (`job_id=977667876027`, `every 30m`, `no_agent=true`, delivery `discord:#운영-보안`) and triggered a no-candidate scheduled run. The direct wrapper no-candidate probe returned exit `0` with stdout/stderr length `0`; the triggered cron run recorded `last_status=ok` with no pending candidates.
- Closeout evidence was written to gitignored `state/reports/hermes_postprocess/closeout-20260521T141641Z.md`. Verification passed with `python3 -m compileall -q scripts tests`, `python3 -m unittest tests.test_hermes_postprocess_hardening -q` (`52` tests), `PYTHONPATH=src:. python3 -m unittest discover -s tests -q` (`191` tests), and `git diff --check`. Bare unittest discovery without `PYTHONPATH=src:.` is not the valid test invocation because the package lives under `src/`.
- Not performed: commit, push, tag, release, bulk backlog processing, existing final overwrite, `05_prompt` automatic edits, cleanup/delete/move, or Hermes model/provider/privacy routing changes.

## 2026-05-20

### Repository README and structure cleanup
- Replaced the stale root README with the current `/Users/geonha/DEV/lecture_stt` operating path, remote/branch/CI summary, repository tree, runtime-data boundary, common commands, Hermes postprocess operator entrypoints, documentation priority, and safety rules. The README no longer uses the archived `/Users/geonha/lecture_stt` path for active commands.
- Cleaned repository ignore policy by consolidating duplicate `.gitignore` entries and adding explicit root-level ignores for legacy local runtime folders (`/audio/`, `/inbox/`, `/transcripts/`, `/errors/`) plus generated frontend artifacts and `/src/logs/`.
- Removed the obsolete tracked root `correct_unix.py` one-off hard-coded Unix correction script. Current correction/summary automation lives under `scripts/hermes_postprocess/` with metadata-only staging, validators, explicit promote, and no raw transcript body in reports.
- Removed local-only ignored clutter from the working tree: `.DS_Store` files, Python `__pycache__/` directories, stale `.bak` files, generated TypeScript build-info/Vite JS artifacts, empty legacy local runtime folders, and stale `src/logs/app.log`. This did not remove `.venv/`, `node_modules/`, `frontend/web-panel/dist/`, `state/`, `models/`, local `config/config.yaml`, or any iCloud/GH archive/Obsidian runtime payload.
- Updated `docs/ARCHITECTURE.md` to reflect the current 2026-05-20 top-level structure, gitignored runtime boundary, active Hermes operator status, and broadened test coverage.
- Verification passed with static added-line security scan (`0` findings), `git diff --check`, `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v` (`139` tests), `.venv/bin/python -m compileall -q scripts tests src`, frontend `npm test -- --run` (`21` tests), frontend `npm run build`, and independent pre-commit review with no blockers.

### Hermes postprocess operator Phase 2~4 repo-local implementation
- Added the repo-local Hermes postprocess helper package under `scripts/hermes_postprocess/` with metadata-only candidate discovery, prompt loading from the existing `05_prompt` surface, canonical path resolution, output contract schemas, correction/summary validators, review-only misrecognition candidate accumulation, metadata-only staging manifest support, and an explicit promote interface that is disabled unless `--allow-promote` is passed. The promote path now also validates candidate destination paths, re-runs deterministic validators before final writes, uses exclusive no-overwrite file creation, and rolls back files created earlier in the same promote attempt after hash verification if a later copy fails.
- Candidate discovery processes at most one stem per tick and reports only metadata/action-plan fields. It does not print raw transcript bodies or segment arrays. Summary-only candidates can use an existing final correction pair; blocked candidates such as partial final correction output are refused before promote.
- Added operator docs under `docs/operators/hermes-postprocess/`: README, runbook, correction prompt policy, summary prompt policy, output contract, failure policy, and cron prompt source. The docs keep iCloud `lecture_recordings/05_prompt` as the correction prompt source of truth and keep cron/promote/iCloud final writes behind later approval gates.
- After later approval in the same session, ran a content staging canary for live stem `260504DS_1`, validated staged correction/summary artifacts, and promoted exactly that one staged candidate to iCloud final paths without overwrite. The metadata-only promote report verified source/final existence, size, and SHA-256 matches for `03_correction/260504DS_1.txt`, `03_correction/260504DS_1.json`, and `04_summarize/260504DS_1.md`; raw transcript and generated bodies were not copied into reports.
- Registered active Hermes script-only cron job `lecture_stt_postprocess_operator` (`job_id=977667876027`, `every 30m`, `workdir=/Users/geonha/DEV/lecture_stt`, `script=lecture_stt_postprocess_operator.py`, `no_agent=true`, delivery `discord:#운영-보안`). The wrapper records an activation baseline in `state/hermes_postprocess/cron-baseline.json` and skips those backlog stems, processing only future candidates; direct wrapper and scheduled run verification showed no-candidate stdout remains empty.
- Added fixture-based tests in `tests/test_hermes_postprocess.py` for candidate selection, action planning, path resolution, raw-body leak prevention, prompt loading, staging manifest privacy, review-only misrecognition queue append/dedupe, invalid raw-excerpt rejection without echoing raw values, promote safety, final overwrite prevention, summary-only promote, blocked candidate refusal, candidate destination tamper rejection, stale pass-report revalidation, promote rollback, unresolved `${LECTURE_RECORDINGS_ROOT}` config placeholders, and validator failures such as `segment_metadata_changed`, `correction_non_text_metadata_changed`, and `summary_too_short`.
- Verification passed with `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v` (`138` tests), `.venv/bin/python -m unittest tests.test_hermes_postprocess -v` (`20` tests), `.venv/bin/python -m compileall -q scripts tests src`, `git diff --check`, added-line static scan (`0` findings), and explicit read-only iCloud candidate discovery via `PYTHONPATH=src .venv/bin/python -m scripts.hermes_postprocess dry-run --lecture-root "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings" --repo-root /Users/geonha/DEV/lecture_stt --stable-for-sec 60`. The live dry-run returned candidate stem `260504DS_1` with correction and summary both needed; it wrote no iCloud final artifacts.
- Not performed: launchd changes, existing-output overwrite, bulk backlog processing, `05_prompt` automatic edits, cleanup/delete/move, Hermes provider/privacy changes, push/tag/release.

### Approved downstream cleanup, runtime archive cleanup, and CI bootstrap
- Resumed the approved overnight closeout from live state rather than handoff assumptions: `main` and `origin/main` were both at `6392762`, the worktree was clean before this batch, GitHub Actions runs for `main` were still `[]`, DB `integrity_check` was `ok`, `foreign_key_check` was empty, STT jobs were `DONE=31` / `ERROR=1`, and downstream started at total rows `152` with problem rows `4`.
- The remaining downstream problem rows were resolved without overwriting destination content or deleting source evidence. A DB backup was written to `state/reports/downstream-resolve-20260520T014011Z/jobs-before-downstream-resolve.sqlite3`, invalid/manual source artifacts were moved to iCloud manual review archive `lecture_recordings/90_manual_review/downstream-excluded-20260520T014011Z`, and the four delivery rows were removed from tracking after manifesting their before state. Follow-up summary reported total rows `148`, problem rows `0`, correction `DELIVERED=148`, and summary `DELIVERED=126` / `MISSING=22`.
- Runtime cleanup was applied in preserve-first mode. Instead of permanent raw deletion, `469` old raw candidates (`01_audio=158`, `02_transcripts=311`, about `8.24GB`) were moved to iCloud `lecture_recordings/90_cleanup_archive/cleanup-20260520T014307Z`; repo-local tmp/log/state-log payloads and legacy `/Users/geonha/lecture_stt` were moved to `~/Library/Application Support/lecture_stt/cleanup_archives/cleanup-20260520T014307Z`. Post-cleanup `cleanup.py --apply` and dry-run both reported no remaining removal candidates beyond the preserved `inbox_staging`; launchd stdout/stderr rotation apply found all active logs under threshold and left them unchanged.
- Added `.github/workflows/ci.yml` because the repo still had no Actions workflow/run history. The workflow runs Python dependency install, unittest discovery, compileall, and web-panel `npm test` / `npm run build` on push, pull request, or manual dispatch. Local workflow YAML parsing, web-panel tests, and web-panel build passed before the final verification batch. Stale 2026-05-19 decision-guide wording was marked as superseded by the 2026-05-20 closeout results.
- Final local verification report was written under gitignored `state/reports/final-closeout-verification-20260520T015436Z`: `git diff --check` OK, workflow YAML parse OK, unittest discovery `118` tests OK, compileall OK, web-panel `21` tests OK, web-panel build OK, DB `integrity_check=ok`, `foreign_key_check=[]`, downstream problem rows `0`, and STT/downstream/webpanel launchd jobs active.
- First remote GitHub Actions run for commit `3d5925c` created the new `CI` workflow but failed because `tests/test_script_entrypoints.py` assumed repo-local `.venv/bin/python`, which does not exist on the GitHub runner. The test harness was made CI-portable by falling back to `sys.executable` when `.venv` is absent; follow-up local verification report `state/reports/ci-fix-verification-20260520T020333Z` passed targeted regression, unittest discovery, compileall, web-panel tests/build, DB integrity/FK, and downstream problem row checks before the fix push.

### Real lecture canary scorecard and downstream routine log-noise closeout
- Next real lecture canary `260504DS_2` was observed through the installed launchd-backed worker as job `203` and reached terminal `DONE`. Expected artifacts exist under iCloud `01_audio/260504DS_2.m4a` and `02_transcripts/260504DS_2.txt` / `.json`; canonical STT model remained `large-v3` with runtime package `faster-whisper==1.2.1`.
- The canary is an immediate operational pass, but its metadata quality report is `warn` with score `61/100` because repetition ratio is high. This is recorded as a quality-review note, not an infrastructure failure. Raw transcript text is not recorded in docs or reports.
- Added metadata-only quality scorecard sidecar support: future STT jobs, including duplicate-result replay jobs, whose transcript metadata contains a quality report now atomically write `02_transcripts/<stem>.quality.json` with `schema_version`, `kind`, `canonical_base`, `health`, `quality_score`, `summary`, metrics, timings, model fields, and artifact paths. Output validation now also parses the sidecar, enforces the metadata-only contract, and checks schema/kind plus key artifact links. The sidecar deliberately excludes transcript body and segment arrays.
- `_validate_output_files()` now treats a missing or malformed quality sidecar as an output validation failure when transcript metadata includes quality information. The already-completed job `203` predates this code path, so `260504DS_2.quality.json` was not created during the original run; it was backfilled later in the remaining-item follow-up below.
- Tightened downstream routine logging: by default, routine `scan_started` is not written to stdout or JSONL, and routine scan stats/stdout plus routine `scan_completed` JSONL entries are suppressed unless stats change or the bounded heartbeat emits `suppressed_scan_count`. Problem events and opt-in `log_routine_scan_events=True` remain available.
- Runtime cleanup stayed conservative. Dry-run report `state/reports/runtime-cleanup-20260519T153859Z` found broad audio/transcript deletion candidates, so no cleanup apply, source deletion, transcript deletion, DB clear, or destination overwrite was performed. Launchd log rotation dry-run found no apply-needed files.
- Downstream live status remains total rows `152`, problem rows `4` (`INVALID_STEM=3`, `CONFLICT=1` for preserved `260422LC`). These are existing manual/document-only rows and were not mutated.
- After code changes, only the named launchd jobs `com.geonha.lecture-stt` and `com.geonha.lecture-stt-distribute` were restarted; `com.geonha.lecture-stt`, `com.geonha.lecture-stt-distribute`, and `com.geonha.lecture-stt-webpanel` are running, while cleanup remains a calendar/on-demand job.
- Final local verification report was written under gitignored `state/reports/final-verification-20260520T000551Z`: unittest discovery `115` tests OK, compileall OK, `git diff --check` OK, DB `integrity_check=ok`, and downstream problem rows remained `4`.
- Independent review of pushed commit `8312890` found no blockers. As a follow-up TDD hardening, the web panel transcript count now ignores `<stem>.quality.json` sidecars and orphan scorecards, and cleanup retention groups `<stem>.txt`, `<stem>.json`, and `<stem>.quality.json` as one transcript set so `retain_min_transcripts` preserves/deletes sidecars with their primary transcript artifacts. Orphan-only scorecards do not consume `retain_min_transcripts` slots.
- Remaining-item follow-up: `260504DS_2.quality.json` was backfilled with metadata-only content and validated; downstream problem rows were re-diagnosed into `state/reports/downstream-diagnose-20260520T010747Z.json` plus manual action table `state/reports/downstream-manual-actions-20260520T010747Z.{json,csv}` with no deterministic automatic repair available; cleanup was re-run as dry-run only under `state/reports/runtime-cleanup-20260520T011046Z` and not applied because it would remove broad raw audio/transcript artifacts; a silent hourly stability monitor was installed for the 24h/next-2-lectures gate and will report only state changes, alerts, or PASS.

## 2026-05-19

### Post-final docs 정합성 및 downstream manual table
- Read-only baseline에서 `main`/`origin/main`이 `48be5a4`로 일치하고 worktree clean, DB `integrity_check=ok`, STT jobs `DONE=30`, `ERROR=1`임을 확인했다.
- 현재 runtime 적용 상태에 맞춰 `docs/OPERATIONS.md`, `docs/MODELS.md`, `docs/RUNTIME_MIGRATION_PLAN_2026-05-19.md`의 stale pre-execution wording을 정리했다.
- 현재 production `.venv`와 `requirements.txt`는 `faster-whisper==1.2.1`이며, canonical STT model은 계속 `large-v3`임을 문서에 재확인했다.
- Downstream problem rows는 total `4`로, `INVALID_STEM` 3건과 document-only `260422LC` 1건만 남아 있다. 파일별 판단용 report는 `state/reports/downstream-manual-table-20260519T131110Z.json` 및 `.csv`에 생성했다.
- Runtime cleanup은 계속 dry-run/list 상태만 유지한다. DB/file/config/launchd/package/model 변경, cleanup apply, push/tag/release는 수행하지 않았다.

### Final downstream cleanup, preserved failure evidence, runtime cleanup dry-run
- Later explicit execution request에 따라 남은 downstream problem 16건 중 safe 자동 수리 가능한 항목을 백업 후 적용했다. Backup은 `state/backups/downstream-repair-20260519T123211Z`, manifest는 `state/reports/downstream-repair-apply-20260519T123211Z.json`이다.
- Source canonical/rename-needed correction pair 9건은 canonical stem으로 rename했고 downstream worker가 처리해 9건 모두 `DELIVERED`가 됐다. DB-only stale rows `zztest`, `test123`, `tmp_260330DStr_2`는 backup 후 삭제했다.
- 정책상 보존 대상인 `260422LC`는 그대로 두었다. 남은 downstream problem rows는 invalid/manual review 3건과 document-only conflict 1건이다.
- Canary 실패 job `201`은 terminal `ERROR` DB row와 `99_errors/260519OOP_1.m4a`를 증거로 보존했다. 관련 stale tmp wav는 backup 후 제거했고 report는 `state/reports/stt-error-cleanup-20260519T123547Z.json`이다.
- Runtime cleanup은 destructive apply 없이 dry-run/list만 수행했다. Report directory는 `state/reports/runtime-cleanup-20260519T123847Z`이며 repo-local stale tmp 6 files, legacy root `/Users/geonha/lecture_stt`, repo/state/runtime logs inventory를 기록했다. iCloud audio/transcript bulk deletion과 log/archive prune은 적용하지 않았다.
- canonical STT model은 계속 `large-v3`다.

### Runtime migration, VAD compatibility fix, launchd canary 완료
- 승인된 runtime migration을 적용해 STT tmp는 `~/Library/Caches/lecture_stt/tmp`, STT/downstream/webpanel/cleanup launchd stdout/stderr와 app/downstream logs는 `~/Library/Logs/lecture_stt`로 이동했다. DB는 계속 repo 내부 `state/jobs.sqlite3`에 유지한다.
- migration backup은 `state/backups/runtime-migration-20260519T114522Z`에 보존했다. post-migration log rotation은 dry-run만 수행했고 결과는 `state/reports/log-rotation-post-migration-dry-run-20260519T114610Z.txt`에 남겼다.
- runtime package workstream 결과로 production `.venv`와 `requirements.txt`는 `faster-whisper==1.2.1`로 정렬됐다. 이는 package-only 변경이며 canonical STT model은 계속 `large-v3`다.
- 1차 copied canary `260519OOP_1`은 launchd가 처리했지만 현재 `faster-whisper`의 `VadOptions` API가 `onset/offset` 대신 `threshold`를 받는 차이로 job `201`이 terminal `ERROR`가 됐다. 실패 row와 `99_errors/260519OOP_1.m4a`는 증거로 보존하고 정리하지 않았다.
- 회귀 테스트를 먼저 추가한 뒤 `STTWorker` VAD parameter builder가 faster-whisper 1.1/1.2 계열 signature를 모두 지원하도록 수정했다.
- 승인 후 `com.geonha.lecture-stt`만 kickstart 재시작했고, 2차 copied canary `260519OOP_2`는 job `202`로 `DONE` 처리됐다. 산출물은 `01_audio/260519OOP_2.m4a`, `02_transcripts/260519OOP_2.txt`, `02_transcripts/260519OOP_2.json`이며 preview는 `컴퓨터공학 전공 수업입니다.`다.
- canary report는 `state/reports/canary-20260519T120017Z.json`에 저장했다. 처리 시간은 total `7.93s`, transcribe `7.87s`, 관찰 elapsed `110.1s`였다.
- 최종 verification은 `state/reports/verification-unittest-20260519T121554Z.log` 기준 unittest 108개 통과, compileall 통과, `git diff --check` 통과, added-line static scan finding 0건이다. 2차 독립 review도 blocking issue 없이 통과했다.

### 남은 고도화 사용자 결정 반영
- 사용자가 지정한 `C/D/R/L/P/B/O` 결정값을 `.hermes/plans/2026-05-19_160615-lecture-stt-remaining-hardening.md`의 `User Decision Snapshot`에 반영했다.
- 결정 경계는 read-only/dry-run 우선, canary 선행, downstream은 report/manual table만, runtime migration은 canary 이후, log cleanup은 dry-run/manifest만, benchmark 확대는 보류로 정리했다.
- `P1=2`, `P2=2`, `P3=3`에 따라 `faster-whisper==1.2.1`은 production `.venv` upgrade 후보로 계획/rollback까지 검토하되, 실제 package 변경은 별도 승인 전 금지로 명시했다.
- canonical STT model은 계속 `large-v3`로 유지하며, DB/file/config/launchd/package/model 변경, local commit, push/tag/release는 수행하지 않았다.

### 남은 고도화 업무 결정 가이드 추가
- 최초 고도화 계획에서 남은 canary, downstream 정리, runtime path migration, log cleanup, runtime package update, benchmark 확대, 문서/commit 정책 결정을 한 번에 검토할 수 있는 `docs/REMAINING_HARDENING_DECISION_GUIDE_2026-05-19.md`를 추가했다.
- canary, launchd, downstream, canonical, hash-conflict, route/rename-needed, dry-run/apply, runtime path migration, copytruncate, `.venv`, benchmark 같은 운영 용어를 사용자 결정용으로 풀어 설명했다.
- 이 문서는 의사결정 보조 자료이며, DB/file/config/launchd/package/git remote에 대한 실제 변경 승인은 포함하지 않는다.

### Pre-commit review blocker fixes
- 독립 리뷰에서 지적된 3개 blocker를 TDD로 재현한 뒤 수정했다.
- secret redaction은 `Authorization: Bearer ...`, `authorization=Bearer ...`, JWT-like token, `sk-...` 표면을 모두 `[REDACTED]` 처리하도록 보강했다.
- launchd plain log rotation은 active fd가 열린 상태에서도 정책이 맞도록 rename+touch 대신 copytruncate 방식으로 바꿨다.
- PROCESSING 상태에서 죽은 retry job은 `engine_params.transcription_failures/transcription_max_retries`를 기준으로 `전사 재시도 대기 n/max`를 복원해 다음 scan에서 즉시 재시도되게 했다.
- 관련 regression test 3개를 추가했고 전체 unittest/compileall/diff-check가 통과했다.

### Launchd canary readiness read-only 점검
- launchd read-only check에서 `com.geonha.lecture-stt`와 `com.geonha.lecture-stt-distribute`는 PID가 있는 running 상태로 확인했다. cleanup은 상시 PID가 없는 보조 서비스로 보이며 canary blocker로 보지 않는다.
- DB read-only check는 `integrity_check=ok`, `foreign_key_check=[]`, job status는 `DONE=29`, `PROCESSING=0`, retry 대기 row `0`, ERROR row `0`로 확인했다.
- downstream 기존 problem row 41건은 별도 conflict/rename 작업으로 남아 있으며, canary 중 DB clear/destination overwrite를 하지 않는 조건으로 blocker에서 제외한다.
- 현재 inbox는 비어 있어 다음 실제 강의 1개를 canary input으로 기다릴 수 있는 상태다.
- installed LaunchAgents와 운영 config는 아직 repo-local log/tmp path를 사용하므로, runtime path migration/restart 없이 현재 운영 기준으로 canary를 기다리는 것으로 문서화했다.

### Runtime path default와 migration/rollback 계획 보강
- macOS 표준 위치 정책에 맞춰 기본 log/tmp/cache helper를 추가했다. 기본 log는 `~/Library/Logs/lecture_stt`, 기본 tmp/cache는 `~/Library/Caches/lecture_stt/tmp`를 사용하고 DB는 repo 내부 `state/jobs.sqlite3`에 유지한다.
- STT/downstream default config와 `config/config.example.yaml`을 위 정책에 맞춰 보강했다.
- `docs/RUNTIME_MIGRATION_PLAN_2026-05-19.md`에 승인 전 read-only preflight, 승인 후 migration 초안, rollback 절차, 아직 하지 않는 작업을 분리해 문서화했다.
- 운영 `config/config.yaml`, installed LaunchAgents, 기존 repo log/tmp 파일, DB에는 손대지 않았다.

### Downstream conflict dry-run/report/repair flow 보강
- `lecture_stt.downstream.status diagnose`에 live source/destination existence/hash 기반 classification을 추가했다.
- classification은 `same-content-now`, `source-missing`, `dest-missing`, `hash-conflict`, `route/rename-needed`, `manual-review`로 나뉘며, route suffix가 붙은 stem은 proposed stem을 계산한다.
- `diagnose --json --report-path ...`로 machine-readable dry-run report를 `state/reports/` 같은 gitignored 경로에 저장할 수 있게 했다.
- DB-only/source-missing row clear는 `clear-stale`로 분리했다. 실제 적용은 `--yes`와 `--backup-path`가 모두 필요하며, DB backup을 만든 뒤 row 1개만 삭제한다.
- `260422LC`는 D3 결정에 맞춰 `clear-stale`에서도 document-only 예외로 거부한다.
- live read-only diagnose 결과는 41 problem rows로, 기존 triage와 동일하게 `260422LC`는 source-missing/document-only, 나머지는 hash conflict 또는 route/rename-needed로 분류됐다.
- 운영 DB/file에 대한 clear, overwrite, delete, migration은 수행하지 않았다.

### 로그 retention/rotation 정책 보강
- app log와 downstream JSONL 기본 rotation 값을 확정 정책에 맞춰 10MB x 5로 맞췄다.
- launchd stdout/stderr plain log에 대해 10MB x 3 정책을 dry-run/apply로 실행할 수 있는 `scripts/rotate_logs.py`를 추가했다. 기본은 dry-run이며, `--apply` 없이는 로그 파일을 변경하지 않는다.
- 압축 archive(`*.gz`)는 30일 보존 기준으로 dry-run/prune 할 수 있는 공통 helper를 추가했다.
- rotation helper, compressed archive retention, downstream 기본값, rotate script dry-run/apply 회귀 테스트를 추가했다.
- 기존 운영 로그 archive/cleanup, launchd restart, 실제 로그 변경은 수행하지 않았다.

### STT retry/failure policy 구현
- STT 실행 실패 job을 기본 2회까지 `PENDING` + `전사 재시도 대기 n/2` 상태로 남기고, 다음 scan에서 즉시 재시도하도록 구현했다.
- retryable job은 canonical audio를 `01_audio`에 유지하며, retry 한도 초과 시 terminal `ERROR`로 확정하고 원본 오디오는 `99_errors`로 이동한다.
- 실패 메시지/trace/알림 payload에는 secret-like 문자열을 `[REDACTED]`로 마스킹하도록 방어 로직을 추가했다.
- retry 상태 metadata는 DB schema migration 없이 `engine_params`의 `transcription_failures`, `transcription_max_retries`, `last_error_message`에 기록한다.
- transient success, terminal failure, startup recovery가 retryable job을 보존하는 회귀 테스트를 추가했다.
- 모델은 변경하지 않았고, launchd/운영 DB/iCloud 실제 artifact에는 손대지 않았다.

### 운영 결정사항 정리와 OPERATIONS runbook 추가
- 남은 고도화 workstream의 사용자 결정사항을 `.hermes/plans/2026-05-19_160615-lecture-stt-remaining-hardening.md`에 반영했다.
- `docs/OPERATIONS.md`를 현재 운영 기준 문서로 추가했다. quick checklist와 상세 runbook을 함께 두고, downstream conflict, retry/failure, log retention, runtime path, model/package, canary 기준을 분리해 정리했다.
- README 상단에 `docs/OPERATIONS.md` 링크와 legacy 경로 주의 문구를 추가했다.
- 코드, DB, launchd, production `.venv`는 변경하지 않았다.

### Claude/Anthropic 자동 교정 제거와 downstream problem row 전수 분류
- correction 단계는 유지하되 API-backed 자동 교정 provider를 비활성화하고 manual/provider-neutral 모드로 전환했다.
- `requirements.txt`에서 `anthropic` dependency를 제거하고, `config/config.yaml`/`config/config.example.yaml`의 Claude 전용 model/API key 설정을 `correction.mode: manual`로 대체했다.
- `CorrectionConfig`에서 API key/model/max token 필드를 제거하고, `CorrectionWorker`가 pending transcript pair를 correction output으로 쓰지 않고 `skipped`로 보고하도록 바꿨다.
- legacy `corrector.py`는 외부 API 호출 없이 provider-neutral helper와 disabled compatibility shim만 남겼다.
- API key 없이 설정을 로드하고 manual mode에서 source/correction 폴더를 변경하지 않는 회귀 테스트를 추가했다.
- 운영 DB의 downstream problem row 41건을 read-only로 전수 분류해 `docs/DOWNSTREAM_TRIAGE_2026-05-19.md`에 기록했다. 25건은 실제 source/destination hash conflict, 1건은 stale/DB-only conflict 의심, 8건은 invalid stem, 7건은 subject route/rename 확인 대상으로 분리했다.

### downstream 로그 폭주 완화와 모델 benchmark 초안
- downstream worker가 반복 conflict/blocked/error 문제를 매 scan마다 다시 stdout/JSONL에 쓰지 않도록, 프로세스 생애 동안 동일 문제 이벤트를 1회만 기록하는 suppression을 추가했다.
- scan 통계 stdout은 최초/변경/heartbeat 때만 출력하도록 `ScanStatsReporter`를 추가해 `downstream.out.log` 증가량을 줄였다.
- `JsonlLogger`에 opt-in size guard/rotation 기능을 추가했다. 기본 example은 안전하게 비활성(`log_jsonl_max_bytes: 0`)으로 두고, 운영 config에서는 별도 승인 후 크기 제한을 켜는 방식으로 분리했다.
- `downstream status summary`가 problem row 수와 `last_error_code`별 reason count를 함께 보여주도록 개선했다.
- `scripts/benchmark_models.py` 초안을 추가했다. 기본은 현재 config의 `large-v3` baseline plan만 실행 가능하고, 후보 모델은 `--allow-candidate`, 다운로드/cache miss는 `--allow-download` 없이는 진행하지 않는다. 실제 benchmark 실행은 transcript payload가 stdout에 노출되지 않도록 `--output`을 필수로 요구한다.
- 관련 unittest와 benchmark plan smoke check를 추가했다. 모델 다운로드/교체/실제 benchmark 실행은 수행하지 않았다.
- 후속 보강으로 반복 문제 suppression cache를 bounded set으로 바꾸고, `backup_count=0` JSONL rotation도 UUID suffix로 충돌 없이 여러 번 회전되도록 했다.
- `config/config.example.yaml`에는 downstream 로그 관련 안전 기본값을 문서화했고, gitignored 운영 `config/config.yaml`에는 승인된 범위에서 `log_jsonl_max_bytes: 10485760`, `log_jsonl_backup_count: 5`, `log_suppression_max_keys: 4096`, `log_routine_scan_events: false`를 적용했다.
- 기존 대용량 downstream 로그는 삭제하지 않고 `state/logs/archive/20260519T045929Z/` 아래 gzip으로 보존한 뒤 원본을 truncate했으며, `com.geonha.lecture-stt-distribute`를 재시작해 stdout 반복 폭주가 멈춘 것을 확인했다.
- 모델 변경은 baseline freeze → 실사용 sample shadow benchmark → 수동 품질판정 → 제한 canary 순서로 검증하도록 `docs/MODELS.md`에 절차를 추가했다.
- `large-v3-turbo`, `deepdml` turbo, `distil-large-v3`, `ghost613` Korean turbo 후보를 실제 짧은/중간 샘플로 비교했고, 속도 이득은 있었지만 전공 용어 오류·누락·hallucination 징후 때문에 canonical STT 기본값으로는 탈락시켰다.
- isolated `faster-whisper==1.2.1` 환경에서 같은 `large-v3`를 재측정했다. 속도는 개선됐지만 short sample 기준 출력 길이가 2338자에서 1742자로 줄고 도입부 누락/initial-prompt성 문장 삽입 징후가 있어 production `.venv` 업그레이드는 보류한다.

### runtime inventory 및 모델 최신성 live 조회
- read-only inventory 결과를 `docs/RUNTIME_INVENTORY_2026-05-19.md`에 저장했다.
- iCloud `lecture_recordings` 실제 구조, repo runtime 폴더, legacy `/Users/geonha/lecture_stt`, launchd/process, DB/deliveries 상태를 정리했다.
- 모델/패키지 최신성 조회 결과와 benchmark 후보를 `docs/MODELS.md`에 저장했다.
- 현행 `faster-whisper==1.1.0` 대비 최신 `1.2.1`이 있음을 확인했고, `large-v3-turbo`, CT2 turbo, MLX, Korean fine-tune 후보를 benchmark 후보로 분리했다.
- 삭제/이동/launchd 재시작/DB 변경/모델 변경은 수행하지 않았다.

### lecture workflow 답변 반영 및 handoff 갱신
- 사용자 답변을 바탕으로 iPhone 녹음 → iCloud `lecture_recordings/00_inbox` 업로드 → STT 전사 → 수동 LLM 교정/요약 흐름을 `docs/HANDOFF_RESTRUCTURE_AND_MODEL_UPGRADE.md`에 반영했다.
- Claude/Anthropic 제거 범위는 correction 단계 삭제가 아니라 Claude API 구현 제거로 조정하고, raw/corrected transcript와 summary는 계속 유지하는 방향으로 정리했다.
- Web panel 개편은 이번 작업 범위에서 제외하고, 모델 고도화는 정확도 최우선 + 컴퓨터 수용 가능성 제약 + 최신 후보 live 확인을 필수 gate로 갱신했다.
- canonical output의 의미와 iCloud/GH_archive/Obsidian 역할 확인 질문을 추가했다.

### 구조 정리 및 모델 고도화 handoff 문서 추가
- 장기 작업을 세션 간 이어가기 위해 `docs/HANDOFF_RESTRUCTURE_AND_MODEL_UPGRADE.md`를 추가했다.
- 사용자가 결정해야 할 경로 정책, Claude 제거 범위, downstream/summary 워크플로우, 모델 benchmark 기준을 분리해 기록했다.
- 삭제/이동/launchd 재시작/DB 변경은 승인 후 진행하도록 안전장치를 명시했다.

## 2026-04-29

### launchd 중복 STT worker 재시작 루프 완화
- `com.geonha.lecture-stt` LaunchAgent가 기존 외부 STT worker의 `state/stt.lock`을 만나면 10초마다 재실행 로그를 남기던 문제를 확인했다.
- launchd로 실행된 `scripts/run_worker.sh`는 `LECTURE_STT_LOCK_WAIT=1`을 기본 설정하도록 바꿨다.
- STT main lock은 해당 환경변수가 켜진 경우 non-blocking 실패로 종료하지 않고 lock을 기다리도록 확장했다.
- launchd job이 대기 프로세스 하나로 유지되므로 반복 로그를 멈추고, 기존 worker가 종료되면 새 job이 자연스럽게 lock을 이어받는다.
- lock 대기 모드 회귀 테스트를 추가했다.
- 전체 점검 중 downstream이 같은 invalid/incomplete/blocked 항목을 30초마다 반복 기록해 로그가 커지는 문제를 확인했다.
- downstream worker 프로세스 생애 동안 같은 반복 문제 이벤트는 한 번만 남기도록 줄이고 회귀 테스트를 추가했다.

## 2026-03-29

### 메인 워커 선점 레이스 완화와 로컬 staging 추가
- 메인 STT 워커 시작 시 `state/stt.lock` 파일 락을 잡아 launchd, 수동 실행, 제어판 실행이 겹쳐도 동시에 두 개 이상 돌지 않도록 막았다.
- inbox에서 안정 판정된 파일은 바로 `01_audio`로 가지 않고 `tmp/inbox_staging`으로 먼저 선점 이동한 뒤 canonical audio로 넘기도록 바꿔, iCloud rename/sync가 전사 본 처리 단계에 끼어드는 구간을 줄였다.
- claim 전에 원본이 사라진 경우는 최근 동일 source를 다른 워커가 이미 잡았는지 재확인하고, benign race나 외부 rename 가능성으로 판단되면 ERROR 대신 warning log만 남기고 skip하도록 완화했다.
- 워커 재시작 시 `tmp/inbox_staging`에 남은 파일을 inbox로 되돌리고, 대응되는 stale staging job을 정리하도록 보완했다.
- 추가로 `01_audio`에만 남은 pre-claim `PENDING` 오디오도 시작 복구 시 inbox로 되돌려, canonical move 직후 크래시가 영구 정체로 남지 않게 했다.
- scheduled cleanup이 `tmp/inbox_staging`을 삭제하지 않도록 예외 처리하고, 관련 STT/cleanup 회귀 테스트를 추가했다.

## 2026-03-27

### PyCharm 모듈 루트 설정 수정
- `.idea/lecture_stt.iml`의 module content root가 `.idea/`를 가리키고 있어 프로젝트 파일 트리가 비정상적으로 보일 수 있던 문제를 수정했다.
- module root를 저장소 루트 기준으로 바꾸고 `src/`, `.venv/` 경로도 같은 기준으로 다시 연결했다.

## 2026-03-26

### 경로 설정 정책 정리와 사용자 절대경로 제거
- STT/downstream/cleanup/UI가 공통 경로 해석 규칙을 쓰도록 정리하고, `config.yaml` 경로 값에서 `~`와 `${VAR}` 확장을 지원하도록 맞췄다.
- 코드 기본값에서는 `state/`, `tmp/`, `logs/` 같은 repo 내부 경로만 남기고, iCloud/GH archive/Obsidian 같은 사용자 데이터 경로 fallback은 제거했다.
- `config/config.example.yaml`을 사용자 절대경로 템플릿에서 env-backed 예시로 바꾸고, launchd plist도 repo 절대경로 대신 `__REPO_ROOT__` 템플릿으로 바꿨다.
- `setup_launchd.sh`는 ffmpeg를 고정 Homebrew 경로 대신 `config -> FFMPEG_BINARY -> PATH` 순으로 찾도록 수정했다.
- 경로 해석 회귀를 막기 위해 control command, 웹 패널 placeholder, `.env` 기반 패널 경로 해석 테스트를 추가했다.

### 스크립트 엔트리포인트 회귀 수정
- `scripts/cleanup.py`가 launchd처럼 직접 파일 실행될 때도 `src/`를 import 경로에 올려 정상 동작하도록 부트스트랩을 추가했다.
- `scripts/ab_test.py`는 기본 `ffmpeg` 값을 그대로 넘기지 않고 실제 실행 파일 경로로 해석한 뒤 `STTWorker`에 주입하도록 수정했다.
- cleanup 직접 실행과 `ab_test.py` ffmpeg 경로 해석을 검증하는 회귀 테스트를 추가했다.

## 2026-03-23

### 웹 패널 알림 채널 선택 UI 추가
- 웹 패널 snapshot과 API 계약에 `notification` 상태와 저장 endpoint를 추가하고, `config.yaml`의 `notification` 섹션을 패널에서 수정할 수 있게 했다.
- React 패널 상단에 Notification 카드를 추가해 텔레그램만, 디스코드만, 둘 다, 끄기 중 하나를 선택하고 저장할 수 있게 했으며, `.env` secret이 없는 선택지는 비활성화했다.
- 저장은 기본적으로 안전한 `다음 재시작부터 적용` 모델로 두고, 필요하면 같은 카드에서 `저장 후 재시작`으로 현재 워커에 즉시 반영할 수 있게 했다.

### STT 알림 채널 텔레그램 전환 기반 추가
- `DiscordNotifier` 단일 구현을 provider 기반 notifier 팩토리로 바꾸고, Telegram/Discord/Noop 및 다중 채널 전송을 지원하도록 정리했다.
- `notification` 설정 섹션과 `.env` secret 조합으로 채널을 선택하게 바꾸고, provider별 중복 마커를 따로 써서 전환 기간의 shadow 전송이 막히지 않도록 했다.
- 텔레그램 payload, auto provider 선택, dual-send 중복 방지에 대한 Python 단위 테스트를 추가하고 운영 문서와 예시 설정을 갱신했다.

### STT 품질 경고/진행률/ETA 현실화
- 품질 게이트를 단순 반복도 기준에서 벗어나 반복 비율, 빈 구간, 기호 노이즈, 짧은 발화 비율, 평균 길이를 함께 보는 복합 점수 방식으로 보강했다.
- `faster-whisper` 세그먼트 스트림을 이용해 전사 중간 진행률을 계속 갱신하고, WAV 길이 대비 처리 구간과 실제 처리 속도로 남은 ETA를 재계산하도록 바꿨다.
- 후처리, 품질 검사, 산출물 저장 단계의 진행률/ETA도 별도 단계 값으로 정리하고, 관련 Python 단위 테스트를 추가했다.

### React 웹 패널 최근 작업/운영 로그 통합
- 접근성을 높이기 위해 `최근 작업` 테이블과 `운영 로그` 카드를 하나의 Activity 패널로 통합했다.
- 최근 작업 행을 선택하면 같은 파일 기준의 운영 이벤트와 오류를 바로 아래에서 볼 수 있도록 바꿔, 화면 이동 없이 작업 맥락을 유지하게 했다.
- raw log, auto-follow, 이력 초기화, 패널 종료 동작은 유지하고, 통합 패널에 맞는 선택 행/상세 영역 스타일만 추가했다.

### React 웹 패널 운영 로그 파서/한국어 로그 뷰 추가
- 로그 패널에 `운영 뷰`, `오류만`, `원본 로그` 전환을 추가하고, 반복되는 STT 로그 패턴을 한국어 운영 이벤트로 정리해 보여주도록 확장했다.
- 파일명 규칙 `YYMMDD + 과목코드 + 선택적 _N + 확장자`를 파싱해 과목명, 날짜, 요일, 교시를 표시하고 `Dstr`는 `DStr`로 정규화했다.
- raw log 수집 계층은 유지하고, `logParser.ts`와 parser 테스트만 추가해 최소 침습적으로 표시 계층을 개선했다.

### React 웹 패널 다크 모드 추가
- `localStorage`에 저장되는 light/dark 테마 훅을 추가하고, 운영 헤더에서 라이트/다크 모드를 전환할 수 있도록 연결했다.
- 기존 레이아웃과 컴포넌트 구조는 유지한 채 CSS 변수를 dark override로 확장해, 카드/테이블/로그/에러 배너까지 동일한 시각 언어로 다크 모드를 적용했다.
- 테마 초기화와 토글 persist 동작을 검증하는 프론트 훅 테스트를 추가했다.

### React 웹 패널 테마 토글 UI 보정
- 상단의 큰 테마 버튼을 제거하고, 상태 배지와 어울리는 작은 pill 스위치로 바꿨다.
- 라이트/다크 두 옵션이 같은 캡슐 안에 들어가고, 현재 선택값은 좌우로 이동하는 슬라이딩 thumb로 표시되도록 조정했다.

### React 웹 패널 다크모드 카드/토글 미세 조정
- `Queue` 카드의 `muted-card` 배경이 다크모드 override를 타지 않던 문제를 CSS 변수로 정리했다.
- 테마 스위치를 더 작고 조밀한 iOS-style 캡슐 비율로 줄이고, thumb 그림자와 이동감을 보정했다.

### React 웹 패널 테마 스위치 아이콘형 재조정
- 상단 테마 스위치를 텍스트 세그먼트에서 icon-only 토글로 다시 줄이고, `실행중` 상태 배지와 맞는 높이의 캡슐 스위치로 정리했다.
- 라이트/다크 구분은 텍스트 대신 sun/moon SVG 아이콘으로 바꾸고, 슬라이딩 thumb는 한 번 클릭할 때 좌우로 이동하도록 단순화했다.

### React 웹 패널 기본 액션 버튼 팔레트 고정
- `시작`과 `일시정지/재개`가 기본 버튼 색을 써서 라이트/다크 전환 시 반전돼 보이던 문제를 수정했다.
- 기본 액션 버튼 팔레트를 테마와 무관한 slate 계열로 고정해, 운영 액션 색이 모드 전환에 따라 뒤집히지 않도록 맞췄다.

### React 웹 패널 액션 버튼/테마 전환 속도 재보정
- 기본 액션 버튼 팔레트를 다시 slate 계열로 되돌려, 기존 운영 패널 톤을 유지하면서도 라이트/다크 전환 시 색이 뒤집히지 않게 조정했다.
- 테마 전환이 너무 급하게 느껴지지 않도록 body, 카드, 메타 카드, 로그, 에러 배너, 스위치 thumb의 transition 시간을 늘렸다.

### React 웹 패널 런타임 액션 버튼 톤 정렬
- `시작`과 `일시정지/재개`가 여전히 기본 버튼 톤을 써서 `동기화`와 다르게 보이던 문제를 수정했다.
- Runtime 카드의 비파괴 액션 세 개를 모두 `secondary` 톤으로 통일해, `동기화` 버튼 기준의 동일한 시각 규칙으로 맞췄다.

### 변경 파일
- `frontend/web-panel/src/hooks/useThemeMode.ts`
- `frontend/web-panel/src/App.tsx`
- `frontend/web-panel/src/components/panel/PanelHero.tsx`
- `frontend/web-panel/src/styles.css`
- `frontend/web-panel/test/useThemeMode.test.tsx`
- `docs/WORKLOG.md`

### React 웹 패널 리뷰 지적 후속 수정
- `refresh/start/pause/resume/stop` 같은 일반 action 이후 로그 버퍼가 지워지던 회귀를 수정하고, `clear_history`일 때만 로그 reset 키가 증가하도록 고쳤다.
- SSE decode 실패가 한 번 발생하면 realtime이 영구 중단되던 문제를 수정해, 스트림을 정리한 뒤 자동 재연결을 시도하도록 바꿨다.
- 로그 reset 이후에도 이전 세션의 hidden-line 수치가 남던 문제를 수정하고 관련 프론트 테스트를 추가했다.

### 변경 파일
- `frontend/web-panel/src/hooks/usePanelState.ts`
- `frontend/web-panel/src/hooks/usePanelLogs.ts`
- `frontend/web-panel/src/lib/panelEvents.ts`
- `frontend/web-panel/test/usePanelState.test.tsx`
- `frontend/web-panel/test/usePanelLogs.test.tsx`
- `frontend/web-panel/test/panelEvents.test.ts`
- `src/lecture_stt/ui/web_panel.py`
- `docs/WORKLOG.md`

### React 웹 패널 상단 메타 카드 보정
- 상단 헤더의 `동기화 정책` 카드를 제거하고, 클라이언트 기준 `현재 시간`과 `실시간 연결됨/fallback polling 중` 상태를 보여주도록 바꿨다.
- `업데이트 시간`은 마지막 상태 변경 시각으로 유지하고, 현재 시각은 별도 카드로 분리해 의미 혼선을 줄였다.

### 변경 파일
- `frontend/web-panel/src/hooks/usePanelState.ts`
- `frontend/web-panel/src/App.tsx`
- `frontend/web-panel/src/components/panel/PanelHero.tsx`
- `frontend/web-panel/src/styles.css`
- `docs/WORKLOG.md`

### React 웹 패널 SSE 실시간 갱신 도입
- `/api/events` SSE 스트림을 추가해 React 패널이 `state`, `log_chunk`, `log_reset` 이벤트를 서버 push로 받도록 확장했다.
- 프론트에 `panelEvents` adapter/service를 추가하고, `usePanelState`와 `usePanelLogs`가 SSE 연결 시 polling을 멈추고 끊기면 기존 polling으로 자동 fallback 하도록 정리했다.
- 로그 truncate/reset 안전성을 위해 `ControlState._log_stream_delta()`를 추가하고, 관련 Python/React 테스트를 보강했다.

### 변경 파일
- `src/lecture_stt/ui/web_panel.py`
- `src/lecture_stt/ui/web_panel_state.py`
- `frontend/web-panel/src/types.ts`
- `frontend/web-panel/src/lib/panelApi.ts`
- `frontend/web-panel/src/lib/panelEvents.ts`
- `frontend/web-panel/src/hooks/usePanelState.ts`
- `frontend/web-panel/src/hooks/usePanelLogs.ts`
- `frontend/web-panel/test/usePanelState.test.tsx`
- `frontend/web-panel/test/usePanelLogs.test.tsx`
- `tests/test_web_panel.py`
- `tests/test_web_panel_state.py`
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`

### React 웹 패널 운영형 대시보드 디자인 개편
- `shadcn/ui`의 dashboard/card/badge/alert/table 레퍼런스를 참고하되, Tailwind나 컴포넌트 스택 교체 없이 기존 로컬 React/CSS 구조만으로 운영형 대시보드 톤으로 재정렬했다.
- 소개형 hero를 얇은 운영 헤더로 바꾸고, 상단 요약 카드와 본문 2열 레이아웃, 경고 alert, 상태 pill, 진행률 bar, 터미널형 로그 패널을 적용했다.
- 기능 경계나 데이터 흐름은 건드리지 않고 `App.tsx`, panel/ui 컴포넌트, `styles.css`만으로 시각 계층과 밀도를 조정했다.

### 변경 파일
- `frontend/web-panel/src/App.tsx`
- `frontend/web-panel/src/components/panel/PanelHero.tsx`
- `frontend/web-panel/src/components/panel/RuntimeCard.tsx`
- `frontend/web-panel/src/components/panel/ProcessingCard.tsx`
- `frontend/web-panel/src/components/panel/JobsTableCard.tsx`
- `frontend/web-panel/src/components/panel/LogPanelCard.tsx`
- `frontend/web-panel/src/components/ui/SectionCard.tsx`
- `frontend/web-panel/src/components/ui/ActionBar.tsx`
- `frontend/web-panel/src/components/ui/ErrorBanner.tsx`
- `frontend/web-panel/src/components/ui/LoadingBlock.tsx`
- `frontend/web-panel/src/components/ui/ErrorState.tsx`
- `frontend/web-panel/src/styles.css`
- `docs/WORKLOG.md`

### React 웹 패널 최종 검증 준비와 소형 테스트 보강
- `vitest + jsdom + @testing-library/react` 기반의 최소 프론트 테스트 러너를 추가했다.
- `decodePanelState`, `usePanelLogs`, `usePanelState`에 대해 decoder 검증, 로그 polling/trim/auto-follow, action 후 refetch를 확인하는 소형 단위 테스트를 보강했다.
- `README.md`에 `/app` 수동 검증 체크리스트와 `package-lock.json`/`dist/` 커밋 기준을 명시했다.

### 변경 파일
- `frontend/web-panel/package.json`
- `frontend/web-panel/package-lock.json`
- `frontend/web-panel/vitest.config.ts`
- `frontend/web-panel/test/setup.ts`
- `frontend/web-panel/test/decodePanelState.test.ts`
- `frontend/web-panel/test/usePanelLogs.test.tsx`
- `frontend/web-panel/test/usePanelState.test.tsx`
- `README.md`
- `docs/WORKLOG.md`

## 2026-03-22

### React 웹 패널 1차 최소 침습 리팩터링
- `App.tsx`에 몰려 있던 상태 조회, action 실행, logs polling, 화면 렌더링을 `panelApi`, decoder, hooks, local UI/panel components로 분리했다.
- panel state 조회와 action 이후 refetch/invalidation에 TanStack Query를 적용하고, logs는 기존 byte-offset polling을 custom hook으로 유지했다.
- React 패널이 백엔드 `actions.endpoints` 계약을 실제로 소비하도록 바꾸고, 로그 auto-follow, manual scroll 감지, trim 메타 표시를 추가했다.
- Vite dev server에 `/api`, `/action` 프록시를 추가하고, 문서와 backend contract 테스트를 보강했다.

### 변경 파일
- `frontend/web-panel/package.json`
- `frontend/web-panel/vite.config.ts`
- `frontend/web-panel/src/main.tsx`
- `frontend/web-panel/src/App.tsx`
- `frontend/web-panel/src/api.ts`
- `frontend/web-panel/src/types.ts`
- `frontend/web-panel/src/styles.css`
- `frontend/web-panel/src/lib/panelApi.ts`
- `frontend/web-panel/src/lib/decodePanelState.ts`
- `frontend/web-panel/src/hooks/usePanelState.ts`
- `frontend/web-panel/src/hooks/usePanelLogs.ts`
- `frontend/web-panel/src/components/panel/PanelHero.tsx`
- `frontend/web-panel/src/components/panel/RuntimeCard.tsx`
- `frontend/web-panel/src/components/panel/ProcessingCard.tsx`
- `frontend/web-panel/src/components/panel/JobsTableCard.tsx`
- `frontend/web-panel/src/components/panel/LogPanelCard.tsx`
- `frontend/web-panel/src/components/ui/SectionCard.tsx`
- `frontend/web-panel/src/components/ui/StatusBadge.tsx`
- `frontend/web-panel/src/components/ui/DefinitionGrid.tsx`
- `frontend/web-panel/src/components/ui/MetricList.tsx`
- `frontend/web-panel/src/components/ui/ActionBar.tsx`
- `frontend/web-panel/src/components/ui/LoadingBlock.tsx`
- `frontend/web-panel/src/components/ui/EmptyState.tsx`
- `frontend/web-panel/src/components/ui/ErrorState.tsx`
- `frontend/web-panel/src/components/ui/ErrorBanner.tsx`
- `tests/test_web_panel.py`
- `tests/test_web_panel_state.py`
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`

### React 웹 패널 0단계 착수
- `web_panel_state.snapshot()`에 React 친화형 구조화 응답(`schema_version`, `runtime_state`, `actions`, `jobs_v2`, `processing_v2`, `summary`)을 추가했다.
- 기존 레거시 SSR 패널은 `/`에서 유지하고, React 빌드가 있으면 `/app`에서 정적 자산을 서빙하도록 `web_panel.py`를 확장했다.
- `frontend/web-panel/`에 Vite + React + TypeScript 골격과 API 클라이언트, 기본 화면, 스타일 파일을 추가했다.
- React 빌드가 없을 때 `/app`에서 안내 placeholder를 보여주도록 했다.
- React용 snapshot 계약과 `/app` placeholder 동작에 대한 테스트를 추가했다.

### 변경 파일
- `src/lecture_stt/ui/web_panel_state.py`
- `src/lecture_stt/ui/web_panel.py`
- `frontend/web-panel/package.json`
- `frontend/web-panel/tsconfig.json`
- `frontend/web-panel/tsconfig.app.json`
- `frontend/web-panel/tsconfig.node.json`
- `frontend/web-panel/vite.config.ts`
- `frontend/web-panel/index.html`
- `frontend/web-panel/src/main.tsx`
- `frontend/web-panel/src/App.tsx`
- `frontend/web-panel/src/api.ts`
- `frontend/web-panel/src/types.ts`
- `frontend/web-panel/src/styles.css`
- `tests/test_web_panel.py`
- `tests/test_web_panel_state.py`
- `.gitignore`
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`

### 웹 패널 제어 계층 분리
- React 전환 전 준비로 웹 패널의 상태/워커 제어 로직을 `web_panel_state.py`로 분리했다.
- `web_panel.py`는 HTML 렌더링, HTTP 핸들러, 서버 시작에 집중하는 엔트리포인트로 정리했다.
- 구조 문서에 새 UI 계층 분리와 현재 테스트 범위를 반영했다.

### 변경 파일
- `src/lecture_stt/ui/web_panel.py`
- `src/lecture_stt/ui/web_panel_state.py`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`

### STT 제어/웹 패널 운영 버그 수정
- `lecture_stt.stt.main`의 `--pause`, `--resume`, `--status`가 `--config`의 `paths.db_path`를 기준으로 pause 플래그를 보도록 수정했다.
- 웹 제어판의 `종료` 버튼이 워커 중지와 전체 이력 삭제를 수행하던 동작을 제거하고, 제어판 서버 종료만 하도록 고쳤다.
- STT control command와 웹 패널 종료 동작에 대한 회귀 테스트를 추가했다.

### 변경 파일
- `src/lecture_stt/stt/main.py`
- `src/lecture_stt/ui/web_panel.py`
- `tests/test_stt_main.py`
- `tests/test_web_panel.py`
- `docs/WORKLOG.md`

### Codex 서브에이전트 도입
- 프로젝트 로컬 `.codex/agents/` 디렉터리를 만들고 작업 성격에 맞는 기본 서브에이전트 8종을 추가했다.
- 리액트 웹 패널 전환을 대비해 `react-specialist`, `frontend-developer`, `typescript-pro`, `browser-debugger`를 기본 세트에 포함했다.
- 저장소 작업 흐름 문서 `AGENTS.md`에 서브에이전트 호출 기준과 추천 사용 순서를 추가했다.

### 변경 파일
- `.codex/agents/react-specialist.toml`
- `.codex/agents/typescript-pro.toml`
- `.codex/agents/frontend-developer.toml`
- `.codex/agents/browser-debugger.toml`
- `.codex/agents/reviewer.toml`
- `.codex/agents/test-automator.toml`
- `.codex/agents/code-mapper.toml`
- `.codex/agents/python-pro.toml`
- `AGENTS.md`
- `docs/WORKLOG.md`

### 리뷰 지적 수정
- `lecture_stt.downstream.status`가 `--db-path` 사용 시 worker/yaml을 선행 import하지 않도록 지연 import로 수정했다.
- `tests/test_distribute_status.py`에 `--db-path`가 worker config import를 건너뛰는 회귀 테스트를 추가했다.
- `scripts/setup_launchd.sh`가 launchd plist를 설치 시점의 저장소 경로로 렌더링하도록 수정했다.

### 변경 파일
- `src/lecture_stt/downstream/status.py`
- `tests/test_distribute_status.py`
- `scripts/setup_launchd.sh`
- `docs/WORKLOG.md`

### 2차 전면 전환
- 운영 진입점을 flat `src/*.py` 직접 실행에서 패키지 모듈 실행 방식으로 바꿨다.
- 새 메인 스크립트 `scripts/run_worker.sh`를 추가했다.
- `run_gui.sh`, `run_distribute.sh`, `distribute_once.sh`, `distribute_status.sh`, `run_once.sh`를 새 패키지 경로 기준으로 갱신했다.
- 웹/Tk 제어판이 워커를 모듈 이름 기준으로 시작하고 PID를 찾도록 수정했다.
- 테스트 import를 새 패키지 경로로 갱신했다.
- 기존 flat `src/*.py` wrapper를 제거했다.

### 변경 파일
- `scripts/run_worker.sh`
- `scripts/run_once.sh`
- `scripts/run_gui.sh`
- `scripts/run_distribute.sh`
- `scripts/distribute_once.sh`
- `scripts/distribute_status.sh`
- `scripts/ab_test.py`
- `scripts/cleanup.py`
- `scripts/setup_launchd.sh`
- `launchd/com.geonha.lecture-stt.plist`
- `src/lecture_stt/shared/paths.py`
- `src/lecture_stt/shared/utils.py`
- `src/lecture_stt/shared/db.py`
- `src/lecture_stt/stt/main.py`
- `src/lecture_stt/stt/watcher.py`
- `src/lecture_stt/stt/transcribe.py`
- `src/lecture_stt/stt/notifier.py`
- `src/lecture_stt/downstream/lib.py`
- `src/lecture_stt/downstream/worker.py`
- `src/lecture_stt/downstream/status.py`
- `src/lecture_stt/ui/web_panel.py`
- `src/lecture_stt/ui/tk_panel.py`
- `tests/test_distribute_lib.py`
- `tests/test_distribute_status.py`
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`

### 1차 구조 개편
- 실제 구현 코드를 `src/lecture_stt/` 패키지로 재배치했다.
- 하위 구조를 `stt`, `shared`, `downstream`, `ui`로 분리했다.
- 기존 `src/*.py` 경로는 launchd, 스크립트, 테스트 호환성을 위해 wrapper로 유지했다.
- `main.py`, `distribute_worker.py`의 repo root 계산을 새 위치 기준으로 보정했다.
- 런타임 경로 호환성을 유지하면서 소스 구조만 정리한 1차 개편이다.

### 변경 파일
- `src/lecture_stt/__init__.py`
- `src/lecture_stt/stt/__init__.py`
- `src/lecture_stt/stt/main.py`
- `src/lecture_stt/stt/watcher.py`
- `src/lecture_stt/stt/transcribe.py`
- `src/lecture_stt/stt/postprocess.py`
- `src/lecture_stt/stt/quality_gate.py`
- `src/lecture_stt/stt/notifier.py`
- `src/lecture_stt/shared/__init__.py`
- `src/lecture_stt/shared/db.py`
- `src/lecture_stt/shared/utils.py`
- `src/lecture_stt/downstream/__init__.py`
- `src/lecture_stt/downstream/lib.py`
- `src/lecture_stt/downstream/worker.py`
- `src/lecture_stt/downstream/status.py`
- `src/lecture_stt/ui/__init__.py`
- `src/lecture_stt/ui/web_panel.py`
- `src/lecture_stt/ui/tk_panel.py`
- `src/main.py`
- `src/watcher.py`
- `src/transcribe.py`
- `src/postprocess.py`
- `src/quality_gate.py`
- `src/notifier.py`
- `src/db.py`
- `src/utils.py`
- `src/distribute_lib.py`
- `src/distribute_worker.py`
- `src/distribute_status.py`
- `src/web_control_panel.py`
- `src/control_panel.py`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`

### 구조 문서화 및 작업 워크플로우 도입
- 저장소 구조와 실행 흐름을 정리한 `docs/ARCHITECTURE.md`를 추가했다.
- 누적 변경 기록 파일 `docs/WORKLOG.md`를 추가했다.
- 앞으로의 작업 순서를 고정하기 위해 `AGENTS.md`를 추가했다.
- `README.md`에 구조 문서와 변경 기록 링크를 추가했다.
- 런타임 동작 변경은 없다.

### 2026-03-23
- 웹 패널 알림 설정 UI를 카드형에서 상단 인라인 순환 토글로 바꿨다.
- 토글 버튼을 누를 때 `둘 다 -> 디스코드 -> 텔레그램 -> 끄기` 순으로 즉시 저장되게 정리했다.
- 저장 후 별도 `적용` 버튼을 누르지 않아도, 실행 중 워커가 있으면 같은 요청 안에서 즉시 재시작 적용되도록 바꿨다.
- 구형 웹 패널 백엔드와 연결된 경우에도 `미지원` 상태로 안전하게 보이도록 라벨을 보정했다.
- 토글 칩 라벨에서 `알림` 접두어를 제거해 더 짧게 보이도록 정리했다.
- 알림 토글은 텍스트 대신 Telegram/Discord 마크 중심의 인라인 SVG 칩으로 바꾸고, `둘 다` 상태는 두 아이콘을 함께 보여주도록 정리했다.

### 변경 파일
- `AGENTS.md`
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/WORKLOG.md`

### 2026-07-25
- Storage V2 전사 분석 API에 모니터링 수집형(Prometheus 텍스트) 출력을 추가했다.
- `/api/storage-v2/analytics/transcriptions/metrics` 라우트를 추가해 period(day/week/month) 기반 시계열 메트릭을 plain-text로 제공하게 했다.
- 상태 레이어에 `transcription_analytics_prometheus` 변환 함수를 추가해 점수 분포/양/분류 메트릭을 텍스트로 변환하도록 했다.
- 웹 패널 라우팅 테스트와 상태 변환 테스트에 Prometheus 경로 검증 케이스를 추가했다.
- Prometheus 출력은 family별 HELP/TYPE을 한 번만 내보내고 정수 `20`을 `2`로 훼손하던 문자열 trim을 제거했다. 알 수 없는 query key를 400으로 닫고 `text/plain; version=0.0.4; charset=utf-8`를 사용한다. 전체 작업 수 gauge는 counter로 오해되는 `_total` suffix 대신 `lecture_stt_transcriptions_jobs_in_window_overall`로 분리했다.
- Archive evidence와 시간표 목록 API도 허용 query key만 받고 각 값을 한 번만 허용하도록 보강했다. 알 수 없는 key, 중복 status/semester/limit/offset은 state 계층을 호출하기 전에 400으로 닫는다.

#### Server-canonical Storage v2 통합 검토 feed

- `src/lecture_stt/storage_v2/unified_review.py`와 `GET /api/storage-v2/review-feed?limit=&offset=`를 추가했다. 기존 archive evidence, timetable, recording library gate를 그대로 사용하며 모든 source가 꺼졌거나 DB가 없으면 파일을 만들지 않고 unavailable ledger로 닫는다. 활성 경로는 Storage v2 DB를 read-only로 열고 같은 transaction에서 exact source count와 stable page를 계산한다.
- Timetable suggested proposal이 가리키는 `review_item_id` 전체 집합을 `DISTINCT`로 만든 뒤 recording open review에서 먼저 차감한다. 따라서 첫 page 밖 proposal도 recording row에 중복 반영되지 않는다. SQL CTE/`UNION ALL`/`LIMIT`/`OFFSET`을 사용해 Python에는 현재 page만 올리고, source ledger에는 page visible count와 dedup 후 exact total/truncation을 분리한다.
- 격리 임시 DB에 recording/review/proposal 2,000세트를 추가한 단일 로컬 측정에서는 total 2,007건의 offset 1,000/page 100 응답이 7.51ms였고 Python 응답 item 수는 100건으로 유지됐다. 이는 운영 성능 보장이 아니라 CTE와 page 메모리 경계가 의도대로 작동하는지 확인한 canary다.
- 응답은 canonical item ID/deep link, 해석 가능한 ISO timestamp, JS-safe count와 NFC 표시 문자열을 재검증한다. Numeric DB ID, 원본·artifact 경로, digest, transcript·교정·요약 본문과 review detail JSON은 반환하지 않는다. Query key·중복·빈 값·canonical decimal을 엄격히 검사하고 내부 오류는 경로를 숨긴 503으로 반환한다.
- React `useUnifiedReviewFeed`는 세 list query와 client-side sort/dedup을 제거하고 unified endpoint 한 개만 읽는다. 서버 순서를 그대로 표시하며 page당 100건의 이전/다음 이동, exact total/source ledger, 첫 page 밖 deep-link workbench를 유지한다. Queue가 비워진 뒤 이전 offset이 남는 경우도 빈 server page를 받아 offset 0으로 복구한다.
- Unified feed 자체에는 write action을 추가하지 않았다. 상태 변경·reject·confirmation·canonical promotion은 기존 source workbench의 기본 비활성 설정과 plan/count/digest/allow-write guard를 계속 사용한다. 운영 DB/root, launchd, worker, iCloud 원본에는 연결·migration·cutover·restart하지 않았고 Git stage/commit/push도 수행하지 않았다.
- 최종 자동 검증은 Storage v2 unittest 229개, 전체 Python unittest 499개, frontend Vitest 27파일 197개, 독립 `tsc -b`, Vite production build, Python `compileall`, `git diff --check`를 통과했다.
- 실제 Chromium 1차 fake API canary에서 홈 unified request 0, `#review` mount unified GET 1, 기존 세 list GET 0, page offset 100/0 왕복, 한국어 NFC, POST/upload 0, console 문제 0, 1280×900·390×844 page overflow 0을 확인했다. 이어 실제 `RequestHandler`와 실제 read-only reader를 migrated 임시 DB에 연결한 canary에서도 첫/둘째 page 200, mount GET 1, unknown query 400, 한국어/NFC, 390px overflow·console·write 0을 확인했다.
- 독립 reviewer가 storage 내부 `ValueError`까지 raw 400으로 노출되던 Medium 경계를 찾아 query parsing 400과 feed 생성의 sanitized 503을 분리했다. RuntimeError/internal ValueError 회귀를 추가한 뒤 재검토에서 unified feed 범위의 High/Medium 잔존 이슈가 없음을 확인했다.

### Confirmed timetable canonical materialization

- `recording_classification_materializations` journal과 `classification_materialization.py`를 추가했다. Confirmation은 계속 audit-only이며, 별도 CLI-only plan/apply만 confirmed/resolved `unique_time_match`를 새 current title, selected class context와 record manifest에 반영한다.
- Apply는 기본 비활성 `--enable-materialization`, `--allow-write`, `expected_count=1`, plan이 출력한 canonical SHA-256을 모두 요구한다. 새 title/context는 inactive 상태로 먼저 저장하고 journal을 `prepared`로 commit한 뒤, manifest를 same-directory exclusive temp + file/directory fsync + `os.replace`로 바꾸고 두 번째 transaction에서 `applied`로 닫는다.
- Manifest write 전과 write 후/finalize 전 중단을 모두 같은 plan의 old/new manifest digest로 forward recovery한다. 중간 상태가 존재한 뒤 실패한 CLI는 pre-write refusal(exit 2)과 구분되는 recovery-required(exit 3)를 반환한다. Post-commit verifier 실패도 별도 예외로 노출한다.
- Materialization plan과 context provenance는 closed metadata-only schema/domain으로 검증한다. Verifier는 journal digest, confirmed proposal, title/context revision 내용과 chain/selection, latest manifest digest를 교차 검사하며 `prepared`를 recovery-required issue로 보고한다. Proposal 상세의 `canonical_metadata_changed`도 applied journal 기준으로 계산한다.
- Proposal의 현재 필드에서 target metadata를 apply/finalize 직전에 다시 계산하므로 같은 timestamp를 재사용한 stale 변경도 차단한다. Proposal touch trigger는 column-scoped inner update로 바꿔 `recursive_triggers=ON`에서도 재귀하지 않으며 millisecond timestamp를 기록한다.
- Storage v2 read-only DB 연결도 final pathname의 symlink, hardlink, 비정규 main DB와 unsafe sidecar를 거부하고 SQLite가 실제로 연 inode를 guard descriptor와 대조한다.
- 새 격리 테스트는 read-only plan, 모든 write guard, 성공/멱등 적용, journal 변조, hard-linked manifest, 교체된 timetable, stale proposal, manifest 교체 전후 crash replay, recursive trigger, 후속 revision으로 대체된 journal의 멱등 재실행, preflight/post-commit verifier 오류 구분과 CLI recovery 상태를 임시 DB/root에서 검증한다.
- 검증 결과 Storage v2 221개, 전체 Python 488개, 관련 집중 Python 56개, frontend Vitest 183개가 통과했고 Python compileall, TypeScript build, Vite production build와 `git diff --check`도 통과했다. 격리 fake analytics 서버를 실제 Chromium으로 열어 month scrape의 한국어 label, 상태/품질/분류/전체량 series를 확인했고 HTTP 200과 Prometheus content type, unknown query 400을 확인했다. 브라우저 console에는 API와 무관한 favicon 404 한 건만 있었다.
- 최신 코드 기준 독립 재리뷰에서 materialization preflight/recovery 오류 구분, superseded successor chain, 목록 query fail-closed와 Prometheus exposition을 다시 확인했으며 요청 범위의 High/Medium 잔존 항목은 없었다.
- 이번 작업은 운영 DB/root migration 또는 cutover, launchd/worker restart, iCloud 원본 변경을 수행하지 않았다. Git stage/commit/push도 수행하지 않았다.

### 변경 파일
- `migrations/v2/0001_recording_store.sql`
- `src/lecture_stt/storage_v2/classification_materialization.py`
- `src/lecture_stt/storage_v2/cli.py`
- `src/lecture_stt/storage_v2/repository.py`
- `src/lecture_stt/storage_v2/timetable.py`
- `src/lecture_stt/storage_v2/verifier.py`
- `tests/test_storage_v2_classification_materialization.py`
- `tests/test_storage_v2_schema.py`
- `src/lecture_stt/ui/web_panel.py`
- `src/lecture_stt/ui/web_panel_state.py`
- `tests/test_web_panel.py`
- `tests/test_web_panel_state.py`
- `docs/ARCHITECTURE.md`
- `docs/STORAGE_V2.md`
- `docs/WORKLOG.md`
