import { useCallback, useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import {
  applyTitleSuggestionConfirmation,
  fetchTitleSuggestionDetail,
  fetchTitleSuggestions,
  planTitleSuggestionConfirmation,
  postTitleSuggestionStatus,
} from "../lib/panelApi"
import type {
  TitleSuggestionConfirmationPlan,
  TitleSuggestionDetailPayload,
} from "../types"
import { UNIFIED_REVIEW_QUERY_KEY } from "./useUnifiedReviewFeed"

export const TITLE_SUGGESTIONS_QUERY_KEY = [
  "storage-v2",
  "title-suggestions",
] as const

interface UseTitleSuggestionReviewOptions {
  initialSelectedProposalId?: number | null
}

interface ConfirmationPlanRequest {
  proposalId: number
  requestId: number
  detailUpdatedAt: string
  signal: AbortSignal
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError"
}

function planMatchesDetail(
  plan: TitleSuggestionConfirmationPlan,
  detail: TitleSuggestionDetailPayload,
): boolean {
  const proposal = detail.proposal
  return (
    plan.proposal_id === proposal.id
    && plan.storage_key === proposal.storage_key
    && plan.suggestion_reason === proposal.suggestion_reason
    && plan.proposed_title === proposal.proposed_title
    && plan.transcript_revision === proposal.transcript_revision
    && plan.expected_count === 1
    && plan.materialization === "confirmation_audit_only"
    && plan.mode === "read_only"
    && plan.canonical_metadata_changed === false
  )
}

export function useTitleSuggestionReview({
  initialSelectedProposalId = null,
}: UseTitleSuggestionReviewOptions = {}) {
  const queryClient = useQueryClient()
  const [selectedProposalId, setSelectedProposalIdState] = useState<number | null>(
    initialSelectedProposalId,
  )
  const [confirmationPlan, setConfirmationPlan] =
    useState<TitleSuggestionConfirmationPlan | null>(null)
  const [confirmationPlanDetailUpdatedAt, setConfirmationPlanDetailUpdatedAt] =
    useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionNotice, setActionNotice] = useState<string | null>(null)
  const selectedProposalIdRef = useRef<number | null>(initialSelectedProposalId)
  const proposalDetailRef = useRef<TitleSuggestionDetailPayload | null>(null)
  const confirmationPlanRequestIdRef = useRef(0)
  const confirmationPlanAbortRef = useRef<AbortController | null>(null)

  const invalidatePendingConfirmationPlan = useCallback(() => {
    confirmationPlanRequestIdRef.current += 1
    confirmationPlanAbortRef.current?.abort()
    confirmationPlanAbortRef.current = null
  }, [])

  const clearTransientState = useCallback(() => {
    invalidatePendingConfirmationPlan()
    setConfirmationPlan(null)
    setConfirmationPlanDetailUpdatedAt(null)
    setActionError(null)
    setActionNotice(null)
  }, [invalidatePendingConfirmationPlan])

  const updateSelection = useCallback((proposalId: number | null) => {
    selectedProposalIdRef.current = proposalId
    clearTransientState()
    setSelectedProposalIdState(proposalId)
  }, [clearTransientState])

  useEffect(() => {
    if (initialSelectedProposalId === selectedProposalIdRef.current) {
      return
    }
    updateSelection(initialSelectedProposalId)
  }, [initialSelectedProposalId, updateSelection])

  useEffect(() => () => {
    confirmationPlanAbortRef.current?.abort()
  }, [])

  const proposalsQuery = useQuery({
    queryKey: [
      ...TITLE_SUGGESTIONS_QUERY_KEY,
      { status: "suggested", limit: 8 },
    ],
    queryFn: ({ signal }) =>
      fetchTitleSuggestions({ status: "suggested", limit: 8 }, signal),
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })

  const hasFreshListResponse =
    proposalsQuery.isFetchedAfterMount
    && proposalsQuery.isSuccess
    && !proposalsQuery.isFetching
  const proposals = hasFreshListResponse ? proposalsQuery.data ?? null : null
  const titleReviewAvailable = proposals?.available === true

  useEffect(() => {
    if (!titleReviewAvailable || proposals === null) {
      return
    }
    const queue = proposals.proposals
    const preservingInitialSelection =
      initialSelectedProposalId !== null
      && selectedProposalId === initialSelectedProposalId
    if (
      selectedProposalId !== null
      && queue.some((proposal) => proposal.id === selectedProposalId)
    ) {
      return
    }
    if (preservingInitialSelection) {
      return
    }
    const nextProposalId = queue[0]?.id ?? null
    if (nextProposalId !== selectedProposalId) {
      updateSelection(nextProposalId)
    }
  }, [
    initialSelectedProposalId,
    proposals,
    selectedProposalId,
    titleReviewAvailable,
    updateSelection,
  ])

  const detailEnabled =
    titleReviewAvailable
    && selectedProposalId !== null
  const detailQuery = useQuery({
    queryKey: [
      ...TITLE_SUGGESTIONS_QUERY_KEY,
      "detail",
      selectedProposalId,
    ],
    queryFn: ({ signal }) =>
      fetchTitleSuggestionDetail(selectedProposalId as number, signal),
    enabled: detailEnabled,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  const hasFreshDetailResponse =
    detailEnabled
    && detailQuery.isFetchedAfterMount
    && detailQuery.isSuccess
    && !detailQuery.isFetching
  const proposalDetail = hasFreshDetailResponse
    ? detailQuery.data ?? null
    : null

  useEffect(() => {
    proposalDetailRef.current = proposalDetail
  }, [proposalDetail])

  useEffect(() => {
    if (
      confirmationPlan === null
      || confirmationPlanDetailUpdatedAt === null
      || proposalDetail === null
      || confirmationPlan.proposal_id !== proposalDetail.proposal.id
      || confirmationPlanDetailUpdatedAt !== proposalDetail.proposal.updated_at
    ) {
      if (
        confirmationPlan !== null
        && (
          proposalDetail === null
          || confirmationPlan.proposal_id !== proposalDetail.proposal.id
          || confirmationPlanDetailUpdatedAt !== proposalDetail.proposal.updated_at
        )
      ) {
        invalidatePendingConfirmationPlan()
        setConfirmationPlan(null)
        setConfirmationPlanDetailUpdatedAt(null)
      }
    }
  }, [
    confirmationPlan,
    confirmationPlanDetailUpdatedAt,
    invalidatePendingConfirmationPlan,
    proposalDetail,
  ])

  const refreshProposalQueries = useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: TITLE_SUGGESTIONS_QUERY_KEY,
      }),
      queryClient.invalidateQueries({
        queryKey: UNIFIED_REVIEW_QUERY_KEY,
      }),
    ])
  }, [queryClient])

  const rejectProposalMutation = useMutation({
    mutationFn: async (proposalId: number) =>
      postTitleSuggestionStatus(
        proposalId,
        { status: "rejected", allow_write: true },
      ),
    onMutate: () => {
      setActionError(null)
      setActionNotice(null)
    },
    onSuccess: async (result, proposalId) => {
      if (selectedProposalIdRef.current === proposalId) {
        invalidatePendingConfirmationPlan()
        setConfirmationPlan(null)
        setConfirmationPlanDetailUpdatedAt(null)
        setActionNotice(
          result.action === "skipped"
            ? "이미 보류 처리된 제목 제안입니다."
            : "제목 제안을 보류 처리했습니다.",
        )
      }
      await refreshProposalQueries()
    },
    onError: (error, proposalId) => {
      if (selectedProposalIdRef.current !== proposalId) {
        return
      }
      setActionError(
        error instanceof Error
          ? error.message
          : "제목 제안 보류 처리 중 오류가 발생했습니다.",
      )
    },
  })

  const planConfirmationMutation = useMutation({
    mutationFn: async ({
      proposalId,
      signal,
    }: ConfirmationPlanRequest) =>
      planTitleSuggestionConfirmation(proposalId, signal),
    onMutate: () => {
      setActionError(null)
      setActionNotice(null)
      setConfirmationPlan(null)
      setConfirmationPlanDetailUpdatedAt(null)
    },
    onSuccess: (plan, request) => {
      const currentDetail = proposalDetailRef.current
      if (
        request.requestId !== confirmationPlanRequestIdRef.current
        || selectedProposalIdRef.current !== request.proposalId
        || currentDetail?.proposal.updated_at !== request.detailUpdatedAt
        || !planMatchesDetail(plan, currentDetail)
      ) {
        if (
          request.requestId === confirmationPlanRequestIdRef.current
          && selectedProposalIdRef.current === request.proposalId
        ) {
          setActionError(
            "확정 계획이 현재 제목 제안 metadata와 달라졌습니다. 상세를 새로 확인한 뒤 다시 계획하세요.",
          )
        }
        return
      }
      setConfirmationPlan(plan)
      setConfirmationPlanDetailUpdatedAt(request.detailUpdatedAt)
      setActionNotice(
        "확정 계획을 불러왔습니다. 이 확정은 감사 상태만 기록하며 정본 이름은 바꾸지 않습니다.",
      )
    },
    onError: (error, request) => {
      if (
        isAbortError(error)
        || request.requestId !== confirmationPlanRequestIdRef.current
        || selectedProposalIdRef.current !== request.proposalId
      ) {
        return
      }
      setActionError(
        error instanceof Error
          ? error.message
          : "제목 제안 확정 계획 생성 중 오류가 발생했습니다.",
      )
    },
    onSettled: (_data, _error, request) => {
      if (
        request.requestId === confirmationPlanRequestIdRef.current
        && confirmationPlanAbortRef.current?.signal === request.signal
      ) {
        confirmationPlanAbortRef.current = null
      }
    },
  })

  const applyConfirmationMutation = useMutation({
    mutationFn: async (plan: TitleSuggestionConfirmationPlan) =>
      applyTitleSuggestionConfirmation(plan.proposal_id, {
        expected_count: plan.expected_count,
        expected_plan_sha256: plan.plan_sha256,
        allow_write: true,
      }),
    onMutate: () => {
      setActionError(null)
      setActionNotice(null)
    },
    onSuccess: async (result, plan) => {
      if (selectedProposalIdRef.current === plan.proposal_id) {
        invalidatePendingConfirmationPlan()
        setConfirmationPlan(null)
        setConfirmationPlanDetailUpdatedAt(null)
        setActionNotice(
          result.action === "skipped"
            ? "이미 같은 계획으로 검토 확정된 제목 제안입니다."
            : "제목 제안의 검토 상태를 확정했습니다. 정본 이름은 변경하지 않았습니다.",
        )
      }
      await refreshProposalQueries()
    },
    onError: (error, plan) => {
      if (selectedProposalIdRef.current !== plan.proposal_id) {
        return
      }
      setActionError(
        error instanceof Error
          ? error.message
          : "제목 제안 확정 적용 중 오류가 발생했습니다.",
      )
    },
  })

  return {
    proposals,
    proposalsLoading:
      proposalsQuery.isFetching || !proposalsQuery.isFetchedAfterMount,
    proposalsError:
      proposalsQuery.isFetchedAfterMount
      && proposalsQuery.error instanceof Error
        ? proposalsQuery.error.message
        : null,
    selectedProposalId,
    setSelectedProposalId: updateSelection,
    proposalDetail,
    proposalDetailLoading:
      detailEnabled
      && (
        detailQuery.isFetching
        || !detailQuery.isFetchedAfterMount
      ),
    proposalDetailError:
      detailEnabled
      && detailQuery.isFetchedAfterMount
      && detailQuery.error instanceof Error
        ? detailQuery.error.message
        : null,
    confirmationPlan,
    actionError,
    actionNotice,
    clearTransientState,
    rejectProposal: async (): Promise<boolean> => {
      const proposalId = selectedProposalIdRef.current
      const detail = proposalDetailRef.current
      if (
        !titleReviewAvailable
        || proposalId === null
        || detail?.proposal.id !== proposalId
        || detail.proposal.status !== "suggested"
        || detail.capabilities.status_writes_enabled !== true
      ) {
        return false
      }
      try {
        await rejectProposalMutation.mutateAsync(proposalId)
        return true
      } catch {
        return false
      }
    },
    loadConfirmationPlan: async (): Promise<boolean> => {
      const proposal = proposalDetailRef.current?.proposal
      if (
        !titleReviewAvailable
        || proposal === undefined
        || proposal.id !== selectedProposalIdRef.current
        || proposal.status !== "suggested"
      ) {
        return false
      }
      invalidatePendingConfirmationPlan()
      const requestId = confirmationPlanRequestIdRef.current
      const controller = new AbortController()
      confirmationPlanAbortRef.current = controller
      try {
        await planConfirmationMutation.mutateAsync({
          proposalId: proposal.id,
          requestId,
          detailUpdatedAt: proposal.updated_at,
          signal: controller.signal,
        })
        return true
      } catch {
        return false
      }
    },
    applyConfirmationPlan: async (): Promise<boolean> => {
      const plan = confirmationPlan
      const proposal = proposalDetailRef.current?.proposal
      if (
        plan === null
        || proposal === undefined
        || proposalDetailRef.current?.capabilities.confirmations_enabled !== true
        || plan.proposal_id !== selectedProposalIdRef.current
        || proposal.id !== plan.proposal_id
        || proposal.status !== "suggested"
        || confirmationPlanDetailUpdatedAt !== proposal.updated_at
        || !planMatchesDetail(plan, proposalDetailRef.current)
      ) {
        return false
      }
      try {
        await applyConfirmationMutation.mutateAsync(plan)
        return true
      } catch {
        return false
      }
    },
    rejectPending: rejectProposalMutation.isPending,
    confirmationPlanPending: planConfirmationMutation.isPending,
    confirmationApplyPending: applyConfirmationMutation.isPending,
  }
}
