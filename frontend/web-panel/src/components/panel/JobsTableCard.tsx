import { SectionCard } from "../ui/SectionCard"
import type { PanelJob } from "../../types"

interface JobsTableCardProps {
  jobs: PanelJob[]
}

export function JobsTableCard({ jobs }: JobsTableCardProps) {
  function statusClassName(status: string): string {
    if (status === "DONE") {
      return "status-pill status-positive"
    }
    if (status === "ERROR") {
      return "status-pill status-negative"
    }
    if (status === "PROCESSING") {
      return "status-pill status-active"
    }
    return "status-pill status-neutral"
  }

  return (
    <SectionCard label="Jobs" title="최근 작업">
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>상태</th>
              <th>파일</th>
              <th>업데이트</th>
              <th>진행률</th>
              <th>단계</th>
              <th>오류</th>
            </tr>
          </thead>
          <tbody>
            {jobs.length === 0 ? (
              <tr>
                <td colSpan={7}>작업 없음</td>
              </tr>
            ) : (
              jobs.map((job) => (
                <tr key={job.id}>
                  <td className="mono-cell">{job.id}</td>
                  <td><span className={statusClassName(job.status)}>{job.status}</span></td>
                  <td className="file-cell">{job.file_name}</td>
                  <td className="muted-cell">{job.updated_at}</td>
                  <td className="mono-cell">{job.progress_pct}%</td>
                  <td>{job.step}</td>
                  <td className="error-cell">{job.error_message || "-"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </SectionCard>
  )
}
