import type {
  ArchiveReviewArtifactKind,
  ArchiveReviewCapabilities,
  ArchiveReviewCanonicalSuggestion,
  ArchiveReviewCaseDetail,
  ArchiveReviewCaseListItem,
  ArchiveReviewCapture,
  ArchiveReviewConfirmedSelection,
  ArchiveReviewDetailPayload,
  ArchiveReviewListPayload,
  ArchiveReviewPromotionPlan,
  ArchiveReviewPromotionPlanSelection,
  ArchiveReviewPromotionResult,
  ArchiveReviewRevision,
  ArchiveReviewStatus,
  ArchiveReviewStatusResult,
} from "../types"

const REVIEW_STATUSES = ["open", "triaged", "resolved", "dismissed"] as const
const ARTIFACT_KINDS = [
  "correction_text",
  "correction_json",
  "summary_markdown",
] as const
const SUGGESTION_STATUSES = ["suggested", "unresolved"] as const
const SUGGESTION_REASON_CODES = [
  "unique_matches_ledger",
  "multiple_ledger_matches",
  "unique_latest_capture_current",
  "multiple_latest_capture_current",
  "no_unique_current_candidate",
] as const
const CASE_KEY_PATTERN = /^[0-9A-Za-z_-]+$/

function asRecord(input: unknown, label: string): Record<string, unknown> {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`)
  }
  return input as Record<string, unknown>
}

function readString(record: Record<string, unknown>, key: string, label: string): string {
  const value = record[key]
  if (typeof value !== "string") {
    throw new Error(`${label}.${key} 값이 문자열이 아닙니다.`)
  }
  return value
}

function readOptionalString(record: Record<string, unknown>, key: string, label: string): string | null {
  const value = record[key]
  if (value === undefined || value === null) {
    return null
  }
  if (typeof value !== "string") {
    throw new Error(`${label}.${key} 값이 문자열이 아닙니다.`)
  }
  return value
}

function readNumber(record: Record<string, unknown>, key: string, label: string): number {
  const value = record[key]
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${label}.${key} 값이 숫자가 아닙니다.`)
  }
  return value
}

function readInteger(record: Record<string, unknown>, key: string, label: string): number {
  const value = readNumber(record, key, label)
  if (!Number.isInteger(value)) {
    throw new Error(`${label}.${key} 값이 정수가 아닙니다.`)
  }
  return value
}

function readNonNegativeInteger(record: Record<string, unknown>, key: string, label: string): number {
  const value = readInteger(record, key, label)
  if (value < 0) {
    throw new Error(`${label}.${key} 값이 0 이상의 정수가 아닙니다.`)
  }
  return value
}

function readPositiveInteger(record: Record<string, unknown>, key: string, label: string): number {
  const value = readInteger(record, key, label)
  if (value <= 0) {
    throw new Error(`${label}.${key} 값이 양의 정수가 아닙니다.`)
  }
  return value
}

function readBoundedInteger(
  record: Record<string, unknown>,
  key: string,
  label: string,
  bounds: {
  min: number
  max: number
  },
): number {
  const { min, max } = bounds
  const value = readInteger(record, key, label)
  if (value < min || value > max) {
    throw new Error(`${label}.${key} 값이 ${min} 이상 ${max} 이하 정수가 아닙니다.`)
  }
  return value
}

function readBoolean(record: Record<string, unknown>, key: string, label: string): boolean {
  const value = record[key]
  if (typeof value !== "boolean") {
    throw new Error(`${label}.${key} 값이 불리언이 아닙니다.`)
  }
  return value
}

function readArray(record: Record<string, unknown>, key: string, label: string): unknown[] {
  const value = record[key]
  if (!Array.isArray(value)) {
    throw new Error(`${label}.${key} 값이 배열이 아닙니다.`)
  }
  return value
}

function readLiteral<T extends string | boolean | number>(
  record: Record<string, unknown>,
  key: string,
  allowed: readonly T[],
  label: string,
): T {
  const value = record[key]
  if (!allowed.includes(value as T)) {
    throw new Error(`${label}.${key} 값이 예상한 literal이 아닙니다.`)
  }
  return value as T
}

function readOptionalLiteral<T extends string>(
  record: Record<string, unknown>,
  key: string,
  allowed: readonly T[],
  label: string,
): T | null {
  const value = record[key]
  if (value === undefined || value === null) {
    return null
  }
  if (!allowed.includes(value as T)) {
    throw new Error(`${label}.${key} 값이 예상한 literal이 아닙니다.`)
  }
  return value as T
}

