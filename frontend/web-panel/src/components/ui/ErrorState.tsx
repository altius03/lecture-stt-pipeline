interface ErrorStateProps {
  eyebrow: string
  title: string
  message: string
  retryLabel?: string
  onRetry?: () => void
}

export function ErrorState({ eyebrow, title, message, retryLabel = "다시 시도", onRetry }: ErrorStateProps) {
  return (
    <section className="hero-card state-card">
      <div className="hero-copy state-stack">
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p className="state-copy">{message}</p>
        {onRetry ? (
          <div className="action-row">
            <button type="button" onClick={onRetry}>{retryLabel}</button>
          </div>
        ) : null}
      </div>
    </section>
  )
}
