import { useEffect, useState } from "react"

import { useTitleSuggestionReview } from "../../hooks/useTitleSuggestionReview"
import type {
  TitleSuggestionContextType,
  TitleSuggestionReason,
  TitleSuggestionStatus,
} from "../../types"
import { SectionCard } from "../ui/SectionCard"

interface TitleSuggestionPanelProps {
  initialSelectedProposalId?: number | null
}

const NARROW_TITLE_LAYOUT_QUERY = "(max-width: 900px)"

interface TitleSuggestionUnavailableCopy {
  queue: string
  workbench: string
}

function titleSuggestionUnavailableCopy(
  disabledReason: string | undefined,
): TitleSuggestionUnavailableCopy {
  switch (disabledReason) {
    case "title_review_disabled":
      return {
        queue:
          "제목 제안 검토 API가 비활성화되어 있습니다. 상세 및 쓰기 요청은 보내지 않습니다.",
        workbench:
          "제목 제안 API가 비활성화되어 상세 workbench를 열지 않았습니다.",
      }
    case "storage_v2_db_unavailable":
      return {
        queue:
          "Storage v2 DB를 사용할 수 없어 제목 제안 목록을 열지 못했습니다. 상세 및 쓰기 요청은 보내지 않습니다.",
        workbench:
          "Storage v2 DB를 사용할 수 없어 제목 제안 상세 workbench를 열지 않았습니다.",
      }
    default:
      return {
        queue:
          "제목 제안 검토 정보를 사용할 수 없습니다. 상세 및 쓰기 요청은 보내지 않습니다.",
        workbench:
          "제목 제안 상세 workbench를 열 수 없습니다.",
      }
  }
}

function readNarrowTitleLayout(): boolean {
  return (
    typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia(NARROW_TITLE_LAYOUT_QUERY).matches
  )
}

function useNarrowTitleLayout(): boolean {
  const [isNarrow, setIsNarrow] = useState(readNarrowTitleLayout)

  useEffect(() => {
    if (
      typeof window === "undefined"
      || typeof window.matchMedia !== "function"
    ) {
      return
    }
    const mediaQuery = window.matchMedia(NARROW_TITLE_LAYOUT_QUERY)
    const handleChange = (event: MediaQueryListEvent) => {
      setIsNarrow(event.matches)
    }
    setIsNarrow(mediaQuery.matches)
    mediaQuery.addEventListener("change", handleChange)
    return () => {
      mediaQuery.removeEventListener("change", handleChange)
    }
  }, [])

  return isNarrow
}

function suggestionReasonLabel(reason: TitleSuggestionReason): string {
  switch (reason) {
    case "schedule_content_match":
      return "시간표·전사 내용 일치"
    case "content_topic":
      return "전사 내용 주제"
    default:
      return reason
  }
}

function suggestionStatusLabel(status: TitleSuggestionStatus): string {
  switch (status) {
    case "suggested":
      return "검토 대기"
    case "confirmed":
      return "검토 확정"
    case "rejected":
      return "보류"
    default:
      return status
  }
}

function contextTypeLabel(contextType: TitleSuggestionContextType | null): string {
  switch (contextType) {
    case "general":
      return "일반"
    case "class_session":
      return "수업"
    case "daily_note":
      return "일상 기록"
    case "meeting":
      return "회의·대화"
    case "memo":
      return "개인 메모"
    case null:
    default:
      return "미지정"
  }
}

function confidenceLabel(confidence: number): string {
  return `${Math.round(confidence * 100)}%`
}

function formatTimestamp(value: string | null): string {
  if (value === null) {
    return "—"
  }
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) {
    return value
  }
  return new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed)
}

function detailLine(label: string, value: string | null) {
  return (
    <div>
      <dt>{label}</dt>
      <dd title={value ?? undefined}>{value ?? "—"}</dd>
    </div>
  )
}

