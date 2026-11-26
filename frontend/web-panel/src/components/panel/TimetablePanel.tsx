import { useEffect, useState } from "react"

import { SectionCard } from "../ui/SectionCard"
import { useTimetableReview } from "../../hooks/useTimetableReview"

function weekdayLabel(value: string | null): string {
  switch (value) {
    case "mon":
      return "월"
    case "tue":
      return "화"
    case "wed":
      return "수"
    case "thu":
      return "목"
    case "fri":
      return "금"
    case "sat":
      return "토"
    case "sun":
      return "일"
    default:
      return "-"
  }
}

function proposalStatusLabel(value: string): string {
  switch (value) {
    case "suggested":
      return "검토 대기"
    case "confirmed":
      return "확정"
    case "rejected":
      return "보류"
    default:
      return value
  }
}

function proposalStatusClassName(value: string): string {
  switch (value) {
    case "confirmed":
      return "status-pill status-positive"
    case "suggested":
      return "status-pill status-active"
    default:
      return "status-pill status-neutral"
  }
}

function proposalReasonLabel(value: string): string {
  switch (value) {
    case "unique_time_match":
      return "유일 시간 일치"
    case "recorded_at_missing":
      return "녹음 시각 없음"
    case "recorded_at_invalid":
      return "녹음 시각 오류"
    case "no_time_match":
      return "시간표 후보 없음"
    case "ambiguous_time_match":
      return "복수 후보"
    default:
      return value
  }
}

function confidenceLabel(value: number | null): string {
  if (value === null) {
    return "-"
  }
  return `${Math.round(value * 100)}%`
}

function detailLine(label: string, value: string | null) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value || "-"}</dd>
    </div>
  )
}

interface TimetablePanelProps {
  initialSelectedProposalId?: number | null
}

