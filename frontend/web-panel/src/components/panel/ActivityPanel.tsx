import { useEffect, useMemo, useState, type KeyboardEvent, type RefObject } from "react"

import { ActionBar } from "../ui/ActionBar"
import { SectionCard } from "../ui/SectionCard"
import type { PanelLogState } from "../../hooks/usePanelLogs"
import { parseLectureFileInfo, parsePanelLogText, type ParsedLogEntry } from "../../lib/logParser"
import type { PanelAction, PanelJob } from "../../types"

interface ActivityPanelProps {
  jobs: PanelJob[]
  logs: PanelLogState
  error: string | null
  logRef: RefObject<HTMLPreElement>
  autoFollow: boolean
  hasUnread: boolean
  pendingAction: PanelAction | null
  onLogScroll: () => void
  onToggleAutoFollow: (nextValue: boolean) => void
  onJumpToLatest: () => void
  onAction: (action: "clear_history" | "exit") => void
}

function formatLog(text: string): string {
  return text.trim() ? text : "로그가 아직 없습니다."
}

function statusClassName(status: string): string {
  if (status === "DONE") {
    return "status-pill status-positive"
  }
  if (status === "ERROR") {
    return "status-pill status-negative"
  }
  if (status === "PROCESSING") {
    return "status-pill status-active"
  }
  return "status-pill status-neutral"
}

function entryLevelClassName(entry: ParsedLogEntry): string {
  if (entry.level === "ERROR") {
    return "status-pill status-negative"
  }
  if (entry.level === "WARNING") {
    return "status-pill status-active"
  }
  return "status-pill status-neutral"
}

function selectedJobDisplay(job: PanelJob | null): { title: string; meta: string } {
  if (!job) {
    return {
      title: "작업을 선택하면 관련 운영 로그를 볼 수 있습니다.",
      meta: "최근 작업 표에서 행을 선택하세요.",
    }
  }

  const fileInfo = parseLectureFileInfo(job.file_name)
  return {
    title: fileInfo?.displayTitle ?? job.file_name,
    meta: fileInfo?.displayMeta ?? `작업 #${job.id} · ${job.updated_at}`,
  }
}

function isEntryForJob(entry: ParsedLogEntry, job: PanelJob | null): boolean {
  if (!job) {
    return false
  }

  if (entry.jobId === String(job.id)) {
    return true
  }

  const selectedFileInfo = parseLectureFileInfo(job.file_name)
  if (!selectedFileInfo || !entry.fileInfo) {
    return false
  }

  return entry.fileInfo.normalizedStem === selectedFileInfo.normalizedStem
}

function entryTargetLabel(entry: ParsedLogEntry): string {
  if (entry.fileInfo) {
    return entry.fileInfo.displayTitle
  }
  if (entry.jobId) {
    return `작업 #${entry.jobId}`
  }
  return "시스템"
}

function entryMetaLabel(entry: ParsedLogEntry): string {
  if (entry.fileInfo) {
    return entry.fileInfo.displayMeta
  }
  if (entry.canonicalBase) {
    return `base · ${entry.canonicalBase}`
  }
  return "공통 이벤트"
}

function pickDefaultJobId(jobs: PanelJob[]): number | null {
  const processingJob = jobs.find((job) => job.status === "PROCESSING")
  return processingJob?.id ?? jobs[0]?.id ?? null
}

