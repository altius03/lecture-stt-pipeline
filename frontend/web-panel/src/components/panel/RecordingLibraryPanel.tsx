import { EmptyState } from "../ui/EmptyState"
import { ErrorState } from "../ui/ErrorState"
import { LoadingBlock } from "../ui/LoadingBlock"
import { SectionCard } from "../ui/SectionCard"
import { useRecordingLibrary } from "../../hooks/useRecordingLibrary"
import type {
  RecordingArtifactStage,
  RecordingContextMetadata,
  RecordingDetailArtifact,
  RecordingDetailJob,
  RecordingDetailPayload,
  RecordingLibraryJobStatus,
  RecordingLibrarySummary,
  RecordingReviewSeverity,
  RecordingTitleSource,
} from "../../types"

const JOB_STATUS_LABELS: Record<RecordingLibraryJobStatus, string> = {
  queued: "대기",
  processing: "처리중",
  done: "완료",
  needs_review: "검토 필요",
  error: "오류",
  canceled: "취소",
}

const TITLE_SOURCE_LABELS: Record<RecordingTitleSource, string> = {
  manual: "직접 지정",
  schedule: "시간표",
  filename_inference: "파일명 추론",
  legacy_import: "기존 기록",
  system: "시스템",
}

const CONTEXT_SOURCE_LABELS: Record<string, string> = {
  manual: "직접 확정",
  schedule_import: "시간표 확정",
  filename_inference: "파일명 추론",
  legacy_import: "기존 기록",
  system: "시스템",
}

const CONTEXT_TYPE_LABELS: Record<string, string> = {
  general: "일반",
  class_session: "수업",
  daily_note: "일상 기록",
  meeting: "회의·대화",
  memo: "개인 메모",
}

const STAGE_LABELS: Record<RecordingArtifactStage, string> = {
  source: "원본",
  transcript: "전사",
  correction: "교정",
  summary: "요약",
  supporting: "지원",
}

const STAGE_ORDER: RecordingArtifactStage[] = [
  "source",
  "transcript",
  "correction",
  "summary",
  "supporting",
]

const REVIEW_SEVERITY_LABELS: Record<RecordingReviewSeverity, string> = {
  low: "낮음",
  medium: "중간",
  high: "높음",
}

function formatTimestamp(value: string | null): string {
  if (value === null) {
    return "없음"
  }
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value))
}

