import { MutationCache, QueryCache, QueryClient, QueryClientProvider, type Query } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { createApiClient, type ClientOptions } from "../api/client";
import { ApiError } from "../api/errors";
import { queryKeys } from "../api/queries";
import type { ApiResult, ReadinessView } from "../api/types";
import { createSessionStore, type SessionStore } from "../auth/session";
import { isUnreachable } from "../lib/apiStatus";
import { clearPages } from "../lib/cursorTrail";
import { ServicesContext, type AppServices } from "./context";
import { createDiagnosticsStore, type DiagnosticsStore } from "./diagnostics";

function isApiResult(value: unknown): value is ApiResult<unknown> {
  return typeof value === "object" && value !== null && "requestId" in value && "status" in value;
}

/**
 * React Query never retries: the API client owns the retry contract (at most 2, transient errors only).
 * Polling intervals live on the individual queries (src/api/queries.ts).
 */
type QueryEvent<T> = (value: T, query: Query<unknown, unknown, unknown>) => void;

export function createQueryClient(onSuccess?: QueryEvent<unknown>, onError?: QueryEvent<unknown>): QueryClient {
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
const READY_KEY = JSON.stringify(queryKeys.ready);

/** One readiness sample per completed readiness query (each already includes the client's retries). */
function recordReadiness(diagnostics: DiagnosticsStore, now: () => number) {
  return {
    onSuccess: (data: unknown, query: Query<unknown, unknown, unknown>) => {
      if (JSON.stringify(query.queryKey) !== READY_KEY || !isApiResult(data)) return;
      const value = data.value as ReadinessView;
      diagnostics.recordReadiness({
        at: now(),
        result: value.status === "ready" ? "ready" : "not_ready",
        status: data.status,
        checks: value.checks,
        requestId: data.requestId,
      });
    },
    onError: (error: unknown, query: Query<unknown, unknown, unknown>) => {
      if (JSON.stringify(query.queryKey) !== READY_KEY) return;
      if (error instanceof ApiError && error.kind === "aborted") return;
      diagnostics.recordReadiness({
        at: now(),
        result: isUnreachable(error) ? "unreachable" : "failed",
        status: error instanceof ApiError ? error.status : null,
        checks: [],
        requestId: error instanceof ApiError ? error.requestId : null,
      });
    },
  };
}

export function createServices(
  overrides: Partial<Pick<ClientOptions, "fetchImpl" | "sleep" | "timeoutMs" | "backoffMs" | "now">> & {
    session?: SessionStore;
  } = {},
): ServicesInit {
  const session = overrides.session ?? createSessionStore();
  const diagnostics = createDiagnosticsStore();
  const now = overrides.now ?? (() => Date.now());
  const readiness = recordReadiness(diagnostics, now);
  const queryClient = createQueryClient(readiness.onSuccess, readiness.onError);
  const client = createApiClient({
    onRequest: diagnostics.recordRequest,
    onRetry: (info) => {
      diagnostics.recordRetry(info, now());
    },
    now,
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
