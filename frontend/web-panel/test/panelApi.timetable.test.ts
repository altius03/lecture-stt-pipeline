import { beforeEach, describe, expect, it, vi } from "vitest"

import {
  applyClassificationConfirmation,
  planClassificationConfirmation,
  postClassificationStatus,
} from "../src/lib/panelApi"

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

describe("panelApi timetable review endpoints", () => {
  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis as unknown as { fetch: typeof fetch }, "fetch")
    fetchSpy.mockReset()
  })

  afterEach(() => {
    fetchSpy.mockRestore()
    globalThis.fetch = originalFetch
  })

  it("posts rejected status with allow_write and exact body", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        schema_version: "storage-v2/classification-status-result@1",
        ok: true,
        action: "rejected",
        proposal_id: 7,
        status: "rejected",
        review_status: "dismissed",
        canonical_metadata_changed: false,
      }),
    )

    await postClassificationStatus(7, { status: "rejected", allow_write: true })

    expect(fetchSpy).toHaveBeenCalledTimes(1)
    const [url, init] = fetchSpy.mock.calls[0] ?? []
    expect(url).toBe("/api/storage-v2/timetable/classifications/7/status")
    expect(init?.method).toBe("POST")
    expect(init?.headers).toMatchObject({
      "Content-Type": "application/json",
    })
    expect(JSON.parse(String(init?.body))).toEqual({
      status: "rejected",
      allow_write: true,
    })
  })

  it("propagates 403 backend error during status update", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(403, {
        error: "상태 변경 권한이 없습니다.",
      }),
    )

    await expect(
      postClassificationStatus(7, { status: "rejected", allow_write: true }),
    ).rejects.toThrow("상태 변경 권한이 없습니다.")
  })

  it("posts confirm-plan request body and exact digest on apply", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        schema_version: "storage-v2/classification-confirmation-plan@1",
        proposal_id: 7,
        storage_key: "20260723_abcd",
        proposal_updated_at: "2026-07-23T09:00:00+09:00",
        review_status: "open",
        semester: "2026-2",
        course_name: "자료구조",
        session_date: "2026-07-23",
        period_label: "5교시",
        proposed_title: "2026-07-23 자료구조 5교시",
        expected_count: 1,
        materialization: "confirmation_audit_only",
        mode: "read_only",
        plan_sha256: "a".repeat(64),
        canonical_metadata_changed: false,
      }),
    )

    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        schema_version: "storage-v2/classification-confirmation-result@1",
        ok: true,
        action: "confirmed",
        proposal_id: 7,
        status: "confirmed",
        plan_sha256: "a".repeat(64),
        canonical_metadata_changed: false,
      }),
    )

    const plan = await planClassificationConfirmation(7)
    const result = await applyClassificationConfirmation(7, {
      expected_count: 1,
      expected_plan_sha256: plan.plan_sha256,
      allow_write: true,
    })

    expect(plan.expected_count).toBe(1)
    expect(plan.plan_sha256).toBe("a".repeat(64))
    expect(fetchSpy).toHaveBeenNthCalledWith(
      1,
      "/api/storage-v2/timetable/classifications/7/confirmation/plan",
      expect.objectContaining({
        method: "POST",
        body: "{}",
        headers: expect.objectContaining({
          "Content-Type": "application/json",
        }),
      }),
    )
    expect(fetchSpy).toHaveBeenNthCalledWith(
      2,
      "/api/storage-v2/timetable/classifications/7/confirmation/apply",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          expected_count: 1,
          expected_plan_sha256: "a".repeat(64),
          allow_write: true,
        }),
        headers: expect.objectContaining({
          "Content-Type": "application/json",
        }),
      }),
    )
    expect(result.action).toBe("confirmed")
  })

  it("rejects a confirmation plan correlated to a different proposal", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        schema_version: "storage-v2/classification-confirmation-plan@1",
        proposal_id: 8,
        storage_key: "20260723_efgh",
        proposal_updated_at: "2026-07-23T09:10:00+09:00",
        review_status: "open",
        semester: "2026-2",
        course_name: "운영체제",
        session_date: "2026-07-23",
        period_label: "2교시",
        proposed_title: "2026-07-23 운영체제 2교시",
        expected_count: 1,
        materialization: "confirmation_audit_only",
        mode: "read_only",
        plan_sha256: "a".repeat(64),
        canonical_metadata_changed: false,
      }),
    )

    await expect(planClassificationConfirmation(7)).rejects.toThrow(
      "proposal_id does not match",
    )
  })

  it("rejects an apply response with a different plan digest", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        schema_version: "storage-v2/classification-confirmation-result@1",
        ok: true,
        action: "confirmed",
        proposal_id: 7,
        status: "confirmed",
        plan_sha256: "b".repeat(64),
        canonical_metadata_changed: false,
      }),
    )

    await expect(
      applyClassificationConfirmation(7, {
        expected_count: 1,
        expected_plan_sha256: "a".repeat(64),
        allow_write: true,
      }),
    ).rejects.toThrow("digest does not match")
  })

  it("propagates 409 backend conflict on confirmation apply", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(409, {
        error: "동시 수정 충돌: 제안이 이미 변경되었습니다.",
      }),
    )

    await expect(
      applyClassificationConfirmation(7, {
        expected_count: 1,
        expected_plan_sha256: "a".repeat(64),
        allow_write: true,
      }),
    ).rejects.toThrow("동시 수정 충돌: 제안이 이미 변경되었습니다.")
  })
})
