export type RuntimeStatus = "running" | "paused" | "stopped"
export type RuntimeSource = "web" | "external" | "none"
export type NotificationSelection = "telegram" | "discord" | "both" | "disabled"
export const PANEL_ACTIONS = [
  "start",
  "pause",
  "resume",
  "stop",
  "refresh",
  "exit",
  "clear_history",
] as const

export type PanelAction = (typeof PANEL_ACTIONS)[number]
export type PanelEndpointKey = "state" | "logs" | "notification" | PanelAction
export type KnownJobStatus = "PENDING" | "PROCESSING" | "DONE" | "ERROR" | "NEEDS_REVIEW"
export type JobStatus = KnownJobStatus | (string & {})

export interface CountSummary {
  PENDING: number
  PROCESSING: number
  NEEDS_REVIEW: number
  DONE: number
  ERROR: number
  UNREGISTERED: number
}

export interface FolderSummary {
  inbox: string
  audio: string
  transcripts: string
  errors: string
}

export interface RuntimeState {
  status: RuntimeStatus
  source: RuntimeSource
  paused: boolean
  managed_running: boolean
  external_worker_pids: number[]
  label: string
  description: string
}

export interface PanelJob {
  id: number
  status: JobStatus
  file_name: string
  updated_at: string
  step: string
  progress_pct: number
  error_message: string
}

export interface ProcessingJob {
  id: number
  file_name: string
  step: string
  progress_pct: number
  eta_sec: number | null
  eta_label: string
}

export interface PanelEndpoints {
  state: string
  logs: string
  notification: string
  start: string
  pause: string
  resume: string
  stop: string
  refresh: string
  exit: string
  clear_history: string
}

export interface ActionSummary {
  pause_action: "pause" | "resume"
  pause_label: string
  endpoints: PanelEndpoints
}

export interface NotificationOption {
  id: NotificationSelection
  label: string
  description: string
  available: boolean
}

export interface NotificationState {
  selection: NotificationSelection
  selected_label: string
  apply_label: string
  restart_required: boolean
  can_apply_now: boolean
  options: NotificationOption[]
}

export interface PanelSummary {
  current_job: ProcessingJob | null
  progress_label: string
  eta_label: string
  notice: string
  refresh_hint: string
  updated_at: string
  poll_interval_sec: number
}

export interface PanelState {
  schema_version: 2
  runtime_state: RuntimeState
  notification: NotificationState
  counts: CountSummary
  folders: FolderSummary
  actions: ActionSummary
  summary: PanelSummary
  jobs_v2: PanelJob[]
  processing_v2: ProcessingJob[]
}

export interface LogPayload {
  offset: number
  text: string
}

export interface PanelStreamLogPayload extends LogPayload {
  reset: boolean
}

export type PanelEvent =
  | {
      type: "state"
      state: PanelState
    }
  | {
      type: "log_chunk"
      payload: LogPayload
    }
  | {
      type: "log_reset"
      payload: PanelStreamLogPayload
    }
  | {
      type: "heartbeat"
    }

export type PanelActionResponse =
  | {
      ok: true
      notice?: string
    }
  | {
      ok: false
      error: string
    }

export interface TimetableCapabilities {
  confirmations_enabled: boolean
  status_writes_enabled: boolean
}

export interface TimetableEntry {
  id: number
  entry_key: string
  semester: string
  course_name: string
  course_code: string | null
  weekday: "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun"
  start_time: string
  end_time: string
  period_label: string
  period_index: number | null
  classroom: string
}

export interface TimetableEntriesPayload {
  schema_version: "storage-v2/timetable-list@1"
  available: boolean
  disabled_reason?: string
  semester?: string | null
  total: number
  entries: TimetableEntry[]
  capabilities?: TimetableCapabilities
}

export type ClassificationReason =
  | "unique_time_match"
  | "recorded_at_missing"
  | "recorded_at_invalid"
  | "no_time_match"
  | "ambiguous_time_match"

