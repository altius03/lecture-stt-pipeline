import { describe, expect, it } from "vitest"

import {
  decodeClassificationConfirmationPlan,
  decodeClassificationConfirmationResult,
  decodeClassificationProposalDetailPayload,
  decodeClassificationProposalListPayload,
  decodeClassificationStatusResult,
  decodeTimetableEntriesPayload,
} from "../src/lib/decodeTimetable"

describe("decodeTimetable", () => {
  it("decodes timetable capabilities including status writes", () => {
    const entries = decodeTimetableEntriesPayload({
      schema_version: "storage-v2/timetable-list@1",
      available: true,
      semester: "2026-2",
      total: 1,
      entries: [
        {
          id: 1,
          entry_key: "entry-1",
          semester: "2026-2",
          course_name: "자료구조",
          course_code: "CSE201",
          weekday: "thu",
          start_time: "13:00",
          end_time: "14:15",
          period_label: "5교시",
          period_index: 5,
          classroom: "E201",
        },
      ],
      capabilities: {
        confirmations_enabled: false,
        status_writes_enabled: false,
      },
    })

    expect(entries.capabilities).toEqual({
      confirmations_enabled: false,
      status_writes_enabled: false,
    })
  })

  it("decodes classification payload capabilities including status writes", () => {
    const proposals = decodeClassificationProposalListPayload({
      schema_version: "storage-v2/classification-proposal-list@1",
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
      capabilities: {
        confirmations_enabled: false,
        status_writes_enabled: false,
      },
    })

    expect(proposals.capabilities).toEqual({
      confirmations_enabled: false,
      status_writes_enabled: false,
    })

    const detail = decodeClassificationProposalDetailPayload({
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
      capabilities: {
        confirmations_enabled: false,
        status_writes_enabled: false,
      },
    })

    expect(detail.capabilities).toEqual({
      confirmations_enabled: false,
      status_writes_enabled: false,
    })
  })

  it("decodes status and confirmation action payloads", () => {
    const statusResult = decodeClassificationStatusResult({
      schema_version: "storage-v2/classification-status-result@1",
      ok: true,
      action: "rejected",
      proposal_id: 7,
      status: "rejected",
      review_status: "dismissed",
      canonical_metadata_changed: false,
    })

    expect(statusResult.action).toBe("rejected")

    const plan = decodeClassificationConfirmationPlan({
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

    expect(plan.mode).toBe("read_only")

    const confirmationResult = decodeClassificationConfirmationResult({
      schema_version: "storage-v2/classification-confirmation-result@1",
      ok: true,
      action: "confirmed",
      proposal_id: 7,
      status: "confirmed",
      plan_sha256: "a".repeat(64),
      canonical_metadata_changed: false,
    })

    expect(confirmationResult.status).toBe("confirmed")
  })

  it("rejects confirmation plans that widen the backend audit contract", () => {
    expect(() =>
      decodeClassificationConfirmationPlan({
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
        expected_count: 2,
        materialization: "confirmation_audit_only",
        mode: "read_only",
        plan_sha256: "A".repeat(64),
        canonical_metadata_changed: true,
      }),
    ).toThrow(/classificationConfirmationPlan/)
  })
})
