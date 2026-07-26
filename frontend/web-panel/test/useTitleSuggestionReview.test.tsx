import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import {
  TITLE_SUGGESTIONS_QUERY_KEY,
  useTitleSuggestionReview,
} from "../src/hooks/useTitleSuggestionReview"
import { UNIFIED_REVIEW_QUERY_KEY } from "../src/hooks/useUnifiedReviewFeed"
import {
  applyTitleSuggestionConfirmation,
  fetchTitleSuggestionDetail,
  fetchTitleSuggestions,
  planTitleSuggestionConfirmation,
  postTitleSuggestionStatus,
} from "../src/lib/panelApi"
import type {
  TitleSuggestionConfirmationPlan,
  TitleSuggestionDetailPayload,
  TitleSuggestionListPayload,
  TitleSuggestionStatusResult,
} from "../src/types"
import {
  buildDisabledTitleSuggestionListPayload,
  buildTitleSuggestionConfirmationPlan,
  buildTitleSuggestionConfirmationResult,
  buildTitleSuggestionDetailPayload,
  buildTitleSuggestionListPayload,
  buildTitleSuggestionStatusResult,
} from "./titleSuggestionFixtures"

vi.mock("../src/lib/panelApi", () => ({
  applyTitleSuggestionConfirmation: vi.fn(),
  fetchTitleSuggestionDetail: vi.fn(),
  fetchTitleSuggestions: vi.fn(),
  planTitleSuggestionConfirmation: vi.fn(),
  postTitleSuggestionStatus: vi.fn(),
}))

const applyConfirmationMock = vi.mocked(applyTitleSuggestionConfirmation)
const fetchDetailMock = vi.mocked(fetchTitleSuggestionDetail)
const fetchListMock = vi.mocked(fetchTitleSuggestions)
const planConfirmationMock = vi.mocked(planTitleSuggestionConfirmation)
const postStatusMock = vi.mocked(postTitleSuggestionStatus)

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

function createHarness(queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
      gcTime: Infinity,
    },
    mutations: {
      retry: false,
    },
  },
})) {
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        {children}
      </QueryClientProvider>
    )
  }
  return { queryClient, wrapper: Wrapper }
}

function writableList(twoProposals = false): TitleSuggestionListPayload {
  const payload = buildTitleSuggestionListPayload()
  payload.capabilities = {
    confirmations_enabled: true,
    status_writes_enabled: true,
  }
  if (twoProposals) {
    payload.proposals.push({
      ...structuredClone(payload.proposals[0]),
      id: 12,
      storage_key: "rec_second",
      proposed_title: "회의 후속 작업 정리",
      suggestion_reason: "content_topic",
      classification_status: null,
      context_type: "meeting",
      created_at: "2026-07-24T13:10:00+09:00",
      updated_at: "2026-07-24T13:10:00+09:00",
    })
    payload.counts.suggested = 2
    payload.total = 2
  }
  return payload
}

function writableDetail(proposalId = 11): TitleSuggestionDetailPayload {
  const detail = buildTitleSuggestionDetailPayload(proposalId)
  detail.capabilities = {
    confirmations_enabled: true,
    status_writes_enabled: true,
  }
  if (proposalId === 12) {
    detail.proposal.storage_key = "rec_second"
    detail.proposal.proposed_title = "회의 후속 작업 정리"
    detail.proposal.suggestion_reason = "content_topic"
    detail.proposal.classification_status = null
    detail.proposal.context_type = "meeting"
    detail.proposal.created_at = "2026-07-24T13:10:00+09:00"
    detail.proposal.updated_at = "2026-07-24T13:10:00+09:00"
    detail.linked_classification = null
  }
  return detail
}

function mockWritableQueue(twoProposals = false): void {
  fetchListMock.mockResolvedValue(writableList(twoProposals))
  fetchDetailMock.mockImplementation(async (proposalId) =>
    writableDetail(proposalId),
  )
}

