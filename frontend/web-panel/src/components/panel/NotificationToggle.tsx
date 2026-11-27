import type { NotificationSelection, NotificationState } from "../../types"

interface NotificationToggleProps {
  notification: NotificationState
  pendingSelection: NotificationSelection | null
  canControl: boolean
  onSave: (selection: NotificationSelection) => void
}

export function NotificationToggle({ notification, pendingSelection, canControl, onSave }: NotificationToggleProps) {
  const available = notification.options.find((option) => option.id === "telegram")?.available ?? false
  const enabled = notification.selection === "telegram" && available
  return (
    <div className="notification-control">
      <div className="notification-channel"><img src="/icons/telegram.svg" alt="" /><div><strong>Telegram</strong><span>{available ? "작업 완료 및 확인 요청" : "연결되지 않음"}</span></div><span className={enabled && available ? "channel-state is-on" : "channel-state"}>{enabled && available ? "사용 중" : "꺼짐"}</span></div>
      <label className="notification-switch-row"><span>알림 사용</span><input type="checkbox" role="switch" checked={enabled} disabled={!canControl || !available || pendingSelection !== null} onChange={(event) => onSave(event.target.checked ? "telegram" : "disabled")} /><span className="switch-track" aria-hidden="true" /></label>
      <p className="notice-copy">변경한 설정은 다음 작업부터 적용됩니다.</p>
    </div>
  )
}
