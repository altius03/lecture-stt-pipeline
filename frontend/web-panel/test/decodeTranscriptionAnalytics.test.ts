import { describe, expect, it } from "vitest"

import { decodeTranscriptionAnalytics } from "../src/lib/decodeTranscriptionAnalytics"
import type {
  TranscriptionAnalyticsPayload,
  TranscriptionAnalyticsPeriod,
} from "../src/types"
import { buildTranscriptionAnalyticsPayload } from "./transcriptionAnalyticsFixtures"

function corruptedPayload(
  mutate: (payload: TranscriptionAnalyticsPayload) => void,
): TranscriptionAnalyticsPayload {
  const payload = structuredClone(buildTranscriptionAnalyticsPayload())
  mutate(payload)
  return payload
}

describe("decodeTranscriptionAnalytics", () => {
  it.each<TranscriptionAnalyticsPeriod>(["day", "week", "month"])(
    "accepts a strict valid %s payload",
    (period) => {
      const payload = buildTranscriptionAnalyticsPayload(period)

      expect(decodeTranscriptionAnalytics(payload)).toEqual(payload)
    },
  )

  it("accepts an explicit disabled payload without dropping its reason", () => {
    const payload = buildTranscriptionAnalyticsPayload("week", false)

    expect(decodeTranscriptionAnalytics(payload)).toEqual(payload)
  })

  it("requires a reason when analytics is disabled", () => {
    const payload = buildTranscriptionAnalyticsPayload("week", false)
    delete payload.disabled_reason

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      /disabled_reason/,
    )
  })

  it.each([
    [
      "schema",
      (payload: TranscriptionAnalyticsPayload) => {
        ;(payload as unknown as { schema_version: string }).schema_version =
          "storage-v2/transcription-analytics@2"
      },
      /schema_version/,
    ],
    [
      "period",
      (payload: TranscriptionAnalyticsPayload) => {
        ;(payload as unknown as { period: string }).period = "year"
      },
      /period/,
    ],
    [
      "period bucket",
      (payload: TranscriptionAnalyticsPayload) => {
        payload.window.bucket = "hour"
      },
      /bucket/,
    ],
  ])("rejects a malformed %s contract", (_, mutate, message) => {
    const payload = corruptedPayload(
      mutate as (payload: TranscriptionAnalyticsPayload) => void,
    )

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      message as RegExp,
    )
  })

  it("rejects a timeline with the wrong bucket count", () => {
    const payload = corruptedPayload((candidate) => {
      candidate.timeline.pop()
    })

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      /timeline bucket 수/,
    )
  })

  it("rejects a timeline that is not in ascending order", () => {
    const payload = corruptedPayload((candidate) => {
      const first = candidate.timeline[0]
      candidate.timeline[0] = candidate.timeline[1]
      candidate.timeline[1] = first
    })

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(/오름차순/)
  })

  it("rejects a quality label that does not match its band", () => {
    const payload = corruptedPayload((candidate) => {
      candidate.quality_distribution[0].label = "90점 이상"
    })

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      /quality_distribution label/,
    )
  })

  it("rejects a classification label that does not match its context", () => {
    const payload = corruptedPayload((candidate) => {
      candidate.classification_distribution[1].label = "회의"
    })

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      /classification_distribution.*label/,
    )
  })

  it.each([
    [
      "negative",
      (payload: TranscriptionAnalyticsPayload) => {
        payload.totals.jobs = -1
      },
    ],
    [
      "boolean",
      (payload: TranscriptionAnalyticsPayload) => {
        ;(
          payload.status_distribution[0] as unknown as { count: unknown }
        ).count = true
      },
    ],
  ])("rejects %s aggregate counts", (_, mutate) => {
    const payload = corruptedPayload(
      mutate as (payload: TranscriptionAnalyticsPayload) => void,
    )

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      /0 이상의 정수/,
    )
  })

  it.each([
    [
      "quality score",
      (payload: TranscriptionAnalyticsPayload) => {
        payload.totals.average_quality_score = 101
      },
      /0~100/,
    ],
    [
      "attention health",
      (payload: TranscriptionAnalyticsPayload) => {
        ;(
          payload.recent_attention[0] as unknown as { health: string }
        ).health = "unknown"
      },
      /health/,
    ],
    [
      "storage key",
      (payload: TranscriptionAnalyticsPayload) => {
        payload.recent_attention[0].storage_key = "private/path.wav"
      },
      /storage_key/,
    ],
    [
      "fractional attention quality score",
      (payload: TranscriptionAnalyticsPayload) => {
        payload.recent_attention[0].quality_score = 73.4
      },
      /정수/,
    ],
    [
      "attention quality and health mismatch",
      (payload: TranscriptionAnalyticsPayload) => {
        payload.recent_attention[0].quality_score = null
      },
      /품질 점수와 health 유무/,
    ],
    [
      "attention classification context and source mismatch",
      (payload: TranscriptionAnalyticsPayload) => {
        payload.recent_attention[1].classification_source = null
      },
      /분류 context와 source 유무/,
    ],
  ])("rejects a bad %s", (_, mutate, message) => {
    const payload = corruptedPayload(
      mutate as (payload: TranscriptionAnalyticsPayload) => void,
    )

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      message as RegExp,
    )
  })

  it.each([
    [
      "quality distribution",
      (payload: TranscriptionAnalyticsPayload) => {
        payload.quality_distribution[0].count += 1
      },
      /quality_distribution 합계/,
    ],
    [
      "status distribution",
      (payload: TranscriptionAnalyticsPayload) => {
        payload.status_distribution[0].count += 1
      },
      /status_distribution 합계/,
    ],
    [
      "classification distribution",
      (payload: TranscriptionAnalyticsPayload) => {
        payload.classification_distribution[0].count += 1
      },
      /classification_distribution 합계/,
    ],
  ])("rejects a %s sum mismatch", (_, mutate, message) => {
    const payload = corruptedPayload(
      mutate as (payload: TranscriptionAnalyticsPayload) => void,
    )

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      message as RegExp,
    )
  })

  it("rejects a coverage denominator mismatch", () => {
    const payload = corruptedPayload((candidate) => {
      candidate.coverage.quality_missing += 1
    })

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      /coverage 품질 분모/,
    )
  })

  it("rejects duration coverage above the job denominator", () => {
    const payload = corruptedPayload((candidate) => {
      candidate.coverage.audio_duration_known =
        candidate.coverage.jobs_total + 1
    })

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      /시간 표본 수/,
    )
  })

  it("rejects duration totals without a matching known sample", () => {
    const payload = corruptedPayload((candidate) => {
      candidate.coverage.processing_duration_known = 0
    })

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      /시간 합계와 시간 표본 수/,
    )
  })

  it("rejects a totals status sum mismatch", () => {
    const payload = corruptedPayload((candidate) => {
      candidate.totals.done += 1
    })

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      /totals 상태 합계/,
    )
  })

  it("rejects status counts assigned to the wrong status", () => {
    const payload = corruptedPayload((candidate) => {
      const done = candidate.status_distribution.find(
        (entry) => entry.status === "done",
      )
      const needsReview = candidate.status_distribution.find(
        (entry) => entry.status === "needs_review",
      )
      if (!done || !needsReview) {
        throw new Error("fixture status distribution is incomplete")
      }
      done.count -= 1
      needsReview.count += 1
    })

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      /상태별 값이 totals/,
    )
  })

  it("rejects timeline status counts assigned to the wrong series", () => {
    const payload = corruptedPayload((candidate) => {
      candidate.timeline[0].done -= 1
      candidate.timeline[0].needs_review += 1
    })

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      /timeline 상태\/품질 합계/,
    )
  })

  it("rejects quality bands assigned to the wrong coverage class", () => {
    const payload = corruptedPayload((candidate) => {
      candidate.quality_distribution[0].count -= 1
      candidate.quality_distribution[5].count += 1
    })

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      /품질 coverage/,
    )
  })

  it("rejects classification counts assigned to the wrong coverage class", () => {
    const payload = corruptedPayload((candidate) => {
      candidate.classification_distribution[1].count -= 1
      candidate.classification_distribution[2].count += 1
    })

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      /분류 coverage/,
    )
  })

  it("rejects duplicate classification context/source pairs", () => {
    const payload = corruptedPayload((candidate) => {
      candidate.classification_distribution[1].context_type = "class_session"
      candidate.classification_distribution[1].source = "schedule_import"
      candidate.classification_distribution[1].label = "수업"
    })

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(
      /context\/source pair.*중복/,
    )
  })

  it("requires exactly one unclassified classification bucket", () => {
    const payload = corruptedPayload((candidate) => {
      candidate.classification_distribution =
        candidate.classification_distribution.filter(
          (entry) => entry.context_type !== null,
        )
    })

    expect(() => decodeTranscriptionAnalytics(payload)).toThrow(/미분류 bucket/)
  })
})