describe("useTitleSuggestionReview", () => {
  beforeEach(() => {
    applyConfirmationMock.mockReset()
    fetchDetailMock.mockReset()
    fetchListMock.mockReset()
    planConfirmationMock.mockReset()
    postStatusMock.mockReset()
  })

  it("preserves a first-page-out deep link after a fresh available list", async () => {
    mockWritableQueue()
    const { result } = renderHook(
      () =>
        useTitleSuggestionReview({
          initialSelectedProposalId: 99,
        }),
      { wrapper: createHarness().wrapper },
    )

    await waitFor(() => {
      expect(result.current.proposalDetail?.proposal.id).toBe(99)
    })

    expect(result.current.selectedProposalId).toBe(99)
    expect(fetchListMock).toHaveBeenCalledWith(
      { status: "suggested", limit: 8 },
      expect.any(AbortSignal),
    )
    expect(fetchDetailMock).toHaveBeenCalledWith(
      99,
      expect.any(AbortSignal),
    )
  })

  it("does not fetch detail or plan for a disabled deep link", async () => {
    fetchListMock.mockResolvedValue(buildDisabledTitleSuggestionListPayload())
    const { result } = renderHook(
      () =>
        useTitleSuggestionReview({
          initialSelectedProposalId: 11,
        }),
      { wrapper: createHarness().wrapper },
    )

    await waitFor(() => {
      expect(result.current.proposals?.available).toBe(false)
    })

    expect(result.current.proposalDetail).toBeNull()
    expect(fetchDetailMock).not.toHaveBeenCalled()
    await expect(result.current.loadConfirmationPlan()).resolves.toBe(false)
    await expect(result.current.rejectProposal()).resolves.toBe(false)
    expect(planConfirmationMock).not.toHaveBeenCalled()
    expect(postStatusMock).not.toHaveBeenCalled()
    expect(fetchListMock).toHaveBeenCalledTimes(1)
  })

  it("hides warm list and detail cache until fresh remount responses settle", async () => {
    mockWritableQueue()
    const harness = createHarness()
    const firstMount = renderHook(
      () =>
        useTitleSuggestionReview({
          initialSelectedProposalId: 11,
        }),
      { wrapper: harness.wrapper },
    )

    await waitFor(() => {
      expect(firstMount.result.current.proposalDetail?.proposal.id).toBe(11)
    })
    firstMount.unmount()

    const listReload = deferred<TitleSuggestionListPayload>()
    const detailReload = deferred<TitleSuggestionDetailPayload>()
    fetchListMock.mockImplementationOnce(() => listReload.promise)
    fetchDetailMock.mockImplementationOnce(() => detailReload.promise)

    const secondMount = renderHook(
      () =>
        useTitleSuggestionReview({
          initialSelectedProposalId: 11,
        }),
      { wrapper: harness.wrapper },
    )

    expect(secondMount.result.current.proposals).toBeNull()
    expect(secondMount.result.current.proposalDetail).toBeNull()
    expect(fetchDetailMock).toHaveBeenCalledTimes(1)

    listReload.resolve(writableList())
    await waitFor(() => {
      expect(fetchDetailMock).toHaveBeenCalledTimes(2)
    })
    expect(secondMount.result.current.proposalDetail).toBeNull()

    detailReload.resolve(writableDetail())
    await waitFor(() => {
      expect(secondMount.result.current.proposalDetail?.proposal.id).toBe(11)
    })
  })

  it("ignores a stale confirmation plan after the selection changes", async () => {
    mockWritableQueue(true)
    const planRequest = deferred<TitleSuggestionConfirmationPlan>()
    planConfirmationMock.mockImplementationOnce(() => planRequest.promise)
    const { result } = renderHook(
      () =>
        useTitleSuggestionReview({
          initialSelectedProposalId: 11,
        }),
      { wrapper: createHarness().wrapper },
    )

    await waitFor(() => {
      expect(result.current.proposalDetail?.proposal.id).toBe(11)
    })

    let pendingPlan!: Promise<boolean>
    act(() => {
      pendingPlan = result.current.loadConfirmationPlan()
    })
    await waitFor(() => {
      expect(planConfirmationMock).toHaveBeenCalledTimes(1)
    })
    act(() => {
      result.current.setSelectedProposalId(12)
    })
    await waitFor(() => {
      expect(result.current.proposalDetail?.proposal.id).toBe(12)
    })

    planRequest.resolve(buildTitleSuggestionConfirmationPlan(11))
    await act(async () => {
      await pendingPlan
    })

    expect(result.current.selectedProposalId).toBe(12)
    expect(result.current.confirmationPlan).toBeNull()
  })

  it("rejects a plan whose metadata no longer matches the fresh detail", async () => {
    mockWritableQueue()
    const mismatchedPlan = buildTitleSuggestionConfirmationPlan()
    mismatchedPlan.transcript_revision = 3
    planConfirmationMock.mockResolvedValue(mismatchedPlan)
    const { result } = renderHook(
      () =>
        useTitleSuggestionReview({
          initialSelectedProposalId: 11,
        }),
      { wrapper: createHarness().wrapper },
    )

    await waitFor(() => {
      expect(result.current.proposalDetail?.proposal.id).toBe(11)
    })
    await act(async () => {
      await result.current.loadConfirmationPlan()
    })

    expect(result.current.confirmationPlan).toBeNull()
    expect(result.current.actionError).toMatch(/metadata와 달라졌습니다/)
  })

  it("applies exact plan guards and invalidates title and unified feeds", async () => {
    mockWritableQueue()
    planConfirmationMock.mockResolvedValue(
      buildTitleSuggestionConfirmationPlan(),
    )
    applyConfirmationMock.mockResolvedValue(
      buildTitleSuggestionConfirmationResult(),
    )
    const harness = createHarness()
    const invalidateSpy = vi.spyOn(harness.queryClient, "invalidateQueries")
    const { result } = renderHook(
      () =>
        useTitleSuggestionReview({
          initialSelectedProposalId: 11,
        }),
      { wrapper: harness.wrapper },
    )

    await waitFor(() => {
      expect(result.current.proposalDetail?.proposal.id).toBe(11)
    })
    await act(async () => {
      await result.current.loadConfirmationPlan()
    })
    expect(result.current.confirmationPlan?.plan_sha256).toBe("a".repeat(64))

    await act(async () => {
      await result.current.applyConfirmationPlan()
    })

    expect(applyConfirmationMock).toHaveBeenCalledWith(11, {
      expected_count: 1,
      expected_plan_sha256: "a".repeat(64),
      allow_write: true,
    })
    expect(result.current.selectedProposalId).toBe(11)
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: TITLE_SUGGESTIONS_QUERY_KEY,
    })
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: UNIFIED_REVIEW_QUERY_KEY,
    })
  })

  it("keeps a newer selection when an older reject request resolves late", async () => {
    mockWritableQueue(true)
    const statusRequest = deferred<TitleSuggestionStatusResult>()
    postStatusMock.mockImplementationOnce(() => statusRequest.promise)
    const { result } = renderHook(
      () =>
        useTitleSuggestionReview({
          initialSelectedProposalId: 11,
        }),
      { wrapper: createHarness().wrapper },
    )

    await waitFor(() => {
      expect(result.current.proposalDetail?.proposal.id).toBe(11)
    })
    let pendingReject!: Promise<boolean>
    act(() => {
      pendingReject = result.current.rejectProposal()
    })
    await waitFor(() => {
      expect(postStatusMock).toHaveBeenCalledTimes(1)
    })
    act(() => {
      result.current.setSelectedProposalId(12)
    })
    await waitFor(() => {
      expect(result.current.proposalDetail?.proposal.id).toBe(12)
    })

    statusRequest.resolve(buildTitleSuggestionStatusResult(11))
    await act(async () => {
      await pendingReject
    })

    expect(result.current.selectedProposalId).toBe(12)
  })
})
