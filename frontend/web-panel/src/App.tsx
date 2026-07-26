import { useEffect, useState } from "react"

import { ActivityPanel } from "./components/panel/ActivityPanel"
import {
  DashboardScreenNav,
  type DashboardScreen,
  type DashboardScreenItem,
} from "./components/panel/DashboardScreenNav"
import { PanelHero } from "./components/panel/PanelHero"
import { ProcessingCard } from "./components/panel/ProcessingCard"
import { RecordingLibraryPanel } from "./components/panel/RecordingLibraryPanel"
import { RuntimeCard } from "./components/panel/RuntimeCard"
import { TimetablePanel } from "./components/panel/TimetablePanel"
import { TranscriptionAnalyticsPanel } from "./components/panel/TranscriptionAnalyticsPanel"
import {
  UnifiedReviewPanel,
  type UnifiedReviewRouteTarget,
} from "./components/panel/UnifiedReviewPanel"
import { ErrorBanner } from "./components/ui/ErrorBanner"
import { EmptyState } from "./components/ui/EmptyState"
import { ErrorState } from "./components/ui/ErrorState"
import { LoadingBlock } from "./components/ui/LoadingBlock"
import { MetricList } from "./components/ui/MetricList"
import { SectionCard } from "./components/ui/SectionCard"
import { usePanelLogs } from "./hooks/usePanelLogs"
import { usePanelState } from "./hooks/usePanelState"
import { useThemeMode } from "./hooks/useThemeMode"
import { FALLBACK_PANEL_ENDPOINTS, PANEL_EVENTS_ENDPOINT } from "./lib/panelApi"

const DASHBOARD_SCREENS: DashboardScreen[] = ["home", "processing", "library", "review", "timetable", "settings"]
const STORAGE_KEY_PATTERN = /^[0-9A-Za-z_-]+$/

const SCREEN_COPY: Record<
  DashboardScreen,
  {
    eyebrow: string
    title: string
    description: string
  }
> = {
  home: {
    eyebrow: "Observability",
    title: "전사 운영과 품질을 한눈에",
    description: "일간·주간·월간 처리량에서 품질 점수, 확정 분류, 확인 필요 항목까지 이어서 보는 개인 STT 관제 화면입니다.",
  },
  processing: {
    eyebrow: "Processing",
    title: "워커 진행과 운영 기록",
    description: "현재 작업, 작업 장부, 운영 로그를 한 화면에서 따라가며 중단 없이 상태를 확인합니다.",
  },
  library: {
    eyebrow: "Library",
    title: "녹음 보관 경로와 revision 경계",
    description: "원본 업로드 흐름은 유지한 채 읽기 전용 보관 경계만 노출하고, 없는 인덱스는 추정해서 만들지 않습니다.",
  },
  review: {
    eyebrow: "Review",
    title: "네 검토 source를 한 줄로 연결",
    description: "archive evidence, 시간표 분류 제안, 내용 기반 제목 제안, 녹음별 남은 review를 함께 훑고 guarded workbench에서 명시적으로 처리합니다.",
  },
  timetable: {
    eyebrow: "Timetable",
    title: "학기별 수업 매핑 검토",
    description: "시간표 제안과 사람이 확정한 분류를 분리해 보고, guard가 있는 작업만 이 화면에서 실행합니다.",
  },
  settings: {
    eyebrow: "Settings",
    title: "운영 기본값과 절전 경계",
    description: "실제로 운영 중인 알림과 새로고침 값만 보여주고, 아직 없는 설정은 임의 기본값처럼 꾸미지 않습니다.",
  },
}

interface DashboardRoute {
  screen: DashboardScreen
  libraryStorageKey: string | null
  reviewTarget: UnifiedReviewRouteTarget | null
}

function decodeExactHashSegment(rawSegment: string): string | null {
  try {
    const decoded = decodeURIComponent(rawSegment)
    if (decoded.includes("/")) {
      return null
    }
    return encodeURIComponent(decoded) === rawSegment ? decoded : null
  } catch {
    return null
  }
}

function parseCanonicalHashKey(rawSegment: string | undefined): string | null {
  if (!rawSegment) {
    return null
  }
  const decoded = decodeExactHashSegment(rawSegment)
  return decoded && STORAGE_KEY_PATTERN.test(decoded) ? decoded : null
}

function parseCanonicalPositiveId(rawSegment: string | undefined): number | null {
  if (!rawSegment) {
    return null
  }
  const decoded = decodeExactHashSegment(rawSegment)
  if (!decoded || !/^[1-9][0-9]*$/.test(decoded)) {
    return null
  }
  const proposalId = Number(decoded)
  return Number.isSafeInteger(proposalId) ? proposalId : null
}

