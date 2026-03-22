import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { usePanelState } from "../src/hooks/usePanelState"
import { subscribePanelEvents } from "../src/lib/panelEvents"
import { FALLBACK_PANEL_ENDPOINTS, fetchPanelState, postPanelAction } from "../src/lib/panelApi"
import type { PanelState } from "../src/types"

vi.mock("../src/lib/panelApi", () => ({
  FALLBACK_PANEL_ENDPOINTS: {
    state: "/api/state",
    logs: "/api/logs",
    start: "/api/start",
    pause: "/api/pause",
    resume: "/api/resume",
    stop: "/api/stop",
    refresh: "/api/refresh",
    exit: "/api/exit",
    clear_history: "/api/clear_history",
  },
  fetchPanelState: vi.fn(),
  postPanelAction: vi.fn(),
}))

vi.mock("../src/lib/panelEvents", () => ({
  subscribePanelEvents: vi.fn(() => () => {}),
}))

const fetchPanelStateMock = vi.mocked(fetchPanelState)
const postPanelActionMock = vi.mocked(postPanelAction)
const subscribePanelEventsMock = vi.mocked(subscribePanelEvents)

function createPanelState(): PanelState {
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
    counts: {
      PENDING: 1,
      PROCESSING: 1,
      DONE: 3,
      ERROR: 0,
      UNREGISTERED: 0,
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
        state: "/api/runtime/state",
        logs: "/api/runtime/logs",
        start: "/api/runtime/start",
        pause: "/api/runtime/pause",
        resume: "/api/runtime/resume",
        stop: "/api/runtime/stop",
        refresh: "/api/runtime/refresh",
        exit: "/api/runtime/exit",
        clear_history: "/api/runtime/clear_history",
      },
    },
    summary: {
      current_job: null,
      progress_label: "대기 중",
      eta_label: "-",
      notice: "",
      refresh_hint: "1초 간격 갱신",
      updated_at: "2026-03-23T09:00:00+09:00",
      poll_interval_sec: 60,
    },
    jobs_v2: [],
    processing_v2: [],
  }
}

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
      mutations: {
        retry: false,
      },
    },
  })

  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
}

describe("usePanelState", () => {
  beforeEach(() => {
    fetchPanelStateMock.mockReset()
    postPanelActionMock.mockReset()
    subscribePanelEventsMock.mockReset()
    subscribePanelEventsMock.mockReturnValue(() => {})
  })

  it("uses runtime-provided action endpoints and refetches state after an action", async () => {
    fetchPanelStateMock.mockResolvedValue(createPanelState())
    postPanelActionMock.mockResolvedValue({ ok: true, notice: "refreshed" })

    const { result } = renderHook(() => usePanelState(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.state?.actions.endpoints.state).toBe("/api/runtime/state")
    })

    expect(fetchPanelStateMock).toHaveBeenNthCalledWith(1, FALLBACK_PANEL_ENDPOINTS.state)

    await act(async () => {
      await result.current.runAction("refresh")
    })

    expect(postPanelActionMock).toHaveBeenCalledWith("/api/runtime/refresh")

    await waitFor(() => {
      expect(fetchPanelStateMock).toHaveBeenCalledTimes(2)
      expect(fetchPanelStateMock).toHaveBeenLastCalledWith("/api/runtime/state")
      expect(result.current.logResetKey).toBe(0)
      expect(result.current.pendingAction).toBeNull()
    })
  })

  it("bumps logResetKey only for clear_history", async () => {
    fetchPanelStateMock.mockResolvedValue(createPanelState())
    postPanelActionMock.mockResolvedValue({ ok: true, notice: "cleared" })

    const { result } = renderHook(() => usePanelState(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.state?.actions.endpoints.clear_history).toBe("/api/runtime/clear_history")
    })

    await act(async () => {
      await result.current.runAction("clear_history")
    })

    await waitFor(() => {
      expect(postPanelActionMock).toHaveBeenCalledWith("/api/runtime/clear_history")
      expect(result.current.logResetKey).toBe(1)
    })
  })

  it("updates panel state from SSE events without a full refetch", async () => {
    fetchPanelStateMock.mockResolvedValue(createPanelState())

    const { result } = renderHook(() => usePanelState(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.state?.runtime_state.status).toBe("running")
    })

    const listener = subscribePanelEventsMock.mock.calls[0]?.[0]
    expect(listener).toBeDefined()

    await act(async () => {
      listener?.onConnectionChange?.(true)
      listener?.onEvent({
        type: "state",
        state: {
          ...createPanelState(),
          runtime_state: {
            ...createPanelState().runtime_state,
            status: "paused",
            label: "일시정지",
          },
          summary: {
            ...createPanelState().summary,
            notice: "stream update",
          },
        },
      })
    })

    await waitFor(() => {
      expect(result.current.state?.runtime_state.status).toBe("paused")
      expect(result.current.state?.summary.notice).toBe("stream update")
    })

    expect(fetchPanelStateMock).toHaveBeenCalledTimes(1)
  })
})
