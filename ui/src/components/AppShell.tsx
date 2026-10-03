import { useQueryClient } from "@tanstack/react-query";
import { NavLink, Outlet } from "react-router";
import { useServices, useSession } from "../app/context";
import { API_STATUS_LABEL } from "../lib/apiStatus";
import { useHealth } from "../pages/status/useHealth";
import { UI_BUILD } from "../lib/buildInfo";
import { clearPages } from "../lib/cursorTrail";
import { StatusBadge, type Health } from "./StatusBadge";

export const NAV_ITEMS: readonly { to: string; label: string }[] = [
  { to: "/", label: "Overview" },
  { to: "/market-intelligence", label: "Market Intelligence" },
  { to: "/alerts", label: "Alerts" },
  { to: "/options-intelligence", label: "Options Intelligence" },
  { to: "/trade-setups", label: "Trade Setups" },
  { to: "/invalidation-checks", label: "Invalidation Checks" },
  { to: "/status", label: "System Status" },
];

/**
 * The global API indicator: Checking, Ready, Not ready, Degraded or Offline, derived deterministically from the latest
 * liveness and readiness observations (lib/apiStatus.ts). "Offline" needs both health endpoints unreachable, each
 * after the client's own retries, so one transient failure never shows it.
 */
function ApiStatus() {
  const { status } = useHealth();
  const health: Health = status === "ready" ? "ok" : status === "checking" ? "unknown" : status === "offline" ? "down" : "degraded";
  return (
    <span aria-live="polite" data-api-status={status}>
      <StatusBadge health={health} text={`API: ${API_STATUS_LABEL[status]}`} />
    </span>
  );
}

export function AppShell() {
  const { session, diagnostics } = useServices();
  const queryClient = useQueryClient();
  const snapshot = useSession();
  const signOut = (): void => {
    session.signOut();
    queryClient.clear();
    clearPages();
    diagnostics.reset();
  };
  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <header className="topbar">
        <span className="brand">MIAS Dashboard</span>
        <span className="topbar-status">
          <ApiStatus />
        </span>
        <span className="topbar-build">
          UI build <code>{UI_BUILD}</code>
          {snapshot.version ? (
            <>
              {" · "}API build <code>{snapshot.version.build}</code>
            </>
          ) : null}
        </span>
        <button type="button" className="button button-quiet" onClick={signOut}>
          Sign out
        </button>
      </header>
      <nav className="sidenav" aria-label="Primary">
        <ul>
          {NAV_ITEMS.map((item) => (
            <li key={item.to}>
              <NavLink to={item.to} end={item.to === "/"}>
                {item.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <main id="main" className="main" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}
