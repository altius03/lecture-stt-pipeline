import type { ReactNode } from "react"

interface DefinitionGridItem {
  label: string
  value: ReactNode
}

interface DefinitionGridProps {
  items: DefinitionGridItem[]
}

export function DefinitionGrid({ items }: DefinitionGridProps) {
  return (
    <dl className="meta-grid">
      {items.map((item) => (
        <div key={item.label}>
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  )
}
