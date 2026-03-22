import { ActionBar } from "../ui/ActionBar"
import { DefinitionGrid } from "../ui/DefinitionGrid"
import { SectionCard } from "../ui/SectionCard"
import type { PanelAction, PanelSummary, RuntimeState } from "../../types"

interface RuntimeCardProps {
  runtimeState: RuntimeState
  summary: PanelSummary
  pauseAction: "pause" | "resume"
  pauseLabel: string
  pendingAction: PanelAction | null
  onAction: (action: PanelAction) => void
}

export function RuntimeCard({
  runtimeState,
  summary,
  pauseAction,
  pauseLabel,
  pendingAction,
  onAction,
}: RuntimeCardProps) {
  return (
    <SectionCard label="Runtime" title={runtimeState.description}>
      <DefinitionGrid
        items={[
          { label: "실행 원천", value: runtimeState.source },
          { label: "업데이트", value: summary.updated_at },
          { label: "현재 단계", value: summary.progress_label },
          { label: "ETA", value: summary.eta_label },
        ]}
      />
      <p className="notice-copy">{summary.notice}</p>
      <ActionBar
        actions={[
          { action: "start", label: "시작", tone: "secondary" },
          { action: pauseAction, label: pauseLabel, tone: "secondary" },
          { action: "stop", label: "중지", tone: "danger" },
          { action: "refresh", label: "동기화", tone: "secondary" },
        ]}
        pendingAction={pendingAction}
        onAction={onAction}
      />
    </SectionCard>
  )
}
