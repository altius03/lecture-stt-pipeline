import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { useTimetableReview } from "../src/hooks/useTimetableReview"
import {
  applyClassificationConfirmation,
  fetchClassificationProposalDetail,
  fetchClassificationProposals,
  fetchTimetableEntries,
  planClassificationConfirmation,
  postClassificationStatus,
} from "../src/lib/panelApi"
import type {
  ClassificationConfirmationPlan,
  ClassificationConfirmationResult,
  ClassificationProposalDetailPayload,
  ClassificationProposalListPayload,
  ClassificationStatusResult,
} from "../src/types"

vi.mock("../src/lib/panelApi", () => ({
  applyClassificationConfirmation: vi.fn(),
  fetchClassificationProposalDetail: vi.fn(),
  fetchClassificationProposals: vi.fn(),
  fetchTimetableEntries: vi.fn(),
  planClassificationConfirmation: vi.fn(),
  postClassificationStatus: vi.fn(),
}))

const applyConfirmationMock = vi.mocked(applyClassificationConfirmation)
const fetchEntriesMock = vi.mocked(fetchTimetableEntries)
const fetchProposalsMock = vi.mocked(fetchClassificationProposals)
const fetchDetailMock = vi.mocked(fetchClassificationProposalDetail)
const planConfirmationMock = vi.mocked(planClassificationConfirmation)
const postStatusMock = vi.mocked(postClassificationStatus)

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
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

function buildConfirmationPlan(): ClassificationConfirmationPlan {
  return {
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
  }
}

function mockTwoProposalQueue() {
  const proposals: ClassificationProposalListPayload = {
    schema_version: "storage-v2/classification-proposal-list@1",
    available: true,
    counts: { suggested: 2, confirmed: 0, rejected: 0 },
    total: 2,
    proposals: [
      {
        id: 7,
        storage_key: "20260723_abcd",
        status: "suggested",
        classification_reason: "unique_time_match",
        proposed_title: "2026-07-23 자료구조 5교시",
        context_type: "class_session",
        semester: "2026-2",
        course_name: "자료구조",
        course_code: "CSE201",
        session_date: "2026-07-23",
        weekday: "thu",
        start_time: "13:00",
        end_time: "14:15",
        period_label: "5교시",
        period_index: 5,
        classroom: "E201",
        confidence: 0.75,
        review_status: "open",
        created_at: "2026-07-23T09:00:00+09:00",
        updated_at: "2026-07-23T09:00:00+09:00",
        confirmed_at: null,
      },
      {
        id: 8,
        storage_key: "20260723_efgh",
        status: "suggested",
        classification_reason: "unique_time_match",
        proposed_title: "2026-07-23 운영체제 2교시",
        context_type: "class_session",
        semester: "2026-2",
        course_name: "운영체제",
        course_code: "CSE301",
        session_date: "2026-07-23",
        weekday: "thu",
        start_time: "10:00",
        end_time: "11:15",
        period_label: "2교시",
        period_index: 2,
        classroom: "E101",
        confidence: 0.66,
        review_status: "open",
        created_at: "2026-07-23T09:10:00+09:00",
        updated_at: "2026-07-23T09:10:00+09:00",
        confirmed_at: null,
      },
    ],
  }
  fetchEntriesMock.mockResolvedValue({
    schema_version: "storage-v2/timetable-list@1",
    available: true,
    semester: "2026-2",
    total: 0,
    entries: [],
  })
  fetchProposalsMock.mockResolvedValue(proposals)
  fetchDetailMock.mockImplementation(async (proposalId) => {
    const listItem = proposals.proposals.find((proposal) => proposal.id === proposalId)
    if (!listItem) {
      throw new Error(`Missing proposal fixture: ${proposalId}`)
    }
    return {
      schema_version: "storage-v2/classification-proposal-detail@1",
      proposal: {
        ...listItem,
        label: listItem.course_name,
        review_reason_code: "schedule_classification_suggested",
      },
      candidate_entry_keys: [`entry-${proposalId}`],
      canonical_metadata_changed: false,
    } satisfies ClassificationProposalDetailPayload
  })
}

