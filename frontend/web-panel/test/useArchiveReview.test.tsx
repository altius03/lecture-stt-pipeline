import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, renderHook, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { useArchiveReview } from "../src/hooks/useArchiveReview"
import {
  applyArchiveReviewPromotion,
  fetchArchiveReviewCaseDetail,
  fetchArchiveReviewCases,
  planArchiveReviewPromotion,
  postArchiveReviewStatus,
} from "../src/lib/panelApi"
import type {
  ArchiveReviewCaseListItem,
  ArchiveReviewDetailPayload,
  ArchiveReviewPromotionPlan,
  ArchiveReviewPromotionResult,
} from "../src/types"

vi.mock("../src/lib/panelApi", () => ({
  applyArchiveReviewPromotion: vi.fn(),
  fetchArchiveReviewCaseDetail: vi.fn(),
  fetchArchiveReviewCases: vi.fn(),
  planArchiveReviewPromotion: vi.fn(),
  postArchiveReviewStatus: vi.fn(),
}))

const fetchCasesMock = vi.mocked(fetchArchiveReviewCases)
const fetchCaseDetailMock = vi.mocked(fetchArchiveReviewCaseDetail)
const planPromotionMock = vi.mocked(planArchiveReviewPromotion)
const applyPromotionMock = vi.mocked(applyArchiveReviewPromotion)
const postStatusMock = vi.mocked(postArchiveReviewStatus)

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
}

function baseCase(caseKey: string, reviewStatus: "open" | "triaged" = "open"): ArchiveReviewCaseListItem {
  return {
    case_key: caseKey,
    logical_stem: `stem-${caseKey}`,
    subject_abbr: "STAT",
    review_status: reviewStatus,
    promoted_recording_id: null,
    promoted_storage_key: null,
    created_at: "2026-07-23T09:00:00+09:00",
    updated_at: "2026-07-23T09:00:00+09:00",
    resolved_at: null,
    capture_count: 3,
    revision_count: 3,
    last_captured_at: "2026-07-23T08:00:00+09:00",
    latest_classification: "candidate",
  }
}

function baseDetail(caseKey: string): ArchiveReviewDetailPayload {
  return {
    schema_version: "storage-v2/archive-review-detail@1",
    case: {
      case_key: caseKey,
      legacy_delivery_key: `legacy-${caseKey}`,
      logical_stem: `stem-${caseKey}`,
      subject_abbr: "STAT",
      review_status: "open",
      promoted_recording_id: null,
      promoted_storage_key: null,
      created_at: "2026-07-23T09:00:00+09:00",
      updated_at: "2026-07-23T09:00:00+09:00",
      resolved_at: null,
    },
    latest_capture: {
      capture_key: `cap-${caseKey}`,
      reconciliation_classification: "candidate",
      captured_at: "2026-07-23T08:00:00+09:00",
    },
    capture_count: 3,
    captures_truncated: false,
    revision_count: 3,
    captures: [],
    revisions: [
      {
        revision_id: 11,
        artifact_kind: "correction_text",
        content_sha256: "a".repeat(64),
        bytes: 1200,
        mime_type: "text/plain",
        created_at: "2026-07-23T08:30:00+09:00",
        observation_count: 1,
        capture_count: 1,
        last_observed_at: null,
        matches_ledger_count: 1,
        differs_from_ledger_count: 0,
        unclaimed_count: 0,
        unexpected_current_count: 0,
        source_roles: ["current"],
        relationships: ["recording"],
      },
      {
        revision_id: 21,
        artifact_kind: "correction_json",
        content_sha256: "b".repeat(64),
        bytes: 1300,
        mime_type: "application/json",
        created_at: "2026-07-23T08:40:00+09:00",
        observation_count: 2,
        capture_count: 1,
        last_observed_at: null,
        matches_ledger_count: 1,
        differs_from_ledger_count: 0,
        unclaimed_count: 0,
        unexpected_current_count: 0,
        source_roles: ["current"],
        relationships: ["recording"],
      },
      {
        revision_id: 31,
        artifact_kind: "summary_markdown",
        content_sha256: "c".repeat(64),
        bytes: 2400,
        mime_type: "text/markdown",
        created_at: "2026-07-23T08:50:00+09:00",
        observation_count: 1,
        capture_count: 1,
        last_observed_at: null,
        matches_ledger_count: 0,
        differs_from_ledger_count: 1,
        unclaimed_count: 0,
        unexpected_current_count: 0,
        source_roles: ["archive"],
        relationships: ["recording"],
      },
    ],
    canonical_suggestions: [
      {
        artifact_kind: "correction_text",
        status: "suggested",
        revision_id: 11,
        content_sha256: "a".repeat(64),
        reason_code: "unique_matches_ledger",
        candidate_revision_ids: [11],
      },
      {
        artifact_kind: "correction_json",
        status: "unresolved",
        revision_id: null,
        content_sha256: null,
        reason_code: "multiple_ledger_matches",
        candidate_revision_ids: [21],
      },
      {
        artifact_kind: "summary_markdown",
        status: "unresolved",
        revision_id: null,
        content_sha256: null,
        reason_code: "no_unique_current_candidate",
        candidate_revision_ids: [31],
      },
    ],
    confirmed_selections: [],
    capabilities: {
      status_writes_enabled: true,
      promotions_enabled: true,
    },
  }
}

