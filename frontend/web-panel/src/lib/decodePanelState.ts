import { PANEL_ACTIONS } from "../types"
import type {
  ActionSummary,
  CountSummary,
  FolderSummary,
  LogPayload,
  NotificationOption,
  NotificationSelection,
  NotificationState,
  PanelAction,
  PanelActionResponse,
  PanelEndpoints,
  PanelJob,
  PanelState,
  PanelSummary,
  ProcessingJob,
  RuntimeSource,
  RuntimeState,
  RuntimeStatus,
} from "../types"

const RUNTIME_STATUSES = ["running", "paused", "stopped"] as const satisfies readonly RuntimeStatus[]
const RUNTIME_SOURCES = ["web", "external", "none"] as const satisfies readonly RuntimeSource[]
const PAUSE_ACTIONS = ["pause", "resume"] as const
const NOTIFICATION_SELECTIONS = ["telegram", "discord", "both", "disabled"] as const satisfies readonly NotificationSelection[]

function asRecord(input: unknown, label: string): Record<string, unknown> {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`)
  }
  return input as Record<string, unknown>
}

function readString(record: Record<string, unknown>, key: string, label: string): string {
  const value = record[key]
  if (typeof value !== "string") {
    throw new Error(`${label}.${key} 값이 문자열이 아닙니다.`)
  }
  return value
}

function readOptionalString(record: Record<string, unknown>, key: string): string | null {
  const value = record[key]
  return typeof value === "string" ? value : null
}

function readNumber(record: Record<string, unknown>, key: string, label: string): number {
  const value = record[key]
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${label}.${key} 값이 숫자가 아닙니다.`)
  }
  return value
}

function readBoolean(record: Record<string, unknown>, key: string, label: string): boolean {
  const value = record[key]
  if (typeof value !== "boolean") {
    throw new Error(`${label}.${key} 값이 불리언이 아닙니다.`)
  }
  return value
}