export type ClassificationProposalStatus = "suggested" | "confirmed" | "rejected"
export type ReviewStatus = "open" | "triaged" | "resolved" | "dismissed" | (string & {})

export interface ClassificationProposalListItem {
  id: number
  storage_key: string
  status: ClassificationProposalStatus
  classification_reason: ClassificationReason
  proposed_title: string
  context_type: string
  semester: string
  course_name: string | null
  course_code: string | null
  session_date: string | null
  weekday: TimetableEntry["weekday"] | null
  start_time: string | null
  end_time: string | null
  period_label: string | null
  period_index: number | null
  classroom: string | null
  confidence: number | null
  review_status: ReviewStatus | null
  created_at: string
  updated_at: string
  confirmed_at: string | null
}

export interface ClassificationProposalCounts {
  suggested: number
  confirmed: number
  rejected: number
}

export interface ClassificationProposalListPayload {
  schema_version: "storage-v2/classification-proposal-list@1"
  available: boolean
  disabled_reason?: string
  counts: ClassificationProposalCounts
  total: number
  proposals: ClassificationProposalListItem[]
  capabilities?: TimetableCapabilities
}

export interface ClassificationProposalDetail {
  id: number
  storage_key: string
  status: ClassificationProposalStatus
  classification_reason: ClassificationReason
  proposed_title: string
  context_type: string
  label: string | null
  semester: string
  course_name: string | null
  course_code: string | null
  session_date: string | null
  weekday: TimetableEntry["weekday"] | null
  start_time: string | null
  end_time: string | null
  period_label: string | null
  period_index: number | null
  classroom: string | null
  confidence: number | null
  review_status: ReviewStatus | null
  review_reason_code: string | null
  created_at: string
  updated_at: string
  confirmed_at: string | null
}

export interface ClassificationProposalDetailPayload {
  schema_version: "storage-v2/classification-proposal-detail@1"
  proposal: ClassificationProposalDetail
  candidate_entry_keys: string[]
  canonical_metadata_changed: boolean
  capabilities?: TimetableCapabilities
}

export interface ClassificationStatusResult {
  schema_version: "storage-v2/classification-status-result@1"
  ok: true
  action: "rejected" | "skipped"
  proposal_id: number
  status: "rejected"
  review_status: "dismissed"
  canonical_metadata_changed: false
}

export interface ClassificationConfirmationPlan {
  schema_version: "storage-v2/classification-confirmation-plan@1"
  proposal_id: number
  storage_key: string
  proposal_updated_at: string
  review_status: ReviewStatus
  semester: string
  course_name: string
  session_date: string
  period_label: string | null
  proposed_title: string
  expected_count: 1
  materialization: "confirmation_audit_only"
  mode: "read_only"
  plan_sha256: string
  canonical_metadata_changed: false
}

export interface ClassificationConfirmationResult {
  schema_version: "storage-v2/classification-confirmation-result@1"
  ok: true
  action: "confirmed" | "skipped"
  proposal_id: number
  status: "confirmed"
  plan_sha256: string
  canonical_metadata_changed: false
}

export type TitleSuggestionStatus = "suggested" | "confirmed" | "rejected"
export type TitleSuggestionReason = "schedule_content_match" | "content_topic"
export type TitleSuggestionContextType =
  | "general"
  | "class_session"
  | "daily_note"
  | "meeting"
  | "memo"
export type TitleSuggestionClassificationStatus =
  | "suggested"
  | "confirmed"
  | "rejected"

export interface TitleSuggestionCapabilities {
  confirmations_enabled: boolean
  status_writes_enabled: boolean
}

export interface TitleSuggestionProposal {
  id: number
  storage_key: string
  status: TitleSuggestionStatus
  suggestion_reason: TitleSuggestionReason
  proposed_title: string
  confidence: number
  generator_version: "deterministic-keywords-v1"
  review_status: "open" | "triaged" | "resolved" | "dismissed"
  transcript_revision: number
  classification_status: TitleSuggestionClassificationStatus | null
  context_type: TitleSuggestionContextType | null
  created_at: string
  updated_at: string
  confirmed_at: string | null
}

