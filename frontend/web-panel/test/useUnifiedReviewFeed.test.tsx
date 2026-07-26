import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { useUnifiedReviewFeed } from "../src/hooks/useUnifiedReviewFeed"
import { fetchUnifiedReviewFeed } from "../src/lib/panelApi"
import type { UnifiedReviewFeedPayload } from "../src/types"
import { buildUnifiedReviewFeedPayload } from "./unifiedReviewFixtures"

vi.mock("../src/lib/panelApi", () => ({
  fetchUnifiedReviewFeed: vi.fn(),
}))

const fetchUnifiedReviewFeedMock = vi.mocked(fetchUnifiedReviewFeed)

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

function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: Infinity,
      },
    },
  })
}

function createWrapper(queryClient = createQueryClient()) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
}

describe("useUnifiedReviewFeed", () => {
  beforeEach(() => {
    fetchUnifiedReviewFeedMock.mockReset()
  })

  it("keeps the exact server order and returns canonical pagination metadata", async () => {
    const payload = buildUnifiedReviewFeedPayload()
    payload.filters.limit = 2
    payload.filters.offset = 2
    payload.total = 5
    payload.items = [
      payload.items[3],
      payload.items[0],
    ]
    payload.sources[0].visible_count = 1
    payload.sources[1].visible_count = 0
    payload.sources[2].visible_count = 0
    payload.sources[3].visible_count = 1
    fetchUnifiedReviewFeedMock.mockResolvedValue(payload)

    const { result } = renderHook(
      () => useUnifiedReviewFeed({ limit: 2, offset: 2 }),
      { wrapper: createWrapper() },
    )

    await waitFor(() => {
      expect(result.current.items).toHaveLength(2)
    })

    expect(fetchUnifiedReviewFeedMock).toHaveBeenCalledWith({ limit: 2, offset: 2 }, expect.any(AbortSignal))
    expect(result.current.items.map((item) => item.id)).toEqual([
      "recording:rec_001",
      "archive:CASE_001",
    ])
    expect(result.current.items[0]).toMatchObject({
      stateLabel: "열린 검토 1건",
      reasonLabel: "recording summary 기준 남은 review",
      metaLabel: "rec_001 · 원본 1건",
    })
    expect(result.current.ledgers.map((ledger) => ledger.status)).toEqual([
      "ready",
      "ready",
      "ready",
      "ready",
    ])
    expect(result.current.pagination).toEqual({
      limit: 2,
      offset: 2,
      total: 5,
      hasPrevious: true,
      hasNext: true,
    })
  })

  it("fails closed on remount until a fresh canonical response settles", async () => {
    const queryClient = createQueryClient()
    const wrapper = createWrapper(queryClient)
    fetchUnifiedReviewFeedMock.mockResolvedValueOnce(buildUnifiedReviewFeedPayload())

    const firstMount = renderHook(() => useUnifiedReviewFeed(), { wrapper })
    await waitFor(() => {
      expect(firstMount.result.current.items).toHaveLength(4)
    })
    firstMount.unmount()

    const reload = deferred<UnifiedReviewFeedPayload>()
    fetchUnifiedReviewFeedMock.mockImplementationOnce(() => reload.promise)

    const secondMount = renderHook(() => useUnifiedReviewFeed(), { wrapper })

    expect(secondMount.result.current.items).toEqual([])
    expect(secondMount.result.current.ledgers.map((ledger) => ledger.status)).toEqual([
      "loading",
      "loading",
      "loading",
      "loading",
    ])

    reload.resolve(buildUnifiedReviewFeedPayload())

    await waitFor(() => {
      expect(secondMount.result.current.items).toHaveLength(4)
    })
  })

  it("surfaces endpoint failure as feed-wide source errors only after a fresh mount response fails", async () => {
    fetchUnifiedReviewFeedMock.mockRejectedValue(new Error("canonical feed unavailable"))

    const { result } = renderHook(() => useUnifiedReviewFeed(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.ledgers.map((ledger) => ledger.status)).toEqual([
        "error",
        "error",
        "error",
        "error",
      ])
    })

    expect(result.current.items).toEqual([])
    expect(result.current.hasHealthySource).toBe(false)
    expect(result.current.hasVisibleItems).toBe(false)
    expect(result.current.isLoading).toBe(false)
    expect(result.current.ledgers.every((ledger) => ledger.error === "canonical feed unavailable")).toBe(true)
  })

  it("keeps unavailable source ledgers from the server while exposing an empty healthy queue", async () => {
    const payload = buildUnifiedReviewFeedPayload()
    payload.items = []
    payload.total = 0
    payload.sources = payload.sources.map((source) => ({
      ...source,
      status: "unavailable",
      available: false,
      visible_count: 0,
      total_count: null,
      note: `${source.source} disabled`,
    }))
    payload.available = false
    fetchUnifiedReviewFeedMock.mockResolvedValue(payload)

    const { result } = renderHook(() => useUnifiedReviewFeed(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false)
    })

    expect(result.current.items).toEqual([])
    expect(result.current.ledgers.map((ledger) => ledger.status)).toEqual([
      "unavailable",
      "unavailable",
      "unavailable",
      "unavailable",
    ])
    expect(result.current.hasHealthySource).toBe(false)
    expect(result.current.hasVisibleItems).toBe(false)
    expect(result.current.pagination).toEqual({
      limit: 100,
      offset: 0,
      total: 0,
      hasPrevious: false,
      hasNext: false,
    })
  })
})
