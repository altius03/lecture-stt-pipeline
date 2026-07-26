import { act, render, screen, waitFor, within } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import App from "../src/App"
import { PANEL_EVENTS_ENDPOINT, PANEL_STATE_EVENTS_ENDPOINT } from "../src/lib/panelApi"
import type { PanelState } from "../src/types"

interface UsePanelStateArgs {
  logsEnabled?: boolean
}

const usePanelStateMock = vi.fn()
const usePanelLogsMock = vi.fn()
const recordingLibraryPanelMock = vi.fn()
const unifiedReviewPanelMock = vi.fn()

vi.mock("../src/hooks/useThemeMode", () => ({
  useThemeMode: () => ({
    theme: "dark",
    setThemeMode: vi.fn(),
  }),
}))

vi.mock("../src/hooks/usePanelLogs", () => ({
  usePanelLogs: (...args: Parameters<typeof usePanelLogsMock>) => usePanelLogsMock(...args),
}))

vi.mock("../src/hooks/usePanelState", () => ({
  usePanelState: (...args: [UsePanelStateArgs] | []) => usePanelStateMock(...args),
}))

vi.mock("../src/components/panel/PanelHero", () => ({
  PanelHero: () => <div>PanelHero</div>,
}))

vi.mock("../src/components/panel/ProcessingCard", () => ({
  ProcessingCard: () => <div>ProcessingCard</div>,
}))

vi.mock("../src/components/panel/RuntimeCard", () => ({
  RuntimeCard: () => <div>RuntimeCard</div>,
}))

vi.mock("../src/components/panel/ActivityPanel", () => ({
  ActivityPanel: () => <div>ActivityPanel</div>,
}))

vi.mock("../src/components/panel/ArchiveReviewPanel", () => ({
  ArchiveReviewPanel: () => <div>ArchiveReviewPanel</div>,
}))

vi.mock("../src/components/panel/TimetablePanel", () => ({
  TimetablePanel: () => <div>TimetablePanel</div>,
}))

vi.mock("../src/components/panel/RecordingLibraryPanel", () => ({
  RecordingLibraryPanel: (props: { selectedStorageKey: string | null }) => {
    recordingLibraryPanelMock(props)
    return <div>RecordingLibraryPanel:{props.selectedStorageKey ?? "none"}</div>
  },
}))

vi.mock("../src/components/panel/UnifiedReviewPanel", () => ({
  UnifiedReviewPanel: (props: {
    selectedTarget:
      | { source: "archive"; caseKey: string }
      | { source: "timetable"; proposalId: number }
      | { source: "title"; proposalId: number }
      | { source: "recording"; storageKey: string }
      | null
  }) => {
    unifiedReviewPanelMock(props)
    const selectedLabel =
      props.selectedTarget === null
        ? "none"
        : props.selectedTarget.source === "archive"
          ? `archive:${props.selectedTarget.caseKey}`
          : props.selectedTarget.source === "timetable"
            ? `timetable:${props.selectedTarget.proposalId}`
            : props.selectedTarget.source === "title"
              ? `title:${props.selectedTarget.proposalId}`
              : `recording:${props.selectedTarget.storageKey}`
    return <div>UnifiedReviewPanel:{selectedLabel}</div>
  },
}))

vi.mock("../src/components/panel/TranscriptionAnalyticsPanel", () => ({
  TranscriptionAnalyticsPanel: () => <div>TranscriptionAnalyticsPanel</div>,
}))

function createState(): PanelState {
  return {
    schema_version: 2,
    runtime_state: {
      status: "running",
      source: "web",
      paused: false,
      managed_running: true,
      external_worker_pids: [],
      label: "실행 중",
      description: "워커가 실행 중입니다.",
    },
    notification: {
      selection: "disabled",
      selected_label: "끄기",
      apply_label: "즉시 적용",
      restart_required: false,
      can_apply_now: true,
      options: [],
    },
    counts: {
      PENDING: 1,
      PROCESSING: 1,
      NEEDS_REVIEW: 2,
      DONE: 3,
      ERROR: 4,
      UNREGISTERED: 5,
    },
    folders: {
      inbox: "/tmp/inbox",
      audio: "/tmp/audio",
      transcripts: "/tmp/transcripts",
      errors: "/tmp/errors",
    },
    actions: {
      pause_action: "pause",
      pause_label: "일시정지",
      endpoints: {
        state: "/api/state",
        logs: "/api/logs",
        notification: "/api/notification",
        start: "/api/start",
        pause: "/api/pause",
        resume: "/api/resume",
        stop: "/api/stop",
        refresh: "/api/refresh",
        exit: "/api/exit",
        clear_history: "/api/clear_history",
      },
    },
    summary: {
      current_job: null,
      progress_label: "대기",
      eta_label: "-",
      notice: "",
      refresh_hint: "1초",
      updated_at: "2026-07-23T09:00:00+09:00",
      poll_interval_sec: 1,
    },
    jobs_v2: [],
    processing_v2: [],
  }
}

