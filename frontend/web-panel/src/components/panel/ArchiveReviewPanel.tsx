import { useEffect, useMemo, useState } from "react"

import { useArchiveReview, type ArchiveReviewFilter } from "../../hooks/useArchiveReview"
import type {
  ArchiveReviewArtifactKind,
  ArchiveReviewCanonicalSuggestion,
  ArchiveReviewRevision,
  ArchiveReviewStatus,
} from "../../types"
import { SectionCard } from "../ui/SectionCard"

const FILTERS: Array<{ value: ArchiveReviewFilter; label: string }> = [
  { value: "all", label: "전체" },
  { value: "open", label: "열림" },
  { value: "triaged", label: "분류 완료" },
  { value: "resolved", label: "해결" },
  { value: "dismissed", label: "보류" },
]

function reviewStatusLabel(value: string): string {
  switch (value) {
    case "open":
      return "열림"
    case "triaged":
      return "분류 완료"
    case "resolved":
      return "해결"
    case "dismissed":
      return "보류"
    default:
      return value
  }
}

function reviewStatusClassName(value: string): string {
  switch (value) {
    case "open":
      return "status-pill status-active"
    case "triaged":
      return "status-pill status-neutral"
    case "resolved":
      return "status-pill status-positive"
    case "dismissed":
      return "status-pill status-negative"
    default:
      return "status-pill status-neutral"
  }
}

function artifactKindLabel(value: ArchiveReviewArtifactKind): string {
  switch (value) {
    case "correction_text":
      return "교정본 텍스트"
    case "correction_json":
      return "교정본 메타데이터"
    case "summary_markdown":
      return "요약본"
    default:
      return value
  }
}

function suggestionReasonLabel(value: ArchiveReviewCanonicalSuggestion["reason_code"]): string {
  switch (value) {
    case "unique_matches_ledger":
      return "ledger와 유일 일치"
    case "multiple_ledger_matches":
      return "ledger 일치 후보 복수"
    case "unique_latest_capture_current":
      return "최신 capture current 후보 1건"
    case "multiple_latest_capture_current":
      return "최신 capture current 후보 복수"
    case "no_unique_current_candidate":
      return "유일 current 후보 없음"
    default:
      return value
  }
}

function classificationLabel(value: string | null): string {
  if (!value) {
    return "-"
  }
  switch (value) {
    case "pending":
      return "대기"
    case "candidate":
      return "후보"
    case "matched":
      return "일치"
    case "ownerless":
      return "ownerless"
    case "blocked":
      return "blocked"
    default:
      return value
  }
}

function formatBytes(value: number): string {
  if (value < 1024) {
    return `${value} B`
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

function detailLine(label: string, value: string | null) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value || "-"}</dd>
    </div>
  )
}

function buildRevisionSelectionId(artifactKind: ArchiveReviewArtifactKind, revisionId: number): string {
  return `archive-revision-${artifactKind}-${revisionId}`
}

function findSuggestion(
  suggestions: ArchiveReviewCanonicalSuggestion[],
  artifactKind: ArchiveReviewArtifactKind,
): ArchiveReviewCanonicalSuggestion | null {
  return suggestions.find((suggestion) => suggestion.artifact_kind === artifactKind) ?? null
}

function availableStatusActions(
  reviewStatus: ArchiveReviewStatus,
  promotedStorageKey: string | null,
): ArchiveReviewStatus[] {
  if (promotedStorageKey) {
    return []
  }
  switch (reviewStatus) {
    case "open":
      return ["triaged", "dismissed"]
    case "triaged":
      return ["open", "dismissed"]
    case "dismissed":
      return ["open"]
    case "resolved":
      return ["triaged"]
    default:
      return []
  }
}

interface ArchiveReviewPanelProps {
  initialSelectedCaseKey?: string | null
}

