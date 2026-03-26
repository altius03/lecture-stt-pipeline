import type { NotificationSelection, NotificationState } from "../../types"

interface NotificationToggleProps {
  notification: NotificationState
  pendingSelection: NotificationSelection | null
  pendingApplyNow: boolean
  onSave: (selection: NotificationSelection, applyNow?: boolean) => void
}

const CYCLE_ORDER: NotificationSelection[] = ["both", "discord", "telegram", "disabled"]

function shortLabel(selection: NotificationSelection, selectedLabel: string): string {
  if (selectedLabel === "미지원 백엔드") {
    return "미지원"
  }
  if (selection === "both") {
    return "둘 다"
  }
  if (selection === "discord") {
    return "디스코드"
  }
  if (selection === "telegram") {
    return "텔레그램"
  }
  if (selection === "disabled") {
    return "끔"
  }
  return selectedLabel
}

function TelegramMark() {
  return (
    <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
      <path
        d="M20.42 4.58 3.9 10.94c-1.13.45-1.12 1.08-.21 1.36l4.24 1.32 1.64 5.1c.2.56.1.78.69.78.46 0 .66-.21.92-.46l2.04-1.99 4.24 3.13c.78.43 1.34.21 1.54-.72l2.8-13.2c.3-1.16-.45-1.68-1.38-1.26Z"
        fill="currentColor"
      />
    </svg>
  )
}

function DiscordMark() {
  return (
    <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
      <path
        d="M19.54 5.34A16.85 16.85 0 0 0 15.4 4l-.2.4a15.6 15.6 0 0 1 3.72 1.42 12.02 12.02 0 0 0-3.7-1.18 13.35 13.35 0 0 0-6.44 0 11.8 11.8 0 0 0-3.7 1.18A15.43 15.43 0 0 1 8.8 4.4L8.6 4a16.64 16.64 0 0 0-4.14 1.34C1.84 9.3 1.13 13.16 1.48 16.97a16.92 16.92 0 0 0 5.07 2.56l1.1-1.76a10.94 10.94 0 0 1-1.73-.82l.42-.31c3.33 1.55 6.95 1.55 10.24 0l.43.31c-.56.33-1.14.6-1.74.82l1.1 1.76a16.88 16.88 0 0 0 5.08-2.56c.43-4.42-.74-8.24-2.93-11.63ZM9.34 14.65c-.99 0-1.8-.91-1.8-2.03s.8-2.04 1.8-2.04c1 0 1.81.92 1.8 2.04 0 1.12-.8 2.03-1.8 2.03Zm5.32 0c-.99 0-1.8-.91-1.8-2.03s.8-2.04 1.8-2.04c1 0 1.81.92 1.8 2.04 0 1.12-.8 2.03-1.8 2.03Z"
        fill="currentColor"
      />
    </svg>
  )
}

function DisabledMark() {
  return (
    <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
      <path
        d="M7 7a5 5 0 1 1 10 0v4.14l1.7 3.4a1 1 0 0 1-.9 1.46H6.2a1 1 0 0 1-.9-1.46L7 11.14V7Zm3.73 12.1a1.8 1.8 0 0 0 2.54 0"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.8"
      />
      <path d="m4 4 16 16" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="1.8" />
    </svg>
  )
}

function NotificationMark({ selection, selectedLabel }: { selection: NotificationSelection; selectedLabel: string }) {
  if (selectedLabel === "미지원 백엔드") {
    return <span className="notification-chip-fallback">?</span>
  }
  if (selection === "both") {
    return (
      <span className="notification-chip-icons dual" aria-hidden="true">
        <span className="notification-brand telegram">
          <TelegramMark />
        </span>
        <span className="notification-brand discord">
          <DiscordMark />
        </span>
      </span>
    )
  }
  if (selection === "telegram") {
    return (
      <span className="notification-chip-icons" aria-hidden="true">
        <span className="notification-brand telegram">
          <TelegramMark />
        </span>
      </span>
    )
  }
  if (selection === "discord") {
    return (
      <span className="notification-chip-icons" aria-hidden="true">
        <span className="notification-brand discord">
          <DiscordMark />
        </span>
      </span>
    )
  }
  return (
    <span className="notification-chip-icons" aria-hidden="true">
      <span className="notification-brand disabled">
        <DisabledMark />
      </span>
    </span>
  )
}

export function NotificationToggle({
  notification,
  pendingSelection,
  pendingApplyNow,
  onSave,
}: NotificationToggleProps) {
  const availableSelections = CYCLE_ORDER.filter((selection) => {
    const option = notification.options.find((item) => item.id === selection)
    return Boolean(option?.available)
  })

  const currentIndex = availableSelections.indexOf(notification.selection)
  const nextSelection =
    availableSelections.length === 0
      ? null
      : currentIndex >= 0
        ? availableSelections[(currentIndex + 1) % availableSelections.length] ?? null
        : availableSelections[0] ?? null

  const canCycle = nextSelection !== null && availableSelections.length > 1
  const isPending = pendingSelection !== null

  return (
    <div className="notification-inline-controls">
      <button
        type="button"
        className={`button-secondary notification-chip${notification.restart_required ? " is-attention" : ""}`}
        disabled={!canCycle || isPending}
        onClick={() => {
          if (nextSelection) {
            onSave(nextSelection, notification.can_apply_now)
          }
        }}
        title={`${shortLabel(notification.selection, notification.selected_label)} · ${notification.apply_label}`}
        aria-label={`${shortLabel(notification.selection, notification.selected_label)}. 누르면 다음 채널로 전환합니다.`}
      >
        {isPending ? (
          <span className="notification-chip-status">{pendingApplyNow ? "..." : "..."}</span>
        ) : (
          <>
            <NotificationMark selection={notification.selection} selectedLabel={notification.selected_label} />
            <span className="notification-chip-sr">{shortLabel(notification.selection, notification.selected_label)}</span>
          </>
        )}
      </button>
    </div>
  )
}
