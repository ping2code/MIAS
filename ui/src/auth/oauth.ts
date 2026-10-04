/**
 * Sign-in through OpenShift OAuth (Hardening Task 8).
 *
 * The dashboard is served behind oauth-proxy: every page and API call needs the proxy's session cookie (HttpOnly, so
 * this code never sees it), and nginx adds the MIAS API read token server-side. The browser holds no token at all.
 * This module is the only place that talks to the proxy itself:
 * - `signOut()` ends the proxy session: /oauth/sign_out clears the cookie and returns to "/", which starts a new
 *   OpenShift login (OpenShift may complete it silently while its own login session is still valid).
 * - `checkSession()` asks /oauth/auth (202 signed in, 401 not), to tell an ended session from an outage.
 * - `reauthenticate()` reloads the current page, so the proxy runs the OAuth flow and returns here.
 */
import { safeReturnPath } from "./returnPath";

export type SessionState = "authenticated" | "unauthenticated" | "unknown";

export interface AuthGateway {
  signOut: () => void;
  checkSession: () => Promise<SessionState>;
  reauthenticate: () => void;
}

export interface AuthGatewayOptions {
  fetchImpl?: typeof fetch;
  /** Full page navigation (tests replace it). */
  navigate?: (url: string) => void;
  /** The current in-app location (path and query). */
  currentLocation?: () => string;
}

export const SIGN_OUT_PATH = "/oauth/sign_out";
export const SESSION_CHECK_PATH = "/oauth/auth";

export function createAuthGateway(options: AuthGatewayOptions = {}): AuthGateway {
  const fetchImpl = options.fetchImpl ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));
  const navigate =
    options.navigate ??
    ((url: string): void => {
      window.location.assign(url);
    });
  const currentLocation = options.currentLocation ?? ((): string => window.location.pathname + window.location.search);
  let pending: Promise<SessionState> | null = null;
  let leaving = false;

  async function ask(): Promise<SessionState> {
    try {
      const response = await fetchImpl(SESSION_CHECK_PATH, {
        method: "GET",
        credentials: "same-origin",
        redirect: "manual",
        cache: "no-store",
      });
      if (response.status === 202) return "authenticated";
      if (response.status === 401 || response.type === "opaqueredirect") return "unauthenticated";
      return "unknown";
    } catch {
      return "unknown";
    }
  }

  return {
    signOut: () => {
      if (leaving) return;
      leaving = true;
      navigate(SIGN_OUT_PATH);
    },
    checkSession: () => {
      // One check at a time, however many requests failed together.
      pending ??= ask().finally(() => {
        pending = null;
      });
      return pending;
    },
    reauthenticate: () => {
      if (leaving) return;
      leaving = true;
      navigate(safeReturnPath(currentLocation()));
    },
  };
}
