import type { PanelAction } from "../../types"

interface ActionItem {
  action: PanelAction
  label: string
  tone?: "default" | "secondary" | "danger"
}

interface ActionBarProps {
  actions: ActionItem[]
  pendingAction: PanelAction | null
  onAction: (action: PanelAction) => void
  compact?: boolean
}

export function ActionBar({ actions, pendingAction, onAction, compact = false }: ActionBarProps) {
  const classNames = compact ? "action-row compact-row" : "action-row"
  const isBusy = pendingAction !== null

  return (
    <div className={classNames}>
      {actions.map((item) => {
        const buttonClassName =
          item.tone === "secondary"
            ? "button-secondary"
            : item.tone === "danger"
              ? "button-danger"
              : undefined
        const isPending = pendingAction === item.action
        return (
          <button
            key={item.action}
            className={buttonClassName}
            type="button"
            disabled={isBusy}
            onClick={() => {
              onAction(item.action)
            }}
          >
            {isPending ? `${item.label}...` : item.label}
          </button>
        )
      })}
    </div>
  )
}
