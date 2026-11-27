import { afterEach, expect, it, vi } from "vitest"

import { postNotificationSelection } from "../../src/lib/panelApi"

afterEach(() => vi.unstubAllGlobals())

it("rejects retired notification channels before sending a request", async () => {
  const fetchMock = vi.fn()
  vi.stubGlobal("fetch", fetchMock)
  await expect(postNotificationSelection("/api/notification", "discord" as never)).rejects.toThrow("지원하지 않는 알림 채널")
  expect(fetchMock).not.toHaveBeenCalled()
})
