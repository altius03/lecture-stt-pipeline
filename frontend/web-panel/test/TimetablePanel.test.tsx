import { fireEvent, render, screen, within } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { TimetablePanel } from "../src/components/panel/TimetablePanel"

const setSelectedProposalId = vi.fn()
const rejectProposal = vi.fn(async () => {})
const loadConfirmationPlan = vi.fn(async () => {})
const applyConfirmationPlan = vi.fn(async () => {})
const hookState = {
  entries: {
    schema_version: "storage-v2/timetable-list@1" as const,
    available: true,
    semester: null,
    total: 2,
    entries: [
      {
        id: 1,
        entry_key: "entry-1",
        semester: "2026-2",
        course_name: "자료구조",
        course_code: "CSE201",
        weekday: "thu" as const,
        start_time: "13:00",
        end_time: "14:15",
        period_label: "5교시",
        period_index: 5,
        classroom: "E201",
      },
      {
        id: 2,
        entry_key: "entry-2",
        semester: "2026-1",
        course_name: "운영체제",
        course_code: "CSE301",
        weekday: "tue" as const,
        start_time: "10:00",
        end_time: "11:15",
        period_label: "2교시",
        period_index: 2,
        classroom: "E101",
      },
    ],
    capabilities: {
      confirmations_enabled: false,
      status_writes_enabled: false,
    },
  },
  entriesLoading: false,
  entriesError: null,
  proposals: {
    schema_version: "storage-v2/classification-proposal-list@1" as const,
    available: true,
    counts: {
      suggested: 1,
      confirmed: 0,
      rejected: 0,
    },
    total: 1,
    proposals: [
      {
        id: 7,
        storage_key: "20260723_abcd",
        status: "suggested" as const,
        classification_reason: "unique_time_match" as const,
        proposed_title: "2026-07-23 자료구조 5교시",
        context_type: "class_session",
        semester: "2026-2",
        course_name: "자료구조",
        course_code: "CSE201",
        session_date: "2026-07-23",
        weekday: "thu" as const,
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
    capabilities: {
      confirmations_enabled: false,
      status_writes_enabled: false,
    },
  },
  proposalsLoading: false,
  proposalsError: null,
  selectedProposalId: 7,
  setSelectedProposalId,
  proposalDetail: {
    schema_version: "storage-v2/classification-proposal-detail@1" as const,
    proposal: {
      id: 7,
      storage_key: "20260723_abcd",
      status: "suggested" as const,
      classification_reason: "unique_time_match" as const,
      proposed_title: "2026-07-23 자료구조 5교시",
      context_type: "class_session",
      label: "자료구조",
      semester: "2026-2",
      course_name: "자료구조",
      course_code: "CSE201",
      session_date: "2026-07-23",
      weekday: "thu" as const,
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
    capabilities: {
      confirmations_enabled: false,
      status_writes_enabled: false,
    },
  },
  proposalDetailLoading: false,
  proposalDetailError: null,
  confirmationPlan: {
    schema_version: "storage-v2/classification-confirmation-plan@1" as const,
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
    materialization: "confirmation_audit_only" as const,
    mode: "read_only" as const,
    plan_sha256: "a".repeat(64),
    canonical_metadata_changed: false,
  },
  actionError: null,
  actionNotice: "확정 계획을 불러왔습니다. 내용을 확인한 뒤 명시적으로 적용하세요.",
  clearTransientState: vi.fn(),
  rejectProposal,
  loadConfirmationPlan,
  applyConfirmationPlan,
  rejectPending: false,
  confirmationPlanPending: false,
  confirmationApplyPending: false,
}

vi.mock("../src/hooks/useTimetableReview", () => ({
  useTimetableReview: () => hookState,
}))

describe("TimetablePanel", () => {
  beforeEach(() => {
    setSelectedProposalId.mockClear()
    rejectProposal.mockClear()
    loadConfirmationPlan.mockClear()
    applyConfirmationPlan.mockClear()
    hookState.selectedProposalId = 7
    hookState.entries.capabilities = {
      confirmations_enabled: false,
      status_writes_enabled: false,
    }
    hookState.proposals.capabilities = {
      confirmations_enabled: false,
      status_writes_enabled: false,
    }
    hookState.proposals.proposals = [
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
    ]
    hookState.proposals.total = 1
    hookState.proposals.counts = {
      suggested: 1,
      confirmed: 0,
      rejected: 0,
    }
    hookState.proposalDetail.capabilities = {
      confirmations_enabled: false,
      status_writes_enabled: false,
    }
    hookState.proposalDetail.proposal = {
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
    }
  })

  it("renders review actions with guarded confirmation copy", () => {
    render(<TimetablePanel />)

    expect(screen.getByText("정본 이름과 manifest는 이 단계에서 바꾸지 않습니다. 현재 승격은 confirmation audit만 남깁니다.")).toBeTruthy()
    expect(screen.getByText("canonical_metadata_changed=false")).toBeTruthy()
    expect(screen.getByRole("button", { name: "확정 계획 보기" })).toBeTruthy()
    expect(screen.getByRole("button", { name: "제안 보류" }).hasAttribute("disabled")).toBe(true)
    expect(screen.getByRole("button", { name: "이 계획으로 확정" }).hasAttribute("disabled")).toBe(true)
    expect(screen.getByText("confirmations_enabled=false 이므로 계획 확인까지만 가능합니다.")).toBeTruthy()
    expect(screen.getByText("status_writes_enabled=false 이므로 보류 처리는 현재 비활성화되어 있습니다.")).toBeTruthy()
  })

  it("renders timetable entries and selected classification detail", () => {
    render(<TimetablePanel />)

    expect(
      screen.getByRole("heading", { name: "학기별 활성 시간표" }),
    ).toBeTruthy()
    expect(screen.getAllByText("2026-2").length).toBeGreaterThan(0)
    expect(screen.getByText("2026-1")).toBeTruthy()
    expect(screen.getAllByText("자료구조").length).toBeGreaterThan(0)
    expect(screen.getByText("CSE201")).toBeTruthy()
    expect(screen.getAllByText("13:00–14:15").length).toBeGreaterThan(0)

    const reviewCard = screen.getByRole("heading", { name: "시간표 분류 검토 큐" }).closest("article")
    expect(reviewCard).not.toBeNull()

    const scope = within(reviewCard as HTMLElement)
    expect(scope.getByText("검토 대기 1건")).toBeTruthy()
    expect(scope.getAllByText("2026-07-23 자료구조 5교시").length).toBeGreaterThan(0)
    expect(scope.getByText("유일 시간 일치")).toBeTruthy()
    expect(scope.getByText("75%")).toBeTruthy()
  })

  it("opens explicit reject confirmation and calls the action", () => {
    hookState.proposalDetail.capabilities = {
      confirmations_enabled: false,
      status_writes_enabled: true,
    }
    render(<TimetablePanel />)

    fireEvent.click(screen.getByRole("button", { name: "제안 보류" }))
    fireEvent.click(screen.getByRole("button", { name: "보류 처리 실행" }))

    expect(rejectProposal).toHaveBeenCalledTimes(1)
  })

  it("uses a native proposal selector and closes armed reject confirmation on selection", () => {
    hookState.proposalDetail.capabilities = {
      confirmations_enabled: false,
      status_writes_enabled: true,
    }
    hookState.proposals.proposals = [
      hookState.proposals.proposals[0],
      {
        ...hookState.proposals.proposals[0],
        id: 8,
        storage_key: "20260723_efgh",
        proposed_title: "2026-07-23 운영체제 2교시",
      },
    ]

    const { rerender } = render(<TimetablePanel />)

    fireEvent.click(screen.getByRole("button", { name: "제안 보류" }))
    expect(screen.getByRole("button", { name: "보류 처리 실행" })).toBeTruthy()

    const nextProposalButton = screen.getByRole("button", {
      name: /2026-07-23 운영체제 2교시/,
    })
    expect(nextProposalButton.getAttribute("aria-pressed")).toBe("false")
    fireEvent.click(nextProposalButton)
    expect(setSelectedProposalId).toHaveBeenCalledWith(8)

    hookState.selectedProposalId = 8
    hookState.proposalDetail = {
      ...hookState.proposalDetail,
      proposal: {
        ...hookState.proposalDetail.proposal,
        id: 8,
        storage_key: "20260723_efgh",
        proposed_title: "2026-07-23 운영체제 2교시",
      },
    }
    rerender(<TimetablePanel />)

    expect(
      screen
        .getByRole("button", { name: /2026-07-23 운영체제 2교시/ })
        .getAttribute("aria-pressed"),
    ).toBe("true")
    expect(screen.queryByRole("button", { name: "보류 처리 실행" })).toBeNull()
  })

  it("keeps reject confirmation open when reject action fails", async () => {
    hookState.proposalDetail.capabilities = {
      confirmations_enabled: true,
      status_writes_enabled: true,
    }
    rejectProposal.mockImplementation(async () => false)

    render(<TimetablePanel />)

    fireEvent.click(screen.getByRole("button", { name: "제안 보류" }))
    fireEvent.click(screen.getByRole("button", { name: "보류 처리 실행" }))

    expect(rejectProposal).toHaveBeenCalledTimes(1)
    expect(screen.getByRole("group", { name: "제안 보류 확인" })).toBeTruthy()
  })

  it("renders confirmation digest preview and guard/metadata visibility", () => {
    hookState.proposalDetail.capabilities = {
      confirmations_enabled: true,
      status_writes_enabled: true,
    }
    render(<TimetablePanel />)

    expect(screen.getByRole("group", { name: "확정 계획" })).toBeTruthy()
    expect(screen.getByText("canonical_metadata_changed=false")).toBeTruthy()
    expect(screen.getByText("Mode")).toBeTruthy()
    expect(screen.getByText("Materialization")).toBeTruthy()
    expect(screen.getByText("Expected Count")).toBeTruthy()
    expect(screen.getByTitle("a".repeat(64))).toBeTruthy()
    expect(
      screen.getByRole("button", { name: "이 계획으로 확정" }),
    ).toBeTruthy()
  })
})
