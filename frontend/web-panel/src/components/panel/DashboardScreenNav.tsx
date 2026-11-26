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
    <nav
      className="recording-index-nav"
      aria-label="운영 화면"
      aria-labelledby="dashboard-command-rail-title"
      aria-describedby="dashboard-command-rail-description"
    >
      <div className="recording-index-header">
        <p className="recording-index-kicker">Command Rail</p>
        <div className="recording-index-heading">
          <strong id="dashboard-command-rail-title">운영 워크벤치</strong>
          <span id="dashboard-command-rail-description">전체 런타임과 검토 큐를 기준으로 필요한 화면만 바로 엽니다.</span>
        </div>
      </div>
      <ul className="recording-index-list">
        {items.map((item) => {
          const isCurrent = item.id === currentScreen
          return (
            <li key={item.id} className="recording-index-item">
              <a
                href={item.id === "home" ? "#" : `#${item.id}`}
                className={isCurrent ? "recording-index-link is-current" : "recording-index-link"}
                aria-current={isCurrent ? "page" : undefined}
                aria-label={
                  item.badge
                    ? `${item.label}. ${item.description}. 대기 항목 ${item.badge}건.`
                    : `${item.label}. ${item.description}.`
                }
              >
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
