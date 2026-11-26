import { useQuery } from "@tanstack/react-query"

import {
  fetchRecordingDetail,
  fetchRecordingLibraryList,
} from "../lib/panelApi"

const RECORDING_LIBRARY_LIST_QUERY_KEY = [
  "storage-v2",
  "recording-library",
  "list",
] as const
const RECORDING_LIBRARY_DETAIL_QUERY_KEY = [
  "storage-v2",
  "recording-library",
  "detail",
] as const

interface UseRecordingLibraryOptions {
  enabled: boolean
  selectedStorageKey: string | null
  limit?: number
  offset?: number
}

export function useRecordingLibrary({
  enabled,
  selectedStorageKey,
  limit = 50,
  offset = 0,
}: UseRecordingLibraryOptions) {
  const listQuery = useQuery({
    queryKey: [...RECORDING_LIBRARY_LIST_QUERY_KEY, limit, offset],
    queryFn: ({ signal }) => fetchRecordingLibraryList({ limit, offset }, signal),
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
  })
  const listGateProven =
    enabled
    && listQuery.isFetchedAfterMount
    && listQuery.isSuccess
  const listGateCurrent = listGateProven && !listQuery.isFetching
  const detailEnabled =
    listGateCurrent
    && selectedStorageKey !== null
    && listQuery.data?.available === true

  const detailQuery = useQuery({
    queryKey: [...RECORDING_LIBRARY_DETAIL_QUERY_KEY, selectedStorageKey],
    queryFn: ({ signal }) => {
      if (selectedStorageKey === null) {
        throw new Error("녹음 storage_key를 입력하세요.")
      }
      return fetchRecordingDetail(selectedStorageKey, signal)
    },
    enabled: detailEnabled,
    retry: false,
    refetchOnWindowFocus: false,
  })

  return {
    list: listGateCurrent ? listQuery.data ?? null : null,
    detail: detailEnabled ? detailQuery.data ?? null : null,
    listLoading:
      enabled
      && !listGateCurrent
      && (listQuery.isLoading || listQuery.isFetching),
    detailLoading: detailEnabled && detailQuery.isLoading,
    listRefreshing: listGateProven && listQuery.isFetching,
    detailRefreshing: detailEnabled && detailQuery.isFetching && !detailQuery.isLoading,
    listError: listQuery.error instanceof Error ? listQuery.error.message : null,
    detailError:
      detailEnabled && detailQuery.error instanceof Error
        ? detailQuery.error.message
        : null,
    retryList: async (): Promise<boolean> => {
      const result = await listQuery.refetch()
      return result.isSuccess
    },
    retryDetail: async (): Promise<boolean> => {
      const result = await detailQuery.refetch()
      return result.isSuccess
    },
  }
}
