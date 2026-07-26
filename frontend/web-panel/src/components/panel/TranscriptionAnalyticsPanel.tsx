import type { CSSProperties, KeyboardEvent } from "react"

import { EmptyState } from "../ui/EmptyState"
import { ErrorState } from "../ui/ErrorState"
import { LoadingBlock } from "../ui/LoadingBlock"
import { SectionCard } from "../ui/SectionCard"
import { useTranscriptionAnalytics } from "../../hooks/useTranscriptionAnalytics"
import type {
  TranscriptionAnalyticsPayload,
  TranscriptionAnalyticsPeriod,
  TranscriptionAnalyticsStatus,
} from "../../types"

const PERIOD_OPTIONS: Array<{
  id: TranscriptionAnalyticsPeriod
  label: string
  compactLabel: string
}> = [
  { id: "day", label: "오늘", compactLabel: "일간" },
  { id: "week", label: "최근 7일", compactLabel: "주간" },
  { id: "month", label: "최근 30일", compactLabel: "월간" },
]

const ANALYTICS_PANEL_ID = "transcription-analytics-period-panel"

const STATUS_LABELS: Record<TranscriptionAnalyticsStatus, string> = {
  queued: "대기",
  processing: "처리중",
  done: "완료",
  needs_review: "검토 필요",
  error: "오류",
  canceled: "취소",
}

const CLASSIFICATION_SOURCE_LABELS: Record<string, string> = {
  manual: "직접 확정",
  schedule_import: "시간표 확정",
  filename_inference: "파일명 추론",
  legacy_import: "기존 기록",
  system: "시스템",
}

function percentage(part: number, total: number): string {
  if (total === 0) {
    return "—"
  }
  return `${Math.round((part / total) * 100)}%`
}

function compactNumber(value: number): string {
  return new Intl.NumberFormat("ko-KR", {
    notation: value >= 10000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(value)
}

function formatDuration(seconds: number | null): string {
  if (seconds === null) {
    return "표본 없음"
  }
  if (seconds < 60) {
    return `${Math.round(seconds)}초`
  }
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) {
    return `${minutes}분`
  }
  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  return remainingMinutes === 0
    ? `${hours}시간`
    : `${hours}시간 ${remainingMinutes}분`
}

function formatTimestamp(
  value: string | null,
  timezone: string,
): string {
  if (value === null) {
    return "아직 없음"
  }
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: timezone,
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value))
}

function barStyle(value: number, maximum: number): CSSProperties {
  const ratio = maximum === 0 ? 0 : value / maximum
  return {
    "--analytics-bar-size": `${Math.max(0, Math.min(1, ratio)) * 100}%`,
  } as CSSProperties
}

function showTimelineAxisLabel(
  period: TranscriptionAnalyticsPeriod,
  index: number,
  length: number,
): boolean {
  if (index === length - 1) {
    return true
  }
  if (period === "month") {
    return index % 5 === 0
  }
  if (period === "day") {
    return index % 3 === 0
  }
  return true
}

function statusTone(status: TranscriptionAnalyticsStatus): string {
  if (status === "error") {
    return "is-error"
  }
  if (status === "needs_review") {
    return "is-review"
  }
  if (status === "done") {
    return "is-done"
  }
  if (status === "processing") {
    return "is-processing"
  }
  return "is-neutral"
}

function AnalyticsKpis({
  analytics,
}: {
  analytics: TranscriptionAnalyticsPayload
}) {
  const { totals, coverage } = analytics
  const attentionCount = totals.needs_review + totals.error
  const kpis = [
    {
      label: "전사량",
      value: compactNumber(totals.jobs),
      detail: analytics.window.label,
    },
    {
      label: "완료율",
      value: percentage(totals.done, totals.jobs),
      detail: `${compactNumber(totals.done)}건 완료`,
    },
    {
      label: "검토 · 오류",
      value: compactNumber(attentionCount),
      detail: `검토 ${totals.needs_review} · 오류 ${totals.error}`,
      tone: attentionCount > 0 ? "is-alert" : "",
    },
    {
      label: "평균 품질",
      value:
        totals.average_quality_score === null
          ? "—"
          : `${Math.round(totals.average_quality_score)}점`,
      detail: `${coverage.quality_scored}/${coverage.jobs_total}건 점수 표본`,
    },
  ]

  return (
    <section
      className="transcription-analytics-kpis"
      aria-label={`${analytics.window.label} 핵심 지표`}
    >
      {kpis.map((kpi) => (
        <article
          key={kpi.label}
          className={`transcription-analytics-kpi ${kpi.tone ?? ""}`.trim()}
        >
          <span>{kpi.label}</span>
          <strong>{kpi.value}</strong>
          <small>{kpi.detail}</small>
        </article>
      ))}
    </section>
  )
}

