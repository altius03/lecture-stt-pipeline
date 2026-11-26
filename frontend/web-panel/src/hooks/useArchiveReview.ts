import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import {
  applyArchiveReviewPromotion,
  fetchArchiveReviewCaseDetail,
  fetchArchiveReviewCases,
  planArchiveReviewPromotion,
  postArchiveReviewStatus,
} from "../lib/panelApi"
import type {
  ArchiveReviewArtifactKind,
  ArchiveReviewDetailPayload,
  ArchiveReviewPromotionPlan,
  ArchiveReviewPromotionSelectionRequest,
  ArchiveReviewPromotionSelectionMap,
  ArchiveReviewStatus,
} from "../types"

const ARCHIVE_REVIEW_QUERY_KEY = ["storage-v2", "archive-review"] as const
const ARCHIVE_REVIEW_PAGE_LIMIT = 20

export type ArchiveReviewFilter = "all" | ArchiveReviewStatus

interface UseArchiveReviewOptions {
  initialSelectedCaseKey?: string | null
}

interface PromotionRequestSnapshot {
  caseKey: string
  targetStorageKey: string
  selectedRevisions: ArchiveReviewPromotionSelectionRequest
}

interface PromotionPlanState {
  plan: ArchiveReviewPromotionPlan
  request: PromotionRequestSnapshot
}

interface StatusRequestSnapshot {
  caseKey: string
  reviewStatus: ArchiveReviewStatus
}

function isPositiveInteger(value: number): boolean {
  return Number.isInteger(value) && value > 0
}

function buildRevisionLaneMap(
  detail: ArchiveReviewDetailPayload,
): Map<ArchiveReviewArtifactKind, Set<number>> {
  const lanes = new Map<ArchiveReviewArtifactKind, Set<number>>()
  for (const revision of detail.revisions) {
    if (!lanes.has(revision.artifact_kind)) {
      lanes.set(revision.artifact_kind, new Set<number>())
    }
    lanes.get(revision.artifact_kind)?.add(revision.revision_id)
  }
  return lanes
}

function hasMatchingPlanRequest(
  current: PromotionRequestSnapshot | null | undefined,
  expected: PromotionRequestSnapshot,
): boolean {
  if (!current) {
    return false
  }
  if (
    current.caseKey !== expected.caseKey ||
    current.targetStorageKey !== expected.targetStorageKey
  ) {
    return false
  }
  const currentEntries = Object.entries(current.selectedRevisions)
  const expectedEntries = Object.entries(expected.selectedRevisions) as Array<
    [ArchiveReviewArtifactKind, number]
  >
  if (currentEntries.length !== expectedEntries.length) {
    return false
  }
  return expectedEntries.every(([artifactKind, revisionId]) => current.selectedRevisions[artifactKind] === revisionId)
}

function buildDefaultSelections(
  detail: ArchiveReviewDetailPayload,
): ArchiveReviewPromotionSelectionMap {
  const revisionLanes = buildRevisionLaneMap(detail)
  const confirmedSelections = new Map(
    detail.confirmed_selections.map((selection) => [selection.artifact_kind, selection.revision_id]),
  )
  const suggestedSelections = new Map(
    detail.canonical_suggestions
      .filter((suggestion) => suggestion.status === "suggested" && suggestion.revision_id !== null)
      .map((suggestion) => [suggestion.artifact_kind, suggestion.revision_id as number]),
  )
  const nextSelections: ArchiveReviewPromotionSelectionMap = {}
  for (const [artifactKind, revisionLane] of revisionLanes) {
    const confirmedRevisionId = confirmedSelections.get(artifactKind)
    if (confirmedRevisionId !== undefined && revisionLane.has(confirmedRevisionId)) {
      nextSelections[artifactKind] = confirmedRevisionId
      continue
    }
    const suggestedRevisionId = suggestedSelections.get(artifactKind)
    if (suggestedRevisionId !== undefined && revisionLane.has(suggestedRevisionId)) {
      nextSelections[artifactKind] = suggestedRevisionId
    }
  }
  return nextSelections
}

function normalizeSelectionMap(
  detail: ArchiveReviewDetailPayload | null,
  selections: ArchiveReviewPromotionSelectionMap,
): ArchiveReviewPromotionSelectionRequest | null {
  if (!detail) {
    return null
  }
  const revisionLanes = buildRevisionLaneMap(detail)
  if (revisionLanes.size === 0) {
    return null
  }
  const normalizedEntries: Array<[ArchiveReviewArtifactKind, number]> = []
  for (const [artifactKind, revisionLane] of revisionLanes) {
    const selectedRevision = selections[artifactKind]
    if (typeof selectedRevision !== "number" || !isPositiveInteger(selectedRevision)) {
      return null
    }
    if (!revisionLane.has(selectedRevision)) {
      return null
    }
    normalizedEntries.push([artifactKind, selectedRevision])
  }
  normalizedEntries.sort(([left], [right]) => left.localeCompare(right))
  return Object.fromEntries(normalizedEntries) as ArchiveReviewPromotionSelectionRequest
}

