import { act, renderHook, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { usePanelLogs } from "../src/hooks/usePanelLogs"
import { subscribePanelEvents } from "../src/lib/panelEvents"
import { fetchLogs, PANEL_EVENTS_ENDPOINT } from "../src/lib/panelApi"

vi.mock("../src/lib/panelApi", () => ({
  fetchLogs: vi.fn(),
  PANEL_EVENTS_ENDPOINT: "/api/events",
}))

vi.mock("../src/lib/panelEvents", () => ({
  subscribePanelEvents: vi.fn(() => () => {}),
}))

const fetchLogsMock = vi.mocked(fetchLogs)
const subscribePanelEventsMock = vi.mocked(subscribePanelEvents)

function attachScrollablePre(
  logRef: { current: HTMLPreElement | null },
  { scrollHeight = 120, clientHeight = 20 }: { scrollHeight?: number; clientHeight?: number } = {},
) {
  const pre = document.createElement("pre")
  Object.defineProperty(pre, "scrollHeight", {
    configurable: true,
    get: () => scrollHeight,
  })
  Object.defineProperty(pre, "clientHeight", {
    configurable: true,
    get: () => clientHeight,
  })
  pre.scrollTop = 0
  logRef.current = pre
  return pre
}

describe("usePanelLogs", () => {
  beforeEach(() => {
    fetchLogsMock.mockReset()
    subscribePanelEventsMock.mockReset()
    subscribePanelEventsMock.mockReturnValue(() => {})
  })

  it("does not open SSE nor poll logs when disabled", () => {
    const { result } = renderHook(() =>
      usePanelLogs({
        endpoint: "/api/logs",
        pollIntervalSec: 1,
        enabled: false,
        resetKey: 0,
      }),
    )

    expect(result.current.logs.text).toBe("")
    expect(fetchLogsMock).not.toHaveBeenCalled()
    expect(subscribePanelEventsMock).not.toHaveBeenCalled()
  })

  it("uses fetch fallback when logs endpoint is omitted", async () => {
    fetchLogsMock.mockResolvedValue({ offset: 9, text: "alpha\n" })

    const { result } = renderHook(() =>
      usePanelLogs({
        endpoint: "/api/logs",
        eventsEndpoint: null,
        pollIntervalSec: 1,
        enabled: true,
        resetKey: 0,
      }),
    )

    attachScrollablePre(result.current.logRef as { current: HTMLPreElement | null })

    await waitFor(() => {
      expect(fetchLogsMock).toHaveBeenCalledWith("/api/logs", null)
      expect(subscribePanelEventsMock).not.toHaveBeenCalled()
    })
  })

  it("passes the combined events endpoint into SSE subscription when active", () => {
    fetchLogsMock.mockResolvedValue({ offset: 0, text: "" })

    renderHook(() =>
      usePanelLogs({
        endpoint: "/api/logs",
        eventsEndpoint: PANEL_EVENTS_ENDPOINT,
        pollIntervalSec: 1,
        enabled: true,
        resetKey: 0,
      }),
    )

    expect(subscribePanelEventsMock).toHaveBeenCalledWith(
      expect.objectContaining({
        onEvent: expect.any(Function),
      }),
      PANEL_EVENTS_ENDPOINT,
    )
  })

  it("loads the first chunk and appends later polling results", async () => {
    fetchLogsMock
      .mockResolvedValueOnce({ offset: 10, text: "first line\n" })
      .mockResolvedValueOnce({ offset: 18, text: "second line\n" })

    const { result } = renderHook(() =>
      usePanelLogs({
        endpoint: "/api/logs",
        pollIntervalSec: 1,
        enabled: true,
        resetKey: 0,
      }),
    )

    attachScrollablePre(result.current.logRef as { current: HTMLPreElement | null })

    await waitFor(() => {
      expect(result.current.logs.text).toBe("first line\n")
      expect(result.current.logs.offset).toBe(10)
    })

    await waitFor(() => {
      expect(result.current.logs.text).toBe("first line\nsecond line\n")
      expect(result.current.logs.offset).toBe(18)
    }, { timeout: 1500 })

    expect(fetchLogsMock).toHaveBeenNthCalledWith(1, "/api/logs", null)
    expect(fetchLogsMock).toHaveBeenNthCalledWith(2, "/api/logs", 10)
  })

  it("trims old log lines when the buffer grows beyond the cap", async () => {
    const overflowingText = Array.from({ length: 505 }, (_, index) => `line-${index + 1}`).join("\n")
    fetchLogsMock.mockResolvedValueOnce({ offset: 505, text: overflowingText })

    const { result } = renderHook(() =>
      usePanelLogs({
        endpoint: "/api/logs",
        pollIntervalSec: 1,
        enabled: true,
        resetKey: 0,
      }),
    )

    attachScrollablePre(result.current.logRef as { current: HTMLPreElement | null })

    await waitFor(() => {
      const lines = result.current.logs.text.split("\n")
      expect(result.current.logs.offset).toBe(505)
      expect(result.current.logs.droppedLineCount).toBe(5)
      expect(lines[0]).toBe("line-6")
      expect(lines.includes("line-1")).toBe(false)
    })
  })

  it("marks unread logs when auto-follow is off and can jump back to the latest entry", async () => {
    fetchLogsMock
      .mockResolvedValueOnce({ offset: 10, text: "first line\n" })
      .mockResolvedValueOnce({ offset: 20, text: "second line\n" })

    const { result } = renderHook(() =>
      usePanelLogs({
        endpoint: "/api/logs",
        pollIntervalSec: 1,
        enabled: true,
        resetKey: 0,
      }),
    )

    const pre = attachScrollablePre(result.current.logRef as { current: HTMLPreElement | null })

    await waitFor(() => {
      expect(result.current.logs.text).toBe("first line\n")
      expect(pre.scrollTop).toBe(120)
    })

    pre.scrollTop = 0
    act(() => {
      result.current.handleLogScroll()
    })

    expect(result.current.autoFollow).toBe(false)

    await waitFor(() => {
      expect(result.current.hasUnread).toBe(true)
      expect(result.current.logs.text).toBe("first line\nsecond line\n")
    }, { timeout: 1500 })

    act(() => {
      result.current.jumpToLatest()
    })

    expect(result.current.autoFollow).toBe(true)
    expect(result.current.hasUnread).toBe(false)
    expect(pre.scrollTop).toBe(120)
  })

  it("applies SSE log reset and append events", async () => {
    fetchLogsMock.mockResolvedValue({ offset: 0, text: "" })

    const { result } = renderHook(() =>
      usePanelLogs({
        endpoint: "/api/logs",
        pollIntervalSec: 1,
        enabled: true,
        resetKey: 0,
      }),
    )

    const pre = attachScrollablePre(result.current.logRef as { current: HTMLPreElement | null })
    const listener = subscribePanelEventsMock.mock.calls[0]?.[0]
    expect(listener).toBeDefined()

    act(() => {
      listener?.onConnectionChange?.(true)
      listener?.onEvent({
        type: "log_reset",
        payload: {
          offset: 5,
          text: "alpha\n",
          reset: true,
        },
      })
      listener?.onEvent({
        type: "log_chunk",
        payload: {
          offset: 11,
          text: "beta\n",
        },
      })
    })

    await waitFor(() => {
      expect(result.current.logs.text).toBe("alpha\nbeta\n")
      expect(result.current.logs.offset).toBe(11)
      expect(pre.scrollTop).toBe(120)
    })
  })

  it("resets droppedLineCount after a log reset event", async () => {
    fetchLogsMock.mockResolvedValue({ offset: 0, text: "" })

    const { result } = renderHook(() =>
      usePanelLogs({
        endpoint: "/api/logs",
        pollIntervalSec: 1,
        enabled: true,
        resetKey: 0,
      }),
    )

    const listener = subscribePanelEventsMock.mock.calls[0]?.[0]
    expect(listener).toBeDefined()

    const overflowingText = Array.from({ length: 505 }, (_, index) => `line-${index + 1}`).join("\n")

    act(() => {
      listener?.onConnectionChange?.(true)
      listener?.onEvent({
        type: "log_chunk",
        payload: {
          offset: 505,
          text: overflowingText,
        },
      })
    })

    await waitFor(() => {
      expect(result.current.logs.droppedLineCount).toBe(5)
    })

    act(() => {
      listener?.onEvent({
        type: "log_reset",
        payload: {
          offset: 6,
          text: "fresh\n",
          reset: true,
        },
      })
    })

    await waitFor(() => {
      expect(result.current.logs.text).toBe("fresh\n")
      expect(result.current.logs.droppedLineCount).toBe(0)
    })
  })
})
