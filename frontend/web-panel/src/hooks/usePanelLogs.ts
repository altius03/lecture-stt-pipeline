import { useEffect, useRef, useState } from "react"

import { fetchLogs } from "../lib/panelApi"
import { subscribePanelEvents } from "../lib/panelEvents"

const MAX_LOG_LINES = 500
const LOG_BOTTOM_THRESHOLD_PX = 6

export interface PanelLogState {
  text: string
  offset: number | null
  revision: number
  droppedLineCount: number
}

interface UsePanelLogsOptions {
  endpoint: string
  eventsEndpoint?: string | null
  pollIntervalSec: number
  enabled: boolean
  resetKey: number
}

const EMPTY_LOG_STATE: PanelLogState = {
  text: "",
  offset: null,
  revision: 0,
  droppedLineCount: 0,
}

function isAtBottom(element: HTMLElement): boolean {
  return element.scrollTop + element.clientHeight >= element.scrollHeight - LOG_BOTTOM_THRESHOLD_PX
}

function toErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

function appendLogText(previous: PanelLogState, nextChunk: string, nextOffset: number, reset: boolean): PanelLogState {
  const combined = reset ? nextChunk : previous.text + nextChunk
  const lines = combined.split("\n")
  const overflow = Math.max(0, lines.length - MAX_LOG_LINES)
  const trimmedText = overflow > 0 ? lines.slice(overflow).join("\n") : combined
  const shouldBumpRevision = reset || nextChunk.length > 0 || overflow > 0
  const droppedLineCount = reset ? overflow : previous.droppedLineCount + overflow

  return {
    text: trimmedText,
    offset: nextOffset,
    revision: shouldBumpRevision ? previous.revision + 1 : previous.revision,
    droppedLineCount,
  }
}

export function usePanelLogs({ endpoint, eventsEndpoint, pollIntervalSec, enabled, resetKey }: UsePanelLogsOptions) {
  const logRef = useRef<HTMLPreElement>(null)
  const offsetRef = useRef<number | null>(null)
  const intervalMsRef = useRef(1000)
  const [logs, setLogs] = useState<PanelLogState>(EMPTY_LOG_STATE)
  const [error, setError] = useState<string | null>(null)
  const [autoFollow, setAutoFollowState] = useState(true)
  const [hasUnread, setHasUnread] = useState(false)
  const [realtimeConnected, setRealtimeConnected] = useState(false)

  useEffect(() => {
    intervalMsRef.current = Math.max(500, Math.round(pollIntervalSec * 1000))
  }, [pollIntervalSec])

  useEffect(() => {
    if (!enabled) {
      offsetRef.current = null
      setLogs(EMPTY_LOG_STATE)
      setError(null)
      setAutoFollowState(true)
      setHasUnread(false)
      setRealtimeConnected(false)
      return
    }

    offsetRef.current = null
    setLogs(EMPTY_LOG_STATE)
    setError(null)
    setHasUnread(false)
  }, [enabled, endpoint, resetKey])

  useEffect(() => {
    if (!enabled || eventsEndpoint === null) {
      setRealtimeConnected(false)
      return
    }

    return subscribePanelEvents(
      {
        onEvent: (event) => {
          if (event.type === "log_chunk") {
            offsetRef.current = event.payload.offset
            setLogs((previous) => appendLogText(previous, event.payload.text, event.payload.offset, false))
            setError(null)
            return
          }

          if (event.type === "log_reset") {
            offsetRef.current = event.payload.offset
            setLogs((previous) => appendLogText(previous, event.payload.text, event.payload.offset, true))
            setError(null)
          }
        },
        onConnectionChange: (connected) => {
          if (connected) {
            setError(null)
          }
          setRealtimeConnected(connected)
        },
        onFatalError: (message) => {
          setError(message)
        },
      },
      eventsEndpoint,
    )
  }, [enabled, eventsEndpoint])

  useEffect(() => {
    if (!enabled || realtimeConnected) {
      return
    }

    let cancelled = false
    let timerId: number | null = null

    async function tick(reset: boolean) {
      try {
        const payload = await fetchLogs(endpoint, reset ? null : offsetRef.current)
        if (cancelled) {
          return
        }
        offsetRef.current = payload.offset
        setLogs((previous) => appendLogText(previous, payload.text, payload.offset, reset))
        setError(null)
      } catch (nextError) {
        if (!cancelled) {
          setError(toErrorMessage(nextError, "로그를 동기화하지 못했습니다."))
        }
      } finally {
        if (!cancelled) {
          timerId = window.setTimeout(() => {
            void tick(false)
          }, intervalMsRef.current)
        }
      }
    }

    void tick(offsetRef.current === null)

    return () => {
      cancelled = true
      if (timerId !== null) {
        window.clearTimeout(timerId)
      }
    }
  }, [enabled, endpoint, resetKey, realtimeConnected])

  useEffect(() => {
    const element = logRef.current
    if (!element) {
      return
    }

    if (logs.text.length === 0) {
      setHasUnread(false)
      return
    }

    if (autoFollow) {
      element.scrollTop = element.scrollHeight
      setHasUnread(false)
      return
    }

    if (!isAtBottom(element)) {
      setHasUnread(true)
    }
  }, [autoFollow, logs.revision, logs.text])

  function handleLogScroll() {
    const element = logRef.current
    if (!element) {
      return
    }

    if (isAtBottom(element)) {
      setAutoFollowState(true)
      setHasUnread(false)
      return
    }

    setAutoFollowState(false)
  }

  function jumpToLatest() {
    const element = logRef.current
    if (element) {
      element.scrollTop = element.scrollHeight
    }
    setAutoFollowState(true)
    setHasUnread(false)
  }

  function setAutoFollow(nextValue: boolean) {
    setAutoFollowState(nextValue)
    if (nextValue) {
      jumpToLatest()
    }
  }

  return {
    logs,
    error,
    logRef,
    autoFollow,
    hasUnread,
    handleLogScroll,
    jumpToLatest,
    setAutoFollow,
  }
}
