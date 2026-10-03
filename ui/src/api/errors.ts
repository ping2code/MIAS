/**
 * The closed error taxonomy of the UI API client and its user-facing messages (Phase 16A §8).
 * Messages never contain headers, tokens, stack traces or raw response bodies.
 */

/** The mias-api closed error codes (Phase 13 §18). */
export type ApiErrorCode =
  | "invalid_request"
  | "unauthorized"
  | "forbidden"
  | "not_found"
  | "method_not_allowed"
  | "ambiguous_latest"
  | "conflict"
  | "payload_too_large"
  | "artifact_invalid"
  | "dependency_unavailable"
  | "internal";

const API_CODES: ReadonlySet<string> = new Set<ApiErrorCode>([
  "invalid_request", "unauthorized", "forbidden", "not_found", "method_not_allowed", "ambiguous_latest",
  "conflict", "payload_too_large", "artifact_invalid", "dependency_unavailable", "internal",
]);

export type ErrorKind = "http" | "network" | "timeout" | "aborted" | "invalid_response";

export class ApiError extends Error {
  override readonly name = "ApiError";
  readonly kind: ErrorKind;
  readonly status: number | null;
  readonly code: ApiErrorCode | null;
  readonly requestId: string | null;

  constructor(kind: ErrorKind, opts: { status?: number; code?: ApiErrorCode | null; requestId?: string | null } = {}) {
    super(kind === "http" ? `HTTP ${String(opts.status ?? 0)}${opts.code ? ` ${opts.code}` : ""}` : kind);
    this.kind = kind;
    this.status = opts.status ?? null;
    this.code = opts.code ?? null;
    this.requestId = opts.requestId ?? null;
  }
}

export function isApiErrorCode(value: unknown): value is ApiErrorCode {
  return typeof value === "string" && API_CODES.has(value);
}

/** Retry only transient failures: network, timeout, 500 internal, 502, 503, 504. Never 4xx or artifact_invalid. */
export function isRetryable(error: ApiError): boolean {
  if (error.kind === "network" || error.kind === "timeout") return true;
  if (error.kind !== "http" || error.status === null) return false;
  if (error.code === "artifact_invalid") return false;
  if (error.status === 500) return error.code === null || error.code === "internal";
  return error.status === 502 || error.status === 503 || error.status === 504;
}

export interface ErrorPresentation {
  title: string;
  detail: string;
  retryable: boolean;
}

/** User-facing wording for an error, by class. */
export function describeError(error: unknown): ErrorPresentation {
  if (!(error instanceof ApiError)) {
    return { title: "Unexpected error", detail: "Something went wrong in the dashboard.", retryable: true };
  }
  switch (error.kind) {
    case "timeout":
      return { title: "Request timed out", detail: "The MIAS API did not answer in time.", retryable: true };
    case "network":
      return { title: "MIAS API unreachable", detail: "A temporary connectivity problem occurred.", retryable: true };
    case "aborted":
      return { title: "Request cancelled", detail: "The request was cancelled.", retryable: true };
    case "invalid_response":
      return { title: "Unexpected response", detail: "The API response was not in the expected format.", retryable: false };
    case "http":
      break;
  }
  switch (error.code) {
    case "unauthorized":
      return { title: "Authentication required", detail: "Your read token is missing, invalid or expired.", retryable: false };
    case "not_found":
      return { title: "Not found", detail: "The requested artifact was not found.", retryable: false };
    case "ambiguous_latest":
      return {
        title: "Ambiguous latest",
        detail: "More than one artifact shares the latest time; see the history instead.",
        retryable: false,
      };
    case "invalid_request":
      return { title: "Request not accepted", detail: "The request parameters were not accepted.", retryable: false };
    case "artifact_invalid":
      return {
        title: "Artifact failed integrity verification",
        detail: "The stored artifact did not verify. Report the request id.",
        retryable: false,
      };
    case "dependency_unavailable":
      return { title: "Service temporarily unavailable", detail: "A dependency of the MIAS API is unavailable.", retryable: true };
    default:
      break;
  }
  if (error.status !== null && error.status >= 500) {
    return { title: "MIAS API error", detail: "The MIAS API is temporarily unavailable.", retryable: isRetryable(error) };
  }
  return { title: "Request failed", detail: "The request could not be completed.", retryable: false };
}
