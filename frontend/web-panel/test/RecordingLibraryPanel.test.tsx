import { render, screen, within } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { RecordingLibraryPanel } from "../src/components/panel/RecordingLibraryPanel"
import { useRecordingLibrary } from "../src/hooks/useRecordingLibrary"
import {
  buildRecordingDetailPayload,
  buildRecordingLibraryListPayload,
} from "./recordingLibraryFixtures"

vi.mock("../src/hooks/useRecordingLibrary", () => ({
  useRecordingLibrary: vi.fn(),
}))

const useRecordingLibraryMock = vi.mocked(useRecordingLibrary)

function mockLibraryHook(
  overrides: Partial<ReturnType<typeof useRecordingLibrary>> = {},
) {
  useRecordingLibraryMock.mockReturnValue({
    list: buildRecordingLibraryListPayload(),
    detail: buildRecordingDetailPayload(),
    listLoading: false,
    detailLoading: false,
    listRefreshing: false,
    detailRefreshing: false,
    listError: null,
    detailError: null,
    retryList: vi.fn(async () => true),
    retryDetail: vi.fn(async () => true),
    ...overrides,
  })
}

describe("RecordingLibraryPanel", () => {
  beforeEach(() => {
    useRecordingLibraryMock.mockReset()
    mockLibraryHook()
  })

  it("renders summary links, revision lanes, and review codes", () => {
    render(<RecordingLibraryPanel selectedStorageKey="rec_2026_07_23_ds_05" />)

    expect(
      screen.getByRole("link", { name: /2026-07-23 자료구조 5교시/ }).getAttribute("href"),
    ).toBe("#library/rec_2026_07_23_ds_05")
    expect(screen.getByText("storage_key와 표시 이름을 분리한 목록")).toBeTruthy()
    expect(screen.getByText("Selected Recording")).toBeTruthy()
    expect(screen.getByText("quality_score_low")).toBeTruthy()
    expect(screen.getByText(/quality_scorecard r1/)).toBeTruthy()
    expect(screen.getAllByText("원본").length).toBeGreaterThan(0)
    expect(screen.getAllByText("전사").length).toBeGreaterThan(0)
    expect(screen.getAllByText("교정").length).toBeGreaterThan(0)
    expect(screen.getAllByText("요약").length).toBeGreaterThan(0)

    const selectedRow = screen.getByRole("link", {
      name: /2026-07-23 자료구조 5교시/,
    }).closest("tr")
    expect(selectedRow?.className).toContain("is-selected")
  })

  it("keeps detail visible when the selected key is outside the current page", () => {
    const list = buildRecordingLibraryListPayload()
    list.summaries = [list.summaries[1]]
    list.counts.recordings = 3
    list.total = 3
    mockLibraryHook({
      list,
      detail: buildRecordingDetailPayload("rec_2026_07_23_ds_05"),
    })

    render(<RecordingLibraryPanel selectedStorageKey="rec_2026_07_23_ds_05" />)

    expect(
      screen.getByText(/현재 선택한 녹음은 첫 페이지 밖에 있어도 detail을 별도 read-only 조회로 유지합니다/),
    ).toBeTruthy()
    expect(
      screen.getByRole("heading", { name: "2026-07-23 자료구조 5교시" }),
    ).toBeTruthy()
  })

  it("shows a fallback label for a backend-valid blank optional profile", () => {
    const detail = buildRecordingDetailPayload()
    detail.jobs[0].requested_profile = ""
    mockLibraryHook({ detail })

    render(<RecordingLibraryPanel selectedStorageKey="rec_2026_07_23_ds_05" />)

    expect(screen.getByText(/profile 없음 · v1/)).toBeTruthy()
  })

  it("shows the disabled boundary without rendering detail", () => {
    mockLibraryHook({
      list: buildRecordingLibraryListPayload(false),
      detail: null,
    })

    render(<RecordingLibraryPanel selectedStorageKey="rec_2026_07_23_ds_05" />)

    expect(
      screen.getByRole("heading", {
        name: "Storage v2 보관함 API가 비활성화되어 있습니다.",
      }),
    ).toBeTruthy()
    expect(screen.getByText("recording_library_disabled")).toBeTruthy()
    expect(screen.queryByText("quality_score_low")).toBeNull()
  })
})
