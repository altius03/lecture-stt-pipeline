import { decodeLogPayload, decodePanelState } from "./decodePanelState"
import { PANEL_EVENTS_ENDPOINT } from "./panelApi"
import type { PanelEvent, PanelState, PanelStreamLogPayload } from "../types"

interface PanelEventListener {
  onEvent: (event: PanelEvent) => void
  onConnectionChange?: (connected: boolean) => void
  onFatalError?: (message: string) => void
}

class PanelEventStream {
  private source: EventSource | null = null
  private listeners = new Set<PanelEventListener>()
  private connected = false
  private reconnectTimer: number | null = null

  constructor(private readonly endpoint: string) {}

  subscribe(listener: PanelEventListener): () => void {
    this.listeners.add(listener)
    listener.onConnectionChange?.(this.connected)
    this.ensureSource()

    return () => {
      this.listeners.delete(listener)
      if (this.listeners.size === 0) {
        this.dispose()
      }
    }
  }

  private ensureSource() {
    if (this.source || this.reconnectTimer !== null || typeof window === "undefined") {
      return
    }

    if (typeof window.EventSource === "undefined") {
      this.notifyConnectionChange(false)
      return
    }

    const source = new window.EventSource(this.endpoint)
    this.source = source

    source.addEventListener("open", () => {
      this.connected = true
      this.notifyConnectionChange(true)
    })

    source.addEventListener("state", (event) => {
      this.handleStateEvent(event)
    })

    source.addEventListener("log_chunk", (event) => {
      this.handleLogEvent(event, false)
    })

    source.addEventListener("log_reset", (event) => {
      this.handleLogEvent(event, true)
    })

    source.addEventListener("heartbeat", () => {
      this.emit({ type: "heartbeat" })
    })

    source.addEventListener("error", () => {
      this.connected = false
      this.notifyConnectionChange(false)
    })
  }

  private handleStateEvent(event: Event) {
    try {
      const message = event as MessageEvent<string>
      const payload = JSON.parse(message.data) as unknown
      const state = decodePanelState(payload)
      this.emit({
        type: "state",
        state,
      })
    } catch (error) {
      this.handleFatalError(error, "실시간 상태 이벤트를 해석하지 못했습니다.")
    }
  }

  private handleLogEvent(event: Event, reset: boolean) {
    try {
      const message = event as MessageEvent<string>
      const payload = JSON.parse(message.data) as unknown
      const decoded = decodeLogPayload(payload)
      const nextPayload: PanelStreamLogPayload = {
        ...decoded,
        reset,
      }
      this.emit({
        type: reset ? "log_reset" : "log_chunk",
        payload: nextPayload,
      })
    } catch (error) {
      this.handleFatalError(error, "실시간 로그 이벤트를 해석하지 못했습니다.")
    }
  }

  private handleFatalError(error: unknown, fallback: string) {
    const message = error instanceof Error ? error.message : fallback
    this.connected = false
    this.notifyConnectionChange(false)
    this.notifyFatalError(message)
    this.closeSource()
    this.scheduleReconnect()
  }

  private emit(event: PanelEvent) {
    for (const listener of this.listeners) {
      listener.onEvent(event)
    }
  }

  private notifyConnectionChange(connected: boolean) {
    for (const listener of this.listeners) {
      listener.onConnectionChange?.(connected)
    }
  }

  private notifyFatalError(message: string) {
    for (const listener of this.listeners) {
      listener.onFatalError?.(message)
    }
  }

  private closeSource() {
    if (this.source) {
      this.source.close()
      this.source = null
    }
  }

  private scheduleReconnect() {
    if (this.listeners.size === 0 || this.reconnectTimer !== null || typeof window === "undefined") {
      return
    }

    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      this.ensureSource()
    }, 1000)
  }

  private dispose() {
    this.closeSource()
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.connected = false
    panelEventStreams.delete(this.endpoint)
  }
}

const panelEventStreams = new Map<string, PanelEventStream>()

export function subscribePanelEvents(listener: PanelEventListener, endpoint: string = PANEL_EVENTS_ENDPOINT): () => void {
  let stream = panelEventStreams.get(endpoint)
  if (!stream) {
    stream = new PanelEventStream(endpoint)
    panelEventStreams.set(endpoint, stream)
  }
  return stream.subscribe(listener)
}

export function applyStateEventToCache(event: PanelEvent): PanelState | null {
  if (event.type !== "state") {
    return null
  }
  return event.state
}