function statusNoticeLabel(status: ArchiveReviewStatus): string {
  switch (status) {
    case "open":
      return "열림"
    case "triaged":
      return "분류 완료"
    case "resolved":
      return "해결"
    case "dismissed":
      return "보류"
    default:
      return status
  }
}

export function useArchiveReview({
  initialSelectedCaseKey = null,
}: UseArchiveReviewOptions = {}) {
  const queryClient = useQueryClient()
  const [reviewStatusFilter, setReviewStatusFilterState] = useState<ArchiveReviewFilter>("all")
  const [offset, setOffsetState] = useState(0)
  const [selectedCaseKey, setSelectedCaseKeyState] = useState<string | null>(initialSelectedCaseKey)
  const [selectedRevisions, setSelectedRevisions] = useState<ArchiveReviewPromotionSelectionMap>({})
  const [targetStorageKey, setTargetStorageKeyState] = useState("")
  const [promotionPlanState, setPromotionPlanState] = useState<PromotionPlanState | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionNotice, setActionNotice] = useState<string | null>(null)
  const selectedCaseKeyRef = useRef<string | null>(initialSelectedCaseKey)
  const planRequestIdRef = useRef(0)
  const statusRequestIdRef = useRef(0)
  const applyRequestIdRef = useRef(0)
  const detailCaseKeyRef = useRef<string | null>(null)
  const promotionPlanStateRef = useRef<PromotionPlanState | null>(null)

  useEffect(() => {
    selectedCaseKeyRef.current = selectedCaseKey
  }, [selectedCaseKey])

  useEffect(() => {
    promotionPlanStateRef.current = promotionPlanState
  }, [promotionPlanState])

  const invalidatePendingPlan = useCallback(() => {
    planRequestIdRef.current += 1
  }, [])

  const clearTransientState = useCallback(() => {
    invalidatePendingPlan()
    setPromotionPlanState(null)
    setActionError(null)
    setActionNotice(null)
  }, [invalidatePendingPlan])

  const resetSelectionState = useCallback(() => {
    setSelectedRevisions({})
    setTargetStorageKeyState("")
  }, [])

  useEffect(() => {
    if (initialSelectedCaseKey === null || initialSelectedCaseKey === selectedCaseKeyRef.current) {
      return
    }
    clearTransientState()
    resetSelectionState()
    detailCaseKeyRef.current = null
    setOffsetState(0)
    setSelectedCaseKeyState(initialSelectedCaseKey)
  }, [clearTransientState, initialSelectedCaseKey, resetSelectionState])

  const listQuery = useQuery({
    queryKey: [
      ...ARCHIVE_REVIEW_QUERY_KEY,
      "cases",
      {
        reviewStatus: reviewStatusFilter === "all" ? null : reviewStatusFilter,
        limit: ARCHIVE_REVIEW_PAGE_LIMIT,
        offset,
      },
    ],
    queryFn: () =>
      fetchArchiveReviewCases({
        reviewStatus: reviewStatusFilter === "all" ? undefined : reviewStatusFilter,
        limit: ARCHIVE_REVIEW_PAGE_LIMIT,
        offset,
      }),
  })

  useEffect(() => {
    const cases = listQuery.data?.cases ?? []
    const preservingInitialSelection =
      initialSelectedCaseKey !== null && selectedCaseKey === initialSelectedCaseKey
    if (cases.length === 0) {
      if (preservingInitialSelection) {
        return
      }
      setSelectedCaseKeyState(null)
      resetSelectionState()
      clearTransientState()
      return
    }
    if (selectedCaseKey !== null && cases.some((item) => item.case_key === selectedCaseKey)) {
      return
    }
    if (preservingInitialSelection) {
      return
    }
    clearTransientState()
    resetSelectionState()
    setSelectedCaseKeyState(cases[0]?.case_key ?? null)
  }, [
    clearTransientState,
    initialSelectedCaseKey,
    listQuery.data?.cases,
    resetSelectionState,
    selectedCaseKey,
  ])

  const detailQuery = useQuery({
    queryKey: [...ARCHIVE_REVIEW_QUERY_KEY, "detail", selectedCaseKey],
    queryFn: () => fetchArchiveReviewCaseDetail(selectedCaseKey as string),
    enabled: selectedCaseKey !== null,
  })

  useEffect(() => {
    const detail = detailQuery.data
    if (!detail) {
      return
    }
    const caseKey = detail?.case.case_key ?? null
    if (!caseKey || caseKey === detailCaseKeyRef.current) {
      return
    }
    detailCaseKeyRef.current = caseKey
    setSelectedRevisions(buildDefaultSelections(detail))
    setTargetStorageKeyState(detail.case.promoted_storage_key ?? "")
  }, [detailQuery.data])

  const refreshArchiveQueries = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: ARCHIVE_REVIEW_QUERY_KEY })
  }, [queryClient])

  const changeFilter = useCallback(
    (nextFilter: ArchiveReviewFilter) => {
      if (nextFilter === reviewStatusFilter) {
        return
      }
      clearTransientState()
      resetSelectionState()
      detailCaseKeyRef.current = null
      setSelectedCaseKeyState(null)
      setOffsetState(0)
      setReviewStatusFilterState(nextFilter)
    },
    [clearTransientState, resetSelectionState, reviewStatusFilter],
  )

  const changeOffset = useCallback(
    (nextOffset: number) => {
      const boundedOffset = Math.max(0, nextOffset)
      if (boundedOffset === offset) {
        return
      }
      clearTransientState()
      resetSelectionState()
      detailCaseKeyRef.current = null
      setSelectedCaseKeyState(null)
      setOffsetState(boundedOffset)
    },
    [clearTransientState, offset, resetSelectionState],
  )

  const setSelectedCaseKey = useCallback(
    (caseKey: string | null) => {
      if (caseKey === selectedCaseKeyRef.current) {
        return
      }
      clearTransientState()
      resetSelectionState()
      detailCaseKeyRef.current = null
      setSelectedCaseKeyState(caseKey)
    },
    [clearTransientState, resetSelectionState],
  )

  const setSelectedRevision = useCallback(
    (artifactKind: ArchiveReviewArtifactKind, revisionId: number) => {
      clearTransientState()
      setSelectedRevisions((current) => ({
        ...current,
        [artifactKind]: revisionId,
      }))
    },
    [clearTransientState],
  )

  const setTargetStorageKey = useCallback(
    (value: string) => {
      clearTransientState()
      setTargetStorageKeyState(value)
    },
    [clearTransientState],
  )

  const normalizedSelectionMap = useMemo(
    () => normalizeSelectionMap(detailQuery.data ?? null, selectedRevisions),
    [detailQuery.data, selectedRevisions],
  )

  const updateStatusMutation = useMutation({
    mutationFn: async (request: StatusRequestSnapshot) =>
      postArchiveReviewStatus(request.caseKey, {
        review_status: request.reviewStatus,
        allow_write: true,
      }),
    onMutate: (request) => {
      setActionError(null)
      setActionNotice(null)
      setPromotionPlanState(null)
      const requestId = statusRequestIdRef.current + 1
      statusRequestIdRef.current = requestId
      return { requestId, request }
    },
    onSuccess: async (result, request, context) => {
      await refreshArchiveQueries()
      if (
        !context ||
        context.requestId !== statusRequestIdRef.current ||
        selectedCaseKeyRef.current !== request.caseKey
      ) {
        return
      }
      setActionNotice(
        result.changed
          ? `검토 상태를 ${statusNoticeLabel(result.review_status)}으로 변경했습니다.`
          : `이미 ${statusNoticeLabel(result.review_status)} 상태입니다.`,
      )
    },
    onError: (error, request, context) => {
      if (
        !context ||
        context.requestId !== statusRequestIdRef.current ||
        selectedCaseKeyRef.current !== request.caseKey
      ) {
        return
      }
      setActionError(error instanceof Error ? error.message : "검토 상태 변경 중 오류가 발생했습니다.")
    },
  })

  const planPromotionMutation = useMutation({
    mutationFn: async (request: PromotionRequestSnapshot) =>
      planArchiveReviewPromotion(request.caseKey, {
        target_storage_key: request.targetStorageKey,
        selected_revisions: request.selectedRevisions,
      }),
    onMutate: (request) => {
      setActionError(null)
      setActionNotice(null)
      setPromotionPlanState(null)
      const requestId = planRequestIdRef.current + 1
      planRequestIdRef.current = requestId
      return { requestId, request }
    },
    onSuccess: (plan, request, context) => {
      if (
        !context ||
        context.requestId !== planRequestIdRef.current ||
        selectedCaseKeyRef.current !== request.caseKey
      ) {
        return
      }
      setPromotionPlanState({
        plan,
        request,
      })
      setActionNotice("정본 연결 계획을 불러왔습니다. digest와 대상을 확인한 뒤 명시적으로 적용하세요.")
    },
    onError: (error, request, context) => {
      if (
        !context ||
        context.requestId !== planRequestIdRef.current ||
        selectedCaseKeyRef.current !== request.caseKey
      ) {
        return
      }
      setActionError(error instanceof Error ? error.message : "정본 연결 계획 생성 중 오류가 발생했습니다.")
    },
  })

  const applyPromotionMutation = useMutation({
    mutationFn: async (planState: PromotionPlanState) =>
      applyArchiveReviewPromotion(planState.request.caseKey, {
        target_storage_key: planState.request.targetStorageKey,
        selected_revisions: planState.request.selectedRevisions,
        expected_count: planState.plan.expected_count,
        expected_plan_sha256: planState.plan.plan_sha256,
        allow_write: true,
      }),
    onMutate: (planState) => {
      setActionError(null)
      setActionNotice(null)
      const requestId = applyRequestIdRef.current + 1
      applyRequestIdRef.current = requestId
      return { requestId, request: planState.request }
    },
    onSuccess: async (result, planState, context) => {
      await refreshArchiveQueries()
      if (
        !context ||
        context.requestId !== applyRequestIdRef.current ||
        selectedCaseKeyRef.current !== planState.request.caseKey ||
        !hasMatchingPlanRequest(promotionPlanStateRef.current?.request, planState.request)
      ) {
        return
      }
      setPromotionPlanState(null)
      setActionNotice(
        result.status === "skipped"
          ? "이미 같은 digest로 정본 연결이 기록된 사례입니다."
          : "정본 연결과 canonical 선택 기록을 남겼습니다.",
      )
    },
    onError: (error, planState, context) => {
      if (
        !context ||
        context.requestId !== applyRequestIdRef.current ||
        selectedCaseKeyRef.current !== planState.request.caseKey ||
        !hasMatchingPlanRequest(promotionPlanStateRef.current?.request, planState.request)
      ) {
        return
      }
      setActionError(error instanceof Error ? error.message : "정본 연결 적용 중 오류가 발생했습니다.")
    },
  })

  const selectedCase =
    listQuery.data?.cases.find((item) => item.case_key === selectedCaseKey) ?? null
  const capabilities = detailQuery.data?.capabilities ?? listQuery.data?.capabilities
  const hasCompleteSelection = normalizedSelectionMap !== null

  return {
    reviewCases: listQuery.data ?? null,
    reviewCasesLoading: listQuery.isLoading,
    reviewCasesError: listQuery.error instanceof Error ? listQuery.error.message : null,
    reviewStatusFilter,
    setReviewStatusFilter: changeFilter,
    offset,
    pageLimit: ARCHIVE_REVIEW_PAGE_LIMIT,
    setOffset: changeOffset,
    selectedCaseKey,
    selectedCase,
    setSelectedCaseKey,
    caseDetail: detailQuery.data ?? null,
    caseDetailLoading: detailQuery.isLoading,
    caseDetailError: detailQuery.error instanceof Error ? detailQuery.error.message : null,
    selectedRevisions,
    setSelectedRevision,
    targetStorageKey,
    setTargetStorageKey,
    promotionPlan: promotionPlanState?.plan ?? null,
    promotionPlanRequest: promotionPlanState?.request ?? null,
    actionError,
    actionNotice,
    capabilities,
    hasCompleteSelection,
    loadPromotionPlan: async (): Promise<boolean> => {
      if (!selectedCaseKey || !normalizedSelectionMap) {
        return false
      }
      const target = targetStorageKey.trim()
      if (!target) {
        setActionError("정본 recording storage_key를 입력하세요.")
        return false
      }
      try {
        await planPromotionMutation.mutateAsync({
          caseKey: selectedCaseKey,
          targetStorageKey: target,
          selectedRevisions: normalizedSelectionMap,
        })
        return true
      } catch {
        return false
      }
    },
    applyPromotionPlan: async (): Promise<boolean> => {
      if (!promotionPlanState) {
        return false
      }
      try {
        await applyPromotionMutation.mutateAsync(promotionPlanState)
        return true
      } catch {
        return false
      }
    },
    updateReviewStatus: async (nextStatus: ArchiveReviewStatus): Promise<boolean> => {
      if (!selectedCaseKey) {
        return false
      }
      try {
        await updateStatusMutation.mutateAsync({
          caseKey: selectedCaseKey,
          reviewStatus: nextStatus,
        })
        return true
      } catch {
        return false
      }
    },
    clearTransientState,
    statusUpdatePending: updateStatusMutation.isPending,
    promotionPlanPending: planPromotionMutation.isPending,
    promotionApplyPending: applyPromotionMutation.isPending,
  }
}
