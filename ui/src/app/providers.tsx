import { MutationCache, QueryCache, QueryClient, QueryClientProvider, type Query } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { createApiClient, type ClientOptions } from "../api/client";
import { ApiError } from "../api/errors";
import { queryKeys } from "../api/queries";
import type { ApiResult, ReadinessView } from "../api/types";
import { createAuthGateway, type AuthGateway } from "../auth/oauth";
import { isUnreachable } from "../lib/apiStatus";
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
 * Wires the OAuth gateway, client, diagnostics and query cache together. When a call fails in a way that may mean
 * the oauth-proxy session ended (401, 403, or the proxy's login redirect seen as a network error), the gateway asks
 * the proxy; only if the session is really gone is the cache cleared and the page reloaded into the OpenShift login.
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
    auth?: AuthGateway;
  } = {},
): ServicesInit {
  const auth = overrides.auth ?? createAuthGateway(overrides.fetchImpl ? { fetchImpl: overrides.fetchImpl } : {});
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
    onPossibleSessionLoss: () => {
      void auth.checkSession().then((state) => {
        // The reload discards all in-memory state; clearing the cache first would only trigger a burst of refetches
        // from the still-mounted page until the browser navigates.
        if (state === "unauthenticated") auth.reauthenticate();
      });
    },
    ...(overrides.fetchImpl ? { fetchImpl: overrides.fetchImpl } : {}),
    ...(overrides.sleep ? { sleep: overrides.sleep } : {}),
    ...(overrides.timeoutMs !== undefined ? { timeoutMs: overrides.timeoutMs } : {}),
    ...(overrides.backoffMs ? { backoffMs: overrides.backoffMs } : {}),
  });
  return { services: { client, auth, diagnostics }, queryClient };
}

export function AppProviders({ init, children }: { init?: ServicesInit; children: ReactNode }) {
  const [{ services, queryClient }] = useState<ServicesInit>(() => init ?? createServices());
  return (
    <ServicesContext.Provider value={services}>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </ServicesContext.Provider>
  );
}
