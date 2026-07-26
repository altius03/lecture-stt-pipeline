import { describe, expect, it } from "vitest"

import {
  decodeRecordingDetail,
  decodeRecordingLibraryList,
} from "../src/lib/decodeRecordingLibrary"
import type {
  RecordingDetailPayload,
  RecordingLibraryListPayload,
} from "../src/types"
import {
  buildRecordingDetailPayload,
  buildRecordingLibraryListPayload,
} from "./recordingLibraryFixtures"

function mutateList(
  mutate: (payload: RecordingLibraryListPayload) => void,
): RecordingLibraryListPayload {
  const payload = structuredClone(buildRecordingLibraryListPayload())
  mutate(payload)
  return payload
}

function mutateDetail(
  mutate: (payload: RecordingDetailPayload) => void,
): RecordingDetailPayload {
  const payload = structuredClone(buildRecordingDetailPayload())
  mutate(payload)
  return payload
}

describe("decodeRecordingLibrary", () => {
  it("accepts a valid list payload", () => {
    const payload = buildRecordingLibraryListPayload()
    expect(decodeRecordingLibraryList(payload)).toEqual(payload)
  })

  it("accepts a valid detail payload", () => {
    const payload = buildRecordingDetailPayload()
    expect(decodeRecordingDetail(payload)).toEqual(payload)
  })

  it("accepts backend optional text fields when they are blank strings", () => {
    const listPayload = mutateList((candidate) => {
      candidate.summaries[0].selected_context!.label = ""
      candidate.summaries[0].current_job!.requested_profile = ""
      candidate.summaries[0].current_job!.requested_profile_version = ""
    })
    const detailPayload = mutateDetail((candidate) => {
      candidate.recording.selected_context!.label = ""
      candidate.recording.source.mime_type = ""
      candidate.jobs[0].requested_profile = ""
      candidate.jobs[0].requested_profile_version = ""
    })

    expect(decodeRecordingLibraryList(listPayload)).toEqual(listPayload)
    expect(decodeRecordingDetail(detailPayload)).toEqual(detailPayload)
  })

  it("accepts an empty page when offset exceeds total", () => {
    const payload = buildRecordingLibraryListPayload()
    payload.filters.offset = 99
    payload.summaries = []

    expect(decodeRecordingLibraryList(payload)).toEqual(payload)
  })

  it("requires the exact disabled reason when unavailable", () => {
    const payload = buildRecordingLibraryListPayload(false)
    expect(decodeRecordingLibraryList(payload)).toEqual(payload)

    payload.disabled_reason = "feature_disabled"
    expect(() => decodeRecordingLibraryList(payload)).toThrow(
      /recording_library_disabled/,
    )
  })

  it("rejects disabled_reason when available", () => {
    const payload = buildRecordingDetailPayload()
    payload.disabled_reason = "recording_library_disabled"

    expect(() => decodeRecordingDetail(payload)).toThrow(/available=true/)
  })

  it("rejects duplicate summary storage keys", () => {
    const payload = mutateList((candidate) => {
      candidate.summaries[1].storage_key = candidate.summaries[0].storage_key
    })

    expect(() => decodeRecordingLibraryList(payload)).toThrow(/storage_key가 중복/)
  })

  it("rejects zero period_index in selected context", () => {
    const payload = mutateList((candidate) => {
      candidate.summaries[0].selected_context!.period_index = 0
    })

    expect(() => decodeRecordingLibraryList(payload)).toThrow(/양의 정수/)
  })

  it("rejects a display_name drift from title/original name", () => {
    const payload = mutateDetail((candidate) => {
      candidate.recording.display_name = "임의 추정 제목"
    })

    expect(() => decodeRecordingDetail(payload)).toThrow(/display_name/)
  })

  it("rejects non-NFC title and display fields", () => {
    const payload = mutateList((candidate) => {
      candidate.summaries[0].current_title!.title = "e\u0301"
    })

    expect(() => decodeRecordingLibraryList(payload)).toThrow(/NFC 정규화/)
  })

  it("rejects non-NFC display metadata in detail payload", () => {
    const payload = mutateDetail((candidate) => {
      candidate.recording.display_name = "e\u0301"
      candidate.recording.current_title!.title = "e\u0301"
    })

    expect(() => decodeRecordingDetail(payload)).toThrow(/NFC 정규화/)
  })

  it("rejects overlong detail display names", () => {
    const payload = mutateDetail((candidate) => {
      candidate.recording.display_name = "d".repeat(1025)
      candidate.recording.current_title!.title = "d".repeat(1025)
    })

    expect(() => decodeRecordingDetail(payload)).toThrow(/current_title\.title/)
  })

  it("rejects a stage that does not match artifact_kind", () => {
    const payload = mutateDetail((candidate) => {
      candidate.jobs[0].artifacts[0].stage = "summary"
    })

    expect(() => decodeRecordingDetail(payload)).toThrow(/artifact_kind와 일치/)
  })

  it("rejects duplicate latest artifacts for the same kind in one job", () => {
    const payload = mutateDetail((candidate) => {
      candidate.jobs[0].artifacts.push({
        artifact_kind: "transcript_raw_text",
        stage: "transcript",
        revision: 3,
        is_latest: true,
        bytes: 123,
        mime_type: "text/plain",
        created_at: "2026-07-23T13:22:00+09:00",
      })
      candidate.counts.artifacts += 1
      candidate.limits.artifacts_truncated = true
    })

    expect(() => decodeRecordingDetail(payload)).toThrow(/latest가 중복/)
  })

  it("rejects multiple current jobs", () => {
    const payload = mutateDetail((candidate) => {
      candidate.jobs[1].is_current = true
    })

    expect(() => decodeRecordingDetail(payload)).toThrow(/is_current=true 항목이 둘 이상/)
  })

  it("requires review job_key when artifact correlation is present", () => {
    const payload = mutateDetail((candidate) => {
      candidate.reviews[0].job_key = null
    })

    expect(() => decodeRecordingDetail(payload)).toThrow(/job_key가 필요/)
  })

  it("rejects unexpected detail limit values", () => {
    const payload = mutateDetail((candidate) => {
      candidate.limits.jobs = 20
    })

    expect(() => decodeRecordingDetail(payload)).toThrow(/고정 상한/)
  })
})