function VolumeTimeline({
  analytics,
}: {
  analytics: TranscriptionAnalyticsPayload
}) {
  const maximum = Math.max(0, ...analytics.timeline.map((point) => point.jobs))
  if (analytics.totals.jobs === 0) {
    return <EmptyState message="선택한 기간에 집계할 전사 작업이 없습니다." />
  }

  return (
    <div className="transcription-analytics-volume">
      <ol
        className={`transcription-analytics-volume-plot is-${analytics.period}`}
        aria-label={`${analytics.window.label} 전사량 추이. 총 ${analytics.totals.jobs}건`}
      >
        {analytics.timeline.map((point, index) => (
          <li
            key={point.bucket_start}
            className="transcription-analytics-volume-column"
            aria-label={`${point.label}: ${point.jobs}건, 완료 ${point.done}건, 검토 ${point.needs_review}건, 오류 ${point.error}건`}
          >
            <span className="transcription-analytics-volume-value">
              {point.jobs > 0 ? point.jobs : ""}
            </span>
            <span
              className="transcription-analytics-volume-bar"
              style={barStyle(point.jobs, maximum)}
              aria-hidden="true"
            >
              <span className="is-error" style={barStyle(point.error, point.jobs)} />
              <span
                className="is-review"
                style={barStyle(point.needs_review, point.jobs)}
              />
            </span>
            <span
              className="transcription-analytics-axis-label"
              title={point.label}
              aria-hidden="true"
            >
              {showTimelineAxisLabel(
                analytics.period,
                index,
                analytics.timeline.length,
              )
                ? point.label
                : ""}
            </span>
          </li>
        ))}
      </ol>
      <div className="transcription-analytics-legend" aria-label="전사량 범례">
        <span><i className="is-volume" />전체</span>
        <span><i className="is-review" />검토</span>
        <span><i className="is-error" />오류</span>
      </div>
    </div>
  )
}

function QualityDistribution({
  analytics,
}: {
  analytics: TranscriptionAnalyticsPayload
}) {
  const maximum = Math.max(
    0,
    ...analytics.quality_distribution.map((entry) => entry.count),
  )
  return (
    <div className="transcription-analytics-bars">
      {analytics.quality_distribution.map((entry) => (
        <div className="transcription-analytics-bar-row" key={entry.band}>
          <div className="transcription-analytics-bar-label">
            <span>{entry.label}</span>
            <strong>{entry.count}</strong>
          </div>
          <span
            className={`transcription-analytics-horizontal-bar is-quality-${entry.band.replace("-", "_")}`}
            style={barStyle(entry.count, maximum)}
            aria-label={`${entry.label} ${entry.count}건`}
          />
        </div>
      ))}
      <p className="transcription-analytics-footnote">
        점수 있음 {analytics.coverage.quality_scored} · 없음{" "}
        {analytics.coverage.quality_missing} · 유효하지 않음{" "}
        {analytics.coverage.quality_invalid}
      </p>
    </div>
  )
}

function ClassificationDistribution({
  analytics,
}: {
  analytics: TranscriptionAnalyticsPayload
}) {
  const maximum = Math.max(
    0,
    ...analytics.classification_distribution.map((entry) => entry.count),
  )
  return (
    <div className="transcription-analytics-bars">
      {analytics.classification_distribution.map((entry) => {
        const isUnclassified = entry.context_type === null
        return (
          <div
            className="transcription-analytics-bar-row"
            key={`${entry.context_type ?? "unclassified"}:${entry.source ?? "none"}`}
          >
            <div className="transcription-analytics-bar-label">
              <span>
                {entry.label}
                <small>
                  {entry.source === null
                    ? "확정 분류 없음"
                    : CLASSIFICATION_SOURCE_LABELS[entry.source] ?? entry.source}
                </small>
              </span>
              <strong>{entry.count}</strong>
            </div>
            <span
              className={
                isUnclassified
                  ? "transcription-analytics-horizontal-bar is-unclassified"
                  : "transcription-analytics-horizontal-bar is-classified"
              }
              style={barStyle(entry.count, maximum)}
              aria-label={`${entry.label} ${entry.count}건`}
            />
          </div>
        )
      })}
      <p className="transcription-analytics-footnote">
        선택된 context만 분류로 집계합니다. 시간표·파일명 제안만 있는 항목은
        자동 확정하지 않고 미분류로 남습니다.
      </p>
    </div>
  )
}

