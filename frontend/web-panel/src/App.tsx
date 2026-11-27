import { useEffect, useState } from "react"

import { ActivityPanel } from "./components/panel/ActivityPanel"
import { DashboardScreenNav, type DashboardScreen, type DashboardScreenItem } from "./components/panel/DashboardScreenNav"
import { JobDetailPanel, OverviewPanel, QualityReviewPanel } from "./components/panel/JobViews"
import { NotificationToggle } from "./components/panel/NotificationToggle"
import { PostprocessRecoveryPanel } from "./components/panel/PostprocessRecoveryPanel"
import { TranscriptionAnalyticsPanel } from "./components/panel/TranscriptionAnalyticsPanel"
import { PanelHero } from "./components/panel/PanelHero"
import { RecordingLibraryPanel } from "./components/panel/RecordingLibraryPanel"
import { TimetablePanel } from "./components/panel/TimetablePanel"
import { UnifiedReviewPanel, type UnifiedReviewRouteTarget } from "./components/panel/UnifiedReviewPanel"
import { ErrorBanner } from "./components/ui/ErrorBanner"
import { ErrorState } from "./components/ui/ErrorState"
import { LoadingBlock } from "./components/ui/LoadingBlock"
import { usePanelLogs } from "./hooks/usePanelLogs"
import { usePanelState } from "./hooks/usePanelState"
import { useThemeMode } from "./hooks/useThemeMode"
import { FALLBACK_PANEL_ENDPOINTS, PANEL_EVENTS_ENDPOINT } from "./lib/panelApi"

const SCREENS: DashboardScreen[] = ["home", "processing", "library", "review", "timetable", "settings"]
const STORAGE_KEY_PATTERN = /^[0-9A-Za-z_-]+$/

interface DashboardRoute {
  screen: DashboardScreen
  libraryStorageKey: string | null
  reviewTarget: UnifiedReviewRouteTarget | null
  jobId: number | null
  qualityJobId: number | null
  logJobId: number | null
}

function emptyRoute(screen: DashboardScreen = "home"): DashboardRoute {
  return { screen, libraryStorageKey: null, reviewTarget: null, jobId: null, qualityJobId: null, logJobId: null }
}

function decodeExactHashSegment(rawSegment: string): string | null {
  try {
    const decoded = decodeURIComponent(rawSegment)
    return decoded.includes("/") || encodeURIComponent(decoded) !== rawSegment ? null : decoded
  } catch { return null }
}

function parseCanonicalHashKey(rawSegment: string | undefined): string | null {
  if (!rawSegment) return null
  const decoded = decodeExactHashSegment(rawSegment)
  return decoded && STORAGE_KEY_PATTERN.test(decoded) ? decoded : null
}

function parseCanonicalPositiveId(rawSegment: string | undefined): number | null {
  if (!rawSegment) return null
  const decoded = decodeExactHashSegment(rawSegment)
  if (!decoded || !/^[1-9][0-9]*$/.test(decoded)) return null
  const id = Number(decoded)
  return Number.isSafeInteger(id) ? id : null
}

