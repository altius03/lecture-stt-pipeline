import { describe, expect, it } from "vitest"

import { decodePanelState } from "../src/lib/decodePanelState"

function createStatePayload() {
  return {
    schema_version: 2,
    runtime_state: {
      status: "running",
      source: "web",
      paused: false,
      managed_running: true,
      external_worker_pids: [1234],
      label: "실행 중",
      description: "워커가 실행 중입니다.",
    },
    counts: {
      PENDING: 2,
      PROCESSING: 1,
      NEEDS_REVIEW: 1,
      DONE: 10,
      ERROR: 0,
      UNREGISTERED: 3,
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
        state: "/api/custom-state",
        logs: "/api/custom-logs",
        notification: "/api/custom-notification",
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
      current_job: {
        id: 7,
        file_name: "lecture.m4a",
        step: "transcribing",
        progress_pct: 55,
        eta_sec: 120,
        eta_label: "2분 남음",
      },
      progress_label: "55%",
      eta_label: "2분 남음",
      notice: "",
      refresh_hint: "1초 간격 갱신",
      updated_at: "2026-03-23T09:00:00+09:00",
      poll_interval_sec: 1,
    },
    jobs_v2: [
      {
        id: 1,
        status: "PROCESSING",
        file_name: "lecture.m4a",
        updated_at: "2026-03-23T09:00:00+09:00",
        step: "transcribing",
        progress_pct: 55,
        error_message: "",
      },
    ],
    processing_v2: [
      {
        id: 1,
        file_name: "lecture.m4a",
        step: "transcribing",
        progress_pct: 55,
        eta_sec: 120,
        eta_label: "2분 남음",
      },
    ],
  }
}

describe("decodePanelState", () => {
  it("decodes a valid panel snapshot", () => {
    const state = decodePanelState(createStatePayload())

    expect(state.schema_version).toBe(2)
    expect(state.runtime_state.status).toBe("running")
    expect(state.actions.endpoints.logs).toBe("/api/custom-logs")
    expect(state.notification.selection).toBe("telegram")
    expect(state.summary.current_job?.eta_sec).toBe(120)
    expect(state.counts.NEEDS_REVIEW).toBe(1)
    expect(state.jobs_v2[0]?.status).toBe("PROCESSING")
  })

  it("rejects unsupported schema versions", () => {
    expect(() =>
      decodePanelState({
        ...createStatePayload(),
        schema_version: 1,
      }),
    ).toThrow("schema_version")
  })

  it("rejects malformed action endpoints", () => {
    expect(() =>
      decodePanelState({
        ...createStatePayload(),
        actions: {
          ...createStatePayload().actions,
          endpoints: {
            ...createStatePayload().actions.endpoints,
            logs: null,
          },
        },
      }),
    ).toThrow("actions.endpoints.logs")
  })

  it("falls back when legacy backend omits notification fields", () => {
    const payload = createStatePayload()
    delete (payload as { notification?: unknown }).notification
    delete (payload.actions.endpoints as { notification?: string }).notification

    const state = decodePanelState(payload)

    expect(state.actions.endpoints.notification).toBe("/api/notification")
    expect(state.notification.selected_label).toBe("미지원 백엔드")
    expect(state.notification.options[0]?.available).toBe(false)
    expect(state.notification.options[3]?.available).toBe(true)
  })

  it("defaults NEEDS_REVIEW count to zero for older schema_version 2 payloads", () => {
    const payload = createStatePayload()
    delete (payload.counts as { NEEDS_REVIEW?: number }).NEEDS_REVIEW

    const state = decodePanelState(payload)

    expect(state.counts.NEEDS_REVIEW).toBe(0)
  })

  it("accepts controller as a runtime source", () => {
    const payload = createStatePayload()
    payload.runtime_state = {
      ...payload.runtime_state,
      source: "controller",
      managed_running: false,
      external_worker_pids: [],
      label: "실행 중",
      description: "controller 대기 중",
    }

    const state = decodePanelState(payload)

    expect(state.runtime_state.source).toBe("controller")
    expect(state.runtime_state.description).toBe("controller 대기 중")
  })
})
