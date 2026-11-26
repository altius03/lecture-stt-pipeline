import { createRef } from "react"
import { render, screen, within } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { ActivityPanel } from "../src/components/panel/ActivityPanel"
import type { PanelJob } from "../src/types"

function createJob(overrides: Partial<PanelJob>): PanelJob {
  return {
    id: 1,
    status: "PENDING",
    file_name: "20260723_operating_system_2교시.m4a",
    updated_at: "2026-07-23T09:00:00+09:00",
    step: "",
    progress_pct: 0,
    error_message: "",
    ...overrides,
  }
}

describe("ActivityPanel", () => {
  it("renders NEEDS_REVIEW separately from ERROR and prioritizes it when no processing job exists", () => {
    const jobs = [
      createJob({
        id: 11,
        status: "ERROR",
        file_name: "20260723_operating_system_3교시.m4a",
        error_message: "fatal backend error",
      }),
      createJob({
        id: 12,
        status: "NEEDS_REVIEW",
        file_name: "20260723_operating_system_2교시.m4a",
        error_message: "low confidence",
      }),
    ]

    render(
      <ActivityPanel
        jobs={jobs}
        logs={{ text: "", droppedLineCount: 0 }}
        error={null}
        logRef={createRef<HTMLPreElement>()}
        autoFollow
        hasUnread={false}
        pendingAction={null}
        onLogScroll={vi.fn()}
        onToggleAutoFollow={vi.fn()}
        onJumpToLatest={vi.fn()}
        onAction={vi.fn()}
      />,
    )

    const rows = screen.getAllByRole("row")
    const reviewRow = rows.find((row) => within(row).queryByText("확인 필요", { selector: "span" }))
    const errorRow = rows.find((row) => within(row).queryByText("오류", { selector: "span" }))

    expect(reviewRow).toBeDefined()
    expect(errorRow).toBeDefined()
    expect(reviewRow?.className).toContain("is-selected")

    const reviewPill = within(reviewRow as HTMLElement).getByText("확인 필요", { selector: "span" })
    const errorPill = within(errorRow as HTMLElement).getByText("오류", { selector: "span" })

    expect(reviewPill.className).toContain("status-active")
    expect(errorPill.className).toContain("status-negative")
    expect(screen.getByRole("button", { name: "완료·오류 이력 정리" })).toBeTruthy()
  })
})
