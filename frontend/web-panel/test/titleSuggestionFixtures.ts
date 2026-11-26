import type {
  TitleSuggestionConfirmationPlan,
  TitleSuggestionConfirmationResult,
  TitleSuggestionDetailPayload,
  TitleSuggestionListPayload,
  TitleSuggestionStatusResult,
} from "../src/types"

export function buildTitleSuggestionListPayload(): TitleSuggestionListPayload {
  return {
    schema_version: "storage-v2/title-suggestion-list@1",
    available: true,
    counts: {
      suggested: 1,
      confirmed: 0,
      rejected: 0,
    },
    total: 1,
    proposals: [
      {
        id: 11,
        storage_key: "rec_shared",
        status: "suggested",
        suggestion_reason: "schedule_content_match",
        proposed_title: "2026-07-24 자료구조 5교시",
        confidence: 0.86,
        generator_version: "deterministic-keywords-v1",
        review_status: "open",
        transcript_revision: 2,
        classification_status: "suggested",
        context_type: "class_session",
        created_at: "2026-07-24T13:00:00+09:00",
        updated_at: "2026-07-24T13:05:00+09:00",
        confirmed_at: null,
        canonical_metadata_changed: false,
      },
    ],
    capabilities: {
      confirmations_enabled: false,
      status_writes_enabled: false,
    },
  }
}

export function buildDisabledTitleSuggestionListPayload(): TitleSuggestionListPayload {
  return {
    schema_version: "storage-v2/title-suggestion-list@1",
    available: false,
    disabled_reason: "title_review_disabled",
    counts: {
      suggested: 0,
      confirmed: 0,
      rejected: 0,
    },
    total: 0,
    proposals: [],
    capabilities: {
      confirmations_enabled: false,
      status_writes_enabled: false,
    },
  }
}

export function buildTitleSuggestionDetailPayload(
  proposalId = 11,
): TitleSuggestionDetailPayload {
  const listProposal = buildTitleSuggestionListPayload().proposals[0]
  const {
    canonical_metadata_changed: canonicalMetadataChanged,
    ...proposal
  } = listProposal
  if (canonicalMetadataChanged !== false) {
    throw new Error("Fixture requires metadata-only title suggestions.")
  }
  return {
    schema_version: "storage-v2/title-suggestion-detail@1",
    proposal: {
      ...proposal,
      id: proposalId,
    },
    linked_classification: {
      status: "suggested",
      classification_reason: "unique_time_match",
      proposed_title: listProposal.proposed_title,
      course_name: "자료구조",
      course_code: "CSE201",
    },
    canonical_metadata_changed: false,
    capabilities: {
      confirmations_enabled: false,
      status_writes_enabled: false,
    },
  }
}

export function buildTitleSuggestionConfirmationPlan(
  proposalId = 11,
): TitleSuggestionConfirmationPlan {
  return {
    schema_version: "storage-v2/title-suggestion-confirmation-plan@1",
    proposal_id: proposalId,
    storage_key: "rec_shared",
    suggestion_reason: "schedule_content_match",
    proposed_title: "2026-07-24 자료구조 5교시",
    transcript_revision: 2,
    expected_count: 1,
    materialization: "confirmation_audit_only",
    mode: "read_only",
    plan_sha256: "a".repeat(64),
    canonical_metadata_changed: false,
  }
}

export function buildTitleSuggestionStatusResult(
  proposalId = 11,
): TitleSuggestionStatusResult {
  return {
    schema_version: "storage-v2/title-suggestion-status-result@1",
    ok: true,
    action: "rejected",
    proposal_id: proposalId,
    status: "rejected",
    review_status: "dismissed",
    canonical_metadata_changed: false,
  }
}

export function buildTitleSuggestionConfirmationResult(
  proposalId = 11,
): TitleSuggestionConfirmationResult {
  return {
    schema_version: "storage-v2/title-suggestion-confirmation-result@1",
    ok: true,
    action: "confirmed",
    proposal_id: proposalId,
    status: "confirmed",
    plan_sha256: "a".repeat(64),
    canonical_metadata_changed: false,
  }
}
