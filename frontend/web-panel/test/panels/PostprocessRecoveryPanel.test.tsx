import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { PostprocessRecoveryPanel } from "../../src/components/panel/PostprocessRecoveryPanel"
import type { RecoveryDetail, RecoveryJob } from "../../src/lib/decodePostprocessRecovery"
import { buildJsonResponse } from "../api/response"
import { deferred } from "../hooks/deferred"
import { createQueryWrapper } from "../hooks/queryHarness"

const base = "/api/postprocess/recovery"
const txtPin = "a".repeat(64)
const jsonPin = "b".repeat(64)
const item: RecoveryJob = {
  id: 42, title: "광명동 녹음", recorded_at: "2026-11-27T10:00:00+09:00",
  reason: "강의로 분류되지 않음", status: "SKIPPED", course_code: null,
  correction_status: "BLOCKED", summary_status: "BLOCKED", delivery_status: "BLOCKED",
}
const detail = (): RecoveryDetail => ({
  job: { ...item }, courses: [{ code: "DB", name: "데이터베이스" }],
  can_requeue: true, source_txt_sha256: txtPin, source_json_sha256: jsonPin,
})

function setup(options: {
  canControl?: boolean
  list?: (params: URLSearchParams) => Response | Promise<Response>
  details?: () => Response | Promise<Response>
  post?: () => Response | Promise<Response>
} = {}) {
  const writes: RequestInit[] = []
  const getList = options.list ?? ((params) => buildJsonResponse(200, {
    enabled: true, total: params.get("view") === "missing" ? 1 : 0, items: params.get("view") === "missing" ? [item] : [],
  }))
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.startsWith(base + "?")) return getList(new URLSearchParams(url.split("?")[1]))
    if (url === base + "/42" && !init?.method) return options.details?.() ?? buildJsonResponse(200, detail())
    if (url === base + "/42/requeue" && init?.method === "POST") {
      writes.push(init)
      return options.post?.() ?? buildJsonResponse(200, { id: 42, status: "PENDING" })
    }
    throw new Error("unexpected request " + url)
  })
  vi.stubGlobal("fetch", fetcher)
  const result = render(<PostprocessRecoveryPanel canControl={options.canControl ?? true} />, { wrapper: createQueryWrapper() })
  return { ...result, writes, fetcher }
}

