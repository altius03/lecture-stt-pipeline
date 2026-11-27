import { useEffect, useMemo, useState, type RefObject } from "react"

import type { PanelLogState } from "../../hooks/usePanelLogs"
import { parseLectureFileInfo, parsePanelLogText, type ParsedLogEntry } from "../../lib/logParser"
import type { PanelJob } from "../../types"

interface ActivityPanelProps {
  jobs: PanelJob[]
  selectedJobId?: number | null
  logs: PanelLogState
  error: string | null
  logRef: RefObject<HTMLPreElement>
  autoFollow: boolean
  hasUnread: boolean
  onLogScroll: () => void
  onToggleAutoFollow: (nextValue: boolean) => void
  onJumpToLatest: () => void
}

type LevelFilter = "all" | "INFO" | "WARNING" | "ERROR"
type StageFilter = "all" | "audio" | "transcript" | "correction" | "summary" | "system"

function entryStage(entry: ParsedLogEntry): StageFilter {
  const message = `${entry.message} ${entry.summary}`.toLocaleLowerCase()
  if (/오디오|audio|stabili|moved to stable/.test(message)) return "audio"
  if (/전사|transcri|quality/.test(message)) return "transcript"
  if (/요약|summary|markdown/.test(message)) return "summary"
  if (/교정|correct|postprocess|후처리/.test(message)) return "correction"
  return "system"
}

const STAGE_LABEL: Record<StageFilter, string> = {
  all: "전체 단계", audio: "오디오", transcript: "전사", correction: "교정", summary: "요약", system: "시스템",
}

export function ActivityPanel({ jobs, selectedJobId, logs, error, logRef, autoFollow, hasUnread, onLogScroll, onToggleAutoFollow, onJumpToLatest }: ActivityPanelProps) {
  const [query, setQuery] = useState("")
  const [jobFilter, setJobFilter] = useState<string>(selectedJobId ? String(selectedJobId) : "all")
  const [levelFilter, setLevelFilter] = useState<LevelFilter>("all")
  const [stageFilter, setStageFilter] = useState<StageFilter>("all")
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null)
  const [showRaw, setShowRaw] = useState(false)
  useEffect(() => { setJobFilter(selectedJobId ? String(selectedJobId) : "all") }, [selectedJobId])

  const parsed = useMemo(() => parsePanelLogText(logs.text, jobs), [logs.text, jobs])
  const selectedStem = useMemo(() => {
    const selectedJob = jobs.find((job) => String(job.id) === jobFilter)
    if (!selectedJob) return null
    const stem = parseLectureFileInfo(selectedJob.file_name)?.normalizedStem
    if (!stem || jobs.some((job) => job.id !== selectedJob.id && parseLectureFileInfo(job.file_name)?.normalizedStem === stem)) {
      return null
    }
    return stem
  }, [jobs, jobFilter])
  const visible = useMemo(() => parsed.filter((entry) => {
    if (jobFilter !== "all") {
      if (entry.jobId !== null && entry.jobId !== jobFilter) return false
      if (entry.jobId === null && (!selectedStem || entry.fileInfo?.normalizedStem !== selectedStem)) return false
    }
    if (levelFilter !== "all" && entry.level !== levelFilter) return false
    if (stageFilter !== "all" && entryStage(entry) !== stageFilter) return false
    return !query || `${entry.raw} ${entry.summary} ${entry.detail ?? ""}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())
  }).reverse().slice(0, 200), [parsed, jobFilter, selectedStem, levelFilter, stageFilter, query])
  const selected = selectedIndex !== null ? visible[selectedIndex] ?? null : null

  return <section className="log-explorer" aria-label="작업 로그 탐색"><div className="data-surface log-context"><div><strong>{jobFilter === "all" ? "전체 작업" : jobs.find((job) => String(job.id) === jobFilter)?.file_name ?? `작업 #${jobFilter}`}</strong><p>{visible.length}개 로그 표시 · 최신 기록부터</p></div><span className="live-state"><span className="runtime-dot is-running" aria-hidden="true" />실시간</span></div><div className="data-surface log-toolbar"><label className="search-field"><span className="sr-only">로그 메시지 검색</span><input type="search" placeholder="로그 메시지 검색" value={query} onChange={(event) => setQuery(event.target.value)} /></label><label><span className="sr-only">작업 필터</span><select value={jobFilter} onChange={(event) => setJobFilter(event.target.value)}><option value="all">전체 작업</option>{jobs.map((job) => <option key={job.id} value={job.id}>#{job.id} {job.file_name}</option>)}</select></label><label><span className="sr-only">단계 필터</span><select value={stageFilter} onChange={(event) => setStageFilter(event.target.value as StageFilter)}>{(Object.keys(STAGE_LABEL) as StageFilter[]).map((stage) => <option key={stage} value={stage}>{STAGE_LABEL[stage]}</option>)}</select></label><label><span className="sr-only">수준 필터</span><select value={levelFilter} onChange={(event) => setLevelFilter(event.target.value as LevelFilter)}><option value="all">전체 수준</option><option value="INFO">INFO</option><option value="WARNING">WARN</option><option value="ERROR">ERROR</option></select></label><button className="button-secondary" type="button" onClick={() => { setQuery(""); setJobFilter(selectedJobId ? String(selectedJobId) : "all"); setLevelFilter("all"); setStageFilter("all"); setSelectedIndex(null) }}>초기화</button></div>{error ? <p role="alert" className="form-error">{error}</p> : null}<div className="data-surface log-table-scroll"><table className="log-table"><thead><tr><th scope="col">시각</th><th scope="col">수준</th><th scope="col">단계</th><th scope="col">메시지</th></tr></thead><tbody>{visible.length === 0 ? <tr><td colSpan={4} className="empty-table">일치하는 로그가 없습니다.</td></tr> : visible.map((entry, index) => <tr key={`${entry.raw}-${index}`} className={selectedIndex === index ? "is-selected" : ""}><td><button type="button" className="log-row-button" onClick={() => setSelectedIndex(index)}>{entry.timeLabel ?? "—"}</button></td><td><span className={`log-level is-${entry.level.toLowerCase()}`}><i aria-hidden="true" />{entry.level === "WARNING" ? "WARN" : entry.level}</span></td><td>{STAGE_LABEL[entryStage(entry)]}</td><td><button type="button" className="log-row-button" onClick={() => setSelectedIndex(index)}>{entry.summary}</button></td></tr>)}</tbody></table>{selected ? <div className="log-detail"><strong>선택한 로그 상세</strong><dl><dt>시각</dt><dd>{selected.timestamp ?? "—"}</dd><dt>작업</dt><dd>{selected.jobId ? `#${selected.jobId}` : "시스템"}</dd><dt>수준</dt><dd>{selected.level}</dd></dl><p>{selected.detail ?? selected.message}</p></div> : null}</div><details className="raw-log-details" open={showRaw} onToggle={(event) => setShowRaw(event.currentTarget.open)}><summary>원본 로그</summary><div className="raw-log-controls"><label><input type="checkbox" checked={autoFollow} onChange={(event) => onToggleAutoFollow(event.target.checked)} />자동 따라가기</label>{hasUnread ? <button type="button" className="button-secondary" onClick={onJumpToLatest}>최신으로 이동</button> : null}{logs.droppedLineCount > 0 ? <span>오래된 로그 {logs.droppedLineCount}줄 숨김</span> : null}</div><pre ref={logRef} onScroll={onLogScroll}>{logs.text || "로그가 아직 없습니다."}</pre></details></section>
}