export interface TitleSuggestionListItem extends TitleSuggestionProposal {
  canonical_metadata_changed: false
}

export interface TitleSuggestionCounts {
  suggested: number
  confirmed: number
  rejected: number
}

export interface TitleSuggestionListPayload {
  schema_version: "storage-v2/title-suggestion-list@1"
  available: boolean
  disabled_reason?: string
  counts: TitleSuggestionCounts
  total: number
  proposals: TitleSuggestionListItem[]
  capabilities: TitleSuggestionCapabilities
}

export interface TitleSuggestionLinkedClassification {
  status: TitleSuggestionClassificationStatus
  classification_reason: "unique_time_match"
  proposed_title: string
  course_name: string | null
  course_code: string | null
}

export interface TitleSuggestionDetailPayload {
  schema_version: "storage-v2/title-suggestion-detail@1"
  proposal: TitleSuggestionProposal
  linked_classification: TitleSuggestionLinkedClassification | null
  canonical_metadata_changed: false
  capabilities: TitleSuggestionCapabilities
}

export interface TitleSuggestionStatusResult {
  schema_version: "storage-v2/title-suggestion-status-result@1"
  ok: true
  action: "rejected" | "skipped"
  proposal_id: number
  status: "rejected"
  review_status: "dismissed"
  canonical_metadata_changed: false
}

export interface TitleSuggestionConfirmationPlan {
  schema_version: "storage-v2/title-suggestion-confirmation-plan@1"
  proposal_id: number
  storage_key: string
  suggestion_reason: TitleSuggestionReason
  proposed_title: string
  transcript_revision: number
  expected_count: 1
  materialization: "confirmation_audit_only"
  mode: "read_only"
  plan_sha256: string
  canonical_metadata_changed: false
}

export interface TitleSuggestionConfirmationResult {
  schema_version: "storage-v2/title-suggestion-confirmation-result@1"
  ok: true
  action: "confirmed" | "skipped"
  proposal_id: number
  status: "confirmed"
  plan_sha256: string
  canonical_metadata_changed: false
}

export type UnifiedReviewFeedSource =
  | "archive"
  | "timetable"
  | "title"
  | "recording"
export type UnifiedReviewFeedSourceStatus = "ready" | "unavailable"

export interface UnifiedReviewFeedItemPayload {
  id: string
  source: UnifiedReviewFeedSource
  title: string
  state_label: string
  reason_label: string
  timestamp: string
  meta_label: string
  href: string
}

export interface UnifiedReviewFeedSourceLedgerPayload {
  source: UnifiedReviewFeedSource
  label: string
  status: UnifiedReviewFeedSourceStatus
  available: boolean
  visible_count: number
  total_count: number | null
  truncated: boolean
  note: string
  error: null
}

export interface UnifiedReviewFeedPayload {
  schema_version: "storage-v2/unified-review-feed@2"
  available: boolean
  filters: {
    limit: number
    offset: number
  }
  total: number
  items: UnifiedReviewFeedItemPayload[]
  sources: UnifiedReviewFeedSourceLedgerPayload[]
}

export type ArchiveReviewStatus = "open" | "triaged" | "resolved" | "dismissed"
export type ArchiveReviewArtifactKind =
  | "correction_text"
  | "correction_json"
  | "summary_markdown"
export type ArchiveSuggestionStatus = "suggested" | "unresolved"
export type ArchiveSuggestionReasonCode =
  | "unique_matches_ledger"
  | "multiple_ledger_matches"
  | "unique_latest_capture_current"
  | "multiple_latest_capture_current"
  | "no_unique_current_candidate"
export type ArchiveLatestClassification =
  | "pending"
  | "candidate"
  | "matched"
  | "ownerless"
  | "blocked"
  | (string & {})

export interface ArchiveReviewCapabilities {
  status_writes_enabled: boolean
  promotions_enabled: boolean
}