function buildPromotionPlan(
  caseKey: string,
  selectedRevisions: Record<string, number>,
  options?: { targetStorageKey?: string },
): ArchiveReviewPromotionPlan {
  const selected = Object.entries(selectedRevisions)
    .sort((left, right) => left[0].localeCompare(right[0]))
    .map(([artifactKind, revisionId]) => {
      const sha = artifactKind === "correction_text" ? "a".repeat(64) : artifactKind === "correction_json" ? "b".repeat(64) : "c".repeat(64)
      return {
        artifact_kind: artifactKind as "correction_text" | "correction_json" | "summary_markdown",
        revision_id: revisionId,
        content_sha256: sha,
        bytes: 1000,
        mime_type: null,
      }
    })

  return {
    schema_version: "storage-v2/archive-review-promotion-plan@1",
    case_key: caseKey,
    case_updated_at: "2026-07-23T09:00:00+09:00",
    review_status: "open",
    target_recording_id: 1,
    target_storage_key: options?.targetStorageKey ?? "20260723_001",
    latest_capture_key: `cap-${caseKey}`,
    latest_capture_classification: "candidate",
    selected_revisions: selected,
    expected_count: 1,
    mode: "read_only",
    plan_sha256: "f".repeat(64),
  }
}

describe("useArchiveReview", () => {
  beforeEach(() => {
    fetchCasesMock.mockReset()
    fetchCaseDetailMock.mockReset()
    planPromotionMock.mockReset()
    applyPromotionMock.mockReset()
    postStatusMock.mockReset()
    fetchCasesMock.mockResolvedValue({
      schema_version: "storage-v2/archive-review-list@1",
      available: true,
      filters: { review_status: null, limit: 20, offset: 0 },
      counts: { open: 1, triaged: 0, resolved: 0, dismissed: 0 },
      total: 1,
      cases: [baseCase("CASE-001")],
      capabilities: {
        status_writes_enabled: true,
        promotions_enabled: true,
      },
    })
    fetchCaseDetailMock.mockResolvedValue(baseDetail("CASE-001"))
  })

  it("loads list and detail and preselects suggested revisions only", async () => {
    const { result } = renderHook(() => useArchiveReview(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.selectedCaseKey).toBe("CASE-001")
      expect(result.current.caseDetail?.case.case_key).toBe("CASE-001")
    })

    expect(result.current.selectedRevisions).toEqual({
      correction_text: 11,
    })
    expect(result.current.hasCompleteSelection).toBe(false)
    expect(result.current.targetStorageKey).toBe("")
  })

  it("preserves an exact initial case selection even when it is outside the current list page", async () => {
    fetchCaseDetailMock.mockImplementation(async (caseKey) => baseDetail(caseKey))

    const { result } = renderHook(
      () =>
        useArchiveReview({
          initialSelectedCaseKey: "CASE-999",
        }),
      {
        wrapper: createWrapper(),
      },
    )

    await waitFor(() => {
      expect(result.current.caseDetail?.case.case_key).toBe("CASE-999")
    })

    expect(result.current.selectedCaseKey).toBe("CASE-999")
    expect(result.current.selectedCase).toBeNull()
    expect(fetchCaseDetailMock).toHaveBeenCalledWith("CASE-999")
  })

  it("allows manual selection for unresolved suggestions and requests a guarded promotion plan", async () => {
    const { result } = renderHook(() => useArchiveReview(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.caseDetail?.case.case_key).toBe("CASE-001")
    })

    act(() => {
      result.current.setTargetStorageKey("target-001")
      result.current.setSelectedRevision("correction_json", 21)
      result.current.setSelectedRevision("summary_markdown", 31)
    })

    await waitFor(() => {
      expect(result.current.hasCompleteSelection).toBe(true)
    })

    planPromotionMock.mockResolvedValueOnce(
      buildPromotionPlan("CASE-001", {
        correction_text: 11,
        correction_json: 21,
        summary_markdown: 31,
      }),
    )

    await act(async () => {
      const success = await result.current.loadPromotionPlan()
      expect(success).toBe(true)
    })

    await waitFor(() => {
      expect(result.current.promotionPlan?.case_key).toBe("CASE-001")
      expect(result.current.promotionPlanRequest).toEqual({
        caseKey: "CASE-001",
        targetStorageKey: "target-001",
        selectedRevisions: {
          correction_text: 11,
          correction_json: 21,
          summary_markdown: 31,
        },
      })
    })
  })

  it("invalidates promotion plan when selected revisions change", async () => {
    const { result } = renderHook(() => useArchiveReview(), {
      wrapper: createWrapper(),
    })
    fetchCasesMock.mockResolvedValueOnce({
      schema_version: "storage-v2/archive-review-list@1",
      available: true,
      filters: { review_status: null, limit: 20, offset: 0 },
      counts: { open: 1, triaged: 0, resolved: 0, dismissed: 0 },
      total: 1,
      cases: [baseCase("CASE-001")],
      capabilities: {
        status_writes_enabled: true,
        promotions_enabled: true,
      },
    })
    planPromotionMock.mockResolvedValueOnce(
      buildPromotionPlan("CASE-001", {
        correction_text: 11,
      }, {
        targetStorageKey: "target-001",
      }),
    )

    await waitFor(() => {
      expect(result.current.caseDetail?.case.case_key).toBe("CASE-001")
    })

    act(() => {
      result.current.setTargetStorageKey("target-001")
      result.current.setSelectedRevision("correction_json", 21)
      result.current.setSelectedRevision("summary_markdown", 31)
    })

    await waitFor(() => {
      expect(result.current.hasCompleteSelection).toBe(true)
    })

    const success = await act(async () => {
      const success = await result.current.loadPromotionPlan()
      return success
    })

    expect(success).toBe(true)

    expect(result.current.actionError).toBeNull()

    await waitFor(() => {
      expect(result.current.promotionPlan?.target_storage_key).toBe("target-001")
    })

    act(() => {
      result.current.setSelectedRevision("correction_text", 11)
      result.current.setTargetStorageKey("target-002")
    })

    await waitFor(() => {
      expect(result.current.promotionPlan).toBeNull()
      expect(result.current.actionNotice).toBeNull()
    })
  })

  it("keeps stale promotion plan result from contaminating a newer selected case", async () => {
    let resolvePlan: ((value: ArchiveReviewPromotionPlan) => void) | null = null

    fetchCasesMock.mockResolvedValueOnce({
      schema_version: "storage-v2/archive-review-list@1",
      available: true,
      filters: { review_status: null, limit: 20, offset: 0 },
      counts: { open: 2, triaged: 0, resolved: 0, dismissed: 0 },
      total: 2,
      cases: [baseCase("CASE-001"), baseCase("CASE-002")],
      capabilities: {
        status_writes_enabled: true,
        promotions_enabled: true,
      },
    })
    fetchCaseDetailMock.mockImplementation((caseKey) =>
      Promise.resolve({
        ...baseDetail(caseKey),
        case: {
          ...baseDetail(caseKey).case,
          case_key: caseKey,
        },
      }),
    )

    const pendingPlan = new Promise<ArchiveReviewPromotionPlan>((resolve) => {
      resolvePlan = resolve
    })
    planPromotionMock.mockImplementation(() => pendingPlan)

    const { result } = renderHook(() => useArchiveReview(), {
      wrapper: createWrapper(),
    })

    act(() => {
      result.current.setSelectedCaseKey("CASE-001")
    })

    await waitFor(() => {
      expect(result.current.selectedCaseKey).toBe("CASE-001")
    })

    act(() => {
      result.current.setTargetStorageKey("target-001")
      result.current.setSelectedRevision("correction_json", 21)
      result.current.setSelectedRevision("summary_markdown", 31)
      void result.current.loadPromotionPlan()
    })

    await act(async () => {
      result.current.setSelectedCaseKey("CASE-002")
    })

    await act(async () => {
      result.current.setSelectedCaseKey("CASE-002")
      resolvePlan?.(
        buildPromotionPlan("CASE-001", {
          correction_text: 11,
          correction_json: 21,
          summary_markdown: 31,
        }),
      )
      await Promise.resolve(pendingPlan)
    })

    expect(result.current.promotionPlan).toBeNull()
    expect(result.current.promotionPlanRequest).toBeNull()
  })

  it("prevents status updates and apply from stale responses after selection changes", async () => {
    let resolveStatus: ((value: boolean) => void) | null = null
    let resolveApply: ((value: ArchiveReviewPromotionResult) => void) | null = null

    fetchCasesMock.mockResolvedValueOnce({
      schema_version: "storage-v2/archive-review-list@1",
      available: true,
      filters: { review_status: null, limit: 20, offset: 0 },
      counts: { open: 2, triaged: 0, resolved: 0, dismissed: 0 },
      total: 2,
      cases: [baseCase("CASE-001"), baseCase("CASE-002")],
      capabilities: {
        status_writes_enabled: true,
        promotions_enabled: true,
      },
    })
    fetchCaseDetailMock.mockImplementation(async (caseKey) => {
      return {
        ...baseDetail(caseKey),
      }
    })
    postStatusMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveStatus = resolve as (value: { ok: true; case_key: string; review_status: "triaged"; changed: boolean }) => void
        }),
    )
    planPromotionMock.mockResolvedValueOnce(
      buildPromotionPlan("CASE-001", {
        correction_text: 11,
        correction_json: 21,
        summary_markdown: 31,
      }, {
        targetStorageKey: "target-001",
      }),
    )
    applyPromotionMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveApply = resolve as (value: ArchiveReviewPromotionResult) => void
        }),
    )

    const { result } = renderHook(() => useArchiveReview(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.selectedCaseKey).toBe("CASE-001")
    })

    const pendingStatus = result.current.updateReviewStatus("triaged")
    expect(pendingStatus).toBeInstanceOf(Promise)
    act(() => {
      result.current.setSelectedCaseKey("CASE-002")
    })

    await waitFor(() => expect(resolveStatus).not.toBeNull())
    act(() => {
      resolveStatus?.({
        ok: true,
        case_key: "CASE-001",
        review_status: "triaged",
        changed: true,
      })
    })
    expect(await pendingStatus).toBe(true)

    expect(result.current.actionNotice).toBeNull()
    expect(result.current.selectedCaseKey).toBe("CASE-002")

    act(() => {
      result.current.setSelectedCaseKey("CASE-001")
    })
    await waitFor(() => {
      expect(result.current.selectedCaseKey).toBe("CASE-001")
    })

    act(() => {
      result.current.setTargetStorageKey("target-001")
      result.current.setSelectedRevision("correction_json", 21)
      result.current.setSelectedRevision("summary_markdown", 31)
    })

    await waitFor(() => {
      expect(result.current.hasCompleteSelection).toBe(true)
    })

    await act(async () => {
      expect(await result.current.loadPromotionPlan()).toBe(true)
    })

    expect(result.current.promotionPlan).not.toBeNull()

    const pendingApply = result.current.applyPromotionPlan()
    expect(pendingApply).toBeInstanceOf(Promise)

    act(() => {
      result.current.setSelectedRevision("correction_text", 11)
    })

    await waitFor(() => expect(resolveApply).not.toBeNull())
    await act(async () => {
      resolveApply?.({
        schema_version: "storage-v2/archive-review-promotion-result@1",
        ok: true,
        status: "promoted",
        case_key: "CASE-001",
        target_storage_key: "target-001",
        plan_sha256: "f".repeat(64),
        selected_count: 1,
      })
    })

    expect(await pendingApply).toBe(true)

    expect(result.current.selectedCaseKey).toBe("CASE-001")
    expect(result.current.actionNotice).toBeNull()
  })

  it("returns false and keeps explicit errors for failed mutations", async () => {
    fetchCasesMock.mockResolvedValueOnce({
      schema_version: "storage-v2/archive-review-list@1",
      available: true,
      filters: { review_status: null, limit: 20, offset: 0 },
      counts: { open: 1, triaged: 0, resolved: 0, dismissed: 0 },
      total: 1,
      cases: [baseCase("CASE-001")],
      capabilities: {
        status_writes_enabled: true,
        promotions_enabled: true,
      },
    })

    postStatusMock.mockRejectedValue(new Error("status update blocked"))
    planPromotionMock.mockResolvedValueOnce(
      buildPromotionPlan("CASE-001", {
        correction_text: 11,
        correction_json: 21,
        summary_markdown: 31,
      }, {
        targetStorageKey: "target-001",
      }),
    )
    applyPromotionMock.mockRejectedValue(new Error("promotion blocked"))

    const { result } = renderHook(() => useArchiveReview(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.selectedCaseKey).toBe("CASE-001")
    })

    const statusResult = await result.current.updateReviewStatus("triaged")
    expect(statusResult).toBe(false)
    await waitFor(() => expect(result.current.actionError).toBe("status update blocked"))

    act(() => {
      result.current.setTargetStorageKey("target-001")
      result.current.setSelectedRevision("correction_json", 21)
      result.current.setSelectedRevision("summary_markdown", 31)
    })

    await waitFor(() => {
      expect(result.current.hasCompleteSelection).toBe(true)
    })

    const planSuccess = await act(async () => result.current.loadPromotionPlan())
    expect(planSuccess).toBe(true)
    await waitFor(() => {
      expect(result.current.promotionPlan).not.toBeNull()
    })
    const applySuccess = await act(async () => result.current.applyPromotionPlan())
    expect(applySuccess).toBe(false)

    await waitFor(() => expect(result.current.actionError).toBe("promotion blocked"))
  })
})
