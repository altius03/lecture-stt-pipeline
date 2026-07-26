import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  fetchRecordingDetail,
  fetchRecordingLibraryList,
} from "../src/lib/panelApi"
import {
  buildRecordingDetailPayload,
  buildRecordingLibraryListPayload,
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
})
