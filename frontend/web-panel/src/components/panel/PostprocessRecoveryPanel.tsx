import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import type { RecoveryDetail, RecoveryJob, RecoveryView, RequeueRequest } from "../../lib/decodePostprocessRecovery"
import { fetchRecoveryDetail, fetchRecoveryList, requeueRecoveryJob } from "../../lib/panelApi"
import { DefinitionGrid } from "../ui/DefinitionGrid"
import { SectionCard } from "../ui/SectionCard"

const PAGE_SIZE = 20
const LIST_KEY = ["postprocess-recovery", "list"] as const
const labels: Record<string, string> = {
  SKIPPED: "후처리 제외", UNROUTED: "과목 미확정", PENDING: "대기", BLOCKED: "이전 단계 대기",
  READY: "완료", DELIVERED: "전달 완료", NEEDS_REVIEW: "검토 필요", ERROR: "오류", CONFLICT: "충돌",
}
const label = (value: string) => labels[value] ?? value
const missing = (job: RecoveryJob) => job.status === "SKIPPED" || job.status === "UNROUTED"
const recordingDate = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", dateStyle: "short", timeStyle: "short",
})

function recordedAt(value: string | null) {
  if (!value) return "녹음 시각 미상"
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? "녹음 시각 미상" : recordingDate.format(date)
}

function CourseChoice({ detail, busy, onConfirm }: {
  detail: RecoveryDetail
  busy: boolean
  onConfirm: (request: RequeueRequest) => void
}) {
  const [code, setCode] = useState("")
  const course = detail.courses.find((item) => item.code === code)
  return (
    <form className="recovery-form" onSubmit={(event) => {
      event.preventDefault()
      if (busy || !course || !detail.can_requeue || !detail.source_txt_sha256 || !detail.source_json_sha256) return
      if (!window.confirm(`작업 #${detail.job.id} ‘${detail.job.title}’\n선택한 과목: ${course.name} (${course.code})\n이 과목으로 확정하고 후처리에 재등록할까요?`)) return
      onConfirm({
        course_code: course.code,
        source_txt_sha256: detail.source_txt_sha256,
        source_json_sha256: detail.source_json_sha256,
      })
    }}>
      <label>
        <span>확정할 과목</span>
        <select disabled={busy} value={code} onChange={(event) => setCode(event.target.value)}>
          <option value="">과목 선택</option>
          {detail.courses.map((item) => <option key={item.code} value={item.code}>{item.name} ({item.code})</option>)}
        </select>
      </label>
      <button type="submit" disabled={!course || busy}>과목 확정 및 재등록</button>
    </form>
  )
}

function LoadError({ error, retry }: { error: Error; retry: () => void }) {
  return <div className="action-row">
    <p className="inline-error" role="alert">{error.message}</p>
    <button type="button" className="button-secondary" onClick={retry}>다시 조회</button>
  </div>
}

