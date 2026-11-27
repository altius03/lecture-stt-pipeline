export type DashboardScreen = "home" | "processing" | "library" | "review" | "timetable" | "settings" | "job"

export interface DashboardScreenItem {
  id: DashboardScreen
  label: string
  description: string
  badge?: string | null
}

const ICONS: Partial<Record<DashboardScreen, string>> = {
  home: "/icons/nav-home.svg", review: "/icons/nav-review.svg", settings: "/icons/nav-settings.svg",
  processing: "/icons/transcript.svg", library: "/icons/audio.svg", timetable: "/icons/summary.svg",
}

export function DashboardScreenNav({ currentScreen, items, runtimeLabel, runtimeStatus }: { currentScreen: DashboardScreen; items: DashboardScreenItem[]; runtimeLabel?: string; runtimeStatus?: string }) {
  const mainItems = items.filter((item) => ["home", "review", "processing", "settings"].includes(item.id))
  const extraItems = items.filter((item) => ["library", "timetable"].includes(item.id))
  return <nav className="recording-index-nav" aria-label="운영 화면"><a href="#home" className="sidebar-brand">transcript sys</a><p className="sidebar-label">WORKSPACE</p><ul className="recording-index-list">{mainItems.map((item) => <li key={item.id}><a href={`#${item.id}`} aria-current={item.id === currentScreen || (currentScreen === "job" && item.id === "home") ? "page" : undefined} className={item.id === currentScreen || (currentScreen === "job" && item.id === "home") ? "recording-index-link is-current" : "recording-index-link"}><img src={ICONS[item.id]} alt="" /><span>{item.label}</span>{item.badge ? <small>{item.badge}</small> : null}</a></li>)}</ul><div className="sidebar-extra"><p className="sidebar-label">도구</p>{extraItems.map((item) => <a key={item.id} href={`#${item.id}`} className={item.id === currentScreen ? "recording-index-link is-current" : "recording-index-link"}><img src={ICONS[item.id]} alt="" /><span>{item.label}</span></a>)}</div><div className="sidebar-foot"><span className={`runtime-dot is-${runtimeStatus ?? "stopped"}`} aria-hidden="true" />로컬 워커 {runtimeLabel ?? "상태 확인 중"}</div></nav>
}
