import { useCallback, useState } from "react"
import { useQuery } from "@tanstack/react-query"

import { fetchTranscriptionAnalytics } from "../lib/panelApi"
import type { TranscriptionAnalyticsPeriod } from "../types"

const TRANSCRIPTION_ANALYTICS_QUERY_KEY = [
  "storage-v2",
  "transcription-analytics",
] as const

interface UseTranscriptionAnalyticsOptions {
  enabled: boolean
}

export function useTranscriptionAnalytics({
  enabled,
}: UseTranscriptionAnalyticsOptions) {
  const [selectedPeriod, setSelectedPeriod] =
    useState<TranscriptionAnalyticsPeriod>("week")

  const query = useQuery({
    queryKey: [...TRANSCRIPTION_ANALYTICS_QUERY_KEY, selectedPeriod],
    queryFn: ({ signal }) =>
      fetchTranscriptionAnalytics(selectedPeriod, signal),
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
  })

  const selectPeriod = useCallback(
    (period: TranscriptionAnalyticsPeriod) => {
      setSelectedPeriod(period)
    },
    [],
  )

  return {
    selectedPeriod,
    selectPeriod,
    analytics: query.data ?? null,
    isLoading: query.isLoading,
    isRefreshing: query.isFetching && !query.isLoading,
    error: query.error instanceof Error ? query.error.message : null,
    retry: async (): Promise<boolean> => {
      const result = await query.refetch()
      return result.isSuccess
    },
  }
}
