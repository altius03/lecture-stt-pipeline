import type {
  TitleSuggestionCapabilities,
  TitleSuggestionConfirmationPlan,
  TitleSuggestionConfirmationResult,
  TitleSuggestionDetailPayload,
  TitleSuggestionLinkedClassification,
  TitleSuggestionListItem,
  TitleSuggestionListPayload,
  TitleSuggestionProposal,
  TitleSuggestionStatusResult,
} from "../types"

const TITLE_SUGGESTION_STATUSES = [
  "suggested",
  "confirmed",
  "rejected",
] as const
const TITLE_SUGGESTION_REASONS = [
  "schedule_content_match",
  "content_topic",
] as const
const TITLE_SUGGESTION_CONTEXT_TYPES = [
  "general",
  "class_session",
  "daily_note",
  "meeting",
  "memo",
] as const
const REVIEW_STATUSES = [
  "open",
  "triaged",
  "resolved",
  "dismissed",
] as const
const STORAGE_KEY_PATTERN = /^[0-9A-Za-z_-]+$/
const SHA256_DIGEST_PATTERN = /^[a-f0-9]{64}$/
const GENERATOR_VERSION = "deterministic-keywords-v1"
const MAX_TITLE_LENGTH = 512
const MAX_STORAGE_KEY_LENGTH = 512
const MAX_DISABLED_REASON_LENGTH = 256
const MAX_TIMESTAMP_LENGTH = 64

