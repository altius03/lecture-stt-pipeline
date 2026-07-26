import type {
  ArchiveReviewStatus,
  RecordingArtifactKind,
  RecordingArtifactStage,
  RecordingContextMetadata,
  RecordingCurrentJobSummary,
  RecordingDetailArtifact,
  RecordingDetailJob,
  RecordingDetailPayload,
  RecordingDetailRecording,
  RecordingDetailReview,
  RecordingLibraryJobStatus,
  RecordingLibraryListPayload,
  RecordingLibrarySummary,
  RecordingReviewSeverity,
  RecordingSourceMetadata,
  RecordingTitleMetadata,
} from "../types"

const STORAGE_KEY_PATTERN = /^[0-9A-Za-z_-]+$/
const JOB_STATUSES = [
  "queued",
  "processing",
  "done",
  "needs_review",
  "error",
  "canceled",
] as const satisfies readonly RecordingLibraryJobStatus[]
const SOURCE_STATES = ["available", "missing"] as const
const TITLE_SOURCES = [
  "manual",
  "schedule",
  "filename_inference",
  "legacy_import",
  "system",
] as const
const CONTEXT_TYPES = [
  "general",
  "class_session",
  "daily_note",
  "meeting",
  "memo",
] as const
const CONTEXT_SOURCES = [
  "manual",
  "schedule_import",
  "filename_inference",
  "legacy_import",
  "system",
] as const
const REVIEW_STATUSES = [
  "open",
  "triaged",
  "resolved",
  "dismissed",
] as const satisfies readonly ArchiveReviewStatus[]
const REVIEW_SEVERITIES = [
  "low",
  "medium",
  "high",
] as const satisfies readonly RecordingReviewSeverity[]
const ARTIFACT_STAGES = [
  "source",
  "transcript",
  "correction",
  "summary",
  "supporting",
] as const satisfies readonly RecordingArtifactStage[]
const ARTIFACT_KINDS = [
  "source_copy",
  "transcript_raw_text",
  "transcript_segments_json",
  "quality_scorecard",
  "correction_text",
  "correction_json",
  "summary_markdown",
  "summary_json",
  "metadata",
  "log",
  "other",
] as const satisfies readonly RecordingArtifactKind[]
const DETAIL_LIMITS = {
  jobs: 50,
  artifacts: 500,
  reviews: 100,
} as const
const ARTIFACT_STAGE_BY_KIND: Record<RecordingArtifactKind, RecordingArtifactStage> = {
  source_copy: "source",
  transcript_raw_text: "transcript",
  transcript_segments_json: "transcript",
  quality_scorecard: "supporting",
  correction_text: "correction",
  correction_json: "correction",
  summary_markdown: "summary",
  summary_json: "summary",
  metadata: "supporting",
  log: "supporting",
  other: "supporting",
}

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
): unknown[] {
  const value = record[key]
  if (!Array.isArray(value)) {
    throw new Error(`${label}.${key} 값이 배열이 아닙니다.`)
  }
  return value
}

function readString(
  record: Record<string, unknown>,
  key: string,
  label: string,
  maxLength = 1024,
): string {
  const value = record[key]
  if (
    typeof value !== "string"
    || value.length === 0
    || value.length > maxLength
    || value.trim().length === 0
  ) {
    throw new Error(`${label}.${key} 값이 올바른 문자열이 아닙니다.`)
  }
  return value
}

function readOptionalString(
  record: Record<string, unknown>,
  key: string,
  label: string,
  maxLength = 1024,
): string | null {
  const value = record[key]
  if (value === undefined || value === null) {
    return null
  }
  if (
    typeof value !== "string"
    || value.length > maxLength
  ) {
    throw new Error(`${label}.${key} 값이 올바른 문자열이 아닙니다.`)
  }
  return value
}