function readSha256Digest(record: Record<string, unknown>, key: string, label: string): string {
  const value = readString(record, key, label)
  if (!/^[a-f0-9]{64}$/.test(value)) {
    throw new Error(`${label}.${key} 값이 소문자 sha256 digest가 아닙니다.`)
  }
  return value
}

function readCaseKey(record: Record<string, unknown>, key: string, label: string): string {
  const value = readString(record, key, label)
  if (!CASE_KEY_PATTERN.test(value)) {
    throw new Error(`${label}.${key} 값이 ASCII case_key 형식이 아닙니다.`)
  }
  return value
}

function readStringArray(record: Record<string, unknown>, key: string, label: string): string[] {
  return readArray(record, key, label).map((value) => {
    if (typeof value !== "string") {
      throw new Error(`${label}.${key} 값이 문자열 배열이 아닙니다.`)
    }
    return value
  })
}

function readNumberArray(record: Record<string, unknown>, key: string, label: string): number[] {
  return readArray(record, key, label).map((value) => {
    if (typeof value !== "number" || !Number.isInteger(value) || value <= 0) {
      throw new Error(`${label}.${key} 값이 양의 정수 배열이 아닙니다.`)
    }
    return value
  })
}

function decodeCapabilities(input: unknown, label: string): ArchiveReviewCapabilities | undefined {
  if (input === undefined) {
    return undefined
  }
  const record = asRecord(input, label)
  return {
    status_writes_enabled: readBoolean(record, "status_writes_enabled", label),
    promotions_enabled: readBoolean(record, "promotions_enabled", label),
  }
}

function decodeCaseListItem(input: unknown): ArchiveReviewCaseListItem {
  const record = asRecord(input, "cases[]")
  return {
    case_key: readCaseKey(record, "case_key", "cases[]"),
    logical_stem: readString(record, "logical_stem", "cases[]"),
    subject_abbr: readOptionalString(record, "subject_abbr", "cases[]"),
    review_status: readLiteral(record, "review_status", REVIEW_STATUSES, "cases[]"),
    promoted_recording_id:
      record.promoted_recording_id === null || record.promoted_recording_id === undefined
        ? null
        : readPositiveInteger(record, "promoted_recording_id", "cases[]"),
    promoted_storage_key: readOptionalString(record, "promoted_storage_key", "cases[]"),
    created_at: readString(record, "created_at", "cases[]"),
    updated_at: readString(record, "updated_at", "cases[]"),
    resolved_at: readOptionalString(record, "resolved_at", "cases[]"),
    capture_count: readNonNegativeInteger(record, "capture_count", "cases[]"),
    revision_count: readNonNegativeInteger(record, "revision_count", "cases[]"),
    last_captured_at: readOptionalString(record, "last_captured_at", "cases[]"),
    latest_classification: readOptionalString(record, "latest_classification", "cases[]"),
  }
}

function decodeCaseDetail(input: unknown): ArchiveReviewCaseDetail {
  const record = asRecord(input, "archiveReview.case")
  return {
    case_key: readCaseKey(record, "case_key", "archiveReview.case"),
    legacy_delivery_key: readString(record, "legacy_delivery_key", "archiveReview.case"),
    logical_stem: readString(record, "logical_stem", "archiveReview.case"),
    subject_abbr: readOptionalString(record, "subject_abbr", "archiveReview.case"),
    review_status: readLiteral(record, "review_status", REVIEW_STATUSES, "archiveReview.case"),
    promoted_recording_id:
      record.promoted_recording_id === null || record.promoted_recording_id === undefined
        ? null
        : readPositiveInteger(record, "promoted_recording_id", "archiveReview.case"),
    promoted_storage_key: readOptionalString(record, "promoted_storage_key", "archiveReview.case"),
    created_at: readString(record, "created_at", "archiveReview.case"),
    updated_at: readString(record, "updated_at", "archiveReview.case"),
    resolved_at: readOptionalString(record, "resolved_at", "archiveReview.case"),
  }
}

function decodeCapture(input: unknown, label: string): ArchiveReviewCapture {
  const record = asRecord(input, label)
  return {
    capture_key: readString(record, "capture_key", label),
    reconciliation_classification: readString(record, "reconciliation_classification", label),
    captured_at: readString(record, "captured_at", label),
  }
}

