import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { useRecordingLibrary } from "../src/hooks/useRecordingLibrary"
import {
  fetchRecordingDetail,
  fetchRecordingLibraryList,
} from "../src/lib/panelApi"
import {
  buildRecordingDetailPayload,
  buildRecordingLibraryListPayload,
} from "./recordingLibraryFixtures"

vi.mock("../src/lib/panelApi", () => ({
  fetchRecordingLibraryList: vi.fn(),
  fetchRecordingDetail: vi.fn(),
}))

const fetchListMock = vi.mocked(fetchRecordingLibraryList)
const fetchDetailMock = vi.mocked(fetchRecordingDetail)

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

describe("useRecordingLibrary", () => {
  beforeEach(() => {
    fetchListMock.mockReset()
    fetchDetailMock.mockReset()
  })

  it("does not query when disabled and starts list on enable", async () => {
    fetchListMock.mockResolvedValue(buildRecordingLibraryListPayload())

    const { result, rerender } = renderHook(
      ({ enabled }) =>
        useRecordingLibrary({
          enabled,
          selectedStorageKey: null,
        }),
      {
        initialProps: { enabled: false },
        wrapper: createWrapper(),
      },
    )

    expect(result.current.list).toBeNull()
    expect(fetchListMock).not.toHaveBeenCalled()

    rerender({ enabled: true })
    await waitFor(() => {
      expect(result.current.list?.schema_version).toBe(
        "storage-v2/recording-library-list@1",
      )
    })

    expect(fetchListMock).toHaveBeenCalledTimes(1)
    expect(fetchDetailMock).not.toHaveBeenCalled()
  })

  it("loads detail only after the list reports available", async () => {
    fetchListMock.mockResolvedValue(buildRecordingLibraryListPayload())
    fetchDetailMock.mockResolvedValue(
      buildRecordingDetailPayload("rec_2026_07_23_ds_05"),
    )

    const { result } = renderHook(
      () =>
        useRecordingLibrary({
          enabled: true,
          selectedStorageKey: "rec_2026_07_23_ds_05",
        }),
      { wrapper: createWrapper() },
    )

    await waitFor(() => {
      expect(result.current.list?.available).toBe(true)
    })
    await waitFor(() => {
      expect(result.current.detail?.recording.storage_key).toBe(
        "rec_2026_07_23_ds_05",
      )
    })

    expect(fetchListMock).toHaveBeenCalledTimes(1)
    expect(fetchDetailMock).toHaveBeenCalledTimes(1)
  })

  it("does not request detail for a disabled deep link", async () => {
    fetchListMock.mockResolvedValue(buildRecordingLibraryListPayload(false))

    const { result } = renderHook(
      () =>
        useRecordingLibrary({
          enabled: true,
          selectedStorageKey: "rec_2026_07_23_ds_05",
        }),
      { wrapper: createWrapper() },
    )

    await waitFor(() => {
      expect(result.current.list?.available).toBe(false)
    })

    expect(fetchListMock).toHaveBeenCalledTimes(1)
    expect(fetchDetailMock).not.toHaveBeenCalled()
    expect(result.current.detail).toBeNull()
  })

  it("aborts stale detail requests when the selected storage_key changes", async () => {
    fetchListMock.mockResolvedValue(buildRecordingLibraryListPayload())

    const requests = new Map<string, Deferred<ReturnType<typeof buildRecordingDetailPayload>>>()
    const signals = new Map<string, AbortSignal | undefined>()

    fetchDetailMock.mockImplementation((storageKey, signal) => {
      const request = deferred<ReturnType<typeof buildRecordingDetailPayload>>()
      requests.set(storageKey, request)
      signals.set(storageKey, signal)
      return request.promise
    })

    const { result, rerender } = renderHook(
      ({ selectedStorageKey }) =>
        useRecordingLibrary({
          enabled: true,
          selectedStorageKey,
        }),
      {
        initialProps: { selectedStorageKey: "rec_2026_07_23_ds_05" as string | null },
        wrapper: createWrapper(),
      },
    )

    await waitFor(() => {
      expect(requests.has("rec_2026_07_23_ds_05")).toBe(true)
    })

    rerender({ selectedStorageKey: "rec_meeting_2026_07_22" })
    await waitFor(() => {
      expect(requests.has("rec_meeting_2026_07_22")).toBe(true)
    })
    expect(signals.get("rec_2026_07_23_ds_05")?.aborted).toBe(true)

    await act(async () => {
      requests.get("rec_meeting_2026_07_22")?.resolve(
        buildRecordingDetailPayload("rec_meeting_2026_07_22"),
      )
      await requests.get("rec_meeting_2026_07_22")?.promise
    })

    await waitFor(() => {
      expect(result.current.detail?.recording.storage_key).toBe(
        "rec_meeting_2026_07_22",
      )
    })
  })

  it("clears cached detail when the list becomes unavailable", async () => {
    fetchListMock
      .mockResolvedValueOnce(buildRecordingLibraryListPayload())
      .mockResolvedValueOnce(buildRecordingLibraryListPayload(false))
    fetchDetailMock.mockResolvedValue(
      buildRecordingDetailPayload("rec_2026_07_23_ds_05"),
    )

    const { result } = renderHook(
      () =>
        useRecordingLibrary({
          enabled: true,
          selectedStorageKey: "rec_2026_07_23_ds_05",
        }),
      { wrapper: createWrapper() },
    )

    await waitFor(() => {
      expect(result.current.detail?.recording.storage_key).toBe(
        "rec_2026_07_23_ds_05",
      )
    })

    await act(async () => {
      await result.current.retryList()
    })

    await waitFor(() => {
      expect(result.current.list?.available).toBe(false)
    })

    expect(result.current.detail).toBeNull()
    expect(fetchDetailMock).toHaveBeenCalledTimes(1)
  })

  it("re-proves the list gate before exposing a warm cache after remount", async () => {
    const queryClient = createQueryClient()
    const wrapper = createWrapper(queryClient)
    fetchListMock.mockResolvedValueOnce(buildRecordingLibraryListPayload())
    fetchDetailMock.mockResolvedValue(
      buildRecordingDetailPayload("rec_2026_07_23_ds_05"),
    )

    const firstMount = renderHook(
      () =>
        useRecordingLibrary({
          enabled: true,
          selectedStorageKey: "rec_2026_07_23_ds_05",
        }),
      { wrapper },
    )

    await waitFor(() => {
      expect(firstMount.result.current.detail?.recording.storage_key).toBe(
        "rec_2026_07_23_ds_05",
      )
    })
    firstMount.unmount()

    fetchListMock.mockResolvedValueOnce(buildRecordingLibraryListPayload(false))
    const secondMount = renderHook(
      () =>
        useRecordingLibrary({
          enabled: true,
          selectedStorageKey: "rec_2026_07_23_ds_05",
        }),
      { wrapper },
    )

    expect(secondMount.result.current.list).toBeNull()
    expect(secondMount.result.current.detail).toBeNull()
    expect(fetchDetailMock).toHaveBeenCalledTimes(1)

    await waitFor(() => {
      expect(secondMount.result.current.list?.available).toBe(false)
    })

    expect(secondMount.result.current.detail).toBeNull()
    expect(fetchDetailMock).toHaveBeenCalledTimes(1)
  })
})
