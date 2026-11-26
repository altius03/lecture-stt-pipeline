import { useMemo } from "react"
import { useQuery } from "@tanstack/react-query"

import { fetchUnifiedReviewFeed } from "../lib/panelApi"
import type {
  UnifiedReviewFeedPayload,
  UnifiedReviewFeedSource,
  UnifiedReviewFeedSourceLedgerPayload,
} from "../types"

const DEFAULT_LIMIT = 100
const SOURCE_ORDER = [
  "archive",
  "timetable",
  "title",
  "recording",
] as const satisfies readonly UnifiedReviewFeedSource[]
export const UNIFIED_REVIEW_QUERY_KEY = [
  "storage-v2",
  "review-feed",
  "canonical",
] as const

export type UnifiedReviewItemSource = UnifiedReviewFeedSource
export type UnifiedReviewLedgerStatus = "loading" | "ready" | "error" | "unavailable"

export interface UnifiedReviewFeedItem {
  id: string
  source: UnifiedReviewItemSource
  title: string
  stateLabel: string
  reasonLabel: string
  timestamp: string
  metaLabel: string
  href: string
}

export interface UnifiedReviewSourceLedger {
  source: UnifiedReviewItemSource
  label: string
  status: UnifiedReviewLedgerStatus
  available: boolean | null
  visibleCount: number
  totalCount: number | null
  truncated: boolean
  note: string
  error: string | null
}

function normalizeLimit(limit: number | undefined): number {
  if (limit === undefined) {
    return DEFAULT_LIMIT
  }
  if (!Number.isInteger(limit) || !Number.isSafeInteger(limit) || limit <= 0) {
    throw new Error("Unified review limit must be a positive integer.")
  }
  return Math.min(limit, DEFAULT_LIMIT)
}

function normalizeOffset(offset: number | undefined): number {
  if (offset === undefined) {
    return 0
  }
  if (!Number.isInteger(offset) || !Number.isSafeInteger(offset) || offset < 0) {
    throw new Error("Unified review offset must be a non-negative integer.")
  }
  return offset
}

function defaultLedgerLabel(source: UnifiedReviewItemSource): string {
  switch (source) {
    case "archive":
      return "archive"
    case "timetable":
      return "classification"
    case "title":
      return "title"
    case "recording":
      return "recording"
    default:
      return source
  }
}

function buildPendingLedger(
  source: UnifiedReviewItemSource,
  status: "loading" | "error",
  error: string | null,
): UnifiedReviewSourceLedger {
  const sourceLabel = defaultLedgerLabel(source)
  return {
    source,
    label: sourceLabel,
    status,
    available: null,
    visibleCount: 0,
    totalCount: null,
    truncated: false,
    note:
      status === "loading"
        ? "server canonical feed를 새 응답 기준으로 확인 중입니다."
        : "새 응답을 받지 못해 server canonical feed를 숨깁니다.",
    error,
  }
}

function mapItem(item: UnifiedReviewFeedPayload["items"][number]): UnifiedReviewFeedItem {
  return {
    id: item.id,
    source: item.source,
    title: item.title,
    stateLabel: item.state_label,
    reasonLabel: item.reason_label,
    timestamp: item.timestamp,
    metaLabel: item.meta_label,
    href: item.href,
  }
}

function mapLedger(source: UnifiedReviewFeedSourceLedgerPayload): UnifiedReviewSourceLedger {
  return {
    source: source.source,
    label: source.label,
    status: source.status,
    available: source.available,
    visibleCount: source.visible_count,
    totalCount: source.total_count,
    truncated: source.truncated,
    note: source.note,
    error: null,
  }
}

export function useUnifiedReviewFeed(params?: { offset?: number; limit?: number }) {
  const limit = normalizeLimit(params?.limit)
  const offset = normalizeOffset(params?.offset)
  const query = useQuery({
    queryKey: [...UNIFIED_REVIEW_QUERY_KEY, { limit, offset }],
    queryFn: ({ signal }) => fetchUnifiedReviewFeed({ limit, offset }, signal),
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })

  const ready = query.isFetchedAfterMount && query.isSuccess
  const failedAfterMount = query.isFetchedAfterMount && query.isError && !query.isFetching
  const data = ready && !query.isFetching ? query.data ?? null : null

  return useMemo(() => {
    const items = data?.items.map(mapItem) ?? []
    const ledgers = data
      ? data.sources.map(mapLedger)
      : SOURCE_ORDER.map((source) =>
          buildPendingLedger(
            source,
            failedAfterMount ? "error" : "loading",
            failedAfterMount && query.error instanceof Error ? query.error.message : null,
          ),
        )
    const total = data?.total ?? 0
    const resolvedLimit = data?.filters.limit ?? limit
    const resolvedOffset = data?.filters.offset ?? offset
    const hasPrevious = resolvedOffset > 0
    const hasNext = resolvedOffset + items.length < total

    return {
      items,
      ledgers,
      hasHealthySource: ledgers.some((ledger) => ledger.status === "ready"),
      isLoading: ledgers.some((ledger) => ledger.status === "loading"),
      hasVisibleItems: items.length > 0,
      pagination: {
        limit: resolvedLimit,
        offset: resolvedOffset,
        total,
        hasPrevious,
        hasNext,
      },
    }
  }, [data, failedAfterMount, limit, offset, query.error])
}