function formatBytes(value: number | null): string {
  if (value === null) {
    return "크기 없음"
  }
  if (value < 1024) {
    return `${value}B`
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)}KiB`
  }
  return `${(value / (1024 * 1024)).toFixed(1)}MiB`
}

function statusTone(status: RecordingLibraryJobStatus): string {
  if (status === "error") {
    return "is-error"
  }
  if (status === "needs_review") {
    return "is-review"
  }
  if (status === "done") {
    return "is-done"
  }
  if (status === "processing") {
    return "is-processing"
  }
  return "is-neutral"
}

function contextSummary(context: RecordingContextMetadata | null): string {
  if (context === null) {
    return "미분류"
  }
  if (context.label) {
    return context.label
  }
  return CONTEXT_TYPE_LABELS[context.context_type] ?? context.context_type
}

function titleSourceDetail(summary: RecordingLibrarySummary): string {
  if (summary.current_title === null) {
    return summary.original_name_nfc
  }
  return TITLE_SOURCE_LABELS[summary.current_title.source] ?? summary.current_title.source
}

function artifactMeta(artifact: RecordingDetailArtifact): string {
  const parts = [
    artifact.artifact_kind,
    `r${artifact.revision}`,
    formatBytes(artifact.bytes),
  ]
  if (artifact.mime_type) {
    parts.push(artifact.mime_type)
  }
  parts.push(formatTimestamp(artifact.created_at))
  return parts.join(" · ")
}

function groupedArtifacts(job: RecordingDetailJob): Record<RecordingArtifactStage, RecordingDetailArtifact[]> {
  return STAGE_ORDER.reduce<Record<RecordingArtifactStage, RecordingDetailArtifact[]>>(
    (groups, stage) => {
      groups[stage] = job.artifacts.filter((artifact) => artifact.stage === stage)
      return groups
    },
    {
      source: [],
      transcript: [],
      correction: [],
      summary: [],
      supporting: [],
    },
  )
}

function LibrarySummaryCards({
  total,
  processing,
  openReviews,
  artifacts,
}: {
  total: number
  processing: number
  openReviews: number
  artifacts: number
}) {
  const items = [
    {
      label: "보관 녹음",
      value: total,
      detail: "전체 read-only 인덱스",
    },
    {
      label: "진행중",
      value: processing,
      detail: "현재 job 상태",
    },
    {
      label: "열린 검토",
      value: openReviews,
      detail: "open·triaged 합계",
    },
    {
      label: "가시 artifact",
      value: artifacts,
      detail: "현재 페이지 합계",
    },
  ]

  return (
    <section className="recording-library-kpis" aria-label="녹음 보관함 핵심 지표">
      {items.map((item) => (
        <article key={item.label} className="recording-library-kpi">
          <span>{item.label}</span>
          <strong>{item.value}</strong>
          <small>{item.detail}</small>
        </article>
      ))}
    </section>
  )
}

function LibraryList({
  summaries,
  selectedStorageKey,
}: {
  summaries: RecordingLibrarySummary[]
  selectedStorageKey: string | null
}) {
  if (summaries.length === 0) {
    return <EmptyState message="현재 페이지에 표시할 녹음이 없습니다." />
  }

  return (
    <div className="table-wrap recording-library-table">
      <table>
        <thead>
          <tr>
            <th scope="col">표시 이름</th>
            <th scope="col">상태</th>
            <th scope="col">분류</th>
            <th scope="col">검토</th>
            <th scope="col">Artifact</th>
          </tr>
        </thead>
        <tbody>
          {summaries.map((summary) => {
            const isSelected = summary.storage_key === selectedStorageKey
            return (
              <tr key={summary.storage_key} className={isSelected ? "is-selected" : undefined}>
                <td>
                  <a
                    href={`#library/${summary.storage_key}`}
                    className="recording-library-row-link"
                    aria-current={isSelected ? "page" : undefined}
                  >
                    <strong>{summary.display_name}</strong>
                    <code>{summary.storage_key}</code>
                    <small>
                      {titleSourceDetail(summary)} · {formatTimestamp(summary.recorded_at ?? summary.received_at)}
                    </small>
                  </a>
                </td>
                <td>
                  {summary.current_job === null ? (
                    <span className="status-pill is-neutral">job 없음</span>
                  ) : (
                    <>
                      <span className={`status-pill ${statusTone(summary.current_job.status)}`}>
                        {JOB_STATUS_LABELS[summary.current_job.status]}
                      </span>
                      <small>{summary.current_job.progress}%</small>
                    </>
                  )}
                </td>
                <td>
                  <strong>{contextSummary(summary.selected_context)}</strong>
                  <small>
                    {summary.selected_context === null
                      ? "자동 확정 없음"
                      : CONTEXT_SOURCE_LABELS[summary.selected_context.source] ?? summary.selected_context.source}
                  </small>
                </td>
                <td>
                  <strong>{summary.open_review_count}</strong>
                  <small>{summary.source_state === "missing" ? "원본 누락" : "원본 정상"}</small>
                </td>
                <td>
                  <strong>{summary.artifact_count}</strong>
                  <small>revision metadata</small>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function LibraryDetail({
  detail,
  outsideCurrentPage,
}: {
  detail: RecordingDetailPayload
  outsideCurrentPage: boolean
}) {
  return (
    <div className="recording-library-detail-stack">
      <SectionCard label="Selected Recording" title={detail.recording.display_name}>
        <div className="recording-library-header">
          <div>
            <code>{detail.recording.storage_key}</code>
            <p className="notice-copy">{detail.recording.original_name_nfc}</p>
          </div>
          <span className="status-pill is-neutral">
            원본 {detail.recording.source.state === "available" ? "사용 가능" : "누락"}
          </span>
        </div>
        {outsideCurrentPage ? (
          <p className="notice-copy">
            현재 선택한 녹음은 첫 페이지 밖에 있어도 detail을 별도 read-only 조회로 유지합니다.
          </p>
        ) : null}
        <dl className="recording-library-definition-grid">
          <div>
            <dt>표시 제목</dt>
            <dd>
              {detail.recording.current_title?.title ?? "미지정"}
              {detail.recording.current_title ? (
                <small>
                  {TITLE_SOURCE_LABELS[detail.recording.current_title.source] ?? detail.recording.current_title.source}
                </small>
              ) : null}
            </dd>
          </div>
          <div>
            <dt>분류</dt>
            <dd>
              {contextSummary(detail.recording.selected_context)}
              {detail.recording.selected_context ? (
                <small>
                  {CONTEXT_SOURCE_LABELS[detail.recording.selected_context.source] ?? detail.recording.selected_context.source}
                </small>
              ) : null}
            </dd>
          </div>
          <div>
            <dt>원본 메타데이터</dt>
            <dd>
              {formatBytes(detail.recording.source.bytes)}
              {detail.recording.source.mime_type ? <small>{detail.recording.source.mime_type}</small> : null}
            </dd>
          </div>
          <div>
            <dt>시각</dt>
            <dd>
              녹음 {formatTimestamp(detail.recording.source.recorded_at)} · 수신{" "}
              {formatTimestamp(detail.recording.source.received_at)}
            </dd>
          </div>
        </dl>
      </SectionCard>

      <SectionCard label="Bounded Detail" title="job · artifact · review 범위">
        <div className="recording-library-limit-grid">
          <div>
            <span>jobs</span>
            <strong>{detail.counts.jobs}</strong>
            <small>표시 상한 {detail.limits.jobs}</small>
          </div>
          <div>
            <span>artifacts</span>
            <strong>{detail.counts.artifacts}</strong>
            <small>표시 상한 {detail.limits.artifacts}</small>
          </div>
          <div>
            <span>reviews</span>
            <strong>{detail.counts.reviews}</strong>
            <small>표시 상한 {detail.limits.reviews}</small>
          </div>
          <div>
            <span>open reviews</span>
            <strong>{detail.counts.open_reviews}</strong>
            <small>open·triaged</small>
          </div>
        </div>
        <p className="notice-copy">
          jobs {detail.limits.jobs_truncated ? "부분 표시" : "전체 표시"} · artifacts{" "}
          {detail.limits.artifacts_truncated ? "부분 표시" : "전체 표시"} · reviews{" "}
          {detail.limits.reviews_truncated ? "부분 표시" : "전체 표시"}
        </p>
      </SectionCard>

      <SectionCard label="Revision Lanes" title="원본 · 전사 · 교정 · 요약 · 지원">
        <div className="recording-library-jobs">
          {detail.jobs.map((job) => {
            const artifacts = groupedArtifacts(job)
            return (
              <article key={job.job_key} className="recording-library-job-card">
                <header className="recording-library-job-header">
                  <div>
                    <strong>{job.job_key}</strong>
                    <small>
                      {job.requested_profile || "profile 없음"}
                      {job.requested_profile_version ? ` · ${job.requested_profile_version}` : ""}
                    </small>
                  </div>
                  <div className="recording-library-job-meta">
                    {job.is_current ? <span className="status-pill is-processing">current</span> : null}
                    <span className={`status-pill ${statusTone(job.status)}`}>
                      {JOB_STATUS_LABELS[job.status]}
                    </span>
                    <small>{job.progress}%</small>
                  </div>
                </header>
                <p className="notice-copy">
                  대기 {formatTimestamp(job.queued_at)} · 시작 {formatTimestamp(job.started_at)} · 종료{" "}
                  {formatTimestamp(job.finished_at)}
                </p>
                <div className="recording-library-stage-grid">
                  {STAGE_ORDER.map((stage) => (
                    <section key={stage} className="recording-library-stage-lane">
                      <header>
                        <strong>{STAGE_LABELS[stage]}</strong>
                        <small>{artifacts[stage].length}</small>
                      </header>
                      {artifacts[stage].length === 0 ? (
                        <EmptyState message="없음" className="recording-library-lane-empty" />
                      ) : (
                        <ul className="recording-library-artifact-list">
                          {artifacts[stage].map((artifact) => (
                            <li key={`${job.job_key}:${artifact.artifact_kind}:${artifact.revision}`}>
                              <span className={artifact.is_latest ? "artifact-chip is-latest" : "artifact-chip"}>
                                {artifactMeta(artifact)}
                              </span>
                            </li>
                          ))}
                        </ul>
                      )}
                    </section>
                  ))}
                </div>
              </article>
            )
          })}
        </div>
      </SectionCard>

      <SectionCard label="Reviews" title="검토 코드와 연결 상태">
        {detail.reviews.length === 0 ? (
          <EmptyState message="연결된 review가 없습니다." />
        ) : (
          <ul className="recording-library-review-list">
            {detail.reviews.map((review, index) => (
              <li key={`${review.reason_code}:${review.created_at}:${index}`}>
                <div>
                  <span className={`status-pill ${review.status === "resolved" || review.status === "dismissed" ? "is-neutral" : "is-review"}`}>
                    {review.status}
                  </span>
                  {review.severity ? (
                    <small>{REVIEW_SEVERITY_LABELS[review.severity]}</small>
                  ) : null}
                </div>
                <strong>{review.reason_code}</strong>
                <small>
                  {review.job_key ?? "job 없음"}
                  {review.artifact_kind ? ` · ${review.artifact_kind} r${review.artifact_revision}` : ""}
                </small>
                <small>
                  생성 {formatTimestamp(review.created_at)} · 종료 {formatTimestamp(review.resolved_at)}
                </small>
              </li>
            ))}
          </ul>
        )}
      </SectionCard>
    </div>
  )
}

