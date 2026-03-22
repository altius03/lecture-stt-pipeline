import type { ReactNode } from "react"

interface SectionCardProps {
  label: string
  title: string
  tone?: "default" | "muted"
  className?: string
  children: ReactNode
}

export function SectionCard({ label, title, tone = "default", className, children }: SectionCardProps) {
  const classNames = ["panel-card"]
  if (tone === "muted") {
    classNames.push("muted-card")
  }
  if (className) {
    classNames.push(className)
  }

  return (
    <article className={classNames.join(" ")}>
      <header className="panel-card-header">
        <div>
          <p className="section-label">{label}</p>
          <h2>{title}</h2>
        </div>
      </header>
      <div className="panel-card-body">{children}</div>
    </article>
  )
}
