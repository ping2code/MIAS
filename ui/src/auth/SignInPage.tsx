import { useQueryClient } from "@tanstack/react-query";
import { useId, useState, type SubmitEvent } from "react";
import { Navigate, useLocation } from "react-router";
import { ApiError } from "../api/errors";
import { queryKeys } from "../api/queries";
import { useApiClient, useServices, useSession } from "../app/context";
import { PageHeading } from "../components/PageHeading";
import { safeReturnPath } from "./RequireAuth";

type Problem = { kind: "invalid" } | { kind: "backend"; requestId: string | null } | { kind: "empty" };

/**
 * Sign-in with the shared read token, validated by GET /api/v1/version. The token is held only in component state
 * while typing and then in the in-memory session; nothing is persisted. A 401 here never triggers the global
 * sign-out path (the candidate token is not the session token).
 */
export function SignInPage() {
  const client = useApiClient();
  const { session } = useServices();
  const snapshot = useSession();
  const queryClient = useQueryClient();
  const location = useLocation();
  const inputId = useId();
  const [token, setToken] = useState("");
  const [problem, setProblem] = useState<Problem | null>(null);
  const from = safeReturnPath((location.state as { from?: unknown } | null)?.from);

  if (snapshot.status === "authenticated") return <Navigate to={from} replace />;

  const busy = snapshot.status === "authenticating";

  const submit = async (event: SubmitEvent): Promise<void> => {
    event.preventDefault();
    const candidate = token.trim();
    if (candidate === "") {
      setProblem({ kind: "empty" });
      return;
    }
    setProblem(null);
    session.beginAuthentication();
    try {
      const result = await client.version({ candidateToken: candidate });
      queryClient.clear();
      queryClient.setQueryData(queryKeys.version, result);
      session.authenticated(candidate, result.value);
      // The re-render below redirects to `from` (a single navigation, replace).
    } catch (error: unknown) {
      session.failAuthentication();
      if (error instanceof ApiError && error.kind === "http" && error.status === 401) {
        setToken("");
        setProblem({ kind: "invalid" });
      } else {
        setProblem({ kind: "backend", requestId: error instanceof ApiError ? error.requestId : null });
      }
    }
  };

  return (
    <div className="signin">
      <form className="signin-card" onSubmit={(e) => void submit(e)} noValidate>
        <PageHeading title="Sign in to MIAS" />
        {snapshot.endedReason === "expired" ? (
          <p className="notice" role="status">
            Your session ended because the read token was rejected. Sign in again.
          </p>
        ) : null}
        {snapshot.endedReason === "signed_out" ? (
          <p className="notice" role="status">
            You have signed out.
          </p>
        ) : null}
        <label htmlFor={inputId}>Read token</label>
        <input
          id={inputId}
          name="mias-read-token"
          type="password"
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          value={token}
          disabled={busy}
          aria-invalid={problem?.kind === "invalid" || problem?.kind === "empty"}
          aria-describedby={`${inputId}-help`}
          onChange={(e) => {
            setToken(e.target.value);
          }}
        />
        <p id={`${inputId}-help`} className="hint">
          The token is kept in memory for this tab only. Refreshing the page signs you out.
        </p>
        {problem ? (
          <div className="state state-error" role="alert">
            {problem.kind === "invalid" ? <p>Invalid or expired read token</p> : null}
            {problem.kind === "empty" ? <p>Enter the read token.</p> : null}
            {problem.kind === "backend" ? (
              <>
                <p>The MIAS API is temporarily unavailable. Your token was not rejected; try again shortly.</p>
                {problem.requestId ? (
                  <p>
                    Request id <code>{problem.requestId}</code>
                  </p>
                ) : null}
              </>
            ) : null}
          </div>
        ) : null}
        <button type="submit" className="button" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
