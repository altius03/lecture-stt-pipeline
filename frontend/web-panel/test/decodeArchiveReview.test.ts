import { describe, expect, it } from "vitest"

import {
  decodeArchiveReviewDetailPayload,
  decodeArchiveReviewListPayload,
  decodeArchiveReviewPromotionPlan,
  decodeArchiveReviewPromotionResult,
  decodeArchiveReviewStatusResult,
} from "../src/lib/decodeArchiveReview"

import type {
  ArchiveReviewCanonicalSuggestion,
  ArchiveReviewRevision,
} from "../src/types"

const baseCase = {
  case_key: "CASE-001",
  legacy_delivery_key: "legacy-CASE-001",
  logical_stem: "SAMPLE-STEM",
  subject_abbr: "STAT",
  review_status: "open" as const,
  promoted_recording_id: 11,
  promoted_storage_key: "20260723_abcd",
  created_at: "2026-07-23T09:00:00+09:00",
  updated_at: "2026-07-23T09:00:00+09:00",
  resolved_at: null,
}

const baseRevision = (override: Partial<ArchiveReviewRevision> = {}): ArchiveReviewRevision => ({
  revision_id: 10,
  artifact_kind: "correction_text",
  content_sha256: "a".repeat(64),
  bytes: 1024,
  mime_type: "text/plain",
  created_at: "2026-07-23T09:00:00+09:00",
  observation_count: 1,
  capture_count: 1,
  last_observed_at: null,
  matches_ledger_count: 1,
  differs_from_ledger_count: 0,
  unclaimed_count: 0,
  unexpected_current_count: 0,
  source_roles: ["current"],
  relationships: ["candidate"],
  ...override,
})

const buildSuggestion = (
  override: Partial<ArchiveReviewCanonicalSuggestion> = {},
): ArchiveReviewCanonicalSuggestion => ({
  artifact_kind: "correction_text",
  status: "suggested",
  revision_id: 10,
  content_sha256: "a".repeat(64),
  reason_code: "unique_matches_ledger",
  candidate_revision_ids: [10],
  ...override,
})

function makeListPayload(caseOverride: Partial<typeof baseCase> = {}) {
  return {
    schema_version: "storage-v2/archive-review-list@1" as const,
    available: true,
    filters: {
      review_status: null,
      limit: 20,
      offset: 0,
    },
    counts: {
      open: 1,
      triaged: 0,
      resolved: 0,
      dismissed: 0,
    },
    total: 1,
    cases: [
      {
        ...baseCase,
        ...caseOverride,
      },
    ],
    capabilities: {
      status_writes_enabled: true,
      promotions_enabled: false,
    },
  }
}

function makeDetailPayload(
  overrides: {
    caseDetail?: Record<string, unknown>
    revisions?: ArchiveReviewRevision[]
    canonicalSuggestions?: ArchiveReviewCanonicalSuggestion[]
    confirmedSelections?: unknown[]
  } = {},
) {
  return {
    schema_version: "storage-v2/archive-review-detail@1" as const,
    case: {
      ...baseCase,
      ...(overrides.caseDetail ?? {}),
    },
    latest_capture: {
      capture_key: "capture-001",
      reconciliation_classification: "candidate",
      captured_at: "2026-07-23T08:00:00+09:00",
    },
    capture_count: 3,
    captures_truncated: false,
    revision_count: 2,
    captures: [
      {
        capture_key: "capture-001",
        reconciliation_classification: "candidate",
        captured_at: "2026-07-23T08:00:00+09:00",
      },
    ],
    revisions: overrides.revisions ?? [
      baseRevision(),
      baseRevision({
        revision_id: 11,
        artifact_kind: "correction_json",
        content_sha256: "b".repeat(64),
        bytes: 2048,
      }),
    ],
    canonical_suggestions: overrides.canonicalSuggestions ?? [
      buildSuggestion(),
      buildSuggestion({
        artifact_kind: "correction_json",
        status: "unresolved",
        revision_id: null,
        content_sha256: null,
        reason_code: "multiple_ledger_matches",
        candidate_revision_ids: [11],
      }),
    ],
    confirmed_selections: overrides.confirmedSelections ?? [
      {
        artifact_kind: "summary_markdown",
        revision_id: 12,
        content_sha256: "c".repeat(64),
        bytes: 512,
        mime_type: "text/markdown",
        promotion_plan_sha256: "d".repeat(64),
        confirmed_at: "2026-07-23T09:10:00+09:00",
      },
    ],
  }
}

