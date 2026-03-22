import { useEffect, useState } from "react"

export type ThemeMode = "light" | "dark"

const STORAGE_KEY = "lecture-stt-web-panel-theme"

function readStoredTheme(): ThemeMode | null {
  if (typeof window === "undefined") {
    return null
  }

  const storedValue = window.localStorage.getItem(STORAGE_KEY)
  if (storedValue === "light" || storedValue === "dark") {
    return storedValue
  }
  return null
}

function detectInitialTheme(): ThemeMode {
  const storedTheme = readStoredTheme()
  if (storedTheme) {
    return storedTheme
  }

  if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"
  }

  return "light"
}

export function useThemeMode() {
  const [theme, setTheme] = useState<ThemeMode>(() => detectInitialTheme())

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    window.localStorage.setItem(STORAGE_KEY, theme)
  }, [theme])

  return {
    theme,
    setThemeMode: (nextTheme: ThemeMode) => {
      setTheme(nextTheme)
    },
    toggleTheme: () => {
      setTheme((currentTheme) => (currentTheme === "dark" ? "light" : "dark"))
    },
  }
}
