import { fireEvent, render, screen } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import {
  UnifiedReviewPanel,
  type UnifiedReviewRouteTarget,
} from "../src/components/panel/UnifiedReviewPanel"
import { useUnifiedReviewFeed } from "../src/hooks/useUnifiedReviewFeed"

const archiveReviewPanelMock = vi.fn()
const timetablePanelMock = vi.fn()
const titleSuggestionPanelMock = vi.fn()
const recordingLibraryPanelMock = vi.fn()

vi.mock("../src/hooks/useUnifiedReviewFeed", () => ({
  useUnifiedReviewFeed: vi.fn(),
}))

vi.mock("../src/components/panel/ArchiveReviewPanel", () => ({
  ArchiveReviewPanel: (props: { initialSelectedCaseKey?: string | null }) => {
    archiveReviewPanelMock(props)
    return <div>ArchiveReviewPanel:{props.initialSelectedCaseKey ?? "none"}</div>
  },
}))

vi.mock("../src/components/panel/TimetablePanel", () => ({
  TimetablePanel: (props: { initialSelectedProposalId?: number | null }) => {
    timetablePanelMock(props)
    return <div>TimetablePanel:{props.initialSelectedProposalId ?? "none"}</div>
  },
}))

vi.mock("../src/components/panel/TitleSuggestionPanel", () => ({
  TitleSuggestionPanel: (props: { initialSelectedProposalId?: number | null }) => {
    titleSuggestionPanelMock(props)
    return <div>TitleSuggestionPanel:{props.initialSelectedProposalId ?? "none"}</div>
  },
}))

vi.mock("../src/components/panel/RecordingLibraryPanel", () => ({
  RecordingLibraryPanel: (props: { selectedStorageKey: string | null }) => {
    recordingLibraryPanelMock(props)
    return <div>RecordingLibraryPanel:{props.selectedStorageKey ?? "none"}</div>
  },
}))

const useUnifiedReviewFeedMock = vi.mocked(useUnifiedReviewFeed)

function buildFeedReturn(overrides: Partial<ReturnType<typeof useUnifiedReviewFeed>> = {}) {
  return {
    items: [
      {
        id: "archive:CASE_001",
        source: "archive" as const,
        title: "STAT · open-case",
        stateLabel: "열림",
        reasonLabel: "latest candidate",
        timestamp: "2026-07-24T12:00:00+09:00",
        metaLabel: "capture 2 · revision 3",
        href: "#review/archive/CASE_001",
      },
      {
        id: "title:11",
        source: "title" as const,
        title: "2026-07-24 자료구조 5교시",
        stateLabel: "검토 대기",
        reasonLabel: "시간표·전사 내용 일치",
        timestamp: "2026-07-24T13:00:00+09:00",
        metaLabel: "rec_shared · transcript r2",
        href: "#review/title/11",
      },
      {
        id: "recording:rec_001",
        source: "recording" as const,
        title: "회의 메모",
        stateLabel: "열린 검토 1건",
        reasonLabel: "recording summary 기준 남은 review",
        timestamp: "2026-07-24T11:00:00+09:00",
        metaLabel: "rec_001 · 원본 1건",
        href: "#review/recording/rec_001",
      },
    ],
    ledgers: [
      {
        source: "archive" as const,
        label: "archive",
        status: "ready" as const,
        available: true,
        visibleCount: 1,
        totalCount: 1,
        truncated: false,
        note: "archive actionable 1건",
        error: null,
      },
      {
        source: "timetable" as const,
        label: "classification",
        status: "error" as const,
        available: null,
        visibleCount: 0,
        totalCount: null,
        truncated: false,
        note: "새 응답을 받지 못해 timetable source를 숨깁니다.",
        error: "classification upstream failed",
      },
      {
        source: "title" as const,
        label: "title",
        status: "ready" as const,
        available: true,
        visibleCount: 1,
        totalCount: 1,
        truncated: false,
        note: "title suggested 1건",
        error: null,
      },
      {
        source: "recording" as const,
        label: "recording",
        status: "ready" as const,
        available: true,
        visibleCount: 1,
        totalCount: 1,
        truncated: false,
        note: "visible review 1건",
        error: null,
      },
    ],
    hasHealthySource: true,
    isLoading: false,
    hasVisibleItems: true,
    pagination: {
      limit: 100,
      offset: 0,
      total: 3,
      hasPrevious: false,
      hasNext: false,
    },
    ...overrides,
  }
}

function renderPanel(selectedTarget: UnifiedReviewRouteTarget | null = null) {
  return render(<UnifiedReviewPanel selectedTarget={selectedTarget} />)
}

function expectBefore(first: Element, second: Element): void {
  expect(
    first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).not.toBe(0)
}

