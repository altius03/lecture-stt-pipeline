import { ActivityPanel } from "./components/panel/ActivityPanel"
import { PanelHero } from "./components/panel/PanelHero"
import { ProcessingCard } from "./components/panel/ProcessingCard"
import { RuntimeCard } from "./components/panel/RuntimeCard"
import { ErrorBanner } from "./components/ui/ErrorBanner"
import { ErrorState } from "./components/ui/ErrorState"
import { LoadingBlock } from "./components/ui/LoadingBlock"
import { MetricList } from "./components/ui/MetricList"
import { SectionCard } from "./components/ui/SectionCard"
import { usePanelLogs } from "./hooks/usePanelLogs"
import { usePanelState } from "./hooks/usePanelState"
import { useThemeMode } from "./hooks/useThemeMode"
import { FALLBACK_PANEL_ENDPOINTS } from "./lib/panelApi"

export default function App() {
  const { theme, setThemeMode } = useThemeMode()
  const {
    state,
    isLoading,
    error,
    pendingAction,
    pendingNotificationSelection,
    pendingNotificationApplyNow,
    runAction,
    saveNotificationSelection,
    retry,
    logResetKey,
    realtimeConnected,
  } = usePanelState()
  const {
    logs,
    error: logsError,
    logRef,
    autoFollow,
    hasUnread,
    handleLogScroll,
    jumpToLatest,
    setAutoFollow,
  } = usePanelLogs({
    endpoint: state?.actions.endpoints.logs ?? FALLBACK_PANEL_ENDPOINTS.logs,
    pollIntervalSec: state?.summary.poll_interval_sec ?? 1,
    enabled: state !== null,
    resetKey: logResetKey,
  })

  if (isLoading) {
    return (
      <main className="app-shell">
        <LoadingBlock
          eyebrow="Lecture STT"
          title="패널을 준비하는 중입니다."
          description="상태 스냅샷과 최근 로그를 불러와 운영 패널을 초기화하고 있습니다."
        />
      </main>
    )
  }

  if (!state) {
    return (
      <main className="app-shell">
        <ErrorState
          eyebrow="Lecture STT"
          title="패널 상태를 불러오지 못했습니다."
          message={error ?? "알 수 없는 오류"}
          retryLabel="다시 시도"
          onRetry={() => {
            void retry()
          }}
        />
      </main>
    )
  }

  const queueItems = [
    { label: "대기(DB)", value: state.counts.PENDING },
    { label: "미등록(inbox)", value: state.counts.UNREGISTERED },
    { label: "처리중", value: state.counts.PROCESSING },
    { label: "완료", value: state.counts.DONE },
    { label: "오류", value: state.counts.ERROR },
  ]

  const folderItems = [
    { label: "00_inbox", value: state.folders.inbox },
    { label: "01_audio", value: state.folders.audio },
    { label: "02_transcripts", value: state.folders.transcripts },
    { label: "99_errors", value: state.folders.errors },
  ]

  const sharedError = error ?? logsError

  return (
    <main className="app-shell">
      <PanelHero
        runtimeState={state.runtime_state}
        summary={state.summary}
        realtimeConnected={realtimeConnected}
        notification={state.notification}
        pendingNotificationSelection={pendingNotificationSelection}
        pendingNotificationApplyNow={pendingNotificationApplyNow}
        theme={theme}
        onNotificationSave={(selection, applyNow) => {
          void saveNotificationSelection(selection, applyNow)
        }}
        onThemeChange={setThemeMode}
      />

      {sharedError ? <ErrorBanner message={sharedError} /> : null}

      <section className="control-layout" aria-label="Lecture STT 운영 현황">
        <ProcessingCard jobs={state.processing_v2} />

        <section className="overview-grid" aria-label="제어 및 요약">
          <RuntimeCard
            runtimeState={state.runtime_state}
            summary={state.summary}
            pauseAction={state.actions.pause_action}
            pauseLabel={state.actions.pause_label}
            pendingAction={pendingAction}
            onAction={(action) => {
              void runAction(action)
            }}
          />

          <SectionCard label="Queue Ledger" title="작업 장부" tone="muted">
            <MetricList items={queueItems} />
          </SectionCard>

          <SectionCard label="Storage" title="폴더 현황">
            <MetricList items={folderItems} />
          </SectionCard>
        </section>

        <ActivityPanel
          jobs={state.jobs_v2}
          logs={logs}
          error={logsError}
          logRef={logRef}
          autoFollow={autoFollow}
          hasUnread={hasUnread}
          pendingAction={pendingAction}
          onLogScroll={handleLogScroll}
          onToggleAutoFollow={setAutoFollow}
          onJumpToLatest={jumpToLatest}
          onAction={(action) => {
            void runAction(action)
          }}
        />
      </section>
    </main>
  )
}
