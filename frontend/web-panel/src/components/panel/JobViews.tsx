import { useEffect, useMemo, useState } from "react"

import { fetchJobTranscriptPreview, fetchQualityReviews, PanelApiError, postJobQualityReview } from "../../lib/panelApi"
import { parseLectureFileInfo } from "../../lib/logParser"
import type { JobStage, JobStageStatus, JobTranscriptPreview, PanelJob, QualityReviewDecision } from "../../types"

const STAGES: { key: JobStage; label: string; icon: string }[] = [
  { key: "audio", label: "오디오", icon: "/icons/audio.svg" },
  { key: "transcript", label: "전사", icon: "/icons/transcript.svg" },
  { key: "correction", label: "교정", icon: "/icons/correction.svg" },
  { key: "summary", label: "요약", icon: "/icons/summary.svg" },
]

const STAGE_LABEL: Record<JobStageStatus, string> = {
  pending: "대기", processing: "진행 중", done: "완료", blocked: "보류", error: "오류", skipped: "건너뜀",
}

function dateLabel(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat("ko-KR", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date)
}

function jobTitle(job: PanelJob): string {
  return parseLectureFileInfo(job.file_name)?.displayTitle ?? job.file_name.replace(/\.[^.]+$/, "")
}

function hasOpenQualityReview(job: PanelJob): boolean {
  return job.status === "DONE" && job.quality_review?.decision !== "no_issue"
    && (job.quality_review?.required === true || job.quality_review?.decision === "needs_correction")
}

function isBlockingReview(job: PanelJob): boolean {
  return job.status === "NEEDS_REVIEW"
}

function hasOpenReviewItem(job: PanelJob): boolean {
  return isBlockingReview(job) || hasOpenQualityReview(job)
}

function reviewReason(job: PanelJob): string {
  return isBlockingReview(job)
    ? job.error_message.trim() || job.quality_review?.summary || "중단 사유를 확인할 수 없습니다."
    : job.quality_review?.summary ?? "품질 확인 요청"
}

function StageState({ status, progress, jobStatus }: { status?: JobStageStatus; progress?: number; jobStatus?: string }) {
  if (!status) return <span className="stage-state is-muted">—</span>
  const waiting = status === "blocked" && (jobStatus === "PENDING" || jobStatus === "PROCESSING")
  return (
    <span className={`stage-state is-${waiting ? "pending" : status}`}>
      {status === "done" ? <span className="stage-dot" aria-hidden="true" /> : null}
      {waiting ? "대기" : status === "processing" && progress !== undefined ? `진행 ${progress}%` : STAGE_LABEL[status]}
    </span>
  )
}

