import {
  decodeArchiveReviewDetailPayload,
  decodeArchiveReviewListPayload,
  decodeArchiveReviewPromotionPlan,
  decodeArchiveReviewPromotionResult,
  decodeArchiveReviewStatusResult,
} from "./decodeArchiveReview"
import { decodeLogPayload, decodePanelActionResponse, decodePanelState } from "./decodePanelState"
import {
  decodeRecordingDetail,
  decodeRecordingLibraryList,
} from "./decodeRecordingLibrary"
import {
  decodeClassificationConfirmationPlan,
  decodeClassificationConfirmationResult,
  decodeClassificationProposalDetailPayload,
  decodeClassificationProposalListPayload,
  decodeClassificationStatusResult,
  decodeTimetableEntriesPayload,
} from "./decodeTimetable"
import {
  decodeTitleSuggestionConfirmationPlan,
  decodeTitleSuggestionConfirmationResult,
  decodeTitleSuggestionDetail,
  decodeTitleSuggestionList,
  decodeTitleSuggestionStatusResult,
} from "./decodeTitleSuggestions"
import { decodeTranscriptionAnalytics } from "./decodeTranscriptionAnalytics"
import { decodeUnifiedReviewFeed } from "./decodeUnifiedReview"
import type {
  ArchiveReviewArtifactKind,
  ArchiveReviewDetailPayload,
  ArchiveReviewListPayload,
  ArchiveReviewPromotionPlan,
  ArchiveReviewPromotionSelectionRequest,
  ArchiveReviewPromotionResult,
  ArchiveReviewStatus,
  ArchiveReviewStatusResult,
  ClassificationConfirmationPlan,
  ClassificationConfirmationResult,
  ClassificationProposalDetailPayload,
  ClassificationProposalListPayload,
  ClassificationStatusResult,
  LogPayload,
  NotificationSelection,
  PanelActionResponse,
  PanelEndpoints,
  PanelState,
  RecordingDetailPayload,
  RecordingLibraryListPayload,
  TimetableEntriesPayload,
  TitleSuggestionConfirmationPlan,
  TitleSuggestionConfirmationResult,
  TitleSuggestionDetailPayload,
  TitleSuggestionListPayload,
  TitleSuggestionStatus,
  TitleSuggestionStatusResult,
  TranscriptionAnalyticsPayload,
  TranscriptionAnalyticsPeriod,
  UnifiedReviewFeedPayload,
} from "../types"

const FORM_HEADERS = {
  "Content-Type": "application/x-www-form-urlencoded",
}

export const FALLBACK_PANEL_ENDPOINTS: PanelEndpoints = {
  state: "/api/state",
  logs: "/api/logs",
  notification: "/api/notification",
  start: "/api/start",
  pause: "/api/pause",
  resume: "/api/resume",
  stop: "/api/stop",
  refresh: "/api/refresh",
  exit: "/api/exit",
  clear_history: "/api/clear_history",
}

export const PANEL_EVENTS_ENDPOINT = "/api/events"
export const PANEL_STATE_EVENTS_ENDPOINT = "/api/events?streams=state"
export const ARCHIVE_REVIEW_ENDPOINTS = {
  cases: "/api/storage-v2/archive-evidence/cases",
}
export const TIMETABLE_ENDPOINTS = {
  entries: "/api/storage-v2/timetable/entries",
  classifications: "/api/storage-v2/timetable/classifications",
}
export const TITLE_SUGGESTION_ENDPOINT = "/api/storage-v2/title-suggestions"
export const UNIFIED_REVIEW_ENDPOINT = "/api/storage-v2/review-feed"
export const RECORDING_LIBRARY_ENDPOINT = "/api/storage-v2/library/recordings"
export const TRANSCRIPTION_ANALYTICS_ENDPOINT =
  "/api/storage-v2/analytics/transcriptions"
const SHA256_DIGEST_PATTERN = /^[a-f0-9]{64}$/
const CASE_KEY_PATTERN = /^[0-9A-Za-z_-]+$/
const ARCHIVE_REVIEW_ARTIFACT_KINDS = [
  "correction_text",
  "correction_json",
  "summary_markdown",
] as const satisfies readonly ArchiveReviewArtifactKind[]

function toErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

function normalizeArchiveReviewCaseKey(caseKey: string): string {
  const normalizedCaseKey = caseKey.trim()
  if (!normalizedCaseKey) {
    throw new Error("Archive review case_key is required.")
  }
  if (!CASE_KEY_PATTERN.test(normalizedCaseKey)) {
    throw new Error("Archive review case_key must use the canonical ASCII key format.")
  }
  return normalizedCaseKey
}

function normalizePositiveInteger(value: number, label: string): number {
  if (!Number.isInteger(value) || !Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`${label} must be a positive integer.`)
  }
  return value
}

function normalizeNonNegativeInteger(value: number, label: string): number {
  if (!Number.isInteger(value) || !Number.isSafeInteger(value) || value < 0) {
    throw new Error(`${label} must be a non-negative integer.`)
  }
  return value
}

function normalizeArchiveReviewSelectionRequest(
  selectedRevisions: Record<string, number>,
): ArchiveReviewPromotionSelectionRequest {
  const entries = Object.entries(selectedRevisions)
  if (entries.length === 0) {
    throw new Error("Archive review promotion requires at least one selected revision.")
  }
  const normalizedEntries: Array<[ArchiveReviewArtifactKind, number]> = []
  for (const [rawKind, rawRevisionId] of entries) {
    const artifactKind = rawKind.trim() as ArchiveReviewArtifactKind
    if (!ARCHIVE_REVIEW_ARTIFACT_KINDS.includes(artifactKind)) {
      throw new Error(`Unsupported archive review artifact kind: ${rawKind}`)
    }
    normalizedEntries.push([
      artifactKind,
      normalizePositiveInteger(rawRevisionId, `Revision id for ${artifactKind}`),
    ])
  }
  normalizedEntries.sort(([left], [right]) => left.localeCompare(right))
  return Object.fromEntries(normalizedEntries) as ArchiveReviewPromotionSelectionRequest
}

function assertPlanMatchesRequest(
  plan: ArchiveReviewPromotionPlan,
  expectedSelections: ArchiveReviewPromotionSelectionRequest,
): void {
  const actualSelections = new Map<ArchiveReviewArtifactKind, number>()
  for (const selection of plan.selected_revisions) {
    if (actualSelections.has(selection.artifact_kind)) {
      throw new Error("Archive review promotion plan returned duplicate artifact kinds.")
    }
    actualSelections.set(selection.artifact_kind, selection.revision_id)
  }
  const expectedEntries = Object.entries(expectedSelections) as Array<[
    ArchiveReviewArtifactKind,
    number,
  ]>
  if (actualSelections.size !== expectedEntries.length) {
    throw new Error("Archive review promotion plan selection count does not match the request.")
  }
  for (const [artifactKind, revisionId] of expectedEntries) {
    if (actualSelections.get(artifactKind) !== revisionId) {
      throw new Error("Archive review promotion plan selected revisions changed; create a new plan.")
    }
  }
}

function readApiErrorMessage(payload: unknown): string | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null
  }
  const { error } = payload as { error?: unknown }
  return typeof error === "string" ? error : null
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch (error) {
    throw new Error(toErrorMessage(error, "서버 응답을 해석하지 못했습니다."))
  }
}

