interface ErrorBannerProps {
  message: string
}

export function ErrorBanner({ message }: ErrorBannerProps) {
  return (
    <div className="error-banner" role="alert">
      <strong>운영 알림</strong>
      <p>{message}</p>
    </div>
  )
}
