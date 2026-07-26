export type DashboardScreen = "home" | "processing" | "library" | "review" | "timetable" | "settings"

export interface DashboardScreenItem {
  id: DashboardScreen
  label: string
  description: string
  badge?: string | null
}

interface DashboardScreenNavProps {
  currentScreen: DashboardScreen
  items: DashboardScreenItem[]
}

export function DashboardScreenNav({ currentScreen, items }: DashboardScreenNavProps) {
  return (
    <nav className="recording-index-nav" aria-label="운영 화면">
      <div className="recording-index-header">
        <p className="recording-index-kicker">Recording Index</p>
        <div className="recording-index-heading">
          <strong>유입부터 검토까지</strong>
          <span>오디오 처리 흐름을 기준으로 필요한 화면만 바로 엽니다.</span>
        </div>
      </div>
      <ul className="recording-index-list">
        {items.map((item, index) => {
          const isCurrent = item.id === currentScreen
          return (
            <li key={item.id} className="recording-index-item">
              <a
                href={item.id === "home" ? "#" : `#${item.id}`}
                className={isCurrent ? "recording-index-link is-current" : "recording-index-link"}
                aria-current={isCurrent ? "page" : undefined}
              >
                <span className="recording-index-id" aria-hidden="true">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <span className="recording-index-copy">
                  <strong>{item.label}</strong>
                  <span>{item.description}</span>
                </span>
                {item.badge ? <span className="recording-index-badge">{item.badge}</span> : null}
              </a>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}
