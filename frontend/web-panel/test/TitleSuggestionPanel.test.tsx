import { fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { TitleSuggestionPanel } from "../src/components/panel/TitleSuggestionPanel"
import { useTitleSuggestionReview } from "../src/hooks/useTitleSuggestionReview"
import {
  buildDisabledTitleSuggestionListPayload,
  buildTitleSuggestionConfirmationPlan,
  buildTitleSuggestionDetailPayload,
  buildTitleSuggestionListPayload,
} from "./titleSuggestionFixtures"

vi.mock("../src/hooks/useTitleSuggestionReview", () => ({
  useTitleSuggestionReview: vi.fn(),
}))

const useTitleSuggestionReviewMock = vi.mocked(useTitleSuggestionReview)
const rejectProposal = vi.fn(async () => true)
const loadConfirmationPlan = vi.fn(async () => true)
const applyConfirmationPlan = vi.fn(async () => true)
const originalMatchMedia = window.matchMedia

function installMatchMedia(matches: boolean) {
  const mediaQuery = {
    matches,
    media: "(max-width: 900px)",
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(() => true),
  } satisfies MediaQueryList
  const matchMedia = vi.fn(() => mediaQuery)
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: matchMedia,
  })
  return { matchMedia, mediaQuery }
}

function expectBefore(first: Element, second: Element): void {
  expect(
    first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).not.toBe(0)
}

function buildHookState(): ReturnType<typeof useTitleSuggestionReview> {
  return {
    proposals: buildTitleSuggestionListPayload(),
    proposalsLoading: false,
    proposalsError: null,
    selectedProposalId: 11,
    setSelectedProposalId: vi.fn(),
    proposalDetail: buildTitleSuggestionDetailPayload(),
    proposalDetailLoading: false,
    proposalDetailError: null,
    confirmationPlan: buildTitleSuggestionConfirmationPlan(),
    actionError: null,
    actionNotice:
      "확정 계획을 불러왔습니다. 정본 이름은 바꾸지 않습니다.",
    clearTransientState: vi.fn(),
    rejectProposal,
    loadConfirmationPlan,
    applyConfirmationPlan,
    rejectPending: false,
    confirmationPlanPending: false,
    confirmationApplyPending: false,
  }
}

