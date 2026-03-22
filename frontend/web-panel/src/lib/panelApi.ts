import { decodeLogPayload, decodePanelActionResponse, decodePanelState } from "./decodePanelState"
import type { LogPayload, PanelActionResponse, PanelEndpoints, PanelState } from "../types"

const FORM_HEADERS = {
  "Content-Type": "application/x-www-form-urlencoded",
}

export const FALLBACK_PANEL_ENDPOINTS: PanelEndpoints = {
  state: "/api/state",
  logs: "/api/logs",
  start: "/api/start",
  pause: "/api/pause",
  resume: "/api/resume",
  stop: "/api/stop",
  refresh: "/api/refresh",
  exit: "/api/exit",
  clear_history: "/api/clear_history",
}

export const PANEL_EVENTS_ENDPOINT = "/api/events"

function toErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch (error) {
    throw new Error(toErrorMessage(error, "서버 응답을 해석하지 못했습니다."))
  }
}

export async function fetchPanelState(endpoint: string = FALLBACK_PANEL_ENDPOINTS.state): Promise<PanelState> {
  const response = await fetch(endpoint, {
    cache: "no-store",
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(`Failed to fetch state: ${response.status}`)
  }
  return decodePanelState(payload)
}

export async function fetchLogs(endpoint: string, offset: number | null): Promise<LogPayload> {
  const query = offset === null ? "" : `?offset=${encodeURIComponent(offset)}`
  const response = await fetch(`${endpoint}${query}`, {
    cache: "no-store",
  })
  const payload = await readJson(response)
  if (!response.ok) {
    throw new Error(`Failed to fetch logs: ${response.status}`)
  }
  return decodeLogPayload(payload)
}

export async function postPanelAction(endpoint: string): Promise<PanelActionResponse> {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: FORM_HEADERS,
    body: "",
  })
  const payload = await readJson(response)
  const decoded = decodePanelActionResponse(payload)
  if (!response.ok) {
    if (!decoded.ok) {
      throw new Error(decoded.error)
    }
    throw new Error(`Failed to run panel action: ${response.status}`)
  }
  if (!decoded.ok) {
    throw new Error(decoded.error)
  }
  return decoded
}
