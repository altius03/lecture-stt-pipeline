import { useEffect, useState } from "react"

import { ArchiveReviewPanel } from "./ArchiveReviewPanel"
import { RecordingLibraryPanel } from "./RecordingLibraryPanel"
import { TimetablePanel } from "./TimetablePanel"
import { TitleSuggestionPanel } from "./TitleSuggestionPanel"
import { SectionCard } from "../ui/SectionCard"
import { useUnifiedReviewFeed } from "../../hooks/useUnifiedReviewFeed"

const PAGE_LIMIT = 100

export type UnifiedReviewRouteTarget =
  | {
      source: "archive"
      caseKey: string
    }
  | {
      source: "timetable"
      proposalId: number
    }
  | {
      source: "title"
      proposalId: number
    }
  | {
      source: "recording"
      storageKey: string
    }

interface UnifiedReviewPanelProps {
  selectedTarget: UnifiedReviewRouteTarget | null
}

function ledgerStatusLabel(status: ReturnType<typeof useUnifiedReviewFeed>["ledgers"][number]["status"]): string {
  switch (status) {
    case "ready":
      return "ready"
    case "unavailable":
      return "disabled"
    case "error":
      return "error"
    case "loading":
    default:
      return "loading"
  }
}

function ledgerStatusTone(
  status: ReturnType<typeof useUnifiedReviewFeed>["ledgers"][number]["status"],
): "status-active" | "status-negative" | "status-neutral" {
  switch (status) {
    case "ready":
      return "status-active"
    case "error":
      return "status-negative"
    case "unavailable":
    case "loading":
    default:
      return "status-neutral"
  }
}

function sourceLabel(source: ReturnType<typeof useUnifiedReviewFeed>["items"][number]["source"]): string {
  switch (source) {
    case "archive":
      return "archive"
    case "timetable":
      return "classification"
    case "title":
      return "title"
    case "recording":
      return "recording"
    default:
      return source
  }
}

function queueHref(target: UnifiedReviewRouteTarget): string {
  switch (target.source) {
    case "archive":
      return `#review/archive/${target.caseKey}`
    case "timetable":
      return `#review/timetable/${target.proposalId}`
    case "title":
      return `#review/title/${target.proposalId}`
    case "recording":
      return `#review/recording/${target.storageKey}`
    default:
      return "#review"
  }
}

function formatTimestamp(value: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) {
    return value
  }
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed)
}

function workbenchTitle(target: UnifiedReviewRouteTarget): string {
  switch (target.source) {
    case "archive":
      return `archive:${target.caseKey}`
    case "timetable":
      return `timetable:${target.proposalId}`
    case "title":
      return `title:${target.proposalId}`
    case "recording":
      return `recording:${target.storageKey}`
    default:
      return "selected review"
  }
}

function clampOffset(offset: number, total: number, limit: number): number {
  if (total <= 0) {
    return 0
  }
  const lastPageOffset = Math.floor((total - 1) / limit) * limit
  return Math.min(offset, lastPageOffset)
}

