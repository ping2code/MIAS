/**
 * UI-side request diagnostics for the System Status page: the time of the last successful API response and the
 * request id of the last failure. Kept in memory; holds no tokens, URLs with secrets, or response bodies.
 */
export interface DiagnosticsSnapshot {
  lastSuccessAt: Date | null;
  lastSuccessRequestId: string | null;
  lastErrorAt: Date | null;
  lastErrorRequestId: string | null;
  lastErrorSummary: string | null;
}

export interface DiagnosticsStore {
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => DiagnosticsSnapshot;
  recordSuccess: (requestId: string | null, at?: Date) => void;
  recordError: (requestId: string | null, summary: string, at?: Date) => void;
  reset: () => void;
}

const EMPTY: DiagnosticsSnapshot = {
  lastSuccessAt: null,
  lastSuccessRequestId: null,
  lastErrorAt: null,
  lastErrorRequestId: null,
  lastErrorSummary: null,
};

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
    recordSuccess(requestId, at = new Date()) {
      set({ ...snapshot, lastSuccessAt: at, lastSuccessRequestId: requestId });
    },
    recordError(requestId, summary, at = new Date()) {
      set({ ...snapshot, lastErrorAt: at, lastErrorRequestId: requestId, lastErrorSummary: summary });
    },
    reset() {
      set(EMPTY);
    },
  };
}
