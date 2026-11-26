import type {
  ClassificationConfirmationPlan,
  ClassificationConfirmationResult,
  ClassificationProposalDetail,
  ClassificationProposalDetailPayload,
  ClassificationProposalListItem,
  ClassificationProposalListPayload,
  ClassificationStatusResult,
  TimetableCapabilities,
  TimetableEntriesPayload,
  TimetableEntry,
} from "../types"

const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const
const CLASSIFICATION_STATUSES = ["suggested", "confirmed", "rejected"] as const
const CLASSIFICATION_REASONS = [
  "unique_time_match",
  "recorded_at_missing",
  "recorded_at_invalid",
  "no_time_match",
  "ambiguous_time_match",
] as const

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

function readOptionalNumber(record: Record<string, unknown>, key: string, label: string): number | null {
  const value = record[key]
  if (value === undefined || value === null) {
    return null
  }
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${label}.${key} 값이 숫자가 아닙니다.`)
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

function readSha256Digest(record: Record<string, unknown>, key: string, label: string): string {
  const value = readString(record, key, label)
  if (!/^[a-f0-9]{64}$/.test(value)) {
    throw new Error(`${label}.${key} 값이 소문자 sha256 digest가 아닙니다.`)
  }
  return value
}

function decodeCapabilities(input: unknown, label: string): TimetableCapabilities | undefined {
  if (input === undefined) {
    return undefined
  }
  const record = asRecord(input, label)
  return {
    confirmations_enabled: readBoolean(record, "confirmations_enabled", label),
    status_writes_enabled: readBoolean(record, "status_writes_enabled", label),
  }
}

function decodeTimetableEntry(input: unknown): TimetableEntry {
  const record = asRecord(input, "entries[]")
  return {
    id: readNumber(record, "id", "entries[]"),
    entry_key: readString(record, "entry_key", "entries[]"),
    semester: readString(record, "semester", "entries[]"),
    course_name: readString(record, "course_name", "entries[]"),
    course_code: readOptionalString(record, "course_code", "entries[]"),
    weekday: readLiteral(record, "weekday", WEEKDAYS, "entries[]"),
    start_time: readString(record, "start_time", "entries[]"),
    end_time: readString(record, "end_time", "entries[]"),
    period_label: readString(record, "period_label", "entries[]"),
    period_index: readOptionalNumber(record, "period_index", "entries[]"),
    classroom: readString(record, "classroom", "entries[]"),
  }
}

function decodeProposalListItem(input: unknown): ClassificationProposalListItem {
  const record = asRecord(input, "proposals[]")
  return {
    id: readNumber(record, "id", "proposals[]"),
    storage_key: readString(record, "storage_key", "proposals[]"),
    status: readLiteral(record, "status", CLASSIFICATION_STATUSES, "proposals[]"),
    classification_reason: readLiteral(record, "classification_reason", CLASSIFICATION_REASONS, "proposals[]"),
    proposed_title: readString(record, "proposed_title", "proposals[]"),
    context_type: readString(record, "context_type", "proposals[]"),
    semester: readString(record, "semester", "proposals[]"),
    course_name: readOptionalString(record, "course_name", "proposals[]"),
    course_code: readOptionalString(record, "course_code", "proposals[]"),
    session_date: readOptionalString(record, "session_date", "proposals[]"),
    weekday:
      record.weekday === null || record.weekday === undefined
        ? null
        : readLiteral(record, "weekday", WEEKDAYS, "proposals[]"),
    start_time: readOptionalString(record, "start_time", "proposals[]"),
    end_time: readOptionalString(record, "end_time", "proposals[]"),
    period_label: readOptionalString(record, "period_label", "proposals[]"),
    period_index: readOptionalNumber(record, "period_index", "proposals[]"),
    classroom: readOptionalString(record, "classroom", "proposals[]"),
    confidence: readOptionalNumber(record, "confidence", "proposals[]"),
    review_status: readOptionalString(record, "review_status", "proposals[]"),
    created_at: readString(record, "created_at", "proposals[]"),
    updated_at: readString(record, "updated_at", "proposals[]"),
    confirmed_at: readOptionalString(record, "confirmed_at", "proposals[]"),
  }
}

function decodeProposalDetail(input: unknown): ClassificationProposalDetail {
  const record = asRecord(input, "proposal")
  return {
    id: readNumber(record, "id", "proposal"),
    storage_key: readString(record, "storage_key", "proposal"),
    status: readLiteral(record, "status", CLASSIFICATION_STATUSES, "proposal"),
    classification_reason: readLiteral(record, "classification_reason", CLASSIFICATION_REASONS, "proposal"),
    proposed_title: readString(record, "proposed_title", "proposal"),
    context_type: readString(record, "context_type", "proposal"),
    label: readOptionalString(record, "label", "proposal"),
    semester: readString(record, "semester", "proposal"),
    course_name: readOptionalString(record, "course_name", "proposal"),
    course_code: readOptionalString(record, "course_code", "proposal"),
    session_date: readOptionalString(record, "session_date", "proposal"),
    weekday:
      record.weekday === null || record.weekday === undefined
        ? null
        : readLiteral(record, "weekday", WEEKDAYS, "proposal"),
    start_time: readOptionalString(record, "start_time", "proposal"),
    end_time: readOptionalString(record, "end_time", "proposal"),
    period_label: readOptionalString(record, "period_label", "proposal"),
    period_index: readOptionalNumber(record, "period_index", "proposal"),
    classroom: readOptionalString(record, "classroom", "proposal"),
    confidence: readOptionalNumber(record, "confidence", "proposal"),
    review_status: readOptionalString(record, "review_status", "proposal"),
    review_reason_code: readOptionalString(record, "review_reason_code", "proposal"),
    created_at: readString(record, "created_at", "proposal"),
    updated_at: readString(record, "updated_at", "proposal"),
    confirmed_at: readOptionalString(record, "confirmed_at", "proposal"),
  }
}

export function decodeTimetableEntriesPayload(input: unknown): TimetableEntriesPayload {
  const record = asRecord(input, "timetable")
  return {
    schema_version: readLiteral(record, "schema_version", ["storage-v2/timetable-list@1"], "timetable"),
    available: readBoolean(record, "available", "timetable"),
    disabled_reason: readOptionalString(record, "disabled_reason", "timetable") ?? undefined,
    semester: readOptionalString(record, "semester", "timetable"),
    total: readNumber(record, "total", "timetable"),
    entries: readArray(record, "entries", "timetable").map(decodeTimetableEntry),
    capabilities: decodeCapabilities(record.capabilities, "timetable.capabilities"),
  }
}

export function decodeClassificationProposalListPayload(input: unknown): ClassificationProposalListPayload {
  const record = asRecord(input, "classifications")
  const countsRecord = asRecord(record.counts, "classifications.counts")
  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/classification-proposal-list@1"],
      "classifications",
    ),
    available: readBoolean(record, "available", "classifications"),
    disabled_reason: readOptionalString(record, "disabled_reason", "classifications") ?? undefined,
    counts: {
      suggested: readNumber(countsRecord, "suggested", "classifications.counts"),
      confirmed: readNumber(countsRecord, "confirmed", "classifications.counts"),
      rejected: readNumber(countsRecord, "rejected", "classifications.counts"),
    },
    total: readNumber(record, "total", "classifications"),
    proposals: readArray(record, "proposals", "classifications").map(decodeProposalListItem),
    capabilities: decodeCapabilities(record.capabilities, "classifications.capabilities"),
  }
}

export function decodeClassificationProposalDetailPayload(input: unknown): ClassificationProposalDetailPayload {
  const record = asRecord(input, "classificationDetail")
  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/classification-proposal-detail@1"],
      "classificationDetail",
    ),
    proposal: decodeProposalDetail(record.proposal),
    candidate_entry_keys: readArray(record, "candidate_entry_keys", "classificationDetail").map((value) => {
      if (typeof value !== "string") {
        throw new Error("classificationDetail.candidate_entry_keys 값이 문자열 배열이 아닙니다.")
      }
      return value
    }),
    canonical_metadata_changed: readBoolean(record, "canonical_metadata_changed", "classificationDetail"),
    capabilities: decodeCapabilities(record.capabilities, "classificationDetail.capabilities"),
  }
}

export function decodeClassificationStatusResult(input: unknown): ClassificationStatusResult {
  const record = asRecord(input, "classificationStatus")
  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/classification-status-result@1"],
      "classificationStatus",
    ),
    ok: readLiteral(record, "ok", [true] as const, "classificationStatus"),
    action: readLiteral(record, "action", ["rejected", "skipped"], "classificationStatus"),
    proposal_id: readNumber(record, "proposal_id", "classificationStatus"),
    status: readLiteral(record, "status", ["rejected"], "classificationStatus"),
    review_status: readLiteral(record, "review_status", ["dismissed"], "classificationStatus"),
    canonical_metadata_changed: readLiteral(
      record,
      "canonical_metadata_changed",
      [false] as const,
      "classificationStatus",
    ),
  }
}

export function decodeClassificationConfirmationPlan(input: unknown): ClassificationConfirmationPlan {
  const record = asRecord(input, "classificationConfirmationPlan")
  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/classification-confirmation-plan@1"],
      "classificationConfirmationPlan",
    ),
    proposal_id: readNumber(record, "proposal_id", "classificationConfirmationPlan"),
    storage_key: readString(record, "storage_key", "classificationConfirmationPlan"),
    proposal_updated_at: readString(record, "proposal_updated_at", "classificationConfirmationPlan"),
    review_status: readString(record, "review_status", "classificationConfirmationPlan"),
    semester: readString(record, "semester", "classificationConfirmationPlan"),
    course_name: readString(record, "course_name", "classificationConfirmationPlan"),
    session_date: readString(record, "session_date", "classificationConfirmationPlan"),
    period_label: readOptionalString(record, "period_label", "classificationConfirmationPlan"),
    proposed_title: readString(record, "proposed_title", "classificationConfirmationPlan"),
    expected_count: readLiteral(record, "expected_count", [1] as const, "classificationConfirmationPlan"),
    materialization: readLiteral(
      record,
      "materialization",
      ["confirmation_audit_only"],
      "classificationConfirmationPlan",
    ),
    mode: readLiteral(record, "mode", ["read_only"], "classificationConfirmationPlan"),
    plan_sha256: readSha256Digest(record, "plan_sha256", "classificationConfirmationPlan"),
    canonical_metadata_changed: readLiteral(
      record,
      "canonical_metadata_changed",
      [false] as const,
      "classificationConfirmationPlan",
    ),
  }
}

export function decodeClassificationConfirmationResult(input: unknown): ClassificationConfirmationResult {
  const record = asRecord(input, "classificationConfirmationResult")
  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/classification-confirmation-result@1"],
      "classificationConfirmationResult",
    ),
    ok: readLiteral(record, "ok", [true] as const, "classificationConfirmationResult"),
    action: readLiteral(
      record,
      "action",
      ["confirmed", "skipped"],
      "classificationConfirmationResult",
    ),
    proposal_id: readNumber(record, "proposal_id", "classificationConfirmationResult"),
    status: readLiteral(record, "status", ["confirmed"], "classificationConfirmationResult"),
    plan_sha256: readSha256Digest(record, "plan_sha256", "classificationConfirmationResult"),
    canonical_metadata_changed: readLiteral(
      record,
      "canonical_metadata_changed",
      [false] as const,
      "classificationConfirmationResult",
    ),
  }
}