export function OverviewPanel({ jobs, processingCount, reviewCount, errorCount, onRefresh }: {
  jobs: PanelJob[]
  processingCount: number
  reviewCount: number
  errorCount: number
  onRefresh: () => void
}) {
  const [search, setSearch] = useState("")
  const [page, setPage] = useState(0)
  const filtered = useMemo(() => jobs.filter((job) => `${job.file_name} ${job.id}`.toLocaleLowerCase().includes(search.toLocaleLowerCase())), [jobs, search])
  const pageSize = 10
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize))
  const safePage = Math.min(page, pageCount - 1)
  const visible = filtered.slice(safePage * pageSize, (safePage + 1) * pageSize)

  return (
    <>
      <div className="page-title-row">
        <div><h2>작업 현황</h2><p>녹음부터 요약까지, 지금 필요한 상태만 확인합니다.</p></div>
        <button className="button-secondary" type="button" onClick={onRefresh}>새로고침</button>
      </div>
      <div className="overview-stats" aria-label="작업 요약">
        <div><strong>{processingCount}</strong><span>진행 중</span></div>
        <div><strong>{reviewCount}</strong><span>확인 요청</span></div>
        <div><strong>{errorCount}</strong><span>오류</span></div>
      </div>
      <section aria-labelledby="jobs-title" className="jobs-section">
        <div className="section-title-row">
          <div><h3 id="jobs-title">작업 목록</h3><span>최근 {jobs.length}건</span></div>
          <label className="search-field"><span className="sr-only">작업 검색</span><input value={search} onChange={(event) => { setSearch(event.target.value); setPage(0) }} placeholder="강의명 또는 파일 검색" type="search" /></label>
        </div>
        {visible.length > 0 ? <p className="table-scroll-hint">옆으로 밀어 단계 보기 →</p> : null}
        <div className="data-surface jobs-table-scroll">
          <table className="jobs-table">
            <thead><tr><th scope="col">작업</th>{STAGES.map((stage) => <th key={stage.key} scope="col"><span className="column-icon"><img src={stage.icon} alt="" />{stage.label}</span></th>)}<th scope="col">최종 갱신</th><th scope="col" aria-label="작업 열기" /></tr></thead>
            <tbody>
              {visible.length === 0 ? <tr><td colSpan={7} className="empty-table">표시할 작업이 없습니다.</td></tr> : visible.map((job) => <tr key={job.id}>
                <td><div className="job-name-line"><strong>{jobTitle(job)}</strong>{isBlockingReview(job) ? <span className="review-note"><img src="/icons/review-warning.svg" alt="" />작업 중단</span> : job.quality_review?.decision === "needs_correction" ? <span className="review-note">수정 필요 기록됨</span> : hasOpenQualityReview(job) ? <span className="review-note"><img src="/icons/review-warning.svg" alt="" />확인 권장</span> : null}</div><small>#{job.id} · {job.file_name}</small></td>
                {STAGES.map((stage) => <td key={stage.key}><StageState status={job.stages?.[stage.key]} jobStatus={job.status} progress={job.stages?.[stage.key] === "processing" ? job.progress_pct : undefined} /></td>)}
                <td className="muted-cell">{dateLabel(job.updated_at)}</td>
                <td className="table-link-cell"><a href={hasOpenReviewItem(job) ? `#review/quality/${job.id}` : `#job/${job.id}`}>{hasOpenReviewItem(job) ? "확인하기" : job.status === "DONE" ? "본문 보기" : "상태 보기"} <span aria-hidden="true">→</span></a></td>
              </tr>)}
            </tbody>
          </table>
          <div className="table-footer"><span>{filtered.length === 0 ? "0" : `${safePage * pageSize + 1}–${Math.min((safePage + 1) * pageSize, filtered.length)}`} / {filtered.length}</span><div><button type="button" disabled={safePage === 0} onClick={() => setPage(safePage - 1)}>이전</button><button type="button" disabled={safePage + 1 >= pageCount} onClick={() => setPage(safePage + 1)}>다음 →</button></div></div>
        </div>
      </section>
    </>
  )
}

function useJobPreview(jobId: number | null) {
  const [preview, setPreview] = useState<JobTranscriptPreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (jobId === null) { setPreview(null); setError(null); return }
    const controller = new AbortController()
    setPreview(null)
    setError(null)
    setLoading(true)
    void fetchJobTranscriptPreview(jobId, controller.signal).then(setPreview).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof PanelApiError && reason.status === 403 ? "본문 조회가 설정에서 비활성화되어 있습니다." : reason instanceof PanelApiError && reason.status === 409 ? "이 작업의 본문은 아직 준비되지 않았습니다." : reason instanceof Error ? reason.message : "본문을 불러오지 못했습니다.")
    }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [jobId])
  return { preview, loading, error }
}