describe("UnifiedReviewPanel", () => {
  beforeEach(() => {
    archiveReviewPanelMock.mockReset()
    timetablePanelMock.mockReset()
    titleSuggestionPanelMock.mockReset()
    recordingLibraryPanelMock.mockReset()
    useUnifiedReviewFeedMock.mockReset()
    useUnifiedReviewFeedMock.mockReturnValue(buildFeedReturn())
  })

  it("renders the unified queue with native links, pagination summary, and the selected workbench", () => {
    renderPanel({
      source: "archive",
      caseKey: "CASE_001",
    })

    const archiveLink = screen.getByRole("link", { name: /STAT · open-case/ })
    expect(archiveLink.getAttribute("href")).toBe("#review/archive/CASE_001")
    expect(archiveLink.getAttribute("aria-current")).toBe("page")
    expect(screen.getByRole("link", { name: /회의 메모/ }).getAttribute("href")).toBe("#review/recording/rec_001")
    expect(screen.getByRole("link", { name: "#review로 돌아가기" }).getAttribute("href")).toBe("#review")
    expect(screen.getByRole("link", { name: /2026-07-24 자료구조 5교시/ }).getAttribute("href")).toBe("#review/title/11")
    expect(screen.getByText("현재 1–3 / 총 3건")).toBeTruthy()
    expect(screen.getByText("ArchiveReviewPanel:CASE_001")).toBeTruthy()
    expect(archiveReviewPanelMock).toHaveBeenCalledWith({
      initialSelectedCaseKey: "CASE_001",
    })

    const layout = screen.getByRole("region", {
      name: "Unified review queue",
    })
    const ledgerCard = screen
      .getByRole("heading", { name: "검토 source ledger" })
      .closest("article")
    const workbench = screen.getByRole("region", {
      name: "Selected review workbench",
    })
    const queueCard = screen
      .getByRole("heading", { name: "review runway" })
      .closest("article")
    expect(layout.classList.contains("has-selected-target")).toBe(true)
    expect(ledgerCard).not.toBeNull()
    expect(queueCard).not.toBeNull()
    expectBefore(ledgerCard as HTMLElement, workbench)
    expectBefore(workbench, queueCard as HTMLElement)
  })

  it("keeps the two-column ledger and queue state when no target is selected", () => {
    renderPanel()

    const layout = screen.getByRole("region", {
      name: "Unified review queue",
    })
    const ledgerCard = screen
      .getByRole("heading", { name: "검토 source ledger" })
      .closest("article")
    const queueCard = screen
      .getByRole("heading", { name: "review runway" })
      .closest("article")
    expect(layout.classList.contains("has-selected-target")).toBe(false)
    expect(ledgerCard).not.toBeNull()
    expect(queueCard).not.toBeNull()
    expectBefore(ledgerCard as HTMLElement, queueCard as HTMLElement)
    expect(
      screen.queryByRole("region", { name: "Selected review workbench" }),
    ).toBeNull()
  })

  it("shows an empty state when healthy sources report no visible review items", () => {
    useUnifiedReviewFeedMock.mockReturnValue(buildFeedReturn({
      items: [],
      ledgers: [
        {
          source: "archive",
          label: "archive",
          status: "ready",
          available: true,
          visibleCount: 0,
          totalCount: 0,
          truncated: false,
          note: "archive actionable 0건",
          error: null,
        },
        {
          source: "timetable",
          label: "classification",
          status: "unavailable",
          available: false,
          visibleCount: 0,
          totalCount: null,
          truncated: false,
          note: "classification disabled",
          error: null,
        },
        {
          source: "title",
          label: "title",
          status: "unavailable",
          available: false,
          visibleCount: 0,
          totalCount: null,
          truncated: false,
          note: "title disabled",
          error: null,
        },
        {
          source: "recording",
          label: "recording",
          status: "unavailable",
          available: false,
          visibleCount: 0,
          totalCount: null,
          truncated: false,
          note: "recording disabled",
          error: null,
        },
      ],
      hasVisibleItems: false,
      pagination: {
        limit: 100,
        offset: 0,
        total: 0,
        hasPrevious: false,
        hasNext: false,
      },
    }))

    renderPanel()

    expect(screen.getByText("지금 바로 열어야 할 review 항목이 없습니다.")).toBeTruthy()
    expect(screen.queryByText(/ArchiveReviewPanel:/)).toBeNull()
  })

  it("surfaces partial source degradation without suppressing healthy items", () => {
    renderPanel({
      source: "recording",
      storageKey: "rec_001",
    })

    expect(screen.getByText(/일부 source는 새 응답이 없어 숨겼습니다: classification/)).toBeTruthy()
    expect(screen.getByText("RecordingLibraryPanel:rec_001")).toBeTruthy()
    expect(recordingLibraryPanelMock).toHaveBeenCalledWith({
      selectedStorageKey: "rec_001",
    })
  })

  it("switches workbench panels when unified review target changes", () => {
    const result = renderPanel({
      source: "archive",
      caseKey: "CASE_001",
    })

    expect(screen.getByText("ArchiveReviewPanel:CASE_001")).toBeTruthy()
    expect(archiveReviewPanelMock).toHaveBeenCalledWith({
      initialSelectedCaseKey: "CASE_001",
    })

    result.rerender(<UnifiedReviewPanel selectedTarget={{ source: "timetable", proposalId: 7 }} />)
    expect(screen.getByText("TimetablePanel:7")).toBeTruthy()
    expect(screen.queryByText("ArchiveReviewPanel:CASE_001")).toBeNull()
    expect(timetablePanelMock).toHaveBeenCalledWith({
      initialSelectedProposalId: 7,
    })

    result.rerender(<UnifiedReviewPanel selectedTarget={{ source: "recording", storageKey: "rec_001" }} />)
    expect(screen.getByText("RecordingLibraryPanel:rec_001")).toBeTruthy()
    expect(screen.queryByText("TimetablePanel:7")).toBeNull()
    expect(recordingLibraryPanelMock).toHaveBeenCalledWith({
      selectedStorageKey: "rec_001",
    })

    result.rerender(
      <UnifiedReviewPanel
        selectedTarget={{ source: "title", proposalId: 11 }}
      />,
    )
    expect(screen.getByText("TitleSuggestionPanel:11")).toBeTruthy()
    expect(screen.queryByText("RecordingLibraryPanel:rec_001")).toBeNull()
    expect(titleSuggestionPanelMock).toHaveBeenCalledWith({
      initialSelectedProposalId: 11,
    })
  })

  it("advances and rewinds server pagination through hook offsets", () => {
    useUnifiedReviewFeedMock.mockImplementation(({ offset = 0 } = {}) =>
      buildFeedReturn({
        items: [
          {
            id: `archive:CASE_${offset === 0 ? "001" : "101"}`,
            source: "archive",
            title: offset === 0 ? "첫 페이지" : "다음 페이지",
            stateLabel: "열림",
            reasonLabel: "latest candidate",
            timestamp: "2026-07-24T12:00:00+09:00",
            metaLabel: "capture 1 · revision 1",
            href: `#review/archive/CASE_${offset === 0 ? "001" : "101"}`,
          },
        ],
        pagination: {
          limit: 100,
          offset,
          total: 201,
          hasPrevious: offset > 0,
          hasNext: offset < 200,
        },
      }),
    )

    renderPanel()

    expect(screen.getByText("현재 1–1 / 총 201건")).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "다음 100건" }))
    expect(screen.getByText("현재 101–101 / 총 201건")).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "이전 100건" }))
    expect(screen.getByText("현재 1–1 / 총 201건")).toBeTruthy()
    expect(useUnifiedReviewFeedMock.mock.calls.map(([args]) => args?.offset ?? 0)).toContain(100)
  })

  it("clamps the current offset when the server total shrinks below the current page", () => {
    let totalShrunk = false
    useUnifiedReviewFeedMock.mockImplementation(({ offset = 0 } = {}) => {
      if (offset === 100) {
        totalShrunk = true
        return buildFeedReturn({
          items: [],
          ledgers: [
            {
              source: "archive",
              label: "archive",
              status: "ready",
              available: true,
              visibleCount: 0,
              totalCount: 5,
              truncated: false,
              note: "archive actionable 5건",
              error: null,
            },
            {
              source: "timetable",
              label: "classification",
              status: "unavailable",
              available: false,
              visibleCount: 0,
              totalCount: null,
              truncated: false,
              note: "classification disabled",
              error: null,
            },
            {
              source: "title",
              label: "title",
              status: "unavailable",
              available: false,
              visibleCount: 0,
              totalCount: null,
              truncated: false,
              note: "title disabled",
              error: null,
            },
            {
              source: "recording",
              label: "recording",
              status: "unavailable",
              available: false,
              visibleCount: 0,
              totalCount: null,
              truncated: false,
              note: "recording disabled",
              error: null,
            },
          ],
          hasVisibleItems: false,
          pagination: {
            limit: 100,
            offset: 100,
            total: 5,
            hasPrevious: true,
            hasNext: false,
          },
        })
      }
      return buildFeedReturn({
        items: [
          {
            id: "archive:CASE_001",
            source: "archive",
            title: "clamped page",
            stateLabel: "열림",
            reasonLabel: "latest candidate",
            timestamp: "2026-07-24T12:00:00+09:00",
            metaLabel: "capture 1 · revision 1",
            href: "#review/archive/CASE_001",
          },
        ],
        pagination: {
          limit: 100,
          offset: 0,
          total: totalShrunk ? 5 : 201,
          hasPrevious: false,
          hasNext: !totalShrunk,
        },
      })
    })

    renderPanel()

    fireEvent.click(screen.getByRole("button", { name: "다음 100건" }))
    expect(screen.getByText("현재 1–1 / 총 5건")).toBeTruthy()
    expect(useUnifiedReviewFeedMock.mock.calls.map(([args]) => args?.offset ?? 0)).toEqual(
      expect.arrayContaining([0, 100, 0]),
    )
  })
})
