import type { PanelJob } from "../types"

export type ParsedLogLevel = "INFO" | "WARNING" | "ERROR" | "UNKNOWN"
export type ParsedLogKind =
  | "pipeline_started"
  | "pipeline_paused"
  | "pipeline_resumed"
  | "recovery"
  | "file_stabilized"
  | "dedupe_completed"
  | "transcription_started"
  | "postprocess_started"
  | "quality_report"
  | "job_done"
  | "job_failed"
  | "downstream_scan"
  | "downstream_event"
  | "unclassified"

export interface LectureFileInfo {
  rawStem: string
  normalizedStem: string
  dateKey: string
  dateLabel: string
  weekdayLabel: string
  courseCode: string
  courseName: string
  period: number | null
  extension: string | null
  displayTitle: string
  displayMeta: string
}

export interface ParsedLogEntry {
  raw: string
  timestamp: string | null
  timeLabel: string | null
  level: ParsedLogLevel
  kind: ParsedLogKind
  message: string
  summary: string
  detail: string | null
  jobId: string | null
  canonicalBase: string | null
  fileInfo: LectureFileInfo | null
}

const STT_LOG_PATTERN =
  /^(?<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?) (?<level>[A-Z]+) \[job=(?<job>[^ ]+) base=(?<base>[^\]]+)\] (?<message>.*)$/
const GENERIC_LOG_PATTERN =
  /^(?<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?) (?<level>[A-Z]+) (?<message>.*)$/
const FILE_PATTERN = /^(?<date>\d{6})(?<course>DStr|Dstr|DS|LA|LC|OOP|Unix)(?:_(?<period>\d+))?$/

const COURSE_NAMES: Record<string, string> = {
  DS: "데이터사이언스",
  DStr: "자료구조",
  LA: "선형대수학",
  LC: "논리회로",
  OOP: "객체지향언어",
  Unix: "유닉스기초",
}

const WEEKDAY_LABELS = ["일", "월", "화", "수", "목", "금", "토"]

function normalizeCourseCode(courseCode: string): string {
  if (courseCode === "Dstr") {
    return "DStr"
  }
  return courseCode
}

function normalizeCandidate(rawValue: string | null | undefined): string | null {
  if (!rawValue || rawValue === "-") {
    return null
  }

  const trimmed = rawValue.trim()
  if (!trimmed) {
    return null
  }

  const fileName = trimmed.split(/[\\/]/).pop() ?? trimmed
  const withoutExtension = fileName.replace(/\.[^.]+$/, "")
  return withoutExtension.split("__")[0] || null
}

