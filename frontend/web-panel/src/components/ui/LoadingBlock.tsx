interface LoadingBlockProps {
  eyebrow: string
  title: string
  description?: string
}

export function LoadingBlock({ eyebrow, title, description }: LoadingBlockProps) {
  return (
    <section className="hero-card state-card">
      <div className="hero-copy state-stack">
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        {description ? <p className="state-copy">{description}</p> : null}
      </div>
    </section>
  )
}
