import type { ReactNode } from "react";
import { ApiError, describeError } from "../api/errors";

/** The four explicit data states (Phase 16A §9): loading, empty, error, loaded. No blank regions. */

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state state-loading" role="status" aria-live="polite" aria-busy="true">
      <span className="skeleton" aria-hidden="true" />
      <span className="skeleton skeleton-short" aria-hidden="true" />
      <span className="visually-hidden-text">{label}</span>
    </div>
  );
}

export function EmptyState({ message = "No data available yet" }: { message?: string }) {
  return (
    <div className="state state-empty" role="status">
      <p>{message}</p>
    </div>
  );
}

/** The error panel: a safe message, the API error code, the request id, and Retry when it can help. */
export function ErrorPanel({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const view = describeError(error);
  const code = error instanceof ApiError ? (error.code ?? (error.status !== null ? `HTTP ${String(error.status)}` : error.kind)) : null;
  const requestId = error instanceof ApiError ? error.requestId : null;
  return (
    <div className="state state-error" role="alert">
      <p className="error-title">
        <span aria-hidden="true">⚠ </span>
        {view.title}
      </p>
      <p>{view.detail}</p>
      <dl className="error-meta">
        {code ? (
          <>
            <dt>Code</dt>
            <dd>
              <code>{code}</code>
            </dd>
          </>
        ) : null}
        {requestId ? (
          <>
            <dt>Request id</dt>
            <dd>
              <code>{requestId}</code>
            </dd>
          </>
        ) : null}
      </dl>
      {onRetry ? (
        <button type="button" className="button" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

interface QueryLike<T> {
  data: T | undefined;
  error: unknown;
  isPending: boolean;
  isError: boolean;
  isFetching: boolean;
  refetch: () => unknown;
}

/**
 * Renders a query's region in exactly one of the four states. A background refresh keeps the loaded data visible
 * with a small "refreshing" note; a failed background refresh shows the error panel above the stale data.
 */
export function QueryRegion<T>({
  query,
  isEmpty,
  emptyMessage,
  children,
}: {
  query: QueryLike<T>;
  isEmpty?: (data: T) => boolean;
  emptyMessage?: string;
  children: (data: T) => ReactNode;
}) {
  const retry = (): void => {
    void query.refetch();
  };
  if (query.isPending && !query.isError) return <LoadingState />;
  if (query.data === undefined) return <ErrorPanel error={query.error} onRetry={retry} />;
  const data = query.data;
  return (
    <>
      {query.isError ? <ErrorPanel error={query.error} onRetry={retry} /> : null}
      {isEmpty?.(data) ? <EmptyState {...(emptyMessage ? { message: emptyMessage } : {})} /> : children(data)}
      <p className="refreshing" aria-live="polite">
        {query.isFetching ? "Refreshing…" : ""}
      </p>
    </>
  );
}