function readBoolean(
  record: Record<string, unknown>,
  key: string,
  label: string,
): boolean {
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

function readOptionalNonNegativeInteger(
  record: Record<string, unknown>,
  key: string,
  label: string,
): number | null {
  const value = record[key]
  if (value === undefined || value === null) {
    return null
  }
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

function readOptionalPositiveInteger(
  record: Record<string, unknown>,
  key: string,
  label: string,
): number | null {
  const value = record[key]
  if (value === undefined || value === null) {
    return null
  }
  if (
    typeof value !== "number"
    || !Number.isInteger(value)
    || !Number.isSafeInteger(value)
    || value <= 0
  ) {
    throw new Error(`${label}.${key} 값이 양의 정수가 아닙니다.`)
  }
  return value
}

function readBoundedInteger(
  record: Record<string, unknown>,
  key: string,
  label: string,
  min: number,
  max: number,
): number {
  const value = readNonNegativeInteger(record, key, label)
  if (value < min || value > max) {
    throw new Error(`${label}.${key} 값이 ${min} 이상 ${max} 이하 정수가 아닙니다.`)
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
    throw new Error(`${label}.${key} 값이 허용된 literal이 아닙니다.`)
  }
  return value as T
}

function readStorageKey(
  record: Record<string, unknown>,
  key: string,
  label: string,
): string {
  const value = readString(record, key, label, 255)
  if (!STORAGE_KEY_PATTERN.test(value)) {
    throw new Error(`${label}.${key} 값이 ASCII storage_key 형식이 아닙니다.`)
  }
  return value
}

function readNfcString(
  record: Record<string, unknown>,
  key: string,
  label: string,
  maxLength: number,
): string {
  const value = readString(record, key, label, maxLength)
  if (value !== value.normalize("NFC")) {
    throw new Error(`${label}.${key} 값이 NFC 정규화와 일치하지 않습니다.`)
  }
  return value
}

function decodeTitleMetadata(
  input: unknown,
  label: string,
): RecordingTitleMetadata | null {
  if (input === undefined || input === null) {
    return null
  }
  const record = asRecord(input, label)
  return {
    title: readNfcString(record, "title", label, 512),
    source: readLiteral(record, "source", TITLE_SOURCES, label),
  }
}

function decodeContextMetadata(
  input: unknown,
  label: string,
): RecordingContextMetadata | null {
  if (input === undefined || input === null) {
    return null
  }
  const record = asRecord(input, label)
  return {
    context_type: readLiteral(record, "context_type", CONTEXT_TYPES, label),
    label: readOptionalString(record, "label", label, 256),
    source: readLiteral(record, "source", CONTEXT_SOURCES, label),
    semester: readOptionalString(record, "semester", label, 128),
    course_name: readOptionalString(record, "course_name", label, 256),
    course_code: readOptionalString(record, "course_code", label, 128),
    session_date: readOptionalString(record, "session_date", label, 64),
    period_label: readOptionalString(record, "period_label", label, 128),
    period_index: readOptionalPositiveInteger(record, "period_index", label),
  }
}

function decodeCurrentJob(
  input: unknown,
  label: string,
): RecordingCurrentJobSummary | null {
  if (input === undefined || input === null) {
    return null
  }
  const record = asRecord(input, label)
  return {
    job_key: readString(record, "job_key", label, 255),
    status: readLiteral(record, "status", JOB_STATUSES, label),
    progress: readBoundedInteger(record, "progress", label, 0, 100),
    requested_profile: readOptionalString(record, "requested_profile", label, 128),
    requested_profile_version: readOptionalString(
      record,
      "requested_profile_version",
      label,
      128,
    ),
    queued_at: readTimestamp(record, "queued_at", label),
    finished_at: readOptionalTimestamp(record, "finished_at", label),
  }
}

function decodeListSummary(
  input: unknown,
  label: string,
): RecordingLibrarySummary {
  const record = asRecord(input, label)
  const currentTitle = decodeTitleMetadata(record.current_title, `${label}.current_title`)
  const originalNameNfc = readNfcString(record, "original_name_nfc", label, 1024)
  const displayName = readNfcString(record, "display_name", label, 1024)
  if (displayName !== (currentTitle?.title ?? originalNameNfc)) {
    throw new Error(`${label}.display_name 값이 title 또는 NFC original_name_nfc와 일치하지 않습니다.`)
  }
  return {
    storage_key: readStorageKey(record, "storage_key", label),
    display_name: displayName,
    original_name_nfc: originalNameNfc,
    source_state: readLiteral(record, "source_state", SOURCE_STATES, label),
    recorded_at: readOptionalTimestamp(record, "recorded_at", label),
    received_at: readTimestamp(record, "received_at", label),
    current_title: currentTitle,
    selected_context: decodeContextMetadata(
      record.selected_context,
      `${label}.selected_context`,
    ),
    current_job: decodeCurrentJob(record.current_job, `${label}.current_job`),
    open_review_count: readNonNegativeInteger(record, "open_review_count", label),
    artifact_count: readNonNegativeInteger(record, "artifact_count", label),
  }
}

function decodeDetailSource(
  input: unknown,
  label: string,
): RecordingSourceMetadata {
  const record = asRecord(input, label)
  return {
    state: readLiteral(record, "state", SOURCE_STATES, label),
    bytes: readOptionalNonNegativeInteger(record, "bytes", label),
    mime_type: readOptionalString(record, "mime_type", label, 256),
    recorded_at: readOptionalTimestamp(record, "recorded_at", label),
    received_at: readTimestamp(record, "received_at", label),
  }
}

function decodeDetailRecording(
  input: unknown,
  label: string,
): RecordingDetailRecording {
  const record = asRecord(input, label)
  const currentTitle = decodeTitleMetadata(record.current_title, `${label}.current_title`)
  const originalNameNfc = readNfcString(record, "original_name_nfc", label, 1024)
  const displayName = readNfcString(record, "display_name", label, 1024)
  if (displayName !== (currentTitle?.title ?? originalNameNfc)) {
    throw new Error(`${label}.display_name 값이 title 또는 NFC original_name_nfc와 일치하지 않습니다.`)
  }
  return {
    storage_key: readStorageKey(record, "storage_key", label),
    display_name: displayName,
    original_name_nfc: originalNameNfc,
    current_title: currentTitle,
    selected_context: decodeContextMetadata(
      record.selected_context,
      `${label}.selected_context`,
    ),
    source: decodeDetailSource(record.source, `${label}.source`),
  }
}

function decodeArtifact(
  input: unknown,
  label: string,
): RecordingDetailArtifact {
  const record = asRecord(input, label)
  const artifactKind = readLiteral(record, "artifact_kind", ARTIFACT_KINDS, label)
  const stage = readLiteral(record, "stage", ARTIFACT_STAGES, label)
  if (ARTIFACT_STAGE_BY_KIND[artifactKind] !== stage) {
    throw new Error(`${label}.stage 값이 artifact_kind와 일치하지 않습니다.`)
  }
  return {
    artifact_kind: artifactKind,
    stage,
    revision: readPositiveInteger(record, "revision", label),
    is_latest: readBoolean(record, "is_latest", label),
    bytes: readOptionalNonNegativeInteger(record, "bytes", label),
    mime_type: readOptionalString(record, "mime_type", label, 256),
    created_at: readTimestamp(record, "created_at", label),
  }
}

function decodeDetailJob(input: unknown, label: string): RecordingDetailJob {
  const record = asRecord(input, label)
  const artifacts = readArray(record, "artifacts", label).map((item, index) =>
    decodeArtifact(item, `${label}.artifacts[${index}]`),
  )
  const seenArtifactKeys = new Set<string>()
  const latestKinds = new Set<string>()
  for (const artifact of artifacts) {
    const key = `${artifact.artifact_kind}:${artifact.revision}`
    if (seenArtifactKeys.has(key)) {
      throw new Error(`${label}.artifacts artifact_kind/revision 조합이 중복되었습니다.`)
    }
    seenArtifactKeys.add(key)
    if (artifact.is_latest) {
      if (latestKinds.has(artifact.artifact_kind)) {
        throw new Error(`${label}.artifacts artifact_kind별 latest가 중복되었습니다.`)
      }
      latestKinds.add(artifact.artifact_kind)
    }
  }
  return {
    job_key: readString(record, "job_key", label, 255),
    status: readLiteral(record, "status", JOB_STATUSES, label),
    progress: readBoundedInteger(record, "progress", label, 0, 100),
    is_current: readBoolean(record, "is_current", label),
    requested_profile: readOptionalString(record, "requested_profile", label, 128),
    requested_profile_version: readOptionalString(
      record,
      "requested_profile_version",
      label,
      128,
    ),
    queued_at: readTimestamp(record, "queued_at", label),
    started_at: readOptionalTimestamp(record, "started_at", label),
    finished_at: readOptionalTimestamp(record, "finished_at", label),
    artifacts,
  }
}

function decodeReview(input: unknown, label: string): RecordingDetailReview {
  const record = asRecord(input, label)
  const artifactKind = readOptionalLiteral(
    record,
    "artifact_kind",
    ARTIFACT_KINDS,
    label,
  )
  const artifactRevision = record.artifact_revision === null || record.artifact_revision === undefined
    ? null
    : readPositiveInteger(record, "artifact_revision", label)
  if ((artifactKind === null) !== (artifactRevision === null)) {
    throw new Error(`${label} artifact_kind와 artifact_revision 유무가 일치하지 않습니다.`)
  }
  const jobKey = readOptionalString(record, "job_key", label, 255)
  if (artifactKind !== null && jobKey === null) {
    throw new Error(`${label} artifact_kind/artifact_revision이 있으면 job_key가 필요합니다.`)
  }
  return {
    status: readLiteral(record, "status", REVIEW_STATUSES, label),
    severity: readOptionalLiteral(record, "severity", REVIEW_SEVERITIES, label),
    reason_code: readString(record, "reason_code", label, 128),
    job_key: jobKey,
    artifact_kind: artifactKind,
    artifact_revision: artifactRevision,
    created_at: readTimestamp(record, "created_at", label),
    resolved_at: readOptionalTimestamp(record, "resolved_at", label),
  }
}

function validateDisabledBoundary(
  available: boolean,
  disabledReason: string | null,
  label: string,
): void {
  if (available && disabledReason !== null) {
    throw new Error(`${label}.disabled_reason 값은 available=true에서 허용되지 않습니다.`)
  }
  if (!available && disabledReason !== "recording_library_disabled") {
    throw new Error(`${label}.disabled_reason 값이 recording_library_disabled와 일치해야 합니다.`)
  }
}

export function decodeRecordingLibraryList(
  input: unknown,
): RecordingLibraryListPayload {
  const record = asRecord(input, "recordingLibrary")
  const filters = asRecord(record.filters, "recordingLibrary.filters")
  const counts = asRecord(record.counts, "recordingLibrary.counts")
  const disabledReason = readOptionalString(
    record,
    "disabled_reason",
    "recordingLibrary",
    256,
  )
  const available = readBoolean(record, "available", "recordingLibrary")
  validateDisabledBoundary(available, disabledReason, "recordingLibrary")

  const payload: RecordingLibraryListPayload = {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/recording-library-list@1"],
      "recordingLibrary",
    ),
    available,
    disabled_reason: disabledReason ?? undefined,
    filters: {
      limit: readBoundedInteger(filters, "limit", "recordingLibrary.filters", 0, 200),
      offset: readBoundedInteger(filters, "offset", "recordingLibrary.filters", 0, 100000),
    },
    counts: {
      recordings: readNonNegativeInteger(counts, "recordings", "recordingLibrary.counts"),
      queued: readNonNegativeInteger(counts, "queued", "recordingLibrary.counts"),
      processing: readNonNegativeInteger(counts, "processing", "recordingLibrary.counts"),
      done: readNonNegativeInteger(counts, "done", "recordingLibrary.counts"),
      needs_review: readNonNegativeInteger(counts, "needs_review", "recordingLibrary.counts"),
      error: readNonNegativeInteger(counts, "error", "recordingLibrary.counts"),
      canceled: readNonNegativeInteger(counts, "canceled", "recordingLibrary.counts"),
      open_reviews: readNonNegativeInteger(counts, "open_reviews", "recordingLibrary.counts"),
    },
    total: readNonNegativeInteger(record, "total", "recordingLibrary"),
    summaries: readArray(record, "summaries", "recordingLibrary").map((item, index) =>
      decodeListSummary(item, `recordingLibrary.summaries[${index}]`),
    ),
  }

  const statusSum = payload.counts.queued
    + payload.counts.processing
    + payload.counts.done
    + payload.counts.needs_review
    + payload.counts.error
    + payload.counts.canceled
  if (statusSum > payload.counts.recordings) {
    throw new Error("recordingLibrary.counts 상태 합계가 recordings를 초과합니다.")
  }
  if (payload.total !== payload.counts.recordings) {
    throw new Error("recordingLibrary.total 값이 counts.recordings와 일치하지 않습니다.")
  }
  if (payload.summaries.length > payload.filters.limit) {
    throw new Error("recordingLibrary.summaries 길이가 filters.limit를 초과합니다.")
  }
  if (payload.summaries.length > 0 && payload.filters.offset + payload.summaries.length > payload.total) {
    throw new Error("recordingLibrary.filters 범위가 total을 초과합니다.")
  }

  const seenStorageKeys = new Set<string>()
  let pageOpenReviews = 0
  for (const item of payload.summaries) {
    if (seenStorageKeys.has(item.storage_key)) {
      throw new Error("recordingLibrary.summaries storage_key가 중복되었습니다.")
    }
    seenStorageKeys.add(item.storage_key)
    pageOpenReviews += item.open_review_count
  }
  if (pageOpenReviews > payload.counts.open_reviews) {
    throw new Error("recordingLibrary.summaries open_review_count 합계가 counts.open_reviews를 초과합니다.")
  }

  return payload
}

export function decodeRecordingDetail(input: unknown): RecordingDetailPayload {
  const record = asRecord(input, "recordingDetail")
  const counts = asRecord(record.counts, "recordingDetail.counts")
  const limits = asRecord(record.limits, "recordingDetail.limits")
  const disabledReason = readOptionalString(
    record,
    "disabled_reason",
    "recordingDetail",
    256,
  )
  const available = readBoolean(record, "available", "recordingDetail")
  validateDisabledBoundary(available, disabledReason, "recordingDetail")

  const payload: RecordingDetailPayload = {
    schema_version: readLiteral(
      record,
      "schema_version",
      ["storage-v2/recording-detail@1"],
      "recordingDetail",
    ),
    available,
    disabled_reason: disabledReason ?? undefined,
    recording: decodeDetailRecording(record.recording, "recordingDetail.recording"),
    counts: {
      jobs: readNonNegativeInteger(counts, "jobs", "recordingDetail.counts"),
      artifacts: readNonNegativeInteger(counts, "artifacts", "recordingDetail.counts"),
      reviews: readNonNegativeInteger(counts, "reviews", "recordingDetail.counts"),
      open_reviews: readNonNegativeInteger(counts, "open_reviews", "recordingDetail.counts"),
    },
    limits: {
      jobs: readPositiveInteger(limits, "jobs", "recordingDetail.limits"),
      artifacts: readPositiveInteger(limits, "artifacts", "recordingDetail.limits"),
      reviews: readPositiveInteger(limits, "reviews", "recordingDetail.limits"),
      jobs_truncated: readBoolean(limits, "jobs_truncated", "recordingDetail.limits"),
      artifacts_truncated: readBoolean(limits, "artifacts_truncated", "recordingDetail.limits"),
      reviews_truncated: readBoolean(limits, "reviews_truncated", "recordingDetail.limits"),
    },
    jobs: readArray(record, "jobs", "recordingDetail").map((item, index) =>
      decodeDetailJob(item, `recordingDetail.jobs[${index}]`),
    ),
    reviews: readArray(record, "reviews", "recordingDetail").map((item, index) =>
      decodeReview(item, `recordingDetail.reviews[${index}]`),
    ),
  }

  if (payload.jobs.length > payload.limits.jobs) {
    throw new Error("recordingDetail.jobs 길이가 limits.jobs를 초과합니다.")
  }
  if (payload.limits.jobs !== DETAIL_LIMITS.jobs) {
    throw new Error("recordingDetail.limits.jobs 값이 backend 고정 상한과 다릅니다.")
  }
  if (payload.limits.artifacts !== DETAIL_LIMITS.artifacts) {
    throw new Error("recordingDetail.limits.artifacts 값이 backend 고정 상한과 다릅니다.")
  }
  if (payload.limits.reviews !== DETAIL_LIMITS.reviews) {
    throw new Error("recordingDetail.limits.reviews 값이 backend 고정 상한과 다릅니다.")
  }
  if (payload.reviews.length > payload.limits.reviews) {
    throw new Error("recordingDetail.reviews 길이가 limits.reviews를 초과합니다.")
  }
  if (payload.counts.open_reviews > payload.counts.reviews) {
    throw new Error("recordingDetail.counts.open_reviews가 counts.reviews를 초과합니다.")
  }
  if (!payload.limits.jobs_truncated && payload.jobs.length !== payload.counts.jobs) {
    throw new Error("recordingDetail.jobs_truncated=false 인데 counts.jobs와 길이가 다릅니다.")
  }
  if (payload.limits.jobs_truncated && payload.counts.jobs <= payload.jobs.length) {
    throw new Error("recordingDetail.jobs_truncated=true 인데 counts.jobs가 증가하지 않았습니다.")
  }
  if (!payload.limits.reviews_truncated && payload.reviews.length !== payload.counts.reviews) {
    throw new Error("recordingDetail.reviews_truncated=false 인데 counts.reviews와 길이가 다릅니다.")
  }
  if (payload.limits.reviews_truncated && payload.counts.reviews <= payload.reviews.length) {
    throw new Error("recordingDetail.reviews_truncated=true 인데 counts.reviews가 증가하지 않았습니다.")
  }

  const jobKeys = new Set<string>()
  let currentJobCount = 0
  const stageArtifactCount = payload.jobs.reduce((sum, job) => sum + job.artifacts.length, 0)
  for (const job of payload.jobs) {
    if (jobKeys.has(job.job_key)) {
      throw new Error("recordingDetail.jobs job_key가 중복되었습니다.")
    }
    jobKeys.add(job.job_key)
    if (job.is_current) {
      currentJobCount += 1
    }
  }
  if (currentJobCount > 1) {
    throw new Error("recordingDetail.jobs is_current=true 항목이 둘 이상입니다.")
  }
  if (stageArtifactCount > payload.limits.artifacts) {
    throw new Error("recordingDetail.jobs.artifacts 길이 합계가 limits.artifacts를 초과합니다.")
  }
  if (stageArtifactCount > payload.counts.artifacts) {
    throw new Error("recordingDetail.jobs.artifacts 길이 합계가 counts.artifacts를 초과합니다.")
  }
  if (!payload.limits.artifacts_truncated && stageArtifactCount !== payload.counts.artifacts) {
    throw new Error("recordingDetail.artifacts_truncated=false 인데 counts.artifacts와 visible artifact 수가 다릅니다.")
  }
  if (payload.limits.artifacts_truncated && payload.counts.artifacts <= stageArtifactCount) {
    throw new Error("recordingDetail.artifacts_truncated=true 인데 counts.artifacts가 증가하지 않았습니다.")
  }

  let visibleOpenReviews = 0
  for (const review of payload.reviews) {
    if (review.status === "open" || review.status === "triaged") {
      visibleOpenReviews += 1
    }
  }
  if (!payload.limits.reviews_truncated && visibleOpenReviews !== payload.counts.open_reviews) {
    throw new Error("recordingDetail.reviews의 open/triaged 개수가 counts.open_reviews와 다릅니다.")
  }
  if (payload.limits.reviews_truncated && visibleOpenReviews > payload.counts.open_reviews) {
    throw new Error("recordingDetail.reviews의 open/triaged 개수가 counts.open_reviews를 초과합니다.")
  }

  return payload
}
