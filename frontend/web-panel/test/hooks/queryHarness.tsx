import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import type { ReactNode } from "react"

export function createQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  })
}

export function createQueryWrapper(queryClient = createQueryClient()) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
}

export function createMutationWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  return createQueryWrapper(queryClient)
}
