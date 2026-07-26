import type {
  TranscriptionAnalyticsAttentionItem,
  TranscriptionAnalyticsBucket,
  TranscriptionAnalyticsCoverage,
  TranscriptionAnalyticsPayload,
  TranscriptionAnalyticsPeriod,
  TranscriptionAnalyticsTimelinePoint,
  TranscriptionClassificationDistribution,
  TranscriptionQualityBand,
  TranscriptionQualityDistribution,
  TranscriptionStatusDistribution,
} from "../types"

const PERIODS = ["day", "week", "month"] as const
const BUCKETS = ["hour", "day"] as const
const STATUSES = [
  "queued",
  "processing",
  "done",
  "needs_review",
  "error",
  "canceled",
] as const
const HEALTH_VALUES = ["good", "warn", "bad"] as const
const QUALITY_BANDS = [
  "90-100",
  "80-89",
  "70-79",
  "60-69",
  "0-59",
  "unscored",
] as const
const CONTEXT_TYPES = [
  "general",
  "class_session",
  "daily_note",
  "meeting",
  "memo",
] as const
const CLASSIFICATION_SOURCES = [
  "manual",
  "schedule_import",
  "filename_inference",
  "legacy_import",
  "system",
] as const
const QUALITY_BAND_LABELS: Record<TranscriptionQualityBand, string> = {
  "90-100": "90–100점",
  "80-89": "80–89점",
  "70-79": "70–79점",
  "60-69": "60–69점",
  "0-59": "0–59점",
  unscored: "점수 없음",
}
const CONTEXT_LABELS: Record<(typeof CONTEXT_TYPES)[number], string> = {
  general: "일반",
  class_session: "수업",
  daily_note: "일상 기록",
  meeting: "회의·대화",
  memo: "개인 메모",
}
const STORAGE_KEY_PATTERN = /^[0-9A-Za-z_-]+$/

