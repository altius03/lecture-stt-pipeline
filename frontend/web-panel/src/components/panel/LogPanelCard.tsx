import type { RefObject } from "react"

import { ActionBar } from "../ui/ActionBar"
import { SectionCard } from "../ui/SectionCard"
import type { PanelLogState } from "../../hooks/usePanelLogs"
import type { PanelAction } from "../../types"

interface LogPanelCardProps {
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

export function LogPanelCard({
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
}: LogPanelCardProps) {
  return (
    <SectionCard label="Logs" title="실행 로그" className="log-card">
      <div className="log-toolbar">
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

      {logs.droppedLineCount > 0 ? (
        <p className="log-meta">오래된 로그 {logs.droppedLineCount}줄을 숨겼습니다.</p>
      ) : null}
      {error ? <p className="log-meta">{error}</p> : null}

      <pre ref={logRef} onScroll={onLogScroll}>{formatLog(logs.text)}</pre>

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
