import { beforeEach, describe, expect, it, vi } from "vitest"

import {
  applyArchiveReviewPromotion,
  fetchArchiveReviewCaseDetail,
  fetchArchiveReviewCases,
  planArchiveReviewPromotion,
  postArchiveReviewStatus,
} from "../src/lib/panelApi"

import type { ArchiveReviewPromotionPlan } from "../src/types"

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

describe("panelApi archive review endpoints", () => {
  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis as unknown as { fetch: typeof fetch }, "fetch")
    fetchSpy.mockReset()
  })

  afterEach(() => {
    fetchSpy.mockRestore()
    globalThis.fetch = originalFetch
  })

  it("queries list with status/limit/offset using status query key", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        schema_version: "storage-v2/archive-review-list@1",
        available: true,
        filters: { review_status: "open", limit: 5, offset: 2 },
        counts: { open: 1, triaged: 0, resolved: 0, dismissed: 0 },
        total: 1,
        cases: [],
        capabilities: {
          status_writes_enabled: true,
          promotions_enabled: false,
        },
      }),
    )

    await fetchArchiveReviewCases({
      reviewStatus: "open",
      limit: 5,
      offset: 2,
    })

    expect(fetchSpy).toHaveBeenCalledTimes(1)
    const [url] = fetchSpy.mock.calls[0] ?? []
    expect(url).toBe("/api/storage-v2/archive-evidence/cases?status=open&limit=5&offset=2")
  })

  it("correlates case detail by requested case key", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        schema_version: "storage-v2/archive-review-detail@1",
        case: {
          case_key: "CASE-001",
          legacy_delivery_key: "legacy-01",
          logical_stem: "SAMPLE-STEM",
          subject_abbr: "STAT",
          review_status: "open",
          promoted_recording_id: null,
          promoted_storage_key: null,
          created_at: "2026-07-23T09:00:00+09:00",
          updated_at: "2026-07-23T09:00:00+09:00",
          resolved_at: null,
        },
        latest_capture: {
          capture_key: "cap-1",
          reconciliation_classification: "candidate",
          captured_at: "2026-07-23T09:00:00+09:00",
        },
        capture_count: 0,
        captures_truncated: false,
        revision_count: 0,
        captures: [],
        revisions: [],
        canonical_suggestions: [],
        confirmed_selections: [],
      }),
    )
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        schema_version: "storage-v2/archive-review-detail@1",
        case: {
          case_key: "CASE-BAD",
          legacy_delivery_key: "legacy-bad",
          logical_stem: "BAD-STEM",
          subject_abbr: "BAD",
          review_status: "open",
          promoted_recording_id: null,
          promoted_storage_key: null,
          created_at: "2026-07-23T09:00:00+09:00",
          updated_at: "2026-07-23T09:00:00+09:00",
          resolved_at: null,
        },
        latest_capture: {
          capture_key: "cap-bad",
          reconciliation_classification: "candidate",
          captured_at: "2026-07-23T09:00:00+09:00",
        },
        capture_count: 0,
        captures_truncated: false,
        revision_count: 0,
        captures: [],
        revisions: [],
        canonical_suggestions: [],
        confirmed_selections: [],
      }),
    )

    const payload = await fetchArchiveReviewCaseDetail("CASE-001")
    expect(payload.case.case_key).toBe("CASE-001")

    await expect(fetchArchiveReviewCaseDetail("CASE-001")).rejects.toThrow(
      /case detail response case_key does not match the request/i,
    )
  })

  it("posts status update with requested status and allow_write", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        schema_version: "storage-v2/archive-review-status@1",
        ok: true,
        case_key: "CASE-001",
        review_status: "dismissed",
        changed: true,
      }),
    )

    await postArchiveReviewStatus("CASE-001", {
      review_status: "dismissed",
      allow_write: true,
    })

    expect(fetchSpy).toHaveBeenCalledTimes(1)
    const [url, init] = fetchSpy.mock.calls[0] ?? []
    expect(url).toBe("/api/storage-v2/archive-evidence/cases/CASE-001/status")
    expect(init?.method).toBe("POST")
    expect(JSON.parse(String(init?.body))).toEqual({
      review_status: "dismissed",
      allow_write: true,
    })
  })

  it("calculates a guarded promotion plan with stable request normalization", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        schema_version: "storage-v2/archive-review-promotion-plan@1",
        case_key: "CASE-001",
        case_updated_at: "2026-07-23T09:00:00+09:00",
        review_status: "open",
        target_recording_id: 7,
        target_storage_key: "2026-07-23-cs",
        latest_capture_key: "cap-1",
        latest_capture_classification: "candidate",
        selected_revisions: [
          {
            artifact_kind: "correction_text",
            revision_id: 11,
            content_sha256: "a".repeat(64),
            bytes: 1024,
            mime_type: null,
          },
          {
            artifact_kind: "summary_markdown",
            revision_id: 13,
            content_sha256: "c".repeat(64),
            bytes: 2048,
            mime_type: "text/markdown",
          },
        ],
        expected_count: 1,
        mode: "read_only",
        plan_sha256: "a".repeat(64),
      }),
    )

    const plan = await planArchiveReviewPromotion("CASE-001", {
      target_storage_key: " 2026-07-23-cs ",
      selected_revisions: {
        summary_markdown: 13,
        correction_text: 11,
      },
    })

    const [url, init] = fetchSpy.mock.calls[0] ?? []
    expect(url).toBe("/api/storage-v2/archive-evidence/cases/CASE-001/promotion/plan")
    expect(JSON.parse(String(init?.body))).toEqual({
      target_storage_key: "2026-07-23-cs",
      selected_revisions: {
        correction_text: 11,
        summary_markdown: 13,
      },
    })
    expect(plan.case_key).toBe("CASE-001")
    expect(plan.expected_count).toBe(1)
  })

  it("rejects guard drift between plan request and backend response", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        schema_version: "storage-v2/archive-review-promotion-plan@1",
        case_key: "CASE-001",
        case_updated_at: "2026-07-23T09:00:00+09:00",
        review_status: "open",
        target_recording_id: 7,
        target_storage_key: "2026-07-23-cs",
        latest_capture_key: "cap-1",
        latest_capture_classification: "candidate",
        selected_revisions: [
          {
            artifact_kind: "correction_text",
            revision_id: 99,
            content_sha256: "a".repeat(64),
            bytes: 1024,
            mime_type: null,
          },
          {
            artifact_kind: "summary_markdown",
            revision_id: 88,
            content_sha256: "c".repeat(64),
            bytes: 2048,
            mime_type: "text/markdown",
          },
        ],
        expected_count: 1,
        mode: "read_only",
        plan_sha256: "a".repeat(64),
      }),
    )

    await expect(
      planArchiveReviewPromotion("CASE-001", {
        target_storage_key: "2026-07-23-cs",
        selected_revisions: { correction_text: 11 },
      }),
    ).rejects.toThrow("selection count does not match")
  })

  it("applies guarded payload with expected_count, digest, allow_write", async () => {
    const plan = {
      schema_version: "storage-v2/archive-review-promotion-plan@1",
      case_key: "CASE-001",
      case_updated_at: "2026-07-23T09:00:00+09:00",
      review_status: "open",
      target_recording_id: 7,
      target_storage_key: "2026-07-23-cs",
      latest_capture_key: "cap-1",
      latest_capture_classification: "candidate",
      selected_revisions: [
        {
          artifact_kind: "correction_text",
          revision_id: 11,
          content_sha256: "a".repeat(64),
          bytes: 1024,
          mime_type: null,
        },
      ],
      expected_count: 1,
      mode: "read_only",
      plan_sha256: "f".repeat(64),
    } satisfies Omit<ArchiveReviewPromotionPlan, "target_recording_id" | "case_updated_at" | "latest_capture_key" | "latest_capture_classification" | "review_status" | "selected_revisions" | "mode" | "target_recording_id">

    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        ...plan,
        selected_revisions: [
          {
            artifact_kind: "correction_text",
            revision_id: 11,
            content_sha256: "a".repeat(64),
            bytes: 1024,
            mime_type: null,
          },
        ],
      }),
    )
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        schema_version: "storage-v2/archive-review-promotion-result@1",
        ok: true,
        status: "promoted",
        case_key: "CASE-001",
        target_storage_key: "2026-07-23-cs",
        plan_sha256: "f".repeat(64),
        selected_count: 1,
      }),
    )

    const gotPlan = await planArchiveReviewPromotion("CASE-001", {
      target_storage_key: "2026-07-23-cs",
      selected_revisions: { correction_text: 11 },
    })
    const result = await applyArchiveReviewPromotion("CASE-001", {
      target_storage_key: "2026-07-23-cs",
      selected_revisions: { correction_text: 11 },
      expected_count: gotPlan.expected_count,
      expected_plan_sha256: gotPlan.plan_sha256,
      allow_write: true,
    })

    expect(result.status).toBe("promoted")
    expect(fetchSpy).toHaveBeenNthCalledWith(
      2,
      "/api/storage-v2/archive-evidence/cases/CASE-001/promotion/apply",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Content-Type": "application/json",
        }),
        body: JSON.stringify({
          target_storage_key: "2026-07-23-cs",
          selected_revisions: { correction_text: 11 },
          expected_count: 1,
          expected_plan_sha256: "f".repeat(64),
          allow_write: true,
        }),
      }),
    )
  })

  it("rejects apply when backend result selected_count does not match request", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        schema_version: "storage-v2/archive-review-promotion-plan@1",
        case_key: "CASE-001",
        case_updated_at: "2026-07-23T09:00:00+09:00",
        review_status: "open",
        target_recording_id: 7,
        target_storage_key: "2026-07-23-cs",
        latest_capture_key: "cap-1",
        latest_capture_classification: "candidate",
        selected_revisions: [
          {
            artifact_kind: "correction_text",
            revision_id: 11,
            content_sha256: "a".repeat(64),
            bytes: 1024,
            mime_type: null,
          },
        ],
        expected_count: 1,
        mode: "read_only",
        plan_sha256: "f".repeat(64),
      }),
    )
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        schema_version: "storage-v2/archive-review-promotion-result@1",
        ok: true,
        status: "promoted",
        case_key: "CASE-001",
        target_storage_key: "2026-07-23-cs",
        plan_sha256: "f".repeat(64),
        selected_count: 2,
      }),
    )

    const plan = await planArchiveReviewPromotion("CASE-001", {
      target_storage_key: "2026-07-23-cs",
      selected_revisions: { correction_text: 11 },
    })

    await expect(
      applyArchiveReviewPromotion("CASE-001", {
        target_storage_key: "2026-07-23-cs",
        selected_revisions: { correction_text: 11 },
        expected_count: plan.expected_count,
        expected_plan_sha256: plan.plan_sha256,
        allow_write: true,
      }),
    ).rejects.toThrow("selected_count");
  })

  it("propagates backend 403/409 errors", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(403, {
        error: "권한이 없습니다.",
      }),
    )

    await expect(
      postArchiveReviewStatus("CASE-001", {
        review_status: "triaged",
        allow_write: true,
      }),
    ).rejects.toThrow("권한이 없습니다.")

    fetchSpy.mockReset()
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, {
        schema_version: "storage-v2/archive-review-promotion-plan@1",
        case_key: "CASE-001",
        case_updated_at: "2026-07-23T09:00:00+09:00",
        review_status: "open",
        target_recording_id: 7,
        target_storage_key: "2026-07-23-cs",
        latest_capture_key: "cap-1",
        latest_capture_classification: "candidate",
        selected_revisions: [
          {
            artifact_kind: "correction_text",
            revision_id: 11,
            content_sha256: "a".repeat(64),
            bytes: 1024,
            mime_type: null,
          },
        ],
        expected_count: 1,
        mode: "read_only",
        plan_sha256: "f".repeat(64),
      }),
    )
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(409, {
        error: "동시 수정 충돌: 사례가 변경되었습니다.",
      }),
    )

    const plan = await planArchiveReviewPromotion("CASE-001", {
      target_storage_key: "2026-07-23-cs",
      selected_revisions: { correction_text: 11 },
    })

    await expect(
      applyArchiveReviewPromotion("CASE-001", {
        target_storage_key: "2026-07-23-cs",
        selected_revisions: { correction_text: 11 },
        expected_count: plan.expected_count,
        expected_plan_sha256: plan.plan_sha256,
        allow_write: true,
      }),
    ).rejects.toThrow("동시 수정 충돌: 사례가 변경되었습니다.")
  })
})