interface RecordingLibraryPanelProps {
  selectedStorageKey: string | null
}

export function RecordingLibraryPanel({
  selectedStorageKey,
}: RecordingLibraryPanelProps) {
  const {
    list,
    detail,
    listLoading,
    detailLoading,
    listError,
    detailError,
    retryList,
    retryDetail,
  } = useRecordingLibrary({
    enabled: true,
    selectedStorageKey,
  })

  if (listLoading && list === null) {
    return (
      <LoadingBlock
        eyebrow="Recording Library"
        title="녹음 보관함을 읽는 중입니다."
        description="첫 페이지 summary와 선택한 recording detail을 분리된 read-only query로 확인합니다."
      />
    )
  }

  if (list === null) {
    return (
      <ErrorState
        eyebrow="Recording Library"
        title="녹음 보관함을 불러오지 못했습니다."
        message={listError ?? "알 수 없는 오류"}
        retryLabel="목록 다시 읽기"
        onRetry={() => {
          void retryList()
        }}
      />
    )
  }

  if (!list.available) {
    return (
      <section className="dashboard-screen-grid" aria-label="녹음 보관함 비활성화">
        <SectionCard label="Recording Library" title="Storage v2 보관함 API가 비활성화되어 있습니다.">
          <EmptyState message="운영 데이터에는 자동 연결하지 않았습니다." />
          <p className="notice-copy">{list.disabled_reason ?? "recording_library_disabled"}</p>
          <p className="notice-copy">
            웹 업로드 없이 iPhone 단축어 → 개인 클라우드 경로를 유지하고, detail query도 사용하지 않습니다.
          </p>
        </SectionCard>
      </section>
    )
  }

  const outsideCurrentPage =
    selectedStorageKey !== null
    && !list.summaries.some((summary) => summary.storage_key === selectedStorageKey)

  return (
    <section className="recording-library" aria-label="녹음 보관함">
      <LibrarySummaryCards
        total={list.counts.recordings}
        processing={list.counts.processing}
        openReviews={list.counts.open_reviews}
        artifacts={list.summaries.reduce((sum, item) => sum + item.artifact_count, 0)}
      />

      <div className="dashboard-screen-grid recording-library-grid">
        <SectionCard label="Recording Library" title="storage_key와 표시 이름을 분리한 목록">
          <p className="notice-copy">
            첫 페이지 {list.filters.limit}건만 읽고, hash detail은 선택된 storage_key 하나를 별도 read-only 조회로 유지합니다.
          </p>
          <LibraryList summaries={list.summaries} selectedStorageKey={selectedStorageKey} />
        </SectionCard>

        <section className="recording-library-detail-shell" aria-label="선택한 녹음 상세">
          {selectedStorageKey === null ? (
            <SectionCard label="Recording Detail" title="상세를 선택하세요" tone="muted">
              <EmptyState message="목록이나 홈의 확인 필요 항목에서 녹음을 선택하면 revision 관계와 review 코드를 바로 보여줍니다." />
            </SectionCard>
          ) : detailLoading && detail === null ? (
            <SectionCard label="Recording Detail" title="상세를 읽는 중입니다." tone="muted">
              <EmptyState message={`${selectedStorageKey} detail을 불러오는 중입니다.`} />
            </SectionCard>
          ) : detail === null ? (
            <ErrorState
              eyebrow="Recording Detail"
              title="선택한 녹음 상세를 읽지 못했습니다."
              message={detailError ?? "알 수 없는 오류"}
              retryLabel="상세 다시 읽기"
              onRetry={() => {
                void retryDetail()
              }}
            />
          ) : (
            <LibraryDetail detail={detail} outsideCurrentPage={outsideCurrentPage} />
          )}
        </section>
      </div>
    </section>
  )
}