function decodeRevision(input: unknown): ArchiveReviewRevision {
  const record = asRecord(input, "archiveReview.revisions[]")
  return {
    revision_id: readPositiveInteger(record, "revision_id", "archiveReview.revisions[]"),
    artifact_kind: readLiteral(record, "artifact_kind", ARTIFACT_KINDS, "archiveReview.revisions[]"),
    content_sha256: readSha256Digest(record, "content_sha256", "archiveReview.revisions[]"),
    bytes: readNonNegativeInteger(record, "bytes", "archiveReview.revisions[]"),
    mime_type: readOptionalString(record, "mime_type", "archiveReview.revisions[]"),
    created_at: readString(record, "created_at", "archiveReview.revisions[]"),
    observation_count: readNonNegativeInteger(record, "observation_count", "archiveReview.revisions[]"),
    capture_count: readNonNegativeInteger(record, "capture_count", "archiveReview.revisions[]"),
    last_observed_at: readOptionalString(record, "last_observed_at", "archiveReview.revisions[]"),
    matches_ledger_count: readNonNegativeInteger(record, "matches_ledger_count", "archiveReview.revisions[]"),
    differs_from_ledger_count: readNonNegativeInteger(record, "differs_from_ledger_count", "archiveReview.revisions[]"),
    unclaimed_count: readNonNegativeInteger(record, "unclaimed_count", "archiveReview.revisions[]"),
    unexpected_current_count: readNonNegativeInteger(record, "unexpected_current_count", "archiveReview.revisions[]"),
    source_roles: readStringArray(record, "source_roles", "archiveReview.revisions[]"),
    relationships: readStringArray(record, "relationships", "archiveReview.revisions[]"),
  }
}

function decodeSuggestion(input: unknown): ArchiveReviewCanonicalSuggestion {
  const record = asRecord(input, "archiveReview.canonical_suggestions[]")
  return {
    artifact_kind: readLiteral(
      record,
      "artifact_kind",
      ARTIFACT_KINDS,
      "archiveReview.canonical_suggestions[]",
    ),
    status: readLiteral(
      record,
      "status",
      SUGGESTION_STATUSES,
      "archiveReview.canonical_suggestions[]",
    ),
    revision_id:
      record.revision_id === null || record.revision_id === undefined
        ? null
        : readPositiveInteger(record, "revision_id", "archiveReview.canonical_suggestions[]"),
    content_sha256:
      record.content_sha256 === null || record.content_sha256 === undefined
        ? null
        : readSha256Digest(record, "content_sha256", "archiveReview.canonical_suggestions[]"),
    reason_code: readLiteral(
      record,
      "reason_code",
      SUGGESTION_REASON_CODES,
      "archiveReview.canonical_suggestions[]",
    ),
    candidate_revision_ids: readNumberArray(
      record,
      "candidate_revision_ids",
      "archiveReview.canonical_suggestions[]",
    ),
  }
}

function decodeConfirmedSelection(input: unknown): ArchiveReviewConfirmedSelection {
  const record = asRecord(input, "archiveReview.confirmed_selections[]")
  return {
    artifact_kind: readLiteral(
      record,
      "artifact_kind",
      ARTIFACT_KINDS,
      "archiveReview.confirmed_selections[]",
    ),
    revision_id: readPositiveInteger(record, "revision_id", "archiveReview.confirmed_selections[]"),
    content_sha256: readSha256Digest(record, "content_sha256", "archiveReview.confirmed_selections[]"),
    bytes: readNonNegativeInteger(record, "bytes", "archiveReview.confirmed_selections[]"),
    mime_type: readOptionalString(record, "mime_type", "archiveReview.confirmed_selections[]"),
    promotion_plan_sha256: readSha256Digest(
      record,
      "promotion_plan_sha256",
      "archiveReview.confirmed_selections[]",
    ),
    confirmed_at: readString(record, "confirmed_at", "archiveReview.confirmed_selections[]"),
  }
}

function decodePromotionPlanSelection(input: unknown): ArchiveReviewPromotionPlanSelection {
  const record = asRecord(input, "archiveReviewPromotionPlan.selected_revisions[]")
  return {
    artifact_kind: readLiteral(
      record,
      "artifact_kind",
      ARTIFACT_KINDS,
      "archiveReviewPromotionPlan.selected_revisions[]",
    ),
    revision_id: readPositiveInteger(record, "revision_id", "archiveReviewPromotionPlan.selected_revisions[]"),
    content_sha256: readSha256Digest(
      record,
      "content_sha256",
      "archiveReviewPromotionPlan.selected_revisions[]",
    ),
    bytes: readNonNegativeInteger(record, "bytes", "archiveReviewPromotionPlan.selected_revisions[]"),
    mime_type: readOptionalString(record, "mime_type", "archiveReviewPromotionPlan.selected_revisions[]"),
  }
}