export async function fetchPanelState(endpoint: string = FALLBACK_PANEL_ENDPOINTS.state): Promise<PanelState> {
  const response = await fetch(endpoint, {
    cache: "no-store",
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(`Failed to fetch state: ${response.status}`)
  }
  return decodePanelState(payload)
}

export async function fetchRecordingLibraryList(
  params?: { limit?: number; offset?: number },
  signal?: AbortSignal,
): Promise<RecordingLibraryListPayload> {
  const query = new URLSearchParams()
  if (typeof params?.limit === "number") {
    query.set("limit", String(params.limit))
  }
  if (typeof params?.offset === "number") {
    query.set("offset", String(params.offset))
  }
  const suffix = query.size > 0 ? `?${query.toString()}` : ""
  const response = await fetch(`${RECORDING_LIBRARY_ENDPOINT}${suffix}`, {
    cache: "no-store",
    signal,
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(readApiErrorMessage(payload) ?? `Failed to fetch recording library: ${response.status}`)
  }
  return decodeRecordingLibraryList(payload)
}

export async function fetchUnifiedReviewFeed(
  params: { limit: number; offset: number },
  signal?: AbortSignal,
): Promise<UnifiedReviewFeedPayload> {
  const limit = normalizePositiveInteger(params.limit, "Unified review limit")
  if (limit > 100) {
    throw new Error("Unified review limit must be less than or equal to 100.")
  }
  const offset = normalizeNonNegativeInteger(params.offset, "Unified review offset")
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  })
  const response = await fetch(`${UNIFIED_REVIEW_ENDPOINT}?${query.toString()}`, {
    cache: "no-store",
    signal,
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(readApiErrorMessage(payload) ?? `Failed to fetch unified review feed: ${response.status}`)
  }
  const decoded = decodeUnifiedReviewFeed(payload)
  if (decoded.filters.limit !== limit || decoded.filters.offset !== offset) {
    throw new Error("Unified review feed response pagination does not match the request.")
  }
  return decoded
}

export async function fetchRecordingDetail(
  storageKey: string,
  signal?: AbortSignal,
): Promise<RecordingDetailPayload> {
  if (!storageKey) {
    throw new Error("녹음 storage_key를 입력하세요.")
  }
  if (storageKey !== storageKey.trim()) {
    throw new Error("Recording storage_key must not include surrounding whitespace.")
  }
  if (!CASE_KEY_PATTERN.test(storageKey)) {
    throw new Error("Recording storage_key must use the canonical ASCII key format.")
  }
  const response = await fetch(
    `${RECORDING_LIBRARY_ENDPOINT}/${encodeURIComponent(storageKey)}`,
    {
      cache: "no-store",
      signal,
    },
  )
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(readApiErrorMessage(payload) ?? `Failed to fetch recording detail: ${response.status}`)
  }
  const detail = decodeRecordingDetail(payload)
  if (detail.recording.storage_key !== storageKey) {
    throw new Error("Recording detail response storage_key does not match the request.")
  }
  return detail
}

export async function fetchLogs(endpoint: string, offset: number | null): Promise<LogPayload> {
  const query = offset === null ? "" : `?offset=${encodeURIComponent(offset)}`
  const response = await fetch(`${endpoint}${query}`, {
    cache: "no-store",
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(`Failed to fetch logs: ${response.status}`)
  }
  return decodeLogPayload(payload)
}

export async function postPanelAction(endpoint: string): Promise<PanelActionResponse> {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: FORM_HEADERS,
    body: "",
  })
  const payload = await readJson(response)
  const decoded = decodePanelActionResponse(payload)
  if (!response.ok) {
    if (!decoded.ok) {
      throw new Error(decoded.error)
    }
    throw new Error(`Failed to run panel action: ${response.status}`)
  }
  if (!decoded.ok) {
    throw new Error(decoded.error)
  }
  return decoded
}

export async function postNotificationSelection(
  endpoint: string,
  selection: NotificationSelection,
  options?: { applyNow?: boolean },
): Promise<PanelActionResponse> {
  const body = new URLSearchParams({
    selection,
    ...(options?.applyNow ? { apply_now: "1" } : {}),
  }).toString()
  const response = await fetch(endpoint, {
    method: "POST",
    headers: FORM_HEADERS,
    body,
  })
  const payload = await readJson(response)
  const decoded = decodePanelActionResponse(payload)
  if (!response.ok) {
    if (!decoded.ok) {
      throw new Error(decoded.error)
    }
    throw new Error(`Failed to save notification selection: ${response.status}`)
  }
  if (!decoded.ok) {
    throw new Error(decoded.error)
  }
  return decoded
}