function asRecord(input: unknown, label: string): Record<string, unknown> {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`)
  }
  return input as Record<string, unknown>
}

function assertExactKeys(
  record: Record<string, unknown>,
  allowedKeys: readonly string[],
  label: string,
): void {
  const extras = Object.keys(record).filter((key) => !allowedKeys.includes(key))
  if (extras.length > 0) {
    throw new Error(`${label}에 허용되지 않은 필드가 있습니다: ${extras.join(", ")}`)
  }
}

function assertRequiredKeys(
  record: Record<string, unknown>,
  requiredKeys: readonly string[],
  label: string,
): void {
  const missing = requiredKeys.filter(
    (key) => !Object.prototype.hasOwnProperty.call(record, key),
  )
  if (missing.length > 0) {
    throw new Error(`${label}에 필수 필드가 없습니다: ${missing.join(", ")}`)
  }
}

function readArray(record: Record<string, unknown>, key: string, label: string): unknown[] {
  const value = record[key]
  if (!Array.isArray(value)) {
    throw new Error(`${label}.${key} 값이 배열이 아닙니다.`)
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

function readString(
  record: Record<string, unknown>,
  key: string,
  label: string,
  options?: {
    allowEmpty?: boolean
    maxLength?: number
  },
): string {
  const value = record[key]
  const allowEmpty = options?.allowEmpty ?? false
  const maxLength = options?.maxLength ?? 2048
  if (
    typeof value !== "string"
    || value.length > maxLength
    || (!allowEmpty && value.length === 0)
  ) {
    throw new Error(`${label}.${key} 값이 올바른 문자열이 아닙니다.`)
  }
  return value
}

function readNfcString(
  record: Record<string, unknown>,
  key: string,
  label: string,
  options?: {
    allowEmpty?: boolean
    maxLength?: number
  },
): string {
  const value = readString(record, key, label, options)
  if (value !== value.normalize("NFC")) {
    throw new Error(`${label}.${key} 값이 NFC 정규화와 일치하지 않습니다.`)
  }
  return value
}

function readOptionalNfcString(
  record: Record<string, unknown>,
  key: string,
  label: string,
  options?: { maxLength?: number },
): string | null {
  if (record[key] === null || record[key] === undefined) {
    return null
  }
  return readNfcString(record, key, label, options)
}

function readSafeInteger(
  record: Record<string, unknown>,
  key: string,
  label: string,
  options?: { min?: number; max?: number },
): number {
  const value = record[key]
  const min = options?.min ?? Number.MIN_SAFE_INTEGER
  const max = options?.max ?? Number.MAX_SAFE_INTEGER
  if (
    typeof value !== "number"
    || !Number.isSafeInteger(value)
    || value < min
    || value > max
  ) {
    throw new Error(`${label}.${key} 값이 ${min} 이상 ${max} 이하의 안전한 정수가 아닙니다.`)
  }
  return value
}

function readConfidence(record: Record<string, unknown>, key: string, label: string): number {
  const value = record[key]
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) {
    throw new Error(`${label}.${key} 값이 0 이상 1 이하의 유한한 숫자가 아닙니다.`)
  }
  return value
}

function readTimestamp(record: Record<string, unknown>, key: string, label: string): string {
  const value = readString(record, key, label, { maxLength: MAX_TIMESTAMP_LENGTH })
  if (!Number.isFinite(Date.parse(value))) {
    throw new Error(`${label}.${key} 값이 해석 가능한 시각이 아닙니다.`)
  }
  return value
}

function readOptionalTimestamp(
  record: Record<string, unknown>,
  key: string,
  label: string,
): string | null {
  if (record[key] === null || record[key] === undefined) {
    return null
  }
  return readTimestamp(record, key, label)
}

function readStorageKey(record: Record<string, unknown>, key: string, label: string): string {
  const value = readString(record, key, label, { maxLength: MAX_STORAGE_KEY_LENGTH })
  if (!STORAGE_KEY_PATTERN.test(value)) {
    throw new Error(`${label}.${key} 값이 canonical storage key 형식이 아닙니다.`)
  }
  return value
}

function readSha256Digest(record: Record<string, unknown>, key: string, label: string): string {
  const value = readString(record, key, label, { maxLength: 64 })
  if (!SHA256_DIGEST_PATTERN.test(value)) {
    throw new Error(`${label}.${key} 값이 소문자 sha256 digest가 아닙니다.`)
  }
  return value
}

function decodeCapabilities(input: unknown, label: string): TitleSuggestionCapabilities {
  const record = asRecord(input, label)
  assertExactKeys(
    record,
    ["confirmations_enabled", "status_writes_enabled"],
    label,
  )
  return {
    confirmations_enabled: readBoolean(record, "confirmations_enabled", label),
    status_writes_enabled: readBoolean(record, "status_writes_enabled", label),
  }
}

function decodeProposal(
  input: unknown,
  label: string,
  { listItem }: { listItem: boolean },
): TitleSuggestionProposal | TitleSuggestionListItem {
  const record = asRecord(input, label)
  const commonKeys = [
    "id",
    "storage_key",
    "status",
    "suggestion_reason",
    "proposed_title",
    "confidence",
    "generator_version",
    "review_status",
    "transcript_revision",
    "classification_status",
    "context_type",
    "created_at",
    "updated_at",
    "confirmed_at",
  ]
  assertExactKeys(
    record,
    listItem ? [...commonKeys, "canonical_metadata_changed"] : commonKeys,
    label,
  )
  assertRequiredKeys(
    record,
    listItem ? [...commonKeys, "canonical_metadata_changed"] : commonKeys,
    label,
  )
  const status = readLiteral(record, "status", TITLE_SUGGESTION_STATUSES, label)
  const classificationStatus =
    record.classification_status === null
      ? null
      : readLiteral(
          record,
          "classification_status",
          TITLE_SUGGESTION_STATUSES,
          label,
        )
  const contextType =
    record.context_type === null
      ? null
      : readLiteral(
          record,
          "context_type",
          TITLE_SUGGESTION_CONTEXT_TYPES,
          label,
        )
  const proposal: TitleSuggestionProposal = {
    id: readSafeInteger(record, "id", label, { min: 1 }),
    storage_key: readStorageKey(record, "storage_key", label),
    status,
    suggestion_reason: readLiteral(
      record,
      "suggestion_reason",
      TITLE_SUGGESTION_REASONS,
      label,
    ),
    proposed_title: readNfcString(record, "proposed_title", label, {
      maxLength: MAX_TITLE_LENGTH,
    }),
    confidence: readConfidence(record, "confidence", label),
    generator_version: readLiteral(
      record,
      "generator_version",
      [GENERATOR_VERSION] as const,
      label,
    ),
    review_status: readLiteral(record, "review_status", REVIEW_STATUSES, label),
    transcript_revision: readSafeInteger(record, "transcript_revision", label, {
      min: 1,
    }),
    classification_status: classificationStatus,
    context_type: contextType,
    created_at: readTimestamp(record, "created_at", label),
    updated_at: readTimestamp(record, "updated_at", label),
    confirmed_at: readOptionalTimestamp(record, "confirmed_at", label),
  }
  if (
    (status === "confirmed" && proposal.confirmed_at === null)
    || (status !== "confirmed" && proposal.confirmed_at !== null)
  ) {
    throw new Error(`${label} status와 confirmed_at 조합이 일치하지 않습니다.`)
  }
  if (
    (status === "suggested"
      && !["open", "triaged"].includes(proposal.review_status))
    || (status === "confirmed" && proposal.review_status !== "resolved")
    || (status === "rejected" && proposal.review_status !== "dismissed")
  ) {
    throw new Error(`${label} proposal과 review lifecycle이 일치하지 않습니다.`)
  }
  if (listItem) {
    return {
      ...proposal,
      canonical_metadata_changed: readLiteral(
        record,
        "canonical_metadata_changed",
        [false] as const,
        label,
      ),
    }
  }
  return proposal
}

function decodeLinkedClassification(
  input: unknown,
): TitleSuggestionLinkedClassification | null {
  if (input === null) {
    return null
  }
  const record = asRecord(input, "titleSuggestionDetail.linked_classification")
  assertExactKeys(
    record,
    [
      "status",
      "classification_reason",
      "proposed_title",
      "course_name",
      "course_code",
    ],
    "titleSuggestionDetail.linked_classification",
  )
  assertRequiredKeys(
    record,
    [
      "status",
      "classification_reason",
      "proposed_title",
      "course_name",
      "course_code",
    ],
    "titleSuggestionDetail.linked_classification",
  )
  return {
    status: readLiteral(
      record,
      "status",
      TITLE_SUGGESTION_STATUSES,
      "titleSuggestionDetail.linked_classification",
    ),
    classification_reason: readLiteral(
      record,
      "classification_reason",
      ["unique_time_match"] as const,
      "titleSuggestionDetail.linked_classification",
    ),
    proposed_title: readNfcString(
      record,
      "proposed_title",
      "titleSuggestionDetail.linked_classification",
      { maxLength: MAX_TITLE_LENGTH },
    ),
    course_name: readOptionalNfcString(
      record,
      "course_name",
      "titleSuggestionDetail.linked_classification",
      { maxLength: 256 },
    ),
    course_code: readOptionalNfcString(
      record,
      "course_code",
      "titleSuggestionDetail.linked_classification",
      { maxLength: 128 },
    ),
  }
}

export function decodeTitleSuggestionList(input: unknown): TitleSuggestionListPayload {
  const record = asRecord(input, "titleSuggestions")
  assertExactKeys(
    record,
    [
      "schema_version",
      "available",
      "disabled_reason",
      "counts",
      "total",
      "proposals",
      "capabilities",
    ],
    "titleSuggestions",
  )
  assertRequiredKeys(
    record,
    [
      "schema_version",
      "available",
      "counts",
      "total",
      "proposals",
      "capabilities",
    ],
    "titleSuggestions",
  )
  const available = readBoolean(record, "available", "titleSuggestions")
  const disabledReason =
    record.disabled_reason === undefined
      ? undefined
      : readNfcString(record, "disabled_reason", "titleSuggestions", {
          maxLength: MAX_DISABLED_REASON_LENGTH,
        })
  if (available === (disabledReason !== undefined)) {
    throw new Error("titleSuggestions available과 disabled_reason 조합이 일치하지 않습니다.")
  }
  const countsRecord = asRecord(record.counts, "titleSuggestions.counts")
  assertExactKeys(
    countsRecord,
    ["suggested", "confirmed", "rejected"],
    "titleSuggestions.counts",
  )
  const counts = {
    suggested: readSafeInteger(
      countsRecord,
      "suggested",
      "titleSuggestions.counts",
      { min: 0 },
    ),
    confirmed: readSafeInteger(
      countsRecord,
      "confirmed",
      "titleSuggestions.counts",
      { min: 0 },
    ),
    rejected: readSafeInteger(
      countsRecord,
      "rejected",
      "titleSuggestions.counts",
      { min: 0 },
    ),
  }
  const proposals = readArray(record, "proposals", "titleSuggestions").map(
    (proposal) =>
      decodeProposal(
        proposal,
        "titleSuggestions.proposals[]",
        { listItem: true },
      ) as TitleSuggestionListItem,
  )
  const seenProposalIds = new Set<number>()
  for (const proposal of proposals) {
    if (seenProposalIds.has(proposal.id)) {
      throw new Error(`titleSuggestions.proposals[] id가 중복되었습니다: ${proposal.id}`)
    }
    seenProposalIds.add(proposal.id)
  }
  const total = readSafeInteger(record, "total", "titleSuggestions", { min: 0 })
  const countSum = counts.suggested + counts.confirmed + counts.rejected
  const visibleCounts = {
    suggested: proposals.filter((proposal) => proposal.status === "suggested").length,
    confirmed: proposals.filter((proposal) => proposal.status === "confirmed").length,
    rejected: proposals.filter((proposal) => proposal.status === "rejected").length,
  }
  if (
    total < proposals.length
    || total > countSum
    || visibleCounts.suggested > counts.suggested
    || visibleCounts.confirmed > counts.confirmed
    || visibleCounts.rejected > counts.rejected
  ) {
    throw new Error("titleSuggestions proposal 수와 count/total이 일치하지 않습니다.")
  }
  const capabilities = decodeCapabilities(
    record.capabilities,
    "titleSuggestions.capabilities",
  )
  if (
    !available
    && (
      total !== 0
      || proposals.length !== 0
      || counts.suggested !== 0
      || counts.confirmed !== 0
      || counts.rejected !== 0
      || capabilities.confirmations_enabled
      || capabilities.status_writes_enabled
    )
  ) {
    throw new Error("비활성 titleSuggestions 응답은 항목, count, 쓰기 capability를 노출할 수 없습니다.")
  }
  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/title-suggestion-list@1"] as const,
      "titleSuggestions",
    ),
    available,
    ...(disabledReason === undefined
      ? {}
      : { disabled_reason: disabledReason }),
    counts,
    total,
    proposals,
    capabilities,
  }
}

export function decodeTitleSuggestionDetail(input: unknown): TitleSuggestionDetailPayload {
  const record = asRecord(input, "titleSuggestionDetail")
  assertExactKeys(
    record,
    [
      "schema_version",
      "proposal",
      "linked_classification",
      "canonical_metadata_changed",
      "capabilities",
    ],
    "titleSuggestionDetail",
  )
  assertRequiredKeys(
    record,
    [
      "schema_version",
      "proposal",
      "linked_classification",
      "canonical_metadata_changed",
      "capabilities",
    ],
    "titleSuggestionDetail",
  )
  const proposal = decodeProposal(
    record.proposal,
    "titleSuggestionDetail.proposal",
    { listItem: false },
  ) as TitleSuggestionProposal
  const linkedClassification = decodeLinkedClassification(
    record.linked_classification,
  )
  if (
    (
      proposal.suggestion_reason === "content_topic"
      && (
        linkedClassification !== null
        || proposal.classification_status !== null
      )
    )
    || (
      proposal.suggestion_reason === "schedule_content_match"
      && (
        linkedClassification === null
        || proposal.classification_status === null
      )
    )
  ) {
    throw new Error("titleSuggestionDetail 제안 사유와 linked_classification이 일치하지 않습니다.")
  }
  if (
    linkedClassification !== null
    && linkedClassification.proposed_title !== proposal.proposed_title
  ) {
    throw new Error("titleSuggestionDetail linked_classification이 제안 metadata와 일치하지 않습니다.")
  }
  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/title-suggestion-detail@1"] as const,
      "titleSuggestionDetail",
    ),
    proposal,
    linked_classification: linkedClassification,
    canonical_metadata_changed: readLiteral(
      record,
      "canonical_metadata_changed",
      [false] as const,
      "titleSuggestionDetail",
    ),
    capabilities: decodeCapabilities(
      record.capabilities,
      "titleSuggestionDetail.capabilities",
    ),
  }
}

export function decodeTitleSuggestionStatusResult(
  input: unknown,
): TitleSuggestionStatusResult {
  const record = asRecord(input, "titleSuggestionStatus")
  assertExactKeys(
    record,
    [
      "schema_version",
      "ok",
      "action",
      "proposal_id",
      "status",
      "review_status",
      "canonical_metadata_changed",
    ],
    "titleSuggestionStatus",
  )
  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/title-suggestion-status-result@1"] as const,
      "titleSuggestionStatus",
    ),
    ok: readLiteral(record, "ok", [true] as const, "titleSuggestionStatus"),
    action: readLiteral(
      record,
      "action",
      ["rejected", "skipped"] as const,
      "titleSuggestionStatus",
    ),
    proposal_id: readSafeInteger(record, "proposal_id", "titleSuggestionStatus", {
      min: 1,
    }),
    status: readLiteral(
      record,
      "status",
      ["rejected"] as const,
      "titleSuggestionStatus",
    ),
    review_status: readLiteral(
      record,
      "review_status",
      ["dismissed"] as const,
      "titleSuggestionStatus",
    ),
    canonical_metadata_changed: readLiteral(
      record,
      "canonical_metadata_changed",
      [false] as const,
      "titleSuggestionStatus",
    ),
  }
}

export function decodeTitleSuggestionConfirmationPlan(
  input: unknown,
): TitleSuggestionConfirmationPlan {
  const record = asRecord(input, "titleSuggestionConfirmationPlan")
  assertExactKeys(
    record,
    [
      "schema_version",
      "proposal_id",
      "storage_key",
      "suggestion_reason",
      "proposed_title",
      "transcript_revision",
      "expected_count",
      "materialization",
      "mode",
      "plan_sha256",
      "canonical_metadata_changed",
    ],
    "titleSuggestionConfirmationPlan",
  )
  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/title-suggestion-confirmation-plan@1"] as const,
      "titleSuggestionConfirmationPlan",
    ),
    proposal_id: readSafeInteger(
      record,
      "proposal_id",
      "titleSuggestionConfirmationPlan",
      { min: 1 },
    ),
    storage_key: readStorageKey(
      record,
      "storage_key",
      "titleSuggestionConfirmationPlan",
    ),
    suggestion_reason: readLiteral(
      record,
      "suggestion_reason",
      TITLE_SUGGESTION_REASONS,
      "titleSuggestionConfirmationPlan",
    ),
    proposed_title: readNfcString(
      record,
      "proposed_title",
      "titleSuggestionConfirmationPlan",
      { maxLength: MAX_TITLE_LENGTH },
    ),
    transcript_revision: readSafeInteger(
      record,
      "transcript_revision",
      "titleSuggestionConfirmationPlan",
      { min: 1 },
    ),
    expected_count: readLiteral(
      record,
      "expected_count",
      [1] as const,
      "titleSuggestionConfirmationPlan",
    ),
    materialization: readLiteral(
      record,
      "materialization",
      ["confirmation_audit_only"] as const,
      "titleSuggestionConfirmationPlan",
    ),
    mode: readLiteral(
      record,
      "mode",
      ["read_only"] as const,
      "titleSuggestionConfirmationPlan",
    ),
    plan_sha256: readSha256Digest(
      record,
      "plan_sha256",
      "titleSuggestionConfirmationPlan",
    ),
    canonical_metadata_changed: readLiteral(
      record,
      "canonical_metadata_changed",
      [false] as const,
      "titleSuggestionConfirmationPlan",
    ),
  }
}

export function decodeTitleSuggestionConfirmationResult(
  input: unknown,
): TitleSuggestionConfirmationResult {
  const record = asRecord(input, "titleSuggestionConfirmationResult")
  assertExactKeys(
    record,
    [
      "schema_version",
      "ok",
      "action",
      "proposal_id",
      "status",
      "plan_sha256",
      "canonical_metadata_changed",
    ],
    "titleSuggestionConfirmationResult",
  )
  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/title-suggestion-confirmation-result@1"] as const,
      "titleSuggestionConfirmationResult",
    ),
    ok: readLiteral(
      record,
      "ok",
      [true] as const,
      "titleSuggestionConfirmationResult",
    ),
    action: readLiteral(
      record,
      "action",
      ["confirmed", "skipped"] as const,
      "titleSuggestionConfirmationResult",
    ),
    proposal_id: readSafeInteger(
      record,
      "proposal_id",
      "titleSuggestionConfirmationResult",
      { min: 1 },
    ),
    status: readLiteral(
      record,
      "status",
      ["confirmed"] as const,
      "titleSuggestionConfirmationResult",
    ),
    plan_sha256: readSha256Digest(
      record,
      "plan_sha256",
      "titleSuggestionConfirmationResult",
    ),
    canonical_metadata_changed: readLiteral(
      record,
      "canonical_metadata_changed",
      [false] as const,
      "titleSuggestionConfirmationResult",
    ),
  }
}