describe("TitleSuggestionPanel", () => {
  beforeEach(() => {
    rejectProposal.mockClear()
    loadConfirmationPlan.mockClear()
    applyConfirmationPlan.mockClear()
    useTitleSuggestionReviewMock.mockReset()
    useTitleSuggestionReviewMock.mockReturnValue(buildHookState())
  })

  afterEach(() => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: originalMatchMedia,
    })
  })

  it("renders native deep links, creation/current classification, and audit-only guards", () => {
    render(<TitleSuggestionPanel initialSelectedProposalId={11} />)

    expect(
      screen.getByRole("link", {
        name: /2026-07-24 자료구조 5교시/,
      }).getAttribute("href"),
    ).toBe("#review/title/11")
    expect(screen.getAllByText("86%").length).toBeGreaterThan(0)
    expect(screen.getByText("제안 당시 분류 상태")).toBeTruthy()
    expect(screen.getByText("현재 분류 상태")).toBeTruthy()
    expect(
      screen.getByText(
        "제안 확인 상태만 기록합니다. current title, storage_key, manifest는 변경하지 않습니다.",
      ),
    ).toBeTruthy()
    expect(screen.getByText("canonical_metadata_changed=false")).toBeTruthy()
    expect(
      screen.getByRole("button", { name: "확정 계획 보기" }).hasAttribute(
        "disabled",
      ),
    ).toBe(false)
    expect(
      screen.getByRole("button", {
        name: "검토 확정 — 정본 이름 미변경",
      }).hasAttribute("disabled"),
    ).toBe(true)
    expect(
      screen.getByRole("button", { name: "제안 보류" }).hasAttribute(
        "disabled",
      ),
    ).toBe(true)
    expect(useTitleSuggestionReviewMock).toHaveBeenCalledWith({
      initialSelectedProposalId: 11,
    })

    const queueCard = screen
      .getByRole("heading", { name: "내용 기반 제목 제안", level: 2 })
      .closest("article")
    const workbenchCard = screen
      .getByRole("heading", {
        name: "2026-07-24 자료구조 5교시",
        level: 2,
      })
      .closest("article")
    expect(queueCard).not.toBeNull()
    expect(workbenchCard).not.toBeNull()
    expectBefore(queueCard as HTMLElement, workbenchCard as HTMLElement)
  })

  it("places the detail workbench before the eight-item queue at 900px and below", () => {
    const { matchMedia } = installMatchMedia(true)
    render(<TitleSuggestionPanel initialSelectedProposalId={11} />)

    const queueCard = screen
      .getByRole("heading", { name: "내용 기반 제목 제안", level: 2 })
      .closest("article")
    const workbenchCard = screen
      .getByRole("heading", {
        name: "2026-07-24 자료구조 5교시",
        level: 2,
      })
      .closest("article")
    expect(queueCard).not.toBeNull()
    expect(workbenchCard).not.toBeNull()
    expectBefore(workbenchCard as HTMLElement, queueCard as HTMLElement)
    expect(matchMedia).toHaveBeenCalledWith("(max-width: 900px)")
  })

  it("requires an explicit second click before rejecting", () => {
    const state = buildHookState()
    if (state.proposals) {
      state.proposals.capabilities.status_writes_enabled = true
    }
    if (state.proposalDetail) {
      state.proposalDetail.capabilities.status_writes_enabled = true
    }
    useTitleSuggestionReviewMock.mockReturnValue(state)
    render(<TitleSuggestionPanel />)

    fireEvent.click(screen.getByRole("button", { name: "제안 보류" }))
    expect(rejectProposal).not.toHaveBeenCalled()
    expect(
      screen.getByText(
        "제안은 rejected, review는 dismissed로 기록됩니다. 정본 이름은 변경하지 않습니다.",
      ),
    ).toBeTruthy()

    fireEvent.click(screen.getByRole("button", { name: "보류 처리 실행" }))
    expect(rejectProposal).toHaveBeenCalledTimes(1)
  })

  it("shows explicit disabled queue and workbench states without detail content", () => {
    const state = buildHookState()
    state.proposals = buildDisabledTitleSuggestionListPayload()
    state.proposalDetail = null
    state.confirmationPlan = null
    useTitleSuggestionReviewMock.mockReturnValue(state)
    render(<TitleSuggestionPanel initialSelectedProposalId={11} />)

    expect(
      screen.getByText(
        "제목 제안 검토 API가 비활성화되어 있습니다. 상세 및 쓰기 요청은 보내지 않습니다.",
      ),
    ).toBeTruthy()
    expect(
      screen.getByText(
        "제목 제안 API가 비활성화되어 상세 workbench를 열지 않았습니다.",
      ),
    ).toBeTruthy()
    expect(screen.queryByText("Plan Digest")).toBeNull()
  })

  it("distinguishes an unavailable Storage v2 DB from a disabled API", () => {
    const state = buildHookState()
    state.proposals = buildDisabledTitleSuggestionListPayload()
    state.proposals.disabled_reason = "storage_v2_db_unavailable"
    state.proposalDetail = null
    state.confirmationPlan = null
    useTitleSuggestionReviewMock.mockReturnValue(state)
    render(<TitleSuggestionPanel initialSelectedProposalId={11} />)

    expect(
      screen.getByText(
        "Storage v2 DB를 사용할 수 없어 제목 제안 목록을 열지 못했습니다. 상세 및 쓰기 요청은 보내지 않습니다.",
      ),
    ).toBeTruthy()
    expect(
      screen.getByText(
        "Storage v2 DB를 사용할 수 없어 제목 제안 상세 workbench를 열지 않았습니다.",
      ),
    ).toBeTruthy()
    expect(
      screen.queryByText(
        "제목 제안 검토 API가 비활성화되어 있습니다. 상세 및 쓰기 요청은 보내지 않습니다.",
      ),
    ).toBeNull()
  })

  it("uses safe generic copy for any other closed unavailable reason", () => {
    const state = buildHookState()
    state.proposals = buildDisabledTitleSuggestionListPayload()
    state.proposals.disabled_reason = "future_closed_reason"
    state.proposalDetail = null
    state.confirmationPlan = null
    useTitleSuggestionReviewMock.mockReturnValue(state)
    render(<TitleSuggestionPanel initialSelectedProposalId={11} />)

    expect(
      screen.getByText(
        "제목 제안 검토 정보를 사용할 수 없습니다. 상세 및 쓰기 요청은 보내지 않습니다.",
      ),
    ).toBeTruthy()
    expect(
      screen.getByText("제목 제안 상세 workbench를 열 수 없습니다."),
    ).toBeTruthy()
    expect(screen.queryByText("future_closed_reason")).toBeNull()
  })

  it("uses fresh detail title for a deep link outside the current list page", () => {
    const state = buildHookState()
    state.selectedProposalId = 99
    state.proposalDetail = buildTitleSuggestionDetailPayload(99)
    state.proposalDetail.proposal.proposed_title = "페이지 밖 한국어 제목"
    if (state.proposalDetail.linked_classification) {
      state.proposalDetail.linked_classification.proposed_title =
        "페이지 밖 한국어 제목"
    }
    state.confirmationPlan = null
    useTitleSuggestionReviewMock.mockReturnValue(state)
    render(<TitleSuggestionPanel initialSelectedProposalId={99} />)

    expect(
      screen.getByRole("heading", { name: "페이지 밖 한국어 제목", level: 2 }),
    ).toBeTruthy()
    expect(
      screen.getByText(
        "deep-link 제안은 현재 8건 목록 밖에 있어도 metadata-only detail로 유지합니다.",
      ),
    ).toBeTruthy()
  })

  it("explains lifecycle restrictions without misreporting enabled writes", () => {
    const state = buildHookState()
    if (state.proposals) {
      state.proposals.capabilities.status_writes_enabled = true
    }
    if (state.proposalDetail) {
      state.proposalDetail.capabilities.status_writes_enabled = true
      state.proposalDetail.proposal.status = "confirmed"
      state.proposalDetail.proposal.review_status = "resolved"
      state.proposalDetail.proposal.confirmed_at =
        "2026-07-24T14:00:00+09:00"
    }
    state.confirmationPlan = null
    useTitleSuggestionReviewMock.mockReturnValue(state)
    render(<TitleSuggestionPanel />)

    expect(
      screen.getByText(
        "검토 대기 lifecycle을 벗어난 제안은 보류 처리할 수 없습니다.",
      ),
    ).toBeTruthy()
    expect(
      screen.queryByText(
        "status_writes_enabled=false 이므로 보류 처리는 비활성화되어 있습니다.",
      ),
    ).toBeNull()
  })

  it("applies a visible digest only when confirmation capability is enabled", () => {
    const state = buildHookState()
    if (state.proposals) {
      state.proposals.capabilities.confirmations_enabled = true
    }
    if (state.proposalDetail) {
      state.proposalDetail.capabilities.confirmations_enabled = true
    }
    useTitleSuggestionReviewMock.mockReturnValue(state)
    render(<TitleSuggestionPanel />)

    expect(screen.getByText("a".repeat(64))).toBeTruthy()
    fireEvent.click(
      screen.getByRole("button", {
        name: "검토 확정 — 정본 이름 미변경",
      }),
    )
    expect(applyConfirmationPlan).toHaveBeenCalledTimes(1)
  })
})
