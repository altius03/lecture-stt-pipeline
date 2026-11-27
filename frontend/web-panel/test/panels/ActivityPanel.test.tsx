import { createRef } from "react"
import { fireEvent, render, screen, within } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { NotificationToggle } from "../../src/components/panel/NotificationToggle"
import { ActionBar } from "../../src/components/ui/ActionBar"
import { ActivityPanel } from "../../src/components/panel/ActivityPanel"
import type { PanelJob } from "../../src/types"

const sampleJob: PanelJob = {
  id: 12,
  status: "DONE",
  file_name: "260723DS.m4a",
  updated_at: "2026-07-23T09:00:00+09:00",
  step: "완료",
  progress_pct: 100,
  error_message: "",
}

describe("ActivityPanel", () => {
  it("filters real log entries by level and preserves the raw log view", () => {
    render(<ActivityPanel
      jobs={[sampleJob]}
      logs={{ text: "2026-07-23 09:00:00 INFO [job=12 base=-] transcription started\n2026-07-23 09:01:00 WARNING [job=12 base=-] quality: low volume", droppedLineCount: 0, offset: 0, revision: 1 }}
      error={null}
      logRef={createRef<HTMLPreElement>()}
      autoFollow
      hasUnread={false}
      onLogScroll={vi.fn()}
      onToggleAutoFollow={vi.fn()}
      onJumpToLatest={vi.fn()}
    />)

    const table = screen.getByRole("table")
    expect(within(table).getByText("WARN")).toBeTruthy()
    expect(within(table).getByText("INFO")).toBeTruthy()
    fireEvent.change(screen.getByLabelText("수준 필터"), { target: { value: "WARNING" } })
    expect(within(table).getByText("WARN")).toBeTruthy()
    expect(within(table).queryByText("INFO")).toBeNull()
    fireEvent.click(screen.getByText("원본 로그"))
    expect(screen.getByText(/quality: low volume/)).toBeTruthy()
  })
  it("disables changes on a read-only connection and keeps refresh available", () => {
    render(<>
      <ActionBar canControl={false} actions={[{ action: "stop", label: "중지" }, { action: "refresh", label: "동기화" }]}
        pendingAction={null} />
      <NotificationToggle canControl={false} pendingSelection={null} onSave={vi.fn()}
        notification={{selection: "disabled", selected_label: "꺼짐", apply_label: "", options: [],
          can_apply_now: false, restart_required: false}} />
    </>)
    expect(screen.getByRole("button", { name: "중지" }).hasAttribute("disabled")).toBe(true)
    expect(screen.getByRole("switch", { name: "알림 사용" }).hasAttribute("disabled")).toBe(true)
    expect(screen.getByRole("button", { name: "동기화" }).hasAttribute("disabled")).toBe(false)
  })

  it("keeps explicitly different job IDs out of the selected recording's logs", () => {
    const job = { ...sampleJob, id: 1, file_name: "260319OOP_1.m4a", status: "PROCESSING" }
    const text = [
      "2026-03-23 01:42:11,123 INFO [job=2 base=260319OOP_1] 다른 녹음 이벤트",
      "2026-03-23 01:42:12,123 INFO [job=1 base=260319OOP_1] 선택한 녹음 이벤트",
    ].join("\n")
    render(<ActivityPanel selectedJobId={1} jobs={[job, { ...job, id: 2 }]}
      logs={{ text, droppedLineCount: 0, offset: 10, revision: 1 }}
      error={null} logRef={createRef<HTMLPreElement>()} autoFollow hasUnread={false}
      onLogScroll={vi.fn()} onToggleAutoFollow={vi.fn()}
      onJumpToLatest={vi.fn()} />)
    expect(screen.getByText("선택한 녹음 이벤트")).toBeTruthy()
    expect(screen.queryByText("다른 녹음 이벤트")).toBeNull()
  })
})