export interface ArchiveReviewCaseListItem {
  case_key: string
  logical_stem: string
  subject_abbr: string | null
  review_status: ArchiveReviewStatus
  promoted_recording_id: number | null
  promoted_storage_key: string | null
  created_at: string
  updated_at: string
  resolved_at: string | null
  capture_count: number
  revision_count: number
  last_captured_at: string | null
  latest_classification: ArchiveLatestClassification | null
}

export interface ArchiveReviewListCounts {
  open: number
  triaged: number
  resolved: number
  dismissed: number
}

export interface ArchiveReviewListPayload {
  schema_version: "storage-v2/archive-review-list@1"
  available: boolean
  disabled_reason?: string
  filters: {
    review_status: ArchiveReviewStatus | null
    limit: number
    offset: number
  }
  counts: ArchiveReviewListCounts
  total: number
  cases: ArchiveReviewCaseListItem[]
  capabilities?: ArchiveReviewCapabilities
}

export interface ArchiveReviewCaseDetail {
  case_key: string
  legacy_delivery_key: string
  logical_stem: string
  subject_abbr: string | null
  review_status: ArchiveReviewStatus
  promoted_recording_id: number | null
  promoted_storage_key: string | null
  created_at: string
  updated_at: string
  resolved_at: string | null
}

export interface ArchiveReviewCapture {
  capture_key: string
  reconciliation_classification: string
  captured_at: string
}

export interface ArchiveReviewRevision {
  revision_id: number
  artifact_kind: ArchiveReviewArtifactKind
  content_sha256: string
  bytes: number
  mime_type: string | null
  created_at: string
  observation_count: number
  capture_count: number
  last_observed_at: string | null
  matches_ledger_count: number
  differs_from_ledger_count: number
  unclaimed_count: number
  unexpected_current_count: number
  source_roles: string[]
  relationships: string[]
}

export interface ArchiveReviewCanonicalSuggestion {
  artifact_kind: ArchiveReviewArtifactKind
  status: ArchiveSuggestionStatus
  revision_id: number | null
  content_sha256: string | null
  reason_code: ArchiveSuggestionReasonCode
  candidate_revision_ids: number[]
}

export interface ArchiveReviewConfirmedSelection {
  artifact_kind: ArchiveReviewArtifactKind
  revision_id: number
  content_sha256: string
  bytes: number
  mime_type: string | null
  promotion_plan_sha256: string
  confirmed_at: string
}

export interface ArchiveReviewDetailPayload {
  schema_version: "storage-v2/archive-review-detail@1"
  case: ArchiveReviewCaseDetail
  latest_capture: ArchiveReviewCapture | null
  capture_count: number
  captures_truncated: boolean
  revision_count: number
  captures: ArchiveReviewCapture[]
  revisions: ArchiveReviewRevision[]
  canonical_suggestions: ArchiveReviewCanonicalSuggestion[]
  confirmed_selections: ArchiveReviewConfirmedSelection[]
  capabilities?: ArchiveReviewCapabilities
}

export interface ArchiveReviewStatusResult {
  schema_version: "storage-v2/archive-review-status@1"
  ok: true
  case_key: string
  review_status: ArchiveReviewStatus
  changed: boolean
}

export type ArchiveReviewPromotionSelectionMap = Partial<
  Record<ArchiveReviewArtifactKind, number>
>
export type ArchiveReviewPromotionSelectionRequest = Record<
  ArchiveReviewArtifactKind,
  number
>

export interface ArchiveReviewPromotionPlanSelection {
  artifact_kind: ArchiveReviewArtifactKind
  revision_id: number
  content_sha256: string
  bytes: number
  mime_type: string | null
}

export interface ArchiveReviewPromotionPlan {
  schema_version: "storage-v2/archive-review-promotion-plan@1"
  case_key: string
  case_updated_at: string
  review_status: ArchiveReviewStatus
  target_recording_id: number
  target_storage_key: string
  latest_capture_key: string
  latest_capture_classification: string
  selected_revisions: ArchiveReviewPromotionPlanSelection[]
  expected_count: 1
  mode: "read_only"
  plan_sha256: string
}

