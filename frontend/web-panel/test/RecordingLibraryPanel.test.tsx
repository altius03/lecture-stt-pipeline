import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { RecordingLibraryPanel } from "../src/components/panel/RecordingLibraryPanel"
import { useRecordingLibrary } from "../src/hooks/useRecordingLibrary"
import { useTranscriptPreview } from "../src/hooks/useTranscriptPreview"
import {
  buildRecordingDetailPayload,
  buildRecordingLibraryListPayload,
  buildTranscriptPreviewPayload,
} from "./recordingLibraryFixtures"

vi.mock("../src/hooks/useRecordingLibrary", () => ({
  useRecordingLibrary: vi.fn(),
}))

vi.mock("../src/hooks/useTranscriptPreview", () => ({
  useTranscriptPreview: vi.fn(),
}))

const useRecordingLibraryMock = vi.mocked(useRecordingLibrary)
const useTranscriptPreviewMock = vi.mocked(useTranscriptPreview)

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

function mockTranscriptPreviewHook(
  overrides: Partial<ReturnType<typeof useTranscriptPreview>> = {},
) {
  useTranscriptPreviewMock.mockReturnValue({
    isOpen: false,
    preview: null,
    previewLoading: false,
    previewRefreshing: false,
    previewError: null,
    previewErrorStatus: null,
    openPreview: vi.fn(),
    closePreview: vi.fn(async () => {}),
    retryPreview: vi.fn(async () => true),
    ...overrides,
  })
}

