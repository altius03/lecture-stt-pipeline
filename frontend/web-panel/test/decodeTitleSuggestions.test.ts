import { describe, expect, it } from "vitest"

import {
  decodeTitleSuggestionConfirmationPlan,
  decodeTitleSuggestionConfirmationResult,
  decodeTitleSuggestionDetail,
  decodeTitleSuggestionList,
  decodeTitleSuggestionStatusResult,
} from "../src/lib/decodeTitleSuggestions"
import type {
  TitleSuggestionDetailPayload,
  TitleSuggestionListPayload,
} from "../src/types"
import {
  buildDisabledTitleSuggestionListPayload,
  buildTitleSuggestionConfirmationPlan,
  buildTitleSuggestionConfirmationResult,
  buildTitleSuggestionDetailPayload,
  buildTitleSuggestionListPayload,
  buildTitleSuggestionStatusResult,
} from "./titleSuggestionFixtures"

function cloneList(): TitleSuggestionListPayload {
  return structuredClone(buildTitleSuggestionListPayload())
}

function cloneDetail(): TitleSuggestionDetailPayload {
  return structuredClone(buildTitleSuggestionDetailPayload())
}

describe("decodeTitleSuggestions", () => {
  it("accepts the closed list, detail, plan, and result contracts", () => {
    const list = buildTitleSuggestionListPayload()
    const detail = buildTitleSuggestionDetailPayload()
    const plan = buildTitleSuggestionConfirmationPlan()
    const statusResult = buildTitleSuggestionStatusResult()
    const confirmationResult = buildTitleSuggestionConfirmationResult()

    expect(decodeTitleSuggestionList(list)).toEqual(list)
    expect(decodeTitleSuggestionDetail(detail)).toEqual(detail)
    expect(decodeTitleSuggestionConfirmationPlan(plan)).toEqual(plan)
    expect(decodeTitleSuggestionStatusResult(statusResult)).toEqual(statusResult)
    expect(decodeTitleSuggestionConfirmationResult(confirmationResult)).toEqual(
      confirmationResult,
    )
  })

  it("accepts a disabled empty list only with write capabilities off", () => {
    const payload = buildDisabledTitleSuggestionListPayload()
    expect(decodeTitleSuggestionList(payload)).toEqual(payload)

    payload.capabilities.status_writes_enabled = true
    expect(() => decodeTitleSuggestionList(payload)).toThrow(
      /쓰기 capability/,
    )
  })

  it("rejects list extras, duplicate ids, and inconsistent totals", () => {
    const extra = {
      ...buildTitleSuggestionListPayload(),
      filters: { limit: 8 },
    }
    expect(() => decodeTitleSuggestionList(extra)).toThrow(
      /허용되지 않은 필드/,
    )

    const duplicate = cloneList()
    duplicate.proposals.push(structuredClone(duplicate.proposals[0]))
    duplicate.total = 2
    duplicate.counts.suggested = 2
    expect(() => decodeTitleSuggestionList(duplicate)).toThrow(/id가 중복/)

    const wrongTotal = cloneList()
    wrongTotal.total = 2
    expect(() => decodeTitleSuggestionList(wrongTotal)).toThrow(/count\/total/)
  })

  it("requires nullable proposal and linked-classification fields to be present", () => {
    const missingProposalField = cloneDetail()
    delete (
      missingProposalField.proposal as unknown as Record<string, unknown>
    ).classification_status
    expect(() => decodeTitleSuggestionDetail(missingProposalField)).toThrow(
      /필수 필드/,
    )

    const missingLinkedField = cloneDetail()
    delete (
      missingLinkedField.linked_classification as unknown as Record<
        string,
        unknown
      >
    ).course_name
    expect(() => decodeTitleSuggestionDetail(missingLinkedField)).toThrow(
      /필수 필드/,
    )
  })

  it("keeps creation-time classification separate from the linked current status", () => {
    const detail = cloneDetail()
    detail.proposal.classification_status = "suggested"
    if (detail.linked_classification) {
      detail.linked_classification.status = "rejected"
    }

    expect(decodeTitleSuggestionDetail(detail)).toEqual(detail)
  })

  it("enforces content-topic and schedule-linked classification boundaries", () => {
    const contentTopic = cloneDetail()
    contentTopic.proposal.suggestion_reason = "content_topic"
    contentTopic.proposal.classification_status = null
    contentTopic.linked_classification = null
    expect(decodeTitleSuggestionDetail(contentTopic)).toEqual(contentTopic)

    const contentWithLink = cloneDetail()
    contentWithLink.proposal.suggestion_reason = "content_topic"
    contentWithLink.proposal.classification_status = null
    expect(() => decodeTitleSuggestionDetail(contentWithLink)).toThrow(
      /제안 사유/,
    )

    const scheduleWithoutLink = cloneDetail()
    scheduleWithoutLink.linked_classification = null
    expect(() => decodeTitleSuggestionDetail(scheduleWithoutLink)).toThrow(
      /제안 사유/,
    )
  })

  it("rejects lifecycle, NFC, and canonical-metadata violations", () => {
    const lifecycle = cloneList()
    lifecycle.proposals[0].review_status = "resolved"
    expect(() => decodeTitleSuggestionList(lifecycle)).toThrow(/lifecycle/)

    const nonNfc = cloneList()
    nonNfc.proposals[0].proposed_title = "강의"
    expect(() => decodeTitleSuggestionList(nonNfc)).toThrow(/NFC 정규화/)

    const canonicalChanged = cloneList()
    canonicalChanged.proposals[0].canonical_metadata_changed = true as false
    expect(() => decodeTitleSuggestionList(canonicalChanged)).toThrow(
      /literal/,
    )
  })

  it("rejects mismatched linked titles and malformed plan digests", () => {
    const detail = cloneDetail()
    if (detail.linked_classification) {
      detail.linked_classification.proposed_title = "다른 제목"
    }
    expect(() => decodeTitleSuggestionDetail(detail)).toThrow(/제안 metadata/)

    const plan = buildTitleSuggestionConfirmationPlan()
    plan.plan_sha256 = "A".repeat(64)
    expect(() => decodeTitleSuggestionConfirmationPlan(plan)).toThrow(
      /소문자 sha256/,
    )
  })
})
