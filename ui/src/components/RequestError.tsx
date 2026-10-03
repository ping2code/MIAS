import type { ReactNode } from "react";
import { ApiError } from "../api/errors";
import { ErrorPanel } from "./PageState";

/**
 * The safe error model with domain-specific wording where the meaning is known:
 * 404 → not found (no retry), 409 ambiguous_latest → ambiguity explained, 400 with a cursor → stale page.
 * Everything else falls back to the shared error panel (code, request id, Retry). 401 never reaches here: it signs
 * the user out globally.
 */
export function RequestError({
  error,
  onRetry,
  notFound,
  ambiguous,
  invalid,
}: {
  error: unknown;
  onRetry?: () => void;
  notFound?: ReactNode;
  ambiguous?: ReactNode;
  invalid?: ReactNode;
}) {
  if (error instanceof ApiError && error.kind === "http") {
    const special =
      error.status === 404 && notFound ? notFound : error.code === "ambiguous_latest" && ambiguous ? ambiguous : error.status === 400 && invalid ? invalid : null;
    if (special) {
      return (
        <div className="state state-notice" role="status">
          {special}
          {error.requestId ? (
            <p className="hint">
              Request id <code>{error.requestId}</code>
            </p>
          ) : null}
        </div>
      );
    }
  }
  return <ErrorPanel error={error} {...(onRetry ? { onRetry } : {})} />;
}
