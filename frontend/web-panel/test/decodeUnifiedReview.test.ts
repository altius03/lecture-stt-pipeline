import { describe, expect, it } from "vitest"

import { decodeUnifiedReviewFeed } from "../src/lib/decodeUnifiedReview"
import type { UnifiedReviewFeedPayload } from "../src/types"
import { buildUnifiedReviewFeedPayload } from "./unifiedReviewFixtures"

function mutate(
  transform: (payload: UnifiedReviewFeedPayload) => void,
): UnifiedReviewFeedPayload {
  const payload = structuredClone(buildUnifiedReviewFeedPayload())
  transform(payload)
  return payload
}

describe("decodeUnifiedReviewFeed", () => {
  it("accepts a valid canonical feed payload", () => {
    const payload = buildUnifiedReviewFeedPayload()
    expect(decodeUnifiedReviewFeed(payload)).toEqual(payload)
  })

  it("rejects closed-schema extras on the root payload", () => {
    const payload = {
      ...buildUnifiedReviewFeedPayload(),
      digest: "unexpected",
    }

    expect(() => decodeUnifiedReviewFeed(payload)).toThrow(/허용되지 않은 필드/)
  })

  it("rejects duplicate item ids", () => {
    const payload = mutate((candidate) => {
      candidate.items[1].id = candidate.items[0].id
    })

    expect(() => decodeUnifiedReviewFeed(payload)).toThrow(/id가 중복/)
  })

  it("rejects duplicate source ledgers", () => {
    const payload = mutate((candidate) => {
      candidate.sources[3].source = "archive"
      candidate.sources[3].visible_count = 1
      candidate.sources[3].total_count = 1
      candidate.sources[3].status = "ready"
      candidate.sources[3].available = true
      candidate.sources[3].note = "duplicate archive"
    })

    expect(() => decodeUnifiedReviewFeed(payload)).toThrow(/source가 중복/)
  })

  it("rejects id and href identity mismatches", () => {
    const payload = mutate((candidate) => {
      candidate.items[0].href = "#review/archive/CASE_999"
    })

    expect(() => decodeUnifiedReviewFeed(payload)).toThrow(/href identity/)
  })

  it("requires a canonical positive id for title source deep links", () => {
    const payload = mutate((candidate) => {
      candidate.items[2].id = "title:01"
      candidate.items[2].href = "#review/title/01"
    })

    expect(() => decodeUnifiedReviewFeed(payload)).toThrow(/JS-safe 양의 정수/)
  })

  it.each([
    { source: "timetable" as const, itemIndex: 1 },
    { source: "title" as const, itemIndex: 2 },
  ])(
    "rejects $source identities above the JS-safe integer bound",
    ({ source, itemIndex }) => {
      const payload = mutate((candidate) => {
        candidate.items[itemIndex].id = `${source}:9007199254740992`
        candidate.items[itemIndex].href =
          `#review/${source}/9007199254740992`
      })

      expect(() => decodeUnifiedReviewFeed(payload)).toThrow(
        /JS-safe 양의 정수/,
      )
    },
  )

  it("requires all four source ledgers exactly once", () => {
    const payload = mutate((candidate) => {
      candidate.sources = candidate.sources.filter(
        (source) => source.source !== "title",
      )
      candidate.items = candidate.items.filter((item) => item.source !== "title")
      candidate.total = 3
    })

    expect(() => decodeUnifiedReviewFeed(payload)).toThrow(/네 source/)
  })

  it("requires the canonical archive, timetable, title, recording ledger order", () => {
    const payload = mutate((candidate) => {
      const timetable = candidate.sources[1]
      candidate.sources[1] = candidate.sources[2]
      candidate.sources[2] = timetable
    })

    expect(() => decodeUnifiedReviewFeed(payload)).toThrow(/canonical 순서/)
  })

  it("rejects non-NFC display metadata", () => {
    const payload = mutate((candidate) => {
      candidate.items[0].title = "강의"
    })

    expect(() => decodeUnifiedReviewFeed(payload)).toThrow(/NFC 정규화/)
  })

  it("rejects pagination bounds that exceed total", () => {
    const payload = mutate((candidate) => {
      candidate.filters.limit = 4
      candidate.filters.offset = 2
      candidate.total = 4
    })

    expect(() => decodeUnifiedReviewFeed(payload)).toThrow(/페이지 범위가 total을 초과/)
  })

  it("accepts a stale offset after the canonical queue becomes empty", () => {
    const payload = mutate((candidate) => {
      candidate.filters.offset = 100
      candidate.total = 0
      candidate.items = []
      for (const source of candidate.sources) {
        source.visible_count = 0
        source.total_count = source.available ? 0 : null
        source.truncated = false
      }
    })

    expect(decodeUnifiedReviewFeed(payload)).toEqual(payload)
  })

  it("rejects inconsistent source totals and availability", () => {
    const wrongTotal = mutate((candidate) => {
      candidate.total = 5
    })
    expect(() => decodeUnifiedReviewFeed(wrongTotal)).toThrow(/source total_count 합계/)

    const wrongAvailability = mutate((candidate) => {
      candidate.available = false
    })
    expect(() => decodeUnifiedReviewFeed(wrongAvailability)).toThrow(/source availability/)
  })

  it("accepts the backend limit bounds including a zero-row metadata request", () => {
    const payload = mutate((candidate) => {
      candidate.filters.limit = 0
      candidate.items = []
      for (const source of candidate.sources) {
        source.visible_count = 0
        source.truncated = source.total_count !== null && source.total_count > 0
      }
    })

    expect(decodeUnifiedReviewFeed(payload)).toEqual(payload)
  })
})
