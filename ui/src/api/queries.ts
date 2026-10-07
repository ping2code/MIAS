/**
 * Query definitions and polling cadence (Phase 16A §10). Query keys never contain the token.
 * staleTime equals the poll interval; detail and canonical are immutable by id and never re-polled.
 */
import { queryOptions } from "@tanstack/react-query";
import type { ApiClient } from "./client";
import type { ApiResult, ArtifactFamily, HistoryParams, ListResponse } from "./types";

export const HEALTH_INTERVAL_MS = 30_000;
export const VERSION_INTERVAL_MS = 5 * 60_000;
export const HISTORY_FIRST_PAGE_INTERVAL_MS = 60_000;
export const LATEST_INTERVAL_MS = 30_000;

export const queryKeys = {
  live: ["health", "live"] as const,
  ready: ["health", "ready"] as const,
  version: ["api", "version"] as const,
  historyHead: (family: ArtifactFamily) => ["api", family, "history", "head"] as const,
  /** Deterministic: every filter slot is always present (null when unset), in a fixed order. */
  history: (family: ArtifactFamily, filters: HistoryFilters, cursor: string | null) =>
    [
      "api",
      family,
      "history",
      {
        symbol: filters.symbol ?? null,
        asOfFrom: filters.asOfFrom ?? null,
        asOfTo: filters.asOfTo ?? null,
        limit: filters.limit,
        cursor,
      },
    ] as const,
  /** Same layout as history (filters at index 3), so list pages share the placeholder logic. */
  optionsActivity: (filters: HistoryFilters, cursor: string | null) =>
    [
      "api",
      "options-intelligence",
      "activity",
      {
        symbol: filters.symbol ?? null,
        asOfFrom: filters.asOfFrom ?? null,
        asOfTo: filters.asOfTo ?? null,
        limit: filters.limit,
        cursor,
      },
    ] as const,
  deliveries: (id: string) => ["api", "alerts", "deliveries", id] as const,
  latest: (family: ArtifactFamily, symbol: string) => ["api", family, "latest", symbol] as const,
  detail: (family: ArtifactFamily, id: string) => ["api", family, "detail", id] as const,
  canonical: (family: ArtifactFamily, id: string) => ["api", family, "canonical", id] as const,
};

/** The supported history filters (exactly the API's parameters, minus the cursor). */
export interface HistoryFilters {
  symbol?: string;
  asOfFrom?: string;
  asOfTo?: string;
  limit: number;
}

/**
 * One history page. Only the first page is re-polled (60 s); later pages are kept stable so cursors stay valid
 * (Phase 16A §10). Previous data stays visible while the next page loads.
 */
export function historyQuery<F extends ArtifactFamily>(client: ApiClient, family: F, filters: HistoryFilters, cursor: string | null) {
  return pagedQuery(queryKeys.history(family, filters, cursor), cursor === null, (signal) =>
    client.history(family, historyParams(filters, cursor), { signal }),
  );
}

/** A cursor-paged list query: only the first page is re-polled; later pages stay stable so cursors stay valid. */
export function pagedQuery<T>(
  queryKey: readonly unknown[],
  firstPage: boolean,
  fetch: (signal: AbortSignal) => Promise<ApiResult<ListResponse<T>>>,
) {
  return queryOptions({
    queryKey,
    queryFn: ({ signal }) => fetch(signal),
    refetchInterval: firstPage ? HISTORY_FIRST_PAGE_INTERVAL_MS : false,
    staleTime: firstPage ? HISTORY_FIRST_PAGE_INTERVAL_MS : Infinity,
  });
}

export type PagedQuery<T> = ReturnType<typeof pagedQuery<T>>;

function historyParams(filters: HistoryFilters, cursor: string | null): HistoryParams {
  const params: HistoryParams = { limit: filters.limit };
  if (filters.symbol !== undefined) params.symbol = filters.symbol;
  if (filters.asOfFrom !== undefined) params.asOfFrom = filters.asOfFrom;
  if (filters.asOfTo !== undefined) params.asOfTo = filters.asOfTo;
  if (cursor !== null) params.cursor = cursor;
  return params;
}

/** One page of options-intelligence activity views, polled like a history page. */
export function optionsActivityQuery(client: ApiClient, filters: HistoryFilters, cursor: string | null) {
  return pagedQuery(queryKeys.optionsActivity(filters, cursor), cursor === null, (signal) =>
    client.optionsActivity(historyParams(filters, cursor), { signal }),
  );
}

/** Alert delivery receipts: refreshed on demand only (no polling). */
export function deliveriesQuery(client: ApiClient, id: string) {
  return queryOptions({
    queryKey: queryKeys.deliveries(id),
    queryFn: ({ signal }) => client.deliveries(id, { signal }),
    staleTime: HISTORY_FIRST_PAGE_INTERVAL_MS,
  });
}

export function liveQuery(client: ApiClient) {
  return queryOptions({
    queryKey: queryKeys.live,
    queryFn: ({ signal }) => client.live({ signal }),
    refetchInterval: HEALTH_INTERVAL_MS,
    staleTime: HEALTH_INTERVAL_MS,
  });
}

export function readyQuery(client: ApiClient) {
  return queryOptions({
    queryKey: queryKeys.ready,
    queryFn: ({ signal }) => client.ready({ signal }),
    refetchInterval: HEALTH_INTERVAL_MS,
    staleTime: HEALTH_INTERVAL_MS,
  });
}

export function versionQuery(client: ApiClient) {
  return queryOptions({
    queryKey: queryKeys.version,
    queryFn: ({ signal }) => client.version({ signal }),
    refetchInterval: VERSION_INTERVAL_MS,
    staleTime: VERSION_INTERVAL_MS,
  });
}

/** The first history page with limit=1: "has data" and the newest as_of per kind. */
export function historyHeadQuery<F extends ArtifactFamily>(client: ApiClient, family: F) {
  return queryOptions({
    queryKey: queryKeys.historyHead(family),
    queryFn: ({ signal }) => client.history(family, { limit: 1 }, { signal }),
    refetchInterval: HISTORY_FIRST_PAGE_INTERVAL_MS,
    staleTime: HISTORY_FIRST_PAGE_INTERVAL_MS,
  });
}

export function latestQuery<F extends ArtifactFamily>(client: ApiClient, family: F, symbol: string) {
  return queryOptions({
    queryKey: queryKeys.latest(family, symbol),
    queryFn: ({ signal }) => client.latest(family, symbol, undefined, { signal }),
    refetchInterval: LATEST_INTERVAL_MS,
    staleTime: LATEST_INTERVAL_MS,
  });
}

export function detailQuery<F extends ArtifactFamily>(client: ApiClient, family: F, id: string) {
  return queryOptions({
    queryKey: queryKeys.detail(family, id),
    queryFn: ({ signal }) => client.detail(family, id, { signal }),
    staleTime: Infinity,
    gcTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
}

export function canonicalQuery(client: ApiClient, family: ArtifactFamily, id: string) {
  return queryOptions({
    queryKey: queryKeys.canonical(family, id),
    queryFn: ({ signal }) => client.canonical(family, id, { signal }),
    staleTime: Infinity,
    gcTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
}