export async function fetchTimetableEntries(
  params?: { semester?: string; limit?: number; offset?: number },
): Promise<TimetableEntriesPayload> {
  const query = new URLSearchParams()
  if (params?.semester) {
    query.set("semester", params.semester)
  }
  if (typeof params?.limit === "number") {
    query.set("limit", String(params.limit))
  }
  if (typeof params?.offset === "number") {
    query.set("offset", String(params.offset))
  }
  const suffix = query.size > 0 ? `?${query.toString()}` : ""
  const response = await fetch(`${TIMETABLE_ENDPOINTS.entries}${suffix}`, {
    cache: "no-store",
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(readApiErrorMessage(payload) ?? `Failed to fetch timetable entries: ${response.status}`)
  }
  return decodeTimetableEntriesPayload(payload)
}

export async function fetchTranscriptionAnalytics(
  period: TranscriptionAnalyticsPeriod,
  signal?: AbortSignal,
): Promise<TranscriptionAnalyticsPayload> {
  const query = new URLSearchParams({ period })
  const response = await fetch(
    `${TRANSCRIPTION_ANALYTICS_ENDPOINT}?${query.toString()}`,
    {
      cache: "no-store",
      signal,
    },
  )
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(
      readApiErrorMessage(payload)
        ?? `Failed to fetch transcription analytics: ${response.status}`,
    )
  }
  const decoded = decodeTranscriptionAnalytics(payload)
  if (decoded.period !== period) {
    throw new Error("Transcription analytics response period does not match the request.")
  }
  return decoded
}

export async function fetchArchiveReviewCases(
  params?: { reviewStatus?: ArchiveReviewStatus; limit?: number; offset?: number },
): Promise<ArchiveReviewListPayload> {
  const query = new URLSearchParams()
  if (params?.reviewStatus) {
    query.set("status", params.reviewStatus)
  }
  if (typeof params?.limit === "number") {
    query.set("limit", String(params.limit))
  }
  if (typeof params?.offset === "number") {
    query.set("offset", String(params.offset))
  }
  const suffix = query.size > 0 ? `?${query.toString()}` : ""
  const response = await fetch(`${ARCHIVE_REVIEW_ENDPOINTS.cases}${suffix}`, {
    cache: "no-store",
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(readApiErrorMessage(payload) ?? `Failed to fetch archive review cases: ${response.status}`)
  }
  return decodeArchiveReviewListPayload(payload)
}

export async function fetchArchiveReviewCaseDetail(caseKey: string): Promise<ArchiveReviewDetailPayload> {
  const normalizedCaseKey = normalizeArchiveReviewCaseKey(caseKey)
  const response = await fetch(`${ARCHIVE_REVIEW_ENDPOINTS.cases}/${encodeURIComponent(normalizedCaseKey)}`, {
    cache: "no-store",
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(readApiErrorMessage(payload) ?? `Failed to fetch archive review case detail: ${response.status}`)
  }
  const detail = decodeArchiveReviewDetailPayload(payload)
  if (detail.case.case_key !== normalizedCaseKey) {
    throw new Error("Archive review case detail response case_key does not match the request.")
  }
  return detail
}

export async function postArchiveReviewStatus(
  caseKey: string,
  params: { review_status: ArchiveReviewStatus; allow_write: true },
): Promise<ArchiveReviewStatusResult> {
  const normalizedCaseKey = normalizeArchiveReviewCaseKey(caseKey)
  const response = await fetch(`${ARCHIVE_REVIEW_ENDPOINTS.cases}/${encodeURIComponent(normalizedCaseKey)}/status`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(params),
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(readApiErrorMessage(payload) ?? `Failed to update archive review status: ${response.status}`)
  }
  const result = decodeArchiveReviewStatusResult(payload)
  if (result.case_key !== normalizedCaseKey) {
    throw new Error("Archive review status response case_key does not match the request.")
  }
  if (result.review_status !== params.review_status) {
    throw new Error("Archive review status response review_status does not match the request.")
  }
  return result
}

export async function planArchiveReviewPromotion(
  caseKey: string,
  params: {
    target_storage_key: string
    selected_revisions: Record<string, number>
  },
) {
  const normalizedCaseKey = normalizeArchiveReviewCaseKey(caseKey)
  const normalizedStorageKey = params.target_storage_key.trim()
  const normalizedSelections = normalizeArchiveReviewSelectionRequest(params.selected_revisions)
  if (!normalizedStorageKey) {
    throw new Error("정본 recording storage_key를 입력하세요.")
  }
  const response = await fetch(
    `${ARCHIVE_REVIEW_ENDPOINTS.cases}/${encodeURIComponent(normalizedCaseKey)}/promotion/plan`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        target_storage_key: normalizedStorageKey,
        selected_revisions: normalizedSelections,
      }),
    },
  )
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(readApiErrorMessage(payload) ?? `Failed to plan archive review promotion: ${response.status}`)
  }
  const plan = decodeArchiveReviewPromotionPlan(payload)
  if (plan.case_key !== normalizedCaseKey) {
    throw new Error("Archive review promotion plan case_key does not match the request.")
  }
  if (plan.target_storage_key !== normalizedStorageKey) {
    throw new Error("Archive review promotion plan target storage key does not match the request.")
  }
  assertPlanMatchesRequest(plan, normalizedSelections)
  return plan
}

