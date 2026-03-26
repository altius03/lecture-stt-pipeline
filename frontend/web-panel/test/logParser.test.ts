import { describe, expect, it } from "vitest"

import { parseLectureFileInfo, parsePanelLogText } from "../src/lib/logParser"
import type { PanelJob } from "../src/types"

function createJob(overrides: Partial<PanelJob> = {}): PanelJob {
  return {
    id: 184,
    status: "PROCESSING",
    file_name: "260319OOP_1.txt",
    updated_at: "2026-03-23T02:00:00+09:00",
    step: "전사 시작/진행",
    progress_pct: 50,
    error_message: "",
    ...overrides,
  }
}

describe("logParser", () => {
  it("parses lecture file metadata and formats Korean labels", () => {
    const parsed = parseLectureFileInfo("260319OOP_2.json")

    expect(parsed).not.toBeNull()
    expect(parsed?.courseCode).toBe("OOP")
    expect(parsed?.courseName).toBe("객체지향언어")
    expect(parsed?.dateLabel).toBe("2026-03-19(목)")
    expect(parsed?.displayTitle).toBe("객체지향언어 2교시")
    expect(parsed?.displayMeta).toBe("2026-03-19(목) · OOP · json")
  })

  it("normalizes Dstr to DStr", () => {
    const parsed = parseLectureFileInfo("260319Dstr_1.md")

    expect(parsed).not.toBeNull()
    expect(parsed?.courseCode).toBe("DStr")
    expect(parsed?.courseName).toBe("자료구조")
    expect(parsed?.displayTitle).toBe("자료구조 1교시")
  })

  it("parses STT worker log lines into Korean operational events", () => {
    const [entry] = parsePanelLogText(
      "2026-03-23 01:42:11,123 INFO [job=184 base=260319OOP_1] transcription started",
      [createJob()],
    )

    expect(entry.timeLabel).toBe("01:42:11")
    expect(entry.kind).toBe("transcription_started")
    expect(entry.summary).toBe("전사를 시작했습니다.")
    expect(entry.fileInfo?.displayTitle).toBe("객체지향언어 1교시")
  })

  it("falls back to job file_name when canonical base is not directly parseable", () => {
    const [entry] = parsePanelLogText(
      "2026-03-23 01:44:08,000 INFO [job=7 base=temporary_run__20260323] done",
      [createJob({ id: 7, file_name: "260318LA.txt" })],
    )

    expect(entry.kind).toBe("job_done")
    expect(entry.fileInfo?.courseName).toBe("선형대수학")
    expect(entry.fileInfo?.displayMeta).toBe("2026-03-18(수) · LA · txt")
  })
})
