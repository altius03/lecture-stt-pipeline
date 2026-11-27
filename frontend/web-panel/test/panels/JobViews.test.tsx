import { afterEach, describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"

import { JobDetailPanel, OverviewPanel, QualityReviewPanel } from "../../src/components/panel/JobViews"
import type { PanelJob } from "../../src/types"

const stages = { audio: "done", transcript: "done", correction: "done", summary: "done" } as const
function job(id: number, overrides: Partial<PanelJob> = {}): PanelJob {
  return { id, status: "DONE", file_name: `lecture-${id}.m4a`, updated_at: "2026-09-28T09:00:00+09:00", step: "완료", progress_pct: 100, error_message: "", stages, ...overrides }
}
function response(payload: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => payload } as Response
}
afterEach(() => { vi.unstubAllGlobals(); window.location.hash = "" })

describe("new job flow", () => {
  it("shows four real stages and routes open reviews to the confirmation queue", () => {
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)
    render(<OverviewPanel jobs={[
      job(1, { quality_review: { required: true, decision: null, decided_at: null, summary: "음량 낮음" } }),
      job(2, { status: "NEEDS_REVIEW", quality_review: { required: false, decision: null, decided_at: null, summary: "본문 누락" } }),
    ]} processingCount={0} reviewCount={1} errorCount={0} onRefresh={vi.fn()} />)
    const rows = screen.getAllByRole("row")
    expect(within(rows[1]).getByRole("link", { name: /확인하기/ }).getAttribute("href")).toBe("#review/quality/1")
    expect(within(rows[1]).getAllByText("완료")).toHaveLength(4)
    expect(within(rows[2]).getByRole("link", { name: /확인하기/ }).getAttribute("href")).toBe("#review/quality/2")
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it("fetches completed body on detail only and explains disabled preview", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ error: "disabled" }, 403))
    vi.stubGlobal("fetch", fetchMock)
    render(<JobDetailPanel job={job(3)} jobId={3} />)
    expect(fetchMock).toHaveBeenCalledWith("/api/jobs/3/transcript-preview", expect.objectContaining({ cache: "no-store" }))
    expect((await screen.findByRole("alert")).textContent).toContain("본문 조회가 설정에서 비활성화되어 있습니다.")
  })

  it("shows quality details to a viewer without accepting review decisions", async () => {
    const item = job(88, { quality_review: { required: true, decision: null, decided_at: null, summary: "음량 확인" } })
    const fetchMock = vi.fn(async () => response({ items: [item], total: 1 }))
    vi.stubGlobal("fetch", fetchMock)
    render(<QualityReviewPanel canControl={false} jobs={[item]} selectedJobId={null} onResolved={vi.fn()} />)
    const decide = await screen.findByRole("button", { name: "문제 없음" })
    expect((decide as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(decide)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/quality-reviews?"), expect.anything())
  })

  it("loads the full review queue beyond recent jobs and refreshes it after a decision", async () => {
    let saved = false
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes("/quality-reviews")) return response({ items: saved ? [] : [job(88, { quality_review: { required: true, decision: null, decided_at: null, summary: "음량이 낮습니다." } })], total: saved ? 0 : 1, limit: 50, offset: 0 })
      if (url.endsWith("/quality-review") && init?.method === "POST") { saved = true; return response({ ok: true }) }
      throw new Error(`unexpected ${url}`)
    })
    vi.stubGlobal("fetch", fetchMock)
    render(<QualityReviewPanel canControl jobs={[]} selectedJobId={null} onResolved={async () => undefined} />)
    expect(await screen.findByRole("heading", { name: "lecture-88" })).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "문제 없음" }))
    await waitFor(() => expect(saved).toBe(true))
    await waitFor(() => expect(screen.getByText("확인할 작업이 없습니다.")).toBeTruthy())
  })

  it("refreshes an open review screen when the SSE count changes beyond recent jobs", async () => {
    const recent = [job(1)]
    let queue: PanelJob[] = []
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      if (!String(input).includes("/quality-reviews")) throw new Error(`unexpected ${input}`)
      return response({ items: queue, total: queue.length, limit: 50, offset: 0 })
    })
    vi.stubGlobal("fetch", fetchMock)
    const onResolved = async () => undefined
    const panel = render(<QualityReviewPanel canControl jobs={recent} openReviewCount={0} selectedJobId={null} onResolved={onResolved} />)
    expect(await screen.findByText("확인할 작업이 없습니다.")).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(1)

    panel.rerender(<QualityReviewPanel canControl jobs={[...recent]} openReviewCount={0} selectedJobId={null} onResolved={onResolved} />)
    expect(fetchMock).toHaveBeenCalledTimes(1)

    queue = [job(101, { quality_review: { required: true, decision: null, decided_at: null, summary: "음량이 낮습니다." } })]
    panel.rerender(<QualityReviewPanel canControl jobs={recent} openReviewCount={1} selectedJobId={null} onResolved={onResolved} />)
    expect(await screen.findByRole("heading", { name: "lecture-101" })).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it("shows blocking reviews without decision actions and refreshes when their count changes", async () => {
    const blocked = job(102, {
      status: "NEEDS_REVIEW",
      error_message: "오디오 파일이 손상되어 작업을 중단했습니다.",
      quality_review: { required: false, decision: null, decided_at: null, summary: "음성 확인 필요" },
    })
    let queue: PanelJob[] = []
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      if (!String(input).includes("/quality-reviews")) throw new Error(`unexpected ${input}`)
      return response({ items: queue, total: queue.length, limit: 50, offset: 0 })
    })
    vi.stubGlobal("fetch", fetchMock)
    const onResolved = async () => undefined
    const panel = render(<QualityReviewPanel canControl jobs={[]} openReviewCount={0} needsReviewCount={0} selectedJobId={null} onResolved={onResolved} />)
    expect(await screen.findByText("확인할 작업이 없습니다.")).toBeTruthy()

    queue = [blocked]
    panel.rerender(<QualityReviewPanel canControl jobs={[]} openReviewCount={0} needsReviewCount={1} selectedJobId={null} onResolved={onResolved} />)
    expect(await screen.findByRole("heading", { name: "lecture-102" })).toBeTruthy()
    expect(screen.getAllByText("오디오 파일이 손상되어 작업을 중단했습니다.")).toHaveLength(2)
    expect(screen.getByRole("link", { name: /작업 상세 보기/ }).getAttribute("href")).toBe("#job/102")
    expect(screen.getByRole("link", { name: /관련 로그 보기/ }).getAttribute("href")).toBe("#processing/102")
    expect(screen.queryByRole("button", { name: "문제 없음" })).toBeNull()
    expect(screen.queryByRole("button", { name: "수정 필요로 기록" })).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    panel.unmount()

    render(<JobDetailPanel job={blocked} jobId={102} />)
    expect(screen.getByRole("heading", { name: "작업 중단" })).toBeTruthy()
    expect(screen.getByRole("link", { name: /확인 요청 보기/ }).getAttribute("href")).toBe("#review/quality/102")
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it("keeps a recorded correction in the queue until it is manually closed", async () => {
    let decision: "needs_correction" | "no_issue" | null = null
    const onResolved = vi.fn(async () => undefined)
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes("/quality-reviews")) {
        const open = decision !== "no_issue"
        return response({
          items: open ? [job(92, { quality_review: { required: decision === null, decision, decided_at: decision ? "2026-09-28T10:00:00+09:00" : null, summary: "음량이 낮습니다." } })] : [],
          total: open ? 1 : 0,
          limit: 50,
          offset: 0,
        })
      }
      if (url.endsWith("/quality-review") && init?.method === "POST") {
        decision = String(init.body) === "decision=needs_correction" ? "needs_correction" : "no_issue"
        return response({ ok: true })
      }
      throw new Error(`unexpected ${url}`)
    })
    vi.stubGlobal("fetch", fetchMock)
    render(<QualityReviewPanel canControl jobs={[]} selectedJobId={null} onResolved={onResolved} />)

    fireEvent.click(await screen.findByRole("button", { name: "수정 필요로 기록" }))
    expect(await screen.findByRole("button", { name: "문제 없음으로 마무리" })).toBeTruthy()
    expect(screen.queryByRole("button", { name: "수정 필요로 기록" })).toBeNull()
    expect(screen.getAllByText(/수정 필요 기록됨/).length).toBeGreaterThan(0)
    expect(screen.getByRole("link", { name: /본문 열어보기/ }).getAttribute("href")).toBe("#job/92")

    fireEvent.click(screen.getByRole("button", { name: "문제 없음으로 마무리" }))
    expect(await screen.findByText("확인할 작업이 없습니다.")).toBeTruthy()
    expect(onResolved).toHaveBeenCalledTimes(2)
    const decisions = fetchMock.mock.calls.filter(([input, init]) => String(input).endsWith("/quality-review") && init?.method === "POST").map(([, init]) => String(init?.body))
    expect(decisions).toEqual(["decision=needs_correction", "decision=no_issue"])
  })

  it("marks recorded corrections in the recent list and the completed detail", async () => {
    const reviewed = job(93, { quality_review: { required: false, decision: "needs_correction", decided_at: "2026-09-28T10:00:00+09:00", summary: "음량이 낮습니다." } })
    const fetchMock = vi.fn().mockResolvedValue(response({ job_id: 93, text: "전사 본문", truncated: false }))
    vi.stubGlobal("fetch", fetchMock)
    const overview = render(<OverviewPanel jobs={[reviewed]} processingCount={0} reviewCount={1} errorCount={0} onRefresh={vi.fn()} />)
    expect(screen.getByText("수정 필요 기록됨")).toBeTruthy()
    expect(screen.getByRole("link", { name: /확인하기/ }).getAttribute("href")).toBe("#review/quality/93")
    overview.unmount()

    render(<JobDetailPanel job={reviewed} jobId={93} />)
    expect(await screen.findByText("전사 본문", { selector: "pre" })).toBeTruthy()
    expect(screen.getByRole("heading", { name: "수정 필요 기록됨" })).toBeTruthy()
    expect(screen.getByRole("link", { name: /확인 요청 보기/ }).getAttribute("href")).toBe("#review/quality/93")
  })
})
