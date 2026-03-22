import { act, renderHook, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { useThemeMode } from "../src/hooks/useThemeMode"

function installMockLocalStorage() {
  const store = new Map<string, string>()

  Object.defineProperty(window, "localStorage", {
    configurable: true,
    writable: true,
    value: {
      getItem: vi.fn((key: string) => store.get(key) ?? null),
      setItem: vi.fn((key: string, value: string) => {
        store.set(key, value)
      }),
      removeItem: vi.fn((key: string) => {
        store.delete(key)
      }),
      clear: vi.fn(() => {
        store.clear()
      }),
    },
  })
}

function mockMatchMedia(matches: boolean) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation(() => ({
      matches,
      media: "(prefers-color-scheme: dark)",
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
}

describe("useThemeMode", () => {
  beforeEach(() => {
    installMockLocalStorage()
    window.localStorage.clear()
    document.documentElement.removeAttribute("data-theme")
    mockMatchMedia(false)
  })

  it("prefers a stored theme over system preference", async () => {
    window.localStorage.setItem("lecture-stt-web-panel-theme", "dark")
    mockMatchMedia(false)

    const { result } = renderHook(() => useThemeMode())

    expect(result.current.theme).toBe("dark")

    await waitFor(() => {
      expect(document.documentElement.dataset.theme).toBe("dark")
    })
  })

  it("persists toggled theme and updates the document dataset", async () => {
    mockMatchMedia(true)

    const { result } = renderHook(() => useThemeMode())

    await waitFor(() => {
      expect(result.current.theme).toBe("dark")
      expect(document.documentElement.dataset.theme).toBe("dark")
    })

    act(() => {
      result.current.toggleTheme()
    })

    await waitFor(() => {
      expect(result.current.theme).toBe("light")
      expect(document.documentElement.dataset.theme).toBe("light")
      expect(window.localStorage.getItem("lecture-stt-web-panel-theme")).toBe("light")
    })
  })
})
