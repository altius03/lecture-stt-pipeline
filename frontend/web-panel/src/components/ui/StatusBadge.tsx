import type { RuntimeStatus } from "../../types"

const STATUS_LABELS: Record<RuntimeStatus, string> = {
  running: "실행중",
  paused: "일시정지",
  stopped: "중지",
}

interface StatusBadgeProps {
  status: RuntimeStatus
  label: string
}

export function StatusBadge({ status, label }: StatusBadgeProps) {
  return (
    <div className="status-chip">
      <span className={`status-dot status-${status}`} />
      {label || STATUS_LABELS[status]}
    </div>
  )
}