function StatusDistribution({
  analytics,
}: {
  analytics: TranscriptionAnalyticsPayload
}) {
  return (
    <ul className="transcription-analytics-status-list">
      {analytics.status_distribution.map((entry) => (
        <li key={entry.status}>
          <span className={`status-pill ${statusTone(entry.status)}`}>
            {STATUS_LABELS[entry.status]}
          </span>
          <strong>{entry.count}</strong>
          <small>{percentage(entry.count, analytics.totals.jobs)}</small>
        </li>
      ))}
    </ul>
  )
}

function RecentAttention({
  analytics,
}: {
  analytics: TranscriptionAnalyticsPayload
}) {
  if (analytics.recent_attention.length === 0) {
    return (
      <EmptyState message="선택한 기간에 바로 확인할 검토·오류 항목이 없습니다." />
    )
  }
  return (
    <div className="table-wrap transcription-analytics-attention-table">
      <table>
        <thead>
          <tr>
            <th scope="col">표시 이름</th>
            <th scope="col">상태</th>
            <th scope="col">품질</th>
            <th scope="col">분류</th>
            <th scope="col">집계 시각</th>
            <th scope="col">후속</th>
          </tr>
        </thead>
        <tbody>
          {analytics.recent_attention.map((item) => (
            <tr key={item.storage_key}>
              <td>
                <strong>{item.display_name}</strong>
                <code>{item.storage_key}</code>
              </td>
              <td>
                <span className={`status-pill ${statusTone(item.status)}`}>
                  {STATUS_LABELS[item.status]}
                </span>
              </td>
              <td>
                {item.quality_score === null
                  ? "미측정"
                  : `${Math.round(item.quality_score)}점`}
              </td>
              <td>
                {item.context_type === null ? (
                  <span className="status-pill is-neutral">미분류</span>
                ) : (
                  <span>
                    {item.context_type}
                    <small>
                      {item.classification_source === null
                        ? ""
                        : CLASSIFICATION_SOURCE_LABELS[
                            item.classification_source
                          ] ?? item.classification_source}
                    </small>
                  </span>
                )}
              </td>
              <td>{formatTimestamp(item.event_at, analytics.timezone)}</td>
              <td>
                <a
                  href={`#library/${item.storage_key}`}
                  className="transcription-analytics-attention-link"
                  aria-label={`${item.display_name} 상세 보기`}
                >
                  상세 보기
                </a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function CoverageNote({
  analytics,
}: {
  analytics: TranscriptionAnalyticsPayload
}) {
  const coverage = analytics.coverage
  return (
    <section className="transcription-analytics-coverage" aria-label="집계 범위와 신선도">
      <div>
        <span>마지막 이벤트</span>
        <strong>
          {formatTimestamp(analytics.freshness.latest_event_at, analytics.timezone)}
        </strong>
      </div>
      <div>
        <span>점수 커버리지</span>
        <strong>
          {coverage.quality_scored}/{coverage.jobs_total}
        </strong>
      </div>
      <div>
        <span>분류 커버리지</span>
        <strong>
          {coverage.classification_known}/{coverage.jobs_total}
        </strong>
      </div>
      <div>
        <span>오디오 · 처리 시간</span>
        <strong>
          오디오 {formatDuration(analytics.totals.audio_duration_sec)} (
          {coverage.audio_duration_known}/{coverage.jobs_total}) · 처리{" "}
          {formatDuration(analytics.totals.total_processing_sec)} (
          {coverage.processing_duration_known}/{coverage.jobs_total})
        </strong>
      </div>
      <p>
        {analytics.limits.truncated
          ? `상한 ${analytics.limits.row_limit}건까지만 검토한 부분 집계입니다.`
          : `최대 ${analytics.limits.row_limit}건, scorecard당 ${Math.round(
              analytics.limits.scorecard_max_bytes / 1024,
            )}KiB로 제한한 읽기 전용 집계입니다.`}
        {" "}생성 {formatTimestamp(analytics.freshness.generated_at, analytics.timezone)}
      </p>
    </section>
  )
}

export function TranscriptionAnalyticsPanel() {
  const {
    selectedPeriod,
    selectPeriod,
    analytics,
    isLoading,
    isRefreshing,
    error,
    retry,
  } = useTranscriptionAnalytics({ enabled: true })

  function handlePeriodKeyDown(
    event: KeyboardEvent<HTMLButtonElement>,
    currentIndex: number,
  ) {
    let nextIndex: number | null = null
    if (event.key === "ArrowRight") {
      nextIndex = (currentIndex + 1) % PERIOD_OPTIONS.length
    } else if (event.key === "ArrowLeft") {
      nextIndex =
        (currentIndex - 1 + PERIOD_OPTIONS.length) % PERIOD_OPTIONS.length
    } else if (event.key === "Home") {
      nextIndex = 0
    } else if (event.key === "End") {
      nextIndex = PERIOD_OPTIONS.length - 1
    }

    if (nextIndex === null) {
      return
    }

    event.preventDefault()
    const nextPeriod = PERIOD_OPTIONS[nextIndex]
    selectPeriod(nextPeriod.id)
    event.currentTarget.parentElement
      ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')
      .item(nextIndex)
      .focus()
  }

  return (
    <section
      className="transcription-analytics"
      aria-label="전사 관제 대시보드"
    >
      <header className="transcription-analytics-toolbar">
        <div>
          <p className="section-label">Transcription Observatory</p>
          <h3>전사 흐름 관제</h3>
          <p>
            기간별 처리량에서 품질·분류 커버리지와 사람이 확인할 항목까지
            이어서 봅니다.
          </p>
        </div>
        <div
          className="transcription-analytics-period-tabs"
          role="tablist"
          aria-label="전사 집계 기간"
        >
          {PERIOD_OPTIONS.map((period, index) => (
            <button
              key={period.id}
              id={`transcription-analytics-tab-${period.id}`}
              type="button"
              role="tab"
              aria-selected={selectedPeriod === period.id}
              aria-controls={
                analytics?.available ? ANALYTICS_PANEL_ID : undefined
              }
              tabIndex={selectedPeriod === period.id ? 0 : -1}
              className={selectedPeriod === period.id ? "is-selected" : ""}
              onClick={() => {
                selectPeriod(period.id)
              }}
              onKeyDown={(event) => {
                handlePeriodKeyDown(event, index)
              }}
            >
              <span>{period.label}</span>
              <small>{period.compactLabel}</small>
            </button>
          ))}
        </div>
      </header>

      {isRefreshing ? (
        <p className="transcription-analytics-refresh" role="status">
          선택한 기간을 다시 집계하고 있습니다.
        </p>
      ) : null}

      {isLoading ? (
        <LoadingBlock
          eyebrow="Read-only analytics"
          title="전사 지표를 집계하는 중입니다."
          description="선택한 기간 하나만 on-demand로 읽고 있습니다."
        />
      ) : error ? (
        <ErrorState
          eyebrow="Analytics unavailable"
          title="전사 지표를 불러오지 못했습니다."
          message={error}
          retryLabel="다시 집계"
          onRetry={() => {
            void retry()
          }}
        />
      ) : analytics && !analytics.available ? (
        <SectionCard
          label="Read-only boundary"
          title="Storage v2 분석 API가 비활성화되어 있습니다."
          tone="muted"
          className="transcription-analytics-disabled"
        >
          <EmptyState message="운영 데이터에는 자동 연결하지 않았습니다. 격리 검증 후 storage_v2.analytics.enabled를 명시적으로 켜야 지표가 표시됩니다." />
          <p className="transcription-analytics-footnote">
            상태: <code>{analytics.disabled_reason}</code> · 웹 업로드와
            백그라운드 polling은 추가하지 않았습니다.
          </p>
        </SectionCard>
      ) : analytics ? (
        <div
          id={ANALYTICS_PANEL_ID}
          role="tabpanel"
          aria-label={`${analytics.window.label} 전사 관제`}
          aria-labelledby={`transcription-analytics-tab-${analytics.period}`}
          className="transcription-analytics-content"
        >
          <AnalyticsKpis analytics={analytics} />

          <section className="transcription-analytics-grid is-primary">
            <SectionCard
              label="Throughput"
              title={`${analytics.window.label} 전사량`}
              className="transcription-analytics-volume-card"
            >
              <VolumeTimeline analytics={analytics} />
            </SectionCard>
            <SectionCard label="Outcome" title="작업 상태 분포" tone="muted">
              <StatusDistribution analytics={analytics} />
            </SectionCard>
          </section>

          <section className="transcription-analytics-grid">
            <SectionCard label="Quality" title="품질 점수 분포">
              <QualityDistribution analytics={analytics} />
            </SectionCard>
            <SectionCard
              label="Classification"
              title="확정 분류 분포"
              tone="muted"
            >
              <ClassificationDistribution analytics={analytics} />
            </SectionCard>
          </section>

          <SectionCard
            label="Attention queue"
            title="최근 확인 필요 항목"
            className="transcription-analytics-attention-card"
          >
            <RecentAttention analytics={analytics} />
          </SectionCard>

          <CoverageNote analytics={analytics} />
        </div>
      ) : null}
    </section>
  )
}
