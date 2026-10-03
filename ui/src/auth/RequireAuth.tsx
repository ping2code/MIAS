import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router";
import { useSession } from "../app/context";
import { LockScreen } from "./LockScreen";
import { safeReturnPath } from "./returnPath";

export { safeReturnPath } from "./returnPath";

/**
 * Routes behind the sign-in gate. Unauthenticated users go to /signin (replace: no history loop) carrying the safe
 * return path; a locked session renders only the lock screen — the protected tree is unmounted, so nothing behind it
 * is in the DOM and no query runs.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const session = useSession();
  const location = useLocation();
  if (session.status !== "authenticated") {
    return <Navigate to="/signin" replace state={{ from: safeReturnPath(location.pathname + location.search) }} />;
  }
  if (session.locked) return <LockScreen />;
  return <>{children}</>;
}
