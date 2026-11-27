import type { RuntimeState } from "../../types"

export function PanelHero({ runtimeState }: { runtimeState: RuntimeState }) {
  return <header className="panel-header"><a className="panel-brand" href="#home">transcript sys</a><div className="panel-runtime"><span className={`runtime-dot is-${runtimeState.status}`} aria-hidden="true" /><span>{runtimeState.label}</span></div></header>
}