export function TimetablePanel({
  initialSelectedProposalId = null,
}: TimetablePanelProps = {}) {
  const {
    entries,
    entriesLoading,
    entriesError,
    proposals,
    proposalsLoading,
    proposalsError,
    selectedProposalId,
    setSelectedProposalId,
    proposalDetail,
    proposalDetailLoading,
    proposalDetailError,
    confirmationPlan,
    actionError,
    actionNotice,
    rejectProposal,
    loadConfirmationPlan,
    applyConfirmationPlan,
    rejectPending,
    confirmationPlanPending,
    confirmationApplyPending,
  } = useTimetableReview({
    initialSelectedProposalId,
  })
  const [rejectConfirmOpen, setRejectConfirmOpen] = useState(false)

  const semesters = Array.from(
    new Set(entries?.entries.map((entry) => entry.semester) ?? []),
  )
  const activeSemester =
    entries?.semester ?? (semesters.length === 1 ? semesters[0] : null)
  const entryCount = entries?.entries.length ?? 0
  const selectedProposal = proposals?.proposals.find((proposal) => proposal.id === selectedProposalId) ?? null
  const timetableCapabilities =
    proposalDetail?.capabilities ?? proposals?.capabilities ?? entries?.capabilities
  const canReject = timetableCapabilities?.status_writes_enabled === true
  const canApplyConfirmation = timetableCapabilities?.confirmations_enabled === true
  const isConfirmCandidate =
    proposalDetail?.proposal.classification_reason === "unique_time_match"
  const hasDetail = proposalDetail !== null
  const selectedOutsideCurrentPage =
    initialSelectedProposalId !== null
    && selectedProposalId === initialSelectedProposalId
    && selectedProposalId !== null
    && proposals?.proposals.some((proposal) => proposal.id === selectedProposalId) === false
    && proposalDetail?.proposal.id === selectedProposalId
  const confirmationDisabledReason = !hasDetail
    ? "제안을 선택하면 확정 계획을 볼 수 있습니다."
    : !isConfirmCandidate
      ? "유일 시간 일치 제안만 확정할 수 있습니다."
      : null
  const confirmationPlanMatchesSelection =
    confirmationPlan?.proposal_id === selectedProposalId

  useEffect(() => {
    setRejectConfirmOpen(false)
  }, [selectedProposalId])

  return (
    <section className="storage-v2-grid" aria-label="Storage v2 timetable review">
      <SectionCard
        label="Timetable"
        title={
          activeSemester
            ? `${activeSemester} 활성 시간표`
            : "학기별 활성 시간표"
        }
        tone="muted"
      >
        {entriesLoading ? <p className="inline-muted">시간표를 불러오는 중입니다.</p> : null}
        {entriesError ? <p className="inline-error">{entriesError}</p> : null}
        {!entriesLoading && !entriesError && entries?.available === false ? (
          <p className="inline-muted">시간표 API가 비활성화되어 있습니다.</p>
        ) : null}
        {!entriesLoading && !entriesError && entries?.available && (
          <>
            <div className="storage-v2-summary">
              <span>표시 {entryCount}건</span>
              <span>전체 {entries.total}건</span>
            </div>
            <div className="table-wrap compact-table">
              <table>
                <thead>
                  <tr>
                    <th>학기</th>
                    <th>과목</th>
                    <th>요일</th>
                    <th>시간</th>
                    <th>교시</th>
                    <th>강의실</th>
                  </tr>
                </thead>
                <tbody>
                  {entries.entries.length === 0 ? (
                    <tr>
                      <td colSpan={6}>활성 시간표가 없습니다.</td>
                    </tr>
                  ) : (
                    entries.entries.map((entry) => (
                      <tr key={entry.entry_key}>
                        <td>{entry.semester}</td>
                        <td>
                          <strong>{entry.course_name}</strong>
                          {entry.course_code ? <span className="row-subcopy">{entry.course_code}</span> : null}
                        </td>
                        <td>{weekdayLabel(entry.weekday)}</td>
                        <td className="mono-cell">
                          {entry.start_time}–{entry.end_time}
                        </td>
                        <td>{entry.period_label}</td>
                        <td>{entry.classroom}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </>
        )}
      </SectionCard>

      <SectionCard label="Review Queue" title="시간표 분류 검토 큐">
        {proposalsLoading ? <p className="inline-muted">분류 제안을 불러오는 중입니다.</p> : null}
        {proposalsError ? <p className="inline-error">{proposalsError}</p> : null}
        {!proposalsLoading && !proposalsError && proposals?.available === false ? (
          <p className="inline-muted">분류 검토 API가 비활성화되어 있습니다.</p>
        ) : null}
        {!proposalsLoading && !proposalsError && proposals?.available && (
          <>
            <div className="storage-v2-summary">
              <span>검토 대기 {proposals.counts.suggested}건</span>
              <span>확정 {proposals.counts.confirmed}건</span>
              <span>보류 {proposals.counts.rejected}건</span>
            </div>
            {selectedOutsideCurrentPage ? (
              <p className="notice-copy">
                현재 선택한 제안은 첫 페이지 밖에 있어도 detail을 별도 read-only 조회로 유지합니다.
              </p>
            ) : null}
            <div className="table-wrap compact-table">
              <table>
                <thead>
                  <tr>
                    <th>제안 제목</th>
                    <th>상태</th>
                    <th>사유</th>
                    <th>신뢰도</th>
                  </tr>
                </thead>
                <tbody>
                  {proposals.proposals.length === 0 ? (
                    <tr>
                      <td colSpan={4}>검토 대기 제안이 없습니다.</td>
                    </tr>
                  ) : (
                    proposals.proposals.map((proposal) => {
                      const isSelected = proposal.id === selectedProposalId
                      return (
                        <tr
                          key={proposal.id}
                          className={
                            isSelected
                              ? "timetable-proposal-row is-selected"
                              : "timetable-proposal-row"
                          }
                        >
                          <td>
                            <button
                              type="button"
                              className="proposal-select-button"
                              aria-pressed={isSelected}
                              onClick={() => {
                                setRejectConfirmOpen(false)
                                setSelectedProposalId(proposal.id)
                              }}
                            >
                              <strong>{proposal.proposed_title}</strong>
                              <span className="row-subcopy mono-cell">{proposal.storage_key}</span>
                            </button>
                          </td>
                          <td>
                            <span className={proposalStatusClassName(proposal.status)}>
                              {proposalStatusLabel(proposal.status)}
                            </span>
                          </td>
                          <td>{proposalReasonLabel(proposal.classification_reason)}</td>
                          <td className="mono-cell">{confidenceLabel(proposal.confidence)}</td>
                        </tr>
                      )
                    })
                  )}
                </tbody>
              </table>
            </div>
            <div className="storage-v2-detail">
              <header>
                <p className="section-label">Selected Proposal</p>
                <h3>{selectedProposal?.proposed_title ?? "제안을 선택하세요."}</h3>
              </header>
              {proposalDetailLoading ? <p className="inline-muted">상세를 불러오는 중입니다.</p> : null}
              {proposalDetailError ? <p className="inline-error">{proposalDetailError}</p> : null}
              {!proposalDetailLoading && !proposalDetailError && proposalDetail ? (
                <>
                  <dl className="meta-grid storage-v2-meta-grid">
                    {detailLine("Storage Key", proposalDetail.proposal.storage_key)}
                    {detailLine("학기", proposalDetail.proposal.semester)}
                    {detailLine("과목", proposalDetail.proposal.course_name)}
                    {detailLine("요일", weekdayLabel(proposalDetail.proposal.weekday))}
                    {detailLine(
                      "시간",
                      proposalDetail.proposal.start_time && proposalDetail.proposal.end_time
                        ? `${proposalDetail.proposal.start_time}–${proposalDetail.proposal.end_time}`
                        : null,
                    )}
                    {detailLine("교시", proposalDetail.proposal.period_label)}
                    {detailLine("강의실", proposalDetail.proposal.classroom)}
                    {detailLine("검토 상태", proposalDetail.proposal.review_status)}
                    {detailLine("후보 Entry Key", proposalDetail.candidate_entry_keys.join(", ") || null)}
                  </dl>
                  <div className="classification-action-panel">
                    <div className="classification-action-copy">
                      <p className="inline-muted">
                        정본 이름과 manifest는 이 단계에서 바꾸지 않습니다. 현재 승격은 confirmation audit만 남깁니다.
                      </p>
                      <p className="inline-muted">
                        canonical_metadata_changed={String(proposalDetail.canonical_metadata_changed)}
                      </p>
                      <div className="classification-action-feedback" aria-live="polite" aria-atomic="true">
                        {actionNotice ? (
                          <p className="inline-success" role="status">
                            {actionNotice}
                          </p>
                        ) : null}
                        {actionError ? (
                          <p className="inline-error" role="alert">
                            {actionError}
                          </p>
                        ) : null}
                      </div>
                    </div>

                    <div className="classification-action-row">
                      <button
                        type="button"
                        className="button-secondary"
                        disabled={confirmationPlanPending || confirmationApplyPending || Boolean(confirmationDisabledReason)}
                        onClick={() => {
                          setRejectConfirmOpen(false)
                          void loadConfirmationPlan()
                        }}
                      >
                        {confirmationPlanPending ? "계획 확인 중…" : "확정 계획 보기"}
                      </button>
                      <button
                        type="button"
                        className="button-danger"
                        disabled={rejectPending || confirmationPlanPending || confirmationApplyPending || !canReject}
                        onClick={() => {
                          setRejectConfirmOpen((current) => !current)
                        }}
                      >
                        {rejectPending ? "보류 처리 중…" : rejectConfirmOpen ? "보류 확인 취소" : "제안 보류"}
                      </button>
                    </div>

                    {confirmationDisabledReason ? (
                      <p className="inline-muted">{confirmationDisabledReason}</p>
                    ) : null}
                    {!canReject ? (
                      <p className="inline-muted">
                        status_writes_enabled=false 이므로 보류 처리는 현재 비활성화되어 있습니다.
                      </p>
                    ) : null}

                    {rejectConfirmOpen ? (
                      <div className="classification-plan-card" role="group" aria-label="제안 보류 확인">
                        <p className="inline-muted">
                          이 제안은 rejected/dismissed로 기록됩니다. 정본 메타데이터는 변경하지 않습니다.
                        </p>
                        <div className="classification-action-row">
                          <button
                            type="button"
                            className="button-danger"
                            disabled={rejectPending}
                            onClick={() => {
                              void rejectProposal().then((completed) => {
                                if (completed) {
                                  setRejectConfirmOpen(false)
                                }
                              })
                            }}
                          >
                            {rejectPending ? "보류 처리 중…" : "보류 처리 실행"}
                          </button>
                          <button
                            type="button"
                            className="button-secondary"
                            disabled={rejectPending}
                            onClick={() => {
                              setRejectConfirmOpen(false)
                            }}
                          >
                            취소
                          </button>
                        </div>
                      </div>
                    ) : null}

                    {confirmationPlan && confirmationPlanMatchesSelection ? (
                      <div className="classification-plan-card" role="group" aria-label="확정 계획">
                        <header>
                          <p className="section-label">Confirmation Plan</p>
                          <h4>{confirmationPlan.proposed_title}</h4>
                        </header>
                        <dl className="meta-grid storage-v2-meta-grid">
                          {detailLine("학기", confirmationPlan.semester)}
                          {detailLine("과목", confirmationPlan.course_name)}
                          {detailLine("일자", confirmationPlan.session_date)}
                          {detailLine("교시", confirmationPlan.period_label)}
                          {detailLine("검토 상태", confirmationPlan.review_status)}
                          {detailLine("Expected Count", String(confirmationPlan.expected_count))}
                          {detailLine("Mode", confirmationPlan.mode)}
                          {detailLine("Materialization", confirmationPlan.materialization)}
                          {detailLine(
                            "Canonical Metadata Changed",
                            String(confirmationPlan.canonical_metadata_changed),
                          )}
                        </dl>
                        <div className="classification-digest-block">
                          <span className="row-subcopy">Plan Digest</span>
                          <code className="mono-cell" title={confirmationPlan.plan_sha256}>
                            {confirmationPlan.plan_sha256}
                          </code>
                        </div>
                        {!canApplyConfirmation ? (
                          <p className="inline-muted">
                            confirmations_enabled=false 이므로 계획 확인까지만 가능합니다.
                          </p>
                        ) : null}
                        <div className="classification-action-row">
                          <button
                            type="button"
                            disabled={!canApplyConfirmation || confirmationApplyPending || rejectPending}
                            onClick={() => {
                              setRejectConfirmOpen(false)
                              void applyConfirmationPlan()
                            }}
                          >
                            {confirmationApplyPending ? "확정 적용 중…" : "이 계획으로 확정"}
                          </button>
                        </div>
                      </div>
                    ) : null}
                  </div>
                </>
              ) : null}
            </div>
          </>
        )}
      </SectionCard>
    </section>
  )
}