describe("RecordingLibraryPanel", () => {
  beforeEach(() => {
    useRecordingLibraryMock.mockReset()
    useTranscriptPreviewMock.mockReset()
    mockLibraryHook()
    mockTranscriptPreviewHook()
    vi.useRealTimers()
  })

  afterEach(() => {
    vi.restoreAllMocks()
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

  it("shows the preview affordance only for a done current job with a latest transcript", () => {
    mockLibraryHook({
      detail: buildRecordingDetailPayload("rec_meeting_2026_07_22"),
    })

    const { rerender } = render(
      <RecordingLibraryPanel selectedStorageKey="rec_meeting_2026_07_22" />,
    )

    expect(
      screen.getByRole("button", { name: "미리보기 열기" }),
    ).toBeTruthy()

    mockLibraryHook()
    rerender(<RecordingLibraryPanel selectedStorageKey="rec_2026_07_23_ds_05" />)
    expect(screen.queryByRole("button", { name: "미리보기 열기" })).toBeNull()
  })

  it("hides the preview affordance when list capabilities disable transcript preview", () => {
    const list = buildRecordingLibraryListPayload()
    list.capabilities.transcript_preview = false
    mockLibraryHook({
      list,
      detail: buildRecordingDetailPayload("rec_meeting_2026_07_22"),
    })

    render(<RecordingLibraryPanel selectedStorageKey="rec_meeting_2026_07_22" />)

    expect(screen.queryByRole("button", { name: "미리보기 열기" })).toBeNull()
    expect(screen.queryByText("Transcript Preview")).toBeNull()
  })

  it("hides the preview affordance when legacy list payload omits capabilities", () => {
    const list = buildRecordingLibraryListPayload()
    delete (list as Partial<typeof list>).capabilities
    mockLibraryHook({
      list: list as ReturnType<typeof buildRecordingLibraryListPayload>,
      detail: buildRecordingDetailPayload("rec_meeting_2026_07_22"),
    })

    render(<RecordingLibraryPanel selectedStorageKey="rec_meeting_2026_07_22" />)

    expect(screen.queryByRole("button", { name: "미리보기 열기" })).toBeNull()
    expect(screen.queryByText("Transcript Preview")).toBeNull()
  })

  it("renders retryable preview error states with close control", () => {
    mockLibraryHook({
      detail: buildRecordingDetailPayload("rec_meeting_2026_07_22"),
    })
    mockTranscriptPreviewHook({
      isOpen: true,
      previewError: "전사 미리보기를 아직 열 수 없습니다.",
      previewErrorStatus: 409,
    })

    render(<RecordingLibraryPanel selectedStorageKey="rec_meeting_2026_07_22" />)

    expect(screen.getByText("검증 실패")).toBeTruthy()
    expect(screen.getByText("전사 미리보기를 아직 열 수 없습니다.")).toBeTruthy()
    expect(screen.getByRole("button", { name: "다시 읽기" })).toBeTruthy()
    expect(screen.getByRole("button", { name: "닫기" })).toBeTruthy()
  })

  it("switches the copy button label on clipboard success and restores it", async () => {
    vi.useFakeTimers()
    const writeText = vi.fn(async () => {})
    Object.defineProperty(globalThis.navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    })
    mockLibraryHook({
      detail: buildRecordingDetailPayload("rec_meeting_2026_07_22"),
    })
    mockTranscriptPreviewHook({
      isOpen: true,
      preview: buildTranscriptPreviewPayload("rec_meeting_2026_07_22"),
    })

    render(<RecordingLibraryPanel selectedStorageKey="rec_meeting_2026_07_22" />)

    const copyButton = screen.getByRole("button", { name: "전체 복사" })
    await act(async () => {
      fireEvent.click(copyButton)
      await Promise.resolve()
    })

    expect(writeText).toHaveBeenCalledWith("회의 안건 정리입니다.\n다음 액션은 테스트 추가입니다.")
    expect(screen.getByRole("button", { name: "복사됨" })).toBeTruthy()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500)
    })
    expect(screen.getByRole("button", { name: "전체 복사" })).toBeTruthy()
  })

  it("falls back to execCommand copy in non-secure contexts and keeps focus on the copy button", async () => {
    const writeText = vi.fn(async () => {
      throw new Error("NotAllowedError")
    })
    Object.defineProperty(globalThis.navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    })
    const execCommandSpy = vi.fn(() => true)
    Object.defineProperty(document, "execCommand", {
      value: execCommandSpy,
      configurable: true,
      writable: true,
    })
    mockLibraryHook({
      detail: buildRecordingDetailPayload("rec_meeting_2026_07_22"),
    })
    mockTranscriptPreviewHook({
      isOpen: true,
      preview: buildTranscriptPreviewPayload("rec_meeting_2026_07_22"),
    })

    render(<RecordingLibraryPanel selectedStorageKey="rec_meeting_2026_07_22" />)

    const copyButton = screen.getByRole("button", { name: "전체 복사" })
    copyButton.focus()
    fireEvent.click(copyButton)

    await waitFor(() => {
      expect(execCommandSpy).toHaveBeenCalledWith("copy")
    })
    expect(document.activeElement).toBe(copyButton)
  })

  it("shows inline copy failure when clipboard and fallback copy both fail", async () => {
    const writeText = vi.fn(async () => {
      throw new Error("NotAllowedError")
    })
    Object.defineProperty(globalThis.navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    })
    const execCommandSpy = vi.fn(() => false)
    Object.defineProperty(document, "execCommand", {
      value: execCommandSpy,
      configurable: true,
      writable: true,
    })
    mockLibraryHook({
      detail: buildRecordingDetailPayload("rec_meeting_2026_07_22"),
    })
    mockTranscriptPreviewHook({
      isOpen: true,
      preview: buildTranscriptPreviewPayload("rec_meeting_2026_07_22"),
    })

    render(<RecordingLibraryPanel selectedStorageKey="rec_meeting_2026_07_22" />)

    fireEvent.click(screen.getByRole("button", { name: "전체 복사" }))

    const [visibleError] = await screen.findAllByText(
      "브라우저가 전사 본문 복사를 허용하지 않았습니다.",
    )
    expect(visibleError.className).toContain("inline-error")
    expect(execCommandSpy).toHaveBeenCalledWith("copy")
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