export async function applyArchiveReviewPromotion(
  caseKey: string,
  params: {
    target_storage_key: string
    selected_revisions: Record<string, number>
    expected_count: 1
    expected_plan_sha256: string
    allow_write: true
  },
): Promise<ArchiveReviewPromotionResult> {
  const normalizedCaseKey = normalizeArchiveReviewCaseKey(caseKey)
  const normalizedStorageKey = params.target_storage_key.trim()
  const normalizedSelections = normalizeArchiveReviewSelectionRequest(params.selected_revisions)
  if (!normalizedStorageKey) {
    throw new Error("정본 recording storage_key를 입력하세요.")
  }
  if (params.expected_count !== 1) {
    throw new Error("Archive review promotion expected_count must equal 1.")
  }
  if (!SHA256_DIGEST_PATTERN.test(params.expected_plan_sha256)) {
    throw new Error("Archive review promotion digest must be a lowercase sha256 hex string.")
  }
  const response = await fetch(
    `${ARCHIVE_REVIEW_ENDPOINTS.cases}/${encodeURIComponent(normalizedCaseKey)}/promotion/apply`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        ...params,
        target_storage_key: normalizedStorageKey,
        selected_revisions: normalizedSelections,
      }),
    },
  )
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(readApiErrorMessage(payload) ?? `Failed to apply archive review promotion: ${response.status}`)
  }
  const result = decodeArchiveReviewPromotionResult(payload)
  if (result.case_key !== normalizedCaseKey) {
    throw new Error("Archive review promotion response case_key does not match the request.")
  }
  if (result.target_storage_key !== normalizedStorageKey) {
    throw new Error("Archive review promotion response target storage key does not match the request.")
  }
  if (result.plan_sha256 !== params.expected_plan_sha256) {
    throw new Error("Archive review promotion response digest does not match the applied plan.")
  }
  if (result.selected_count !== Object.keys(normalizedSelections).length) {
    throw new Error("Archive review promotion response selected_count does not match the request.")
  }
  return result
}