export interface ArchiveReviewPromotionResult {
  schema_version: "storage-v2/archive-review-promotion-result@1"
  ok: true
  status: "promoted" | "skipped"
  case_key: string
  target_storage_key: string
  plan_sha256: string
  selected_count: number
}

export type RecordingSourceState = "available" | "missing"
export type RecordingRecordedAtSource =
  | "audio_metadata"
  | "file_created_at"
  | "received_at"
  | "manual"
  | "legacy_import"
export type RecordingTitleSource =
  | "manual"
  | "schedule"
  | "filename_inference"
  | "legacy_import"
  | "system"
export type RecordingContextType =
  | "general"
  | "class_session"
  | "daily_note"
  | "meeting"
  | "memo"
export type RecordingContextSource =
  | "manual"
  | "schedule_import"
  | "filename_inference"
  | "legacy_import"
  | "system"
export type RecordingLibraryJobStatus =
  | "queued"
  | "processing"
  | "done"
  | "needs_review"
  | "error"
  | "canceled"
export type RecordingReviewSeverity = "low" | "medium" | "high"
export type RecordingArtifactStage =
  | "source"
  | "transcript"
  | "correction"
  | "summary"
  | "supporting"
export type RecordingArtifactKind =
  | "source_copy"
  | "transcript_raw_text"
  | "transcript_segments_json"
  | "quality_scorecard"
  | "correction_text"
  | "correction_json"
  | "summary_markdown"
  | "summary_json"
  | "metadata"
  | "log"
  | "other"

export interface RecordingTitleMetadata {
  title: string
  source: RecordingTitleSource
}

export interface RecordingContextMetadata {
  context_type: RecordingContextType
  label: string | null
  source: RecordingContextSource
  semester: string | null
  course_name: string | null
  course_code: string | null
  session_date: string | null
  period_label: string | null
  period_index: number | null
}

export interface RecordingSourceMetadata {
  state: RecordingSourceState
  bytes: number | null
  mime_type: string | null
  recorded_at: string | null
  received_at: string
}

export interface RecordingCurrentJobSummary {
  job_key: string
  status: RecordingLibraryJobStatus
  progress: number
  requested_profile: string | null
  requested_profile_version: string | null
  queued_at: string
  finished_at: string | null
}

export interface RecordingLibrarySummary {
  storage_key: string
  display_name: string
  original_name_nfc: string
  source_state: RecordingSourceState
  recorded_at: string | null
  received_at: string
  current_title: RecordingTitleMetadata | null
  selected_context: RecordingContextMetadata | null
  current_job: RecordingCurrentJobSummary | null
  open_review_count: number
  artifact_count: number
}

export interface RecordingLibraryListCounts {
  recordings: number
  queued: number
  processing: number
  done: number
  needs_review: number
  error: number
  canceled: number
  open_reviews: number
}

export interface RecordingLibraryListPayload {
  schema_version: "storage-v2/recording-library-list@1"
  available: boolean
  disabled_reason?: string
  filters: {
    limit: number
    offset: number
  }
  counts: RecordingLibraryListCounts
  total: number
  summaries: RecordingLibrarySummary[]
}

export interface RecordingDetailArtifact {
  stage: RecordingArtifactStage
  artifact_kind: RecordingArtifactKind
  revision: number
  is_latest: boolean
  bytes: number | null
  mime_type: string | null
  created_at: string
}

export interface RecordingDetailJob {
  job_key: string
  status: RecordingLibraryJobStatus
  progress: number
  is_current: boolean
  requested_profile: string | null
  requested_profile_version: string | null
  queued_at: string
  started_at: string | null
  finished_at: string | null
  artifacts: RecordingDetailArtifact[]
}

export interface RecordingDetailReview {
  status: ArchiveReviewStatus
  severity: RecordingReviewSeverity | null
  reason_code: string
  job_key: string | null
  artifact_kind: RecordingArtifactKind | null
  artifact_revision: number | null
  created_at: string
  resolved_at: string | null
}

export interface RecordingDetailCounts {
  jobs: number
  artifacts: number
  reviews: number
  open_reviews: number
}