export function ActivityPanel({
  jobs,
  logs,
  error,
  logRef,
  autoFollow,
  hasUnread,
  pendingAction,
  onLogScroll,
  onToggleAutoFollow,
  onJumpToLatest,
  onAction,
}: ActivityPanelProps) {
  const [viewMode, setViewMode] = useState<"selected" | "errors" | "raw">("selected")
  const [selectedJobId, setSelectedJobId] = useState<number | null>(() => pickDefaultJobId(jobs))

  useEffect(() => {
    const nextSelectedJobExists = jobs.some((job) => job.id === selectedJobId)
    if (nextSelectedJobExists) {
      return
    }
    setSelectedJobId(pickDefaultJobId(jobs))
  }, [jobs, selectedJobId])

  const selectedJob = jobs.find((job) => job.id === selectedJobId) ?? null
  const selectedDisplay = selectedJobDisplay(selectedJob)
  const parsedEntries = useMemo(() => parsePanelLogText(logs.text, jobs), [jobs, logs.text])
  const parsedOperationalEntries = useMemo(
    () => parsedEntries.filter((entry) => entry.kind !== "unclassified" || entry.level !== "UNKNOWN"),
    [parsedEntries],
  )
  const selectedEntries = useMemo(
    () => parsedOperationalEntries.filter((entry) => isEntryForJob(entry, selectedJob)),
    [parsedOperationalEntries, selectedJob],
  )
  const errorEntries = useMemo(
    () => parsedOperationalEntries.filter((entry) => entry.level === "ERROR" || entry.level === "WARNING"),
    [parsedOperationalEntries],
  )
  const visibleEntries = useMemo(() => {
    const sourceEntries = viewMode === "errors" ? errorEntries : selectedEntries
    return [...sourceEntries].slice(-5).reverse()
  }, [errorEntries, selectedEntries, viewMode])

  function handleRowKeyDown(event: KeyboardEvent<HTMLTableRowElement>, jobId: number) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault()
      setSelectedJobId(jobId)
    }
  }

  return (
    <SectionCard label="Activity" title="최근 작업과 운영 로그" className="activity-card">
      <div className="table-wrap activity-table">
        <table>
          <colgroup>
            <col className="jobs-col-id" />
            <col className="jobs-col-status" />
            <col className="jobs-col-file" />
            <col className="jobs-col-updated" />
            <col className="jobs-col-progress" />
            <col className="jobs-col-step" />
            <col className="jobs-col-error" />
          </colgroup>
          <thead>
            <tr>
              <th>ID</th>
              <th>상태</th>
              <th>파일</th>
              <th>업데이트</th>
              <th>진행률</th>
              <th>단계</th>
              <th>오류</th>
            </tr>
          </thead>
          <tbody>
            {jobs.length === 0 ? (
              <tr>
                <td colSpan={7}>작업 없음</td>
              </tr>
            ) : (
              jobs.map((job) => {
                const isSelected = selectedJobId === job.id
                return (
                  <tr
                    key={job.id}
                    className={isSelected ? "jobs-row is-selected" : "jobs-row"}
                    tabIndex={0}
                    onClick={() => {
                      setSelectedJobId(job.id)
                    }}
                    onKeyDown={(event) => {
                      handleRowKeyDown(event, job.id)
                    }}
                  >
                    <td className="mono-cell">{job.id}</td>
                    <td className="status-cell"><span className={statusClassName(job.status)}>{job.status}</span></td>
                    <td className="file-cell">{job.file_name}</td>
                    <td className="muted-cell">{job.updated_at}</td>
                    <td className="mono-cell progress-cell">{job.progress_pct}%</td>
                    <td className="step-cell">{job.step || "-"}</td>
                    <td className="error-cell" title={job.error_message || "-"}>{job.error_message || "-"}</td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>

      <div className="activity-detail">
        <div className="activity-selection-card">
          <span>선택 작업</span>
          <strong>{selectedDisplay.title}</strong>
          <p>{selectedDisplay.meta}</p>
        </div>

        <div className="activity-toolbar">
          <div className="log-view-switch" role="tablist" aria-label="작업 로그 보기">
            <button
              className={`button-secondary log-view-tab${viewMode === "selected" ? " is-active" : ""}`}
              type="button"
              onClick={() => {
                setViewMode("selected")
              }}
            >
              선택 작업 로그
            </button>
            <button
              className={`button-secondary log-view-tab${viewMode === "errors" ? " is-active" : ""}`}
              type="button"
              onClick={() => {
                setViewMode("errors")
              }}
            >
              전체 오류
            </button>
            <button
              className={`button-secondary log-view-tab${viewMode === "raw" ? " is-active" : ""}`}
              type="button"
              onClick={() => {
                setViewMode("raw")
              }}
            >
              원본 로그
            </button>
          </div>

          {viewMode === "raw" ? (
            <div className="log-toolbar-actions">
              <label className="log-follow-toggle">
                <input
                  checked={autoFollow}
                  type="checkbox"
                  onChange={(event) => {
                    onToggleAutoFollow(event.target.checked)
                  }}
                />
                자동 따라가기
              </label>
              {hasUnread ? (
                <button className="button-secondary log-jump-button" type="button" onClick={onJumpToLatest}>
                  최신으로 이동
                </button>
              ) : null}
            </div>
          ) : (
            <p className="log-toolbar-copy">
              {viewMode === "selected"
                ? "선택한 작업과 연결된 운영 이벤트를 우선 보여줍니다."
                : "전체 경고와 오류 이벤트만 모아 보여줍니다."}
            </p>
          )}
        </div>

        {logs.droppedLineCount > 0 ? (
          <p className="log-meta">오래된 로그 {logs.droppedLineCount}줄을 숨겼습니다.</p>
        ) : null}
        {error ? <p className="log-meta">{error}</p> : null}

        {viewMode === "raw" ? (
          <pre ref={logRef} onScroll={onLogScroll}>{formatLog(logs.text)}</pre>
        ) : visibleEntries.length === 0 ? (
          <div className="parsed-log-empty">
            {viewMode === "selected"
              ? "선택한 작업과 연결된 운영 이벤트가 아직 없습니다."
              : "아직 경고나 오류가 없습니다."}
          </div>
        ) : (
          <div className="parsed-log-list">
            {visibleEntries.map((entry, index) => (
              <article className="parsed-log-entry" key={`${entry.raw}-${index}`}>
                <div className="parsed-log-header">
                  <div>
                    <strong className="parsed-log-target">{entryTargetLabel(entry)}</strong>
                    <p className="parsed-log-meta">
                      {(entry.timeLabel ?? "시간 미상") + " · " + entryMetaLabel(entry)}
                    </p>
                  </div>
                  <span className={entryLevelClassName(entry)}>{entry.level === "UNKNOWN" ? "기타" : entry.level}</span>
                </div>
                <p className="parsed-log-summary">{entry.summary}</p>
                {entry.detail ? <p className="parsed-log-detail">{entry.detail}</p> : null}
              </article>
            ))}
          </div>
        )}
      </div>

      <ActionBar
        compact
        actions={[
          { action: "clear_history", label: "이력 초기화", tone: "secondary" },
          { action: "exit", label: "패널 종료", tone: "danger" },
        ]}
        pendingAction={pendingAction}
        onAction={(action) => {
          if (action === "clear_history" || action === "exit") {
            onAction(action)
          }
        }}
      />
    </SectionCard>
  )
}