export function ArchiveReviewPanel({
  initialSelectedCaseKey = null,
}: ArchiveReviewPanelProps = {}) {
  const {
    reviewCases,
    reviewCasesLoading,
    reviewCasesError,
    reviewStatusFilter,
    setReviewStatusFilter,
    offset,
    pageLimit,
    setOffset,
    selectedCaseKey,
    setSelectedCaseKey,
    caseDetail,
    caseDetailLoading,
    caseDetailError,
    selectedRevisions,
    setSelectedRevision,
    targetStorageKey,
    setTargetStorageKey,
    promotionPlan,
    promotionPlanRequest,
    actionError,
    actionNotice,
    capabilities,
    hasCompleteSelection,
    loadPromotionPlan,
    applyPromotionPlan,
    updateReviewStatus,
    statusUpdatePending,
    promotionPlanPending,
    promotionApplyPending,
  } = useArchiveReview({
    initialSelectedCaseKey,
  })
  const [dismissConfirmOpen, setDismissConfirmOpen] = useState(false)

  useEffect(() => {
    setDismissConfirmOpen(false)
  }, [selectedCaseKey])

  const groupedRevisions = useMemo(() => {
    const groups = new Map<ArchiveReviewArtifactKind, ArchiveReviewRevision[]>()
    for (const revision of caseDetail?.revisions ?? []) {
      const current = groups.get(revision.artifact_kind) ?? []
      current.push(revision)
      groups.set(revision.artifact_kind, current)
    }
    return Array.from(groups.entries())
  }, [caseDetail?.revisions])

  const currentStatusActions = caseDetail
    ? availableStatusActions(caseDetail.case.review_status, caseDetail.case.promoted_storage_key)
    : []
  const canWriteStatus = capabilities?.status_writes_enabled === true
  const canApplyPromotion = capabilities?.promotions_enabled === true
  const pageStart = reviewCases ? reviewCases.filters.offset + 1 : 0
  const pageEnd = reviewCases ? reviewCases.filters.offset + reviewCases.cases.length : 0
  const hasPreviousPage = offset > 0
  const hasNextPage = reviewCases ? offset + pageLimit < reviewCases.total : false
  const selectedOutsideCurrentPage =
    initialSelectedCaseKey !== null
    && selectedCaseKey === initialSelectedCaseKey
    && selectedCaseKey !== null
    && reviewCases?.cases.some((item) => item.case_key === selectedCaseKey) === false
    && caseDetail?.case.case_key === selectedCaseKey
  const promotionLifecycleDisabledReason =
    !caseDetail
      ? null
      : caseDetail.case.promoted_storage_key
        ? `이미 ${caseDetail.case.promoted_storage_key} recording에 정본 연결된 사례입니다.`
        : caseDetail.case.review_status === "resolved"
          ? "resolved 사례는 재검토 상태로 되돌린 뒤에만 새 승격 계획을 만들 수 있습니다."
          : caseDetail.case.review_status === "dismissed"
            ? "보류한 사례는 열림 상태로 되돌린 뒤에만 승격 계획을 만들 수 있습니다."
            : !caseDetail.latest_capture
              ? "capture가 없는 사례는 승격 계획을 만들 수 없습니다."
              : caseDetail.latest_capture.reconciliation_classification === "blocked"
                ? "최신 capture가 blocked이므로 canonical 승격 계획을 만들 수 없습니다."
                : null
  const selectionDisabledReason =
    promotionLifecycleDisabledReason
      ?? (groupedRevisions.length === 0
      ? "revision 후보가 없는 사례는 승격 계획을 만들 수 없습니다."
      : !hasCompleteSelection
        ? "artifact kind마다 revision을 하나씩 명시적으로 선택하세요."
        : !targetStorageKey.trim()
          ? "기존 recording의 storage_key를 직접 입력하세요."
          : null)
  const selectedSuggestionReasons = groupedRevisions.map(([artifactKind]) => findSuggestion(
    caseDetail?.canonical_suggestions ?? [],
    artifactKind,
  ))

  return (
    <section className="storage-v2-grid archive-review-layout" aria-label="Archive evidence review queue">
      <SectionCard label="Review Queue" title="archive evidence 검토 큐">
        <div className="archive-review-queue">
        {reviewCasesLoading ? <p className="inline-muted">archive evidence 사례를 불러오는 중입니다.</p> : null}
        {reviewCasesError ? <p className="inline-error">{reviewCasesError}</p> : null}
        {!reviewCasesLoading && !reviewCasesError && reviewCases?.available === false ? (
          <p className="inline-muted">archive evidence 검토 API가 비활성화되어 있습니다.</p>
        ) : null}
        {!reviewCasesLoading && !reviewCasesError && reviewCases?.available && (
          <>
            <div className="storage-v2-summary archive-review-counts">
              <span>열림 {reviewCases.counts.open}건</span>
              <span>분류 완료 {reviewCases.counts.triaged}건</span>
              <span>해결 {reviewCases.counts.resolved}건</span>
              <span>보류 {reviewCases.counts.dismissed}건</span>
            </div>
            {selectedOutsideCurrentPage ? (
              <p className="notice-copy">
                현재 선택한 사례는 첫 페이지 밖에 있어도 detail을 별도 read-only 조회로 유지합니다.
              </p>
            ) : null}

            <div className="classification-action-row archive-review-filter-bar" aria-label="검토 상태 필터">
              {FILTERS.map((filter) => (
                <button
                  key={filter.value}
                  type="button"
                  className={`button-secondary${reviewStatusFilter === filter.value ? " log-view-tab is-active" : ""}`}
                  aria-pressed={reviewStatusFilter === filter.value}
                  onClick={() => {
                    setDismissConfirmOpen(false)
                    setReviewStatusFilter(filter.value)
                  }}
                >
                  {filter.label}
                </button>
              ))}
            </div>

            <div className="storage-v2-summary archive-review-page-summary">
              <span>
                표시 {reviewCases.cases.length}건
              </span>
              <span>전체 {reviewCases.total}건</span>
              <span>
                범위 {reviewCases.total === 0 ? "0" : `${pageStart}–${pageEnd}`}
              </span>
            </div>

            <div className="table-wrap compact-table archive-review-case-table">
              <table>
                <thead>
                  <tr>
                    <th>사례</th>
                    <th>상태</th>
                    <th>capture</th>
                    <th>revision</th>
                    <th>최신 분류</th>
                  </tr>
                </thead>
                <tbody>
                  {reviewCases.cases.length === 0 ? (
                    <tr>
                      <td colSpan={5}>조건에 맞는 검토 사례가 없습니다.</td>
                    </tr>
                  ) : (
                    reviewCases.cases.map((item) => {
                      const isSelected = item.case_key === selectedCaseKey
                      return (
                        <tr
                          key={item.case_key}
                          className={
                            isSelected
                              ? "timetable-proposal-row archive-review-case-row is-selected"
                              : "timetable-proposal-row archive-review-case-row"
                          }
                        >
                          <td>
                            <button
                              type="button"
                              className="proposal-select-button archive-review-case-select"
                              aria-pressed={isSelected}
                              onClick={() => {
                                setDismissConfirmOpen(false)
                                setSelectedCaseKey(item.case_key)
                              }}
                            >
                              <strong>{item.logical_stem}</strong>
                              <span className="row-subcopy mono-cell">{item.case_key}</span>
                              {item.subject_abbr ? <span className="row-subcopy">{item.subject_abbr}</span> : null}
                              {item.promoted_storage_key ? (
                                <span className="row-subcopy">정본 연결: {item.promoted_storage_key}</span>
                              ) : null}
                            </button>
                          </td>
                          <td>
                            <span className={reviewStatusClassName(item.review_status)}>
                              {reviewStatusLabel(item.review_status)}
                            </span>
                          </td>
                          <td className="mono-cell">{item.capture_count}</td>
                          <td className="mono-cell">{item.revision_count}</td>
                          <td>{classificationLabel(item.latest_classification)}</td>
                        </tr>
                      )
                    })
                  )}
                </tbody>
              </table>
            </div>

            <div className="classification-action-row archive-review-pagination">
              <button
                type="button"
                className="button-secondary"
                disabled={!hasPreviousPage}
                onClick={() => {
                  setDismissConfirmOpen(false)
                  setOffset(Math.max(0, offset - pageLimit))
                }}
              >
                이전 20건
              </button>
              <button
                type="button"
                className="button-secondary"
                disabled={!hasNextPage}
                onClick={() => {
                  setDismissConfirmOpen(false)
                  setOffset(offset + pageLimit)
                }}
              >
                다음 20건
              </button>
            </div>
          </>
        )}
        </div>
      </SectionCard>

      <SectionCard label="Evidence Docket" title="revision 비교와 guarded 승격">
        <div className="storage-v2-detail archive-review-docket">
          <header>
            <p className="section-label">Selected Case</p>
            <h3>{caseDetail?.case.logical_stem ?? "사례를 선택하세요."}</h3>
          </header>
          {caseDetailLoading ? <p className="inline-muted">사례 상세를 불러오는 중입니다.</p> : null}
          {caseDetailError ? <p className="inline-error">{caseDetailError}</p> : null}
          {!caseDetailLoading && !caseDetailError && caseDetail ? (
            <>
              <dl className="meta-grid storage-v2-meta-grid">
                {detailLine("Case Key", caseDetail.case.case_key)}
                {detailLine("Legacy Delivery Key", caseDetail.case.legacy_delivery_key)}
                {detailLine("논리 stem", caseDetail.case.logical_stem)}
                {detailLine("과목 약어", caseDetail.case.subject_abbr)}
                {detailLine("검토 상태", reviewStatusLabel(caseDetail.case.review_status))}
                {detailLine("최신 capture", caseDetail.latest_capture?.capture_key ?? null)}
                {detailLine(
                  "최신 분류",
                  classificationLabel(caseDetail.latest_capture?.reconciliation_classification ?? null),
                )}
                {detailLine("정본 연결", caseDetail.case.promoted_storage_key)}
                {detailLine(
                  "capture 요약",
                  `${caseDetail.capture_count}건${caseDetail.captures_truncated ? " (상세 100건까지만 표시)" : ""}`,
                )}
                {detailLine("revision 수", String(caseDetail.revision_count))}
              </dl>

              <div className="classification-action-panel archive-review-guard">
                <div className="classification-action-copy">
                  <p className="inline-muted">
                    승격은 metadata-only canonical 선택 기록만 남깁니다. 파일 이동, recording 생성, 표시 이름 확정은 하지 않습니다.
                  </p>
                  <p className="inline-muted">
                    source path와 본문은 노출하지 않고 sha256, 크기, 관측 관계만 비교합니다.
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
                  {currentStatusActions.map((status) =>
                    status === "dismissed" ? (
                      <button
                        key={status}
                        type="button"
                        className="button-danger"
                        disabled={!canWriteStatus || statusUpdatePending || promotionPlanPending || promotionApplyPending}
                        onClick={() => {
                          setDismissConfirmOpen((current) => !current)
                        }}
                      >
                        {dismissConfirmOpen ? "보류 확인 취소" : "사례 보류"}
                      </button>
                    ) : (
                      <button
                        key={status}
                        type="button"
                        className="button-secondary"
                        disabled={!canWriteStatus || statusUpdatePending || promotionPlanPending || promotionApplyPending}
                        onClick={() => {
                          setDismissConfirmOpen(false)
                          void updateReviewStatus(status)
                        }}
                      >
                        {status === "triaged" ? "분류 완료로 이동" : status === "open" ? "열림으로 되돌리기" : "재검토로 되돌리기"}
                      </button>
                    ),
                  )}
                </div>

                {!canWriteStatus ? (
                  <p className="inline-muted">
                    status_writes_enabled=false 이므로 검토 상태 변경은 현재 비활성화되어 있습니다.
                  </p>
                ) : null}

                {dismissConfirmOpen ? (
                  <div className="classification-plan-card" role="group" aria-label="사례 보류 확인">
                    <p className="inline-muted">
                      보류는 review_status=dismissed만 기록합니다. evidence나 정본 메타데이터는 바꾸지 않습니다.
                    </p>
                    <div className="classification-action-row">
                      <button
                        type="button"
                        className="button-danger"
                        disabled={statusUpdatePending}
                        onClick={() => {
                          void updateReviewStatus("dismissed").then((completed) => {
                            if (completed) {
                              setDismissConfirmOpen(false)
                            }
                          })
                        }}
                      >
                        {statusUpdatePending ? "보류 처리 중…" : "보류 확정"}
                      </button>
                      <button
                        type="button"
                        className="button-secondary"
                        disabled={statusUpdatePending}
                        onClick={() => {
                          setDismissConfirmOpen(false)
                        }}
                      >
                        취소
                      </button>
                    </div>
                  </div>
                ) : null}

                <div className="archive-review-target-field">
                  <label htmlFor="archive-target-storage-key" className="section-label">
                    Canonical Target Storage Key
                  </label>
                  <input
                    id="archive-target-storage-key"
                    className="archive-review-target-input"
                    type="text"
                    autoComplete="off"
                    spellCheck={false}
                    placeholder="예: 2026-03-18-cs101-period1"
                    value={targetStorageKey}
                    disabled={Boolean(promotionLifecycleDisabledReason)}
                    onChange={(event) => {
                      setDismissConfirmOpen(false)
                      setTargetStorageKey(event.target.value)
                    }}
                  />
                  <span className="row-subcopy">
                    기존 active recording의 ASCII storage_key를 직접 입력합니다. 표시 이름이나 새 recording은 만들지 않습니다.
                  </span>
                </div>

                <div className="classification-action-row archive-review-promotion-actions">
                  <button
                    type="button"
                    className="button-secondary"
                    disabled={promotionPlanPending || promotionApplyPending || Boolean(selectionDisabledReason)}
                    onClick={() => {
                      setDismissConfirmOpen(false)
                      void loadPromotionPlan()
                    }}
                  >
                    {promotionPlanPending ? "계획 계산 중…" : "승격 계획 보기"}
                  </button>
                  <button
                    type="button"
                    disabled={!promotionPlan || promotionApplyPending || !canApplyPromotion}
                    onClick={() => {
                      setDismissConfirmOpen(false)
                      void applyPromotionPlan()
                    }}
                  >
                    {promotionApplyPending ? "정본 연결 적용 중…" : "이 계획으로 정본 연결"}
                  </button>
                </div>

                {selectionDisabledReason ? <p className="inline-muted">{selectionDisabledReason}</p> : null}
                {!canApplyPromotion ? (
                  <p className="inline-muted">
                    promotions_enabled=false 이므로 적용은 닫혀 있습니다. 계획 조회는 read-only로 계속 가능합니다.
                  </p>
                ) : null}

                {promotionPlan && promotionPlanRequest ? (
                  <div
                    className="classification-plan-card archive-review-plan-receipt"
                    role="group"
                    aria-label="승격 계획"
                  >
                    <header className="archive-review-plan-header">
                      <p className="section-label">Guarded Promotion</p>
                      <h4>Plan Receipt</h4>
                      <span className="status-pill status-neutral">read-only plan</span>
                    </header>
                    <dl className="meta-grid storage-v2-meta-grid">
                      {detailLine("Case Key", promotionPlan.case_key)}
                      {detailLine("Target Storage Key", promotionPlan.target_storage_key)}
                      {detailLine("Target Recording ID", String(promotionPlan.target_recording_id))}
                      {detailLine("Latest Capture", promotionPlan.latest_capture_key)}
                      {detailLine("Latest Classification", promotionPlan.latest_capture_classification)}
                      {detailLine("Review Status", reviewStatusLabel(promotionPlan.review_status))}
                      {detailLine("Expected Count", String(promotionPlan.expected_count))}
                      {detailLine("Mode", promotionPlan.mode)}
                    </dl>

                    <div className="classification-digest-block">
                      <span className="row-subcopy">Plan Digest</span>
                      <code className="mono-cell" title={promotionPlan.plan_sha256}>
                        {promotionPlan.plan_sha256}
                      </code>
                    </div>

                    <div className="table-wrap compact-table archive-review-plan-table">
                      <table>
                        <thead>
                          <tr>
                            <th>artifact</th>
                            <th>revision</th>
                            <th>sha256</th>
                            <th>크기</th>
                          </tr>
                        </thead>
                        <tbody>
                          {promotionPlan.selected_revisions.map((selection) => (
                            <tr key={`${selection.artifact_kind}-${selection.revision_id}`}>
                              <td>{artifactKindLabel(selection.artifact_kind)}</td>
                              <td className="mono-cell">{selection.revision_id}</td>
                              <td className="mono-cell" title={selection.content_sha256}>
                                {selection.content_sha256.slice(0, 16)}…
                              </td>
                              <td className="mono-cell">{formatBytes(selection.bytes)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>

                    <p className="inline-muted">
                      apply는 expected_count=1, sha256 digest, target storage_key, 선택 revision snapshot이 모두 그대로일 때만 통과합니다.
                    </p>
                    <p className="inline-muted">
                      request snapshot: {promotionPlanRequest.targetStorageKey}
                    </p>
                  </div>
                ) : null}
              </div>

              {caseDetail.confirmed_selections.length > 0 ? (
                <div
                  className="classification-plan-card archive-review-confirmed"
                  role="group"
                  aria-label="확정된 canonical 선택"
                >
                  <h4>Confirmed Canonical Selection</h4>
                  <div className="table-wrap compact-table">
                    <table>
                      <thead>
                        <tr>
                          <th>artifact</th>
                          <th>revision</th>
                          <th>sha256</th>
                          <th>confirmed_at</th>
                        </tr>
                      </thead>
                      <tbody>
                        {caseDetail.confirmed_selections.map((selection) => (
                          <tr key={`${selection.artifact_kind}-${selection.revision_id}`}>
                            <td>{artifactKindLabel(selection.artifact_kind)}</td>
                            <td className="mono-cell">{selection.revision_id}</td>
                            <td className="mono-cell" title={selection.content_sha256}>
                              {selection.content_sha256.slice(0, 16)}…
                            </td>
                            <td>{selection.confirmed_at}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              ) : null}

              {groupedRevisions.map(([artifactKind, revisions], index) => {
                const suggestion = selectedSuggestionReasons[index]
                const selectedRevisionId = selectedRevisions[artifactKind]
                return (
                  <div
                    key={artifactKind}
                    className={`classification-plan-card archive-review-revision-lane${
                      suggestion?.status === "unresolved" ? " is-unresolved" : " is-suggested"
                    }`}
                    role="group"
                    aria-label={`${artifactKindLabel(artifactKind)} revision 비교`}
                  >
                    <header className="archive-review-lane-header">
                      <div>
                        <p className="section-label">Artifact Lane</p>
                        <h4>{artifactKindLabel(artifactKind)}</h4>
                      </div>
                      <span className="status-pill status-neutral">
                        {suggestion?.status === "suggested" ? "suggested" : "manual review"}
                      </span>
                    </header>
                    <p className="inline-muted archive-review-suggestion-copy">
                      {suggestion
                        ? suggestion.status === "suggested"
                          ? `추천 revision ${suggestion.revision_id} · ${suggestionReasonLabel(suggestion.reason_code)}`
                          : `자동 추천 없음 · ${suggestionReasonLabel(suggestion.reason_code)}`
                        : "추천 정보가 없습니다."}
                    </p>
                    {suggestion?.status === "unresolved" ? (
                      <p className="inline-muted">
                        candidate revision: {suggestion.candidate_revision_ids.join(", ") || "-"}
                      </p>
                    ) : null}
                    <div className="table-wrap compact-table archive-review-revision-table">
                      <table>
                        <thead>
                          <tr>
                            <th>선택</th>
                            <th>revision</th>
                            <th>sha256</th>
                            <th>크기</th>
                            <th>관측</th>
                            <th>관계</th>
                          </tr>
                        </thead>
                        <tbody>
                          {revisions.map((revision) => {
                            const inputId = buildRevisionSelectionId(artifactKind, revision.revision_id)
                            return (
                              <tr key={revision.revision_id}>
                                <td>
                                  <input
                                    id={inputId}
                                    className="archive-review-radio"
                                    type="radio"
                                    name={`archive-revision-${artifactKind}`}
                                    aria-label={`${artifactKindLabel(artifactKind)} revision ${revision.revision_id} 선택`}
                                    checked={selectedRevisionId === revision.revision_id}
                                    onChange={() => {
                                      setDismissConfirmOpen(false)
                                      setSelectedRevision(artifactKind, revision.revision_id)
                                    }}
                                  />
                                </td>
                                <td className="mono-cell">
                                  <label htmlFor={inputId}>{revision.revision_id}</label>
                                </td>
                                <td className="mono-cell" title={revision.content_sha256}>
                                  {revision.content_sha256.slice(0, 16)}…
                                </td>
                                <td className="mono-cell">{formatBytes(revision.bytes)}</td>
                                <td>
                                  {revision.observation_count}건
                                  <span className="row-subcopy">
                                    capture {revision.capture_count} · latest {revision.last_observed_at ?? "-"}
                                  </span>
                                </td>
                                <td>
                                  {revision.relationships.join(", ") || "-"}
                                  <span className="row-subcopy">
                                    ledger {revision.matches_ledger_count}/{revision.differs_from_ledger_count} ·
                                    unclaimed {revision.unclaimed_count} · unexpected {revision.unexpected_current_count}
                                  </span>
                                  <span className="row-subcopy">
                                    roles {revision.source_roles.join(", ") || "-"} · {revision.mime_type ?? "mime 없음"}
                                  </span>
                                </td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )
              })}
            </>
          ) : null}
        </div>
      </SectionCard>
    </section>
  )
}