export function parseLectureFileInfo(rawValue: string | null | undefined): LectureFileInfo | null {
  const candidate = normalizeCandidate(rawValue)
  if (!candidate) {
    return null
  }

  const match = FILE_PATTERN.exec(candidate)
  if (!match?.groups) {
    return null
  }

  const dateToken = match.groups.date
  const courseCode = normalizeCourseCode(match.groups.course)
  const periodValue = match.groups.period ? Number.parseInt(match.groups.period, 10) : null

  const year = 2000 + Number.parseInt(dateToken.slice(0, 2), 10)
  const month = Number.parseInt(dateToken.slice(2, 4), 10)
  const day = Number.parseInt(dateToken.slice(4, 6), 10)
  const date = new Date(Date.UTC(year, month - 1, day))
  if (Number.isNaN(date.getTime())) {
    return null
  }

  const weekdayLabel = WEEKDAY_LABELS[date.getUTCDay()] ?? "-"
  const dateKey = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`
  const extensionMatch = rawValue?.match(/(\.[^.]+)$/)
  const extension = extensionMatch ? extensionMatch[1] : null
  const courseName = COURSE_NAMES[courseCode]
  if (!courseName) {
    return null
  }

  return {
    rawStem: candidate,
    normalizedStem: candidate.replace(/^(\d{6})(Dstr)/, "$1DStr"),
    dateKey,
    dateLabel: `${dateKey}(${weekdayLabel})`,
    weekdayLabel,
    courseCode,
    courseName,
    period: Number.isFinite(periodValue) ? periodValue : null,
    extension,
    displayTitle: `${courseName}${periodValue ? ` ${periodValue}교시` : ""}`,
    displayMeta: `${dateKey}(${weekdayLabel}) · ${courseCode}${extension ? ` · ${extension.replace(".", "")}` : ""}`,
  }
}

function formatTimeLabel(timestamp: string | null): string | null {
  if (!timestamp) {
    return null
  }
  const match = timestamp.match(/\d{2}:\d{2}:\d{2}/)
  return match ? match[0] : timestamp
}

function toLevel(rawLevel: string | null | undefined): ParsedLogLevel {
  if (rawLevel === "INFO" || rawLevel === "WARNING" || rawLevel === "ERROR") {
    return rawLevel
  }
  return "UNKNOWN"
}

function describeMessage(message: string, level: ParsedLogLevel): Pick<ParsedLogEntry, "kind" | "summary" | "detail"> {
  if (message === "pipeline start") {
    return { kind: "pipeline_started", summary: "파이프라인을 시작했습니다.", detail: null }
  }
  if (message === "pipeline paused") {
    return { kind: "pipeline_paused", summary: "파이프라인이 일시정지되었습니다.", detail: null }
  }
  if (message === "pipeline resumed") {
    return { kind: "pipeline_resumed", summary: "파이프라인을 다시 시작했습니다.", detail: null }
  }
  if (message.startsWith("Recovered PROCESSING jobs:")) {
    return { kind: "recovery", summary: "중단된 작업 상태를 복구했습니다.", detail: message }
  }
  if (message === "moved to stable folder") {
    return { kind: "file_stabilized", summary: "안정 저장 폴더로 이동했습니다.", detail: null }
  }
  if (message === "dedupe completed") {
    return { kind: "dedupe_completed", summary: "기존 결과를 재사용해 중복 처리를 마쳤습니다.", detail: null }
  }
  if (message === "transcription started") {
    return { kind: "transcription_started", summary: "전사를 시작했습니다.", detail: null }
  }
  if (message.startsWith("quality:")) {
    const detail = message.slice("quality:".length).trim()
    return {
      kind: "quality_report",
      summary: level === "WARNING" ? "품질 경고가 감지되었습니다." : "품질 점검을 완료했습니다.",
      detail: detail || null,
    }
  }
  if (message === "done") {
    return { kind: "job_done", summary: "전사와 저장이 완료되었습니다.", detail: null }
  }
  if (message === "failed") {
    return { kind: "job_failed", summary: "작업 처리 중 오류가 발생했습니다.", detail: null }
  }
  if (message === "Paused: skipping --once") {
    return { kind: "pipeline_paused", summary: "일시정지 상태라 1회 실행을 건너뛰었습니다.", detail: null }
  }
  if (message.includes("후처리")) {
    return { kind: "postprocess_started", summary: "후처리를 진행하고 있습니다.", detail: message }
  }
  if (message.startsWith("downstream scan stats=")) {
    return { kind: "downstream_scan", summary: "후처리 배포 스캔을 완료했습니다.", detail: message }
  }
  if (/^[a-z_]+\s+\{.*\}$/.test(message)) {
    return { kind: "downstream_event", summary: "후처리 이벤트를 기록했습니다.", detail: message }
  }
  return { kind: "unclassified", summary: message, detail: null }
}

function findFileInfo(jobId: string | null, canonicalBase: string | null, jobs: PanelJob[]): LectureFileInfo | null {
  const directBaseInfo = parseLectureFileInfo(canonicalBase)
  if (directBaseInfo) {
    return directBaseInfo
  }

  if (jobId && jobId !== "-") {
    const matchingJob = jobs.find((job) => String(job.id) === jobId)
    const jobFileInfo = parseLectureFileInfo(matchingJob?.file_name)
    if (jobFileInfo) {
      return jobFileInfo
    }
  }

  const normalizedBase = normalizeCandidate(canonicalBase)
  if (!normalizedBase) {
    return null
  }

  for (const job of jobs) {
    const fileInfo = parseLectureFileInfo(job.file_name)
    if (fileInfo && normalizedBase.startsWith(fileInfo.normalizedStem)) {
      return fileInfo
    }
  }

  return null
}

function parseStructuredLine(line: string, jobs: PanelJob[]): ParsedLogEntry {
  const sttMatch = STT_LOG_PATTERN.exec(line)
  if (sttMatch?.groups) {
    const timestamp = sttMatch.groups.timestamp
    const level = toLevel(sttMatch.groups.level)
    const jobId = sttMatch.groups.job === "-" ? null : sttMatch.groups.job
    const canonicalBase = sttMatch.groups.base === "-" ? null : sttMatch.groups.base
    const message = sttMatch.groups.message.trim()
    const description = describeMessage(message, level)

    return {
      raw: line,
      timestamp,
      timeLabel: formatTimeLabel(timestamp),
      level,
      kind: description.kind,
      message,
      summary: description.summary,
      detail: description.detail,
      jobId,
      canonicalBase,
      fileInfo: findFileInfo(jobId, canonicalBase, jobs),
    }
  }

  const genericMatch = GENERIC_LOG_PATTERN.exec(line)
  if (genericMatch?.groups) {
    const timestamp = genericMatch.groups.timestamp
    const level = toLevel(genericMatch.groups.level)
    const message = genericMatch.groups.message.trim()
    const description = describeMessage(message, level)

    return {
      raw: line,
      timestamp,
      timeLabel: formatTimeLabel(timestamp),
      level,
      kind: description.kind,
      message,
      summary: description.summary,
      detail: description.detail,
      jobId: null,
      canonicalBase: null,
      fileInfo: null,
    }
  }

  return {
    raw: line,
    timestamp: null,
    timeLabel: null,
    level: "UNKNOWN",
    kind: "unclassified",
    message: line,
    summary: line,
    detail: null,
    jobId: null,
    canonicalBase: null,
    fileInfo: null,
  }
}

export function parsePanelLogText(text: string, jobs: PanelJob[]): ParsedLogEntry[] {
  return text
    .split("\n")
    .map((line) => line.trimEnd())
    .filter((line) => line.trim().length > 0)
    .map((line) => parseStructuredLine(line, jobs))
}