async function openJob() {
  fireEvent.click(await screen.findByRole("button", { name: /광명동 녹음/ }))
  return screen.findByRole("region", { name: "선택한 녹음" })
}
async function chooseCourse() {
  const select = await screen.findByRole("combobox", { name: "확정할 과목" })
  await waitFor(() => expect((select as HTMLSelectElement).disabled).toBe(false))
  fireEvent.change(select, { target: { value: "DB" } })
  return screen.getByRole("button", { name: "과목 확정 및 재등록" })
}
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe("PostprocessRecoveryPanel", () => {
  it("requires explicit selection and confirmation and sends one pinned request before tracking delivery", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const pending = deferred<Response>()
    const confirmed = detail()
    confirmed.job = { ...item, status: "PENDING", course_code: "DB", correction_status: "PENDING", reason: "운영자 과목 확정" }
    confirmed.courses = []
    confirmed.can_requeue = false
    confirmed.source_txt_sha256 = null
    confirmed.source_json_sha256 = null
    let accepted = false
    let delivered = false
    const { writes, fetcher } = setup({
      list: (p) => {
        const show = accepted ? p.get("view") === "confirmed" : p.get("view") === "missing"
        const row = accepted ? { ...confirmed.job, status: delivered ? "DELIVERED" : "PENDING" } : item
        return buildJsonResponse(200, { enabled: true, total: Number(show), items: show ? [row] : [] })
      },
      details: () => {
        if (delivered) Object.assign(confirmed.job, {
          status: "DELIVERED", correction_status: "READY", summary_status: "READY", delivery_status: "DELIVERED",
        })
        return buildJsonResponse(200, accepted ? confirmed : detail())
      },
      post: () => pending.promise,
    })
    await openJob()
    const initial = await screen.findByRole("combobox", { name: "확정할 과목" })
    expect((initial as HTMLSelectElement).value).toBe("")
    expect((screen.getByRole("button", { name: "과목 확정 및 재등록" }) as HTMLButtonElement).disabled).toBe(true)
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValue(true)
    const button = await chooseCourse()
    fireEvent.click(button)
    expect(confirm).toHaveBeenCalledWith(expect.stringMatching(/#42.*광명동 녹음.*데이터베이스.*DB/s))
    expect(writes).toHaveLength(0)
    fireEvent.click(button)
    fireEvent.click(button)
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(JSON.parse(String(writes[0].body))).toEqual({
      course_code: "DB", source_txt_sha256: txtPin, source_json_sha256: jsonPin,
    })
    expect(screen.queryByText(txtPin)).toBeNull()
    accepted = true
    await act(async () => pending.resolve(buildJsonResponse(200, { id: 42, status: "PENDING" })))
    await waitFor(() => expect(screen.getByRole("button", { name: "확정 이력" }).getAttribute("aria-pressed")).toBe("true"))
    await waitFor(() => expect(within(screen.getByRole("region", { name: "선택한 녹음" })).getByText("DB")).toBeTruthy())
    delivered = true
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    const tracked = screen.getByRole("region", { name: "선택한 녹음" })
    await waitFor(() => expect(within(tracked).getAllByText("전달 완료")).toHaveLength(2))
    const n = fetcher.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(5100) })
    expect(fetcher.mock.calls).toHaveLength(n)
    expect(writes).toHaveLength(1)
  })
  it("discards a stale selection after a conflict and does not resubmit on refetch", async () => {
    let conflicted = false
    vi.spyOn(window, "confirm").mockReturnValue(true)
    const { writes } = setup({
      details: () => buildJsonResponse(200, { ...detail(), source_txt_sha256: conflicted ? "c".repeat(64) : txtPin }),
      post: () => { conflicted = true; return buildJsonResponse(409, { error: "원전사가 변경됐습니다." }) },
    })
    await openJob()
    fireEvent.click(await chooseCourse())
    await screen.findByRole("alert")
    await waitFor(() => {
      const select = screen.getByRole("combobox") as HTMLSelectElement
      expect(select.value).toBe("")
      expect(select.disabled).toBe(false)
    })
    expect(writes).toHaveLength(1)
    expect((screen.getByRole("button", { name: "과목 확정 및 재등록" }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(await chooseCourse())
    await waitFor(() => expect(writes).toHaveLength(2))
    expect(JSON.parse(String(writes[1].body)).source_txt_sha256).toBe("c".repeat(64))
  })
  it.each([true, false])("only offers a mutation when both server and panel allow it (%s)", async (canControl) => {
    const data = detail()
    if (canControl) Object.assign(data, { can_requeue: false, source_txt_sha256: null, source_json_sha256: null })
    const { writes } = setup({ canControl, details: () => buildJsonResponse(200, data) })
    await openJob()
    await screen.findByText(canControl ? /현재 등록할 수 없습니다/ : /조회 전용입니다/)
    expect(screen.queryByRole("button", { name: "과목 확정 및 재등록" })).toBeNull()
    expect(writes).toHaveLength(0)
  })
  it("shows disabled and error states and allows a read retry", async () => {
    let unavailable = true
    const { fetcher } = setup({ list: () => unavailable
      ? buildJsonResponse(503, { error: "일시적 오류" })
      : buildJsonResponse(200, { enabled: false, total: 0, items: [] }) })
    expect((await screen.findByRole("alert")).textContent).toContain("일시적 오류")
    unavailable = false
    fireEvent.click(screen.getByRole("button", { name: "다시 조회" }))
    await screen.findByText(/후처리가 꺼져 있습니다/)
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
  it("changes pages without fetching transcript bodies, and resets paging for confirmed history", async () => {
    const { fetcher } = setup({ list: (p) => buildJsonResponse(200,
      p.get("view") === "confirmed" ? { enabled: true, total: 0, items: [] }
      : { enabled: true, total: 21, items: [{ ...item, title: p.get("offset") === "20" ? "마지막 녹음" : item.title }] }) })
    fireEvent.click(await screen.findByRole("button", { name: "다음" }))
    await screen.findByRole("button", { name: /마지막 녹음/ })
    fireEvent.click(screen.getByRole("button", { name: "확정 이력" }))
    await screen.findByText("과목을 확정한 녹음이 없습니다.")
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      base + "?view=missing&limit=20&offset=0", base + "?view=missing&limit=20&offset=20", base + "?view=confirmed&limit=20&offset=0",
    ])
  })
  it("fails closed on mismatched detail IDs and invalid source pins", async () => {
    let differentId = true
    const { writes } = setup({ details: () => buildJsonResponse(200, differentId
      ? { ...detail(), job: { ...item, id: 21 } }
      : { ...detail(), source_json_sha256: "invalid" }) })
    await openJob()
    await screen.findByRole("alert")
    expect(screen.queryByRole("combobox")).toBeNull()
    differentId = false
    fireEvent.click(screen.getByRole("button", { name: "다시 조회" }))
    await screen.findByRole("alert")
    expect(screen.queryByRole("combobox")).toBeNull()
    expect(writes).toHaveLength(0)
  })
})
