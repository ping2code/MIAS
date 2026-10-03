/**
 * Client-side diagnostics for the System Status page (Phase 16D). Memory only: nothing is persisted and a reload
 * starts empty. Bounded: the newest MAX_REQUESTS request summaries and MAX_READINESS readiness samples, newest
 * first. Safe: entries are re-built field by field from an allow-list — a route template (validated), status,
 * outcome, timing, attempt count and request id (validated) — so no header, token, query string, body or
 * canonical text can be stored even if a caller passed one.
 */
import type { CheckView } from "../api/types";
import type { Outcome, RequestSummary } from "../api/requestSummary";

export const MAX_REQUESTS = 50;
export const MAX_READINESS = 20;

const ROUTE = /^\/(health\/(live|ready)|api\/v1(\/[a-z-]+)*(\/\{id\}(\/(canonical|deliveries))?)?)$/;
const REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const OUTCOMES: ReadonlySet<Outcome> = new Set(["success", "client_error", "server_error", "network_error", "timeout", "cancelled"]);
const CHECK_NAME = /^[a-z][a-z0-9_]{0,63}$/;
const CHECK_STATUS: ReadonlySet<string> = new Set(["pass", "fail"]);

export type ReadinessResult = "ready" | "not_ready" | "unreachable" | "failed";

export interface ReadinessSample {
  at: number;
  result: ReadinessResult;
  /** HTTP status when a response arrived. */
  status: number | null;
  /** Each check exactly as the API named it (empty when no body arrived). */
  checks: readonly CheckView[];
  requestId: string | null;
}

export interface RetryState {
  route: string;
  attempt: number;
  delayMs: number;
  at: number;
}

export interface DiagnosticsSnapshot {
  /** Newest first, at most MAX_REQUESTS. */
  requests: readonly RequestSummary[];
  /** Newest first, at most MAX_READINESS. */
  readiness: readonly ReadinessSample[];
  /** Kept beyond the bounded list so the last event of each kind is never lost to eviction. */
  lastSuccess: RequestSummary | null;
  lastFailure: RequestSummary | null;
  lastNetworkError: RequestSummary | null;
  lastTimeout: RequestSummary | null;
  lastServerError: RequestSummary | null;
  /** Requests currently waiting to retry, by route template. */
  retrying: readonly RetryState[];
  /** Total logical requests recorded in this tab (the list is bounded; this count is not). */
  total: number;
}

export interface DiagnosticsStore {
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => DiagnosticsSnapshot;
  recordRequest: (summary: RequestSummary) => void;
  recordRetry: (info: { route: string; attempt: number; delayMs: number }, at?: number) => void;
  recordReadiness: (sample: ReadinessSample) => void;
  clearRequests: () => void;
  reset: () => void;
}

const EMPTY: DiagnosticsSnapshot = {
  requests: [],
  readiness: [],
  lastSuccess: null,
  lastFailure: null,
  lastNetworkError: null,
  lastTimeout: null,
  lastServerError: null,
  retrying: [],
  total: 0,
};

function safeRoute(route: unknown): string {
  return typeof route === "string" && ROUTE.test(route) ? route : "(other)";
}

function safeRequestId(id: unknown): string | null {
  return typeof id === "string" && REQUEST_ID.test(id) ? id : null;
}

function finite(n: unknown, fallback: number): number {
  return typeof n === "number" && Number.isFinite(n) ? n : fallback;
}

/** Rebuild a summary from allowed fields only. */
export function sanitizeSummary(summary: RequestSummary): RequestSummary {
  const status = typeof summary.status === "number" && Number.isInteger(summary.status) ? summary.status : null;
  const note = summary.note === "not_ready" || summary.note === "capability_unavailable" ? summary.note : null;
  return {
    startedAt: finite(summary.startedAt, 0),
    method: "GET",
    route: safeRoute(summary.route),
    status,
    outcome: OUTCOMES.has(summary.outcome) ? summary.outcome : "network_error",
    durationMs: Math.max(0, Math.round(finite(summary.durationMs, 0))),
    attempts: Math.max(1, Math.round(finite(summary.attempts, 1))),
    requestId: safeRequestId(summary.requestId),
    note,
  };
}

export function sanitizeReadiness(sample: ReadinessSample): ReadinessSample {
  return {
    at: finite(sample.at, 0),
    result: sample.result,
    status: typeof sample.status === "number" ? sample.status : null,
    checks: sample.checks
      .filter((c) => CHECK_NAME.test(c.name) && CHECK_STATUS.has(c.status))
      .map((c) => ({ name: c.name, status: c.status })),
    requestId: safeRequestId(sample.requestId),
  };
}

/** A failure worth surfacing: not success, not a contract-defined answer (not-ready, missing capability), not a cancellation. */
export function isFailure(summary: RequestSummary): boolean {
  return summary.outcome !== "success" && summary.outcome !== "cancelled" && summary.note === null;
}

export function createDiagnosticsStore(): DiagnosticsStore {
  let snapshot = EMPTY;
  const listeners = new Set<() => void>();
  const set = (next: DiagnosticsSnapshot): void => {
    snapshot = next;
    for (const listener of listeners) listener();
  };
  return {
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    getSnapshot: () => snapshot,
    recordRequest(raw) {
      const s = sanitizeSummary(raw);
      set({
        ...snapshot,
        requests: [s, ...snapshot.requests].slice(0, MAX_REQUESTS),
        lastSuccess: s.outcome === "success" ? s : snapshot.lastSuccess,
        lastFailure: isFailure(s) ? s : snapshot.lastFailure,
        lastNetworkError: s.outcome === "network_error" ? s : snapshot.lastNetworkError,
        lastTimeout: s.outcome === "timeout" ? s : snapshot.lastTimeout,
        lastServerError: s.outcome === "server_error" && s.note === null ? s : snapshot.lastServerError,
        retrying: snapshot.retrying.filter((r) => r.route !== s.route),
        total: snapshot.total + 1,
      });
    },
    recordRetry(info, at = Date.now()) {
      const route = safeRoute(info.route);
      const entry: RetryState = { route, attempt: Math.max(1, Math.round(info.attempt)), delayMs: Math.max(0, Math.round(info.delayMs)), at };
      set({ ...snapshot, retrying: [entry, ...snapshot.retrying.filter((r) => r.route !== route)].slice(0, MAX_READINESS) });
    },
    recordReadiness(raw) {
      set({ ...snapshot, readiness: [sanitizeReadiness(raw), ...snapshot.readiness].slice(0, MAX_READINESS) });
    },
    clearRequests() {
      set({ ...snapshot, requests: [] });
    },
    reset() {
      set(EMPTY);
    },
  };
}
