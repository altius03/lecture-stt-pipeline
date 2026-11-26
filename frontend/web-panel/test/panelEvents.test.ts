import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { subscribePanelEvents } from "../src/lib/panelEvents"
import type { PanelState } from "../src/types"

class FakeEventSource {
  static instances: FakeEventSource[] = []

  readonly listeners = new Map<string, Array<(event: Event) => void>>()
  closed = false

  constructor(public readonly url: string) {
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: (event: Event) => void) {
    const listeners = this.listeners.get(type) ?? []
    listeners.push(listener)
    this.listeners.set(type, listeners)
  }

  emit(type: string, data?: string) {
    const event = new MessageEvent(type, {
      data,
    })
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event)
    }
  }

  close() {
    this.closed = true
  }
}

function createPanelState(): PanelState {
  return {
    schema_version: 2,
    runtime_state: {
      status: "running",
      source: "web",
      paused: false,
      managed_running: true,
      external_worker_pids: [],
      label: "실행 중",
      description: "워커가 실행 중입니다.",
    },
    counts: {
      PENDING: 1,
      PROCESSING: 0,
      NEEDS_REVIEW: 0,
      DONE: 0,
      ERROR: 0,
      UNREGISTERED: 0,
    },
    folders: {
      inbox: "/tmp/inbox",
      audio: "/tmp/audio",
      transcripts: "/tmp/transcripts",
      errors: "/tmp/errors",
    },
    notification: {
      selection: "telegram",
      selected_label: "텔레그램만",
      apply_label: "다음 시작부터 적용됩니다.",
      restart_required: false,
      can_apply_now: false,
      options: [
        {
          id: "telegram",
          label: "텔레그램만",
          description: "TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID가 필요합니다.",
          available: true,
        },
        {
          id: "discord",
          label: "디스코드만",
          description: "DISCORD_WEBHOOK_URL이 필요합니다.",
          available: true,
        },
        {
          id: "both",
          label: "둘 다",
          description: "텔레그램과 디스코드 secret이 모두 필요합니다.",
          available: true,
        },
        {
          id: "disabled",
          label: "끄기",
          description: "알림 전송을 중단합니다.",
          available: true,
        },
      ],
    },
    actions: {
      pause_action: "pause",
      pause_label: "일시정지",
      endpoints: {
        state: "/api/state",
        logs: "/api/logs",
        notification: "/api/notification",
        start: "/api/start",
        pause: "/api/pause",
        resume: "/api/resume",
        stop: "/api/stop",
        refresh: "/api/refresh",
        exit: "/api/exit",
        clear_history: "/api/clear_history",
      },
    },
    summary: {
      current_job: null,
      progress_label: "대기",
      eta_label: "-",
      notice: "",
      refresh_hint: "1초",
      updated_at: "2026-03-23 01:00:00",
      poll_interval_sec: 1,
    },
    jobs_v2: [],
    processing_v2: [],
  }
}

describe("panelEvents", () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeEventSource.instances = []
    vi.stubGlobal("EventSource", FakeEventSource)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.runOnlyPendingTimers()
    vi.useRealTimers()
  })

  it("reconnects after a malformed state event and resumes delivery", async () => {
    const onEvent = vi.fn()
    const onConnectionChange = vi.fn()
    const onFatalError = vi.fn()

    const unsubscribe = subscribePanelEvents({
      onEvent,
      onConnectionChange,
      onFatalError,
    })

    expect(FakeEventSource.instances).toHaveLength(1)

    const firstSource = FakeEventSource.instances[0]
    firstSource?.emit("open")
    firstSource?.emit("state", "{")

    expect(onFatalError).toHaveBeenCalled()
    expect(firstSource?.closed).toBe(true)

    await vi.advanceTimersByTimeAsync(1000)

    expect(FakeEventSource.instances).toHaveLength(2)

    const secondSource = FakeEventSource.instances[1]
    secondSource?.emit("open")
    secondSource?.emit("state", JSON.stringify(createPanelState()))

    expect(onEvent).toHaveBeenCalledWith({
      type: "state",
      state: createPanelState(),
    })
    expect(onConnectionChange).toHaveBeenCalledWith(true)

    unsubscribe()
  })

  it("delivers controller-backed state events without treating them as fatal", () => {
    const onEvent = vi.fn()
    const onFatalError = vi.fn()

    const unsubscribe = subscribePanelEvents({
      onEvent,
      onFatalError,
    })

    expect(FakeEventSource.instances).toHaveLength(1)

    const source = FakeEventSource.instances[0]
    source?.emit("open")
    source?.emit(
      "state",
      JSON.stringify({
        ...createPanelState(),
        runtime_state: {
          ...createPanelState().runtime_state,
          source: "controller",
          managed_running: false,
          description: "controller 대기 중",
        },
      }),
    )

    expect(onFatalError).not.toHaveBeenCalled()
    expect(onEvent).toHaveBeenCalledWith({
      type: "state",
      state: {
        ...createPanelState(),
        runtime_state: {
          ...createPanelState().runtime_state,
          source: "controller",
          managed_running: false,
          description: "controller 대기 중",
        },
      },
    })

    unsubscribe()
  })
})