export function JobDetailPanel({ job, jobId }: { job: PanelJob | null; jobId: number }) {
  const completedId = job?.status === "DONE" || job === null ? jobId : null
  const { preview, loading, error } = useJobPreview(completedId)
  if (!job) return <><div className="page-title-row"><div><a className="back-link" href="#home">← 현황 / 작업 목록</a><h2>작업 #{jobId}</h2><p>최근 작업 목록에 없는 완료 작업입니다.</p></div><a className="button-secondary as-button" href={`#processing/${jobId}`}>관련 로그</a></div><section className="data-surface transcript-panel"><div className="surface-heading"><h3>전사 본문</h3></div><div className="transcript-body">{loading ? <p role="status">본문을 불러오는 중입니다.</p> : error ? <p role="alert">{error}</p> : preview?.text ? <pre>{preview.text}</pre> : <p>표시할 본문이 없습니다.</p>}</div></section></>
  return <>
    <div className="page-title-row"><div><a className="back-link" href="#home">← 현황 / 작업 목록</a><h2>{jobTitle(job)}</h2><p>{job.status === "DONE" ? "완료" : job.status === "PROCESSING" ? "진행 중" : job.status === "NEEDS_REVIEW" ? "작업 중단" : job.status === "ERROR" ? "오류" : "대기"} · 마지막 갱신 {dateLabel(job.updated_at)}</p></div><a className="button-secondary as-button" href={`#processing/${job.id}`}>관련 로그</a></div>
    <div className="detail-stages data-surface" aria-label="처리 단계">{STAGES.map((stage) => <div key={stage.key}><div className="stage-heading"><img src={stage.icon} alt="" /><strong>{stage.label}</strong></div><StageState status={job.stages?.[stage.key]} jobStatus={job.status} progress={job.stages?.[stage.key] === "processing" ? job.progress_pct : undefined} /></div>)}</div>
    <div className="detail-layout"><section className="data-surface transcript-panel" aria-labelledby="transcript-title"><div className="surface-heading"><h3 id="transcript-title">전사 본문</h3></div><div className="transcript-body">{completedId === null ? <p>작업 완료 후 본문을 볼 수 있습니다.</p> : loading ? <p role="status">본문을 불러오는 중입니다.</p> : error ? <p role="alert">{error}</p> : preview?.text ? <pre>{preview.text}</pre> : <p>표시할 본문이 없습니다.</p>}</div></section><aside className="detail-aside"><div className="data-surface detail-info"><h3>작업 정보</h3><dl><dt>원본 파일</dt><dd>{job.file_name}</dd><dt>현재 단계</dt><dd>{job.step || "—"}</dd><dt>진행률</dt><dd>{job.progress_pct}%</dd><dt>작업 ID</dt><dd>#{job.id}</dd></dl></div>{hasOpenReviewItem(job) ? <div className="data-surface detail-info"><h3>{isBlockingReview(job) ? "작업 중단" : job.quality_review?.decision === "needs_correction" ? "수정 필요 기록됨" : "완료 후 확인"}</h3><p>{reviewReason(job)}</p>{job.quality_review?.decision === "needs_correction" ? <p>수동 수정 후 문제 없음으로 마무리할 수 있습니다. 자동 재처리는 시작하지 않았습니다.</p> : null}<a className="button-secondary as-button" href={`#review/quality/${job.id}`}>확인 요청 보기 →</a></div> : null}</aside></div>
  </>
}

