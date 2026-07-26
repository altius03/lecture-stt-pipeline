import { useCallback, useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import {
  applyClassificationConfirmation,
  fetchClassificationProposalDetail,
  fetchClassificationProposals,
  fetchTimetableEntries,
  planClassificationConfirmation,
  postClassificationStatus,
} from "../lib/panelApi"
import type { ClassificationConfirmationPlan } from "../types"

const TIMETABLE_ENTRIES_QUERY_KEY = ["storage-v2", "timetable", "entries"] as const
const TIMETABLE_CLASSIFICATIONS_QUERY_KEY = ["storage-v2", "timetable", "classifications"] as const

interface UseTimetableReviewOptions {
  initialSelectedProposalId?: number | null
}

export function useTimetableReview({
  initialSelectedProposalId = null,
}: UseTimetableReviewOptions = {}) {
  const queryClient = useQueryClient()
  const [selectedProposalId, setSelectedProposalIdState] = useState<number | null>(initialSelectedProposalId)
  const [confirmationPlan, setConfirmationPlan] = useState<ClassificationConfirmationPlan | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionNotice, setActionNotice] = useState<string | null>(null)
  const selectedProposalIdRef = useRef<number | null>(initialSelectedProposalId)
  const confirmationPlanRequestIdRef = useRef(0)

  useEffect(() => {
    selectedProposalIdRef.current = selectedProposalId
  }, [selectedProposalId])

  const invalidatePendingConfirmationPlan = useCallback(() => {
    confirmationPlanRequestIdRef.current += 1
  }, [])

  const clearTransientState = useCallback(() => {
    invalidatePendingConfirmationPlan()
    setConfirmationPlan(null)
    setActionError(null)
    setActionNotice(null)
  }, [invalidatePendingConfirmationPlan])

  useEffect(() => {
    if (
      initialSelectedProposalId === null
      || initialSelectedProposalId === selectedProposalIdRef.current
    ) {
      return
    }
    clearTransientState()
    setSelectedProposalIdState(initialSelectedProposalId)
  }, [clearTransientState, initialSelectedProposalId])

  const setSelectedProposalId = useCallback((proposalId: number | null) => {
    clearTransientState()
    setSelectedProposalIdState(proposalId)
  }, [clearTransientState])

  const entriesQuery = useQuery({
    queryKey: [...TIMETABLE_ENTRIES_QUERY_KEY, { limit: 12 }],
    queryFn: () => fetchTimetableEntries({ limit: 12 }),
  })

  const proposalsQuery = useQuery({
    queryKey: [
      ...TIMETABLE_CLASSIFICATIONS_QUERY_KEY,
      { status: "suggested", limit: 8 },
    ],
    queryFn: () =>
      fetchClassificationProposals({ status: "suggested", limit: 8 }),
  })

  useEffect(() => {
    const proposals = proposalsQuery.data?.proposals ?? []
    const preservingInitialSelection =
      initialSelectedProposalId !== null && selectedProposalId === initialSelectedProposalId
    if (proposals.length === 0) {
      if (preservingInitialSelection) {
        return
      }
      setSelectedProposalIdState(null)
      invalidatePendingConfirmationPlan()
      setConfirmationPlan(null)
      return
    }
    if (selectedProposalId !== null && proposals.some((proposal) => proposal.id === selectedProposalId)) {
      return
    }
    if (preservingInitialSelection) {
      return
    }
    setSelectedProposalIdState(proposals[0]?.id ?? null)
    invalidatePendingConfirmationPlan()
    setConfirmationPlan(null)
  }, [
    initialSelectedProposalId,
    invalidatePendingConfirmationPlan,
    proposalsQuery.data?.proposals,
    selectedProposalId,
  ])

  const detailQuery = useQuery({
    queryKey: [...TIMETABLE_CLASSIFICATIONS_QUERY_KEY, "detail", selectedProposalId],
    queryFn: () => fetchClassificationProposalDetail(selectedProposalId as number),
    enabled: selectedProposalId !== null,
  })

  const refreshProposalQueries = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: TIMETABLE_CLASSIFICATIONS_QUERY_KEY })
  }, [queryClient])

  const rejectProposalMutation = useMutation({
    mutationFn: async (proposalId: number) =>
      postClassificationStatus(proposalId, { status: "rejected", allow_write: true }),
    onMutate: () => {
      setActionError(null)
      setActionNotice(null)
    },
    onSuccess: async (result) => {
      if (selectedProposalIdRef.current === result.proposal_id) {
        setSelectedProposalIdState(null)
      }
      setConfirmationPlan(null)
      setActionNotice(
        result.action === "skipped" ? "이미 보류 처리된 제안입니다." : "제안을 보류 처리했습니다.",
      )
      await refreshProposalQueries()
    },
    onError: (error) => {
      setActionError(error instanceof Error ? error.message : "제안 보류 처리 중 오류가 발생했습니다.")
    },
  })

  const planConfirmationMutation = useMutation({
    mutationFn: async (proposalId: number) => planClassificationConfirmation(proposalId),
    onMutate: (proposalId) => {
      setActionError(null)
      setActionNotice(null)
      setConfirmationPlan(null)
      const requestId = confirmationPlanRequestIdRef.current + 1
      confirmationPlanRequestIdRef.current = requestId
      return { proposalId, requestId }
    },
    onSuccess: (plan, proposalId, context) => {
      if (
        !context
        || context.requestId !== confirmationPlanRequestIdRef.current
        || selectedProposalIdRef.current !== proposalId
      ) {
        return
      }
      setConfirmationPlan(plan)
      setActionNotice("확정 계획을 불러왔습니다. 내용을 확인한 뒤 명시적으로 적용하세요.")
    },
    onError: (error, proposalId, context) => {
      if (
        !context
        || context.requestId !== confirmationPlanRequestIdRef.current
        || selectedProposalIdRef.current !== proposalId
      ) {
        return
      }
      setActionError(error instanceof Error ? error.message : "확정 계획 생성 중 오류가 발생했습니다.")
    },
  })

  const applyConfirmationMutation = useMutation({
    mutationFn: async (plan: ClassificationConfirmationPlan) =>
      applyClassificationConfirmation(plan.proposal_id, {
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
        setSelectedProposalIdState(null)
      }
      setConfirmationPlan(null)
      setActionNotice(
        result.action === "skipped" ? "이미 같은 계획으로 확정된 제안입니다." : "분류 제안을 확정했습니다.",
      )
      await refreshProposalQueries()
    },
    onError: (error) => {
      setActionError(error instanceof Error ? error.message : "분류 확정 적용 중 오류가 발생했습니다.")
    },
  })

  return {
    entries: entriesQuery.data ?? null,
    entriesLoading: entriesQuery.isLoading,
    entriesError: entriesQuery.error instanceof Error ? entriesQuery.error.message : null,
    proposals: proposalsQuery.data ?? null,
    proposalsLoading: proposalsQuery.isLoading,
    proposalsError: proposalsQuery.error instanceof Error ? proposalsQuery.error.message : null,
    selectedProposalId,
    setSelectedProposalId,
    proposalDetail: detailQuery.data ?? null,
    proposalDetailLoading: detailQuery.isLoading,
    proposalDetailError: detailQuery.error instanceof Error ? detailQuery.error.message : null,
    confirmationPlan,
    actionError,
    actionNotice,
    clearTransientState,
    rejectProposal: async (): Promise<boolean> => {
      if (selectedProposalId === null) {
        return false
      }
      try {
        await rejectProposalMutation.mutateAsync(selectedProposalId)
        return true
      } catch {
        return false
      }
    },
    loadConfirmationPlan: async (): Promise<boolean> => {
      if (selectedProposalId === null) {
        return false
      }
      try {
        await planConfirmationMutation.mutateAsync(selectedProposalId)
        return true
      } catch {
        return false
      }
    },
    applyConfirmationPlan: async (): Promise<boolean> => {
      if (confirmationPlan === null) {
        return false
      }
      try {
        await applyConfirmationMutation.mutateAsync(confirmationPlan)
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