describe("useTimetableReview", () => {
  beforeEach(() => {
    fetchEntriesMock.mockReset()
    fetchProposalsMock.mockReset()
    fetchDetailMock.mockReset()
    planConfirmationMock.mockReset()
    applyConfirmationMock.mockReset()
    postStatusMock.mockReset()
  })

  it("loads all active semester entries but only suggested review work", async () => {
    fetchEntriesMock.mockResolvedValue({
      schema_version: "storage-v2/timetable-list@1",
      available: true,
      semester: null,
      total: 0,
      entries: [],
    })
    fetchProposalsMock.mockResolvedValue({
      schema_version: "storage-v2/classification-proposal-list@1",
      available: true,
      counts: { suggested: 0, confirmed: 2, rejected: 1 },
      total: 0,
      proposals: [],
    })

    const { result } = renderHook(() => useTimetableReview(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.entries?.available).toBe(true)
      expect(result.current.proposals?.available).toBe(true)
    })

    expect(fetchEntriesMock).toHaveBeenCalledWith({ limit: 12 })
    expect(fetchProposalsMock).toHaveBeenCalledWith({
      status: "suggested",
      limit: 8,
    })
    expect(fetchDetailMock).not.toHaveBeenCalled()
  })

  it("preserves an exact initial proposal selection even when it is outside the first suggested page", async () => {
    fetchEntriesMock.mockResolvedValue({
      schema_version: "storage-v2/timetable-list@1",
      available: true,
      semester: "2026-2",
      total: 1,
      entries: [],
    })
    fetchProposalsMock.mockResolvedValue({
      schema_version: "storage-v2/classification-proposal-list@1",
      available: true,
      counts: { suggested: 1, confirmed: 0, rejected: 0 },
      total: 1,
      proposals: [
        {
          id: 7,
          storage_key: "20260723_abcd",
          status: "suggested",
          classification_reason: "unique_time_match",
          proposed_title: "2026-07-23 자료구조 5교시",
          context_type: "class_session",
          semester: "2026-2",
          course_name: "자료구조",
          course_code: "CSE201",
          session_date: "2026-07-23",
          weekday: "thu",
          start_time: "13:00",
          end_time: "14:15",
          period_label: "5교시",
          period_index: 5,
          classroom: "E201",
          confidence: 0.75,
          review_status: "open",
          created_at: "2026-07-23T09:00:00+09:00",
          updated_at: "2026-07-23T09:00:00+09:00",
          confirmed_at: null,
        },
      ],
    })
    fetchDetailMock.mockImplementation(async (proposalId) => ({
      schema_version: "storage-v2/classification-proposal-detail@1",
      proposal: {
        id: proposalId,
        storage_key: "20260724_outside_page",
        status: "suggested",
        classification_reason: "unique_time_match",
        proposed_title: "바깥 페이지 제안",
        context_type: "class_session",
        label: "바깥 페이지 제안",
        semester: "2026-2",
        course_name: "자료구조",
        course_code: "CSE201",
        session_date: "2026-07-24",
        weekday: "fri",
        start_time: "13:00",
        end_time: "14:15",
        period_label: "5교시",
        period_index: 5,
        classroom: "E201",
        confidence: 0.88,
        review_status: "open",
        review_reason_code: "schedule_classification_suggested",
        created_at: "2026-07-24T09:00:00+09:00",
        updated_at: "2026-07-24T09:00:00+09:00",
        confirmed_at: null,
      },
      candidate_entry_keys: ["entry-99"],
      canonical_metadata_changed: false,
    }))

    const { result } = renderHook(
      () =>
        useTimetableReview({
          initialSelectedProposalId: 99,
        }),
      {
        wrapper: createWrapper(),
      },
    )

    await waitFor(() => {
      expect(result.current.proposalDetail?.proposal.id).toBe(99)
    })

    expect(result.current.selectedProposalId).toBe(99)
    expect(fetchDetailMock).toHaveBeenCalledWith(99)
  })

  it("loads a confirmation plan and applies exact guard values", async () => {
    fetchEntriesMock.mockResolvedValue({
      schema_version: "storage-v2/timetable-list@1",
      available: true,
      semester: "2026-2",
      total: 1,
      entries: [],
    })
    fetchProposalsMock.mockResolvedValue({
      schema_version: "storage-v2/classification-proposal-list@1",
      available: true,
      counts: { suggested: 1, confirmed: 0, rejected: 0 },
      total: 1,
      proposals: [
        {
          id: 7,
          storage_key: "20260723_abcd",
          status: "suggested",
          classification_reason: "unique_time_match",
          proposed_title: "2026-07-23 자료구조 5교시",
          context_type: "class_session",
          semester: "2026-2",
          course_name: "자료구조",
          course_code: "CSE201",
          session_date: "2026-07-23",
          weekday: "thu",
          start_time: "13:00",
          end_time: "14:15",
          period_label: "5교시",
          period_index: 5,
          classroom: "E201",
          confidence: 0.75,
          review_status: "open",
          created_at: "2026-07-23T09:00:00+09:00",
          updated_at: "2026-07-23T09:00:00+09:00",
          confirmed_at: null,
        },
      ],
    })
    fetchDetailMock.mockResolvedValue({
      schema_version: "storage-v2/classification-proposal-detail@1",
      proposal: {
        id: 7,
        storage_key: "20260723_abcd",
        status: "suggested",
        classification_reason: "unique_time_match",
        proposed_title: "2026-07-23 자료구조 5교시",
        context_type: "class_session",
        label: "자료구조",
        semester: "2026-2",
        course_name: "자료구조",
        course_code: "CSE201",
        session_date: "2026-07-23",
        weekday: "thu",
        start_time: "13:00",
        end_time: "14:15",
        period_label: "5교시",
        period_index: 5,
        classroom: "E201",
        confidence: 0.75,
        review_status: "open",
        review_reason_code: "schedule_classification_suggested",
        created_at: "2026-07-23T09:00:00+09:00",
        updated_at: "2026-07-23T09:00:00+09:00",
        confirmed_at: null,
      },
      candidate_entry_keys: ["entry-1"],
      canonical_metadata_changed: false,
    })
    planConfirmationMock.mockResolvedValue({
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
    })
    applyConfirmationMock.mockResolvedValue({
      schema_version: "storage-v2/classification-confirmation-result@1",
      ok: true,
      action: "confirmed",
      proposal_id: 7,
      status: "confirmed",
      plan_sha256: "a".repeat(64),
      canonical_metadata_changed: false,
    })

    const { result } = renderHook(() => useTimetableReview(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.selectedProposalId).toBe(7)
      expect(result.current.proposalDetail?.proposal.id).toBe(7)
    })

    await result.current.loadConfirmationPlan()

    await waitFor(() => {
      expect(result.current.confirmationPlan?.proposal_id).toBe(7)
    })

    await result.current.applyConfirmationPlan()

    expect(planConfirmationMock).toHaveBeenCalledWith(7)
    expect(applyConfirmationMock).toHaveBeenCalledWith(7, {
      expected_count: 1,
      expected_plan_sha256: "a".repeat(64),
      allow_write: true,
    })
  })

  it("ignores a stale confirmation plan that resolves after selection changes", async () => {
    let resolvePlan: ((value: Awaited<ReturnType<typeof planClassificationConfirmation>>) => void) | null = null

    fetchEntriesMock.mockResolvedValue({
      schema_version: "storage-v2/timetable-list@1",
      available: true,
      semester: "2026-2",
      total: 1,
      entries: [],
    })
    fetchProposalsMock.mockResolvedValue({
      schema_version: "storage-v2/classification-proposal-list@1",
      available: true,
      counts: { suggested: 2, confirmed: 0, rejected: 0 },
      total: 2,
      proposals: [
        {
          id: 7,
          storage_key: "20260723_abcd",
          status: "suggested",
          classification_reason: "unique_time_match",
          proposed_title: "2026-07-23 자료구조 5교시",
          context_type: "class_session",
          semester: "2026-2",
          course_name: "자료구조",
          course_code: "CSE201",
          session_date: "2026-07-23",
          weekday: "thu",
          start_time: "13:00",
          end_time: "14:15",
          period_label: "5교시",
          period_index: 5,
          classroom: "E201",
          confidence: 0.75,
          review_status: "open",
          created_at: "2026-07-23T09:00:00+09:00",
          updated_at: "2026-07-23T09:00:00+09:00",
          confirmed_at: null,
        },
        {
          id: 8,
          storage_key: "20260723_efgh",
          status: "suggested",
          classification_reason: "unique_time_match",
          proposed_title: "2026-07-23 운영체제 2교시",
          context_type: "class_session",
          semester: "2026-2",
          course_name: "운영체제",
          course_code: "CSE301",
          session_date: "2026-07-23",
          weekday: "thu",
          start_time: "10:00",
          end_time: "11:15",
          period_label: "2교시",
          period_index: 2,
          classroom: "E101",
          confidence: 0.66,
          review_status: "open",
          created_at: "2026-07-23T09:10:00+09:00",
          updated_at: "2026-07-23T09:10:00+09:00",
          confirmed_at: null,
        },
      ],
    })
    fetchDetailMock.mockImplementation(async (proposalId) => ({
      schema_version: "storage-v2/classification-proposal-detail@1",
      proposal: {
        id: proposalId,
        storage_key: proposalId === 7 ? "20260723_abcd" : "20260723_efgh",
        status: "suggested",
        classification_reason: "unique_time_match",
        proposed_title:
          proposalId === 7 ? "2026-07-23 자료구조 5교시" : "2026-07-23 운영체제 2교시",
        context_type: "class_session",
        label: proposalId === 7 ? "자료구조" : "운영체제",
        semester: "2026-2",
        course_name: proposalId === 7 ? "자료구조" : "운영체제",
        course_code: proposalId === 7 ? "CSE201" : "CSE301",
        session_date: "2026-07-23",
        weekday: "thu",
        start_time: proposalId === 7 ? "13:00" : "10:00",
        end_time: proposalId === 7 ? "14:15" : "11:15",
        period_label: proposalId === 7 ? "5교시" : "2교시",
        period_index: proposalId === 7 ? 5 : 2,
        classroom: proposalId === 7 ? "E201" : "E101",
        confidence: proposalId === 7 ? 0.75 : 0.66,
        review_status: "open",
        review_reason_code: "schedule_classification_suggested",
        created_at: "2026-07-23T09:00:00+09:00",
        updated_at: "2026-07-23T09:00:00+09:00",
        confirmed_at: null,
      },
      candidate_entry_keys: [`entry-${proposalId}`],
      canonical_metadata_changed: false,
    }))
    planConfirmationMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvePlan = resolve
        }),
    )

    const { result } = renderHook(() => useTimetableReview(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.selectedProposalId).toBe(7)
    })

    const pendingLoad = result.current.loadConfirmationPlan()

    await act(async () => {
      result.current.setSelectedProposalId(8)
    })

    await waitFor(() => {
      expect(result.current.selectedProposalId).toBe(8)
    })

    await act(async () => {
      resolvePlan?.({
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
      })
      await pendingLoad
    })

    expect(result.current.confirmationPlan).toBeNull()
    expect(result.current.actionNotice).toBeNull()
  })

  it("preserves a newer selection when a late reject succeeds", async () => {
    let resolveStatus: ((value: ClassificationStatusResult) => void) | null = null
    mockTwoProposalQueue()
    postStatusMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveStatus = resolve
        }),
    )

    const { result } = renderHook(() => useTimetableReview(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.selectedProposalId).toBe(7)
    })

    let pendingReject: Promise<boolean> | null = null
    act(() => {
      pendingReject = result.current.rejectProposal()
    })
    await act(async () => {
      result.current.setSelectedProposalId(8)
    })
    await waitFor(() => {
      expect(result.current.selectedProposalId).toBe(8)
    })

    await act(async () => {
      resolveStatus?.({
        schema_version: "storage-v2/classification-status-result@1",
        ok: true,
        action: "rejected",
        proposal_id: 7,
        status: "rejected",
        review_status: "dismissed",
        canonical_metadata_changed: false,
      })
      expect(await pendingReject).toBe(true)
    })

    expect(result.current.selectedProposalId).toBe(8)
  })

  it("preserves a newer selection when a late confirmation apply succeeds", async () => {
    let resolveApply: ((value: ClassificationConfirmationResult) => void) | null = null
    mockTwoProposalQueue()
    planConfirmationMock.mockResolvedValue(buildConfirmationPlan())
    applyConfirmationMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveApply = resolve
        }),
    )

    const { result } = renderHook(() => useTimetableReview(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.selectedProposalId).toBe(7)
    })
    await act(async () => {
      await result.current.loadConfirmationPlan()
    })
    await waitFor(() => {
      expect(result.current.confirmationPlan?.proposal_id).toBe(7)
    })

    let pendingApply: Promise<boolean> | null = null
    act(() => {
      pendingApply = result.current.applyConfirmationPlan()
    })
    await act(async () => {
      result.current.setSelectedProposalId(8)
    })
    await waitFor(() => {
      expect(result.current.selectedProposalId).toBe(8)
    })

    await act(async () => {
      resolveApply?.({
        schema_version: "storage-v2/classification-confirmation-result@1",
        ok: true,
        action: "confirmed",
        proposal_id: 7,
        status: "confirmed",
        plan_sha256: "a".repeat(64),
        canonical_metadata_changed: false,
      })
      expect(await pendingApply).toBe(true)
    })

    expect(result.current.selectedProposalId).toBe(8)
  })

  it("returns false instead of surfacing a rejected mutation promise to the UI", async () => {
    fetchEntriesMock.mockResolvedValue({
      schema_version: "storage-v2/timetable-list@1",
      available: true,
      semester: "2026-2",
      total: 0,
      entries: [],
    })
    fetchProposalsMock.mockResolvedValue({
      schema_version: "storage-v2/classification-proposal-list@1",
      available: true,
      counts: { suggested: 1, confirmed: 0, rejected: 0 },
      total: 1,
      proposals: [
        {
          id: 7,
          storage_key: "20260723_abcd",
          status: "suggested",
          classification_reason: "unique_time_match",
          proposed_title: "2026-07-23 자료구조 5교시",
          context_type: "class_session",
          semester: "2026-2",
          course_name: "자료구조",
          course_code: "CSE201",
          session_date: "2026-07-23",
          weekday: "thu",
          start_time: "13:00",
          end_time: "14:15",
          period_label: "5교시",
          period_index: 5,
          classroom: "E201",
          confidence: 0.75,
          review_status: "open",
          created_at: "2026-07-23T09:00:00+09:00",
          updated_at: "2026-07-23T09:00:00+09:00",
          confirmed_at: null,
        },
      ],
    })
    fetchDetailMock.mockResolvedValue({
      schema_version: "storage-v2/classification-proposal-detail@1",
      proposal: {
        id: 7,
        storage_key: "20260723_abcd",
        status: "suggested",
        classification_reason: "unique_time_match",
        proposed_title: "2026-07-23 자료구조 5교시",
        context_type: "class_session",
        label: "자료구조",
        semester: "2026-2",
        course_name: "자료구조",
        course_code: "CSE201",
        session_date: "2026-07-23",
        weekday: "thu",
        start_time: "13:00",
        end_time: "14:15",
        period_label: "5교시",
        period_index: 5,
        classroom: "E201",
        confidence: 0.75,
        review_status: "open",
        review_reason_code: "schedule_classification_suggested",
        created_at: "2026-07-23T09:00:00+09:00",
        updated_at: "2026-07-23T09:00:00+09:00",
        confirmed_at: null,
      },
      candidate_entry_keys: ["entry-1"],
      canonical_metadata_changed: false,
    })
    postStatusMock.mockRejectedValue(new Error("status write disabled"))

    const { result } = renderHook(() => useTimetableReview(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.selectedProposalId).toBe(7)
    })

    await expect(result.current.rejectProposal()).resolves.toBe(false)
    await waitFor(() => {
      expect(result.current.actionError).toBe("status write disabled")
    })
  })

  it("keeps confirmation plan visible and surfaces apply error on request conflict", async () => {
    fetchEntriesMock.mockResolvedValue({
      schema_version: "storage-v2/timetable-list@1",
      available: true,
      semester: "2026-2",
      total: 0,
      entries: [],
    })
    fetchProposalsMock.mockResolvedValue({
      schema_version: "storage-v2/classification-proposal-list@1",
      available: true,
      counts: { suggested: 1, confirmed: 0, rejected: 0 },
      total: 1,
      proposals: [
        {
          id: 7,
          storage_key: "20260723_abcd",
          status: "suggested",
          classification_reason: "unique_time_match",
          proposed_title: "2026-07-23 자료구조 5교시",
          context_type: "class_session",
          semester: "2026-2",
          course_name: "자료구조",
          course_code: "CSE201",
          session_date: "2026-07-23",
          weekday: "thu",
          start_time: "13:00",
          end_time: "14:15",
          period_label: "5교시",
          period_index: 5,
          classroom: "E201",
          confidence: 0.75,
          review_status: "open",
          created_at: "2026-07-23T09:00:00+09:00",
          updated_at: "2026-07-23T09:00:00+09:00",
          confirmed_at: null,
        },
      ],
    })
    fetchDetailMock.mockResolvedValue({
      schema_version: "storage-v2/classification-proposal-detail@1",
      proposal: {
        id: 7,
        storage_key: "20260723_abcd",
        status: "suggested",
        classification_reason: "unique_time_match",
        proposed_title: "2026-07-23 자료구조 5교시",
        context_type: "class_session",
        label: "자료구조",
        semester: "2026-2",
        course_name: "자료구조",
        course_code: "CSE201",
        session_date: "2026-07-23",
        weekday: "thu",
        start_time: "13:00",
        end_time: "14:15",
        period_label: "5교시",
        period_index: 5,
        classroom: "E201",
        confidence: 0.75,
        review_status: "open",
        review_reason_code: "schedule_classification_suggested",
        created_at: "2026-07-23T09:00:00+09:00",
        updated_at: "2026-07-23T09:00:00+09:00",
        confirmed_at: null,
      },
      candidate_entry_keys: ["entry-1"],
      canonical_metadata_changed: false,
    })
    planConfirmationMock.mockResolvedValue({
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
    })
    applyConfirmationMock.mockRejectedValue(new Error("동시 수정 충돌: 제안이 이미 변경되었습니다."))

    const { result } = renderHook(() => useTimetableReview(), {
      wrapper: createWrapper(),
    })

    await waitFor(() => {
      expect(result.current.selectedProposalId).toBe(7)
    })

    await act(async () => {
      await result.current.loadConfirmationPlan()
    })

    await waitFor(() => {
      expect(result.current.confirmationPlan?.proposal_id).toBe(7)
    })

    await act(async () => {
      await result.current.applyConfirmationPlan()
    })

    await waitFor(() => {
      expect(result.current.actionError).toBe("동시 수정 충돌: 제안이 이미 변경되었습니다.")
      expect(result.current.confirmationPlan?.proposal_id).toBe(7)
      expect(result.current.confirmationApplyPending).toBe(false)
    })
  })
})
