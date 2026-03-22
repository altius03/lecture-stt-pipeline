interface EmptyStateProps {
  message: string
  className?: string
}

export function EmptyState({ message, className }: EmptyStateProps) {
  return <p className={className ? `empty-copy ${className}` : "empty-copy"}>{message}</p>
}
