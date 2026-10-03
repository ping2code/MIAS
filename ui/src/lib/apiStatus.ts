/**
 * Deterministic API status derivation from the latest observation of each health endpoint (Phase 16D).
 *
 * Each observation is the newest result of its query: data, or an error newer than the data. Because the client
 * retries network errors and timeouts twice (1 s, 3 s) before a query fails, a single transient failure never
 * becomes an observation — that is the hysteresis. Rules, first match wins:
 *   1. readiness ready      → "ready" (or "degraded" if liveness's latest observation failed)
 *   2. readiness not_ready  → "not_ready"   (the API answered 503: reachable, not able to serve correctly)
 *   3. both unreachable     → "offline"     (neither health endpoint could be reached)
 *   4. readiness pending    → "checking"
 *   5. anything else        → "degraded"    (e.g. readiness failed unexpectedly, or only one endpoint unreachable)
 */
import { ApiError } from "../api/errors";
import type { ApiResult, LivenessView, ReadinessView } from "../api/types";
import type { ReadinessResult } from "../app/diagnostics";

export type LiveObservation = "pending" | "ok" | "unreachable" | "failed";
export type ReadyObservation = "pending" | ReadinessResult;
export type ApiStatus = "checking" | "ready" | "not_ready" | "degraded" | "offline";
export type Health = "healthy" | "degraded" | "unavailable" | "unknown";

interface QueryLike<T> {
  data: T | undefined;
  error: unknown;
  dataUpdatedAt: number;
  errorUpdatedAt: number;
}

/** The latest result is the error only when it is newer than the data. */
function latestError<T>(q: QueryLike<T>): unknown {
  if (q.error === null || q.error === undefined) return null;
  return q.data === undefined || q.errorUpdatedAt > q.dataUpdatedAt ? q.error : null;
}

export function isUnreachable(error: unknown): boolean {
  return error instanceof ApiError && (error.kind === "network" || error.kind === "timeout");
}

export function observeLive(q: QueryLike<ApiResult<LivenessView>>): LiveObservation {
  const error = latestError(q);
  if (error !== null) return isUnreachable(error) ? "unreachable" : "failed";
  return q.data ? "ok" : "pending";
}

export function observeReady(q: QueryLike<ApiResult<ReadinessView>>): ReadyObservation {
  const error = latestError(q);
  if (error !== null) return isUnreachable(error) ? "unreachable" : "failed";
  if (!q.data) return "pending";
  return q.data.value.status === "ready" ? "ready" : "not_ready";
}

export function deriveApiStatus(live: LiveObservation, ready: ReadyObservation): ApiStatus {
  if (ready === "ready") return live === "unreachable" || live === "failed" ? "degraded" : "ready";
  if (ready === "not_ready") return "not_ready";
  if (ready === "unreachable" && live === "unreachable") return "offline";
  if (ready === "pending") return "checking";
  return "degraded";
}

export const API_STATUS_LABEL: Record<ApiStatus, string> = {
  checking: "Checking",
  ready: "Ready",
  not_ready: "Not ready",
  degraded: "Degraded",
  offline: "Offline",
};

export const API_STATUS_HEALTH: Record<ApiStatus, Health> = {
  checking: "unknown",
  ready: "healthy",
  not_ready: "degraded",
  degraded: "degraded",
  offline: "unavailable",
};

/** Precise wording for each endpoint. Liveness: the process is running. Readiness: it can serve correctly. */
export function describeLive(obs: LiveObservation, error: unknown): { health: Health; text: string } {
  switch (obs) {
    case "ok":
      return { health: "healthy", text: "Live — the API process is running" };
    case "unreachable":
      return { health: "unavailable", text: "API unreachable" };
    case "failed":
      return { health: "unavailable", text: `API liveness check failed${httpSuffix(error)}` };
    case "pending":
      return { health: "unknown", text: "Checking…" };
  }
}

export function describeReady(obs: ReadyObservation, error: unknown): { health: Health; text: string } {
  switch (obs) {
    case "ready":
      return { health: "healthy", text: "Ready — the API can serve correctly" };
    case "not_ready":
      return { health: "degraded", text: "API reachable but not ready (HTTP 503)" };
    case "unreachable":
      return { health: "unavailable", text: "API unreachable" };
    case "failed":
      return { health: "unavailable", text: `API readiness check failed${httpSuffix(error)}` };
    case "pending":
      return { health: "unknown", text: "Checking…" };
  }
}

function httpSuffix(error: unknown): string {
  if (!(error instanceof ApiError)) return "";
  if (error.kind === "invalid_response") return " (unexpected response)";
  return error.status !== null ? ` (HTTP ${String(error.status)})` : "";
}

/** Whether the API answered at all on its latest health observations. */
export function reachability(live: LiveObservation, ready: ReadyObservation): "reachable" | "unreachable" | "unknown" {
  if (live === "ok" || live === "failed" || ready === "ready" || ready === "not_ready" || ready === "failed") return "reachable";
  // Here live is pending or unreachable, and ready is pending or unreachable.
  return live === "unreachable" || ready === "unreachable" ? "unreachable" : "unknown";
}