function readNullableNumber(record: Record<string, unknown>, key: string, label: string): number | null {
  const value = record[key]
  if (value === null) {
    return null
  }
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${label}.${key} 값이 nullable number가 아닙니다.`)
  }
  return value
}

function readArray(record: Record<string, unknown>, key: string, label: string): unknown[] {
  const value = record[key]
  if (!Array.isArray(value)) {
    throw new Error(`${label}.${key} 값이 배열이 아닙니다.`)
  }
  return value
}

function readLiteral<T extends string>(
  record: Record<string, unknown>,
  key: string,
  allowed: readonly T[],
  label: string,
): T {
  const value = record[key]
  if (typeof value !== "string" || !allowed.includes(value as T)) {
    throw new Error(`${label}.${key} 값이 예상한 literal이 아닙니다.`)
  }
  return value as T
}

function decodeCounts(input: unknown): CountSummary {
  const record = asRecord(input, "counts")
  return {
    PENDING: readNumber(record, "PENDING", "counts"),
    PROCESSING: readNumber(record, "PROCESSING", "counts"),
    DONE: readNumber(record, "DONE", "counts"),
    ERROR: readNumber(record, "ERROR", "counts"),
    UNREGISTERED: readNumber(record, "UNREGISTERED", "counts"),
  }
}

function decodeFolders(input: unknown): FolderSummary {
  const record = asRecord(input, "folders")
  return {
    inbox: readString(record, "inbox", "folders"),
    audio: readString(record, "audio", "folders"),
    transcripts: readString(record, "transcripts", "folders"),
    errors: readString(record, "errors", "folders"),
  }
}

function decodeRuntimeState(input: unknown): RuntimeState {
  const record = asRecord(input, "runtime_state")
  return {
    status: readLiteral(record, "status", RUNTIME_STATUSES, "runtime_state"),
    source: readLiteral(record, "source", RUNTIME_SOURCES, "runtime_state"),
    paused: readBoolean(record, "paused", "runtime_state"),
    managed_running: readBoolean(record, "managed_running", "runtime_state"),
    external_worker_pids: readArray(record, "external_worker_pids", "runtime_state").map((value) => {
      if (typeof value !== "number" || !Number.isInteger(value)) {
        throw new Error("runtime_state.external_worker_pids 값이 정수 배열이 아닙니다.")
      }
      return value
    }),
    label: readString(record, "label", "runtime_state"),
    description: readString(record, "description", "runtime_state"),
  }
}

function decodePanelJob(input: unknown): PanelJob {
  const record = asRecord(input, "jobs_v2[]")
  return {
    id: readNumber(record, "id", "jobs_v2[]"),
    status: readString(record, "status", "jobs_v2[]"),
    file_name: readString(record, "file_name", "jobs_v2[]"),
    updated_at: readString(record, "updated_at", "jobs_v2[]"),
    step: readString(record, "step", "jobs_v2[]"),
    progress_pct: readNumber(record, "progress_pct", "jobs_v2[]"),
    error_message: readString(record, "error_message", "jobs_v2[]"),
  }
}

function decodeProcessingJob(input: unknown): ProcessingJob {
  const record = asRecord(input, "processing_v2[]")
  return {
    id: readNumber(record, "id", "processing_v2[]"),
    file_name: readString(record, "file_name", "processing_v2[]"),
    step: readString(record, "step", "processing_v2[]"),
    progress_pct: readNumber(record, "progress_pct", "processing_v2[]"),
    eta_sec: readNullableNumber(record, "eta_sec", "processing_v2[]"),
    eta_label: readString(record, "eta_label", "processing_v2[]"),
  }
}

function decodeEndpoints(input: unknown): PanelEndpoints {
  const record = asRecord(input, "actions.endpoints")
  return {
    state: readString(record, "state", "actions.endpoints"),
    logs: readString(record, "logs", "actions.endpoints"),
    notification: readOptionalString(record, "notification") ?? "/api/notification",
    start: readString(record, "start", "actions.endpoints"),
    pause: readString(record, "pause", "actions.endpoints"),
    resume: readString(record, "resume", "actions.endpoints"),
    stop: readString(record, "stop", "actions.endpoints"),
    refresh: readString(record, "refresh", "actions.endpoints"),
    exit: readString(record, "exit", "actions.endpoints"),
    clear_history: readString(record, "clear_history", "actions.endpoints"),
  }
}

function decodeActions(input: unknown): ActionSummary {
  const record = asRecord(input, "actions")
  return {
    pause_action: readLiteral(record, "pause_action", PAUSE_ACTIONS, "actions"),
    pause_label: readString(record, "pause_label", "actions"),
    endpoints: decodeEndpoints(record.endpoints),
  }
}

function decodeNotificationOption(input: unknown): NotificationOption {
  const record = asRecord(input, "notification.options[]")
  return {
    id: readLiteral(record, "id", NOTIFICATION_SELECTIONS, "notification.options[]"),
    label: readString(record, "label", "notification.options[]"),
    description: readString(record, "description", "notification.options[]"),
    available: readBoolean(record, "available", "notification.options[]"),
  }
}

function decodeNotificationState(input: unknown): NotificationState {
  const record = asRecord(input, "notification")
  return {
    selection: readLiteral(record, "selection", NOTIFICATION_SELECTIONS, "notification"),
    selected_label: readString(record, "selected_label", "notification"),
    apply_label: readString(record, "apply_label", "notification"),
    restart_required: readBoolean(record, "restart_required", "notification"),
    can_apply_now: readBoolean(record, "can_apply_now", "notification"),
    options: readArray(record, "options", "notification").map(decodeNotificationOption),
  }
}

function decodeNotificationStateOrFallback(input: unknown): NotificationState {
  if (input === undefined) {
    return {
      selection: "disabled",
      selected_label: "미지원 백엔드",
      apply_label: "웹 패널 백엔드를 재시작하면 알림 설정을 사용할 수 있습니다.",
      restart_required: false,
      can_apply_now: false,
      options: [
        {
          id: "telegram",
          label: "텔레그램만",
          description: "현재 실행 중인 패널 백엔드는 이 설정을 아직 제공하지 않습니다.",
          available: false,
        },
        {
          id: "discord",
          label: "디스코드만",
          description: "현재 실행 중인 패널 백엔드는 이 설정을 아직 제공하지 않습니다.",
          available: false,
        },
        {
          id: "both",
          label: "둘 다",
          description: "현재 실행 중인 패널 백엔드는 이 설정을 아직 제공하지 않습니다.",
          available: false,
        },
        {
          id: "disabled",
          label: "끄기",
          description: "웹 패널 백엔드 재시작 전까지는 읽기 전용 상태입니다.",
          available: true,
        },
      ],
    }
  }
  return decodeNotificationState(input)
}

function decodeSummary(input: unknown): PanelSummary {
  const record = asRecord(input, "summary")
  return {
    current_job: record.current_job === null ? null : decodeProcessingJob(record.current_job),
    progress_label: readString(record, "progress_label", "summary"),
    eta_label: readString(record, "eta_label", "summary"),
    notice: readString(record, "notice", "summary"),
    refresh_hint: readString(record, "refresh_hint", "summary"),
    updated_at: readString(record, "updated_at", "summary"),
    poll_interval_sec: readNumber(record, "poll_interval_sec", "summary"),
  }
}

export function decodePanelState(input: unknown): PanelState {
  const record = asRecord(input, "state")
  if (record.schema_version !== 2) {
    throw new Error("지원하지 않는 panel schema_version 입니다.")
  }

  return {
    schema_version: 2,
    runtime_state: decodeRuntimeState(record.runtime_state),
    notification: decodeNotificationStateOrFallback(record.notification),
    counts: decodeCounts(record.counts),
    folders: decodeFolders(record.folders),
    actions: decodeActions(record.actions),
    summary: decodeSummary(record.summary),
    jobs_v2: readArray(record, "jobs_v2", "state").map(decodePanelJob),
    processing_v2: readArray(record, "processing_v2", "state").map(decodeProcessingJob),
  }
}

export function decodeLogPayload(input: unknown): LogPayload {
  const record = asRecord(input, "logs")
  return {
    offset: readNumber(record, "offset", "logs"),
    text: readString(record, "text", "logs"),
  }
}

export function decodePanelActionResponse(input: unknown): PanelActionResponse {
  const record = asRecord(input, "action")
  if (typeof record.ok !== "boolean") {
    throw new Error("action.ok 값이 불리언이 아닙니다.")
  }
  if (record.ok) {
    const notice = record.notice
    if (notice !== undefined && typeof notice !== "string") {
      throw new Error("action.notice 값이 문자열이 아닙니다.")
    }
    return {
      ok: true,
      notice,
    }
  }
  return {
    ok: false,
    error: readString(record, "error", "action"),
  }
}

export function isPanelAction(action: string): action is PanelAction {
  return PANEL_ACTIONS.includes(action as PanelAction)
}