function readRouteFromHash(): DashboardRoute {
  if (typeof window === "undefined") return emptyRoute()
  const hash = window.location.hash.replace(/^#/, "")
  if (!hash) return emptyRoute()
  const segments = hash.split("/")
  const [screen, rawTarget] = segments
  if (screen === "job") return segments.length === 2 ? { ...emptyRoute("job"), jobId: parseCanonicalPositiveId(rawTarget) } : emptyRoute("job")
  if (screen === "processing") return segments.length === 2 ? { ...emptyRoute("processing"), logJobId: parseCanonicalPositiveId(rawTarget) } : emptyRoute("processing")
  if (screen === "library") return segments.length <= 2 ? { ...emptyRoute("library"), libraryStorageKey: parseCanonicalHashKey(rawTarget) } : emptyRoute("library")
  if (screen === "review") {
    if (segments.length === 1) return emptyRoute("review")
    if (segments.length !== 3) return emptyRoute("review")
    const [, source, target] = segments
    if (source === "quality") return { ...emptyRoute("review"), qualityJobId: parseCanonicalPositiveId(target) }
    const reviewTarget = source === "archive"
      ? (() => { const caseKey = parseCanonicalHashKey(target); return caseKey === null ? null : { source: "archive" as const, caseKey } })()
      : source === "timetable"
        ? (() => { const proposalId = parseCanonicalPositiveId(target); return proposalId === null ? null : { source: "timetable" as const, proposalId } })()
        : source === "title"
          ? (() => { const proposalId = parseCanonicalPositiveId(target); return proposalId === null ? null : { source: "title" as const, proposalId } })()
          : source === "recording"
            ? (() => { const storageKey = parseCanonicalHashKey(target); return storageKey === null ? null : { source: "recording" as const, storageKey } })()
            : null
    return { ...emptyRoute("review"), reviewTarget }
  }
  return segments.length === 1 && SCREENS.includes(screen as DashboardScreen) ? emptyRoute(screen as DashboardScreen) : emptyRoute()
}

export default function App() {
  const [route, setRoute] = useState<DashboardRoute>(() => readRouteFromHash())
  const [analyticsOpen, setAnalyticsOpen] = useState(false)
  const logsEnabled = route.screen === "processing"
  const { theme, setThemeMode } = useThemeMode()
  const panel = usePanelState({ logsEnabled })
  const state = panel.state
  const logState = usePanelLogs({
    endpoint: state?.actions.endpoints.logs ?? FALLBACK_PANEL_ENDPOINTS.logs,
    eventsEndpoint: panel.streamEndpoint === PANEL_EVENTS_ENDPOINT ? PANEL_EVENTS_ENDPOINT : null,
    pollIntervalSec: state?.summary.poll_interval_sec ?? 1,
    enabled: state !== null && logsEnabled,
  })

  useEffect(() => {
    const onHashChange = () => setRoute(readRouteFromHash())
    window.addEventListener("hashchange", onHashChange)
    return () => window.removeEventListener("hashchange", onHashChange)
  }, [])

  if (panel.isLoading) return <main className="app-shell"><LoadingBlock eyebrow="transcript sys" title="패널을 준비하는 중입니다." description="작업 상태를 불러오고 있습니다." /></main>
  if (!state) return <main className="app-shell"><ErrorState eyebrow="transcript sys" title="패널 상태를 불러오지 못했습니다." message={panel.error ?? "알 수 없는 오류"} retryLabel="다시 시도" onRetry={() => { void panel.retry() }} /></main>

  const canControl = state.actions.can_control === true
  const qualityCount = state.quality_review_open_count ?? state.jobs_v2.filter((job) => job.status === "DONE" && job.quality_review?.required).length
  const reviewCount = qualityCount + state.counts.NEEDS_REVIEW
  const screenItems: DashboardScreenItem[] = [
    { id: "home", label: "현황", description: "작업 진행과 완료 본문" },
    { id: "review", label: "확인", description: "결과 확인과 중단 작업", badge: reviewCount > 0 ? String(reviewCount) : null },
    { id: "processing", label: "로그", description: "처리 기록과 오류" },
    { id: "settings", label: "설정", description: "작업과 알림" },
    { id: "library", label: "녹음 보관함", description: "보관 기록" },
    { id: "timetable", label: "시간표", description: "강의 분류" },
  ]

  const workerStopped = state.runtime_state.status === "stopped"
  const controlsDisabled = !canControl || panel.pendingAction !== null
  const runAction = (action: Parameters<typeof panel.runAction>[0]) => {
    if (canControl) void panel.runAction(action).catch(() => {})
  }

  function renderScreen() {
    if (!state) return null
    switch (route.screen) {
      case "home":
        return <OverviewPanel jobs={state.jobs_v2} processingCount={state.counts.PROCESSING}
          reviewCount={reviewCount} errorCount={state.counts.ERROR} onRefresh={() => { void panel.retry() }} />
      case "job":
        return route.jobId
          ? <JobDetailPanel job={state.jobs_v2.find((job) => job.id === route.jobId) ?? null} jobId={route.jobId} />
          : <p className="empty-state-message">올바른 작업 ID가 아닙니다. <a href="#home">현황으로 돌아가기</a></p>
      case "review":
        return <>
          <div className="page-title-row"><div><h2>확인할 항목</h2><p>결과를 확인하거나 중단된 작업의 사유를 살펴봅니다.</p></div></div>
          <QualityReviewPanel jobs={state.jobs_v2} openReviewCount={qualityCount}
            needsReviewCount={state.counts.NEEDS_REVIEW} selectedJobId={route.qualityJobId}
            canControl={canControl} onResolved={panel.retry} />
          <PostprocessRecoveryPanel canControl={canControl} />
          <details className="secondary-workbench" open={route.reviewTarget !== null}>
            <summary>분류·보관함 등 기타 확인 작업</summary>
            <UnifiedReviewPanel selectedTarget={route.reviewTarget} />
          </details>
        </>
      case "processing":
        return <>
          <div className="page-title-row"><div><h2>작업 로그</h2><p>작업, 단계, 수준으로 처리 기록을 좁혀 확인합니다.</p></div></div>
          <ActivityPanel jobs={state.jobs_v2} selectedJobId={route.logJobId} logs={logState.logs}
            error={logState.error} logRef={logState.logRef} autoFollow={logState.autoFollow}
            hasUnread={logState.hasUnread} onLogScroll={logState.handleLogScroll}
            onToggleAutoFollow={logState.setAutoFollow} onJumpToLatest={logState.jumpToLatest} />
        </>
      case "settings":
        return <>
          <div className="page-title-row"><div><h2>설정</h2><p>작업 실행과 알림을 조정합니다.</p></div></div>
          <div className="settings-grid">
            <section className="data-surface settings-card">
              <h3>작업 실행</h3><p>자동 처리 워커의 현재 상태와 실행을 관리합니다.</p>
              <div className="setting-state"><span className={`runtime-dot is-${state.runtime_state.status}`} aria-hidden="true" /><strong>{state.runtime_state.label}</strong></div>
              <p>{state.runtime_state.description}</p>
              <button className="button-secondary" type="button" disabled={controlsDisabled}
                onClick={() => runAction(workerStopped ? "start" : state.actions.pause_action)}>
                {workerStopped ? "시작" : state.actions.pause_label}
              </button>
              {!workerStopped && <button type="button" className="button-secondary" disabled={controlsDisabled}
                onClick={() => runAction("stop")}>중지</button>}
            </section>
            <section className="data-surface settings-card">
              <h3>알림 채널</h3><p>작업 완료와 확인 요청을 Telegram으로 받습니다.</p>
              <NotificationToggle notification={state.notification} pendingSelection={panel.pendingNotificationSelection}
                canControl={canControl} onSave={(selection) => { void panel.saveNotificationSelection(selection).catch(() => {}) }} />
            </section>
            <section className="data-surface settings-card">
              <h3>시간표와 강의 분류</h3><p>녹음에 강의를 연결할 때 사용하는 수업 정보를 관리합니다.</p>
              <a className="button-secondary as-button" href="#timetable">시간표 보기 →</a>
            </section>
            <section className="data-surface settings-card">
              <h3>화면</h3><p>작업 현황과 로그를 읽기 편한 방식으로 표시합니다.</p>
              <div className="theme-options" role="group" aria-label="화면 테마">
                <button type="button" aria-pressed={theme === "light"} onClick={() => setThemeMode("light")}>라이트</button>
                <button type="button" aria-pressed={theme === "dark"} onClick={() => setThemeMode("dark")}>다크</button>
              </div>
            </section>
            <section className="data-surface settings-card settings-wide">
              <h3>고급 작업</h3><p>기록을 지우거나 패널을 종료하기 전에 대상과 영향을 확인합니다.</p>
              <div className="advanced-links">
                <a href="#library">녹음 보관함 →</a>
                <button type="button" disabled={controlsDisabled} onClick={() => runAction("clear_history")}>완료 이력 정리</button>
                <button type="button" disabled={controlsDisabled} onClick={() => runAction("exit")}>패널 종료</button>
              </div>
            </section>
          </div>
        </>
      case "library":
        return <>
          <div className="page-title-row"><div><h2>녹음 보관함</h2><p>원본과 전사 revision을 확인합니다.</p></div></div>
          <RecordingLibraryPanel selectedStorageKey={route.libraryStorageKey} />
          <details className="secondary-workbench" open={analyticsOpen}
            onToggle={(event) => setAnalyticsOpen(event.currentTarget.open)}>
            <summary>전사 운영 통계</summary>
            {analyticsOpen && <TranscriptionAnalyticsPanel />}
          </details>
        </>
      case "timetable":
        return <>
          <div className="page-title-row"><div><h2>시간표</h2><p>녹음에 강의를 연결할 때 사용하는 수업 정보를 관리합니다.</p></div></div>
          <TimetablePanel />
        </>
    }
  }

  return <main className="app-shell">
    <aside className="panel-sidebar">
      <DashboardScreenNav currentScreen={route.screen} items={screenItems} runtimeLabel={state.runtime_state.label}
        runtimeStatus={state.runtime_state.status} />
    </aside>
    <div className="panel-main">
      <PanelHero runtimeState={state.runtime_state} />
      {!canControl && <p className="notice-copy" role="status">이 접속은 읽기 전용입니다. 작업을 변경하려면 운영자 권한으로 접속해 주세요.</p>}
      {panel.error || logState.error ? <ErrorBanner message={panel.error ?? logState.error ?? ""} /> : null}
      <div className="panel-content">{renderScreen()}</div>
    </div>
  </main>
}