export interface RecordingDetailLimits {
  jobs: number
  artifacts: number
  reviews: number
  jobs_truncated: boolean
  artifacts_truncated: boolean
  reviews_truncated: boolean
}

export interface RecordingDetailRecording {
  storage_key: string
  display_name: string
  original_name_nfc: string
  current_title: RecordingTitleMetadata | null
  selected_context: RecordingContextMetadata | null
  source: RecordingSourceMetadata
}

export interface RecordingDetailPayload {
  schema_version: "storage-v2/recording-detail@1"
  available: boolean
  disabled_reason?: string
  recording: RecordingDetailRecording
  counts: RecordingDetailCounts
  limits: RecordingDetailLimits
  jobs: RecordingDetailJob[]
  reviews: RecordingDetailReview[]
}

export type TranscriptionAnalyticsPeriod = "day" | "week" | "month"
export type TranscriptionAnalyticsBucket = "hour" | "day"
export type TranscriptionAnalyticsStatus =
  | "queued"
  | "processing"
  | "done"
  | "needs_review"
  | "error"
  | "canceled"
export type TranscriptionAnalyticsHealth = "good" | "warn" | "bad"
export type TranscriptionQualityBand =
  | "90-100"
  | "80-89"
  | "70-79"
  | "60-69"
  | "0-59"
  | "unscored"

export interface TranscriptionAnalyticsWindow {
  start_at: string
  end_at: string
  bucket: TranscriptionAnalyticsBucket
  label: string
}

export interface TranscriptionAnalyticsFreshness {
  generated_at: string
  latest_event_at: string | null
}

export interface TranscriptionAnalyticsLimits {
  row_limit: number
  scorecard_max_bytes: number
  truncated: boolean
}

export interface TranscriptionAnalyticsCoverage {
  jobs_total: number
  quality_scored: number
  quality_missing: number
  quality_invalid: number
  classification_known: number
  classification_unclassified: number
  audio_duration_known: number
  processing_duration_known: number
  event_time_recorded: number
  event_time_received: number
  event_time_queued: number
  event_time_invalid: number
}

export interface TranscriptionAnalyticsTotals {
  jobs: number
  queued: number
  processing: number
  done: number
  needs_review: number
  error: number
  canceled: number
  average_quality_score: number | null
  audio_duration_sec: number | null
  total_processing_sec: number | null
}

export interface TranscriptionAnalyticsTimelinePoint {
  bucket_start: string
  label: string
  jobs: number
  done: number
  needs_review: number
  error: number
  quality_scored: number
  average_quality_score: number | null
}

export interface TranscriptionQualityDistribution {
  band: TranscriptionQualityBand
  label: string
  min: number | null
  max: number | null
  count: number
}

export interface TranscriptionStatusDistribution {
  status: TranscriptionAnalyticsStatus
  count: number
}

export interface TranscriptionClassificationDistribution {
  context_type: string | null
  source: string | null
  label: string
  count: number
}

export interface TranscriptionAnalyticsAttentionItem {
  storage_key: string
  display_name: string
  status: TranscriptionAnalyticsStatus
  event_at: string
  quality_score: number | null
  health: TranscriptionAnalyticsHealth | null
  context_type: string | null
  classification_source: string | null
}

export interface TranscriptionAnalyticsPayload {
  schema_version: "storage-v2/transcription-analytics@1"
  available: boolean
  disabled_reason?: string
  period: TranscriptionAnalyticsPeriod
  timezone: string
  window: TranscriptionAnalyticsWindow
  freshness: TranscriptionAnalyticsFreshness
  limits: TranscriptionAnalyticsLimits
  coverage: TranscriptionAnalyticsCoverage
  totals: TranscriptionAnalyticsTotals
  timeline: TranscriptionAnalyticsTimelinePoint[]
  quality_distribution: TranscriptionQualityDistribution[]
  status_distribution: TranscriptionStatusDistribution[]
  classification_distribution: TranscriptionClassificationDistribution[]
  recent_attention: TranscriptionAnalyticsAttentionItem[]
}