export async function fetchClassificationProposals(
  params?: { status?: string; limit?: number; offset?: number },
): Promise<ClassificationProposalListPayload> {
  const query = new URLSearchParams()
  if (params?.status) {
    query.set("status", params.status)
  }
  if (typeof params?.limit === "number") {
    query.set("limit", String(params.limit))
  }
  if (typeof params?.offset === "number") {
    query.set("offset", String(params.offset))
  }
  const suffix = query.size > 0 ? `?${query.toString()}` : ""
  const response = await fetch(`${TIMETABLE_ENDPOINTS.classifications}${suffix}`, {
    cache: "no-store",
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(readApiErrorMessage(payload) ?? `Failed to fetch timetable classifications: ${response.status}`)
  }
  return decodeClassificationProposalListPayload(payload)
}

export async function fetchClassificationProposalDetail(
  proposalId: number,
): Promise<ClassificationProposalDetailPayload> {
  const response = await fetch(`${TIMETABLE_ENDPOINTS.classifications}/${proposalId}`, {
    cache: "no-store",
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(
      readApiErrorMessage(payload) ?? `Failed to fetch timetable classification detail: ${response.status}`,
    )
  }
  return decodeClassificationProposalDetailPayload(payload)
}

export async function postClassificationStatus(
  proposalId: number,
  params: { status: "rejected"; allow_write: true },
): Promise<ClassificationStatusResult> {
  const response = await fetch(`${TIMETABLE_ENDPOINTS.classifications}/${proposalId}/status`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(params),
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(readApiErrorMessage(payload) ?? `Failed to update classification status: ${response.status}`)
  }
  const result = decodeClassificationStatusResult(payload)
  if (result.proposal_id !== proposalId) {
    throw new Error("Classification status response proposal_id does not match the request.")
  }
  return result
}

export async function planClassificationConfirmation(
  proposalId: number,
): Promise<ClassificationConfirmationPlan> {
  const response = await fetch(`${TIMETABLE_ENDPOINTS.classifications}/${proposalId}/confirmation/plan`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: "{}",
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(readApiErrorMessage(payload) ?? `Failed to plan classification confirmation: ${response.status}`)
  }
  const plan = decodeClassificationConfirmationPlan(payload)
  if (plan.proposal_id !== proposalId) {
    throw new Error("Classification confirmation plan proposal_id does not match the request.")
  }
  return plan
}

export async function applyClassificationConfirmation(
  proposalId: number,
  params: { expected_count: 1; expected_plan_sha256: string; allow_write: true },
): Promise<ClassificationConfirmationResult> {
  if (params.expected_count !== 1) {
    throw new Error("Classification confirmation expected_count must equal 1.")
  }
  if (!SHA256_DIGEST_PATTERN.test(params.expected_plan_sha256)) {
    throw new Error("Classification confirmation digest must be a lowercase sha256 hex string.")
  }
  const response = await fetch(`${TIMETABLE_ENDPOINTS.classifications}/${proposalId}/confirmation/apply`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(params),
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(readApiErrorMessage(payload) ?? `Failed to apply classification confirmation: ${response.status}`)
  }
  const result = decodeClassificationConfirmationResult(payload)
  if (result.proposal_id !== proposalId) {
    throw new Error("Classification confirmation response proposal_id does not match the request.")
  }
  if (result.plan_sha256 !== params.expected_plan_sha256) {
    throw new Error("Classification confirmation response digest does not match the applied plan.")
  }
  return result
}

export async function fetchTitleSuggestions(
  params?: {
    status?: TitleSuggestionStatus
    limit?: number
    offset?: number
  },
  signal?: AbortSignal,
): Promise<TitleSuggestionListPayload> {
  const query = new URLSearchParams()
  if (params?.status !== undefined) {
    if (!["suggested", "confirmed", "rejected"].includes(params.status)) {
      throw new Error("Unsupported title suggestion status.")
    }
    query.set("status", params.status)
  }
  if (params?.limit !== undefined) {
    const limit = normalizeNonNegativeInteger(params.limit, "Title suggestion limit")
    if (limit > 200) {
      throw new Error("Title suggestion limit must be less than or equal to 200.")
    }
    query.set("limit", String(limit))
  }
  if (params?.offset !== undefined) {
    const offset = normalizeNonNegativeInteger(params.offset, "Title suggestion offset")
    if (offset > 100_000) {
      throw new Error("Title suggestion offset must be less than or equal to 100000.")
    }
    query.set("offset", String(offset))
  }
  const suffix = query.size > 0 ? `?${query.toString()}` : ""
  const response = await fetch(`${TITLE_SUGGESTION_ENDPOINT}${suffix}`, {
    cache: "no-store",
    signal,
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(
      readApiErrorMessage(payload)
      ?? `Failed to fetch title suggestions: ${response.status}`,
    )
  }
  const decoded = decodeTitleSuggestionList(payload)
  if (
    params?.limit !== undefined
    && decoded.proposals.length > params.limit
  ) {
    throw new Error("Title suggestion response exceeds the requested limit.")
  }
  if (
    params?.status !== undefined
    && (
      decoded.proposals.some((proposal) => proposal.status !== params.status)
      || decoded.total !== decoded.counts[params.status]
    )
  ) {
    throw new Error("Title suggestion response does not match the requested status.")
  }
  return decoded
}

export async function fetchTitleSuggestionDetail(
  proposalId: number,
  signal?: AbortSignal,
): Promise<TitleSuggestionDetailPayload> {
  const normalizedProposalId = normalizePositiveInteger(
    proposalId,
    "Title suggestion proposal id",
  )
  const response = await fetch(
    `${TITLE_SUGGESTION_ENDPOINT}/${normalizedProposalId}`,
    {
      cache: "no-store",
      signal,
    },
  )
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(
      readApiErrorMessage(payload)
      ?? `Failed to fetch title suggestion detail: ${response.status}`,
    )
  }
  const decoded = decodeTitleSuggestionDetail(payload)
  if (decoded.proposal.id !== normalizedProposalId) {
    throw new Error("Title suggestion detail proposal_id does not match the request.")
  }
  return decoded
}

export async function postTitleSuggestionStatus(
  proposalId: number,
  params: { status: "rejected"; allow_write: true },
): Promise<TitleSuggestionStatusResult> {
  const normalizedProposalId = normalizePositiveInteger(
    proposalId,
    "Title suggestion proposal id",
  )
  if (params.status !== "rejected" || params.allow_write !== true) {
    throw new Error(
      "Title suggestion status update requires rejected status and allow_write=true.",
    )
  }
  const response = await fetch(
    `${TITLE_SUGGESTION_ENDPOINT}/${normalizedProposalId}/status`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(params),
    },
  )
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(
      readApiErrorMessage(payload)
      ?? `Failed to update title suggestion status: ${response.status}`,
    )
  }
  const decoded = decodeTitleSuggestionStatusResult(payload)
  if (decoded.proposal_id !== normalizedProposalId) {
    throw new Error("Title suggestion status proposal_id does not match the request.")
  }
  return decoded
}

