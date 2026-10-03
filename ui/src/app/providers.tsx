import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { createApiClient, type ClientOptions } from "../api/client";
import { ApiError, describeError } from "../api/errors";
import type { ApiResult } from "../api/types";
import { createSessionStore, type SessionStore } from "../auth/session";
import { clearPages } from "../lib/cursorTrail";
import { ServicesContext, type AppServices } from "./context";
import { createDiagnosticsStore } from "./diagnostics";

function isApiResult(value: unknown): value is ApiResult<unknown> {
  return typeof value === "object" && value !== null && "requestId" in value && "status" in value;
}

/**
 * React Query never retries: the API client owns the retry contract (at most 2, transient errors only).
 * Polling intervals live on the individual queries (src/api/queries.ts).
 */
export function createQueryClient(onSuccess?: (data: unknown) => void, onError?: (error: unknown) => void): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({
      ...(onSuccess ? { onSuccess } : {}),
      ...(onError ? { onError } : {}),
    }),
    mutationCache: new MutationCache(),
    defaultOptions: {
      queries: {
        retry: false,
        refetchOnWindowFocus: true,
        refetchIntervalInBackground: false,
        refetchOnReconnect: true,
      },
      mutations: { retry: false },
    },
  });
}

export interface ServicesInit {
  services: AppServices;
  queryClient: QueryClient;
}

/**
 * Wires the session, client, diagnostics and query cache together. A 401 from any protected call clears the token
 * and every cached query; the route guard then sends the user to /signin (no navigation from inside the client).
 */
export function createServices(overrides: Partial<Pick<ClientOptions, "fetchImpl" | "sleep" | "timeoutMs" | "backoffMs">> & {
  session?: SessionStore;
} = {}): ServicesInit {
  const session = overrides.session ?? createSessionStore();
  const diagnostics = createDiagnosticsStore();
  const queryClient = createQueryClient(
    (data) => {
      if (isApiResult(data)) diagnostics.recordSuccess(data.requestId);
    },
    (error) => {
      if (error instanceof ApiError && error.kind === "aborted") return;
      diagnostics.recordError(error instanceof ApiError ? error.requestId : null, describeError(error).title);
    },
  );
  const client = createApiClient({
    getToken: session.getToken,
    onUnauthorized: () => {
      session.expire();
      queryClient.clear();
      clearPages();
    },
    ...(overrides.fetchImpl ? { fetchImpl: overrides.fetchImpl } : {}),
    ...(overrides.sleep ? { sleep: overrides.sleep } : {}),
    ...(overrides.timeoutMs !== undefined ? { timeoutMs: overrides.timeoutMs } : {}),
    ...(overrides.backoffMs ? { backoffMs: overrides.backoffMs } : {}),
  });
  return { services: { client, session, diagnostics }, queryClient };
}

export function AppProviders({ init, children }: { init?: ServicesInit; children: ReactNode }) {
  const [{ services, queryClient }] = useState<ServicesInit>(() => init ?? createServices());
  return (
    <ServicesContext.Provider value={services}>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </ServicesContext.Provider>
  );
}
