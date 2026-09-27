import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { useTranscriptPreview } from "../src/hooks/useTranscriptPreview"
import { fetchTranscriptPreview } from "../src/lib/panelApi"
import { buildTranscriptPreviewPayload } from "./recordingLibraryFixtures"

vi.mock("../src/lib/panelApi", () => ({
  PanelApiError: class PanelApiError extends Error {
    constructor(
      message: string,
      readonly status: number,
    ) {
      super(message)
      this.name = "PanelApiError"
    }
  },
  fetchTranscriptPreview: vi.fn(),
}))

const fetchTranscriptPreviewMock = vi.mocked(fetchTranscriptPreview)

interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (reason?: unknown) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: Infinity,
      },
    },
  })
}

function createWrapper(queryClient = createQueryClient()) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        {children}
      </QueryClientProvider>
    )
  }
}

describe("useTranscriptPreview", () => {
  beforeEach(() => {
    fetchTranscriptPreviewMock.mockReset()
  })

  it("does not preload until the user opens the preview", async () => {
    fetchTranscriptPreviewMock.mockResolvedValue(buildTranscriptPreviewPayload())

    const { result } = renderHook(
      () =>
        useTranscriptPreview({
          enabled: true,
          target: {
            storageKey: "rec_meeting_2026_07_22",
            jobKey: "job_meeting_01",
            revision: 2,
          },
        }),
      { wrapper: createWrapper() },
    )

    expect(fetchTranscriptPreviewMock).not.toHaveBeenCalled()
    expect(result.current.preview).toBeNull()

    act(() => {
      result.current.openPreview()
    })

    await waitFor(() => {
      expect(result.current.preview?.recording.storage_key).toBe(
        "rec_meeting_2026_07_22",
      )
    })
    expect(fetchTranscriptPreviewMock).toHaveBeenCalledTimes(1)
  })

  it("aborts and removes cached preview on close", async () => {
    const request = deferred<ReturnType<typeof buildTranscriptPreviewPayload>>()
    let requestSignal: AbortSignal | undefined
    const queryClient = createQueryClient()

    fetchTranscriptPreviewMock.mockImplementation(async (_storageKey, signal) => {
      requestSignal = signal
      return request.promise
    })

    const { result } = renderHook(
      () =>
        useTranscriptPreview({
          enabled: true,
          target: {
            storageKey: "rec_meeting_2026_07_22",
            jobKey: "job_meeting_01",
            revision: 2,
          },
        }),
      { wrapper: createWrapper(queryClient) },
    )

    act(() => {
      result.current.openPreview()
    })

    await waitFor(() => {
      expect(fetchTranscriptPreviewMock).toHaveBeenCalledTimes(1)
    })

    await act(async () => {
      await result.current.closePreview()
    })

    expect(requestSignal?.aborted).toBe(true)
    expect(
      queryClient.getQueryData([
        "storage-v2",
        "recording-library",
        "transcript-preview",
        "rec_meeting_2026_07_22",
        "job_meeting_01",
        2,
      ]),
    ).toBeUndefined()
    expect(result.current.isOpen).toBe(false)
    expect(result.current.preview).toBeNull()
  })

  it("clears the visible preview and requires explicit reopen when the same recording gets a new transcript identity", async () => {
    fetchTranscriptPreviewMock.mockResolvedValue(
      buildTranscriptPreviewPayload("rec_meeting_2026_07_22"),
    )

    const { result, rerender } = renderHook(
      ({ revision }) =>
        useTranscriptPreview({
          enabled: true,
          target: {
            storageKey: "rec_meeting_2026_07_22",
            jobKey: "job_meeting_01",
            revision,
          },
        }),
      {
        initialProps: { revision: 2 },
        wrapper: createWrapper(),
      },
    )

    act(() => {
      result.current.openPreview()
    })

    await waitFor(() => {
      expect(result.current.preview?.recording.storage_key).toBe(
        "rec_meeting_2026_07_22",
      )
    })

    rerender({ revision: 3 })

    expect(result.current.isOpen).toBe(false)
    expect(result.current.preview).toBeNull()
    expect(fetchTranscriptPreviewMock).toHaveBeenCalledTimes(1)
  })

  it("reports PanelApiError status codes for retry UI", async () => {
    const { PanelApiError } = await import("../src/lib/panelApi")
    fetchTranscriptPreviewMock.mockRejectedValue(
      new PanelApiError("전사 미리보기를 아직 열 수 없습니다.", 409),
    )

    const { result } = renderHook(
      () =>
        useTranscriptPreview({
          enabled: true,
          target: {
            storageKey: "rec_meeting_2026_07_22",
            jobKey: "job_meeting_01",
            revision: 2,
          },
        }),
      { wrapper: createWrapper() },
    )

    act(() => {
      result.current.openPreview()
    })

    await waitFor(() => {
      expect(result.current.previewErrorStatus).toBe(409)
    })
    expect(result.current.previewError).toBe("전사 미리보기를 아직 열 수 없습니다.")
  })
})
