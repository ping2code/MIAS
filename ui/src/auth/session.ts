/**
 * The in-memory session store (Phase 16A §4, locked for 16B: memory only).
 *
 * The read token lives only in this object's closure. It is never written to localStorage, sessionStorage, cookies,
 * IndexedDB, the URL or the query cache, and is never logged. A page refresh therefore means signing in again.
 */
import type { VersionView } from "../api/types";

export type SessionStatus = "unauthenticated" | "authenticating" | "authenticated";

export interface SessionSnapshot {
  status: SessionStatus;
  /** The API version observed when the token was validated (non-secret). */
  version: VersionView | null;
  /** Why the last session ended, for the sign-in screen (never contains the token). */
  endedReason: "signed_out" | "expired" | null;
}

export interface SessionStore {
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => SessionSnapshot;
  getToken: () => string | null;
  beginAuthentication: () => void;
  authenticated: (token: string, version: VersionView) => void;
  failAuthentication: () => void;
  signOut: () => void;
  expire: () => void;
}

const INITIAL: SessionSnapshot = { status: "unauthenticated", version: null, endedReason: null };

export function createSessionStore(): SessionStore {
  let token: string | null = null;
  let snapshot: SessionSnapshot = INITIAL;
  const listeners = new Set<() => void>();

  function set(next: SessionSnapshot): void {
    snapshot = next;
    for (const listener of listeners) listener();
  }

  return {
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    getSnapshot: () => snapshot,
    getToken: () => (snapshot.status === "authenticated" ? token : null),
    beginAuthentication() {
      token = null;
      set({ status: "authenticating", version: null, endedReason: null });
    },
    authenticated(newToken, version) {
      token = newToken;
      set({ status: "authenticated", version, endedReason: null });
    },
    failAuthentication() {
      token = null;
      set({ status: "unauthenticated", version: null, endedReason: null });
    },
    signOut() {
      token = null;
      set({ status: "unauthenticated", version: null, endedReason: "signed_out" });
    },
    expire() {
      // Idempotent: concurrent 401s collapse into one transition (no redirect loops, no repeated renders).
      if (snapshot.status !== "authenticated" && token === null) return;
      token = null;
      set({ status: "unauthenticated", version: null, endedReason: "expired" });
    },
  };
}