export function decodeArchiveReviewListPayload(input: unknown): ArchiveReviewListPayload {
  const record = asRecord(input, "archiveReviewList")
  const filtersRecord = asRecord(record.filters, "archiveReviewList.filters")
  const countsRecord = asRecord(record.counts, "archiveReviewList.counts")
  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/archive-review-list@1"],
      "archiveReviewList",
    ),
    available: readBoolean(record, "available", "archiveReviewList"),
    disabled_reason: readOptionalString(record, "disabled_reason", "archiveReviewList") ?? undefined,
    filters: {
      review_status: readOptionalLiteral(
        filtersRecord,
        "review_status",
        REVIEW_STATUSES,
        "archiveReviewList.filters",
      ) as ArchiveReviewStatus | null,
      limit: readBoundedInteger(filtersRecord, "limit", "archiveReviewList.filters", { min: 0, max: 100 }),
      offset: readBoundedInteger(filtersRecord, "offset", "archiveReviewList.filters", { min: 0, max: 100000 }),
    },
    counts: {
      open: readNonNegativeInteger(countsRecord, "open", "archiveReviewList.counts"),
      triaged: readNonNegativeInteger(countsRecord, "triaged", "archiveReviewList.counts"),
      resolved: readNonNegativeInteger(countsRecord, "resolved", "archiveReviewList.counts"),
      dismissed: readNonNegativeInteger(countsRecord, "dismissed", "archiveReviewList.counts"),
    },
    total: readNonNegativeInteger(record, "total", "archiveReviewList"),
    cases: readArray(record, "cases", "archiveReviewList").map(decodeCaseListItem),
    capabilities: decodeCapabilities(record.capabilities, "archiveReviewList.capabilities"),
  }
}

export function decodeArchiveReviewDetailPayload(input: unknown): ArchiveReviewDetailPayload {
  const record = asRecord(input, "archiveReview")
  const revisions = readArray(record, "revisions", "archiveReview").map(decodeRevision)
  const revisionLanes = new Map<ArchiveReviewArtifactKind, Set<number>>()
  const revisionDigests = new Map<number, string>()
  for (const revision of revisions) {
    if (!revisionLanes.has(revision.artifact_kind)) {
      revisionLanes.set(revision.artifact_kind, new Set<number>())
    }
    revisionLanes.get(revision.artifact_kind)?.add(revision.revision_id)
    revisionDigests.set(revision.revision_id, revision.content_sha256)
  }
  const canonicalSuggestions = readArray(
    record,
    "canonical_suggestions",
    "archiveReview",
  ).map(decodeSuggestion)
  for (const suggestion of canonicalSuggestions) {
    const lane = revisionLanes.get(suggestion.artifact_kind)
    if (!lane) {
      throw new Error(`archiveReview.canonical_suggestions[] ${suggestion.artifact_kind} lane이 없습니다.`)
    }
    for (const candidateRevisionId of suggestion.candidate_revision_ids) {
      if (!lane.has(candidateRevisionId)) {
        throw new Error(`archiveReview.canonical_suggestions[] candidate revision이 lane에 없습니다.`)
      }
    }
    if (suggestion.status === "suggested") {
      if (suggestion.revision_id === null || suggestion.content_sha256 === null) {
        throw new Error("archiveReview.canonical_suggestions[] suggested 항목이 revision을 포함하지 않습니다.")
      }
      if (!lane.has(suggestion.revision_id)) {
        throw new Error("archiveReview.canonical_suggestions[] suggested revision이 lane에 없습니다.")
      }
      if (!suggestion.candidate_revision_ids.includes(suggestion.revision_id)) {
        throw new Error("archiveReview.canonical_suggestions[] suggested revision이 candidate 목록에 없습니다.")
      }
      if (revisionDigests.get(suggestion.revision_id) !== suggestion.content_sha256) {
        throw new Error("archiveReview.canonical_suggestions[] suggested digest가 revision metadata와 다릅니다.")
      }
    } else if (suggestion.revision_id !== null || suggestion.content_sha256 !== null) {
      throw new Error("archiveReview.canonical_suggestions[] unresolved 항목은 revision digest를 포함할 수 없습니다.")
    }
  }
  const confirmedSelections = readArray(
    record,
    "confirmed_selections",
    "archiveReview",
  ).map(decodeConfirmedSelection)
  for (const selection of confirmedSelections) {
    const lane = revisionLanes.get(selection.artifact_kind)
    if (!lane?.has(selection.revision_id)) {
      throw new Error("archiveReview.confirmed_selections[] revision이 artifact lane에 없습니다.")
    }
    if (revisionDigests.get(selection.revision_id) !== selection.content_sha256) {
      throw new Error("archiveReview.confirmed_selections[] digest가 revision metadata와 다릅니다.")
    }
  }
  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/archive-review-detail@1"],
      "archiveReview",
    ),
    case: decodeCaseDetail(record.case),
    latest_capture:
      record.latest_capture === null || record.latest_capture === undefined
        ? null
        : decodeCapture(record.latest_capture, "archiveReview.latest_capture"),
    capture_count: readNonNegativeInteger(record, "capture_count", "archiveReview"),
    captures_truncated: readBoolean(record, "captures_truncated", "archiveReview"),
    revision_count: readNonNegativeInteger(record, "revision_count", "archiveReview"),
    captures: readArray(record, "captures", "archiveReview").map((item) =>
      decodeCapture(item, "archiveReview.captures[]"),
    ),
    revisions,
    canonical_suggestions: canonicalSuggestions,
    confirmed_selections: confirmedSelections,
    capabilities: decodeCapabilities(record.capabilities, "archiveReview.capabilities"),
  }
}

