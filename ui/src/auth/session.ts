/**
 * The in-memory session store (Phase 16A §4, locked for 16B: memory only).
 *
 * The read token lives only in this object's closure. It is never written to localStorage, sessionStorage, cookies,
 * IndexedDB, the URL or the query cache, and is never logged. A page refresh therefore means signing in again.
 *
 * Lock (Phase 16E): an authenticated session can be locked. The token stays in this same closure (nowhere else), but
 * `getToken()` returns null while locked, so no request can carry it, and the UI renders only the lock screen.
 * Unlocking requires the same token again; it is compared here and never copied out.
 */
import type { VersionView } from "../api/types";

export type SessionStatus = "unauthenticated" | "authenticating" | "authenticated";

export interface SessionSnapshot {
  status: SessionStatus;
  /** The API version observed when the token was validated (non-secret). */
  version: VersionView | null;
  /** Why the last session ended, for the sign-in screen (never contains the token). */
  endedReason: "signed_out" | "expired" | null;
  /** Authenticated but hidden behind the lock screen. */
  locked: boolean;
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
  lock: () => void;
  /** True when `candidate` is exactly this session's token (the token itself never leaves the store). */
  matchesToken: (candidate: string) => boolean;
  /** Leave the lock screen after the candidate was verified locally and by the API. */
  unlock: (version: VersionView) => void;
}

const INITIAL: SessionSnapshot = { status: "unauthenticated", version: null, endedReason: null, locked: false };

/** Length-independent comparison loop (timing is not a meaningful threat here, but it costs nothing). */
function sameText(a: string, b: string): boolean {
  let diff = a.length ^ b.length;
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i += 1) diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return diff === 0;
}

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
    getToken: () => (snapshot.status === "authenticated" && !snapshot.locked ? token : null),
    beginAuthentication() {
      token = null;
      set({ status: "authenticating", version: null, endedReason: null, locked: false });
    },
    authenticated(newToken, version) {
      token = newToken;
      set({ status: "authenticated", version, endedReason: null, locked: false });
    },
    failAuthentication() {
      token = null;
      set({ status: "unauthenticated", version: null, endedReason: null, locked: false });
    },
    signOut() {
      token = null;
      set({ status: "unauthenticated", version: null, endedReason: "signed_out", locked: false });
    },
    expire() {
      // Idempotent: concurrent 401s collapse into one transition (no redirect loops, no repeated renders).
      if (snapshot.status !== "authenticated" && token === null) return;
      token = null;
      set({ status: "unauthenticated", version: null, endedReason: "expired", locked: false });
    },
    lock() {
      if (snapshot.status !== "authenticated" || snapshot.locked) return;
      set({ ...snapshot, locked: true });
    },
    matchesToken(candidate) {
      return token !== null && snapshot.status === "authenticated" && sameText(candidate, token);
    },
    unlock(version) {
      if (snapshot.status !== "authenticated" || !snapshot.locked) return;
      set({ ...snapshot, version, locked: false });
    },
  };
}