export function UnifiedReviewPanel({ selectedTarget }: UnifiedReviewPanelProps) {
  const [offset, setOffset] = useState(0)
  const { items, ledgers, hasHealthySource, hasVisibleItems, isLoading, pagination } = useUnifiedReviewFeed({
    offset,
    limit: PAGE_LIMIT,
  })
  const degradedSources = ledgers.filter((ledger) => ledger.status === "error" || ledger.status === "unavailable")
  const selectedHref = selectedTarget ? queueHref(selectedTarget) : null
  const hasResolvedFeed = ledgers.some((ledger) => ledger.status === "ready" || ledger.status === "unavailable")
  const rangeStart = items.length > 0 ? pagination.offset + 1 : 0
  const rangeEnd = items.length > 0 ? pagination.offset + items.length : 0

  useEffect(() => {
    if (isLoading || !hasResolvedFeed) {
      return
    }
    const nextOffset = clampOffset(pagination.offset, pagination.total, pagination.limit)
    if (nextOffset !== pagination.offset) {
      setOffset(nextOffset)
    }
  }, [hasResolvedFeed, isLoading, pagination.limit, pagination.offset, pagination.total])

  return (
    <section
      className={
        selectedTarget
          ? "storage-v2-grid unified-review-layout has-selected-target"
          : "storage-v2-grid unified-review-layout"
      }
      aria-label="Unified review queue"
    >
      <SectionCard
        label="Source Ledger"
        title="검토 source ledger"
        tone="muted"
        className="unified-review-ledger-card"
      >
        <div className="recording-index-list unified-review-ledger-grid" role="list" aria-label="검토 source 상태">
          {ledgers.map((ledger) => (
            <article
              key={ledger.source}
              className={`recording-index-item unified-review-ledger-item is-${ledger.status}`}
              role="listitem"
            >
              <div className="recording-index-link" aria-label={`${ledger.label} 상태`}>
                <span className={`status-pill unified-review-ledger-status ${ledgerStatusTone(ledger.status)}`}>
                  {ledgerStatusLabel(ledger.status)}
                </span>
                <div className="recording-index-copy unified-review-ledger-body">
                  <strong className="unified-review-ledger-label">{ledger.label}</strong>
                  <span className="unified-review-ledger-note">{ledger.note}</span>
                </div>
                <div className="recording-index-copy unified-review-ledger-count">
                  <strong className="unified-review-ledger-summary">
                    {ledger.visibleCount}{ledger.totalCount !== null ? ` / ${ledger.totalCount}` : ""}
                  </strong>
                  <span>{ledger.error ?? "fresh response"}</span>
                </div>
              </div>
            </article>
          ))}
        </div>
      </SectionCard>

      {selectedTarget ? (
        <section className="dashboard-screen-grid unified-review-workbench-grid" aria-label="Selected review workbench">
          <SectionCard label="Workbench" title={workbenchTitle(selectedTarget)} className="unified-review-workbench-card">
            <p className="notice-copy">
              <a href="#review">#review로 돌아가기</a>
            </p>
            <p className="inline-muted">
              source queue는 유지한 채 archive evidence, timetable, 제목 제안, recording 보관함 패널로 정확히 이동합니다.
            </p>
          </SectionCard>
          {selectedTarget.source === "archive" ? (
            <ArchiveReviewPanel initialSelectedCaseKey={selectedTarget.caseKey} />
          ) : null}
          {selectedTarget.source === "timetable" ? (
            <TimetablePanel initialSelectedProposalId={selectedTarget.proposalId} />
          ) : null}
          {selectedTarget.source === "title" ? (
            <TitleSuggestionPanel
              initialSelectedProposalId={selectedTarget.proposalId}
            />
          ) : null}
          {selectedTarget.source === "recording" ? (
            <RecordingLibraryPanel selectedStorageKey={selectedTarget.storageKey} />
          ) : null}
        </section>
      ) : null}

      <SectionCard label="Unified Queue" title="review runway" className="unified-review-queue-card">
        <div className="unified-review-kicker-row" aria-label="queue summary">
          <span className="status-pill status-neutral">server canonical feed</span>
          {selectedTarget ? <span className="status-pill status-active">deep link active</span> : null}
        </div>
        {isLoading && !hasHealthySource && !hasVisibleItems ? (
          <p className="inline-muted">server canonical feed를 새 응답 기준으로 확인 중입니다.</p>
        ) : null}
        {!isLoading && !hasVisibleItems && hasHealthySource ? (
          <p className="inline-muted">지금 바로 열어야 할 review 항목이 없습니다.</p>
        ) : null}
        {degradedSources.length > 0 ? (
          <p className="notice-copy">
            일부 source는 새 응답이 없어 숨겼습니다: {degradedSources.map((ledger) => ledger.label).join(", ")}
          </p>
        ) : null}
        {items.length > 0 ? (
          <div className="recording-index-list unified-review-queue-list" role="list" aria-label="Unified review items">
            {items.map((item) => {
              const isSelected = selectedHref !== null && item.href === selectedHref
              return (
                <article key={item.id} className="recording-index-item" role="listitem">
                  <a
                    href={item.href}
                    className={isSelected ? "recording-index-link is-current" : "recording-index-link"}
                    aria-current={isSelected ? "page" : undefined}
                  >
                    <span className={`recording-index-id unified-review-source-pill is-${item.source}`}>
                      {sourceLabel(item.source)}
                    </span>
                    <span className="recording-index-copy">
                      <strong>{item.title}</strong>
                      <span>{item.stateLabel} · {item.reasonLabel}</span>
                      <span className="unified-review-meta-line">
                        {formatTimestamp(item.timestamp)} · {item.metaLabel}
                      </span>
                    </span>
                  </a>
                </article>
              )
            })}
          </div>
        ) : null}
        {hasResolvedFeed ? (
          <div className="classification-action-row archive-review-pagination" aria-label="Unified review pagination">
            <button
              type="button"
              className="button-secondary"
              onClick={() => {
                setOffset(Math.max(0, pagination.offset - pagination.limit))
              }}
              disabled={!pagination.hasPrevious || isLoading}
            >
              이전 {pagination.limit}건
            </button>
            <span className="inline-muted">
              현재 {rangeStart}–{rangeEnd} / 총 {pagination.total}건
            </span>
            <button
              type="button"
              className="button-secondary"
              onClick={() => {
                setOffset(pagination.offset + pagination.limit)
              }}
              disabled={!pagination.hasNext || isLoading}
            >
              다음 {pagination.limit}건
            </button>
          </div>
        ) : null}
      </SectionCard>
    </section>
  )
}
