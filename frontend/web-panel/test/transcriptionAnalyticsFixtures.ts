import type {
  TranscriptionAnalyticsPayload,
  TranscriptionAnalyticsPeriod,
  TranscriptionAnalyticsTimelinePoint,
} from "../src/types"

const PERIOD_LENGTHS: Record<TranscriptionAnalyticsPeriod, number> = {
  day: 24,
  week: 7,
  month: 30,
}

const PERIOD_LABELS: Record<TranscriptionAnalyticsPeriod, string> = {
  day: "오늘",
  week: "최근 7일",
  month: "최근 30일",
}

function buildTimeline(
  period: TranscriptionAnalyticsPeriod,
): TranscriptionAnalyticsTimelinePoint[] {
  const stepMs = period === "day" ? 60 * 60 * 1000 : 24 * 60 * 60 * 1000
  const baseMs = Date.parse("2026-07-01T00:00:00+09:00")

  return Array.from({ length: PERIOD_LENGTHS[period] }, (_, index) => {
    const emptyPoint: TranscriptionAnalyticsTimelinePoint = {
      bucket_start: new Date(baseMs + index * stepMs).toISOString(),
      label:
        period === "day"
          ? `${String(index).padStart(2, "0")}시`
          : `7월 ${index + 1}일`,
      jobs: 0,
      done: 0,
      needs_review: 0,
      error: 0,
      quality_scored: 0,
      average_quality_score: null,
    }

    if (index === 0) {
      return {
        ...emptyPoint,
        jobs: 2,
        done: 1,
        needs_review: 1,
        quality_scored: 2,
        average_quality_score: 85,
      }
    }
    if (index === 1) {
      return {
        ...emptyPoint,
        jobs: 1,
        done: 1,
        quality_scored: 1,
        average_quality_score: 75,
      }
    }
    if (index === 2) {
      return {
        ...emptyPoint,
        jobs: 1,
        error: 1,
      }
    }
    return emptyPoint
  })
}

export function buildTranscriptionAnalyticsPayload(
  period: TranscriptionAnalyticsPeriod = "week",
  available = true,
): TranscriptionAnalyticsPayload {
  const timeline = buildTimeline(period)
  const label = PERIOD_LABELS[period]

  return {
    schema_version: "storage-v2/transcription-analytics@1",
    available,
    ...(available ? {} : { disabled_reason: "feature_disabled" }),
    period,
    timezone: "Asia/Seoul",
    window: {
      start_at: timeline[0].bucket_start,
      end_at: timeline[timeline.length - 1].bucket_start,
      bucket: period === "day" ? "hour" : "day",
      label,
    },
    freshness: {
      generated_at: "2026-07-23T10:00:00+09:00",
      latest_event_at: timeline[2].bucket_start,
    },
    limits: {
      row_limit: 500,
      scorecard_max_bytes: 64 * 1024,
      truncated: false,
    },
    coverage: {
      jobs_total: 4,
      quality_scored: 3,
      quality_missing: 1,
      quality_invalid: 0,
      classification_known: 3,
      classification_unclassified: 1,
      audio_duration_known: 3,
      processing_duration_known: 3,
      event_time_recorded: 2,
      event_time_received: 1,
      event_time_queued: 1,
      event_time_invalid: 0,
    },
    totals: {
      jobs: 4,
      queued: 0,
      processing: 0,
      done: 2,
      needs_review: 1,
      error: 1,
      canceled: 0,
      average_quality_score: 81.7,
      audio_duration_sec: 5400,
      total_processing_sec: 360,
    },
    timeline,
    quality_distribution: [
      {
        band: "90-100",
        label: "90–100점",
        min: 90,
        max: 100,
        count: 1,
      },
      {
        band: "80-89",
        label: "80–89점",
        min: 80,
        max: 89,
        count: 1,
      },
      {
        band: "70-79",
        label: "70–79점",
        min: 70,
        max: 79,
        count: 1,
      },
      {
        band: "60-69",
        label: "60–69점",
        min: 60,
        max: 69,
        count: 0,
      },
      {
        band: "0-59",
        label: "0–59점",
        min: 0,
        max: 59,
        count: 0,
      },
      {
        band: "unscored",
        label: "점수 없음",
        min: null,
        max: null,
        count: 1,
      },
    ],
    status_distribution: [
      { status: "queued", count: 0 },
      { status: "processing", count: 0 },
      { status: "done", count: 2 },
      { status: "needs_review", count: 1 },
      { status: "error", count: 1 },
      { status: "canceled", count: 0 },
    ],
    classification_distribution: [
      {
        context_type: "class_session",
        source: "schedule_import",
        label: "수업",
        count: 2,
      },
      {
        context_type: "meeting",
        source: "manual",
        label: "회의·대화",
        count: 1,
      },
      {
        context_type: null,
        source: null,
        label: "미분류",
        count: 1,
      },
    ],
    recent_attention: [
      {
        storage_key: "rec_review_01",
        display_name: "2026-07-23 자료구조 5교시",
        status: "needs_review",
        event_at: timeline[1].bucket_start,
        quality_score: 73,
        health: "warn",
        context_type: null,
        classification_source: null,
      },
      {
        storage_key: "rec_error_02",
        display_name: "프로젝트 회의 메모",
        status: "error",
        event_at: timeline[2].bucket_start,
        quality_score: null,
        health: null,
        context_type: "meeting",
        classification_source: "manual",
      },
    ],
  }
}
