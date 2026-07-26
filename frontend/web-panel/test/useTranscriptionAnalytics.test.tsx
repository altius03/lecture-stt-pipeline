import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { useTranscriptionAnalytics } from "../src/hooks/useTranscriptionAnalytics"
import { fetchTranscriptionAnalytics } from "../src/lib/panelApi"
import type {
  TranscriptionAnalyticsPayload,
  TranscriptionAnalyticsPeriod,
} from "../src/types"
import { buildTranscriptionAnalyticsPayload } from "./transcriptionAnalyticsFixtures"

vi.mock("../src/lib/panelApi", () => ({
  fetchTranscriptionAnalytics: vi.fn(),
}))

const fetchAnalyticsMock = vi.mocked(fetchTranscriptionAnalytics)

interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (reason?: unknown) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: Infinity,
      },
    },
  })
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        {children}
      </QueryClientProvider>
    )
  }
}

describe("useTranscriptionAnalytics", () => {
  beforeEach(() => {
    fetchAnalyticsMock.mockReset()
  })

  it("defaults to week and issues only one on-demand query", async () => {
    fetchAnalyticsMock.mockResolvedValue(
      buildTranscriptionAnalyticsPayload("week"),
    )

    const { result } = renderHook(
      () => useTranscriptionAnalytics({ enabled: true }),
      { wrapper: createWrapper() },
    )

    expect(result.current.selectedPeriod).toBe("week")
    await waitFor(() => {
      expect(result.current.analytics?.period).toBe("week")
    })

    expect(fetchAnalyticsMock).toHaveBeenCalledTimes(1)
    expect(fetchAnalyticsMock.mock.calls[0]?.[0]).toBe("week")
    expect(fetchAnalyticsMock.mock.calls[0]?.[1]).toBeInstanceOf(AbortSignal)
  })

  it("does not query off-home and starts one query when enabled", async () => {
    fetchAnalyticsMock.mockResolvedValue(
      buildTranscriptionAnalyticsPayload("week"),
    )

    const { result, rerender } = renderHook(
      ({ enabled }) => useTranscriptionAnalytics({ enabled }),
      {
        initialProps: { enabled: false },
        wrapper: createWrapper(),
      },
    )

    expect(result.current.selectedPeriod).toBe("week")
    expect(result.current.analytics).toBeNull()
    expect(fetchAnalyticsMock).not.toHaveBeenCalled()

    rerender({ enabled: true })
    await waitFor(() => {
      expect(result.current.analytics?.period).toBe("week")
    })
    expect(fetchAnalyticsMock).toHaveBeenCalledTimes(1)

    rerender({ enabled: false })
    expect(fetchAnalyticsMock).toHaveBeenCalledTimes(1)
  })

  it("aborts the previous period and isolates a late stale response", async () => {
    const requests = new Map<
      TranscriptionAnalyticsPeriod,
      Deferred<TranscriptionAnalyticsPayload>
    >()
    const signals = new Map<
      TranscriptionAnalyticsPeriod,
      AbortSignal | undefined
    >()

    fetchAnalyticsMock.mockImplementation((period, signal) => {
      const request = deferred<TranscriptionAnalyticsPayload>()
      requests.set(period, request)
      signals.set(period, signal)
      return request.promise
    })

    const { result } = renderHook(
      () => useTranscriptionAnalytics({ enabled: true }),
      { wrapper: createWrapper() },
    )

    await waitFor(() => {
      expect(requests.has("week")).toBe(true)
    })

    act(() => {
      result.current.selectPeriod("day")
    })
    await waitFor(() => {
      expect(requests.has("day")).toBe(true)
    })
    expect(signals.get("week")?.aborted).toBe(true)

    await act(async () => {
      requests.get("day")?.resolve(buildTranscriptionAnalyticsPayload("day"))
      await requests.get("day")?.promise
    })
    await waitFor(() => {
      expect(result.current.selectedPeriod).toBe("day")
      expect(result.current.analytics?.period).toBe("day")
    })

    await act(async () => {
      requests
        .get("week")
        ?.resolve(buildTranscriptionAnalyticsPayload("week"))
      await requests.get("week")?.promise
    })

    expect(result.current.selectedPeriod).toBe("day")
    expect(result.current.analytics?.period).toBe("day")
    expect(fetchAnalyticsMock).toHaveBeenCalledTimes(2)
  })

  it("surfaces an error and succeeds on explicit retry", async () => {
    fetchAnalyticsMock
      .mockRejectedValueOnce(new Error("analytics unavailable"))
      .mockResolvedValueOnce(buildTranscriptionAnalyticsPayload("week"))

    const { result } = renderHook(
      () => useTranscriptionAnalytics({ enabled: true }),
      { wrapper: createWrapper() },
    )

    await waitFor(() => {
      expect(result.current.error).toBe("analytics unavailable")
    })

    let retrySucceeded = false
    await act(async () => {
      retrySucceeded = await result.current.retry()
    })

    expect(retrySucceeded).toBe(true)
    await waitFor(() => {
      expect(result.current.analytics?.period).toBe("week")
      expect(result.current.error).toBeNull()
    })
    expect(fetchAnalyticsMock).toHaveBeenCalledTimes(2)
  })
})