export function TitleSuggestionPanel({
  initialSelectedProposalId = null,
}: TitleSuggestionPanelProps = {}) {
  const {
    proposals,
    proposalsLoading,
    proposalsError,
    selectedProposalId,
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
  } = useTitleSuggestionReview({
    initialSelectedProposalId,
  })
  const [rejectConfirmOpen, setRejectConfirmOpen] = useState(false)
  const isNarrowLayout = useNarrowTitleLayout()

  const selectedProposal =
    proposals?.proposals.find(
      (proposal) => proposal.id === selectedProposalId,
    ) ?? null
  const selectedOutsideCurrentPage =
    initialSelectedProposalId !== null
    && selectedProposalId === initialSelectedProposalId
    && selectedProposalId !== null
    && proposals?.proposals.some(
      (proposal) => proposal.id === selectedProposalId,
    ) === false
    && proposalDetail?.proposal.id === selectedProposalId
  const capabilities =
    proposalDetail?.capabilities ?? proposals?.capabilities
  const canReject =
    capabilities?.status_writes_enabled === true
    && proposalDetail?.proposal.status === "suggested"
  const canApplyConfirmation =
    capabilities?.confirmations_enabled === true
    && proposalDetail?.proposal.status === "suggested"
  const confirmationPlanMatchesSelection =
    confirmationPlan?.proposal_id === selectedProposalId
  const confirmationDisabledReason =
    proposalDetail === null
      ? "새 목록 응답이 준비된 뒤 제목 제안 상세를 확인할 수 있습니다."
      : proposalDetail.proposal.status !== "suggested"
        ? "검토 대기 상태의 제목 제안만 확정 계획을 만들 수 있습니다."
        : null
  const unavailableCopy =
    proposals?.available === false
      ? titleSuggestionUnavailableCopy(proposals.disabled_reason)
      : null

  useEffect(() => {
    setRejectConfirmOpen(false)
  }, [selectedProposalId])

  const queueCard = (
    <SectionCard
      key="title-suggestion-queue"
      label="Title Signal"
      title="내용 기반 제목 제안"
      tone="muted"
    >
        {proposalsLoading ? (
          <p className="inline-muted">제목 제안 원장을 새 응답으로 확인 중입니다.</p>
        ) : null}
        {proposalsError ? (
          <p className="inline-error" role="alert">{proposalsError}</p>
        ) : null}
        {!proposalsLoading && !proposalsError && unavailableCopy ? (
          <p className="inline-muted">
            {unavailableCopy.queue}
          </p>
        ) : null}
        {!proposalsLoading && !proposalsError && proposals?.available ? (
          <>
            <div className="storage-v2-summary">
              <span>검토 대기 {proposals.counts.suggested}건</span>
              <span>검토 확정 {proposals.counts.confirmed}건</span>
              <span>보류 {proposals.counts.rejected}건</span>
            </div>
            {selectedOutsideCurrentPage ? (
              <p className="notice-copy">
                deep-link 제안은 현재 8건 목록 밖에 있어도 metadata-only detail로 유지합니다.
              </p>
            ) : null}
            {proposals.proposals.length === 0 ? (
              <p className="inline-muted">검토 대기 제목 제안이 없습니다.</p>
            ) : (
              <div
                className="recording-index-list title-suggestion-queue-list"
                role="list"
                aria-label="제목 제안 목록"
              >
                {proposals.proposals.map((proposal) => {
                  const isSelected = proposal.id === selectedProposalId
                  return (
                    <article
                      key={proposal.id}
                      className="recording-index-item"
                      role="listitem"
                    >
                      <a
                        href={`#review/title/${proposal.id}`}
                        className={
                          isSelected
                            ? "recording-index-link title-suggestion-link is-current"
                            : "recording-index-link title-suggestion-link"
                        }
                        aria-current={isSelected ? "page" : undefined}
                      >
                        <span className="recording-index-id unified-review-source-pill is-title">
                          title
                        </span>
                        <span className="recording-index-copy">
                          <strong className="title-suggestion-title">
                            {proposal.proposed_title}
                          </strong>
                          <span>
                            {suggestionReasonLabel(proposal.suggestion_reason)}
                            {" · "}
                            {contextTypeLabel(proposal.context_type)}
                          </span>
                          <span className="unified-review-meta-line">
                            {proposal.storage_key}
                            {" · "}
                            transcript r{proposal.transcript_revision}
                          </span>
                        </span>
                        <span className="title-suggestion-confidence">
                          {confidenceLabel(proposal.confidence)}
                        </span>
                      </a>
                    </article>
                  )
                })}
              </div>
            )}
          </>
        ) : null}
    </SectionCard>
  )

  const workbenchCard = (
    <SectionCard
      key="title-suggestion-workbench"
      label="Review Workbench"
      title={
        proposalDetail?.proposal.proposed_title
        ?? selectedProposal?.proposed_title
        ?? "제목 제안 상세"
      }
      className="title-suggestion-workbench-card"
    >
        {proposalDetailLoading ? (
          <p className="inline-muted">선택한 제목 제안 metadata를 확인 중입니다.</p>
        ) : null}
        {proposalDetailError ? (
          <p className="inline-error" role="alert">{proposalDetailError}</p>
        ) : null}
        {!proposalDetailLoading && !proposalDetailError && proposalDetail ? (
          <>
            <header className="title-suggestion-detail-header">
              <span className="status-pill status-neutral">
                {suggestionStatusLabel(proposalDetail.proposal.status)}
              </span>
              <h3>{proposalDetail.proposal.proposed_title}</h3>
              <p className="inline-muted">
                제안 확인 상태만 기록합니다. current title, storage_key, manifest는 변경하지 않습니다.
              </p>
            </header>

            <dl className="meta-grid storage-v2-meta-grid">
              {detailLine("Storage Key", proposalDetail.proposal.storage_key)}
              {detailLine(
                "제안 근거",
                suggestionReasonLabel(proposalDetail.proposal.suggestion_reason),
              )}
              {detailLine(
                "맥락",
                contextTypeLabel(proposalDetail.proposal.context_type),
              )}
              {detailLine(
                "신뢰도",
                confidenceLabel(proposalDetail.proposal.confidence),
              )}
              {detailLine(
                "Transcript Revision",
                `r${proposalDetail.proposal.transcript_revision}`,
              )}
              {detailLine(
                "Generator",
                proposalDetail.proposal.generator_version,
              )}
              {detailLine(
                "검토 상태",
                proposalDetail.proposal.review_status,
              )}
              {detailLine(
                "제안 당시 분류 상태",
                proposalDetail.proposal.classification_status,
              )}
              {detailLine(
                "생성 시각",
                formatTimestamp(proposalDetail.proposal.created_at),
              )}
              {detailLine(
                "갱신 시각",
                formatTimestamp(proposalDetail.proposal.updated_at),
              )}
              {detailLine(
                "확정 시각",
                formatTimestamp(proposalDetail.proposal.confirmed_at),
              )}
            </dl>

            {proposalDetail.linked_classification ? (
              <div
                className="classification-plan-card title-suggestion-classification-card"
                aria-label="연결된 시간표 분류 metadata"
              >
                <header>
                  <p className="section-label">Linked Classification</p>
                  <h4>{proposalDetail.linked_classification.proposed_title}</h4>
                </header>
                <dl className="meta-grid storage-v2-meta-grid">
                  {detailLine(
                    "현재 분류 상태",
                    proposalDetail.linked_classification.status,
                  )}
                  {detailLine(
                    "분류 근거",
                    proposalDetail.linked_classification.classification_reason,
                  )}
                  {detailLine(
                    "과목",
                    proposalDetail.linked_classification.course_name,
                  )}
                  {detailLine(
                    "과목 코드",
                    proposalDetail.linked_classification.course_code,
                  )}
                </dl>
              </div>
            ) : null}

            <div className="classification-action-panel">
              <div className="classification-action-copy">
                <p className="inline-muted">
                  confirmation은 audit-only입니다. 확인 후에도 사용자 정본 이름으로 승격하지 않습니다.
                </p>
                <p className="inline-muted">
                  canonical_metadata_changed={String(
                    proposalDetail.canonical_metadata_changed,
                  )}
                </p>
                <div
                  className="classification-action-feedback"
                  aria-live="polite"
                  aria-atomic="true"
                >
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
                  disabled={
                    confirmationPlanPending
                    || confirmationApplyPending
                    || rejectPending
                    || Boolean(confirmationDisabledReason)
                  }
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
                  disabled={
                    rejectPending
                    || confirmationPlanPending
                    || confirmationApplyPending
                    || !canReject
                  }
                  onClick={() => {
                    setRejectConfirmOpen((current) => !current)
                  }}
                >
                  {rejectPending
                    ? "보류 처리 중…"
                    : rejectConfirmOpen
                      ? "보류 확인 취소"
                      : "제안 보류"}
                </button>
              </div>

              {confirmationDisabledReason ? (
                <p className="inline-muted">{confirmationDisabledReason}</p>
              ) : null}
              {!canReject ? (
                <p className="inline-muted">
                  {proposalDetail.proposal.status !== "suggested"
                    ? "검토 대기 lifecycle을 벗어난 제안은 보류 처리할 수 없습니다."
                    : "status_writes_enabled=false 이므로 보류 처리는 비활성화되어 있습니다."}
                </p>
              ) : null}

              {rejectConfirmOpen ? (
                <div
                  className="classification-plan-card"
                  role="group"
                  aria-label="제목 제안 보류 확인"
                >
                  <p className="inline-muted">
                    제안은 rejected, review는 dismissed로 기록됩니다. 정본 이름은 변경하지 않습니다.
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
                <div
                  className="classification-plan-card"
                  role="group"
                  aria-label="제목 제안 확정 계획"
                >
                  <header>
                    <p className="section-label">Confirmation Plan</p>
                    <h4>{confirmationPlan.proposed_title}</h4>
                  </header>
                  <dl className="meta-grid storage-v2-meta-grid">
                    {detailLine(
                      "제안 근거",
                      suggestionReasonLabel(confirmationPlan.suggestion_reason),
                    )}
                    {detailLine(
                      "Transcript Revision",
                      `r${confirmationPlan.transcript_revision}`,
                    )}
                    {detailLine(
                      "Expected Count",
                      String(confirmationPlan.expected_count),
                    )}
                    {detailLine("Mode", confirmationPlan.mode)}
                    {detailLine(
                      "Materialization",
                      confirmationPlan.materialization,
                    )}
                    {detailLine(
                      "Canonical Metadata Changed",
                      String(confirmationPlan.canonical_metadata_changed),
                    )}
                  </dl>
                  <div className="classification-digest-block">
                    <span className="row-subcopy">Plan Digest</span>
                    <code
                      className="mono-cell"
                      title={confirmationPlan.plan_sha256}
                    >
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
                      disabled={
                        !canApplyConfirmation
                        || confirmationApplyPending
                        || rejectPending
                      }
                      onClick={() => {
                        setRejectConfirmOpen(false)
                        void applyConfirmationPlan()
                      }}
                    >
                      {confirmationApplyPending
                        ? "검토 확정 중…"
                        : "검토 확정 — 정본 이름 미변경"}
                    </button>
                  </div>
                </div>
              ) : null}
            </div>
          </>
        ) : null}
        {!proposalDetailLoading && !proposalDetailError && !proposalDetail ? (
          <p className="inline-muted">
            {unavailableCopy
              ? unavailableCopy.workbench
              : selectedProposalId === null
                ? "검토할 제목 제안을 선택하세요."
                : "새 목록 응답을 확인한 뒤 선택한 제안의 상세 metadata를 엽니다."}
          </p>
        ) : null}
    </SectionCard>
  )

  return (
    <section
      className="storage-v2-grid title-suggestion-layout"
      aria-label="Storage v2 title suggestion review"
    >
      {isNarrowLayout
        ? [workbenchCard, queueCard]
        : [queueCard, workbenchCard]}
    </section>
  )
}
