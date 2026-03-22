import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { FALLBACK_PANEL_ENDPOINTS, fetchPanelState, postPanelAction } from "../lib/panelApi"
import { subscribePanelEvents } from "../lib/panelEvents"
import type { PanelAction, PanelState } from "../types"

const PANEL_STATE_QUERY_KEY = ["panel", "state"] as const

function toErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

export function usePanelState() {
  const queryClient = useQueryClient()
  const pollTimerRef = useRef<number | null>(null)
  const [pendingAction, setPendingAction] = useState<PanelAction | null>(null)
  const [logResetKey, setLogResetKey] = useState(0)
  const [realtimeConnected, setRealtimeConnected] = useState(false)
  const [streamError, setStreamError] = useState<string | null>(null)

  const stateQuery = useQuery({
    queryKey: PANEL_STATE_QUERY_KEY,
    queryFn: () => {
      const cachedState = queryClient.getQueryData<PanelState>(PANEL_STATE_QUERY_KEY)
      const endpoint = cachedState?.actions.endpoints.state ?? FALLBACK_PANEL_ENDPOINTS.state
      return fetchPanelState(endpoint)
    },
  })
  const hasInitialState = stateQuery.data != null

  useEffect(() => {
    if (!hasInitialState) {
      return
    }

    return subscribePanelEvents({
      onEvent: (event) => {
        if (event.type !== "state") {
          return
        }
        setStreamError(null)
        queryClient.setQueryData(PANEL_STATE_QUERY_KEY, event.state)
      },
      onConnectionChange: (connected) => {
        if (connected) {
          setStreamError(null)
        }
        setRealtimeConnected(connected)
      },
      onFatalError: (message) => {
        setStreamError(message)
      },
    })
  }, [hasInitialState, queryClient])

  useEffect(() => {
    if (!stateQuery.data || realtimeConnected) {
      return
    }

    const intervalMs = Math.max(500, Math.round(stateQuery.data.summary.poll_interval_sec * 1000))
    pollTimerRef.current = window.setTimeout(() => {
      void stateQuery.refetch()
    }, intervalMs)

    return () => {
      if (pollTimerRef.current !== null) {
        window.clearTimeout(pollTimerRef.current)
        pollTimerRef.current = null
      }
    }
  }, [realtimeConnected, stateQuery.data, stateQuery.error, stateQuery.refetch, stateQuery.status])

  const actionMutation = useMutation({
    mutationFn: async (action: PanelAction) => {
      const cachedState = queryClient.getQueryData<PanelState>(PANEL_STATE_QUERY_KEY)
      const endpoints = cachedState?.actions.endpoints ?? stateQuery.data?.actions.endpoints ?? FALLBACK_PANEL_ENDPOINTS
      return postPanelAction(endpoints[action])
    },
    onMutate: async (action) => {
      setPendingAction(action)
    },
    onSuccess: async (_response, action) => {
      if (action === "clear_history") {
        setLogResetKey((current) => current + 1)
      }
      await queryClient.invalidateQueries({ queryKey: PANEL_STATE_QUERY_KEY })
    },
    onSettled: async () => {
      setPendingAction(null)
    },
  })

  const error =
    streamError ||
    (actionMutation.error && toErrorMessage(actionMutation.error, "패널 작업 요청에 실패했습니다.")) ||
    (stateQuery.error && toErrorMessage(stateQuery.error, "패널 상태를 불러오지 못했습니다.")) ||
    null

  return {
    state: stateQuery.data ?? null,
    isLoading: stateQuery.status === "pending" && stateQuery.data == null,
    error,
    pendingAction,
    logResetKey,
    realtimeConnected,
    runAction: async (action: PanelAction) => {
      await actionMutation.mutateAsync(action)
    },
    retry: async () => {
      await stateQuery.refetch()
    },
  }
}