describe("decodeArchiveReview", () => {
  it("decodes valid list/detail/status/plan/result payloads", () => {
    const list = decodeArchiveReviewListPayload(makeListPayload({
      capture_count: 3,
      revision_count: 2,
      case_key: "CASE-001",
      logical_stem: "SAMPLE-STEM",
      subject_abbr: "STAT",
      review_status: "open",
      promoted_recording_id: 11,
      promoted_storage_key: "20260723_abcd",
      created_at: "2026-07-23T09:00:00+09:00",
      updated_at: "2026-07-23T09:00:00+09:00",
      resolved_at: null,
      last_captured_at: "2026-07-23T09:00:00+09:00",
      latest_classification: null,
    }))
    expect(list.available).toBe(true)
    expect(list.capabilities).toEqual({
      status_writes_enabled: true,
      promotions_enabled: false,
    })

    const detail = decodeArchiveReviewDetailPayload(
      makeDetailPayload({
        revisions: [
          baseRevision({ revision_id: 10 }),
          baseRevision({
            revision_id: 11,
            artifact_kind: "correction_json",
            content_sha256: "b".repeat(64),
          }),
          baseRevision({
            revision_id: 12,
            artifact_kind: "summary_markdown",
            content_sha256: "c".repeat(64),
          }),
        ],
        canonicalSuggestions: [
          buildSuggestion(),
          buildSuggestion({
            artifact_kind: "correction_json",
            status: "unresolved",
            revision_id: null,
            content_sha256: null,
            reason_code: "unique_latest_capture_current",
            candidate_revision_ids: [11],
          }),
          buildSuggestion({
            artifact_kind: "summary_markdown",
            status: "unresolved",
            revision_id: null,
            content_sha256: null,
            reason_code: "multiple_latest_capture_current",
            candidate_revision_ids: [12],
          }),
        ],
        confirmedSelections: [
          {
            artifact_kind: "correction_text",
            revision_id: 10,
            content_sha256: "a".repeat(64),
            bytes: 1024,
            mime_type: "text/plain",
            promotion_plan_sha256: "d".repeat(64),
            confirmed_at: "2026-07-23T09:10:00+09:00",
          },
        ],
      }),
    )
    expect(detail.canonical_suggestions).toHaveLength(3)
    expect(detail.confirmed_selections).toHaveLength(1)

    const status = decodeArchiveReviewStatusResult({
      schema_version: "storage-v2/archive-review-status@1",
      ok: true,
      case_key: "CASE-001",
      review_status: "triaged",
      changed: true,
    })
    expect(status.changed).toBe(true)

    const plan = decodeArchiveReviewPromotionPlan({
      schema_version: "storage-v2/archive-review-promotion-plan@1",
      case_key: "CASE-001",
      case_updated_at: "2026-07-23T09:00:00+09:00",
      review_status: "open",
      target_recording_id: 15,
      target_storage_key: "20260723_abc",
      latest_capture_key: "capture-001",
      latest_capture_classification: "candidate",
      selected_revisions: [
        {
          artifact_kind: "correction_text",
          revision_id: 10,
          content_sha256: "a".repeat(64),
          bytes: 1024,
          mime_type: "text/plain",
        },
      ],
      expected_count: 1,
      mode: "read_only",
      plan_sha256: "e".repeat(64),
    })
    expect(plan.mode).toBe("read_only")

    const result = decodeArchiveReviewPromotionResult({
      schema_version: "storage-v2/archive-review-promotion-result@1",
      ok: true,
      status: "promoted",
      case_key: "CASE-001",
      target_storage_key: "20260723_abc",
      plan_sha256: "e".repeat(64),
      selected_count: 1,
    })
    expect(result.status).toBe("promoted")
  })

  it("rejects non-integer or negative ids, counts, bytes, and bounds", () => {
    expect(() =>
      decodeArchiveReviewListPayload(
        makeListPayload({
          capture_count: 3,
          revision_count: -1,
        }) as never,
      ),
    ).toThrow()

    expect(() =>
      decodeArchiveReviewDetailPayload(
        makeDetailPayload({
          revisions: [
            baseRevision({
              revision_id: 10.5,
            }),
            baseRevision({ revision_id: 11 }),
          ],
        }),
      ),
    ).toThrow()

    expect(() =>
      decodeArchiveReviewDetailPayload(
        makeDetailPayload({
          revisions: [
            baseRevision({
              bytes: -16,
            }),
            baseRevision({
              revision_id: 11,
              artifact_kind: "correction_json",
              content_sha256: "b".repeat(64),
            }),
          ],
        }),
      ),
    ).toThrow()

    expect(() =>
      decodeArchiveReviewListPayload(
        makeListPayload({
          case_key: "INVALID KEY",
        }),
      ),
    ).toThrow()

    expect(() =>
      decodeArchiveReviewListPayload({
        ...makeListPayload(),
        filters: {
          ...makeListPayload().filters,
          limit: 101,
        },
      }),
    ).toThrow()
  })

  it("rejects malformed suggestion and confirmed selection cross-lane references", () => {
    expect(() =>
      decodeArchiveReviewDetailPayload(
        makeDetailPayload({
          revisions: [
            baseRevision({ revision_id: 10 }),
            baseRevision({ revision_id: 11, artifact_kind: "correction_json", content_sha256: "b".repeat(64) }),
          ],
          canonicalSuggestions: [
            buildSuggestion({
              artifact_kind: "summary_markdown",
              status: "suggested",
              revision_id: 10,
              content_sha256: "a".repeat(64),
              reason_code: "no_unique_current_candidate",
              candidate_revision_ids: [10],
            }),
          ],
        }),
      ),
    ).toThrow(/lane이 없습니다/)

    expect(() =>
      decodeArchiveReviewDetailPayload(
        makeDetailPayload({
          revisions: [
            baseRevision({ revision_id: 10 }),
            baseRevision({ revision_id: 11, artifact_kind: "correction_json", content_sha256: "b".repeat(64) }),
            baseRevision({ revision_id: 12, artifact_kind: "summary_markdown", content_sha256: "c".repeat(64) }),
          ],
          canonicalSuggestions: [
            buildSuggestion({
              status: "suggested",
              revision_id: 99,
              content_sha256: "a".repeat(64),
              candidate_revision_ids: [99],
            }),
          ],
        }),
      ),
    ).toThrow(/candidate revision이 lane에 없습니다|suggested revision이 candidate 목록에 없습니다|suggested revision이 lane에 없습니다/)

    expect(() =>
      decodeArchiveReviewDetailPayload(
        makeDetailPayload({
          revisions: [
            baseRevision({ revision_id: 10 }),
            baseRevision({ revision_id: 11, artifact_kind: "correction_json", content_sha256: "b".repeat(64) }),
            baseRevision({ revision_id: 12, artifact_kind: "summary_markdown", content_sha256: "c".repeat(64) }),
          ],
          canonicalSuggestions: [
            buildSuggestion({
              status: "unresolved",
              revision_id: 12,
              content_sha256: "c".repeat(64),
              reason_code: "multiple_ledger_matches",
            }),
            buildSuggestion({
              artifact_kind: "correction_json",
              status: "unresolved",
              revision_id: null,
              content_sha256: null,
              reason_code: "multiple_ledger_matches",
              candidate_revision_ids: [9],
            }),
          ],
        }),
      ),
    ).toThrow(/unresolved 항목은 revision digest를 포함할 수 없습니다|candidate revision이 lane에 없습니다/)

    expect(() =>
      decodeArchiveReviewDetailPayload(
        makeDetailPayload({
          revisions: [
            baseRevision({ revision_id: 10 }),
            baseRevision({ revision_id: 11, artifact_kind: "correction_json", content_sha256: "b".repeat(64) }),
            baseRevision({ revision_id: 12, artifact_kind: "summary_markdown", content_sha256: "c".repeat(64) }),
          ],
          confirmedSelections: [
            {
              artifact_kind: "correction_text",
              revision_id: 11,
              content_sha256: "b".repeat(64),
              bytes: 1024,
              mime_type: null,
              promotion_plan_sha256: "e".repeat(64),
              confirmed_at: "2026-07-23T09:10:00+09:00",
            },
          ],
        }),
      ),
    ).toThrow(/confirmed_selections\[\] revision이 artifact lane에 없습니다/)
  })

  it("rejects mismatched plan result shape and selected_count digest checks", () => {
    expect(() =>
      decodeArchiveReviewPromotionPlan({
        schema_version: "storage-v2/archive-review-promotion-plan@1",
        case_key: "CASE-001",
        case_updated_at: "2026-07-23T09:00:00+09:00",
        review_status: "open",
        target_recording_id: -1,
        target_storage_key: "20260723_abc",
        latest_capture_key: "capture-001",
        latest_capture_classification: "candidate",
        selected_revisions: [],
        expected_count: 2,
        mode: "read_only",
        plan_sha256: "g".repeat(64),
      }),
    ).toThrow()

    expect(() =>
      decodeArchiveReviewPromotionResult({
        schema_version: "storage-v2/archive-review-promotion-result@1",
        ok: true,
        status: "promoted",
        case_key: "CASE-001",
        target_storage_key: "20260723_abc",
        plan_sha256: "invalid",
        selected_count: 1,
      }),
    ).toThrow()

    expect(() =>
      decodeArchiveReviewPromotionResult({
        schema_version: "storage-v2/archive-review-promotion-result@1",
        ok: true,
        status: "promoted",
        case_key: "CASE-001",
        target_storage_key: "20260723_abc",
        plan_sha256: "g".repeat(64),
        selected_count: -1,
      }),
    ).toThrow()
  })
})