function asRecord(input: unknown, label: string): Record<string, unknown> {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`)
  }
  return input as Record<string, unknown>
}

function readArray(
  record: Record<string, unknown>,
  key: string,
  label: string,
  maxLength: number,
): unknown[] {
  const value = record[key]
  if (!Array.isArray(value) || value.length > maxLength) {
    throw new Error(`${label}.${key} 배열이 없거나 허용 길이를 넘었습니다.`)
  }
  return value
}

function readString(
  record: Record<string, unknown>,
  key: string,
  label: string,
  maxLength = 512,
): string {
  const value = record[key]
  if (typeof value !== "string" || value.length === 0 || value.length > maxLength) {
    throw new Error(`${label}.${key} 값이 올바른 문자열이 아닙니다.`)
  }
  return value
}

function readOptionalString(
  record: Record<string, unknown>,
  key: string,
  label: string,
  maxLength = 512,
): string | null {
  const value = record[key]
  if (value === undefined || value === null) {
    return null
  }
  if (typeof value !== "string" || value.length === 0 || value.length > maxLength) {
    throw new Error(`${label}.${key} 값이 올바른 문자열이 아닙니다.`)
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

function readNonNegativeInteger(
  record: Record<string, unknown>,
  key: string,
  label: string,
): number {
  const value = record[key]
  if (
    typeof value !== "number"
    || !Number.isInteger(value)
    || !Number.isSafeInteger(value)
    || value < 0
  ) {
    throw new Error(`${label}.${key} 값이 0 이상의 정수가 아닙니다.`)
  }
  return value
}

function readPositiveInteger(
  record: Record<string, unknown>,
  key: string,
  label: string,
): number {
  const value = readNonNegativeInteger(record, key, label)
  if (value === 0) {
    throw new Error(`${label}.${key} 값이 양의 정수가 아닙니다.`)
  }
  return value
}

function readOptionalNonNegativeNumber(
  record: Record<string, unknown>,
  key: string,
  label: string,
): number | null {
  const value = record[key]
  if (value === undefined || value === null) {
    return null
  }
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new Error(`${label}.${key} 값이 0 이상의 유한한 숫자가 아닙니다.`)
  }
  return value
}

function readQualityScore(
  record: Record<string, unknown>,
  key: string,
  label: string,
): number | null {
  const value = readOptionalNonNegativeNumber(record, key, label)
  if (value !== null && value > 100) {
    throw new Error(`${label}.${key} 값이 0~100 범위를 벗어났습니다.`)
  }
  return value
}

function readIntegerQualityScore(
  record: Record<string, unknown>,
  key: string,
  label: string,
): number | null {
  const value = readQualityScore(record, key, label)
  if (value !== null && !Number.isInteger(value)) {
    throw new Error(`${label}.${key} 값이 정수가 아닙니다.`)
  }
  return value
}

function readTimestamp(
  record: Record<string, unknown>,
  key: string,
  label: string,
): string {
  const value = readString(record, key, label, 64)
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
  const value = readOptionalString(record, key, label, 64)
  if (value !== null && !Number.isFinite(Date.parse(value))) {
    throw new Error(`${label}.${key} 값이 해석 가능한 시각이 아닙니다.`)
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
    throw new Error(`${label}.${key} 값이 허용된 literal이 아닙니다.`)
  }
  return value as T
}

function decodeCoverage(input: unknown): TranscriptionAnalyticsCoverage {
  const record = asRecord(input, "analytics.coverage")
  return {
    jobs_total: readNonNegativeInteger(record, "jobs_total", "analytics.coverage"),
    quality_scored: readNonNegativeInteger(record, "quality_scored", "analytics.coverage"),
    quality_missing: readNonNegativeInteger(record, "quality_missing", "analytics.coverage"),
    quality_invalid: readNonNegativeInteger(record, "quality_invalid", "analytics.coverage"),
    classification_known: readNonNegativeInteger(record, "classification_known", "analytics.coverage"),
    classification_unclassified: readNonNegativeInteger(
      record,
      "classification_unclassified",
      "analytics.coverage",
    ),
    audio_duration_known: readNonNegativeInteger(
      record,
      "audio_duration_known",
      "analytics.coverage",
    ),
    processing_duration_known: readNonNegativeInteger(
      record,
      "processing_duration_known",
      "analytics.coverage",
    ),
    event_time_recorded: readNonNegativeInteger(record, "event_time_recorded", "analytics.coverage"),
    event_time_received: readNonNegativeInteger(record, "event_time_received", "analytics.coverage"),
    event_time_queued: readNonNegativeInteger(record, "event_time_queued", "analytics.coverage"),
    event_time_invalid: readNonNegativeInteger(record, "event_time_invalid", "analytics.coverage"),
  }
}

function decodeTimelinePoint(input: unknown): TranscriptionAnalyticsTimelinePoint {
  const record = asRecord(input, "analytics.timeline[]")
  const jobs = readNonNegativeInteger(record, "jobs", "analytics.timeline[]")
  const qualityScored = readNonNegativeInteger(record, "quality_scored", "analytics.timeline[]")
  const point = {
    bucket_start: readTimestamp(record, "bucket_start", "analytics.timeline[]"),
    label: readString(record, "label", "analytics.timeline[]", 64),
    jobs,
    done: readNonNegativeInteger(record, "done", "analytics.timeline[]"),
    needs_review: readNonNegativeInteger(record, "needs_review", "analytics.timeline[]"),
    error: readNonNegativeInteger(record, "error", "analytics.timeline[]"),
    quality_scored: qualityScored,
    average_quality_score: readQualityScore(
      record,
      "average_quality_score",
      "analytics.timeline[]",
    ),
  }
  if (point.done + point.needs_review + point.error > jobs || qualityScored > jobs) {
    throw new Error("analytics.timeline[] 상태/품질 집계가 bucket 작업 수를 넘었습니다.")
  }
  if ((qualityScored === 0) !== (point.average_quality_score === null)) {
    throw new Error("analytics.timeline[] 평균 품질과 품질 표본 수가 일치하지 않습니다.")
  }
  return point
}

function decodeQualityDistribution(input: unknown): TranscriptionQualityDistribution {
  const record = asRecord(input, "analytics.quality_distribution[]")
  return {
    band: readLiteral(record, "band", QUALITY_BANDS, "analytics.quality_distribution[]"),
    label: readString(record, "label", "analytics.quality_distribution[]", 64),
    min: readQualityScore(record, "min", "analytics.quality_distribution[]"),
    max: readQualityScore(record, "max", "analytics.quality_distribution[]"),
    count: readNonNegativeInteger(record, "count", "analytics.quality_distribution[]"),
  }
}

function decodeStatusDistribution(input: unknown): TranscriptionStatusDistribution {
  const record = asRecord(input, "analytics.status_distribution[]")
  return {
    status: readLiteral(record, "status", STATUSES, "analytics.status_distribution[]"),
    count: readNonNegativeInteger(record, "count", "analytics.status_distribution[]"),
  }
}

function decodeClassificationDistribution(
  input: unknown,
): TranscriptionClassificationDistribution {
  const record = asRecord(input, "analytics.classification_distribution[]")
  const contextType = readOptionalString(
    record,
    "context_type",
    "analytics.classification_distribution[]",
    64,
  )
  const source = readOptionalString(
    record,
    "source",
    "analytics.classification_distribution[]",
    64,
  )
  if (
    contextType !== null
    && !CONTEXT_TYPES.includes(contextType as (typeof CONTEXT_TYPES)[number])
  ) {
    throw new Error("analytics.classification_distribution[] context_type이 허용 범위를 벗어났습니다.")
  }
  if (
    source !== null
    && !CLASSIFICATION_SOURCES.includes(
      source as (typeof CLASSIFICATION_SOURCES)[number],
    )
  ) {
    throw new Error("analytics.classification_distribution[] source가 허용 범위를 벗어났습니다.")
  }
  if ((contextType === null) !== (source === null)) {
    throw new Error("미분류 bucket은 context_type과 source가 모두 null이어야 합니다.")
  }
  const label = readString(
    record,
    "label",
    "analytics.classification_distribution[]",
    128,
  )
  const expectedLabel =
    contextType === null
      ? "미분류"
      : CONTEXT_LABELS[contextType as (typeof CONTEXT_TYPES)[number]]
  if (label !== expectedLabel) {
    throw new Error(
      "analytics.classification_distribution[] label이 context_type과 일치하지 않습니다.",
    )
  }
  return {
    context_type: contextType,
    source,
    label,
    count: readNonNegativeInteger(record, "count", "analytics.classification_distribution[]"),
  }
}

function decodeAttentionItem(input: unknown): TranscriptionAnalyticsAttentionItem {
  const record = asRecord(input, "analytics.recent_attention[]")
  const storageKey = readString(record, "storage_key", "analytics.recent_attention[]", 256)
  if (!STORAGE_KEY_PATTERN.test(storageKey)) {
    throw new Error("analytics.recent_attention[] storage_key 형식이 올바르지 않습니다.")
  }
  const contextType = readOptionalString(
    record,
    "context_type",
    "analytics.recent_attention[]",
    64,
  )
  const classificationSource = readOptionalString(
    record,
    "classification_source",
    "analytics.recent_attention[]",
    64,
  )
  if (
    contextType !== null
    && !CONTEXT_TYPES.includes(contextType as (typeof CONTEXT_TYPES)[number])
  ) {
    throw new Error("analytics.recent_attention[] context_type이 허용 범위를 벗어났습니다.")
  }
  if (
    classificationSource !== null
    && !CLASSIFICATION_SOURCES.includes(
      classificationSource as (typeof CLASSIFICATION_SOURCES)[number],
    )
  ) {
    throw new Error("analytics.recent_attention[] classification_source가 허용 범위를 벗어났습니다.")
  }
  if ((contextType === null) !== (classificationSource === null)) {
    throw new Error(
      "analytics.recent_attention[] 분류 context와 source 유무가 일치하지 않습니다.",
    )
  }
  const health =
    record.health === null
      ? null
      : readLiteral(
          record,
          "health",
          HEALTH_VALUES,
          "analytics.recent_attention[]",
        )
  const qualityScore = readIntegerQualityScore(
    record,
    "quality_score",
    "analytics.recent_attention[]",
  )
  if ((qualityScore === null) !== (health === null)) {
    throw new Error(
      "analytics.recent_attention[] 품질 점수와 health 유무가 일치하지 않습니다.",
    )
  }
  return {
    storage_key: storageKey,
    display_name: readString(record, "display_name", "analytics.recent_attention[]", 1024),
    status: readLiteral(record, "status", STATUSES, "analytics.recent_attention[]"),
    event_at: readTimestamp(record, "event_at", "analytics.recent_attention[]"),
    quality_score: qualityScore,
    health,
    context_type: contextType,
    classification_source: classificationSource,
  }
}

function assertUnique<T>(values: T[], label: string): void {
  if (new Set(values).size !== values.length) {
    throw new Error(`${label} 값이 중복되었습니다.`)
  }
}

function sum(values: number[]): number {
  return values.reduce((total, value) => total + value, 0)
}

function expectedTimelineLength(period: TranscriptionAnalyticsPeriod): number {
  if (period === "day") {
    return 24
  }
  return period === "week" ? 7 : 30
}

function expectedBucket(period: TranscriptionAnalyticsPeriod): TranscriptionAnalyticsBucket {
  return period === "day" ? "hour" : "day"
}

export function decodeTranscriptionAnalytics(
  input: unknown,
): TranscriptionAnalyticsPayload {
  const record = asRecord(input, "analytics")
  const schemaVersion = readLiteral(
    record,
    "schema_version",
    ["storage-v2/transcription-analytics@1"],
    "analytics",
  )
  const available = readBoolean(record, "available", "analytics")
  const disabledReason = readOptionalString(record, "disabled_reason", "analytics", 128)
  if (!available && disabledReason === null) {
    throw new Error("비활성 analytics 응답에는 disabled_reason이 필요합니다.")
  }
  const period = readLiteral(record, "period", PERIODS, "analytics")
  const timezone = readString(record, "timezone", "analytics", 64)
  if (timezone !== "Asia/Seoul") {
    throw new Error("analytics.timezone은 Asia/Seoul이어야 합니다.")
  }

  const windowRecord = asRecord(record.window, "analytics.window")
  const bucket = readLiteral(windowRecord, "bucket", BUCKETS, "analytics.window")
  if (bucket !== expectedBucket(period)) {
    throw new Error("analytics.window.bucket이 period와 일치하지 않습니다.")
  }
  const window = {
    start_at: readTimestamp(windowRecord, "start_at", "analytics.window"),
    end_at: readTimestamp(windowRecord, "end_at", "analytics.window"),
    bucket,
    label: readString(windowRecord, "label", "analytics.window", 64),
  }
  if (Date.parse(window.start_at) > Date.parse(window.end_at)) {
    throw new Error("analytics.window 시작 시각이 종료 시각보다 늦습니다.")
  }

  const freshnessRecord = asRecord(record.freshness, "analytics.freshness")
  const freshness = {
    generated_at: readTimestamp(freshnessRecord, "generated_at", "analytics.freshness"),
    latest_event_at: readOptionalTimestamp(
      freshnessRecord,
      "latest_event_at",
      "analytics.freshness",
    ),
  }
  const limitsRecord = asRecord(record.limits, "analytics.limits")
  const limits = {
    row_limit: readPositiveInteger(limitsRecord, "row_limit", "analytics.limits"),
    scorecard_max_bytes: readPositiveInteger(
      limitsRecord,
      "scorecard_max_bytes",
      "analytics.limits",
    ),
    truncated: readBoolean(limitsRecord, "truncated", "analytics.limits"),
  }
  if (limits.row_limit > 5000 || limits.scorecard_max_bytes > 1024 * 1024) {
    throw new Error("analytics.limits가 클라이언트 허용 범위를 넘었습니다.")
  }

  const coverage = decodeCoverage(record.coverage)
  const totalsRecord = asRecord(record.totals, "analytics.totals")
  const totals = {
    jobs: readNonNegativeInteger(totalsRecord, "jobs", "analytics.totals"),
    queued: readNonNegativeInteger(totalsRecord, "queued", "analytics.totals"),
    processing: readNonNegativeInteger(totalsRecord, "processing", "analytics.totals"),
    done: readNonNegativeInteger(totalsRecord, "done", "analytics.totals"),
    needs_review: readNonNegativeInteger(totalsRecord, "needs_review", "analytics.totals"),
    error: readNonNegativeInteger(totalsRecord, "error", "analytics.totals"),
    canceled: readNonNegativeInteger(totalsRecord, "canceled", "analytics.totals"),
    average_quality_score: readQualityScore(
      totalsRecord,
      "average_quality_score",
      "analytics.totals",
    ),
    audio_duration_sec: readOptionalNonNegativeNumber(
      totalsRecord,
      "audio_duration_sec",
      "analytics.totals",
    ),
    total_processing_sec: readOptionalNonNegativeNumber(
      totalsRecord,
      "total_processing_sec",
      "analytics.totals",
    ),
  }

  const timeline = readArray(
    record,
    "timeline",
    "analytics",
    expectedTimelineLength(period),
  ).map(decodeTimelinePoint)
  if (timeline.length !== expectedTimelineLength(period)) {
    throw new Error("analytics.timeline bucket 수가 period와 일치하지 않습니다.")
  }
  assertUnique(
    timeline.map((point) => point.bucket_start),
    "analytics.timeline.bucket_start",
  )
  for (let index = 1; index < timeline.length; index += 1) {
    if (
      Date.parse(timeline[index - 1].bucket_start)
      >= Date.parse(timeline[index].bucket_start)
    ) {
      throw new Error("analytics.timeline은 시각 오름차순이어야 합니다.")
    }
  }

  const qualityDistribution = readArray(
    record,
    "quality_distribution",
    "analytics",
    QUALITY_BANDS.length,
  ).map(decodeQualityDistribution)
  if (
    qualityDistribution.length !== QUALITY_BANDS.length
    || qualityDistribution.some(
      (entry, index) => entry.band !== QUALITY_BANDS[index],
    )
  ) {
    throw new Error("analytics.quality_distribution band 구성이 고정 계약과 다릅니다.")
  }
  const expectedBandRanges: Record<
    TranscriptionQualityBand,
    [number | null, number | null]
  > = {
    "90-100": [90, 100],
    "80-89": [80, 89],
    "70-79": [70, 79],
    "60-69": [60, 69],
    "0-59": [0, 59],
    unscored: [null, null],
  }
  for (const distribution of qualityDistribution) {
    const [expectedMin, expectedMax] = expectedBandRanges[distribution.band]
    if (distribution.min !== expectedMin || distribution.max !== expectedMax) {
      throw new Error("analytics.quality_distribution 점수 구간이 고정 계약과 다릅니다.")
    }
    if (distribution.label !== QUALITY_BAND_LABELS[distribution.band]) {
      throw new Error("analytics.quality_distribution label이 band와 일치하지 않습니다.")
    }
  }

  const statusDistribution = readArray(
    record,
    "status_distribution",
    "analytics",
    STATUSES.length,
  ).map(decodeStatusDistribution)
  if (statusDistribution.length !== STATUSES.length) {
    throw new Error("analytics.status_distribution은 모든 상태를 포함해야 합니다.")
  }
  assertUnique(
    statusDistribution.map((entry) => entry.status),
    "analytics.status_distribution.status",
  )

  const classificationDistribution = readArray(
    record,
    "classification_distribution",
    "analytics",
    26,
  ).map(decodeClassificationDistribution)
  const unclassifiedRows = classificationDistribution.filter(
    (entry) => entry.context_type === null && entry.source === null,
  )
  if (unclassifiedRows.length !== 1) {
    throw new Error("analytics.classification_distribution에는 미분류 bucket이 하나 필요합니다.")
  }
  assertUnique(
    classificationDistribution.map((entry) =>
      JSON.stringify([entry.context_type, entry.source]),
    ),
    "analytics.classification_distribution context/source pair",
  )

  const recentAttention = readArray(
    record,
    "recent_attention",
    "analytics",
    20,
  ).map(decodeAttentionItem)
  assertUnique(
    recentAttention.map((entry) => entry.storage_key),
    "analytics.recent_attention.storage_key",
  )

  if (
    coverage.quality_scored + coverage.quality_missing + coverage.quality_invalid
    !== coverage.jobs_total
  ) {
    throw new Error("analytics.coverage 품질 분모가 jobs_total과 일치하지 않습니다.")
  }
  if (
    coverage.classification_known + coverage.classification_unclassified
    !== coverage.jobs_total
  ) {
    throw new Error("analytics.coverage 분류 분모가 jobs_total과 일치하지 않습니다.")
  }
  if (
    coverage.audio_duration_known > coverage.jobs_total
    || coverage.processing_duration_known > coverage.jobs_total
  ) {
    throw new Error("analytics.coverage 시간 표본 수가 jobs_total을 넘었습니다.")
  }
  if (
    coverage.event_time_recorded
      + coverage.event_time_received
      + coverage.event_time_queued
      + coverage.event_time_invalid
    !== coverage.jobs_total
  ) {
    throw new Error("analytics.coverage 시각 분모가 jobs_total과 일치하지 않습니다.")
  }
  if (
    totals.jobs !== coverage.jobs_total
    || sum([
      totals.queued,
      totals.processing,
      totals.done,
      totals.needs_review,
      totals.error,
      totals.canceled,
    ]) !== totals.jobs
  ) {
    throw new Error("analytics.totals 상태 합계가 jobs_total과 일치하지 않습니다.")
  }
  if (
    (coverage.quality_scored === 0) !== (totals.average_quality_score === null)
  ) {
    throw new Error("analytics.totals 평균 품질과 품질 표본 수가 일치하지 않습니다.")
  }
  if (
    (coverage.audio_duration_known === 0)
      !== (totals.audio_duration_sec === null)
    || (coverage.processing_duration_known === 0)
      !== (totals.total_processing_sec === null)
  ) {
    throw new Error("analytics.totals 시간 합계와 시간 표본 수가 일치하지 않습니다.")
  }
  if (sum(timeline.map((point) => point.jobs)) !== totals.jobs) {
    throw new Error("analytics.timeline 합계가 totals.jobs와 일치하지 않습니다.")
  }
  if (
    sum(timeline.map((point) => point.done)) !== totals.done
    || sum(timeline.map((point) => point.needs_review)) !== totals.needs_review
    || sum(timeline.map((point) => point.error)) !== totals.error
    || sum(timeline.map((point) => point.quality_scored))
      !== coverage.quality_scored
  ) {
    throw new Error("analytics.timeline 상태/품질 합계가 totals/coverage와 일치하지 않습니다.")
  }
  if (sum(qualityDistribution.map((entry) => entry.count)) !== totals.jobs) {
    throw new Error("analytics.quality_distribution 합계가 totals.jobs와 일치하지 않습니다.")
  }
  const unscoredQualityCount = qualityDistribution.find(
    (entry) => entry.band === "unscored",
  )?.count
  const scoredQualityCount = sum(
    qualityDistribution
      .filter((entry) => entry.band !== "unscored")
      .map((entry) => entry.count),
  )
  if (
    scoredQualityCount !== coverage.quality_scored
    || unscoredQualityCount
      !== coverage.quality_missing + coverage.quality_invalid
  ) {
    throw new Error(
      "analytics.quality_distribution 품질 coverage와 일치하지 않습니다.",
    )
  }
  if (sum(statusDistribution.map((entry) => entry.count)) !== totals.jobs) {
    throw new Error("analytics.status_distribution 합계가 totals.jobs와 일치하지 않습니다.")
  }
  const expectedStatusCounts: Record<
    (typeof STATUSES)[number],
    number
  > = {
    queued: totals.queued,
    processing: totals.processing,
    done: totals.done,
    needs_review: totals.needs_review,
    error: totals.error,
    canceled: totals.canceled,
  }
  if (
    statusDistribution.some(
      (entry) => entry.count !== expectedStatusCounts[entry.status],
    )
  ) {
    throw new Error("analytics.status_distribution 상태별 값이 totals와 일치하지 않습니다.")
  }
  if (sum(classificationDistribution.map((entry) => entry.count)) !== totals.jobs) {
    throw new Error("analytics.classification_distribution 합계가 totals.jobs와 일치하지 않습니다.")
  }
  const unclassifiedCount = unclassifiedRows[0].count
  const classifiedCount = sum(
    classificationDistribution
      .filter((entry) => entry.context_type !== null)
      .map((entry) => entry.count),
  )
  if (
    classifiedCount !== coverage.classification_known
    || unclassifiedCount !== coverage.classification_unclassified
  ) {
    throw new Error(
      "analytics.classification_distribution 분류 coverage와 일치하지 않습니다.",
    )
  }

  return {
    schema_version: schemaVersion,
    available,
    ...(disabledReason === null ? {} : { disabled_reason: disabledReason }),
    period,
    timezone,
    window,
    freshness,
    limits,
    coverage,
    totals,
    timeline,
    quality_distribution: qualityDistribution,
    status_distribution: statusDistribution,
    classification_distribution: classificationDistribution,
    recent_attention: recentAttention,
  }
}