beforeEach(() => {
  usePanelStateMock.mockReset()
  usePanelLogsMock.mockReset()
  recordingLibraryPanelMock.mockReset()
  unifiedReviewPanelMock.mockReset()

  usePanelStateMock.mockImplementation(({ logsEnabled = false }: UsePanelStateArgs = {}) => ({
    state: createState(),
    isLoading: false,
    error: null,
    pendingAction: null,
    pendingNotificationSelection: null,
    pendingNotificationApplyNow: false,
    runAction: vi.fn(),
    saveNotificationSelection: vi.fn(),
    retry: vi.fn(),
    logResetKey: 0,
    realtimeConnected: true,
    streamEndpoint: logsEnabled ? PANEL_EVENTS_ENDPOINT : PANEL_STATE_EVENTS_ENDPOINT,
  }))

  usePanelLogsMock.mockReturnValue({
    logs: { text: "", droppedLineCount: 0 },
    error: null,
    logRef: { current: null },
    autoFollow: true,
    hasUnread: false,
    handleLogScroll: vi.fn(),
    jumpToLatest: vi.fn(),
    setAutoFollow: vi.fn(),
  })

  window.history.replaceState({}, "", "/")
  window.location.hash = ""
})

afterEach(() => {
  vi.resetAllMocks()
})

function setHash(hash: string) {
  window.location.hash = hash ? `#${hash}` : ""
}

