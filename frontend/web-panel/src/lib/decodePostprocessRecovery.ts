export type RecoveryView = "missing" | "confirmed"

export interface RecoveryJob {
  id: number
  title: string
  recorded_at: string | null
  reason: string
  status: string
  course_code: string | null
  correction_status: string
  summary_status: string
  delivery_status: string
}

export interface RecoveryDetail {
  job: RecoveryJob
  courses: { code: string; name: string }[]
  can_requeue: boolean
  source_txt_sha256: string | null
  source_json_sha256: string | null
}

export interface RequeueRequest {
  course_code: string
  source_txt_sha256: string
  source_json_sha256: string
}

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

function job(value: unknown): value is RecoveryJob {
  return record(value)
    && typeof value.id === "number" && Number.isSafeInteger(value.id) && value.id > 0
    && typeof value.title === "string" && typeof value.reason === "string"
    && (value.recorded_at === null || typeof value.recorded_at === "string")
    && (value.course_code === null || typeof value.course_code === "string")
    && typeof value.status === "string" && typeof value.correction_status === "string"
    && typeof value.summary_status === "string" && typeof value.delivery_status === "string"
}

function digest(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value)
}

export function decodeRecoveryList(value: unknown): { enabled: boolean; total: number; items: RecoveryJob[] } {
  if (!record(value) || typeof value.enabled !== "boolean" || typeof value.total !== "number"
      || !Number.isSafeInteger(value.total) || value.total < 0
      || !Array.isArray(value.items) || !value.items.every(job)) {
    throw new Error("강의 복구 목록을 확인할 수 없습니다.")
  }
  return { enabled: value.enabled, total: value.total, items: value.items }
}

export function decodeRecoveryDetail(value: unknown, id: number): RecoveryDetail {
  if (!record(value) || !job(value.job) || value.job.id !== id
      || typeof value.can_requeue !== "boolean" || !Array.isArray(value.courses)
      || !value.courses.every((course): course is { code: string; name: string } =>
        record(course) && typeof course.code === "string" && course.code.length > 0 && typeof course.name === "string")
      || !(digest(value.source_txt_sha256) || (!value.can_requeue && value.source_txt_sha256 === null))
      || !(digest(value.source_json_sha256) || (!value.can_requeue && value.source_json_sha256 === null))) {
    throw new Error("강의 복구 상세 정보를 확인할 수 없습니다.")
  }
  return { job: value.job, courses: value.courses, can_requeue: value.can_requeue,
    source_txt_sha256: value.source_txt_sha256, source_json_sha256: value.source_json_sha256 }
}

export function decodeRequeueResult(value: unknown, id: number) {
  if (!record(value) || value.id !== id || typeof value.status !== "string") {
    throw new Error("재등록 결과를 확인할 수 없습니다. 작업 상태를 다시 조회하세요.")
  }
  return { id, status: value.status }
}
