import { useEffect, useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"

import { PanelApiError, fetchTranscriptPreview } from "../lib/panelApi"

const RECORDING_TRANSCRIPT_PREVIEW_QUERY_KEY = [
  "storage-v2",
  "recording-library",
  "transcript-preview",
] as const

interface UseTranscriptPreviewOptions {
  enabled: boolean
  target:
    | {
        storageKey: string
        jobKey: string
        revision: number
      }
    | null
}

export function useTranscriptPreview({
  enabled,
  target,
}: UseTranscriptPreviewOptions) {
  const queryClient = useQueryClient()
  const [openedIdentity, setOpenedIdentity] = useState<string | null>(null)
  const currentIdentity = target
    ? `${target.storageKey}:${target.jobKey}:r${target.revision}`
    : null
  const queryKey = [
    ...RECORDING_TRANSCRIPT_PREVIEW_QUERY_KEY,
    target?.storageKey ?? null,
    target?.jobKey ?? null,
    target?.revision ?? null,
  ] as const
  const isOpen = currentIdentity !== null && openedIdentity === currentIdentity

  useEffect(() => {
    if (!enabled && openedIdentity !== null) {
      setOpenedIdentity(null)
    }
  }, [enabled, openedIdentity])

  useEffect(() => {
    if (openedIdentity !== null && openedIdentity !== currentIdentity) {
      setOpenedIdentity(null)
    }
  }, [currentIdentity, openedIdentity])

  useEffect(() => {
    const storageKey = target?.storageKey ?? null
    const jobKey = target?.jobKey ?? null
    const revision = target?.revision ?? null
    return () => {
      if (storageKey === null || jobKey === null || revision === null) {
        return
      }
      void queryClient.cancelQueries({
        queryKey: [
          ...RECORDING_TRANSCRIPT_PREVIEW_QUERY_KEY,
          storageKey,
          jobKey,
          revision,
        ],
        exact: true,
      })
      queryClient.removeQueries({
        queryKey: [
          ...RECORDING_TRANSCRIPT_PREVIEW_QUERY_KEY,
          storageKey,
          jobKey,
          revision,
        ],
        exact: true,
      })
    }
  }, [queryClient, target?.storageKey, target?.jobKey, target?.revision])

  const query = useQuery({
    queryKey,
    queryFn: ({ signal }) => {
      if (target === null) {
        throw new Error("녹음 storage_key를 입력하세요.")
      }
      return fetchTranscriptPreview(
        target.storageKey,
        signal,
        {
          jobKey: target.jobKey,
          revision: target.revision,
        },
      )
    },
    enabled: enabled && isOpen && target !== null,
    retry: false,
    refetchOnWindowFocus: false,
    gcTime: 0,
  })

  async function clearTarget(
    targetToClear: UseTranscriptPreviewOptions["target"],
  ): Promise<void> {
    if (targetToClear === null) {
      return
    }
    await queryClient.cancelQueries({
      queryKey: [
        ...RECORDING_TRANSCRIPT_PREVIEW_QUERY_KEY,
        targetToClear.storageKey,
        targetToClear.jobKey,
        targetToClear.revision,
      ],
      exact: true,
    })
    queryClient.removeQueries({
      queryKey: [
        ...RECORDING_TRANSCRIPT_PREVIEW_QUERY_KEY,
        targetToClear.storageKey,
        targetToClear.jobKey,
        targetToClear.revision,
      ],
      exact: true,
    })
  }

  return {
    isOpen,
    preview: enabled && isOpen ? query.data ?? null : null,
    previewLoading: enabled && isOpen && query.isLoading,
    previewRefreshing: enabled && isOpen && query.isFetching && !query.isLoading,
    previewError:
      enabled && isOpen && query.error instanceof Error
        ? query.error.message
        : null,
    previewErrorStatus:
      enabled && isOpen && query.error instanceof PanelApiError
        ? query.error.status
        : null,
    openPreview: (): void => {
      if (!enabled || currentIdentity === null) {
        return
      }
      setOpenedIdentity(currentIdentity)
    },
    closePreview: async (): Promise<void> => {
      const activeTarget = openedIdentity === currentIdentity ? target : null
      setOpenedIdentity(null)
      await clearTarget(activeTarget)
    },
    retryPreview: async (): Promise<boolean> => {
      if (!enabled || !isOpen) {
        return false
      }
      const result = await query.refetch()
      return result.isSuccess
    },
  }
}