describe("App", () => {
  it("shows review-needed counts separately from errors in the queue ledger", () => {
    setHash("processing")
    render(<App />)

    const ledger = screen.getByRole("heading", { name: "작업 장부" }).closest("article")
    expect(ledger).not.toBeNull()
    const scope = within(ledger as HTMLElement)

    expect(scope.getByText("확인 필요")).toBeTruthy()
    expect(scope.getByText("오류")).toBeTruthy()
    expect(scope.getByText("2")).toBeTruthy()
    expect(scope.getByText("4")).toBeTruthy()
  })

  it("keeps logs disabled on home and mounts only the home analytics panel", () => {
    setHash("home")
    render(<App />)

    expect(usePanelStateMock).toHaveBeenCalledWith({ logsEnabled: false })
    expect(usePanelLogsMock).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: false,
        eventsEndpoint: null,
      }),
    )
    expect(screen.getByText("TranscriptionAnalyticsPanel")).toBeTruthy()
    expect(screen.queryByText("ActivityPanel")).toBeNull()
    expect(screen.queryByText("TimetablePanel")).toBeNull()
  })

  it("mounts ActivityPanel and enables logs for processing hash", () => {
    setHash("processing")
    render(<App />)

    expect(usePanelStateMock).toHaveBeenCalledWith({ logsEnabled: true })
    expect(usePanelLogsMock).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: true,
        eventsEndpoint: PANEL_EVENTS_ENDPOINT,
      }),
    )
    expect(screen.getByText("ActivityPanel")).toBeTruthy()
    expect(screen.queryByText("TimetablePanel")).toBeNull()
    expect(screen.queryByText("TranscriptionAnalyticsPanel")).toBeNull()
  })

  it("mounts TimetablePanel and disables logs on timetable hash", () => {
    setHash("timetable")
    render(<App />)

    expect(usePanelStateMock).toHaveBeenCalledWith({ logsEnabled: false })
    expect(usePanelLogsMock).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: false,
        eventsEndpoint: null,
      }),
    )
    expect(screen.getByText("TimetablePanel")).toBeTruthy()
    expect(screen.queryByText("ActivityPanel")).toBeNull()
  })

  it("mounts UnifiedReviewPanel and disables logs on review hash", () => {
    setHash("review")
    render(<App />)

    expect(usePanelStateMock).toHaveBeenCalledWith({ logsEnabled: false })
    expect(usePanelLogsMock).toHaveBeenCalledWith(
      expect.objectContaining({
        enabled: false,
        eventsEndpoint: null,
      }),
    )
    expect(screen.getByText("UnifiedReviewPanel:none")).toBeTruthy()
    expect(unifiedReviewPanelMock).toHaveBeenCalledWith({
      selectedTarget: null,
    })
    expect(screen.queryByText("ActivityPanel")).toBeNull()
    expect(screen.queryByText("TimetablePanel")).toBeNull()
  })

  it("mounts RecordingLibraryPanel and disables logs on library hash", () => {
    setHash("library")
    render(<App />)

    expect(usePanelStateMock).toHaveBeenCalledWith({ logsEnabled: false })
    expect(screen.getByText("RecordingLibraryPanel:none")).toBeTruthy()
    expect(recordingLibraryPanelMock).toHaveBeenCalledWith({
      selectedStorageKey: null,
    })
  })

  it("passes the deep-linked storage_key to RecordingLibraryPanel", () => {
    setHash("library/rec_2026_07_23_ds_05")
    render(<App />)

    expect(screen.getByText("RecordingLibraryPanel:rec_2026_07_23_ds_05")).toBeTruthy()
    expect(recordingLibraryPanelMock).toHaveBeenCalledWith({
      selectedStorageKey: "rec_2026_07_23_ds_05",
    })
  })

  it("passes the exact archive review route target", () => {
    setHash("review/archive/CASE_001")
    render(<App />)

    expect(screen.getByText("UnifiedReviewPanel:archive:CASE_001")).toBeTruthy()
    expect(unifiedReviewPanelMock).toHaveBeenCalledWith({
      selectedTarget: {
        source: "archive",
        caseKey: "CASE_001",
      },
    })
  })

  it("passes the exact timetable review route target", () => {
    setHash("review/timetable/17")
    render(<App />)

    expect(screen.getByText("UnifiedReviewPanel:timetable:17")).toBeTruthy()
    expect(unifiedReviewPanelMock).toHaveBeenCalledWith({
      selectedTarget: {
        source: "timetable",
        proposalId: 17,
      },
    })
  })

  it("passes the exact recording review route target", () => {
    setHash("review/recording/rec_2026_07_23_ds_05")
    render(<App />)

    expect(screen.getByText("UnifiedReviewPanel:recording:rec_2026_07_23_ds_05")).toBeTruthy()
    expect(unifiedReviewPanelMock).toHaveBeenCalledWith({
      selectedTarget: {
        source: "recording",
        storageKey: "rec_2026_07_23_ds_05",
      },
    })
  })

  it("passes the exact title suggestion review route target", () => {
    setHash("review/title/11")
    render(<App />)

    expect(screen.getByText("UnifiedReviewPanel:title:11")).toBeTruthy()
    expect(unifiedReviewPanelMock).toHaveBeenCalledWith({
      selectedTarget: {
        source: "title",
        proposalId: 11,
      },
    })
  })

  it("updates unified review selection when review hash target changes", async () => {
    setHash("review/archive/CASE_001")
    render(<App />)

    expect(screen.getByText("UnifiedReviewPanel:archive:CASE_001")).toBeTruthy()
    expect(unifiedReviewPanelMock).toHaveBeenLastCalledWith({
      selectedTarget: {
        source: "archive",
        caseKey: "CASE_001",
      },
    })

    act(() => {
      window.location.hash = "#review/timetable/17"
      window.dispatchEvent(new HashChangeEvent("hashchange"))
    })

    await waitFor(() => {
      expect(screen.getByText("UnifiedReviewPanel:timetable:17")).toBeTruthy()
      expect(unifiedReviewPanelMock).toHaveBeenLastCalledWith({
        selectedTarget: {
          source: "timetable",
          proposalId: 17,
        },
      })
    })
  })

  it("fails closed for malformed library hash encoding", () => {
    setHash("library/%E0%A4%A")
    render(<App />)

    expect(screen.getByText("RecordingLibraryPanel:none")).toBeTruthy()
    expect(recordingLibraryPanelMock).toHaveBeenCalledWith({
      selectedStorageKey: null,
    })
  })

  it("fails closed for extra library hash segments", () => {
    setHash("library/rec_2026_07_23_ds_05/extra")
    render(<App />)

    expect(screen.getByText("RecordingLibraryPanel:none")).toBeTruthy()
    expect(recordingLibraryPanelMock).toHaveBeenCalledWith({
      selectedStorageKey: null,
    })
  })

  it("fails closed for percent-encoded slash in library hash", () => {
    setHash("library/rec_2026_07_23%2Fds_05")
    render(<App />)

    expect(screen.getByText("RecordingLibraryPanel:none")).toBeTruthy()
    expect(recordingLibraryPanelMock).toHaveBeenCalledWith({
      selectedStorageKey: null,
    })
  })

  it("fails closed for malformed review route encodings", () => {
    setHash("review/archive/%E0%A4%A")
    render(<App />)

    expect(screen.getByText("UnifiedReviewPanel:none")).toBeTruthy()
    expect(unifiedReviewPanelMock).toHaveBeenCalledWith({
      selectedTarget: null,
    })
  })

  it("fails closed for review routes with unknown source or extra segments", () => {
    setHash("review/unknown/abc")
    render(<App />)

    expect(screen.getByText("UnifiedReviewPanel:none")).toBeTruthy()
    expect(unifiedReviewPanelMock).toHaveBeenCalledWith({
      selectedTarget: null,
    })
  })

  it("fails closed for non-canonical timetable ids", () => {
    setHash("review/timetable/001")
    render(<App />)

    expect(screen.getByText("UnifiedReviewPanel:none")).toBeTruthy()
    expect(unifiedReviewPanelMock).toHaveBeenCalledWith({
      selectedTarget: null,
    })
  })

  it.each(["review/timetable/%2B17", "review/timetable/+17", "review/timetable/%2F17", "review/timetable/abc"])(
    "fails closed for malformed timetable hash %s",
    (hash) => {
      setHash(hash)
      render(<App />)

      expect(screen.getByText("UnifiedReviewPanel:none")).toBeTruthy()
      expect(unifiedReviewPanelMock).toHaveBeenCalledWith({
        selectedTarget: null,
      })
    },
  )

  it.each(["review/title/001", "review/title/%2B17", "review/title/+17", "review/title/%2F17", "review/title/abc"])(
    "fails closed for malformed title suggestion hash %s",
    (hash) => {
      setHash(hash)
      render(<App />)

      expect(screen.getByText("UnifiedReviewPanel:none")).toBeTruthy()
      expect(unifiedReviewPanelMock).toHaveBeenCalledWith({
        selectedTarget: null,
      })
    },
  )

  it("falls back to home for an invalid hash", () => {
    setHash("not-a-real-screen")
    render(<App />)

    expect(usePanelStateMock).toHaveBeenCalledWith({ logsEnabled: false })
    expect(screen.getByText("TranscriptionAnalyticsPanel")).toBeTruthy()
    expect(screen.queryByText("ActivityPanel")).toBeNull()
    expect(screen.queryByText("TimetablePanel")).toBeNull()
  })

  it("restores mounted screens across hash back and forward navigation", async () => {
    render(<App />)

    act(() => {
      window.history.pushState({}, "", "/#processing")
      window.dispatchEvent(new HashChangeEvent("hashchange"))
    })
    expect(screen.getByText("ActivityPanel")).toBeTruthy()

    act(() => {
      window.history.pushState({}, "", "/#timetable")
      window.dispatchEvent(new HashChangeEvent("hashchange"))
    })
    expect(screen.getByText("TimetablePanel")).toBeTruthy()

    act(() => {
      window.history.back()
    })
    await waitFor(() => {
      expect(window.location.hash).toBe("#processing")
      expect(screen.getByText("ActivityPanel")).toBeTruthy()
    })

    act(() => {
      window.history.forward()
    })
    await waitFor(() => {
      expect(window.location.hash).toBe("#timetable")
      expect(screen.getByText("TimetablePanel")).toBeTruthy()
    })
  })
})