export function PostprocessRecoveryPanel({ canControl }: { canControl: boolean }) {
  const client = useQueryClient()
  const [view, setView] = useState<RecoveryView>("missing")
  const [offset, setOffset] = useState(0)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [formVersion, setFormVersion] = useState(0)
  const [notice, setNotice] = useState("")
  const sending = useRef(false)
  const list = useQuery({
    queryKey: [...LIST_KEY, view, offset],
    queryFn: ({ signal }) => fetchRecoveryList(view, offset, PAGE_SIZE, signal),
    retry: false,
    refetchInterval: (query) => view === "confirmed" && query.state.data?.items.some((job) => job.status === "PENDING") ? 5000 : false,
  })
  const detail = useQuery({
    queryKey: ["postprocess-recovery", "detail", selectedId, canControl],
    queryFn: ({ signal }) => fetchRecoveryDetail(selectedId!, signal),
    enabled: selectedId !== null && list.data?.enabled === true,
    gcTime: 0,
    retry: false,
    refetchInterval: (query) => query.state.data?.job.status === "PENDING" ? 5000 : false,
  })
  const submit = useMutation({
    mutationFn: ({ id, request }: { id: number; request: RequeueRequest }) => requeueRecoveryJob(id, request),
    retry: false,
    onSuccess: (result) => {
      setView("confirmed")
      setOffset(0)
      setSelectedId(result.id)
      setNotice(`작업 #${result.id}의 재등록을 확인했습니다.`)
    },
    onSettled: async () => {
      setFormVersion((value) => value + 1)
      try {
        await Promise.all([
          client.invalidateQueries({ queryKey: LIST_KEY }),
          client.invalidateQueries({ queryKey: ["postprocess-recovery", "detail", selectedId] }),
        ])
      } finally {
        sending.current = false
      }
    },
  })
  const page = list.data
  useEffect(() => {
    if (page && offset > 0 && offset >= page.total) {
      setOffset(Math.max(0, Math.floor((page.total - 1) / PAGE_SIZE) * PAGE_SIZE))
    }
  }, [page, offset])
  const info = detail.data
  const switchList = (next: RecoveryView, nextOffset: number) => {
    setView(next)
    setOffset(nextOffset)
    setSelectedId(null)
    setNotice("")
    submit.reset()
  }
  const busy = submit.isPending
  return (
    <SectionCard title="누락 강의 복구" label="후처리" className="postprocess-recovery">
      {list.isError ? <LoadError error={list.error} retry={() => { void list.refetch() }} /> : null}
      {list.isLoading ? <p role="status">복구 목록을 불러오는 중입니다.</p> : null}
      {page && !page.enabled ? <p className="empty-copy">후처리가 꺼져 있습니다. 활성화하면 복구할 녹음을 확인할 수 있습니다.</p> : null}
      {page?.enabled ? <>
        <div className="recovery-toolbar">
          <div className="action-row" role="group" aria-label="복구 목록 선택">
            {(["missing", "confirmed"] as const).map((mode) => (
              <button key={mode} type="button" aria-pressed={view === mode} disabled={busy}
                className={mode === view ? undefined : "button-secondary"} onClick={() => switchList(mode, 0)}>
                {mode === "missing" ? "과목 확인 대기" : "확정 이력"}
              </button>
            ))}
          </div>
          <button type="button" className="button-secondary" disabled={busy || list.isFetching}
            onClick={() => { void list.refetch(); if (selectedId !== null) void detail.refetch() }}>새로고침</button>
        </div>
        {page.items.length ? <ul className="recovery-list" aria-label="녹음 목록">
          {page.items.map((job) => (
            <li key={job.id}>
              <button type="button" className="recovery-record" aria-pressed={selectedId === job.id} disabled={busy}
                onClick={() => { setSelectedId(job.id); setNotice(""); submit.reset() }}>
                <strong>{job.title}</strong>
                <span>#{job.id} · {recordedAt(job.recorded_at)} · {label(job.status)}</span>
                <span>{missing(job) ? job.reason : job.course_code}</span>
              </button>
            </li>
          ))}
        </ul> : <p className="empty-copy">{view === "missing" ? "과목 확인을 기다리는 녹음이 없습니다." : "과목을 확정한 녹음이 없습니다."}</p>}
        {(page.total > PAGE_SIZE || offset > 0) && <nav className="action-row recovery-paging" aria-label="복구 목록 페이지">
          <button type="button" className="button-secondary" disabled={busy || offset === 0}
            onClick={() => switchList(view, Math.max(0, offset - PAGE_SIZE))}>이전</button>
          <span>{Math.min(offset + 1, page.total)}–{Math.min(offset + page.items.length, page.total)} / {page.total}건</span>
          <button type="button" className="button-secondary" disabled={busy || offset + PAGE_SIZE >= page.total || offset + PAGE_SIZE > 100000}
            onClick={() => switchList(view, offset + PAGE_SIZE)}>다음</button>
        </nav>}
        {notice && <p role="status">{notice}</p>}
        {submit.error && <p className="inline-error" role="alert">{submit.error.message} 최신 정보를 확인한 뒤 과목을 다시 선택하세요.</p>}
        {selectedId !== null && <section className="recovery-detail" aria-label="선택한 녹음">
          {detail.isLoading && <p role="status">작업 상태를 확인하는 중입니다.</p>}
          {detail.isError && <LoadError error={detail.error} retry={() => { void detail.refetch() }} />}
          {info && !detail.isError && <>
            <h3>#{info.job.id} · {info.job.title}</h3>
            <DefinitionGrid items={[
              { label: "녹음 시각 (한국시간)", value: recordedAt(info.job.recorded_at) },
              { label: "후처리 상태", value: <span aria-live="polite">{label(info.job.status)}</span> },
              { label: "과목", value: info.job.course_code ?? "미확정" },
              { label: "처리 사유", value: info.job.reason },
              { label: "교정", value: label(info.job.correction_status) },
              { label: "요약", value: label(info.job.summary_status) },
              { label: "전달", value: label(info.job.delivery_status) },
            ]} />
            {missing(info.job) && (
              !canControl ? <p className="notice-copy">조회 전용입니다. 재등록하려면 운영자 권한이 필요합니다.</p>
              : !info.can_requeue || !info.courses.length ? <p className="notice-copy">현재 등록할 수 없습니다. 활성 학기와 원전사 상태를 확인하세요.</p>
              : <CourseChoice key={`${info.job.id}-${info.source_txt_sha256}-${info.source_json_sha256}-${formVersion}`}
                  detail={info} busy={busy || !detail.isFetchedAfterMount || detail.isFetching || !list.isSuccess}
                  onConfirm={(request) => {
                    if (sending.current) return
                    sending.current = true
                    setNotice("")
                    submit.mutate({ id: info.job.id, request })
                  }} />
            )}
          </>}
        </section>}
      </> : null}
    </SectionCard>
  )
}
