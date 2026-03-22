import { EmptyState } from "../ui/EmptyState"
import { SectionCard } from "../ui/SectionCard"
import type { ProcessingJob } from "../../types"

interface ProcessingCardProps {
  jobs: ProcessingJob[]
}

export function ProcessingCard({ jobs }: ProcessingCardProps) {
  return (
    <SectionCard label="Processing" title="현재 처리 작업">
      {jobs.length === 0 ? (
        <EmptyState message="현재 처리중인 작업이 없습니다." />
      ) : (
        <ul className="processing-list">
          {jobs.map((job) => (
            <li key={job.id}>
              <div className="processing-row">
                <strong>#{job.id} {job.file_name}</strong>
                <span className="processing-percent">{job.progress_pct}%</span>
              </div>
              <span>{job.step} · {job.eta_label}</span>
              <div className="progress-track" aria-hidden="true">
                <span className="progress-indicator" style={{ width: `${Math.max(0, Math.min(100, job.progress_pct))}%` }} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </SectionCard>
  )
}