export function QualityReviewPanel({ jobs, openReviewCount, needsReviewCount, selectedJobId, canControl, onResolved }: { jobs: PanelJob[]; openReviewCount?: number; needsReviewCount?: number; selectedJobId: number | null; canControl: boolean; onResolved: () => Promise<unknown> }) {
  const [offset, setOffset] = useState(0)
  const [feed, setFeed] = useState<{ items: PanelJob[]; total: number } | null>(null)
  const [feedError, setFeedError] = useState<string | null>(null)
  const [feedLoading, setFeedLoading] = useState(true)
  const [revision, setRevision] = useState(0)
  const recentReviewKey = jobs.filter(hasOpenReviewItem).map((job) => `${job.id}:${job.status}:${job.quality_review?.decision ?? "pending"}`).join("|")
  useEffect(() => {
    const controller = new AbortController()
    setFeedLoading(true)
    void fetchQualityReviews(50, offset, controller.signal).then((result) => { setFeed(result); setFeedError(null) }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setFeedError(reason instanceof Error ? reason.message : "확인 목록을 불러오지 못했습니다.")
    }).finally(() => { if (!controller.signal.aborted) setFeedLoading(false) })
    return () => controller.abort()
  }, [offset, revision, openReviewCount, needsReviewCount, recentReviewKey])
  const pending = (feed?.items ?? jobs).filter(hasOpenReviewItem)
  const [localSelectedId, setLocalSelectedId] = useState<number | null>(selectedJobId)
  const selected = selectedJobId !== null
    ? pending.find((job) => job.id === selectedJobId) ?? null
    : pending.find((job) => job.id === localSelectedId) ?? pending[0] ?? null
  const [saving, setSaving] = useState<QualityReviewDecision | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function decide(decision: QualityReviewDecision) {
    if (!canControl || !selected || saving !== null || isBlockingReview(selected)) return
    setSaving(decision)
    setError(null)
    try {
      await postJobQualityReview(selected.id, decision)
      await onResolved()
      setRevision((value) => value + 1)
      window.location.hash = decision === "needs_correction" ? `#review/quality/${selected.id}` : "#review"
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "확인 결과를 저장하지 못했습니다.")
    } finally { setSaving(null) }
  }

  return (
    <section aria-labelledby="quality-review-title" className="review-section">
      <div className="section-title-row">
        <div><h3 id="quality-review-title">완료 후 확인</h3></div>
      </div>
      {feedError ? <p role="alert" className="form-error">{feed ? "기존 확인 목록을 표시합니다." : "전체 목록을 불러오지 못해 최근 작업만 표시합니다."} {feedError}</p> : null}
      {feedLoading && !feed ? (
        <div className="data-surface empty-state-message" role="status">확인 목록을 불러오는 중입니다.</div>
      ) : pending.length === 0 ? (
        <div className="data-surface empty-state-message">확인할 작업이 없습니다.</div>
      ) : (
        <div className="review-layout">
          <div className="data-surface review-list">
            <div className="review-list-heading"><h4>확인 요청</h4><span>{feed?.total ?? pending.length}건</span></div>
            {pending.map((job) => (
              <button key={job.id} type="button" className={selected?.id === job.id ? "review-list-item is-selected" : "review-list-item"} onClick={() => { setLocalSelectedId(job.id); window.location.hash = `#review/quality/${job.id}` }}>
                <span className="review-list-meta"><small>전사 품질</small><small>{isBlockingReview(job) ? "작업 중단" : job.quality_review?.decision === "needs_correction" ? "수정 필요 기록됨" : "완료 후 확인"}</small></span>
                <strong>{jobTitle(job)}</strong>
                <span>{reviewReason(job)}</span>
                <small>#{job.id} · {dateLabel(job.updated_at)}</small>
              </button>
            ))}
            {feed && feed.total > 50 ? <div className="table-footer"><span>{offset + 1}–{Math.min(offset + 50, feed.total)} / {feed.total}</span><div><button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>이전</button><button disabled={offset + 50 >= feed.total} onClick={() => setOffset(offset + 50)}>다음 →</button></div></div> : null}
          </div>
          <div className="data-surface review-detail">
            {selected ? <>
              <div className="surface-heading"><div><small>전사 품질 · {isBlockingReview(selected) ? "작업 중단" : selected.quality_review?.decision === "needs_correction" ? "수정 필요 기록됨" : "완료 후 확인"}</small><h3>{jobTitle(selected)}</h3></div>{isBlockingReview(selected) ? <span className="stage-state is-blocked">작업 중단</span> : <span className="complete-label">처리 완료</span>}</div>
              <div className="review-detail-body">
                <p className="review-caption">처리 결과</p>
                <p>{STAGES.map((stage) => `${stage.label} ${selected.stages?.[stage.key] ? STAGE_LABEL[selected.stages[stage.key]] : "미확인"}`).join(" · ")}</p>
                <hr />
                <p className="review-caption">{isBlockingReview(selected) ? "중단 사유" : "확인 요청 이유"}</p>
                <p>{reviewReason(selected)}</p>
                <p className="review-caption">{isBlockingReview(selected) ? "이 작업의 전사는 중단됐고 다른 작업은 계속 진행됩니다." : selected.quality_review?.decision === "needs_correction" ? "수동 처리 후 문제 없음으로 마무리하세요. 자동 재처리는 시작되지 않았습니다." : "확인 결과를 기록하며 자동 재처리는 시작하지 않습니다."}</p>
                <hr />
                <p className="review-caption">{isBlockingReview(selected) ? "작업 기록" : "전사 본문"}</p>
                <p><a className="text-link" href={`#job/${selected.id}`}>{isBlockingReview(selected) ? "작업 상세 보기 →" : "본문 열어보기 →"}</a>{isBlockingReview(selected) ? <> <a className="text-link" href={`#processing/${selected.id}`}>관련 로그 보기 →</a></> : null}</p>
              </div>
              {!isBlockingReview(selected) ? <div className="review-actions">{selected.quality_review?.decision !== "needs_correction" ? <button type="button" className="button-secondary" disabled={!canControl || saving !== null} onClick={() => { void decide("needs_correction") }}>수정 필요로 기록</button> : null}<button type="button" className="button-primary" disabled={!canControl || saving !== null} onClick={() => { void decide("no_issue") }}>{selected.quality_review?.decision === "needs_correction" ? "문제 없음으로 마무리" : "문제 없음"}</button></div> : null}
              {error ? <p role="alert" className="form-error">{error}</p> : null}
            </> : <p className="empty-state-message">선택한 작업이 현재 목록 페이지에 없습니다. 목록 페이지를 이동해 확인하세요.</p>}
          </div>
        </div>
      )}
    </section>
  )
}
