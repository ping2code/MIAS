/**
 * The safe, final summary of one logical API request (all attempts included), for client diagnostics.
 *
 * It holds only: when, method, a route template (never a query string, symbol, cursor or id), the final status,
 * the outcome category, the duration, the attempt count and the echoed request id. Never headers, tokens, bodies,
 * canonical bytes or error messages.
 */
import { ApiError } from "./errors";

export type Outcome = "success" | "client_error" | "server_error" | "network_error" | "timeout" | "cancelled";

export interface RequestSummary {
  /** Epoch ms when the logical request started. */
  startedAt: number;
  method: "GET";
  /** e.g. `/health/ready`, `/api/v1/alerts/{id}/canonical`, `/api/v1/market-intelligence` (no query). */
  route: string;
  /** Final HTTP status, or null when no response arrived (network error, timeout, cancellation). */
  status: number | null;
  outcome: Outcome;
  durationMs: number;
  /** 1 + retries performed. */
  attempts: number;
  requestId: string | null;
  /**
   * A non-2xx answer the endpoint's contract defines as information, not failure: readiness 503 (`not_ready`) or
   * deliveries 503 `dependency_unavailable` (`capability_unavailable`).
   */
  note: "not_ready" | "capability_unavailable" | null;
}

/** Outcome from the final HTTP status; transport failures by kind. An unexpected body counts as a server error. */
export function outcomeFor(status: number | null, error: unknown): Outcome {
  if (error instanceof ApiError) {
    if (error.kind === "network") return "network_error";
    if (error.kind === "timeout") return "timeout";
    if (error.kind === "aborted") return "cancelled";
    if (error.kind === "invalid_response") return "server_error";
  } else if (error !== undefined && error !== null) {
    return "network_error";
  }
  if (status === null) return "network_error";
  if (status >= 500) return "server_error";
  if (status >= 400) return "client_error";
  return "success";
}

/** "Recovered after retry": the final attempt succeeded after at least one failed attempt. */
export function recoveredAfterRetry(summary: RequestSummary): boolean {
  return summary.outcome === "success" && summary.attempts > 1;
}
