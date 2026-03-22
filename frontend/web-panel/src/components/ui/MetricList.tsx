import type { ReactNode } from "react"

interface MetricListItem {
  label: string
  value: ReactNode
}

interface MetricListProps {
  items: MetricListItem[]
}

export function MetricList({ items }: MetricListProps) {
  return (
    <ul className="metric-list">
      {items.map((item) => (
        <li key={item.label}>
          <span>{item.label}</span>
          <strong>{item.value}</strong>
        </li>
      ))}
    </ul>
  )
}
