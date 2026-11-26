import type {
  UnifiedReviewFeedItemPayload,
  UnifiedReviewFeedPayload,
  UnifiedReviewFeedSource,
  UnifiedReviewFeedSourceLedgerPayload,
  UnifiedReviewFeedSourceStatus,
} from "../types"

const UNIFIED_REVIEW_SCHEMA = "storage-v2/unified-review-feed@2"
const UNIFIED_REVIEW_SOURCES = [
  "archive",
  "timetable",
  "title",
  "recording",
] as const satisfies readonly UnifiedReviewFeedSource[]
const UNIFIED_REVIEW_SOURCE_STATUSES = [
  "ready",
  "unavailable",
] as const satisfies readonly UnifiedReviewFeedSourceStatus[]
const CANONICAL_KEY_PATTERN = /^[0-9A-Za-z_-]+$/
const POSITIVE_INTEGER_PATTERN = /^[1-9][0-9]*$/
const MAX_LIMIT = 200
const MAX_OFFSET = 100_000
const MAX_TITLE_LENGTH = 1024
const MAX_LABEL_LENGTH = 512
const MAX_META_LENGTH = 1024
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

function readLiteral<T extends string>(
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
  if (typeof value !== "string" || value.length > maxLength || (!allowEmpty && value.length === 0)) {
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

function readSafeInteger(
  record: Record<string, unknown>,
  key: string,
  label: string,
  options?: {
    min?: number
    max?: number
  },
): number {
  const value = record[key]
  const min = options?.min ?? Number.MIN_SAFE_INTEGER
  const max = options?.max ?? Number.MAX_SAFE_INTEGER
  if (
    typeof value !== "number"
    || !Number.isInteger(value)
    || !Number.isSafeInteger(value)
    || value < min
    || value > max
  ) {
    throw new Error(`${label}.${key} 값이 ${min} 이상 ${max} 이하의 안전한 정수가 아닙니다.`)
  }
  return value
}

function readNullableSafeInteger(
  record: Record<string, unknown>,
  key: string,
  label: string,
  options?: {
    min?: number
    max?: number
  },
): number | null {
  const value = record[key]
  if (value === null) {
    return null
  }
  return readSafeInteger(record, key, label, options)
}

function readTimestamp(record: Record<string, unknown>, key: string, label: string): string {
  const value = readString(record, key, label, { maxLength: MAX_TIMESTAMP_LENGTH })
  if (!Number.isFinite(Date.parse(value))) {
    throw new Error(`${label}.${key} 값이 해석 가능한 시각이 아닙니다.`)
  }
  return value
}

function decodeCanonicalItemIdentity(
  source: UnifiedReviewFeedSource,
  id: string,
  href: string,
): void {
  const [idPrefix, rawIdentity] = id.split(":", 2)
  if (idPrefix !== source || !rawIdentity) {
    throw new Error(`items[] id가 ${source} canonical identity와 일치하지 않습니다.`)
  }
  const hrefPrefix = `#review/${source}/`
  if (!href.startsWith(hrefPrefix)) {
    throw new Error(`items[] href가 ${source} review deep link와 일치하지 않습니다.`)
  }
  const hrefIdentity = href.slice(hrefPrefix.length)
  if (hrefIdentity !== rawIdentity) {
    throw new Error(`items[] href identity가 ${source} id와 일치하지 않습니다.`)
  }
  if (source === "timetable" || source === "title") {
    const numericIdentity = Number(rawIdentity)
    if (
      !POSITIVE_INTEGER_PATTERN.test(rawIdentity)
      || !Number.isSafeInteger(numericIdentity)
      || numericIdentity <= 0
    ) {
      throw new Error(
        `items[] ${source} id는 JS-safe 양의 정수 identity를 사용해야 합니다.`,
      )
    }
    return
  }
  if (!CANONICAL_KEY_PATTERN.test(rawIdentity)) {
    throw new Error(`items[] ${source} identity는 canonical ASCII key 형식이어야 합니다.`)
  }
}

function decodeItem(
  input: unknown,
  seenItemIds: Set<string>,
): UnifiedReviewFeedItemPayload {
  const record = asRecord(input, "items[]")
  assertExactKeys(
    record,
    ["id", "source", "title", "state_label", "reason_label", "timestamp", "meta_label", "href"],
    "items[]",
  )
  const source = readLiteral(record, "source", UNIFIED_REVIEW_SOURCES, "items[]")
  const id = readString(record, "id", "items[]", { maxLength: 512 })
  if (seenItemIds.has(id)) {
    throw new Error(`items[] id가 중복되었습니다: ${id}`)
  }
  seenItemIds.add(id)
  const href = readString(record, "href", "items[]", { maxLength: 1024 })
  decodeCanonicalItemIdentity(source, id, href)
  return {
    id,
    source,
    title: readNfcString(record, "title", "items[]", { maxLength: MAX_TITLE_LENGTH }),
    state_label: readNfcString(record, "state_label", "items[]", { maxLength: MAX_LABEL_LENGTH }),
    reason_label: readNfcString(record, "reason_label", "items[]", { maxLength: MAX_LABEL_LENGTH }),
    timestamp: readTimestamp(record, "timestamp", "items[]"),
    meta_label: readNfcString(record, "meta_label", "items[]", { maxLength: MAX_META_LENGTH }),
    href,
  }
}

function decodeSourceLedger(
  input: unknown,
  itemCountsBySource: ReadonlyMap<UnifiedReviewFeedSource, number>,
): UnifiedReviewFeedSourceLedgerPayload {
  const record = asRecord(input, "sources[]")
  assertExactKeys(
    record,
    ["source", "label", "status", "available", "visible_count", "total_count", "truncated", "note", "error"],
    "sources[]",
  )
  const source = readLiteral(record, "source", UNIFIED_REVIEW_SOURCES, "sources[]")
  const status = readLiteral(record, "status", UNIFIED_REVIEW_SOURCE_STATUSES, "sources[]")
  const available = readBoolean(record, "available", "sources[]")
  if ((available && status !== "ready") || (!available && status !== "unavailable")) {
    throw new Error(`sources[] ${source}의 available/status 조합이 일치하지 않습니다.`)
  }
  const visibleCount = readSafeInteger(record, "visible_count", "sources[]", { min: 0 })
  const actualVisibleCount = itemCountsBySource.get(source) ?? 0
  if (visibleCount !== actualVisibleCount) {
    throw new Error(`sources[] ${source} visible_count가 items[]와 일치하지 않습니다.`)
  }
  const totalCount = readNullableSafeInteger(record, "total_count", "sources[]", { min: 0 })
  if (totalCount !== null && totalCount < visibleCount) {
    throw new Error(`sources[] ${source} total_count가 visible_count보다 작을 수 없습니다.`)
  }
  if (available && totalCount === null) {
    throw new Error(`sources[] ${source} ready source는 total_count가 필요합니다.`)
  }
  if (!available && (visibleCount !== 0 || totalCount !== null)) {
    throw new Error(`sources[] ${source} unavailable source는 count를 노출할 수 없습니다.`)
  }
  const truncated = readBoolean(record, "truncated", "sources[]")
  if (available && truncated !== (totalCount! > visibleCount)) {
    throw new Error(`sources[] ${source} truncated가 count와 일치하지 않습니다.`)
  }
  if (!available && truncated) {
    throw new Error(`sources[] ${source} unavailable source는 truncated일 수 없습니다.`)
  }
  const error = record.error
  if (error !== null) {
    throw new Error(`sources[] ${source}.error 값은 null이어야 합니다.`)
  }
  return {
    source,
    label: readNfcString(record, "label", "sources[]", { maxLength: MAX_LABEL_LENGTH }),
    status,
    available,
    visible_count: visibleCount,
    total_count: totalCount,
    truncated,
    note: readNfcString(record, "note", "sources[]", { allowEmpty: true, maxLength: MAX_META_LENGTH }),
    error: null,
  }
}

export function decodeUnifiedReviewFeed(input: unknown): UnifiedReviewFeedPayload {
  const record = asRecord(input, "unifiedReviewFeed")
  assertExactKeys(
    record,
    ["schema_version", "available", "filters", "total", "items", "sources"],
    "unifiedReviewFeed",
  )
  const filters = asRecord(record.filters, "unifiedReviewFeed.filters")
  assertExactKeys(filters, ["limit", "offset"], "unifiedReviewFeed.filters")
  const limit = readSafeInteger(filters, "limit", "unifiedReviewFeed.filters", { min: 0, max: MAX_LIMIT })
  const offset = readSafeInteger(filters, "offset", "unifiedReviewFeed.filters", { min: 0, max: MAX_OFFSET })
  const total = readSafeInteger(record, "total", "unifiedReviewFeed", { min: 0 })
  const seenItemIds = new Set<string>()
  const items = readArray(record, "items", "unifiedReviewFeed").map((item) => decodeItem(item, seenItemIds))
  if (items.length > limit) {
    throw new Error("unifiedReviewFeed.items 길이가 filters.limit를 초과했습니다.")
  }
  if (total === 0) {
    if (items.length !== 0) {
      throw new Error("unifiedReviewFeed 빈 페이지 bounds가 일치하지 않습니다.")
    }
  } else if (offset > total) {
    if (items.length !== 0) {
      throw new Error("unifiedReviewFeed offset이 total보다 클 때 items는 비어 있어야 합니다.")
    }
  } else if (offset + items.length > total) {
    throw new Error("unifiedReviewFeed 페이지 범위가 total을 초과했습니다.")
  }

  const itemCountsBySource = new Map<UnifiedReviewFeedSource, number>()
  for (const item of items) {
    itemCountsBySource.set(item.source, (itemCountsBySource.get(item.source) ?? 0) + 1)
  }
  const sources = readArray(record, "sources", "unifiedReviewFeed").map((source) =>
    decodeSourceLedger(source, itemCountsBySource),
  )
  const seenSources = new Set<UnifiedReviewFeedSource>()
  for (const source of sources) {
    if (seenSources.has(source.source)) {
      throw new Error(`sources[] source가 중복되었습니다: ${source.source}`)
    }
    seenSources.add(source.source)
  }
  if (sources.length !== UNIFIED_REVIEW_SOURCES.length || seenSources.size !== UNIFIED_REVIEW_SOURCES.length) {
    throw new Error("sources[]는 archive, timetable, title, recording 네 source를 정확히 한 번씩 포함해야 합니다.")
  }
  for (const source of UNIFIED_REVIEW_SOURCES) {
    if (!seenSources.has(source)) {
      throw new Error(`sources[]에 ${source} source가 없습니다.`)
    }
  }
  for (const [index, expectedSource] of UNIFIED_REVIEW_SOURCES.entries()) {
    if (sources[index]?.source !== expectedSource) {
      throw new Error(
        "sources[]는 archive, timetable, title, recording canonical 순서를 따라야 합니다.",
      )
    }
  }
  const exactTotal = sources.reduce(
    (sum, source) => sum + (source.total_count ?? 0),
    0,
  )
  if (exactTotal !== total) {
    throw new Error("unifiedReviewFeed total이 source total_count 합계와 일치하지 않습니다.")
  }

  const available = readBoolean(record, "available", "unifiedReviewFeed")
  if (available !== sources.some((source) => source.available)) {
    throw new Error("unifiedReviewFeed available이 source availability와 일치하지 않습니다.")
  }
  if (!available) {
    if (total !== 0 || items.length !== 0) {
      throw new Error("unifiedReviewFeed available=false 응답은 비어 있어야 합니다.")
    }
    if (sources.some((source) => source.available || source.status === "ready")) {
      throw new Error("unifiedReviewFeed available=false 응답은 ready source를 포함할 수 없습니다.")
    }
  }

  return {
    schema_version: readLiteral(
      record,
      "schema_version",
      [UNIFIED_REVIEW_SCHEMA],
      "unifiedReviewFeed",
    ),
    available,
    filters: {
      limit,
      offset,
    },
    total,
    items,
    sources,
  }
}
