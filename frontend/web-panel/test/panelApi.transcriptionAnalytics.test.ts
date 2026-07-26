import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { fetchTranscriptionAnalytics } from "../src/lib/panelApi"
import { buildTranscriptionAnalyticsPayload } from "./transcriptionAnalyticsFixtures"

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

describe("panelApi transcription analytics endpoint", () => {
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

  it("fetches exactly one period with no-store and the caller signal", async () => {
    const controller = new AbortController()
    const payload = buildTranscriptionAnalyticsPayload("month")
    fetchSpy.mockResolvedValueOnce(buildJsonResponse(200, payload))

    await expect(
      fetchTranscriptionAnalytics("month", controller.signal),
    ).resolves.toEqual(payload)

    expect(fetchSpy).toHaveBeenCalledTimes(1)
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/storage-v2/analytics/transcriptions?period=month",
      {
        cache: "no-store",
        signal: controller.signal,
      },
    )
  })

  it("propagates the backend error without decoding a failed response", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(503, {
        error: "격리 분석 DB를 열 수 없습니다.",
      }),
    )

    await expect(fetchTranscriptionAnalytics("week")).rejects.toThrow(
      "격리 분석 DB를 열 수 없습니다.",
    )
  })

  it("rejects a valid response correlated to a different requested period", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, buildTranscriptionAnalyticsPayload("week")),
    )

    await expect(fetchTranscriptionAnalytics("day")).rejects.toThrow(
      "period does not match",
    )
  })
})
