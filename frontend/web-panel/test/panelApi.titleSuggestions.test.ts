import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  applyTitleSuggestionConfirmation,
  fetchTitleSuggestionDetail,
  fetchTitleSuggestions,
  planTitleSuggestionConfirmation,
  postTitleSuggestionStatus,
} from "../src/lib/panelApi"
import {
  buildTitleSuggestionConfirmationPlan,
  buildTitleSuggestionConfirmationResult,
  buildTitleSuggestionDetailPayload,
  buildTitleSuggestionListPayload,
  buildTitleSuggestionStatusResult,
} from "./titleSuggestionFixtures"

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

describe("panelApi title suggestion review", () => {
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

  it("fetches a correlated list and detail with no-store and abort signals", async () => {
    const listController = new AbortController()
    const detailController = new AbortController()
    const list = buildTitleSuggestionListPayload()
    const detail = buildTitleSuggestionDetailPayload()
    fetchSpy
      .mockResolvedValueOnce(buildJsonResponse(200, list))
      .mockResolvedValueOnce(buildJsonResponse(200, detail))

    await expect(
      fetchTitleSuggestions(
        { status: "suggested", limit: 8, offset: 0 },
        listController.signal,
      ),
    ).resolves.toEqual(list)
    await expect(
      fetchTitleSuggestionDetail(11, detailController.signal),
    ).resolves.toEqual(detail)

    expect(fetchSpy).toHaveBeenNthCalledWith(
      1,
      "/api/storage-v2/title-suggestions?status=suggested&limit=8&offset=0",
      {
        cache: "no-store",
        signal: listController.signal,
      },
    )
    expect(fetchSpy).toHaveBeenNthCalledWith(
      2,
      "/api/storage-v2/title-suggestions/11",
      {
        cache: "no-store",
        signal: detailController.signal,
      },
    )
  })

  it("rejects an oversized list page and invalid request bounds", async () => {
    const oversized = buildTitleSuggestionListPayload()
    oversized.proposals.push({
      ...structuredClone(oversized.proposals[0]),
      id: 12,
      storage_key: "rec_second",
    })
    oversized.counts.suggested = 2
    oversized.total = 2
    fetchSpy.mockResolvedValueOnce(buildJsonResponse(200, oversized))

    await expect(
      fetchTitleSuggestions({ status: "suggested", limit: 1 }),
    ).rejects.toThrow(/exceeds the requested limit/)

    await expect(
      fetchTitleSuggestions({ offset: -1 }),
    ).rejects.toThrow(/non-negative integer/)
    await expect(
      fetchTitleSuggestions({ limit: 201 }),
    ).rejects.toThrow(/less than or equal to 200/)
    expect(fetchSpy).toHaveBeenCalledTimes(1)
  })

  it("rejects list status and detail identity mismatches", async () => {
    const wrongStatus = buildTitleSuggestionListPayload()
    wrongStatus.proposals[0].status = "confirmed"
    wrongStatus.proposals[0].review_status = "resolved"
    wrongStatus.proposals[0].confirmed_at = "2026-07-24T14:00:00+09:00"
    wrongStatus.counts = {
      suggested: 0,
      confirmed: 1,
      rejected: 0,
    }
    fetchSpy
      .mockResolvedValueOnce(buildJsonResponse(200, wrongStatus))
      .mockResolvedValueOnce(
        buildJsonResponse(200, buildTitleSuggestionDetailPayload(12)),
      )

    await expect(
      fetchTitleSuggestions({ status: "suggested", limit: 8 }),
    ).rejects.toThrow(/requested status/)
    await expect(fetchTitleSuggestionDetail(11)).rejects.toThrow(
      /proposal_id does not match/,
    )
  })

  it("posts the guarded reject body and confirmation plan/apply bodies exactly", async () => {
    fetchSpy
      .mockResolvedValueOnce(
        buildJsonResponse(200, buildTitleSuggestionStatusResult()),
      )
      .mockResolvedValueOnce(
        buildJsonResponse(200, buildTitleSuggestionConfirmationPlan()),
      )
      .mockResolvedValueOnce(
        buildJsonResponse(200, buildTitleSuggestionConfirmationResult()),
      )

    await postTitleSuggestionStatus(11, {
      status: "rejected",
      allow_write: true,
    })
    const plan = await planTitleSuggestionConfirmation(11)
    await applyTitleSuggestionConfirmation(11, {
      expected_count: 1,
      expected_plan_sha256: plan.plan_sha256,
      allow_write: true,
    })

    expect(fetchSpy).toHaveBeenNthCalledWith(
      1,
      "/api/storage-v2/title-suggestions/11/status",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          status: "rejected",
          allow_write: true,
        }),
      }),
    )
    expect(fetchSpy).toHaveBeenNthCalledWith(
      2,
      "/api/storage-v2/title-suggestions/11/confirmation/plan",
      expect.objectContaining({
        method: "POST",
        body: "{}",
      }),
    )
    expect(fetchSpy).toHaveBeenNthCalledWith(
      3,
      "/api/storage-v2/title-suggestions/11/confirmation/apply",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          expected_count: 1,
          expected_plan_sha256: "a".repeat(64),
          allow_write: true,
        }),
      }),
    )
  })

  it("enforces allow-write, exact count, and digest guards before fetch", async () => {
    await expect(
      postTitleSuggestionStatus(
        11,
        {
          status: "rejected",
          allow_write: false,
        } as unknown as {
          status: "rejected"
          allow_write: true
        },
      ),
    ).rejects.toThrow(/allow_write=true/)
    await expect(
      applyTitleSuggestionConfirmation(
        11,
        {
          expected_count: 2,
          expected_plan_sha256: "a".repeat(64),
          allow_write: true,
        } as unknown as {
          expected_count: 1
          expected_plan_sha256: string
          allow_write: true
        },
      ),
    ).rejects.toThrow(/expected_count/)
    await expect(
      applyTitleSuggestionConfirmation(11, {
        expected_count: 1,
        expected_plan_sha256: "A".repeat(64),
        allow_write: true,
      }),
    ).rejects.toThrow(/lowercase sha256/)
    await expect(
      applyTitleSuggestionConfirmation(
        11,
        {
          expected_count: 1,
          expected_plan_sha256: "a".repeat(64),
          allow_write: false,
        } as unknown as {
          expected_count: 1
          expected_plan_sha256: string
          allow_write: true
        },
      ),
    ).rejects.toThrow(/allow_write=true/)

    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it("rejects an apply response with a different plan digest", async () => {
    const result = buildTitleSuggestionConfirmationResult()
    result.plan_sha256 = "b".repeat(64)
    fetchSpy.mockResolvedValueOnce(buildJsonResponse(200, result))

    await expect(
      applyTitleSuggestionConfirmation(11, {
        expected_count: 1,
        expected_plan_sha256: "a".repeat(64),
        allow_write: true,
      }),
    ).rejects.toThrow(/digest does not match/)
  })
})
