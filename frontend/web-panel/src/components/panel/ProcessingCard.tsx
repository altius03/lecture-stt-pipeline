import { EmptyState } from "../ui/EmptyState"
import { SectionCard } from "../ui/SectionCard"
import type { ProcessingJob } from "../../types"

interface ProcessingCardProps {
  jobs: ProcessingJob[]
}

const PIPELINE_STEPS = [
  { label: "감시", matches: ["파일 감지"] },
  { label: "선점", matches: ["로컬 staging", "파일 이동"] },
  { label: "전사", matches: ["전사 시작/진행", "전사 실행"] },
  { label: "후처리", matches: ["후처리"] },
  { label: "검사", matches: ["품질 검사"] },
  { label: "저장", matches: ["산출물 저장", "전사문 생성", "전체 완료"] },
]

function stepIndex(step: string): number {
  const normalizedStep = step.trim()
  if (!normalizedStep) {
    return 0
  }

  const index = PIPELINE_STEPS.findIndex((item) =>
    item.matches.some((match) => normalizedStep.includes(match)),
  )

  return index >= 0 ? index : 0
}

export function ProcessingCard({ jobs }: ProcessingCardProps) {
  const currentJob = jobs[0] ?? null
  const activeIndex = currentJob ? stepIndex(currentJob.step) : -1

  return (
    <SectionCard label="Current Run" title="현재 처리 작업" className="processing-card">
      {currentJob === null ? (
        <div className="current-job-empty">
          <EmptyState message="현재 처리중인 작업이 없습니다." />
          <p>워커가 실행 중이면 inbox 안정 파일을 기다리는 상태입니다.</p>
        </div>
      ) : (
        <div className="current-job-panel">
          <div className="current-job-summary">
            <div>
              <span className="current-job-id">작업 #{currentJob.id}</span>
              <strong>{currentJob.file_name}</strong>
              <p>{currentJob.step || "파일 감지"} · {currentJob.eta_label}</p>
            </div>
            <span className="processing-percent">{currentJob.progress_pct}%</span>
          </div>

          <div className="progress-track" aria-label={`현재 진행률 ${currentJob.progress_pct}%`}>
            <span
              className="progress-indicator"
              style={{ width: `${Math.max(0, Math.min(100, currentJob.progress_pct))}%` }}
            />
          </div>

          <ol className="pipeline-strip" aria-label="전사 처리 단계">
            {PIPELINE_STEPS.map((item, index) => {
              const stateClass =
                index < activeIndex
                  ? "is-complete"
                  : index === activeIndex
                    ? "is-active"
                    : "is-pending"
              return (
                <li key={item.label} className={stateClass}>
                  <span>{index + 1}</span>
                  <strong>{item.label}</strong>
                </li>
              )
            })}
          </ol>

          {jobs.length > 1 ? (
            <ul className="processing-queue">
              {jobs.slice(1).map((job) => (
                <li key={job.id}>
                  <span>#{job.id}</span>
                  <strong>{job.file_name}</strong>
                  <em>{job.progress_pct}%</em>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      )}
    </SectionCard>
  )
}
