import { useEffect, useState } from "react"

import { StatusBadge } from "../ui/StatusBadge"
import type { PanelSummary, RuntimeState } from "../../types"
import type { ThemeMode } from "../../hooks/useThemeMode"

interface PanelHeroProps {
  runtimeState: RuntimeState
  summary: PanelSummary
  realtimeConnected: boolean
  theme: ThemeMode
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

export function PanelHero({ runtimeState, summary, realtimeConnected, theme, onThemeChange }: PanelHeroProps) {
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
    <section className="hero-card">
      <div className="hero-copy state-stack">
        <p className="eyebrow">Lecture STT Control Panel</p>
        <div className="hero-heading-row">
          <div className="hero-heading-copy">
            <h1>실시간 운영 패널</h1>
            <p className="hero-description">워커 상태, 큐, 최근 작업, 로그를 한 화면에서 확인하고 제어합니다.</p>
          </div>
          <div className="hero-actions">
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
        </div>
      </div>

      <div className="hero-meta-grid">
        <div className="hero-meta-item">
          <span>마지막 갱신</span>
          <strong>{summary.updated_at}</strong>
        </div>
        <div className="hero-meta-item">
          <span>현재 안내</span>
          <strong>{summary.notice || "운영상 특이사항이 없습니다."}</strong>
        </div>
        <div className="hero-meta-item">
          <span>현재 시간</span>
          <strong>{currentTime}</strong>
          <em className="hero-meta-status">{realtimeConnected ? "실시간 연결됨" : "fallback polling 중"}</em>
        </div>
      </div>
    </section>
  )
}
