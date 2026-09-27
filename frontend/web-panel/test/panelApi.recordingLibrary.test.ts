import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  fetchRecordingDetail,
  fetchRecordingLibraryList,
  fetchTranscriptPreview,
} from "../src/lib/panelApi"
import {
  buildRecordingDetailPayload,
  buildRecordingLibraryListPayload,
  buildTranscriptPreviewPayload,
} from "./recordingLibraryFixtures"

let fetchSpy: ReturnType<typeof vi.spyOn>
const originalFetch = globalThis.fetch

function buildJsonResponse(status: number, payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
    },
  })
}

describe("panelApi recording library endpoints", () => {
  beforeEach(() => {
    fetchSpy = vi.spyOn(
      globalThis as unknown as { fetch: typeof fetch },
      "fetch",
    )
    fetchSpy.mockReset()
  })

  afterEach(() => {
    fetchSpy.mockRestore()
    globalThis.fetch = originalFetch
  })

  it("fetches list with no-store and the caller signal", async () => {
    const controller = new AbortController()
    const payload = buildRecordingLibraryListPayload()
    fetchSpy.mockResolvedValueOnce(buildJsonResponse(200, payload))

    await expect(
      fetchRecordingLibraryList({ limit: 50, offset: 0 }, controller.signal),
    ).resolves.toEqual(payload)

    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/storage-v2/library/recordings?limit=50&offset=0",
      {
        cache: "no-store",
        signal: controller.signal,
      },
    )
  })

  it("fetches detail with the canonical storage_key path", async () => {
    const controller = new AbortController()
    const payload = buildRecordingDetailPayload("rec_2026_07_23_ds_05")
    fetchSpy.mockResolvedValueOnce(buildJsonResponse(200, payload))

    await expect(
      fetchRecordingDetail("rec_2026_07_23_ds_05", controller.signal),
    ).resolves.toEqual(payload)

    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/storage-v2/library/recordings/rec_2026_07_23_ds_05",
      {
        cache: "no-store",
        signal: controller.signal,
      },
    )
  })

  it("propagates backend errors from list", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(503, {
        error: "격리 library DB를 열 수 없습니다.",
      }),
    )

    await expect(fetchRecordingLibraryList()).rejects.toThrow(
      "격리 library DB를 열 수 없습니다.",
    )
  })

  it("rejects a detail response for another storage_key", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, buildRecordingDetailPayload("rec_meeting_2026_07_22")),
    )

    await expect(
      fetchRecordingDetail("rec_2026_07_23_ds_05"),
    ).rejects.toThrow(/storage_key does not match/)
  })

  it("rejects storage_key values with surrounding whitespace before fetch", async () => {
    await expect(
      fetchRecordingDetail(" rec_2026_07_23_ds_05 "),
    ).rejects.toThrow(/must not include surrounding whitespace/)

    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it("fetches transcript preview with the canonical path and request match", async () => {
    const controller = new AbortController()
    const payload = buildTranscriptPreviewPayload("rec_meeting_2026_07_22")
    fetchSpy.mockResolvedValueOnce(buildJsonResponse(200, payload))

    await expect(
      fetchTranscriptPreview("rec_meeting_2026_07_22", controller.signal),
    ).resolves.toEqual(payload)

    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/storage-v2/library/recordings/rec_meeting_2026_07_22/transcript-preview",
      {
        cache: "no-store",
        signal: controller.signal,
      },
    )
  })

  it("rejects transcript preview responses for another storage_key", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, buildTranscriptPreviewPayload("rec_2026_07_23_ds_05")),
    )

    await expect(
      fetchTranscriptPreview("rec_meeting_2026_07_22"),
    ).rejects.toThrow(/storage_key does not match/)
  })

  it("rejects transcript preview responses when the current job identity changed", async () => {
    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, buildTranscriptPreviewPayload("rec_meeting_2026_07_22")),
    )

    await expect(
      fetchTranscriptPreview(
        "rec_meeting_2026_07_22",
        undefined,
        { jobKey: "job_meeting_02", revision: 2 },
      ),
    ).rejects.toThrow(/job_key does not match/)

    fetchSpy.mockResolvedValueOnce(
      buildJsonResponse(200, buildTranscriptPreviewPayload("rec_meeting_2026_07_22")),
    )

    await expect(
      fetchTranscriptPreview(
        "rec_meeting_2026_07_22",
        undefined,
        { jobKey: "job_meeting_01", revision: 9 },
      ),
    ).rejects.toThrow(/revision does not match/)
  })

  it("maps transcript preview status failures to clear fallback messages", async () => {
    fetchSpy
      .mockResolvedValueOnce(buildJsonResponse(404, {}))
      .mockResolvedValueOnce(buildJsonResponse(403, {}))
      .mockResolvedValueOnce(buildJsonResponse(409, {}))

    await expect(fetchTranscriptPreview("rec_meeting_2026_07_22")).rejects.toThrow(
      "전사 미리보기를 찾지 못했습니다.",
    )
    await expect(fetchTranscriptPreview("rec_meeting_2026_07_22")).rejects.toThrow(
      "전사 미리보기 접근이 허용되지 않았습니다.",
    )
    await expect(fetchTranscriptPreview("rec_meeting_2026_07_22")).rejects.toThrow(
      "전사 미리보기를 아직 열 수 없습니다.",
    )
  })
})