function readRouteFromHash(): DashboardRoute {
  if (typeof window === "undefined") {
    return {
      screen: "home",
      libraryStorageKey: null,
      reviewTarget: null,
    }
  }
  const normalized = window.location.hash.replace(/^#/, "")
  if (!normalized || normalized === "home") {
    return {
      screen: "home",
      libraryStorageKey: null,
      reviewTarget: null,
    }
  }
  const segments = normalized.split("/")
  const [screen, rawStorageKey] = segments
  if (screen === "library") {
    if (segments.length > 2) {
      return {
        screen: "library",
        libraryStorageKey: null,
        reviewTarget: null,
      }
    }
    return {
      screen: "library",
      libraryStorageKey: parseCanonicalHashKey(rawStorageKey),
      reviewTarget: null,
    }
  }
  if (screen === "review") {
    if (segments.length === 1) {
      return {
        screen: "review",
        libraryStorageKey: null,
        reviewTarget: null,
      }
    }
    if (segments.length !== 3) {
      return {
        screen: "review",
        libraryStorageKey: null,
        reviewTarget: null,
      }
    }
    const [, source, rawTarget] = segments
    const reviewTarget =
      source === "archive"
        ? (() => {
            const caseKey = parseCanonicalHashKey(rawTarget)
            return caseKey === null ? null : { source: "archive" as const, caseKey }
          })()
        : source === "timetable"
          ? (() => {
              const proposalId = parseCanonicalPositiveId(rawTarget)
              return proposalId === null ? null : { source: "timetable" as const, proposalId }
            })()
          : source === "title"
            ? (() => {
                const proposalId = parseCanonicalPositiveId(rawTarget)
                return proposalId === null ? null : { source: "title" as const, proposalId }
              })()
            : source === "recording"
              ? (() => {
                  const storageKey = parseCanonicalHashKey(rawTarget)
                  return storageKey === null ? null : { source: "recording" as const, storageKey }
                })()
              : null
    return {
      screen: "review",
      libraryStorageKey: null,
      reviewTarget,
    }
  }
  return {
    screen: DASHBOARD_SCREENS.includes(screen as DashboardScreen) ? (screen as DashboardScreen) : "home",
    libraryStorageKey: null,
    reviewTarget: null,
  }
}

export default function App() {
  const [currentRoute, setCurrentRoute] = useState<DashboardRoute>(() => readRouteFromHash())
  const currentScreen = currentRoute.screen
  const logsEnabled = currentScreen === "processing"
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
    streamEndpoint,
  } = usePanelState({
    logsEnabled,
  })
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
    eventsEndpoint: streamEndpoint === PANEL_EVENTS_ENDPOINT ? PANEL_EVENTS_ENDPOINT : null,
    pollIntervalSec: state?.summary.poll_interval_sec ?? 1,
    enabled: state !== null && logsEnabled,
    resetKey: logResetKey,
  })

  useEffect(() => {
    if (typeof window === "undefined") {
      return
    }

    const handleHashChange = () => {
      setCurrentRoute(readRouteFromHash())
    }

    window.addEventListener("hashchange", handleHashChange)
    return () => {
      window.removeEventListener("hashchange", handleHashChange)
    }
  }, [])

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

  const panelState = state

  const queueItems = [
    { label: "대기(DB)", value: panelState.counts.PENDING },
    { label: "미등록(inbox)", value: panelState.counts.UNREGISTERED },
    { label: "처리중", value: panelState.counts.PROCESSING },
    { label: "확인 필요", value: panelState.counts.NEEDS_REVIEW },
    { label: "완료", value: panelState.counts.DONE },
    { label: "오류", value: panelState.counts.ERROR },
  ]

  const screenItems: DashboardScreenItem[] = [
    {
      id: "home",
      label: "홈",
      description: "주의가 필요한 흐름만 빠르게 확인",
    },
    {
      id: "processing",
      label: "처리 현황",
      description: "현재 작업과 운영 로그",
      badge: panelState.processing_v2.length > 0 ? String(panelState.processing_v2.length) : null,
    },
    {
      id: "library",
      label: "녹음 보관함",
      description: "보관 경로와 연결 상태",
    },
    {
      id: "review",
      label: "검토 큐",
      description: "런타임 신호와 보관소 검토 항목",
      badge: panelState.counts.NEEDS_REVIEW > 0 ? String(panelState.counts.NEEDS_REVIEW) : null,
    },
    {
      id: "timetable",
      label: "시간표",
      description: "학기별 수업 매핑 검토",
    },
    {
      id: "settings",
      label: "설정",
      description: "알림과 운영 기본값",
    },
  ]
  const sharedError = error ?? logsError
  const currentScreenItem = screenItems.find((item) => item.id === currentScreen) ?? screenItems[0]
  const currentScreenCopy = SCREEN_COPY[currentScreen]
  const screenStatus =
    currentScreen === "processing"
      ? realtimeConnected
        ? "처리 화면에서 상태와 로그를 하나의 실시간 연결로 받고 있습니다."
        : `실시간 연결을 기다리는 동안 ${panelState.summary.poll_interval_sec}초 주기 조회로 상태와 로그를 맞춥니다.`
      : currentScreen === "review"
        ? "네 source는 이 화면에서만 새로 읽습니다. 상태 변경·확정·승격은 각 guarded workbench의 비활성 기본값과 plan/count/digest/allow-write guard를 그대로 따릅니다."
        : currentScreen === "library"
          ? currentRoute.libraryStorageKey
            ? "선택한 storage_key detail은 별도 read-only query로 읽고, 목록 첫 페이지와 독립적으로 유지합니다."
            : "Storage v2 보관함은 첫 페이지 summary와 선택된 recording detail만 on-demand로 읽습니다."
          : currentScreen === "timetable"
            ? "시간표 확정은 plan/count/digest/allow-write guard를 통과한 작업만 실행합니다."
            : currentScreen === "settings"
              ? "알림과 절전 정책 중 이미 운영 중인 값만 노출합니다."
              : "Storage v2 집계는 홈을 열었을 때 선택한 기간 하나만 읽으며, 백그라운드 polling이나 운영 데이터 자동 연결은 하지 않습니다."

  function renderHomeScreen() {
    return (
      <>
        <ProcessingCard jobs={panelState.processing_v2} />
        <TranscriptionAnalyticsPanel />
      </>
    )
  }

  function renderProcessingScreen() {
    return (
      <>
        <ProcessingCard jobs={panelState.processing_v2} />

        <section className="overview-grid dashboard-screen-grid" aria-label="처리 제어">
          <RuntimeCard
            runtimeState={panelState.runtime_state}
            summary={panelState.summary}
            pauseAction={panelState.actions.pause_action}
            pauseLabel={panelState.actions.pause_label}
            pendingAction={pendingAction}
            onAction={(action) => {
              void runAction(action)
            }}
          />

          <SectionCard label="Queue Ledger" title="작업 장부" tone="muted">
            <MetricList items={queueItems} />
          </SectionCard>

          <SectionCard label="Realtime" title="실시간 동기화 경로">
            <MetricList
              items={[
                {
                  label: "상태 동기화",
                  value: realtimeConnected
                    ? "실시간 상태 이벤트 연결됨"
                    : `실시간 연결 대기 중 · ${panelState.summary.poll_interval_sec}초 주기 조회`,
                },
                {
                  label: "로그 수집",
                  value: "처리 현황 화면에서만 활성화",
                },
                {
                  label: "유휴 절전",
                  value: "다른 화면에서는 상태 이벤트만 받아 로그 파일 읽기를 생략",
                },
              ]}
            />
          </SectionCard>
        </section>

        <ActivityPanel
          jobs={panelState.jobs_v2}
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
      </>
    )
  }

  function renderLibraryScreen() {
    return <RecordingLibraryPanel selectedStorageKey={currentRoute.libraryStorageKey} />
  }

  function renderReviewScreen() {
    return <UnifiedReviewPanel selectedTarget={currentRoute.reviewTarget} />
  }

  function renderSettingsScreen() {
    return (
      <section className="dashboard-screen-grid" aria-label="설정">
        <SectionCard label="Runtime Defaults" title="운영 기본값">
          <MetricList
            items={[
              { label: "실행 상태", value: panelState.runtime_state.label },
              { label: "알림 채널", value: panelState.notification.selected_label },
              { label: "새로고침 힌트", value: panelState.summary.refresh_hint },
            ]}
          />
          <p className="notice-copy">알림 변경과 테마 전환은 상단 헤더에서 바로 적용합니다.</p>
        </SectionCard>

        <SectionCard label="Unconnected" title="아직 연결되지 않은 설정" tone="muted">
          <EmptyState message="학기 import, 제목 제안 정책, 리소스 절전 정책의 세부 설정 화면은 아직 붙지 않았습니다." />
          <p className="notice-copy">
            현재 화면은 운영 중인 값만 보여주며, 없는 설정을 임의 기본값으로 노출하지 않습니다.
          </p>
        </SectionCard>
      </section>
    )
  }

  function renderCurrentScreen() {
    switch (currentScreen) {
      case "processing":
        return renderProcessingScreen()
      case "library":
        return renderLibraryScreen()
      case "review":
        return renderReviewScreen()
      case "timetable":
        return <TimetablePanel />
      case "settings":
        return renderSettingsScreen()
      case "home":
      default:
        return renderHomeScreen()
    }
  }

  return (
    <main className="app-shell">
      <PanelHero
        runtimeState={panelState.runtime_state}
        summary={panelState.summary}
        realtimeConnected={realtimeConnected}
        notification={panelState.notification}
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
        <DashboardScreenNav currentScreen={currentScreen} items={screenItems} />
        <section className="dashboard-screen-shell" aria-label={`${currentScreenItem.label} 화면`}>
          <header className="screen-shell-header">
            <p className="screen-shell-kicker">{currentScreenCopy.eyebrow}</p>
            <div className="screen-shell-title-row">
              <h2>{currentScreenCopy.title}</h2>
              {currentScreenItem.badge ? <span className="screen-shell-badge">{currentScreenItem.badge}</span> : null}
            </div>
            <p className="screen-shell-description">{currentScreenCopy.description}</p>
            <p className="screen-shell-status">{screenStatus}</p>
          </header>
          <div className="screen-shell-body">{renderCurrentScreen()}</div>
        </section>
      </section>
    </main>
  )
}
