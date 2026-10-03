import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState, type SubmitEvent } from "react";
import { ApiError } from "../api/errors";
import { queryKeys } from "../api/queries";
import { useApiClient, useServices } from "../app/context";
import { PageHeading } from "../components/PageHeading";
import { clearPages } from "../lib/cursorTrail";

type Problem = "empty" | "mismatch" | "backend";

/**
 * The lock screen (Phase 16E). While locked the protected UI is unmounted, the query cache is cleared and the
 * session hands out no token. Unlocking needs the same read token: it is compared inside the session store, then
 * validated with GET /api/v1/version. A 401 there means the token no longer works, so the session ends and the
 * user signs in again; any other failure keeps the dashboard locked.
 */
export function LockScreen() {
  const client = useApiClient();
  const { session, diagnostics } = useServices();
  const queryClient = useQueryClient();
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  useEffect(() => {
    // After a failed attempt, return focus to the field; the alert is announced and linked via aria-describedby.
    if (problem) inputRef.current?.focus();
  }, [problem]);

  const submit = async (event: SubmitEvent): Promise<void> => {
    event.preventDefault();
    const candidate = token.trim();
    if (candidate === "") {
      setProblem("empty");
      return;
    }
    if (!session.matchesToken(candidate)) {
      setToken("");
      setProblem("mismatch");
      return;
    }
    setBusy(true);
    setProblem(null);
    try {
      const result = await client.version({ candidateToken: candidate });
      queryClient.setQueryData(queryKeys.version, result);
      setToken("");
      session.unlock(result.value);
    } catch (error: unknown) {
      if (error instanceof ApiError && error.kind === "http" && error.status === 401) {
        session.expire();
        queryClient.clear();
        clearPages();
        return;
      }
      setProblem("backend");
    } finally {
      setBusy(false);
    }
  };

  const signOut = (): void => {
    session.signOut();
    queryClient.clear();
    clearPages();
    diagnostics.reset();
  };

  return (
    <div className="signin">
      <form className="signin-card" onSubmit={(e) => void submit(e)} noValidate aria-describedby={`${inputId}-about`}>
        <PageHeading title="MIAS is locked" />
        <p id={`${inputId}-about`} className="hint">
          The dashboard is hidden. Enter the same read token to unlock. Nothing was saved; refreshing the page signs you
          out.
        </p>
        <label htmlFor={inputId}>Read token</label>
        <input
          ref={inputRef}
          id={inputId}
          name="mias-read-token"
          type="password"
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          value={token}
          disabled={busy}
          aria-invalid={problem === "empty" || problem === "mismatch"}
          aria-describedby={problem ? `${inputId}-problem` : undefined}
          onChange={(e) => {
            setToken(e.target.value);
          }}
        />
        {problem ? (
          <div className="state state-error" role="alert" id={`${inputId}-problem`}>
            {problem === "empty" ? <p>Enter the read token.</p> : null}
            {problem === "mismatch" ? <p>That token does not match this session. The dashboard stays locked.</p> : null}
            {problem === "backend" ? <p>MIAS is temporarily unavailable. The dashboard stays locked — try again.</p> : null}
          </div>
        ) : null}
        <div className="form-actions">
          <button type="submit" className="button" disabled={busy}>
            {busy ? "Unlocking…" : "Unlock"}
          </button>
          <button type="button" className="button button-quiet" onClick={signOut} disabled={busy}>
            Sign out
          </button>
        </div>
      </form>
    </div>
  );
}
