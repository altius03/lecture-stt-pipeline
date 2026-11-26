import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { fetchUnifiedReviewFeed } from "../src/lib/panelApi"
import { buildUnifiedReviewFeedPayload } from "./unifiedReviewFixtures"

let fetchSpy: ReturnType<typeof vi.spyOn>
const originalFetch = globalThis.fetch

function buildJsonResponse(status: number, payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
    },
  })
}

describe("panelApi unified review feed", () => {
  beforeEach(() => {
    fetchSpy = vi.spyOn(
      globalThis as unknown as { fetch: typeof fetch },
      "fetch",
    )
    fetchSpy.mockReset()
  })

  afterEach(() => {
    fetchSpy.mockRestore()
    globalThis.fetch = originalFetch
  })

  it("fetches the canonical feed with no-store and caller abort signal", async () => {
    const controller = new AbortController()
    const payload = buildUnifiedReviewFeedPayload()
    payload.filters.limit = 25
    payload.filters.offset = 50
    payload.total = 120
    payload.sources[0].total_count = 117
    payload.sources[0].truncated = true
    fetchSpy.mockResolvedValueOnce(buildJsonResponse(200, payload))

    await expect(
      fetchUnifiedReviewFeed({ limit: 25, offset: 50 }, controller.signal),
    ).resolves.toEqual(payload)

    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/storage-v2/review-feed?limit=25&offset=50",
      {
        cache: "no-store",
        signal: controller.signal,
      },
    )
  })

  it("rejects a response whose pagination does not match the request", async () => {
    const payload = buildUnifiedReviewFeedPayload()
    payload.filters.offset = 1
    payload.total = 5
    payload.sources[0].total_count = 2
    payload.sources[0].truncated = true
    fetchSpy.mockResolvedValueOnce(buildJsonResponse(200, payload))

    await expect(
      fetchUnifiedReviewFeed({ limit: 100, offset: 0 }),
    ).rejects.toThrow(/pagination does not match/)
  })

  it("propagates backend HTTP errors", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(503, {
        error: "review feed unavailable",
      }),
    )

    await expect(
      fetchUnifiedReviewFeed({ limit: 100, offset: 0 }),
    ).rejects.toThrow("review feed unavailable")
  })

  it("rejects invalid request bounds before fetch", async () => {
    await expect(
      fetchUnifiedReviewFeed({ limit: 101, offset: 0 }),
    ).rejects.toThrow(/less than or equal to 100/)
    await expect(
      fetchUnifiedReviewFeed({ limit: 10, offset: -1 }),
    ).rejects.toThrow(/non-negative integer/)

    expect(fetchSpy).not.toHaveBeenCalled()
  })
})
