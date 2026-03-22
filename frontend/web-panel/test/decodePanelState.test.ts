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
    actions: {
      pause_action: "pause",
      pause_label: "일시정지",
      endpoints: {
        state: "/api/custom-state",
        logs: "/api/custom-logs",
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
    expect(state.summary.current_job?.eta_sec).toBe(120)
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
})
