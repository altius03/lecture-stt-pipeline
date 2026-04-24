import { useEffect, useState } from "react"

import { NotificationToggle } from "./NotificationToggle"
import { StatusBadge } from "../ui/StatusBadge"
import type { NotificationSelection, NotificationState, PanelSummary, RuntimeState } from "../../types"
import type { ThemeMode } from "../../hooks/useThemeMode"

interface PanelHeroProps {
  runtimeState: RuntimeState
  summary: PanelSummary
  realtimeConnected: boolean
  notification: NotificationState
  pendingNotificationSelection: NotificationSelection | null
  pendingNotificationApplyNow: boolean
  theme: ThemeMode
  onNotificationSave: (selection: NotificationSelection, applyNow?: boolean) => void
  onThemeChange: (nextTheme: ThemeMode) => void
}

function formatCurrentTime(now: Date): string {
  return now.toLocaleTimeString("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  })
}

export function PanelHero({
  runtimeState,
  summary,
  realtimeConnected,
  notification,
  pendingNotificationSelection,
  pendingNotificationApplyNow,
  theme,
  onNotificationSave,
  onThemeChange,
}: PanelHeroProps) {
  const [currentTime, setCurrentTime] = useState(() => formatCurrentTime(new Date()))
  const nextTheme = theme === "dark" ? "light" : "dark"

  useEffect(() => {
    const timerId = window.setInterval(() => {
      setCurrentTime(formatCurrentTime(new Date()))
    }, 1000)

    return () => {
      window.clearInterval(timerId)
    }
  }, [])

  return (
    <header className="panel-header">
      <div className="panel-brand">
        <div className="brand-mark" aria-hidden="true">
          STT
        </div>
        <div>
          <p className="eyebrow">Lecture STT</p>
          <h1>전사 파이프라인 관제판</h1>
        </div>
      </div>

      <div className="panel-header-center" aria-label="현재 패널 상태">
        <div className="header-meta-item">
          <span>마지막 갱신</span>
          <strong>{summary.updated_at}</strong>
        </div>
        <div className="header-meta-item">
          <span>현재 시간</span>
          <strong>{currentTime}</strong>
          <em>{realtimeConnected ? "SSE 연결" : "Polling"}</em>
        </div>
        <div className="header-meta-item header-notice">
          <span>안내</span>
          <strong>{summary.notice || "운영상 특이사항이 없습니다."}</strong>
        </div>
      </div>

      <div className="panel-command-bar">
        <NotificationToggle
          notification={notification}
          pendingSelection={pendingNotificationSelection}
          pendingApplyNow={pendingNotificationApplyNow}
          onSave={onNotificationSave}
        />
        <button
          className="theme-switch"
          data-theme={theme}
          type="button"
          onClick={() => {
            onThemeChange(nextTheme)
          }}
          aria-label={theme === "dark" ? "라이트 모드로 전환" : "다크 모드로 전환"}
          aria-pressed={theme === "dark"}
          title={theme === "dark" ? "라이트 모드" : "다크 모드"}
        >
          <span className="theme-switch-icon theme-switch-icon-light" aria-hidden="true">
            <svg viewBox="0 0 24 24" focusable="false">
              <circle cx="12" cy="12" r="4.25" fill="none" stroke="currentColor" strokeWidth="1.8" />
              <path
                d="M12 2.75v2.5M12 18.75v2.5M21.25 12h-2.5M5.25 12h-2.5M18.54 5.46l-1.77 1.77M7.23 16.77l-1.77 1.77M18.54 18.54l-1.77-1.77M7.23 7.23L5.46 5.46"
                fill="none"
                stroke="currentColor"
                strokeLinecap="round"
                strokeWidth="1.8"
              />
            </svg>
          </span>
          <span className="theme-switch-icon theme-switch-icon-dark" aria-hidden="true">
            <svg viewBox="0 0 24 24" focusable="false">
              <path
                d="M14.6 3.35a8.65 8.65 0 1 0 6.05 14.95 8.95 8.95 0 0 1-10.6-10.6A8.6 8.6 0 0 0 14.6 3.35Z"
                fill="none"
                stroke="currentColor"
                strokeLinejoin="round"
                strokeWidth="1.8"
              />
            </svg>
          </span>
          <span className="theme-switch-thumb" aria-hidden="true" />
        </button>
        <StatusBadge status={runtimeState.status} label={runtimeState.label} />
      </div>
    </header>
  )
}
