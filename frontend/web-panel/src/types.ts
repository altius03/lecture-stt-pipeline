export type RuntimeStatus = "running" | "paused" | "stopped"
export type RuntimeSource = "web" | "external" | "none"
export type NotificationSelection = "telegram" | "discord" | "both" | "disabled"
export const PANEL_ACTIONS = [
  "start",
  "pause",
  "resume",
  "stop",
  "refresh",
  "exit",
  "clear_history",
] as const

export type PanelAction = (typeof PANEL_ACTIONS)[number]
export type PanelEndpointKey = "state" | "logs" | "notification" | PanelAction
export type KnownJobStatus = "PENDING" | "PROCESSING" | "DONE" | "ERROR"
export type JobStatus = KnownJobStatus | (string & {})

export interface CountSummary {
  PENDING: number
  PROCESSING: number
  DONE: number
  ERROR: number
  UNREGISTERED: number
}

export interface FolderSummary {
  inbox: string
  audio: string
  transcripts: string
  errors: string
}

export interface RuntimeState {
  status: RuntimeStatus
  source: RuntimeSource
  paused: boolean
  managed_running: boolean
  external_worker_pids: number[]
  label: string
  description: string
}

export interface PanelJob {
  id: number
  status: JobStatus
  file_name: string
  updated_at: string
  step: string
  progress_pct: number
  error_message: string
}

export interface ProcessingJob {
  id: number
  file_name: string
  step: string
  progress_pct: number
  eta_sec: number | null
  eta_label: string
}

export interface PanelEndpoints {
  state: string
  logs: string
  notification: string
  start: string
  pause: string
  resume: string
  stop: string
  refresh: string
  exit: string
  clear_history: string
}

export interface ActionSummary {
  pause_action: "pause" | "resume"
  pause_label: string
  endpoints: PanelEndpoints
}

export interface NotificationOption {
  id: NotificationSelection
  label: string
  description: string
  available: boolean
}

export interface NotificationState {
  selection: NotificationSelection
  selected_label: string
  apply_label: string
  restart_required: boolean
  can_apply_now: boolean
  options: NotificationOption[]
}

export interface PanelSummary {
  current_job: ProcessingJob | null
  progress_label: string
  eta_label: string
  notice: string
  refresh_hint: string
  updated_at: string
  poll_interval_sec: number
}

export interface PanelState {
  schema_version: 2
  runtime_state: RuntimeState
  notification: NotificationState
  counts: CountSummary
  folders: FolderSummary
  actions: ActionSummary
  summary: PanelSummary
  jobs_v2: PanelJob[]
  processing_v2: ProcessingJob[]
}

export interface LogPayload {
  offset: number
  text: string
}

export interface PanelStreamLogPayload extends LogPayload {
  reset: boolean
}

export type PanelEvent =
  | {
      type: "state"
      state: PanelState
    }
  | {
      type: "log_chunk"
      payload: LogPayload
    }
  | {
      type: "log_reset"
      payload: PanelStreamLogPayload
    }
  | {
      type: "heartbeat"
    }

export type PanelActionResponse =
  | {
      ok: true
      notice?: string
    }
  | {
      ok: false
      error: string
    }
