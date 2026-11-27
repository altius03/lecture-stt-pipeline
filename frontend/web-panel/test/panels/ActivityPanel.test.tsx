import { createRef } from "react"
import { render, screen, within } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { NotificationToggle } from "../../src/components/panel/NotificationToggle"
import { ActionBar } from "../../src/components/ui/ActionBar"
import { ActivityPanel } from "../../src/components/panel/ActivityPanel"
import type { PanelJob } from "../../src/types"

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
    expect(screen.getByRole("button", { name: "완료 이력 정리" })).toBeTruthy()
  })

  it("disables changes on a read-only connection and keeps refresh available", () => {
    render(<>
      <ActionBar canControl={false} actions={[{ action: "stop", label: "중지" }, { action: "refresh", label: "동기화" }]}
        onAction={vi.fn()} pendingAction={null} />
      <NotificationToggle canControl={false} pendingSelection={null} onSave={vi.fn()}
        notification={{selection: "disabled", selected_label: "꺼짐", apply_label: "", options: [],
          can_apply_now: false, restart_required: false}} />
    </>)
    expect(screen.getByRole("button", { name: "중지" }).hasAttribute("disabled")).toBe(true)
    expect(screen.getByRole("combobox", { name: "알림 채널" }).hasAttribute("disabled")).toBe(true)
    expect(screen.getByRole("button", { name: "동기화" }).hasAttribute("disabled")).toBe(false)
  })

  it("keeps explicitly different job IDs out of the selected recording's logs", () => {
    const job = createJob({ id: 1, file_name: "260319OOP_1.m4a", status: "PROCESSING" })
    const text = [
      "2026-03-23 01:42:11,123 INFO [job=2 base=260319OOP_1] 다른 녹음 이벤트",
      "2026-03-23 01:42:12,123 INFO [job=1 base=260319OOP_1] 선택한 녹음 이벤트",
    ].join("\n")
    render(<ActivityPanel jobs={[job, { ...job, id: 2 }]}
      logs={{ text, droppedLineCount: 0, offset: 10, revision: 1 }}
      error={null} logRef={createRef<HTMLPreElement>()} autoFollow hasUnread={false}
      pendingAction={null} onLogScroll={vi.fn()} onToggleAutoFollow={vi.fn()}
      onJumpToLatest={vi.fn()} onAction={vi.fn()} />)
    expect(screen.getByText("선택한 녹음 이벤트")).toBeTruthy()
    expect(screen.queryByText("다른 녹음 이벤트")).toBeNull()
  })
})