export async function planTitleSuggestionConfirmation(
  proposalId: number,
  signal?: AbortSignal,
): Promise<TitleSuggestionConfirmationPlan> {
  const normalizedProposalId = normalizePositiveInteger(
    proposalId,
    "Title suggestion proposal id",
  )
  const response = await fetch(
    `${TITLE_SUGGESTION_ENDPOINT}/${normalizedProposalId}/confirmation/plan`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: "{}",
      signal,
    },
  )
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(
      readApiErrorMessage(payload)
      ?? `Failed to plan title suggestion confirmation: ${response.status}`,
    )
  }
  const decoded = decodeTitleSuggestionConfirmationPlan(payload)
  if (decoded.proposal_id !== normalizedProposalId) {
    throw new Error("Title suggestion confirmation plan proposal_id does not match the request.")
  }
  return decoded
}

export async function applyTitleSuggestionConfirmation(
  proposalId: number,
  params: {
    expected_count: 1
    expected_plan_sha256: string
    allow_write: true
  },
): Promise<TitleSuggestionConfirmationResult> {
  const normalizedProposalId = normalizePositiveInteger(
    proposalId,
    "Title suggestion proposal id",
  )
  if (params.expected_count !== 1) {
    throw new Error("Title suggestion confirmation expected_count must equal 1.")
  }
  if (params.allow_write !== true) {
    throw new Error("Title suggestion confirmation requires allow_write=true.")
  }
  if (!SHA256_DIGEST_PATTERN.test(params.expected_plan_sha256)) {
    throw new Error(
      "Title suggestion confirmation digest must be a lowercase sha256 hex string.",
    )
  }
  const response = await fetch(
    `${TITLE_SUGGESTION_ENDPOINT}/${normalizedProposalId}/confirmation/apply`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(params),
    },
  )
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(
      readApiErrorMessage(payload)
      ?? `Failed to apply title suggestion confirmation: ${response.status}`,
    )
  }
  const decoded = decodeTitleSuggestionConfirmationResult(payload)
  if (decoded.proposal_id !== normalizedProposalId) {
    throw new Error("Title suggestion confirmation response proposal_id does not match the request.")
  }
  if (decoded.plan_sha256 !== params.expected_plan_sha256) {
    throw new Error("Title suggestion confirmation response digest does not match the applied plan.")
  }
  return decoded
}