export function decodeArchiveReviewStatusResult(input: unknown): ArchiveReviewStatusResult {
  const record = asRecord(input, "archiveReviewStatus")
  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/archive-review-status@1"],
      "archiveReviewStatus",
    ),
    ok: readLiteral(record, "ok", [true] as const, "archiveReviewStatus"),
    case_key: readCaseKey(record, "case_key", "archiveReviewStatus"),
    review_status: readLiteral(record, "review_status", REVIEW_STATUSES, "archiveReviewStatus"),
    changed: readBoolean(record, "changed", "archiveReviewStatus"),
  }
}

export function decodeArchiveReviewPromotionPlan(input: unknown): ArchiveReviewPromotionPlan {
  const record = asRecord(input, "archiveReviewPromotionPlan")
  const selectedRevisions = readArray(
    record,
    "selected_revisions",
    "archiveReviewPromotionPlan",
  ).map(decodePromotionPlanSelection)
  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/archive-review-promotion-plan@1"],
      "archiveReviewPromotionPlan",
    ),
    case_key: readCaseKey(record, "case_key", "archiveReviewPromotionPlan"),
    case_updated_at: readString(record, "case_updated_at", "archiveReviewPromotionPlan"),
    review_status: readLiteral(
      record,
      "review_status",
      REVIEW_STATUSES,
      "archiveReviewPromotionPlan",
    ),
    target_recording_id: readPositiveInteger(record, "target_recording_id", "archiveReviewPromotionPlan"),
    target_storage_key: readString(record, "target_storage_key", "archiveReviewPromotionPlan"),
    latest_capture_key: readString(record, "latest_capture_key", "archiveReviewPromotionPlan"),
    latest_capture_classification: readString(
      record,
      "latest_capture_classification",
      "archiveReviewPromotionPlan",
    ),
    selected_revisions: selectedRevisions,
    expected_count: readLiteral(record, "expected_count", [1] as const, "archiveReviewPromotionPlan"),
    mode: readLiteral(record, "mode", ["read_only"], "archiveReviewPromotionPlan"),
    plan_sha256: readSha256Digest(record, "plan_sha256", "archiveReviewPromotionPlan"),
  }
}

export function decodeArchiveReviewPromotionResult(input: unknown): ArchiveReviewPromotionResult {
  const record = asRecord(input, "archiveReviewPromotionResult")
  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/archive-review-promotion-result@1"],
      "archiveReviewPromotionResult",
    ),
    ok: readLiteral(record, "ok", [true] as const, "archiveReviewPromotionResult"),
    status: readLiteral(
      record,
      "status",
      ["promoted", "skipped"],
      "archiveReviewPromotionResult",
    ),
    case_key: readCaseKey(record, "case_key", "archiveReviewPromotionResult"),
    target_storage_key: readString(record, "target_storage_key", "archiveReviewPromotionResult"),
    plan_sha256: readSha256Digest(record, "plan_sha256", "archiveReviewPromotionResult"),
    selected_count: readNonNegativeInteger(record, "selected_count", "archiveReviewPromotionResult"),
  }
}

export const ARCHIVE_REVIEW_STATUSES = REVIEW_STATUSES satisfies readonly ArchiveReviewStatus[]
export const ARCHIVE_REVIEW_ARTIFACT_KINDS =
  ARTIFACT_KINDS satisfies readonly ArchiveReviewArtifactKind[]
