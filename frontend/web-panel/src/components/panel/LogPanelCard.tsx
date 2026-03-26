import { useMemo, useState, type RefObject } from "react"

import { ActionBar } from "../ui/ActionBar"
import { SectionCard } from "../ui/SectionCard"
import type { PanelLogState } from "../../hooks/usePanelLogs"
import { parsePanelLogText, type ParsedLogEntry } from "../../lib/logParser"
import type { PanelAction, PanelJob } from "../../types"

interface LogPanelCardProps {
  logs: PanelLogState
  jobs: PanelJob[]
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

function entryLevelClassName(entry: ParsedLogEntry): string {
  if (entry.level === "ERROR") {
    return "status-pill status-negative"
  }
  if (entry.level === "WARNING") {
    return "status-pill status-active"
  }
  return "status-pill status-neutral"
}

export function LogPanelCard({
  logs,
  jobs,
  error,
  logRef,
  autoFollow,
  hasUnread,
  pendingAction,
  onLogScroll,
  onToggleAutoFollow,
  onJumpToLatest,
  onAction,
}: LogPanelCardProps) {
  const [viewMode, setViewMode] = useState<"parsed" | "errors" | "raw">("parsed")
  const parsedEntries = useMemo(() => parsePanelLogText(logs.text, jobs), [jobs, logs.text])
  const parsedOperationalEntries = useMemo(
    () => parsedEntries.filter((entry) => entry.kind !== "unclassified" || entry.level !== "UNKNOWN"),
    [parsedEntries],
  )
  const visibleEntries = useMemo(() => {
    const sourceEntries =
      viewMode === "errors"
        ? parsedOperationalEntries.filter((entry) => entry.level === "ERROR" || entry.level === "WARNING")
        : parsedOperationalEntries
    return [...sourceEntries].slice(-16).reverse()
  }, [parsedOperationalEntries, viewMode])
  const warningOrErrorCount = parsedOperationalEntries.filter(
    (entry) => entry.level === "WARNING" || entry.level === "ERROR",
  ).length
  const jobScopedCount = parsedOperationalEntries.filter((entry) => entry.jobId !== null || entry.fileInfo !== null).length

  return (
    <SectionCard label="Logs" title="실행 로그" className="log-card">
      <div className="log-toolbar">
        <div className="log-view-switch" role="tablist" aria-label="로그 보기">
          <button
            className={`button-secondary log-view-tab${viewMode === "parsed" ? " is-active" : ""}`}
            type="button"
            onClick={() => {
              setViewMode("parsed")
            }}
          >
            운영 뷰
          </button>
          <button
            className={`button-secondary log-view-tab${viewMode === "errors" ? " is-active" : ""}`}
            type="button"
            onClick={() => {
              setViewMode("errors")
            }}
          >
            오류만
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
          <p className="log-toolbar-copy">반복 로그를 한국어 운영 이벤트로 정리해 보여줍니다.</p>
        )}
      </div>

      {logs.droppedLineCount > 0 ? (
        <p className="log-meta">오래된 로그 {logs.droppedLineCount}줄을 숨겼습니다.</p>
      ) : null}
      {error ? <p className="log-meta">{error}</p> : null}

      {viewMode === "raw" ? (
        <pre ref={logRef} onScroll={onLogScroll}>{formatLog(logs.text)}</pre>
      ) : (
        <div className="parsed-log-panel">
          <div className="log-summary-grid">
            <article className="log-summary-card">
              <span>운영 이벤트</span>
              <strong>{parsedOperationalEntries.length}</strong>
            </article>
            <article className="log-summary-card">
              <span>경고 / 오류</span>
              <strong>{warningOrErrorCount}</strong>
            </article>
            <article className="log-summary-card">
              <span>파일 연관 이벤트</span>
              <strong>{jobScopedCount}</strong>
            </article>
          </div>

          {visibleEntries.length === 0 ? (
            <div className="parsed-log-empty">
              {viewMode === "errors" ? "아직 경고나 오류가 없습니다." : "운영 로그로 정리할 항목이 아직 없습니다."}
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
      )}

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
