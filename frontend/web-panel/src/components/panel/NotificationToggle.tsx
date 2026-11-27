import type { NotificationSelection, NotificationState } from "../../types"

interface NotificationToggleProps {
  canControl?: boolean
  notification: NotificationState
  pendingSelection: NotificationSelection | null
  onSave: (selection: NotificationSelection) => void
}

export function NotificationToggle({ canControl = true, notification, pendingSelection, onSave }: NotificationToggleProps) {
  return (
    <label className="notification-channel">
      알림 채널
      <select
        value={pendingSelection ?? notification.selection}
        disabled={!canControl || pendingSelection !== null}
        onChange={(event) => {
          const option = notification.options.find((item) => item.id === event.target.value)
          if (option?.available) onSave(option.id)
        }}
        title={notification.apply_label}
      >
        {notification.options.map((option) => (
          <option key={option.id} value={option.id} disabled={!option.available}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}
