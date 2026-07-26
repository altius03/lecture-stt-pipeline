import { fireEvent, render, screen, within } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { TranscriptionAnalyticsPanel } from "../src/components/panel/TranscriptionAnalyticsPanel"
import { useTranscriptionAnalytics } from "../src/hooks/useTranscriptionAnalytics"
import type { TranscriptionAnalyticsPayload } from "../src/types"
import { buildTranscriptionAnalyticsPayload } from "./transcriptionAnalyticsFixtures"

vi.mock("../src/hooks/useTranscriptionAnalytics", () => ({
  useTranscriptionAnalytics: vi.fn(),
}))

const useAnalyticsMock = vi.mocked(useTranscriptionAnalytics)
const selectPeriodMock = vi.fn()
const retryMock = vi.fn(async () => true)

function mockAnalyticsHook(
  overrides: Partial<ReturnType<typeof useTranscriptionAnalytics>> = {},
) {
  useAnalyticsMock.mockReturnValue({
    selectedPeriod: "week",
    selectPeriod: selectPeriodMock,
    analytics: buildTranscriptionAnalyticsPayload("week"),
    isLoading: false,
    isRefreshing: false,
    error: null,
    retry: retryMock,
    ...overrides,
  })
}

describe("TranscriptionAnalyticsPanel", () => {
  beforeEach(() => {
    selectPeriodMock.mockReset()
    retryMock.mockReset()
    retryMock.mockResolvedValue(true)
    useAnalyticsMock.mockReset()
    mockAnalyticsHook()
  })

  it("shows day, week, and month tabs and delegates period changes", () => {
    render(<TranscriptionAnalyticsPanel />)

    expect(
      screen.getByRole("tab", { name: /최근 7일/ }).getAttribute(
        "aria-selected",
      ),
    ).toBe("true")

    fireEvent.click(screen.getByRole("tab", { name: /오늘/ }))
    fireEvent.click(screen.getByRole("tab", { name: /최근 30일/ }))

    expect(selectPeriodMock).toHaveBeenNthCalledWith(1, "day")
    expect(selectPeriodMock).toHaveBeenNthCalledWith(2, "month")
  })

  it("supports roving keyboard navigation across period tabs", () => {
    render(<TranscriptionAnalyticsPanel />)

    const weekTab = screen.getByRole("tab", { name: /최근 7일/ })
    const monthTab = screen.getByRole("tab", { name: /최근 30일/ })
    const dayTab = screen.getByRole("tab", { name: /오늘/ })

    weekTab.focus()
    fireEvent.keyDown(weekTab, { key: "ArrowRight" })
    expect(selectPeriodMock).toHaveBeenLastCalledWith("month")
    expect(document.activeElement).toBe(monthTab)

    fireEvent.keyDown(monthTab, { key: "Home" })
    expect(selectPeriodMock).toHaveBeenLastCalledWith("day")
    expect(document.activeElement).toBe(dayTab)

    expect(weekTab.getAttribute("aria-controls")).toBe(
      "transcription-analytics-period-panel",
    )
    expect(weekTab.getAttribute("tabindex")).toBe("0")
    expect(monthTab.getAttribute("tabindex")).toBe("-1")
  })

  it("states the disabled boundary without rendering fabricated metrics", () => {
    mockAnalyticsHook({
      analytics: buildTranscriptionAnalyticsPayload("week", false),
    })

    render(<TranscriptionAnalyticsPanel />)

    expect(
      screen.getByRole("heading", {
        name: "Storage v2 분석 API가 비활성화되어 있습니다.",
      }),
    ).toBeTruthy()
    expect(
      screen.getByText(
        /운영 데이터에는 자동 연결하지 않았습니다/,
      ),
    ).toBeTruthy()
    expect(screen.getByText("feature_disabled")).toBeTruthy()
    expect(
      screen.getByText(/웹 업로드와 백그라운드 polling은 추가하지 않았습니다/),
    ).toBeTruthy()
    expect(screen.queryByRole("tabpanel")).toBeNull()
    expect(screen.queryByLabelText("최근 7일 핵심 지표")).toBeNull()
  })

  it("renders KPIs, distributions, attention links, and no private content", () => {
    const analytics = buildTranscriptionAnalyticsPayload("week")
    const privateValues = {
      source_path: "/Users/private/iCloud/recording.m4a",
      transcript_body: "절대 노출하면 안 되는 전사 전문",
      summary: "절대 노출하면 안 되는 요약",
      error_message: "절대 노출하면 안 되는 내부 오류",
    }
    Object.assign(
      analytics.recent_attention[0] as unknown as Record<string, unknown>,
      privateValues,
    )
    mockAnalyticsHook({ analytics })

    render(<TranscriptionAnalyticsPanel />)

    const kpis = screen.getByLabelText("최근 7일 핵심 지표")
    expect(within(kpis).getByText("4")).toBeTruthy()
    expect(within(kpis).getByText("50%")).toBeTruthy()
    expect(within(kpis).getByText("2")).toBeTruthy()
    expect(within(kpis).getByText("82점")).toBeTruthy()
    expect(within(kpis).getByText("3/4건 점수 표본")).toBeTruthy()

    expect(
      screen.getByRole("list", {
        name: "최근 7일 전사량 추이. 총 4건",
      }),
    ).toBeTruthy()
    expect(
      screen.getByRole("listitem", {
        name: "7월 1일: 2건, 완료 1건, 검토 1건, 오류 0건",
      }),
    ).toBeTruthy()
    expect(screen.getByText("90–100점")).toBeTruthy()
    expect(screen.getByText("점수 없음")).toBeTruthy()
    expect(
      screen.getByText(/점수 있음 3 · 없음 1 · 유효하지 않음 0/),
    ).toBeTruthy()
    expect(
      screen.getByText(/오디오 1시간 30분 \(3\/4\) · 처리 6분 \(3\/4\)/),
    ).toBeTruthy()

    expect(screen.getByText("수업")).toBeTruthy()
    expect(screen.getByText("시간표 확정")).toBeTruthy()
    expect(screen.getByText("회의·대화")).toBeTruthy()
    expect(screen.getAllByText("직접 확정")).toHaveLength(2)
    expect(screen.getByText(/자동 확정하지 않고 미분류로 남습니다/)).toBeTruthy()

    const displayName = screen.getByText("2026-07-23 자료구조 5교시")
    const displayCell = displayName.closest("td")
    expect(displayCell).not.toBeNull()
    expect(
      within(displayCell as HTMLTableCellElement).getByText("rec_review_01")
        .tagName,
    ).toBe("CODE")

    expect(
      screen.getByRole("link", { name: "2026-07-23 자료구조 5교시 상세 보기" }).getAttribute("href"),
    ).toBe("#library/rec_review_01")
    expect(
      screen.getByRole("link", { name: "프로젝트 회의 메모 상세 보기" }).getAttribute("href"),
    ).toBe("#library/rec_error_02")
    expect(screen.getByRole("table").closest(".table-wrap")).not.toBeNull()

    for (const privateValue of Object.values(privateValues)) {
      expect(screen.queryByText(privateValue)).toBeNull()
    }
    expect(
      screen.queryByRole("button", { name: /업로드/ }),
    ).toBeNull()
  })

  it("keeps the API error visible and retries only on explicit action", () => {
    mockAnalyticsHook({
      analytics: null,
      error: "격리 분석 DB를 열 수 없습니다.",
    })

    render(<TranscriptionAnalyticsPanel />)

    expect(
      screen.getByText("격리 분석 DB를 열 수 없습니다."),
    ).toBeTruthy()
    expect(retryMock).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole("button", { name: "다시 집계" }))
    expect(retryMock).toHaveBeenCalledTimes(1)
  })
})
