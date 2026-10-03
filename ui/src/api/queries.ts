/**
 * Query definitions and polling cadence (Phase 16A §10). Query keys never contain the token.
 * staleTime equals the poll interval; detail and canonical are immutable by id and never re-polled.
 */
import { queryOptions } from "@tanstack/react-query";
import type { ApiClient } from "./client";
import type { ArtifactFamily } from "./types";

export const HEALTH_INTERVAL_MS = 30_000;
export const VERSION_INTERVAL_MS = 5 * 60_000;
export const HISTORY_FIRST_PAGE_INTERVAL_MS = 60_000;
export const LATEST_INTERVAL_MS = 30_000;

export const queryKeys = {
  live: ["health", "live"] as const,
  ready: ["health", "ready"] as const,
  version: ["api", "version"] as const,
  historyHead: (family: ArtifactFamily) => ["api", family, "history", "head"] as const,
  latest: (family: ArtifactFamily, symbol: string) => ["api", family, "latest", symbol] as const,
  detail: (family: ArtifactFamily, id: string) => ["api", family, "detail", id] as const,
  canonical: (family: ArtifactFamily, id: string) => ["api", family, "canonical", id] as const,
};

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
