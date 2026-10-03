import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router";
import { useSession } from "../app/context";

/** Routes behind the sign-in gate. Unauthenticated users go to /signin (replace: no history loop). */
export function RequireAuth({ children }: { children: ReactNode }) {
  const session = useSession();
  const location = useLocation();
  if (session.status !== "authenticated") {
    return <Navigate to="/signin" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
}

/** Only same-app paths are accepted as a post-sign-in destination (no open redirect, never back to /signin). */
export function safeReturnPath(value: unknown): string {
  if (typeof value !== "string") return "/";
  if (!value.startsWith("/") || value.startsWith("//") || value.includes("\\")) return "/";
  if (value === "/signin" || value.startsWith("/signin/") || value.startsWith("/signin?")) return "/";
  return value;
}
